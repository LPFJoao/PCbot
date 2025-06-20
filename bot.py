import os
import discord
from discord.ext import commands, tasks
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
from datetime import datetime, timedelta
import unicodedata
import asyncio
import asyncpg  # <<— now using asyncpg instead of psycopg

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
    '🕘': 'Saturday 22:00',
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
#  DATABASE INITIALIZATION & HELPERS (using asyncpg)
# ──────────────────────────────────────────────────────────────────────────────
async def init_db():
    """
    Create tables if they do not exist yet.
    """
    conn = await asyncpg.connect(DATABASE_URL)
    # Create event_status table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS event_status (
            event TEXT PRIMARY KEY,
            enabled BOOLEAN NOT NULL
        )
    """)
    # Insert default events
    for event, enabled in default_event_status().items():
        await conn.execute("""
            INSERT INTO event_status(event, enabled)
            VALUES($1, $2)
            ON CONFLICT (event) DO NOTHING
        """, event, enabled)

    # Create vote_results table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS vote_results (
            id SERIAL PRIMARY KEY,
            type TEXT NOT NULL,
            emoji TEXT NOT NULL,
            count INTEGER NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await conn.close()

async def load_event_status():
    global event_status
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT event, enabled FROM event_status")
    await conn.close()
    # Build dictionary
    event_status = {r['event']: r['enabled'] for r in rows}

async def save_event_status():
    conn = await asyncpg.connect(DATABASE_URL)
    for event, enabled in event_status.items():
        await conn.execute(
            "UPDATE event_status SET enabled = $1 WHERE event = $2",
            enabled, event
        )
    await conn.close()

async def save_vote_results(results):
    """
    Insert the final vote counts into vote_results table.
    `results` is a dict: { "boss": {"<:Daigon:...>": 3, ...}, "schedule": {"🕙":2, ...} }
    """
    print("▶️ save_vote_results() called with:", results)
    conn = await asyncpg.connect(DATABASE_URL)
    for vote_type, summary in results.items():
        for emoji, count in summary.items():
            print(f"   → Inserting row: type={vote_type}, emoji={emoji}, count={count}")
            await conn.execute(
                "INSERT INTO vote_results(type, emoji, count) VALUES($1, $2, $3)",
                vote_type, emoji, count
            )
    await conn.close()
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


# ──────────────────────────────────────────────────────────────────────────────
@bot.command()
async def testsend(ctx):
    ch = bot.get_channel(1371898595288158231)  # your channel ID
    await ch.send("✅ testsend: send perms are good!")
    await ctx.send("…and I just tested it.")

# ──────────────────────────────────────────────────────────────────────────────

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
    """Post a new vote message (“@everyone • Vote for X” ) and add reactions."""
    desc = '@everyone\n**Vote for {0}**\n'.format(t.capitalize())
    for e, label in opts.items():
        desc += f"{e} → {label}\n"
    msg = await channel.send(desc)

    # React with each emoji
    for e in opts.keys():
        await msg.add_reaction(e)

    # Keep this message in memory
    vote_data['messages'][msg.id] = {
        'type': t,
        'channel_id': channel.id,
        'expires_at': datetime.now(pytz.timezone('Europe/Paris')) + timedelta(hours=12)
    }

@bot.command()
async def results(ctx):
    """Fetch the most recent saved vote_results rows (from the DB)."""
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT type, emoji, count
          FROM vote_results
         WHERE created_at >= (
             SELECT MAX(created_at) - INTERVAL '25 seconds'
               FROM vote_results
         )
    """)
    await conn.close()

    if not rows:
        await ctx.send("No vote results found yet.")
        return

    # Group by vote_type
    grouped = {}
    for r in rows:
        grouped.setdefault(r['type'], []).append((r['emoji'], r['count']))

    for vote_type, entries in grouped.items():
        title = "🗳️ Boss vote results:" if vote_type == "boss" else "🗳️ Schedule vote results:"
        lines = [f"{emoji}: {count} vote(s)" for emoji, count in entries]
        await ctx.send(f"**{title}**\n" + "\n".join(lines))

@bot.command()
async def current_results(ctx):
    """Show live reaction counts for any active vote messages."""
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
    print("▶️ closevote invoked; ctx =", ctx)
    now = datetime.now(pytz.timezone('Europe/Paris'))
    expired = []
    final_results = {}

    for mid, meta in list(vote_data['messages'].items()):
        print(f"🔍 Processing message {mid} (type={meta['type']})")

        # ← get the channel FIRST
        ch = bot.get_channel(meta['channel_id'])
        print("   → bot.get_channel returned:", ch)
        if not ch:
            print(f"   ⚠️ Missing channel {meta['channel_id']}, skipping")
            continue

        # ← then fetch inside try
        try:
            msg = await ch.fetch_message(mid)
            print(f"   → fetched message {msg.id} with {len(msg.reactions)} reactions")
            print("   🛠️ Passed fetch, about to build summary")
            await ctx.send("🧪 passed fetch—building summary now")
        except Exception as e:
            print(f"   ❌ fetch_message({mid}) failed:", type(e).__name__, e)
            continue

        # ─── Build & log the summary ───────────────────────────────────────────
        summary = {}
        for reaction in msg.reactions:
            users = await reaction.users().flatten()
            summary[str(reaction.emoji)] = len([u for u in users if not u.bot])
        print("   → summary:", summary)
        await ctx.send(f"🧪 summary is: {summary}")

        # ─── Send the official results back to the poll channel ───────────────
        try:
            sent = await ch.send(
                f"🗳️ **{meta['type'].capitalize()} vote results:**\n"
                + "\n".join(f"{e}: {c} vote(s)" for e, c in summary.items())
            )
            print(f"   ✅ ch.send succeeded (message {sent.id})")
        except Exception as e:
            print("   ❌ ch.send failed:", type(e).__name__, e)

        expired.append(mid)

    # ─── persist & clean up (unchanged) ────────────────────────────────────
    if final_results:
        try:
            await save_vote_results(final_results)
            print("✅ save_vote_results() succeeded.")
        except Exception as e:
            print("❌ save_vote_results() raised:", e)
    else:
        print("ℹ️ No results to save.")

    for mid in expired:
        vote_data['messages'].pop(mid, None)


@bot.command()
@commands.has_permissions(administrator=True)
async def startvote(ctx):
    """
    Manually start both the “schedule” vote and the “boss” vote.
    """
    await start_vote(ctx.channel, 'schedule', TIME_EMOJIS)
    await start_vote(ctx.channel, 'boss', BOSS_EMOJIS)
    await ctx.send("✅ Schedule and Boss votes started manually.")

# Automatically run on Thursdays at 16:00 (Europe/Paris)
@scheduler.scheduled_job('cron', day_of_week='thu', hour=16, minute=0)
async def post_scheduled_votes():
    # Replace this ID with the channel you want both polls to post into
    ch = bot.get_channel(1353371080273952939)
    await start_vote(ch, 'schedule', TIME_EMOJIS)
    await start_vote(ch, 'boss', BOSS_EMOJIS)

# Every 10 minutes, check for expired votes and close them automatically
@tasks.loop(minutes=10)
async def auto_start_votes():
    """
    Check for any expired votes and close them in a single batch.
    """
    now = datetime.now(pytz.timezone('Europe/Paris'))
    if any(now > meta['expires_at'] for meta in vote_data['messages'].values()):
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
    if a user clicks one boss emoji, remove any other boss emoji they might have added.
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
    # DEBUG: confirm that we see the event
    print("▶️ on_member_join fired for:", member.name)

    guild = member.guild

    # 1) Make sure the Staff role exists exactly as spelled below:
    staff_role = discord.utils.get(guild.roles, name="Staff")
    if not staff_role:
        print("⚠️ Staff role not found.")
        return

    # 2) Make sure the category exists exactly as spelled (spaces/case):
    category = discord.utils.get(guild.categories, name="O N B O A R D I N G")
    if not category:
        category = await guild.create_category("O N B O A R D I N G")

    # 3) Set permissions so only the new member + Staff can see/send
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        staff_role: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }

    # 4) Create a “build-<username>” channel
    safe_name = unicodedata.normalize("NFKD", member.name).encode("ascii", "ignore").decode().lower()
    channel = await guild.create_text_channel(
        name=f"build-{safe_name}",
        overwrites=overwrites,
        category=category,
        topic=f"Private channel for {member.display_name} gear & stat review"
    )

    # 5) Send the welcome/instructions message
    await channel.send(
        f"👋 Welcome {member.mention}!\n\n"
        "Please share a screenshot of your current **gear and build**.\n"
        "Once reviewed, we’ll grant you access to the rest of the guild.\n"
        "*This channel will remain open to track your progress over time.*"
    )

# ──────────────────────────────────────────────────────────────────────────────
#  ON_READY: Start DB, load state, kick off scheduler & loops
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
