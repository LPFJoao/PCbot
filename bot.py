import os
import discord
from discord.ext import commands, tasks
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
from datetime import datetime, timedelta
import unicodedata
import asyncio
import psycopg

# ──────────────────────────────────────────────────────────────────────────────
#  Load environment variables
# ──────────────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# ──────────────────────────────────────────────────────────────────────────────
#  Intents & Bot initialization
# ──────────────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
scheduler = AsyncIOScheduler(timezone="Europe/Paris")

# ──────────────────────────────────────────────────────────────────────────────
#  “Default” event-status (for your reminder commands)
# ──────────────────────────────────────────────────────────────────────────────
def default_event_status():
    return {
        "boonstone": False,
        "riftstone": False,
        "siege": False,
        "tax": False
    }

event_status = default_event_status()

# ──────────────────────────────────────────────────────────────────────────────
#  Vote data structure (keeps track of active vote messages in memory)
# ──────────────────────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────────────────────
#  DATABASE INITIALIZATION
# ──────────────────────────────────────────────────────────────────────────────
async def init_db():
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            # Create (or ensure) event_status table
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS event_status (
                    event TEXT PRIMARY KEY,
                    enabled BOOLEAN NOT NULL
                )
            """)
            # Insert the default events if they do not already exist
            for event, enabled in default_event_status().items():
                await cur.execute("""
                    INSERT INTO event_status (event, enabled)
                    VALUES (%s, %s)
                    ON CONFLICT (event) DO NOTHING
                """, (event, enabled))

            # Create (or ensure) vote_results table
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS vote_results (
                    id SERIAL PRIMARY KEY,
                    type TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        await conn.commit()

async def load_event_status():
    global event_status
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT event, enabled FROM event_status")
            rows = await cur.fetchall()
            # Build a dictionary back into event_status
            event_status = {event: enabled for event, enabled in rows}

async def save_event_status():
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            for event, enabled in event_status.items():
                await cur.execute(
                    "UPDATE event_status SET enabled = %s WHERE event = %s",
                    (enabled, event)
                )
        await conn.commit()

async def save_vote_results(results):
    """Insert the final vote counts into vote_results table."""
    print("▶️ save_vote_results() called with:", results)
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            for vote_type, summary in results.items():
                for emoji, count in summary.items():
                    print(f"   → Inserting row: type={vote_type}, emoji={emoji}, count={count}")
                    await cur.execute(
                        "INSERT INTO vote_results (type, emoji, count) VALUES (%s, %s, %s)",
                        (vote_type, emoji, count)
                    )
        await conn.commit()
        print("✅ save_vote_results() committed to database.")

# ──────────────────────────────────────────────────────────────────────────────
#  REMINDER COMMANDS (activate/deactivate/status)
# ──────────────────────────────────────────────────────────────────────────────
@bot.command()
async def activate(ctx, event: str):
    key = event.lower()
    if key in event_status:
        event_status[key] = True
        await save_event_status()
        await ctx.send(f"✅ Activated {key} reminders.")
    else:
        await ctx.send("❌ Unknown event. Options: boonstone, riftstone, siege, tax.")

@bot.command()
async def deactivate(ctx, event: str):
    key = event.lower()
    if key in event_status:
        event_status[key] = False
        await save_event_status()
        await ctx.send(f"❌ Deactivated {key} reminders.")
    else:
        await ctx.send("❌ Unknown event. Options: boonstone, riftstone, siege, tax.")

@bot.command()
async def status(ctx):
    lines = [
        f"{e.capitalize()}: {'ON' if state else 'OFF'}"
        for e, state in event_status.items()
    ]
    await ctx.send("**Reminder Status**\n" + "\n".join(lines))

# ──────────────────────────────────────────────────────────────────────────────
#  VOTING SYSTEM
# ──────────────────────────────────────────────────────────────────────────────
async def start_vote(channel, t, opts):
    """Post a new vote message (“@everyone • Vote for X”) and add reactions."""
    desc = '@everyone\n**Vote for {0}**\n'.format(t.capitalize())
    for e, label in opts.items():
        desc += f"{e} → {label}\n"
    msg = await channel.send(desc)

    # React with each emoji
    for e in opts.keys():
        await msg.add_reaction(e)

    # Keep track of this message ID in vote_data
    vote_data['messages'][msg.id] = {
        'type': t,
        'channel_id': channel.id,
        'expires_at': datetime.now(pytz.timezone('Europe/Paris')) + timedelta(hours=12)
    }

@bot.command()
async def results(ctx):
    """Fetch the most recent saved vote_results rows (from the DB)."""
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT type, emoji, count
                FROM vote_results
                WHERE created_at >= (
                    SELECT MAX(created_at) - INTERVAL '25 seconds'
                    FROM vote_results
                )
            """)
            rows = await cur.fetchall()
            if not rows:
                await ctx.send("No vote results found yet.")
                return

            grouped = {}
            for vote_type, emoji, count in rows:
                grouped.setdefault(vote_type, []).append((emoji, count))

            for vote_type, entries in grouped.items():
                title = "🗳️ Boss vote results:" if vote_type == "boss" else "🗳️ Schedule vote results:"
                lines = [f"{emoji}: {count} vote(s)" for emoji, count in entries]
                await ctx.send(f"**{title}**\n" + "\n".join(lines))

@bot.command()
async def current_results(ctx):
    """Show live reaction counts for any currently‐active vote messages."""
    for msg_id, meta in vote_data['messages'].items():
        ch = bot.get_channel(meta['channel_id'])
        if not ch:
            continue
        try:
            msg = await ch.fetch_message(msg_id)
        except discord.NotFound:
            continue

        counts = {}
        for reaction in msg.reactions:
            users = await reaction.users().flatten()
            counts[str(reaction.emoji)] = len([u for u in users if not u.bot])

        mapping = BOSS_EMOJIS if meta['type'] == 'boss' else TIME_EMOJIS
        lines = [f"{emoji}: {counts.get(emoji, 0)} vote(s)" for emoji in mapping.keys()]
        await ctx.send(f"**Live results for {meta['type']} vote:**\n" + "\n".join(lines))

@bot.command()
async def closevote(ctx):
    """
    1) “Close” any active votes (either expired or forced by !closevote).
    2) Post a summary in each vote’s channel.
    3) Insert those final counts into vote_results table.
    4) Remove them from memory.
    """
    print("▶️ closevote() called. vote_data['messages'].keys() =", vote_data['messages'].keys())

    now = datetime.now(pytz.timezone('Europe/Paris'))
    expired = []
    final_results = {}

    # 1) Gather reactions on every vote‐message still in memory
    for mid, meta in list(vote_data['messages'].items()):
        # Close it if it’s expired or if !closevote was invoked (ctx is not None)
        if now > meta['expires_at'] or ctx is not None:
            ch = bot.get_channel(meta['channel_id'])
            if not ch:
                print(f"⚠️ closevote: channel {meta['channel_id']} not found for message {mid}")
                continue

            try:
                msg = await ch.fetch_message(mid)
            except discord.NotFound:
                print(f"⚠️ closevote: message {mid} not found. Skipping.")
                continue

            # Count up reactions (excluding bots)
            summary = {}
            for reaction in msg.reactions:
                users = await reaction.users().flatten()
                summary[str(reaction.emoji)] = len([u for u in users if not u.bot])

            final_results[meta['type']] = summary
            await ch.send(
                f"🗳️ **{meta['type'].capitalize()} vote results:**\n" +
                "\n".join([f"{emoji}: {count} vote(s)" for emoji, count in summary.items()])
            )

            expired.append(mid)

    # 2) Insert into DB if we have any real results
    if final_results:
        print("🔍 closevote() final_results to save:", final_results)
        try:
            await save_vote_results(final_results)
            print("✅ closevote() save_vote_results() succeeded.")
        except Exception as e:
            print("❌ closevote() save_vote_results() raised exception:", e)
    else:
        print("ℹ️ closevote() found no final_results to save (maybe vote_data was empty?).")

    # 3) Remove them from memory
    for mid in expired:
        vote_data['messages'].pop(mid, None)

@bot.command()
@commands.has_permissions(administrator=True)
async def startvote(ctx):
    """
    Manually start both the “schedule” vote and the “boss” vote
    (as if it were Thursday 16:00).
    """
    await start_vote(ctx.channel, 'schedule', TIME_EMOJIS)
    await start_vote(ctx.channel, 'boss', BOSS_EMOJIS)
    await ctx.send("✅ Schedule and Boss votes started manually.")

# Schedule the weekly Thursday 16:00 votes automatically
@scheduler.scheduled_job('cron', day_of_week='thu', hour=16, minute=0)
async def post_scheduled_votes():
    # Replace 1353371080273952939 with your real channel ID
    ch = bot.get_channel(1353371080273952939)
    await start_vote(ch, 'schedule', TIME_EMOJIS)
    await start_vote(ch, 'boss', BOSS_EMOJIS)

# Periodically check if any stored vote‐messages have just expired
@tasks.loop(minutes=10)
async def auto_start_votes():
    now = datetime.now(pytz.timezone('Europe/Paris'))
    for mid, meta in list(vote_data['messages'].items()):
        if now > meta['expires_at']:
            await closevote(None)

@bot.command()
async def help(ctx):
    help_text = """!activate <event>       — Enables a reminder (boonstone, riftstone, siege, tax)
!deactivate <event>     — Disables a reminder
!status                 — Shows ON/OFF status of all reminders

!startvote              — Manually starts both “schedule” and “boss” votes
!current_results        — Shows live vote counts for active vote(s)
!closevote              — Forces vote to close, posts results immediately, and saves to DB
!results                — Shows the most‐recently saved vote results

"""
    await ctx.send(f"```{help_text}```")

@bot.event
async def on_raw_reaction_add(payload):
    """
    Enforce a “one boss‐vote only” rule:
    if a user clicks one boss emoji, remove any other boss emojis they might have added.
    """
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
        user = await guild.fetch_member(payload.user_id)
        if user.bot:
            return
    except discord.NotFound:
        return

    added = str(payload.emoji)
    for reaction in message.reactions:
        react = str(reaction.emoji)
        if react in BOSS_EMOJIS and react != added:
            try:
                await message.remove_reaction(reaction.emoji, user)
            except:
                pass

# ──────────────────────────────────────────────────────────────────────────────
#  ONBOARDING: CREATE A “build-<username>” CHANNEL FOR NEW JOINERS
# ──────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_member_join(member):
    guild = member.guild

    # Make sure the “Staff” role exists exactly as spelled below:
    staff_role = discord.utils.get(guild.roles, name="Staff")
    if not staff_role:
        print("⚠️ Staff role not found.")
        return

    # Make sure the category “O N B O A R D I N G” exists (spaces and casing must match):
    category = discord.utils.get(guild.categories, name="O N B O A R D I N G")
    if not category:
        category = await guild.create_category("O N B O A R D I N G")

    # Lock permissions so only the new member + Staff can see/send
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        staff_role: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }

    safe_name = unicodedata.normalize("NFKD", member.name).encode("ascii", "ignore").decode().lower()
    channel = await guild.create_text_channel(
        name=f"build-{safe_name}",
        overwrites=overwrites,
        category=category,
        topic=f"Private channel for {member.display_name} gear & stat review"
    )

    await channel.send(
        f"👋 Welcome {member.mention}!\n\n"
        "Please share a screenshot of your current **gear and build**.\n"
        "Once reviewed, we’ll grant you access to the rest of the guild.\n"
        "*This channel will remain open to track your progress over time.*"
    )

# ──────────────────────────────────────────────────────────────────────────────
#  ON_READY: Start DB, load state, kick off scheduler/loops
# ──────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()
    await load_event_status()
    scheduler.start()
    auto_start_votes.start()
    print(f"✅ Bot running as {bot.user}")

async def main():
    await bot.start(TOKEN)

asyncio.run(main())
