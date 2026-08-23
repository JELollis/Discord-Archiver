import discord
from discord.ext import commands
from discord import app_commands
import datetime
import os
import logging
import urllib.parse
import re

from wiki_client import MediaWikiClient, WikiError, load_config
import discord_export

# Setup logging
log_directory = "logs"
os.makedirs(log_directory, exist_ok=True)
log_filename = os.path.join(log_directory, f"log_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log")
logging.basicConfig(
    filename=log_filename,
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s]: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logging.info("Bot starting up.")

with open("Bot Key.txt", "r", encoding="utf-8") as key_file:
    TOKEN = key_file.readline().strip()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

# ---- MediaWiki archive publishing (lazy client; created + logged in on first use) ----
try:
    WIKI_CONFIG = load_config()
    WIKI_CONFIG_ERROR = None
    logging.info("MediaWiki configuration loaded (namespace: %s).", WIKI_CONFIG["MEDIAWIKI_ARCHIVE_NAMESPACE"])
except WikiError as exc:
    WIKI_CONFIG = None
    WIKI_CONFIG_ERROR = str(exc)
    logging.warning("MediaWiki not configured; wiki commands disabled: %s", exc)

_wiki_client: MediaWikiClient | None = None


async def get_wiki_client() -> MediaWikiClient:
    """Return a logged-in MediaWiki client, creating it on first use."""
    global _wiki_client
    if WIKI_CONFIG is None:
        raise WikiError(WIKI_CONFIG_ERROR or "MediaWiki is not configured on this host.")
    if _wiki_client is None:
        client = MediaWikiClient.from_config(WIKI_CONFIG)
        await client.login()
        _wiki_client = client
    return _wiki_client


def wiki_page_url(title: str) -> str:
    """Public index.php URL for a wiki page title."""
    base = WIKI_CONFIG["MEDIAWIKI_API_URL"].rsplit("/api.php", 1)[0]
    return f"{base}/index.php/" + urllib.parse.quote(title.replace(" ", "_"), safe="/:")


async def capture_channel(channel: discord.TextChannel) -> list:
    """Read a text channel's full history (oldest first) into serialisable dicts."""
    messages = []
    async for m in channel.history(limit=None, oldest_first=True):
        messages.append({
            "id": m.id,
            "author": getattr(m.author, "display_name", str(m.author)),
            "author_username": getattr(m.author, "name", None),
            "author_id": m.author.id,
            "created": m.created_at,
            "edited": m.edited_at,
            "content": discord_export.resolve_mentions(
                m.content,
                channel.guild,
                members={member.id: member for member in getattr(m, "mentions", [])},
                channels={mentioned.id: mentioned for mentioned in getattr(m, "channel_mentions", [])},
                roles={role.id: role for role in getattr(m, "role_mentions", [])},
            ),
            "attachments": [
                {"id": a.id, "filename": a.filename, "size": a.size,
                 "content_type": a.content_type, "_obj": a}
                for a in m.attachments
            ],
            "reply_to": m.reference.message_id if m.reference else None,
        })
    return messages


async def archive_attachments(client: MediaWikiClient, channel_name: str, messages: list) -> int:
    """Upload each attachment to the wiki; annotate dicts with wiki_filename/error."""
    uploaded = 0
    for m in messages:
        for att in m["attachments"]:
            obj = att.pop("_obj", None)
            if obj is None:
                att["error"] = "attachment unavailable"
                continue
            try:
                if att.get("size", 0) > 25 * 1024 * 1024:
                    raise WikiError("attachment exceeds the 25 MiB archive limit")
                data = await obj.read()
                name = discord_export.attachment_upload_name(channel_name, m["id"], att["id"], att["filename"])
                result = await client.upload_file(name, data, comment=f"Discord attachment from #{channel_name}")
                att["wiki_filename"] = result.get("filename") or name
                uploaded += 1
            except Exception as exc:
                att["error"] = str(exc)
                logging.warning("Attachment upload failed (#%s msg %s): %s", channel_name, m["id"], exc)
    return uploaded


async def is_channel_archived(client: MediaWikiClient, channel) -> bool:
    """True if a verified archive page for this channel exists on the wiki.

    Voice/stage channels have no message history and are exempt. Forum and
    other message-bearing channels are never considered archived by the v1 exporter.
    """
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return True
    if not isinstance(channel, discord.TextChannel):
        return False
    title = discord_export.make_page_title(channel.name, WIKI_CONFIG["MEDIAWIKI_ARCHIVE_NAMESPACE"])
    page = await client.get_page(title)
    if not page:
        return False
    marker = re.search(rf"source_channel_id={channel.id}[^\n]*captured_until=(\d+)", page["content"])
    if not marker:
        return False
    latest = None
    async for message in channel.history(limit=1, oldest_first=False):
        latest = message
        break
    return latest is None or latest.id <= int(marker.group(1))

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}!')
    print(f'Logged in as {bot.user}!')
    try:
        # Sync commands globally
        await tree.sync()
        logging.info("Slash commands synchronized globally.")
    except Exception as e:
        logging.error("Error syncing commands: %s", str(e))
        print(f"Error syncing commands: {str(e)}")

# Define slash command to archive
@tree.command(name="archive", description="Archive channels and categories based on a term and year.")
@app_commands.default_permissions(manage_channels=True)
@app_commands.guild_only()
@app_commands.describe(term="The term to archive (e.g., Spring, Summer, Fall)", year="The year (e.g., 2025)")
async def archive(interaction: discord.Interaction, term: str, year: int):
    responded = False
    try:
        # Defer the interaction to allow processing time
        await interaction.response.defer(ephemeral=True)
        responded = True
        logging.debug("Archive command invoked with term: %s, year: %d", term, year)

        # Validate inputs
        if term.lower() not in ["spring", "summer", "fall"]:
            await interaction.followup.send("Invalid term. Please specify: `Spring`, `Summer`, or `Fall`.", ephemeral=True)
            return
        if not (1900 <= year <= 2100):
            await interaction.followup.send("Invalid year. Please provide a valid year (e.g., 2025).", ephemeral=True)
            return

        term = term.lower()
        archive_category_name = f"{term.capitalize()} {year} Archive"
        guild = interaction.guild
        if guild is None or not interaction.user.guild_permissions.manage_channels:
            await interaction.followup.send("You need the Manage Channels permission to use `/archive`.", ephemeral=True)
            return

        # Fetch the Verified role
        verified_role = discord.utils.get(guild.roles, name="Verified")

        if not verified_role:
            await interaction.followup.send("The 'Verified' role does not exist. Please create it first.", ephemeral=True)
            logging.error("Verified role not found.")
            return

        # Create the archive category if it doesn't exist
        archive_category = discord.utils.get(guild.categories, name=archive_category_name)
        if not archive_category:
            archive_category = await guild.create_category(archive_category_name)
            await archive_category.set_permissions(guild.default_role, read_messages=False, send_messages=False)
            await archive_category.set_permissions(verified_role, read_messages=True, send_messages=False, read_message_history=True)
            logging.info("Archive category '%s' created.", archive_category_name)

        # Find and move matching channels
        moved_channels = []
        for channel in guild.text_channels:
            logging.debug("Checking channel: %s", channel.name)
            if f"-{term}-{year}" in channel.name.lower():
                await channel.edit(category=archive_category)

                # Clear all existing permissions
                for target in list(channel.overwrites.keys()):
                    await channel.set_permissions(target, overwrite=None)

                # Apply archive-specific permissions
                await channel.set_permissions(guild.default_role, read_messages=False, send_messages=False)
                await channel.set_permissions(verified_role, read_messages=True, send_messages=False, read_message_history=True)
                moved_channels.append(channel.name)
                logging.info("Channel '%s' moved to archive and permissions updated.", channel.name)

        if moved_channels:
            await interaction.followup.send(f"Archived channels: {', '.join(moved_channels)}.", ephemeral=True)
        else:
            await interaction.followup.send("No channels found matching the specified term and year.", ephemeral=True)
        logging.info("Archive process completed for %s %d.", term, year)

    except Exception as e:
        logging.error("An error occurred in archive: %s", str(e))
        try:
            if not responded:
                await interaction.response.send_message(f"An error occurred: {str(e)}", ephemeral=True)
            else:
                await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)
        except discord.errors.InteractionResponded:
            logging.warning("Interaction already responded when handling archive error.")

# Define slash command to populate
@tree.command(name="populate", description="Create categories and channels dynamically.")
@app_commands.default_permissions(manage_channels=True)
@app_commands.guild_only()
@app_commands.describe(
    category="Select or create a category",
    term="Specify the term (Spring, Summer, Fall)",
    year="Specify the year (e.g., 2025)",
    courses="Comma-separated list of course numbers"
)
@app_commands.choices(category=[
    app_commands.Choice(name="CPT", value="CPT"),
    app_commands.Choice(name="CYB", value="CYB"),
    app_commands.Choice(name="IST", value="IST"),
    app_commands.Choice(name="SPC", value="SPC"),
    app_commands.Choice(name="SOC", value="SOC"),
    app_commands.Choice(name="HSS", value="HSS"),
    app_commands.Choice(name="HIS", value="HIS")
])
async def populate(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    term: str,
    year: int,
    courses: str
):
    responded = False
    try:
        # Defer the interaction to allow processing time
        await interaction.response.defer(ephemeral=True)
        responded = True
        logging.debug("Populate command invoked with category: %s, term: %s, year: %d, courses: %s",
                      category.value, term, year, courses)

        # Validate the term
        if term.lower() not in ["spring", "summer", "fall"]:
            await interaction.followup.send("Invalid term. Please specify: `Spring`, `Summer`, or `Fall`.", ephemeral=True)
            return

        if not (1900 <= year <= 2100):
            await interaction.followup.send("Invalid year. Please provide a valid year (e.g., 2025).", ephemeral=True)
            return

        course_numbers = [course.strip() for course in courses.split(",") if course.strip().isdigit()]
        if not course_numbers:
            await interaction.followup.send("No valid course numbers provided. Please provide a comma-separated list of numbers.", ephemeral=True)
            return

        # Create the category if it does not exist
        guild = interaction.guild
        if guild is None or not interaction.user.guild_permissions.manage_channels:
            await interaction.followup.send("You need the Manage Channels permission to use `/populate`.", ephemeral=True)
            return
        category_name = category.value
        existing_category = discord.utils.get(guild.categories, name=category_name)
        if not existing_category:
            existing_category = await guild.create_category(category_name)
            logging.info("Category '%s' created.", category_name)

        # Create channels under the category as private and assign roles
        lab_tech_role = discord.utils.get(guild.roles, name="Lab Tech")
        if lab_tech_role is None:
            logging.warning("Lab Tech role not found; channels will be created without Lab Tech access.")

        created_channels = []
        skipped_courses = []
        for course_number in course_numbers:
            channel_name = f"{category_name}-{course_number}-{term.capitalize()}-{year}"
            existing_channel = next(
                (c for c in guild.text_channels if c.name.casefold() == channel_name.casefold()),
                None,
            )
            if not existing_channel:
                role_name = f"{category_name}-{course_number}"
                role = discord.utils.get(guild.roles, name=role_name)
                if not role:
                    logging.warning("Role '%s' not found for channel '%s'.", role_name, channel_name)
                    skipped_courses.append(course_number)
                    continue
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    role: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }
                if lab_tech_role is not None:
                    overwrites[lab_tech_role] = discord.PermissionOverwrite(
                        read_messages=True,
                        send_messages=True,
                        read_message_history=True
                    )
                new_channel = await guild.create_text_channel(name=channel_name, category=existing_category, overwrites=overwrites)
                created_channels.append(new_channel.name)
                logging.info("Channel '%s' created as private with role '%s' assigned.", new_channel.name, role_name)

        if created_channels:
            message = f"Created private channels with roles: {', '.join(created_channels)}"
            if lab_tech_role is None:
                message += "\n⚠️ The `Lab Tech` role was not found, so these channels were created without Lab Tech access."
            if skipped_courses:
                message += f"\n⚠️ Skipped missing roles for courses: {', '.join(skipped_courses)}"
            await interaction.followup.send(message, ephemeral=True)
        else:
            message = "No new channels were created. All channels already exist or roles were missing."
            if skipped_courses:
                message += f"\n⚠️ Skipped missing roles for courses: {', '.join(skipped_courses)}"
            await interaction.followup.send(message, ephemeral=True)

    except Exception as e:
        logging.error("An error occurred in populate: %s", str(e))
        try:
            if not responded:
                await interaction.response.send_message(f"An error occurred: {str(e)}", ephemeral=True)
            else:
                await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)
        except discord.errors.InteractionResponded:
            logging.warning("Interaction already responded when handling populate error.")

# ---- Shared permissions helper for course roles ----
def build_course_permissions() -> discord.Permissions:
    perms = discord.Permissions.none()

    # Text / Threads / Forum “Posts”
    perms.view_channel = True
    perms.change_nickname = True
    perms.send_messages = True
    perms.send_messages_in_threads = True
    perms.create_public_threads = True
    if hasattr(discord.Permissions, "create_forum_threads"):
        perms.create_forum_threads = True  # forum posts

    perms.embed_links = True
    perms.attach_files = True
    perms.add_reactions = True
    perms.read_message_history = True
    perms.use_application_commands = True

    # Emoji/Stickers
    perms.use_external_emojis = True
    if hasattr(discord.Permissions, "use_external_stickers"):
        perms.use_external_stickers = True

    # Voice/Activities
    perms.connect = True
    perms.speak = True
    perms.stream = True  # “Video”/Go Live
    if hasattr(discord.Permissions, "use_embedded_activities"):
        perms.use_embedded_activities = True

    return perms


# ---- /add_role command (creates missing; updates permissions on existing) ----
@tree.command(name="add_role", description="Create or update course roles for a category (e.g., CYB 110, 201, 269)")
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
@app_commands.describe(
    category="Course category (CPT, CYB, IST, SPC, SOC, HSS, HIS)",
    courses="Comma-separated list of course numbers (e.g., 110, 201, 269)"
)
@app_commands.choices(category=[
    app_commands.Choice(name="CPT", value="CPT"),
    app_commands.Choice(name="CYB", value="CYB"),
    app_commands.Choice(name="IST", value="IST"),
    app_commands.Choice(name="SPC", value="SPC"),
    app_commands.Choice(name="SOC", value="SOC"),
    app_commands.Choice(name="HSS", value="HSS"),
    app_commands.Choice(name="HIS", value="HIS"),
])
async def add_role(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    courses: str
):
    responded = False
    try:
        await interaction.response.defer(ephemeral=True)
        responded = True

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command must be used in a server.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.followup.send("You need the Manage Roles permission to use `/add_role`.", ephemeral=True)
            return

        # Parse numbers
        course_numbers = [c.strip() for c in courses.split(",") if c.strip().isdigit()]
        if not course_numbers:
            await interaction.followup.send("No valid course numbers provided. Use a comma-separated list of numbers.", ephemeral=True)
            return

        perms = build_course_permissions()
        created, updated, unchanged = [], [], []

        for num in course_numbers:
            role_name = f"{category.value}-{num}"
            role = discord.utils.get(guild.roles, name=role_name)

            # Create if missing
            if role is None:
                try:
                    role = await guild.create_role(
                        name=role_name,
                        permissions=perms,
                        reason=f"Auto-created by /add_role for {category.value} {num}"
                    )
                    created.append(role_name)
                    continue
                except discord.Forbidden:
                    await interaction.followup.send(
                        f"❌ Missing permissions to create role `{role_name}` (need Manage Roles, and my top role must be above it).",
                        ephemeral=True
                    )
                    return
                except Exception as e:
                    await interaction.followup.send(f"❌ Error creating `{role_name}`: {e}", ephemeral=True)
                    return

            # Update perms if different
            try:
                if role.permissions != perms:
                    await role.edit(permissions=perms, reason=f"Sync perms via /add_role for {category.value} {num}")
                    updated.append(role_name)
                else:
                    unchanged.append(role_name)
            except discord.Forbidden:
                await interaction.followup.send(
                    f"❌ I can't edit `{role_name}`. Ensure I have Manage Roles and my top role is above `{role_name}`.",
                    ephemeral=True
                )
                return
            except Exception as e:
                await interaction.followup.send(f"❌ Error updating `{role_name}`: {e}", ephemeral=True)
                return

        # Summary
        lines = []
        if created:   lines.append(f"✅ Created: {', '.join(created)}")
        if updated:   lines.append(f"🔁 Updated perms: {', '.join(updated)}")
        if unchanged: lines.append(f"✔️ Already correct: {', '.join(unchanged)}")
        if not lines: lines.append("No changes made.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    except Exception as e:
        logging.error("An error occurred in add_role: %s", str(e))
        try:
            if not responded:
                await interaction.response.send_message(f"An error occurred: {str(e)}", ephemeral=True)
            else:
                await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)
        except discord.errors.InteractionResponded:
            logging.warning("Interaction already responded when handling add_role error.")


# ---- /delete confirmation UI ----
class DeleteConfirmationView(discord.ui.View):
    """Confirmation controls and deletion state for a single `/delete` request.

    Only the member who invoked the command may use the controls. Channels are
    deleted before categories so category deletion never leaves selected child
    channels behind. Individual failures are collected and reported instead of
    stopping the remainder of the deletion operation.
    """

    def __init__(self, requester_id: int, channels, categories):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.channels = channels
        self.categories = categories

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Prevent anyone except the original requester from confirming/cancelling."""
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the user who ran `/delete` can confirm this deletion.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Perform the confirmed deletion and report successes/failures."""
        # Acknowledge immediately because a large category can take several seconds.
        await interaction.response.defer(ephemeral=True)

        # Disable the controls so the same deletion cannot be submitted twice.
        for child in self.children:
            child.disabled = True

        deleted_channels = []
        deleted_categories = []
        failures = []

        # Delete child/standalone channels first, then their categories.
        for channel in self.channels:
            try:
                await channel.delete(reason=f"Bulk deletion requested by {interaction.user} ({interaction.user.id})")
                deleted_channels.append(channel.name)
                logging.info("Channel '%s' (%s) deleted by %s (%s).", channel.name, channel.id, interaction.user, interaction.user.id)
            except Exception as e:
                failures.append(f"channel `{channel.name}`: {e}")
                logging.exception("Failed deleting channel '%s' (%s).", channel.name, channel.id)

        # Categories are removed after all selected child channels have been attempted.
        for category in self.categories:
            try:
                await category.delete(reason=f"Bulk deletion requested by {interaction.user} ({interaction.user.id})")
                deleted_categories.append(category.name)
                logging.info("Category '%s' (%s) deleted by %s (%s).", category.name, category.id, interaction.user, interaction.user.id)
            except Exception as e:
                failures.append(f"category `{category.name}`: {e}")
                logging.exception("Failed deleting category '%s' (%s).", category.name, category.id)

        # Replace the confirmation prompt with the final result.
        lines = [f"Deletion complete: {len(deleted_categories)} categor{'y' if len(deleted_categories) == 1 else 'ies'} and {len(deleted_channels)} channel{'s' if len(deleted_channels) != 1 else ''} deleted."]
        if failures:
            lines.append("Failures:\n" + "\n".join(f"- {failure}" for failure in failures[:15]))
        await interaction.edit_original_response(content="\n".join(lines), view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel the pending deletion without changing the server."""
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Deletion cancelled. Nothing was deleted.", view=self)
        self.stop()

    async def on_timeout(self):
        """Disable the local controls when the 60-second confirmation window expires."""
        for child in self.children:
            child.disabled = True


# ---- /delete command (bulk category/channel deletion with confirmation) ----
@tree.command(name="delete", description="Bulk-delete channels, categories, or both by name.")
@app_commands.describe(
    target_type="What kind of targets to delete",
    targets="Comma-separated list of channel/category names"
)
@app_commands.choices(target_type=[
    app_commands.Choice(name="Category", value="category"),
    app_commands.Choice(name="Channel", value="channel"),
    app_commands.Choice(name="Both", value="both"),
])
@app_commands.default_permissions(manage_channels=True)
@app_commands.guild_only()
async def delete(interaction: discord.Interaction, target_type: app_commands.Choice[str], targets: str):
    """Resolve requested Discord channels/categories and request deletion confirmation.

    `targets` is a comma-separated list matched case-insensitively by exact
    Discord name. Category mode selects each matching category plus all child
    channels. Channel mode selects only matching non-category channels. Both
    mode allows either kind of target. No destructive action occurs until the
    invoking member presses the confirmation button.
    """
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    # The decorator controls command visibility, while this runtime check also
    # protects execution if Discord's cached command permissions are stale.
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("You need the Manage Channels permission to use `/delete`.", ephemeral=True)
        return

    # Normalize the comma-separated input while preserving original spelling
    # for useful missing-target messages.
    names = [name.strip() for name in targets.split(",") if name.strip()]
    if not names:
        await interaction.followup.send("Provide at least one channel or category name.", ephemeral=True)
        return

    # Case-insensitive exact-name matching. Duplicate names are all included so
    # the confirmation accurately represents what Discord will delete.
    wanted = {name.casefold() for name in names}
    matched_categories = []
    matched_channels = []

    # Resolve categories when Category or Both mode was selected.
    if target_type.value in ("category", "both"):
        matched_categories = [category for category in guild.categories if category.name.casefold() in wanted]

    # Resolve every non-category Discord channel type in Channel or Both mode.
    if target_type.value in ("channel", "both"):
        matched_channels = [channel for channel in guild.channels if not isinstance(channel, discord.CategoryChannel) and channel.name.casefold() in wanted]

    # Selecting a category means deleting the category and everything in it.
    # Include all channel types exposed through CategoryChannel.channels.
    for category in matched_categories:
        for channel in category.channels:
            if channel not in matched_channels:
                matched_channels.append(channel)

    # Determine which user-supplied names never resolved to an applicable target.
    found_names = {category.name.casefold() for category in matched_categories}
    found_names.update(channel.name.casefold() for channel in matched_channels)
    missing = [name for name in names if name.casefold() not in found_names]

    if not matched_categories and not matched_channels:
        await interaction.followup.send("No matching channels or categories were found. Nothing was deleted.", ephemeral=True)
        return

    # Safety gate (Auth_plan): every selected TEXT channel must already have a
    # verified wiki archive before it can be deleted. Non-text channels are exempt.
    try:
        wiki = await get_wiki_client()
    except Exception as exc:
        await interaction.followup.send(
            f"🛑 Cannot verify wiki archives ({exc}). Deletion blocked for safety.", ephemeral=True)
        return
    unarchived = [
        ch.name for ch in matched_channels
        if isinstance(ch, discord.TextChannel) and not await is_channel_archived(wiki, ch)
    ]
    if unarchived:
        listing = ", ".join(f"`{n}`" for n in unarchived[:20])
        more = "" if len(unarchived) <= 20 else f" (+{len(unarchived) - 20} more)"
        await interaction.followup.send(
            "🛑 Deletion blocked — these channels have no verified wiki archive yet:\n"
            f"{listing}{more}\n\nRun `/publish` on each channel first.", ephemeral=True)
        return

    # List categories and explicitly selected standalone channels separately;
    # child channels are represented by the total count to keep the prompt sane.
    category_names = ", ".join(f"`{category.name}`" for category in matched_categories) or "None"
    standalone_channels = [channel for channel in matched_channels if channel.category not in matched_categories]
    channel_names = ", ".join(f"`{channel.name}`" for channel in standalone_channels) or "None"

    # Present the complete impact before creating the destructive controls.
    message = (
        "**Permanent deletion confirmation**\n"
        f"This will permanently delete **{len(matched_categories)}** categor{'y' if len(matched_categories) == 1 else 'ies'} "
        f"and **{len(matched_channels)}** channel{'s' if len(matched_channels) != 1 else ''}.\n\n"
        f"Categories: {category_names}\n"
        f"Standalone channels: {channel_names}"
    )
    if missing:
        message += "\n\nNot found: " + ", ".join(f"`{name}`" for name in missing)
    message += "\n\nThis cannot be undone."

    # Log both the original request and the resolved Discord objects for auditability.
    logging.info("Delete requested by %s (%s): type=%s targets=%s; resolved categories=%s channels=%s missing=%s", interaction.user, interaction.user.id, target_type.value, names, [c.name for c in matched_categories], [c.name for c in matched_channels], missing)

    # Only the invoking member can activate this confirmation view.
    view = DeleteConfirmationView(interaction.user.id, matched_channels, matched_categories)
    await interaction.followup.send(message, view=view, ephemeral=True)


# ---- Command help registry (drives /help and the admin guide) ----
COMMAND_HELP = {
    "publish": {
        "summary": "Archive channel(s) to the wiki — a single channel, a whole category, or a name list.",
        "usage": "/publish [channel:<#channel>] [category:<category>] [channels:<name,name,...>]",
        "params": [
            "channel — (optional) a single text channel.",
            "category — (optional) archive every text channel in this category.",
            "channels — (optional) comma-separated channel names to archive.",
            "With none given, archives the channel you run it in. Inputs combine and de-duplicate.",
        ],
        "examples": [
            "/publish",
            "/publish channel:#cpt-257-summer-2023",
            "/publish category:Summer 2023 Archive",
            "/publish channels:cpt-257-summer-2023, ist-201-summer-2023",
        ],
        "notes": (
            "For each channel: reads full history, uploads attachments, renders a page at "
            "Archive:DEPT-NUM/Term Year (or Archive:Misc/<name>), then reads it back to verify. Verified pages "
            "post to #archives and become eligible for /delete; failures are listed and are NOT marked deletable. "
            "Results post to #archives as each finishes. Requires Manage Channels."
        ),
    },
    "delete": {
        "summary": "Permanently delete channels and/or categories (guarded by confirmation and wiki archive).",
        "usage": "/delete target_type:<Category|Channel|Both> targets:<comma,separated,names>",
        "params": [
            "target_type — Category (a category + all its channels), Channel (named channels only), or Both.",
            "targets — comma-separated, exact channel/category names (case-insensitive).",
        ],
        "examples": [
            "/delete target_type:Channel targets:cpt-257-summer-2023, ist-201-summer-2023",
            "/delete target_type:Category targets:Summer 2023 Archive",
        ],
        "notes": (
            "SAFETY: every selected TEXT channel must already have a verified wiki archive (via /publish) or the "
            "deletion is blocked. A confirmation with counts appears and expires after 60 seconds; only the admin "
            "who ran it can confirm. Deletion is permanent. Requires Manage Channels."
        ),
    },
    "publish_help_placeholder": None,  # (kept intentionally out; see below)
    "archive": {
        "summary": "Move a term's Discord channels into an archive category and lock them to read-only.",
        "usage": "/archive term:<Spring|Summer|Fall> year:<YYYY>",
        "params": [
            "term — Spring, Summer, or Fall.",
            "year — 4-digit year, e.g. 2025.",
        ],
        "examples": ["/archive term:Summer year:2023"],
        "notes": (
            "Discord-side only: moves channels whose name contains -<term>-<year> into a '<Term> <Year> Archive' "
            "category and grants the Verified role read-only access. This does NOT publish to the wiki — use "
            "/publish for the permanent wiki archive."
        ),
    },
    "populate": {
        "summary": "Create course channels under a category, each restricted to its course role.",
        "usage": "/populate category:<CPT|CYB|IST|SPC|SOC|HSS|HIS> term:<Spring|Summer|Fall> year:<YYYY> courses:<nums>",
        "params": [
            "category — department (CPT, CYB, IST, SPC, SOC, HSS, HIS).",
            "term — Spring, Summer, or Fall.",
            "year — 4-digit year.",
            "courses — comma-separated course numbers, e.g. 113, 127, 170.",
        ],
        "examples": [
            "/populate category:CPT term:Spring year:2027 courses:113, 127, 170, 209",
        ],
        "notes": (
            "Each channel <DEPT>-<num>-<Term>-<Year> is created private to the matching <DEPT>-<num> role (which "
            "must already exist — see /add_role) plus Lab Tech. Missing course roles are skipped with a warning."
        ),
    },
    "add_role": {
        "summary": "Create or update course roles for a department with the standard student permission set.",
        "usage": "/add_role category:<CPT|CYB|IST|SPC|SOC|HSS|HIS> courses:<nums>",
        "params": [
            "category — department prefix.",
            "courses — comma-separated course numbers.",
        ],
        "examples": ["/add_role category:CYB courses:110, 201, 269"],
        "notes": (
            "Creates <DEPT>-<num> roles that are missing and syncs permissions on existing ones. Run this before "
            "/populate so the channels can be locked to their course roles. Requires Manage Roles."
        ),
    },
    "wiki_status": {
        "summary": "Check the archive wiki connection and the bot's wiki permissions.",
        "usage": "/wiki_status",
        "params": [],
        "examples": ["/wiki_status"],
        "notes": "Reports the MediaWiki version, the bot's wiki user/groups, whether it can edit and upload, and the archive namespace. Requires Manage Channels.",
    },
    "help": {
        "summary": "List all commands, or show detailed help and examples for one.",
        "usage": "/help [command:<name>]",
        "params": ["command — (optional) a command name to get full details for."],
        "examples": ["/help", "/help command:publish"],
        "notes": "With no argument, lists every command. With a command name, shows its usage, parameters, examples, and notes.",
    },
}
# Drop the placeholder key used only to keep insertion order readable.
COMMAND_HELP.pop("publish_help_placeholder", None)

# Order commands are listed in /help (grouped: wiki, then Discord management, then help).
_HELP_ORDER = ["publish", "wiki_status", "delete", "archive", "populate", "add_role", "help"]


@tree.command(name="help", description="List all bot commands, or get detailed help for one.")
@app_commands.describe(command="(optional) a specific command to get full help for")
@app_commands.choices(command=[app_commands.Choice(name=c, value=c) for c in _HELP_ORDER])
@app_commands.guild_only()
async def help_command(interaction: discord.Interaction, command: app_commands.Choice[str] | None = None):
    await interaction.response.defer(ephemeral=True)
    if command is None:
        lines = ["**GTC Archive Bot — command list**", ""]
        for name in _HELP_ORDER:
            info = COMMAND_HELP.get(name)
            if info:
                lines.append(f"• **/{name}** — {info['summary']}")
        lines.append("")
        lines.append("Use `/help command:<name>` for usage, parameters, and examples.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    info = COMMAND_HELP.get(command.value)
    if not info:
        await interaction.followup.send(f"No help found for `/{command.value}`.", ephemeral=True)
        return
    lines = [f"**/{command.value}** — {info['summary']}", "", f"**Usage:** `{info['usage']}`"]
    if info.get("params"):
        lines.append("")
        lines.append("**Parameters:**")
        lines.extend(f"• {p}" for p in info["params"])
    if info.get("examples"):
        lines.append("")
        lines.append("**Examples:**")
        lines.extend(f"`{e}`" for e in info["examples"])
    if info.get("notes"):
        lines.append("")
        lines.append(f"**Notes:** {info['notes']}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ---- publish helper (archive one channel to the wiki, with read-back verification) ----
async def publish_channel_to_wiki(client: MediaWikiClient, channel: discord.TextChannel) -> dict:
    """Capture, upload, render, publish and verify one channel. Returns a result dict."""
    messages = await capture_channel(channel)
    uploaded = await archive_attachments(client, channel.name, messages)
    namespace = WIKI_CONFIG["MEDIAWIKI_ARCHIVE_NAMESPACE"]
    title = discord_export.make_page_title(channel.name, namespace)
    existing = await client.get_page(title)
    if existing:
        other = re.search(r"source_channel_id=(\d+)", existing["content"])
        if other and int(other.group(1)) != channel.id:
            raise WikiError(f"archive page {title!r} belongs to another Discord channel")
    captured_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    meta = {
        "channel_id": channel.id,
        "captured_at": captured_at,
        "captured_until": max((m["id"] for m in messages), default=0),
        "message_count": len(messages),
        "complete": all("error" not in att for m in messages for att in m["attachments"]),
    }
    course_match = re.match(r"^([a-z]+)-(\d+)-(spring|summer|fall)-(\d{4})$", channel.name.strip(), re.I)
    if course_match:
        meta["category"] = f"{course_match.group(1).upper()}-{course_match.group(2)}"
    parts = discord_export.split_messages(messages, channel.name, meta)
    if course_match:
        course = f"{course_match.group(1).upper()}-{course_match.group(2)}"
        department = course_match.group(1).upper()
        for category_title, category_text in (
            (f"Category:{course}", f"[[Category:{department}]]\n"),
            (f"Category:{department}", "[[Category:Discord archive]]\n"),
        ):
            try:
                await client.edit_page(category_title, category_text, "Create archive category", createonly=True)
            except WikiError as exc:
                if "articleexists" not in str(exc).lower():
                    raise
    if len(parts) > 1:
        for index, part in enumerate(parts, 1):
            part_title = f"{title}/Part {index}"
            part_text = discord_export.render_page(channel.name, part, {**meta, "complete": False})
            await client.edit_page(part_title, part_text, f"Archive part {index} for #{channel.name}")
        links = "\n".join(
            f"* [[{title}/Part {index}|Part {index}]]" for index in range(1, len(parts) + 1)
        )
        text = discord_export.render_page(channel.name, [], meta) + f"\n\n== Archive parts ==\n{links}\n"
    else:
        text = discord_export.render_page(channel.name, messages, meta)
    edit = await client.edit_page(title, text, f"Archive #{channel.name} ({len(messages)} messages)")
    page = await client.get_page(title)
    marker = f"source_channel_id={channel.id}"
    same_revision = edit.get("result") == "Nochange" or (page and page["revid"] == edit.get("newrevid"))
    verified = bool(meta["complete"] and page and marker in page["content"] and same_revision)
    return {
        "channel": channel, "ok": verified, "title": title, "url": wiki_page_url(title),
        "messages": len(messages), "attachments": uploaded,
        "revid": page["revid"] if page else None,
        "complete": meta["complete"],
    }


# ---- /publish command (single channel, a whole category, and/or a name list) ----
@tree.command(name="publish", description="Archive channel(s) to the wiki: a channel, a category, or a name list.")
@app_commands.describe(
    channel="A single text channel to archive",
    category="Archive every text channel in this category",
    channels="Comma-separated channel names to archive",
)
@app_commands.default_permissions(manage_channels=True)
@app_commands.guild_only()
async def publish(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    category: discord.CategoryChannel | None = None,
    channels: str | None = None,
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # Resolve the target set from any combination of the three inputs.
    targets: list[discord.TextChannel] = []
    if channel is not None:
        targets.append(channel)
    if category is not None:
        targets.extend(c for c in category.text_channels)
    if channels:
        wanted = {n.strip().lstrip("#").casefold() for n in channels.split(",") if n.strip()}
        found = {c.name.casefold() for c in guild.text_channels}
        targets.extend(c for c in guild.text_channels if c.name.casefold() in wanted)
        missing = [n for n in wanted if n not in found]
        if missing:
            await interaction.followup.send(
                "⚠️ Not found: " + ", ".join(f"`{n}`" for n in sorted(missing)), ephemeral=True)
        if not targets:
            await interaction.followup.send("No supplied channel names matched; nothing was published.", ephemeral=True)
            return
    if not targets and isinstance(interaction.channel, discord.TextChannel):
        targets.append(interaction.channel)  # default: the current channel

    # De-duplicate, preserving order.
    seen: set[int] = set()
    targets = [t for t in targets if not (t.id in seen or seen.add(t.id))]
    if not targets:
        await interaction.followup.send("No text channels to publish.", ephemeral=True)
        return

    try:
        client = await get_wiki_client()
    except Exception as exc:
        await interaction.followup.send(f"❌ Wiki unavailable: {exc}", ephemeral=True)
        return

    await interaction.followup.send(
        f"⏳ Publishing {len(targets)} channel(s)… results will post to #archives as they finish.",
        ephemeral=True,
    )
    archives_channel = discord.utils.get(guild.text_channels, name="archives")

    results = []
    for ch in targets:
        try:
            result = await publish_channel_to_wiki(client, ch)
        except Exception as exc:
            logging.exception("publish failed for #%s", ch.name)
            result = {"channel": ch, "ok": False, "error": str(exc)}
        results.append(result)
        if result.get("ok") and archives_channel is not None:
            try:
                await archives_channel.send(
                    f"📚 Archived **#{ch.name}** — {result['messages']} messages, "
                    f"{result['attachments']} attachment(s): {result['url']}"
                )
            except Exception:
                logging.exception("Failed to post archive URL to #archives")
                result["announcement_error"] = "could not post the archive URL to #archives"
        elif result.get("ok"):
            result["announcement_error"] = "#archives channel was not found"

    ok = [r for r in results if r.get("ok")]
    bad = [r for r in results if not r.get("ok")]
    lines = [f"✅ Published and verified {len(ok)}/{len(results)} channel(s)."]
    for r in ok[:12]:
        lines.append(f"• #{r['channel'].name} → <{r['url']}> ({r['messages']} msgs)")
        if r.get("announcement_error"):
            lines.append(f"  ⚠️ {r['announcement_error']}")
    if len(ok) > 12:
        lines.append(f"• …and {len(ok) - 12} more (see #archives)")
    if bad:
        lines.append(f"⚠️ **Failed/unverified ({len(bad)}) — do NOT delete these:**")
        for r in bad[:12]:
            lines.append(f"• #{r['channel'].name}: {r.get('error', 'read-back verification failed')}")
        if len(bad) > 12:
            lines.append(f"• …and {len(bad) - 12} more failures; inspect the bot log and republish them.")
    try:
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)
    except Exception:
        logging.exception("Failed to send publish summary (interaction may have expired)")


# ---- /wiki_status command (verify archive wiki connectivity) ----
@tree.command(name="wiki_status", description="Check the archive wiki connection and bot permissions.")
@app_commands.default_permissions(manage_channels=True)
@app_commands.guild_only()
async def wiki_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        client = await get_wiki_client()
        info = await client.userinfo()
        generator = await client.site_generator()
        rights = info.get("rights", [])
        await interaction.followup.send(
            f"✅ Connected to **{generator}**\n"
            f"Bot user: `{info.get('name')}` (groups: {', '.join(info.get('groups', [])) or 'none'})\n"
            f"Can edit: {'yes' if 'edit' in rights else '**NO**'} | "
            f"upload: {'yes' if 'upload' in rights else '**NO**'}\n"
            f"Archive namespace: `{WIKI_CONFIG['MEDIAWIKI_ARCHIVE_NAMESPACE']}`",
            ephemeral=True,
        )
    except Exception as exc:
        logging.exception("wiki_status failed")
        await interaction.followup.send(f"❌ Wiki connection failed: {exc}", ephemeral=True)


bot.run(TOKEN)
