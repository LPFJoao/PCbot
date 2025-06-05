import os
import discord
from discord.ext import commands, tasks
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
from datetime import datetime, timedelta
import unicodedata
import asyncio
import psycopg

# Load environment variables
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
scheduler = AsyncIOScheduler(timezone="Europe/Paris")

# Default event reminder states
def default_event_status():
    return {
        "boonstone": False,
        "riftstone": False,
        "siege": False,
        "tax": False
    }

event_status = default_event_status()
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

async def init_db():
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS event_status (
                    event TEXT PRIMARY KEY,
                    enabled BOOLEAN NOT NULL
                )
            """)
            for event, enabled in default_event_status().items():
                await cur.execute("""
                    INSERT INTO event_status (event, enabled)
                    VALUES (%s, %s)
                    ON CONFLICT (event) DO NOTHING
                """, (event, enabled))
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
            event_status = {event: enabled for event, enabled in rows}

async def save_event_status():
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            for event, enabled in event_status.items():
                await cur.execute("UPDATE event_status SET enabled = %s WHERE event = %s", (enabled, event))
        await conn.commit()

async def save_vote_results(results):
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            for vote_type, summary in results.items():
                for emoji, count in summary.items():
                    await cur.execute("""
                        INSERT INTO vote_results (type, emoji, count)
                        VALUES (%s, %s, %s)
                    """, (vote_type, emoji, count))
        await conn.commit()

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
    lines = [f"{e.capitalize()}: {'ON' if state else 'OFF'}" for e, state in event_status.items()]
    await ctx.send("**Reminder Status**\n" + "\n".join(lines))

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

@bot.command()
async def results(ctx):
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT type, emoji, count 
                FROM vote_results
                WHERE created_at >= (
                    SELECT MAX(created_at) - INTERVAL '25 seconds'
                    FROM vote_results)
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
        lines = [f"{e}: {counts.get(e, 0)} vote(s)" for e in (BOSS_EMOJIS if meta['type'] == 'boss' else TIME_EMOJIS)]
        await ctx.send(f"**Live results for {meta['type']} vote:**\n" + "\n".join(lines))

@bot.command()
async def closevote(ctx):
    now = datetime.now(pytz.timezone('Europe/Paris'))
    expired = []
    final_results = {}

    @bot.command()
async def closevote(ctx):
    now = datetime.now(pytz.timezone('Europe/Paris'))
    expired = []
    final_results = {}

    # 1) Build up final_results from all active vote messages:
    for mid, meta in list(vote_data['messages'].items()):
        if now > meta['expires_at'] or ctx is not None:
            ch = bot.get_channel(meta['channel_id'])
            if not ch:
                continue

            try:
                msg = await ch.fetch_message(mid)
            except discord.NotFound:
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

    # 2) Debug-print what we intend to save:
    if final_results:
        print("🔍 closevote() final_results:", final_results)
        await save_vote_results(final_results)
        print("✅ closevote() running save_vote_results() succeeded.")
    else:
        print("ℹ️ closevote() found no final_results to save.")

    # 3) Remove expired vote messages from memory:
    for mid in expired:
        vote_data['messages'].pop(mid, None)



async def save_vote_results(results):
    print("🔍 save_vote_results() called with:", results)
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

@bot.command()
@commands.has_permissions(administrator=True)
async def startvote(ctx):
    await start_vote(ctx.channel, 'schedule', TIME_EMOJIS)
    await start_vote(ctx.channel, 'boss', BOSS_EMOJIS)
    await ctx.send("✅ Schedule and Boss votes started manually.")

@scheduler.scheduled_job('cron', day_of_week='thu', hour=16, minute=0)
async def post_scheduled_votes():
    ch = bot.get_channel(1353371080273952939)
    await start_vote(ch, 'schedule', TIME_EMOJIS)
    await start_vote(ch, 'boss', BOSS_EMOJIS)

@tasks.loop(minutes=10)
async def auto_start_votes():
    now = datetime.now(pytz.timezone('Europe/Paris'))
    for mid, meta in list(vote_data['messages'].items()):
        if now > meta['expires_at']:
            await closevote(None)

@bot.command()
async def help(ctx):
    help_text = """!activate <event> — Enables reminders
!deactivate <event> — Disables reminders
!status — Shows reminder status
!startvote — Manually starts both votes
!results — Shows saved results after vote closes
!current_results — Shows live vote counts
!closevote — Forces vote closure and saves results"""
    await ctx.send(f"```{help_text}```")

@bot.event
async def on_raw_reaction_add(payload):
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
