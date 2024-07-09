import discord
from discord.ext import commands
import datetime

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')

# Define a decorator function to check if the user has a specific role
def is_admin_or_has_role(role_name):
    async def predicate(ctx):
        # Check if the user is an administrator
        if ctx.author.guild_permissions.administrator:
            return True
        
        # Check if the user has the specified role
        role = discord.utils.get(ctx.guild.roles, name=role_name)
        if role and role in ctx.author.roles:
            return True
        
        # Default case: user does not have permission
        return False
    
    return commands.check(predicate)

@bot.command()
@is_admin_or_has_role("Admin")  # Replace "Admin" with the role name you want to check
async def archive(ctx, term: str = None):
    try:
        if term is None:
            await ctx.send("I don't know what term you want to archive. Please specify a term like `Spring`, `Summer`, or `Fall`.")
            return
        
        current_year = datetime.datetime.now().year
        archive_category_name = f"{term.capitalize()} {current_year} Archive"
        
        # Create the archive category if it doesn't exist
        archive_category = discord.utils.get(ctx.guild.categories, name=archive_category_name)
        if not archive_category:
            archive_category = await ctx.guild.create_category(archive_category_name)
            await archive_category.set_permissions(ctx.guild.default_role, read_messages=True, send_messages=False)
            await ctx.send(f'Archive category {archive_category_name} created and permissions set.')
        else:
            await ctx.send(f'Archive category {archive_category_name} already exists.')
        
        # Move existing channels to archive and set read-only permissions
        moved_channels = []
        for channel in ctx.guild.text_channels:
            if f"-{term.lower()}-{current_year}" in channel.name.lower():
                await channel.edit(category=archive_category)
                await channel.set_permissions(ctx.guild.default_role, read_messages=True, send_messages=False)
                moved_channels.append(channel.name)
                await ctx.send(f'Moved channel: {channel.name}')
        
        if not moved_channels:
            await ctx.send('No channels were moved.')

        # Create new channels in the respective categories
        created_channels = []
        categories = ['CPT', 'IST', 'SPC', 'HSS', 'SOC']
        for category_name in categories:
            category = discord.utils.get(ctx.guild.categories, name=category_name)
            if category:
                for channel_name in moved_channels:
                    if channel_name.lower().startswith(category_name.lower()):
                        new_channel_name = channel_name.replace(term.lower(), get_next_term(term.lower()))
                        if term.lower() == "fall":
                            new_channel_name = new_channel_name.replace(str(current_year), str(current_year + 1))
                        
                        # Create the channel with specific permissions
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
                        
                        await ctx.send(f'Created and set permissions for channel: {new_channel_name}')
                        created_channels.append(new_channel_name)
        
        if not created_channels:
            await ctx.send('No new channels were created.')

        await ctx.send(f'Archive process completed for {term} {current_year}.')
    except Exception as e:
        await ctx.send(f'An error occurred: {str(e)}')
        print(f'[ERROR] {str(e)}')

def get_next_term(current_term):
    terms = ["spring", "summer", "fall"]
    index = terms.index(current_term)
    return terms[(index + 1) % len(terms)]
