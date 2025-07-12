import discord
import asyncio
import logging
import os
from datetime import datetime
import re

# Setup logging
log_directory = "logs"
os.makedirs(log_directory, exist_ok=True)
log_filename = os.path.join(log_directory, f"grant_rw_summer2025_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log")
logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s]: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Load token from Bot Key.txt in the same directory
with open("Bot Key.txt", "r", encoding="utf-8") as key_file:
    TOKEN = key_file.readline().strip()

# Regex pattern for current semester channels
pattern = re.compile(r'^[a-zA-Z]+-\d{3}-summer-2025$', re.IGNORECASE)

intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    logging.info(f"Logged in as {client.user}")
    for guild in client.guilds:
        await update_labtech_rw_access(guild)
    await client.close()

async def update_labtech_rw_access(guild: discord.Guild):
    logging.info(f"Updating Summer 2025 RW access in guild: {guild.name}")
    lab_tech_role = discord.utils.get(guild.roles, name="Lab Tech")

    if not lab_tech_role:
        logging.error("Lab Tech role not found in the guild.")
        return

    updated_channels = []
    for channel in guild.text_channels:
        if pattern.match(channel.name):
            try:
                await channel.set_permissions(lab_tech_role, overwrite=discord.PermissionOverwrite(
                    read_messages=True,
                    send_messages=True,
                    read_message_history=True
                ))
                updated_channels.append(channel.name)
                logging.info(f"Granted RW access to Lab Tech for '{channel.name}'")
            except Exception as e:
                logging.error(f"Failed to update '{channel.name}': {e}")

    if updated_channels:
        logging.info(f"Updated {len(updated_channels)} channels: {', '.join(updated_channels)}")
    else:
        logging.info("No matching channels found.")

client.run(TOKEN)
