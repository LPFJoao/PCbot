import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime, timedelta
import unicodedata
import asyncio
import asyncpg  

# ───────────────────────────────────────────────────────────────────────────
# Custom assets
# ───────────────────────────────────────────────────────────────────────────
EMOJI_MAP = {
    "Tank":   "<:myTankEmoji:1401856113623826524>",
    "DPS":    "<:myDPSEmoji:1401856076449579049>",
    "Healer": "<:myHealerEmoji:1401856048230432809>",
}

STYLE_MAP = {
    "Tank":   discord.ButtonStyle.danger,   # red
    "DPS":    discord.ButtonStyle.primary,  # blue
    "Healer": discord.ButtonStyle.success,  # green
}

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
    # Reminder status table
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
    guild = member.guild
    staff_role = discord.utils.get(guild.roles, name="Staff")
    if not staff_role:
        return

    category = discord.utils.get(guild.categories, name="O N B O A R D I N G")
    if not category:
        category = await guild.create_category("O N B O A R D I N G")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        member: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, embed_links=True),
        staff_role: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, embed_links=True)
    }

    safe = unicodedata.normalize("NFKD", member.name).encode('ascii','ignore').decode().lower()
    channel = await guild.create_text_channel(
        name=f"build-{safe}", category=category,
        overwrites=overwrites,
        topic=f"Private channel for {member.display_name} gear review"
    )
    await channel.send(
        f"👋 Welcome {member.mention}!\n"
        "Please share a screenshot of your current gear and build.\n"
        "This channel will remain open for questions related to your build."
    )


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
    def __init__(self, options: list[str], timeout: float = 12*3600):
        super().__init__(timeout=timeout)
        self.counts = {opt: 0 for opt in options}
        self.voters = {}
        for opt in options:
            self.add_item(PollButton(opt, self.counts, self.voters))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        lines = [f"{opt}: {cnt} vote(s)" for opt, cnt in self.counts.items()]
        results = "📊 **Poll Closed - Final Results**\n" + "\n".join(lines)

        await self.message.edit(content=results, view=self)
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
    trigger=CronTrigger(day_of_week='thu', hour=14, minute=0, timezone='Europe/Paris')
)
async def weekly_polls():
    ch = bot.get_channel(1353371080273952939)  # your channel ID
    await create_poll(
        ch,
        "When should we run this weekend’s Guild Boss runs?",
        ["Friday 22:00 CET", "Saturday 18:00 CET", "Saturday 22:00 CET", "Sunday 18:00 CET", "Sunday 21:00 CET"],
        timeout_s=12*3600
    )
    await create_poll(
        ch,
        "Which boss are we targeting?",
        ["Daigon", "Pakilo Naru", "Leviathan", "Manticus"],
        timeout_s=12*3600
    )


# ───────────────────────────────────────────────────────────────────────────
# Attendance system
# ───────────────────────────────────────────────────────────────────────────
class AttendanceView(discord.ui.View):
    def __init__(self, title: str, raid_time_unix: int, details: str):
        super().__init__(timeout=None)
        self.title = title
        self.details = details
        self.raid_time_unix = raid_time_unix
        self.signups = {"Tank": [], "DPS": [], "Healer": []}
        self.message: discord.Message | None = None

        for role in ["Tank", "DPS", "Healer"]:
            self.add_item(RoleButton(role, parent=self))

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"🗓️ {self.title}", color=discord.Color.blurple())
        
        # Add description if provided
        if self.details:
            embed.add_field(name="📝 Details", value=self.details, inline=False)

        embed.add_field(name="🕡 Raid Time", value=f"<t:{self.raid_time_unix}:F>", inline=False)
        embed.add_field(name="⏳ Countdown", value=f"<t:{self.raid_time_unix}:R>", inline=False)

        for role, members in self.signups.items():
            icon = EMOJI_MAP.get(role, "")
            names = "\n".join(members) if members else "—"
            embed.add_field(
                name=f"{icon} {role}s ({len(members)})",
                value=names,
                inline=False
            )

        return embed


    async def update_message(self):
        if self.message:
            await self.message.edit(embed=self.build_embed(), view=self)


class RoleButton(discord.ui.Button):
    def __init__(self, role_label: str, parent: AttendanceView):
        emoji = EMOJI_MAP[role_label]
        style = STYLE_MAP[role_label]
        super().__init__(label=role_label, emoji=emoji, style=style)
        self.role_label = role_label
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user.display_name
        # Remove from previous role
        for lst in self.parent_view.signups.values():
            if user in lst:
                lst.remove(user)
        self.parent_view.signups[self.role_label].append(user)
        
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(),
            view=self.parent_view
        )


@bot.tree.command(name="attendance", description="Create a raid attendance signup")
@app_commands.describe(
    title="Raid title (e.g., BOONSTONE)",
    details="Additional details or notes for the raid",
    date="Date of the raid (YYYY-MM-DD)",
    time="Start time in 24h format (HH:MM)"
)
async def attendance(interaction: discord.Interaction, title: str, date: str, time: str, details: str):
    try:
        # Role check
        if not discord.utils.get(interaction.user.roles, name="Staff"):
            await interaction.response.send_message(
                "❌ You need the **Staff** role to use this.", ephemeral=True
            )
            return

        # Parse datetime
        try:
            dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
            dt = pytz.timezone("Europe/Paris").localize(dt)
            unix_ts = int(dt.timestamp())
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid format. Use YYYY-MM-DD for date and HH:MM (24h) for time.",
                ephemeral=True
            )
            return

        # Ack the command
        await interaction.response.send_message("✅ Attendance created!", ephemeral=True)

        # Send public embed + buttons
        view = AttendanceView(title, unix_ts, details)
        embed = view.build_embed()

        # This line is critical; we'll check here if it silently fails
        msg = await interaction.followup.send(
             content="@everyone", 
             embed=embed,
             view=view
 )
        view.message = msg

    except Exception as e:
        print(f"⚠️ Error in /attendance command: {e}")
# ───────────────────────────────────────────────────────────────────────────
# On ready: start DB, load state, scheduler, sync commands
# ───────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()
    await load_event_status()
    scheduler.start()
    await bot.tree.sync()
    print(f"✅ Bot ready as {bot.user}")


async def main():
    await bot.start(TOKEN)

asyncio.run(main())
