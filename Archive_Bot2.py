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

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

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
@app_commands.describe(
    term="The term to archive (e.g., Spring, Summer, Fall)",
    year="The year to archive (e.g., 2025)"
)
async def archive(interaction: discord.Interaction, term: str, year: int):
    try:
        # Defer the interaction to allow processing time
        await interaction.response.defer(ephemeral=True)

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
                await channel.set_permissions(verified_role, read_messages=True, send_messages=False,
                                              read_message_history=True)

                moved_channels.append(channel.name)
                logging.info("Channel '%s' moved to archive and permissions updated.", channel.name)

        if moved_channels:
            await interaction.followup.send(
                f"Archived channels: {', '.join(moved_channels)}.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "No channels found matching the specified term and year.", ephemeral=True
            )

        logging.info("Archive process completed for %s %d.", term, year)
    except Exception as e:
        logging.error("An error occurred: %s", str(e))
        await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

# Define slash command to populate
@tree.command(name="populate", description="Create categories and channels dynamically.")
@app_commands.describe(
    category="Select or create a category",
    term="Specify the term (Spring, Summer, Fall)",
    year="Specify the year (e.g., 2025)",
    courses="Comma-separated list of course numbers"
)
@app_commands.choices(category=[
    app_commands.Choice(name="CPT", value="CPT"),
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
    try:
        # Defer the interaction to allow processing time
        await interaction.response.defer(ephemeral=True)

        logging.debug("Populate command invoked with category: %s, term: %s, year: %d, courses: %s",
                      category.value, term, year, courses)

        # Validate the term
        if term.lower() not in ["spring", "summer", "fall"]:
            await interaction.followup.send(
                "Invalid term. Please specify a valid term: `Spring`, `Summer`, or `Fall`.",
                ephemeral=True
            )
            logging.debug("Invalid term provided: %s", term)
            return

        if not (1900 <= year <= 2100):
            await interaction.followup.send(
                "Invalid year. Please provide a valid year (e.g., 2025).",
                ephemeral=True
            )
            return

        course_numbers = [course.strip() for course in courses.split(",") if course.strip().isdigit()]
        if not course_numbers:
            await interaction.followup.send(
                "No valid course numbers provided. Please provide a comma-separated list of numbers.",
                ephemeral=True
            )
            return

        # Create the category if it does not exist
        guild = interaction.guild
        category_name = category.value
        existing_category = discord.utils.get(guild.categories, name=category_name)
        if not existing_category:
            existing_category = await guild.create_category(category_name)
            logging.info("Category '%s' created.", category_name)

        # Create channels under the category as private and assign roles
        created_channels = []
        for course_number in course_numbers:
            channel_name = f"{category_name}-{course_number}-{term.capitalize()}-{year}"
            existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
            if not existing_channel:
                role_name = f"{category_name}-{course_number}"  # Role format
                role = discord.utils.get(guild.roles, name=role_name)

                if not role:
                    logging.warning("Role '%s' not found for channel '%s'.", role_name, channel_name)
                    continue  # Skip creating channel if the role doesn't exist

                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),  # Make channel private
                    role: discord.PermissionOverwrite(read_messages=True, send_messages=True),  # Assign role permissions
                }
                new_channel = await guild.create_text_channel(name=channel_name, category=existing_category, overwrites=overwrites)
                created_channels.append(new_channel.name)
                logging.info("Channel '%s' created as private with role '%s' assigned.", new_channel.name, role_name)

        if created_channels:
            await interaction.followup.send(f"Created private channels with roles: {', '.join(created_channels)}", ephemeral=True)
        else:
            await interaction.followup.send("No new channels were created. All channels already exist or roles were missing.", ephemeral=True)

    except Exception as e:
        logging.error("An error occurred: %s", str(e))
        await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

bot.run('{API_Key}')
