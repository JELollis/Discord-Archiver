import discord
from discord.ext import commands
from discord import app_commands
import datetime
import os
import re
import logging
from pathlib import Path

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


def load_token() -> str:
    """Load the bot token from the DISCORD_TOKEN env var, falling back to the
    'Bot Key.txt' file located next to this script."""
    env_token = os.environ.get("DISCORD_TOKEN")
    if env_token:
        return env_token.strip()
    key_path = Path(__file__).parent / "Bot Key.txt"
    try:
        with open(key_path, "r", encoding="utf-8") as key_file:
            return key_file.readline().strip()
    except FileNotFoundError:
        raise SystemExit(
            f"No bot token found. Set the DISCORD_TOKEN environment variable or "
            f"create '{key_path}'."
        )


TOKEN = load_token()

# Slash-command-only bot: no privileged message_content intent required.
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

_commands_synced = False


@bot.event
async def on_ready():
    global _commands_synced
    logging.info(f'Logged in as {bot.user}!')
    print(f'Logged in as {bot.user}!')
    # on_ready can fire on every reconnect; sync the global command tree only once.
    if _commands_synced:
        return
    try:
        await tree.sync()
        _commands_synced = True
        logging.info("Slash commands synchronized globally.")
    except Exception as e:
        logging.error("Error syncing commands: %s", str(e))
        print(f"Error syncing commands: {str(e)}")


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Present permission/usage errors cleanly; log everything else without
    leaking internals to users."""
    if isinstance(error, app_commands.MissingPermissions):
        message = "🚫 You don't have permission to use this command."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "This command can only be used in a server."
    else:
        logging.error("Unhandled app command error: %s", error, exc_info=error)
        message = "⚠️ Something went wrong while processing this command. An admin can check the logs."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.errors.InteractionResponded:
        logging.warning("Interaction already responded when handling app command error.")


async def respond_error(interaction: discord.Interaction, responded: bool, label: str, exc: Exception):
    """Log the real exception and show the user a generic, non-leaky message."""
    logging.error("An error occurred in %s: %s", label, exc, exc_info=exc)
    message = "⚠️ Something went wrong while processing this command. An admin can check the logs."
    try:
        if not responded:
            await interaction.response.send_message(message, ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)
    except discord.errors.InteractionResponded:
        logging.warning("Interaction already responded when handling %s error.", label)


# Define slash command to archive
@tree.command(name="archive", description="Archive channels and categories based on a term and year.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
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

        # Match channels named like "<cat>-<course>-<term>-<year>" (anchored, so
        # we don't sweep in "...-fall-2025-backup" or similar).
        channel_pattern = re.compile(rf'^[a-z]+-\d+-{re.escape(term)}-{year}$')

        # Full overwrite set applied to each archived channel in a single edit.
        archive_overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
            verified_role: discord.PermissionOverwrite(read_messages=True, send_messages=False, read_message_history=True),
        }

        # Find and move matching channels
        moved_channels = []
        for channel in guild.text_channels:
            logging.debug("Checking channel: %s", channel.name)
            if channel_pattern.match(channel.name.lower()):
                # Move and replace all permission overwrites in a single API call.
                await channel.edit(category=archive_category, overwrites=archive_overwrites)
                moved_channels.append(channel.name)
                logging.info("Channel '%s' moved to archive and permissions updated.", channel.name)

        if moved_channels:
            await interaction.followup.send(f"Archived channels: {', '.join(moved_channels)}.", ephemeral=True)
        else:
            await interaction.followup.send("No channels found matching the specified term and year.", ephemeral=True)
        logging.info("Archive process completed for %s %d.", term, year)

    except Exception as e:
        await respond_error(interaction, responded, "archive", e)

# Define slash command to populate
@tree.command(name="populate", description="Create categories and channels dynamically.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
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
        category_name = category.value
        existing_category = discord.utils.get(guild.categories, name=category_name)
        if not existing_category:
            existing_category = await guild.create_category(category_name)
            logging.info("Category '%s' created.", category_name)

        # Create channels under the category as private and assign roles
        lab_tech_role = discord.utils.get(guild.roles, name="Lab Tech")
        created_channels = []
        for course_number in course_numbers:
            channel_name = f"{category_name}-{course_number}-{term.capitalize()}-{year}"
            existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
            if not existing_channel:
                role_name = f"{category_name}-{course_number}"
                role = discord.utils.get(guild.roles, name=role_name)
                if not role:
                    logging.warning("Role '%s' not found for channel '%s'.", role_name, channel_name)
                    continue
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                }
                # Only add the Lab Tech overwrite if the role actually exists;
                # a None key would make create_text_channel raise.
                if lab_tech_role:
                    overwrites[lab_tech_role] = discord.PermissionOverwrite(
                        read_messages=True, send_messages=True, read_message_history=True)
                else:
                    logging.warning("Lab Tech role not found; creating '%s' without it.", channel_name)
                new_channel = await guild.create_text_channel(name=channel_name, category=existing_category, overwrites=overwrites)
                created_channels.append(new_channel.name)
                logging.info("Channel '%s' created as private with role '%s' assigned.", new_channel.name, role_name)

        if created_channels:
            await interaction.followup.send(f"Created private channels with roles: {', '.join(created_channels)}", ephemeral=True)
        else:
            await interaction.followup.send("No new channels were created. All channels already exist or roles were missing.", ephemeral=True)

    except Exception as e:
        await respond_error(interaction, responded, "populate", e)

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
@app_commands.guild_only()
@app_commands.default_permissions(manage_roles=True)
@app_commands.checks.has_permissions(manage_roles=True)
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

        # Parse numbers
        course_numbers = [c.strip() for c in courses.split(",") if c.strip().isdigit()]
        if not course_numbers:
            await interaction.followup.send("No valid course numbers provided. Use a comma-separated list of numbers.", ephemeral=True)
            return

        perms = build_course_permissions()
        created, updated, unchanged, failed = [], [], [], []

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
                except discord.Forbidden:
                    failed.append(f"{role_name} (missing permission to create)")
                except Exception as e:
                    logging.error("Error creating role '%s': %s", role_name, e, exc_info=e)
                    failed.append(f"{role_name} (create error)")
                continue

            # Update perms if different
            try:
                if role.permissions != perms:
                    await role.edit(permissions=perms, reason=f"Sync perms via /add_role for {category.value} {num}")
                    updated.append(role_name)
                else:
                    unchanged.append(role_name)
            except discord.Forbidden:
                failed.append(f"{role_name} (missing permission to edit)")
            except Exception as e:
                logging.error("Error updating role '%s': %s", role_name, e, exc_info=e)
                failed.append(f"{role_name} (update error)")

        # Summary
        lines = []
        if created:   lines.append(f"✅ Created: {', '.join(created)}")
        if updated:   lines.append(f"🔁 Updated perms: {', '.join(updated)}")
        if unchanged: lines.append(f"✔️ Already correct: {', '.join(unchanged)}")
        if failed:    lines.append(f"⚠️ Failed: {', '.join(failed)}")
        if not lines: lines.append("No changes made.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    except Exception as e:
        await respond_error(interaction, responded, "add_role", e)

bot.run(TOKEN)
