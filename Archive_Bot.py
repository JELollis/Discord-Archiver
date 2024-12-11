import discord
from discord.ext import commands
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

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}!')
    print(f'Logged in as {bot.user}!')

# Define a decorator function to check if the user has a specific role
def is_admin_or_has_role(role_name):
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        role = discord.utils.get(ctx.guild.roles, name=role_name)
        if role and role in ctx.author.roles:
            return True
        return False
    return commands.check(predicate)

@bot.command()
@is_admin_or_has_role("Admin")  # Replace "Admin" with the role name you want to check
async def archive(ctx, term: str = None):
    try:
        logging.debug("Command invoked with term: %s", term)
        if term is None or term.lower() not in ["spring", "summer", "fall"]:
            await ctx.send("Invalid term. Please specify a valid term: `Spring`, `Summer`, or `Fall`.")
            logging.debug("Invalid term provided or term is None.")
            return

        term = term.lower()
        current_year = datetime.datetime.now().year
        logging.debug("Current year: %d, Term: %s", current_year, term)

        archive_category_name = f"{term.capitalize()} {current_year} Archive"
        logging.debug("Archive category name: %s", archive_category_name)

        # Create the archive category if it doesn't exist
        archive_category = discord.utils.get(ctx.guild.categories, name=archive_category_name)
        if not archive_category:
            archive_category = await ctx.guild.create_category(archive_category_name)
            await archive_category.set_permissions(ctx.guild.default_role, read_messages=True, send_messages=False)
            await ctx.send(f'Archive category {archive_category_name} created and permissions set.')
            logging.info("Archive category %s created.", archive_category_name)
        else:
            await ctx.send(f'Archive category {archive_category_name} already exists.')
            logging.info("Archive category %s already exists.", archive_category_name)

        # Move existing channels to archive and set read-only permissions
        moved_channels = []
        for channel in ctx.guild.text_channels:
            if f"-{term}-{current_year}" in channel.name.lower():
                await channel.edit(category=archive_category)
                await channel.set_permissions(ctx.guild.default_role, read_messages=True, send_messages=False)
                moved_channels.append(channel.name)
                await ctx.send(f'Moved channel: {channel.name}')
                logging.debug("Moved channel: %s", channel.name)

        if not moved_channels:
            await ctx.send('No channels were moved.')
            logging.debug("No channels matched the criteria to move.")

        # Create new channels in the respective categories
        created_channels = []
        categories = ['CPT', 'IST', 'SPC', 'HSS', 'SOC']
        for category_name in categories:
            category = discord.utils.get(ctx.guild.categories, name=category_name)
            if category:
                logging.debug("Processing category: %s", category_name)
                for channel_name in moved_channels:
                    if channel_name.lower().startswith(category_name.lower()):
                        new_channel_name = channel_name.replace(term, get_next_term(term))
                        if term == "fall":
                            new_channel_name = new_channel_name.replace(str(current_year), str(current_year + 1))

                        logging.debug("New channel name: %s", new_channel_name)

                        # Check if the channel already exists
                        existing_channel = discord.utils.get(category.channels, name=new_channel_name)
                        if existing_channel:
                            await ctx.send(f'Channel {new_channel_name} already exists in {category_name}.')
                            logging.debug("Channel %s already exists in %s.", new_channel_name, category_name)
                            continue

                        # Create the channel
                        new_channel = await category.create_text_channel(new_channel_name)

                        # Set channel permissions
                        role_name = '-'.join(new_channel_name.split('-')[:-2])
                        role = discord.utils.get(ctx.guild.roles, name=role_name)
                        if role:
                            overwrites = {
                                ctx.guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
                                role: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                            }
                            await new_channel.edit(overwrites=overwrites)
                            logging.debug("Permissions set for role: %s", role_name)
                        else:
                            await ctx.send(f"Role '{role_name}' not found. Permissions for {new_channel_name} were not fully applied.")
                            logging.debug("Role '%s' not found for %s.", role_name, new_channel_name)

                        await ctx.send(f'Created and set permissions for channel: {new_channel_name}')
                        created_channels.append(new_channel_name)
                        logging.info("Created new channel: %s", new_channel_name)

        if not created_channels:
            await ctx.send('No new channels were created.')
            logging.debug("No new channels were created.")

        await ctx.send(f'Archive process completed for {term.capitalize()} {current_year}.')
        logging.info("Archive process completed for %s %d.", term.capitalize(), current_year)
    except Exception as e:
        await ctx.send(f'An error occurred: {str(e)}')
        logging.error("An error occurred: %s", str(e))

def get_next_term(current_term):
    terms = ["spring", "summer", "fall"]
    try:
        index = terms.index(current_term)
        return terms[(index + 1) % len(terms)]
    except ValueError:
        logging.debug("Invalid term provided to get_next_term: %s", current_term)
        return "spring"  # Default to spring if term is invalid

# Replace 'YOUR_BOT_TOKEN' with your actual bot token
bot.run('')