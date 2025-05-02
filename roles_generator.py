import os

import discord
from discord.ext import commands
import logging

# Setup logging
log_filename = "create_roles.log"
log_directory = "logs"
os.makedirs(log_directory, exist_ok=True)

logging.basicConfig(
    filename=log_filename,
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s]: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logging.info("Role creation script starting up.")

# Enable intents
intents = discord.Intents.default()
intents.message_content = True  # Required for command handling
bot = commands.Bot(command_prefix='!', intents=intents)

# Predefined categories and their respective courses
categories = {
    "CPT": [113, 168, 170, 187, 189, 209, 230, 231, 234, 236, 237, 239, 257, 264, 267, 270, 273, 275, 280, 283, 289],
    "IST": [110, 190, 191, 198, 201, 202, 203, 220, 226, 239, 257, 258, 266, 267, 272, 278, 291, 292, 293, 294, 295, 299],
    "SPC": [205, 208, 209],
    "SOC": [101],
    "HSS": [105],
    "HIS": [122]
}

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    logging.info(f"Logged in as {bot.user}")
    print("Available commands:")
    for command in bot.commands:
        print(f" - {command.name}")
    logging.info("Commands registered successfully.")

@bot.command()
async def create_roles(ctx):
    guild = ctx.guild

    if not guild:
        await ctx.send("This command can only be used in a server.")
        logging.error("Command invoked outside of a guild.")
        return

    created_roles = []
    for category, courses in categories.items():
        for course in courses:
            role_name = f"{category}-{course}"
            existing_role = discord.utils.get(guild.roles, name=role_name)
            if not existing_role:
                try:
                    await guild.create_role(name=role_name)
                    created_roles.append(role_name)
                    logging.info(f"Role '{role_name}' created successfully.")
                except Exception as e:
                    logging.error(f"Failed to create role '{role_name}': {e}")
            else:
                logging.info(f"Role '{role_name}' already exists. Skipping.")

    if created_roles:
        await ctx.send(f"Created roles: {', '.join(created_roles)}")
    else:
        await ctx.send("No new roles were created. All roles already exist.")

# Debugging Command Prefix
print(f"Command Prefix: {bot.command_prefix}")
logging.info(f"Command Prefix: {bot.command_prefix}")

bot.run('{API_Key}')