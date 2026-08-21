import discord
from discord.ext import commands
from discord import app_commands
import datetime
import os
import logging

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

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}!')
    print(f'Logged in as {bot.user}!')
    try:
        await tree.sync()
        logging.info("Slash commands synchronized globally.")
    except Exception as e:
        logging.error("Error syncing commands: %s", str(e))
        print(f"Error syncing commands: {str(e)}")


@tree.command(name="archive", description="Archive channels and categories based on a term and year.")
@app_commands.describe(term="The term to archive (e.g., Spring, Summer, Fall)", year="The year (e.g., 2025)")
async def archive(interaction: discord.Interaction, term: str, year: int):
    responded = False
    try:
        await interaction.response.defer(ephemeral=True)
        responded = True
        logging.debug("Archive command invoked with term: %s, year: %d", term, year)

        if term.lower() not in ["spring", "summer", "fall"]:
            await interaction.followup.send("Invalid term. Please specify: `Spring`, `Summer`, or `Fall`.", ephemeral=True)
            return
        if not (1900 <= year <= 2100):
            await interaction.followup.send("Invalid year. Please provide a valid year (e.g., 2025).", ephemeral=True)
            return

        term = term.lower()
        archive_category_name = f"{term.capitalize()} {year} Archive"
        guild = interaction.guild
        verified_role = discord.utils.get(guild.roles, name="Verified")

        if not verified_role:
            await interaction.followup.send("The 'Verified' role does not exist. Please create it first.", ephemeral=True)
            logging.error("Verified role not found.")
            return

        archive_category = discord.utils.get(guild.categories, name=archive_category_name)
        if not archive_category:
            archive_category = await guild.create_category(archive_category_name)
            await archive_category.set_permissions(guild.default_role, read_messages=False, send_messages=False)
            await archive_category.set_permissions(verified_role, read_messages=True, send_messages=False, read_message_history=True)
            logging.info("Archive category '%s' created.", archive_category_name)

        moved_channels = []
        for channel in guild.text_channels:
            logging.debug("Checking channel: %s", channel.name)
            if f"-{term}-{year}" in channel.name.lower():
                await channel.edit(category=archive_category)
                for target in list(channel.overwrites.keys()):
                    await channel.set_permissions(target, overwrite=None)
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


@tree.command(name="populate", description="Create categories and channels dynamically.")
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
async def populate(interaction: discord.Interaction, category: app_commands.Choice[str], term: str, year: int, courses: str):
    responded = False
    try:
        await interaction.response.defer(ephemeral=True)
        responded = True
        logging.debug("Populate command invoked with category: %s, term: %s, year: %d, courses: %s", category.value, term, year, courses)

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

        guild = interaction.guild
        category_name = category.value
        existing_category = discord.utils.get(guild.categories, name=category_name)
        if not existing_category:
            existing_category = await guild.create_category(category_name)
            logging.info("Category '%s' created.", category_name)

        created_channels = []
        for course_number in course_numbers:
            channel_name = f"{category_name}-{course_number}-{term.capitalize()}-{year}"
            existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
            if not existing_channel:
                role_name = f"{category_name}-{course_number}"
                role = discord.utils.get(guild.roles, name=role_name)
                lab_tech_role = discord.utils.get(guild.roles, name="Lab Tech")
                if not role:
                    logging.warning("Role '%s' not found for channel '%s'.", role_name, channel_name)
                    continue
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                    lab_tech_role: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)
                }
                new_channel = await guild.create_text_channel(name=channel_name, category=existing_category, overwrites=overwrites)
                created_channels.append(new_channel.name)
                logging.info("Channel '%s' created as private with role '%s' assigned.", new_channel.name, role_name)

        if created_channels:
            await interaction.followup.send(f"Created private channels with roles: {', '.join(created_channels)}", ephemeral=True)
        else:
            await interaction.followup.send("No new channels were created. All channels already exist or roles were missing.", ephemeral=True)

    except Exception as e:
        logging.error("An error occurred in populate: %s", str(e))
        try:
            if not responded:
                await interaction.response.send_message(f"An error occurred: {str(e)}", ephemeral=True)
            else:
                await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)
        except discord.errors.InteractionResponded:
            logging.warning("Interaction already responded when handling populate error.")


def build_course_permissions() -> discord.Permissions:
    perms = discord.Permissions.none()
    perms.view_channel = True
    perms.change_nickname = True
    perms.send_messages = True
    perms.send_messages_in_threads = True
    perms.create_public_threads = True
    if hasattr(discord.Permissions, "create_forum_threads"):
        perms.create_forum_threads = True
    perms.embed_links = True
    perms.attach_files = True
    perms.add_reactions = True
    perms.read_message_history = True
    perms.use_application_commands = True
    perms.use_external_emojis = True
    if hasattr(discord.Permissions, "use_external_stickers"):
        perms.use_external_stickers = True
    perms.connect = True
    perms.speak = True
    perms.stream = True
    if hasattr(discord.Permissions, "use_embedded_activities"):
        perms.use_embedded_activities = True
    return perms


@tree.command(name="add_role", description="Create or update course roles for a category (e.g., CYB 110, 201, 269)")
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
async def add_role(interaction: discord.Interaction, category: app_commands.Choice[str], courses: str):
    responded = False
    try:
        await interaction.response.defer(ephemeral=True)
        responded = True
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command must be used in a server.", ephemeral=True)
            return

        course_numbers = [c.strip() for c in courses.split(",") if c.strip().isdigit()]
        if not course_numbers:
            await interaction.followup.send("No valid course numbers provided. Use a comma-separated list of numbers.", ephemeral=True)
            return

        perms = build_course_permissions()
        created, updated, unchanged = [], [], []
        for num in course_numbers:
            role_name = f"{category.value}-{num}"
            role = discord.utils.get(guild.roles, name=role_name)
            if role is None:
                try:
                    role = await guild.create_role(name=role_name, permissions=perms, reason=f"Auto-created by /add_role for {category.value} {num}")
                    created.append(role_name)
                    continue
                except discord.Forbidden:
                    await interaction.followup.send(f"Missing permissions to create role `{role_name}` (need Manage Roles, and my top role must be above it).", ephemeral=True)
                    return
                except Exception as e:
                    await interaction.followup.send(f"Error creating `{role_name}`: {e}", ephemeral=True)
                    return
            try:
                if role.permissions != perms:
                    await role.edit(permissions=perms, reason=f"Sync perms via /add_role for {category.value} {num}")
                    updated.append(role_name)
                else:
                    unchanged.append(role_name)
            except discord.Forbidden:
                await interaction.followup.send(f"I can't edit `{role_name}`. Ensure I have Manage Roles and my top role is above `{role_name}`.", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"Error updating `{role_name}`: {e}", ephemeral=True)
                return

        lines = []
        if created: lines.append(f"Created: {', '.join(created)}")
        if updated: lines.append(f"Updated perms: {', '.join(updated)}")
        if unchanged: lines.append(f"Already correct: {', '.join(unchanged)}")
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


class DeleteConfirmationView(discord.ui.View):
    def __init__(self, requester_id: int, channels, categories):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.channels = channels
        self.categories = categories

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the user who ran `/delete` can confirm this deletion.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
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

        for category in self.categories:
            try:
                await category.delete(reason=f"Bulk deletion requested by {interaction.user} ({interaction.user.id})")
                deleted_categories.append(category.name)
                logging.info("Category '%s' (%s) deleted by %s (%s).", category.name, category.id, interaction.user, interaction.user.id)
            except Exception as e:
                failures.append(f"category `{category.name}`: {e}")
                logging.exception("Failed deleting category '%s' (%s).", category.name, category.id)

        lines = [f"Deletion complete: {len(deleted_categories)} categor{'y' if len(deleted_categories) == 1 else 'ies'} and {len(deleted_channels)} channel{'s' if len(deleted_channels) != 1 else ''} deleted."]
        if failures:
            lines.append("Failures:\n" + "\n".join(f"- {failure}" for failure in failures[:15]))
        await interaction.edit_original_response(content="\n".join(lines), view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Deletion cancelled. Nothing was deleted.", view=self)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


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
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need the Manage Channels permission to use `/delete`.", ephemeral=True)
        return

    names = [name.strip() for name in targets.split(",") if name.strip()]
    if not names:
        await interaction.response.send_message("Provide at least one channel or category name.", ephemeral=True)
        return

    # Case-insensitive exact-name matching. Duplicate names are all included so
    # the confirmation accurately represents what Discord will delete.
    wanted = {name.casefold() for name in names}
    matched_categories = []
    matched_channels = []

    if target_type.value in ("category", "both"):
        matched_categories = [category for category in guild.categories if category.name.casefold() in wanted]

    if target_type.value in ("channel", "both"):
        matched_channels = [channel for channel in guild.channels if not isinstance(channel, discord.CategoryChannel) and channel.name.casefold() in wanted]

    # Selecting a category means deleting the category and everything in it.
    # Include all channel types exposed through CategoryChannel.channels.
    for category in matched_categories:
        for channel in category.channels:
            if channel not in matched_channels:
                matched_channels.append(channel)

    found_names = {category.name.casefold() for category in matched_categories}
    found_names.update(channel.name.casefold() for channel in matched_channels if channel.category not in matched_categories)
    missing = [name for name in names if name.casefold() not in found_names]

    if not matched_categories and not matched_channels:
        await interaction.response.send_message("No matching channels or categories were found. Nothing was deleted.", ephemeral=True)
        return

    category_names = ", ".join(f"`{category.name}`" for category in matched_categories) or "None"
    standalone_channels = [channel for channel in matched_channels if channel.category not in matched_categories]
    channel_names = ", ".join(f"`{channel.name}`" for channel in standalone_channels) or "None"

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

    logging.info("Delete requested by %s (%s): type=%s targets=%s; resolved categories=%s channels=%s missing=%s", interaction.user, interaction.user.id, target_type.value, names, [c.name for c in matched_categories], [c.name for c in matched_channels], missing)
    view = DeleteConfirmationView(interaction.user.id, matched_channels, matched_categories)
    await interaction.response.send_message(message, view=view, ephemeral=True)


bot.run(TOKEN)
