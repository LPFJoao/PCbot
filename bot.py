import os
import json
import discord
from discord.ext import commands, tasks
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
import pytz
from datetime import datetime, timedelta
import unicodedata

# Load .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
scheduler = AsyncIOScheduler(timezone="Europe/Paris")

# --- Reminder System ---
def default_event_status():
    return {"boonstone": False, "riftstone": False, "siege": False, "tax": False}

event_status = default_event_status()
STATUS_FILE = "event_status.json"

def save_event_status():
    with open(STATUS_FILE, "w") as f:
        json.dump(event_status, f)


def load_event_status():
    global event_status
    try:
        with open(STATUS_FILE, "r") as f:
            event_status = json.load(f)
    except FileNotFoundError:
        save_event_status()

@bot.command()
async def activate(ctx, event: str):
    key = event.lower()
    if key in event_status:
        event_status[key] = True
        save_event_status()
        await ctx.send(f"✅ Activated {key} reminders.")
    else:
        await ctx.send("❌ Unknown event. Options: boonstone, riftstone, siege, tax.")

@bot.command()
async def deactivate(ctx, event: str):
    key = event.lower()
    if key in event_status:
        event_status[key] = False
        save_event_status()
        await ctx.send(f"❌ Deactivated {key} reminders.")
    else:
        await ctx.send("❌ Unknown event. Options: boonstone, riftstone, siege, tax.")

@bot.command()
async def status(ctx):
    lines = [f"{e.capitalize()}: {'ON' if state else 'OFF'}" for e, state in event_status.items()]
    await ctx.send("**Reminder Status**\n" + "\n".join(lines))

# Static reminder jobs omitted for brevity...

# --- Voting System ---
vote_data = {'messages': {}, 'results': {}}
TIME_EMOJIS = {'🕙': 'Friday 22:00', '🕕': 'Saturday 18:00', '🕘': 'Saturday 21:00', '🕔': 'Sunday 18:00', '🕐': 'Sunday 21:00'}
BOSS_EMOJIS = {'🐉': 'Daigon', '🎅🏻': 'Pakilo Naru', '🐋': 'Leviathan', '🦁': 'Manticus'}

async def start_vote(channel, t, opts):
    desc = '@everyone\n**Vote for {0}**\n'.format(t.capitalize())
    for e, l in opts.items():
        desc += f'{e} → {l}\n'
    msg = await channel.send(desc)
    for e in opts:
        await msg.add_reaction(e)
    vote_data['messages'][msg.id] = {
        'type': t,
        'channel_id': channel.id,
        'expires_at': datetime.now(pytz.timezone('Europe/Paris')) + timedelta(hours=12)
    }

@scheduler.scheduled_job('cron', day_of_week='tue', hour=23, minute=39)
async def post_scheduled_votes():
    ch = bot.get_channel(1353371080273952939)
    await start_vote(ch, 'schedule', TIME_EMOJIS)
    await start_vote(ch, 'boss', BOSS_EMOJIS)

@bot.event
async def on_raw_reaction_add(payload):
    # Only enforce single boss vote
    vm = vote_data['messages'].get(payload.message_id)
    if not vm or vm['type'] != 'boss':
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    channel = guild.get_channel(payload.channel_id)
    if not channel:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return
    # Fetch member to ensure we have a valid user object
    try:
        user = await guild.fetch_member(payload.user_id)
    except discord.NotFound:
        return
    if user.bot:
        return
    # Compare raw emoji strings to handle modifiers consistently
    added = str(payload.emoji)
    for reaction in message.reactions:
        react = str(reaction.emoji)
        # Remove any other boss reaction this user has
        if react in BOSS_EMOJIS and react != added:
            try:
                await message.remove_reaction(reaction.emoji, user)
            except (discord.Forbidden, discord.HTTPException):
                pass

@tasks.loop(minutes=10)
async def auto_start_votes():
    now = datetime.now(pytz.timezone('Europe/Paris'))
    for mid, meta in list(vote_data['messages'].items()):
        if now > meta['expires_at']:
            await closevote(None)

@bot.command()
async def help(ctx):
    help_text = """!activate <event>
Enables reminders for one of: boonstone, riftstone, siege, or tax.
Example: !activate boonstone

!deactivate <event>
Disables reminders for one of those same events.
Example: !deactivate siege

!status
Shows whether each of the four reminder events is currently ON or OFF."""
    await ctx.send(f"```{help_text}```")

@bot.event
async def on_ready():
    load_event_status()
    scheduler.start()
    auto_start_votes.start()
    print(f"✅ Bot running as {bot.user}")

bot.run(TOKEN)
