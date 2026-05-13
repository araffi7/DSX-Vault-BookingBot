import discord
from discord.ext import commands, tasks
import aiosqlite
from datetime import datetime, timedelta
from keep_alive import keep_alive

# ---------------- CONFIG ----------------

import os

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))

GERMANY = datetime.now().astimezone().tzinfo

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

SLOTS = [
    "00:00-00:30",
    "08:00-08:30",
    "16:00-16:30"
]

# ---------------- BOT ----------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- DB ----------------

async def init_db():
    async with aiosqlite.connect("slots.db") as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            day TEXT,
            slot TEXT,
            user_id INTEGER,
            start_ts INTEGER,
            end_ts INTEGER,
            reminded_60 INTEGER DEFAULT 0,
            reminded_30 INTEGER DEFAULT 0,
            reminded_start INTEGER DEFAULT 0
        )
        """)
        await db.commit()

# ---------------- TIME LOGIC ----------------

def get_slot_datetime(day_name, slot):
    now = datetime.now()

    target_weekday = DAYS.index(day_name)
    current_weekday = now.weekday()

    days_ahead = target_weekday - current_weekday
    if days_ahead < 0:
        days_ahead += 7

    ingame_hour = int(slot.split(":")[0])
    minute = int(slot.split(":")[1].split("-")[0])

    # INGAME OFFSET (+4h = DE time)
    real_hour = (ingame_hour + 4) % 24

    target_date = now + timedelta(days=days_ahead)
    target_date = target_date.replace(
        hour=real_hour,
        minute=minute,
        second=0,
        microsecond=0
    )

    return target_date

# ---------------- UI BUTTON ----------------

class SlotButton(discord.ui.Button):
    def __init__(self, day, slot, booked=False):
        self.day = day
        self.slot = slot

        super().__init__(
            label=f"{day[:3]} {slot}",
            style=discord.ButtonStyle.danger if booked else discord.ButtonStyle.success,
            custom_id=f"{day}-{slot}"
        )

    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        async with aiosqlite.connect("slots.db") as db:

            # Prüfen ob Slot existiert
            async with db.execute(
                "SELECT * FROM bookings WHERE day=? AND slot=?",
                (self.day, self.slot)
            ) as cursor:
                existing = await cursor.fetchone()

            # Slot freigeben wenn selber Nutzer
            if existing and existing[2] == user_id:
                await db.execute(
                    "DELETE FROM bookings WHERE day=? AND slot=?",
                    (self.day, self.slot)
                )

                await db.commit()

                await log_action(
                    f"❌ Released: {self.day} {self.slot} by <@{user_id}>"
                )

            # Bereits von anderem gebucht
            elif existing:
                await interaction.response.defer()
                return

            # Neu buchen
            else:
                start = get_slot_datetime(self.day, self.slot)
                end = start + timedelta(minutes=30)

                await db.execute("""
                    INSERT INTO bookings
                    (day, slot, user_id, start_ts, end_ts,
                    reminded_60, reminded_30, reminded_start)
                    VALUES (?, ?, ?, ?, ?, 0, 0, 0)
                """, (
                    self.day,
                    self.slot,
                    user_id,
                    int(start.timestamp()),
                    int(end.timestamp())
                ))

                await db.commit()

                await log_action(
                    f"📌 Booked: {self.day} {self.slot} by <@{user_id}>"
                )

        await send_calendar()
        await interaction.response.defer()

# ---------------- VIEW ----------------

class SlotView(discord.ui.View):
    def __init__(self, bookings):
        super().__init__(timeout=None)

        for day in DAYS:
            for slot in SLOTS:
                booked = (day, slot) in bookings
                self.add_item(SlotButton(day, slot, booked))

# ---------------- LOG ----------------

async def log_action(text):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        await channel.send(text)

# ---------------- BOOKING MESSAGE ----------------

async def send_calendar():
    channel = bot.get_channel(CHANNEL_ID)

    if not channel:
        print("Channel not found")
        return

    async with aiosqlite.connect("slots.db") as db:
        async with db.execute("SELECT * FROM bookings") as cursor:
            rows = await cursor.fetchall()

    bookings = {}
    for row in rows:
        bookings[(row[0], row[1])] = row[2]

    now = datetime.now()

    german_time = now.strftime("%H:%M")
    ingame_time = (now - timedelta(hours=4)).strftime("%H:%M")

    text = "🎯 **Vault Slot Calendar**\n"
    text += f"📅 Current Week: {now.strftime('%d.%m.%Y')}\n"
    text += f"🇩🇪 German Time: `{german_time}`\n"
    text += f"🎮 Ingame Time: `{ingame_time}`\n\n"

    for day in DAYS:
        text += f"## {day}\n"

        for slot in SLOTS:
            key = (day, slot)

            if key in bookings:
                user_id = bookings[key]
                text += f"🔴 `{slot}` → Booked by <@{user_id}>\n"
            else:
                text += f"🟢 `{slot}` → Free\n"

        text += "\n"

    view = SlotView(bookings)

    messages = [msg async for msg in channel.history(limit=10)]

    for msg in messages:
        if msg.author == bot.user:
            await msg.delete()

    await channel.send(
        text,
        view=view
    )
# ---------------- REMINDER LOOP ----------------

@tasks.loop(seconds=30)
async def reminder_loop():
    now = datetime.now()

    async with aiosqlite.connect("slots.db") as db:
        async with db.execute("SELECT * FROM bookings") as cursor:
            rows = await cursor.fetchall()

            for row in rows:
                day, slot, user_id, start_ts, end_ts, r60, r30, rstart = row

                start = datetime.fromtimestamp(start_ts)
                end = datetime.fromtimestamp(end_ts)

                channel = bot.get_channel(CHANNEL_ID)

                # 60 min reminder
                if not r60 and start - timedelta(hours=1) <= now:
                    await channel.send(f"⏰ @everyone Vault Slot in 1 hour: {day} {slot}")
                    await db.execute("UPDATE bookings SET reminded_60=1 WHERE day=? AND slot=?", (day, slot))

                # 30 min reminder
                if not r30 and start - timedelta(minutes=30) <= now:
                    await channel.send(f"⏰ @everyone Vault Slot in 30 minutes: {day} {slot}")
                    await db.execute("UPDATE bookings SET reminded_30=1 WHERE day=? AND slot=?", (day, slot))

                # start
                if not rstart and start <= now:
                    await channel.send(f"🚀 Vault Slot STARTING NOW: {day} {slot}")
                    await db.execute("UPDATE bookings SET reminded_start=1 WHERE day=? AND slot=?", (day, slot))

                # auto release
                if end <= now:
                    await db.execute("DELETE FROM bookings WHERE day=? AND slot=?", (day, slot))
                    await log_action(f"♻️ Auto released: {day} {slot}")

        await db.commit()

# ---------------- EVENTS ----------------

@bot.event
async def on_ready():
    print(f"Bot online as {bot.user}")

    await init_db()
    await send_calendar()

    reminder_loop.start()

# ---------------- START ----------------

keep_alive()
bot.run(TOKEN)
