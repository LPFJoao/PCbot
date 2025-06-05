import os
import json
import discord
from discord.ext import commands, tasks
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
from datetime import datetime, timedelta
import unicodedata
import threading
import asyncio

# Load .env
TOKEN = os.getenv("DISCORD_TOKEN")

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
scheduler = AsyncIOScheduler(timezone="Europe/Paris")


# --- Reminder System ---
def default_event_status():
    return {
        "boonstone": False,
        "riftstone": False,
        "siege": False,
        "TAX": False
    }


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
        await ctx.send(
            "❌ Unknown event. Options: boonstone, riftstone, siege, tax.")


@bot.command()
async def deactivate(ctx, event: str):
    key = event.lower()
    if key in event_status:
        event_status[key] = False
        save_event_status()
        await ctx.send(f"❌ Deactivated {key} reminders.")
    else:
        await ctx.send(
            "❌ Unknown event. Options: boonstone, riftstone, siege, tax.")


@bot.command()
async def status(ctx):
    lines = [
        f"{e.capitalize()}: {'ON' if state else 'OFF'}"
        for e, state in event_status.items()
    ]
    await ctx.send("**Reminder Status**\n" + "\n".join(lines))


# Static reminder jobs omitted for brevity...

# --- Voting System ---
vote_data = {'messages': {}, 'results': {}}
TIME_EMOJIS = {
    '🕙': 'Friday 22:00',
    '🕕': 'Saturday 18:00',
    '🕘': 'Saturday 21:00',
    '🕔': 'Sunday 18:00',
    '🕐': 'Sunday 21:00'
}
BOSS_EMOJIS = {
    '<:Daigon:1379778836350369922>': 'Daigon',
    '<:Pakilo:1379778632075186276>': 'Pakilo Naru',
    '<:Levi:1379778779412697118>': 'Leviathan',
    '<:Manti:1379778716388818964>': 'Manticus'
}


async def start_vote(channel, t, opts):
    desc = '@everyone\n**Vote for {0}**\n'.format(t.capitalize())
    for e, l in opts.items():
        desc += f'{e} → {l}\n'
    msg = await channel.send(desc)
    for e in opts:
        await msg.add_reaction(e)
    vote_data['messages'][msg.id] = {
        'type':
        t,
        'channel_id':
        channel.id,
        'expires_at':
        datetime.now(pytz.timezone('Europe/Paris')) + timedelta(hours=12)
    }


@scheduler.scheduled_job('cron', day_of_week='thu', hour=16, minute=0)
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

@bot.command()
async def results(ctx):
    """Show current vote results if any active vote exists."""
    now = datetime.now(pytz.timezone('Europe/Paris'))
    to_remove = []
    for msg_id, meta in vote_data['messages'].items():
        channel = bot.get_channel(meta['channel_id'])
        if not channel:
            continue
        try:
            message = await channel.fetch_message(msg_id)
        except discord.NotFound:
            continue

        if now > meta['expires_at']:
            continue  # Skip expired votes

        counts = {}
        for reaction in message.reactions:
            if str(reaction.emoji) in TIME_EMOJIS or str(reaction.emoji) in BOSS_EMOJIS:
                users = await reaction.users().flatten()
                counts[str(reaction.emoji)] = len([u for u in users if not u.bot])

        # Build results message
        if meta['type'] == 'boss':
            mapping = BOSS_EMOJIS
            title = "🗳️ Boss vote results:"
        else:
            mapping = TIME_EMOJIS
            title = "🗳️ Schedule vote results:"

        result_lines = [f"{e}: {counts.get(e, 0)} vote(s)" for e in mapping]
        await ctx.send(f"**{title}**\n" + "\n".join(result_lines))


@bot.command()
async def closevote(ctx):
    now = datetime.now(pytz.timezone('Europe/Paris'))
    expired = []

    for mid, meta in list(vote_data['messages'].items()):
        if now > meta['expires_at'] or ctx is not None:
            ch = bot.get_channel(meta['channel_id'])
            try:
                msg = await ch.fetch_message(mid)
            except discord.NotFound:
                continue

            summary = {}
            for reaction in msg.reactions:
                users = await reaction.users().flatten()
                summary[str(reaction.emoji)] = len([u for u in users if not u.bot])

            result = '\n'.join([f"{emoji}: {count} vote(s)" for emoji, count in summary.items()])
            await ch.send(f"🗳️ **{meta['type'].capitalize()} vote results:**\n{result}")
            expired.append(mid)

    for mid in expired:
        vote_data['messages'].pop(mid, None)

@bot.event
async def on_ready():
    load_event_status()
    scheduler.start()
    auto_start_votes.start()
    print(f"✅ Bot running as {bot.user}")


@bot.event
async def on_member_join(member):
    guild = member.guild

    # Replace this with the name of your Staff role
    staff_role = discord.utils.get(guild.roles, name="Staff")
    if not staff_role:
        print("⚠️ Staff role not found.")
        return

    # Replace with the category name for these onboarding channels
    category = discord.utils.get(guild.categories, name="O N B O A R D I N G")
    if not category:
        category = await guild.create_category("O N B O A R D I N G")

    # Set permissions
    overwrites = {
        guild.default_role:
        discord.PermissionOverwrite(read_messages=False),
        member:
        discord.PermissionOverwrite(read_messages=True, send_messages=True),
        staff_role:
        discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }

    # Create the channel
    safe_name = unicodedata.normalize("NFKD", member.name).encode(
        "ascii", "ignore").decode().lower()
    channel = await guild.create_text_channel(
        name=f"build-{safe_name}",
        overwrites=overwrites,
        category=category,
        topic=f"Private channel for {member.display_name} gear & stat review")

    await channel.send(
        f"👋 Welcome {member.mention}!\n\n"
        "Please share a screenshot of your current **gear and build**.\n"
        "Once reviewed, we’ll grant you access to the rest of the guild.\n"
        "*This channel will remain open to track your progress over time.*")

@bot.command()
@commands.has_permissions(administrator=True)
async def startbossvote(ctx):
    await start_vote(ctx.channel, 'boss', BOSS_EMOJIS)
    await ctx.send("✅ Boss vote started manually.")



async def main():
    await bot.start(TOKEN)


asyncio.run(main())
