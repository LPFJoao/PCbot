import os
import discord
from discord.ext import commands, tasks
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime, timedelta
import unicodedata
import asyncio
import asyncpg  # using asyncpg instead of psycopg

# ───────────────────────────────────────────────────────────────────────────
# Load environment variables
# ───────────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# ───────────────────────────────────────────────────────────────────────────
# Intents & Bot initialization
# ───────────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
scheduler = AsyncIOScheduler(timezone="Europe/Paris")

# ───────────────────────────────────────────────────────────────────────────
# Default event-status for reminders
# ───────────────────────────────────────────────────────────────────────────
def default_event_status():
    return {
        "boonstone": False,
        "riftstone": False,
        "siege": False,
        "tax": False
    }

event_status = default_event_status()

# ───────────────────────────────────────────────────────────────────────────
# Database initialization & helpers
# ───────────────────────────────────────────────────────────────────────────
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    # Reminder status
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS event_status (
            event TEXT PRIMARY KEY,
            enabled BOOLEAN NOT NULL
        )
    """)
    for event, enabled in default_event_status().items():
        await conn.execute(
            """
            INSERT INTO event_status(event, enabled)
            VALUES($1,$2) ON CONFLICT(event) DO NOTHING
            """, event, enabled
        )
    # Poll results table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS vote_results (
            id SERIAL PRIMARY KEY,
            type TEXT NOT NULL,
            option TEXT NOT NULL,
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
    event_status = {r['event']: r['enabled'] for r in rows}

async def save_event_status():
    conn = await asyncpg.connect(DATABASE_URL)
    for event, enabled in event_status.items():
        await conn.execute("UPDATE event_status SET enabled=$1 WHERE event=$2", enabled, event)
    await conn.close()

# ───────────────────────────────────────────────────────────────────────────
# Reminder Commands: activate / deactivate / status
# ───────────────────────────────────────────────────────────────────────────
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

# ───────────────────────────────────────────────────────────────────────────
# Test-send command
# ───────────────────────────────────────────────────────────────────────────
@bot.command()
async def testsend(ctx):
    ch = bot.get_channel(ctx.channel.id)
    await ch.send("✅ testsend: send perms are good!")
    await ctx.send("…and I just tested it.")

# ───────────────────────────────────────────────────────────────────────────
# Onboarding: Create a "build-<username>" channel for new joiners
# ───────────────────────────────────────────────────────────────────────────
@bot.event
async def on_member_join(member):
    print(f"▶️ on_member_join: {member.name}")
    guild = member.guild
    staff_role = discord.utils.get(guild.roles, name="Staff")
    if not staff_role:
        return
    category = discord.utils.get(guild.categories, name="O N B O A R D I N G")
    if not category:
        category = await guild.create_category("O N B O A R D I N G")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            read_messages=False),
        member: discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True,
            attach_files=True,
            embed_links=True
        ),
        staff_role: discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True,
            attach_files=True,
            embed_links=True
        )
    }
    safe = unicodedata.normalize("NFKD", member.name).encode('ascii','ignore').decode().lower()
    channel = await guild.create_text_channel(
        name=f"build-{safe}", category=category,
        overwrites=overwrites,
        topic=f"Private channel for {member.display_name} gear review"
    )
    await channel.send(f"👋 Welcome {member.mention}!\nPlease share a screenshot of your build.")

# ───────────────────────────────────────────────────────────────────────────
# Button-based Poll Implementation
# ───────────────────────────────────────────────────────────────────────────
from discord import ButtonStyle, Interaction
from discord.ui import View, Button

class PollButton(Button):
    def __init__(self, label: str, counts: dict, voters: dict):
        super().__init__(style=ButtonStyle.primary, label=label)
        self.counts = counts
        self.voters = voters

    async def callback(self, interaction: Interaction):
        user_id = interaction.user.id
        prev = self.voters.get(user_id)
        if prev:
            self.counts[prev] -= 1
        self.voters[user_id] = self.label
        self.counts[self.label] += 1
        lines = [f"**{opt}** — {cnt} vote(s)" for opt, cnt in self.counts.items()]
        await interaction.response.edit_message(
            content="📊 **Poll Results**\n" + "\n".join(lines),
            view=self.view
        )

class PollView(View):
    def __init__(self, options: list[str], timeout: float = 12*3600):  # 12h default
        super().__init__(timeout=timeout)
        self.counts = {opt: 0 for opt in options}
        self.voters = {}
        for opt in options:
            self.add_item(PollButton(opt, self.counts, self.voters))

    async def on_timeout(self):
        # disable all buttons
        for item in self.children:
            item.disabled = True
        # build final summary
        lines = [f"{opt}: {cnt} vote(s)" for opt, cnt in self.counts.items()]
        results = "📊 **Poll Closed - Final Results**\n" + "\n".join(lines)
        # edit original message with results and disabled view
        await self.message.edit(content=results, view=self)
        # send a separate summary message
        await self.message.channel.send(f"@everyone\n{results}")

async def create_poll(channel, question: str, options: list[str], timeout_s: float = 12*3600):
    header = f"@everyone\n📢 **{question}**\nClick a button to vote!"
    view = PollView(options, timeout=timeout_s)
    msg = await channel.send(header, view=view)
    view.message = msg

# ───────────────────────────────────────────────────────────────────────────
# Scheduled Weekly Polls: Thursdays at 16:00 Paris time
# ───────────────────────────────────────────────────────────────────────────
@scheduler.scheduled_job(
    trigger=CronTrigger(day_of_week='thu', hour=14, minute=00, timezone='Europe/Paris')
)
async def weekly_polls():
    ch = bot.get_channel(1353371080273952939)  # your channel ID
    # Time-slot poll
    await create_poll(
        ch,
        "When should we run this weekend’s Guild Boss runs?",
        ["Friday 22:00", "Saturday 18:00", "Saturday 22:00", "Sunday 18:00", "Sunday 21:00"],
        timeout_s=12*3600
    )
    # Boss-choice poll
    await create_poll(
        ch,
        "Which boss are we targeting?",
        ["Daigon", "Pakilo Naru", "Leviathan", "Manticus"],
        timeout_s=12*3600
    )

# ───────────────────────────────────────────────────────────────────────────
# On ready: start database, load state, scheduler
# ───────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()
    await load_event_status()
    scheduler.start()
    print(f"✅ Bot running as {bot.user}")

async def main():
    await bot.start(TOKEN)

asyncio.run(main())
