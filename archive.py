import discord
from discord.ext import commands
import datetime

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')

@bot.command()
async def archive(ctx, term: str):
    current_year = datetime.datetime.now().year
    archive_category_name = f"{term.capitalize()} {current_year} Archive"
    
    # Create the archive category
    archive_category = await ctx.guild.create_category(archive_category_name)
    await archive_category.set_permissions(ctx.guild.default_role, read_messages=False, send_messages=False)

    # Move existing channels to archive and set read-only permissions
    for channel in ctx.guild.text_channels:
        if channel.name.endswith(f"-{term.capitalize()}-{current_year}"):
            await channel.edit(category=archive_category)
            await channel.set_permissions(ctx.guild.default_role, read_messages=True, send_messages=False)
    
    # Create new channels in the respective categories
    categories = ['CPT', 'IST', 'SPC', 'HSS']
    for category_name in categories:
        category = discord.utils.get(ctx.guild.categories, name=category_name)
        if category:
            for channel in archive_category.text_channels:
                if channel.name.startswith(category_name):
                    new_channel_name = channel.name.replace(term.capitalize(), get_next_term(term.capitalize()))
                    new_channel = await category.create_text_channel(new_channel_name)
                    
                    # Grant access only to roles matching the class
                    role_name = '-'.join(new_channel_name.split('-')[:-2])
                    role = discord.utils.get(ctx.guild.roles, name=role_name)
                    if role:
                        await new_channel.set_permissions(role, read_messages=True, send_messages=True)

def get_next_term(current_term):
    terms = ["Spring", "Summer", "Fall"]
    index = terms.index(current_term)
    return terms[(index + 1) % len(terms)]

bot.run('YOUR_BOT_TOKEN')
