import discord
from discord.ext import commands, tasks
import aiosqlite
from datetime import datetime, timedelta
from keep_alive import keep_alive
import os

# ---------------- CONFIG ----------------

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))

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

        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        await db.commit()


# ---------------- SETTINGS ----------------

async def set_setting(key, value):
    async with aiosqlite.connect("slots.db") as db:
        await db.execute("""
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))
        await db.commit()


async def get_setting(key):
    async with aiosqlite.connect("slots.db") as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
            row = await c.fetchone()
            return row[0] if row else None


# ---------------- TIME ----------------

def now_times():
    """
    Germany = UTC+2
    Ingame = Germany -4h
    """

    now_utc = datetime.utcnow()

    now_de = now_utc + timedelta(hours=2)
    now_ingame = now_de - timedelta(hours=4)

    return now_de, now_ingame


def get_next_weekday_date(target_weekday):
    now_de, _ = now_times()

    current_weekday = now_de.weekday()

    days_ahead = target_weekday - current_weekday

    if days_ahead < 0:
        days_ahead += 7

    return now_de + timedelta(days=days_ahead)


def get_slot_datetime(day_name, slot):
    """
    Slot time is INGAME TIME.
    Reminder + release use GERMAN TIME.
    """

    target_weekday = DAYS.index(day_name)

    target_date = get_next_weekday_date(target_weekday)

    ingame_hour = int(slot.split(":")[0])
    minute = int(slot.split(":")[1].split("-")[0])

    # Convert ingame -> german time
    german_hour = (ingame_hour + 4) % 24

    target_date = target_date.replace(
        hour=german_hour,
        minute=minute,
        second=0,
        microsecond=0
    )

    return target_date


# ---------------- LOG ----------------

async def log_action(text):
    ch = bot.get_channel(LOG_CHANNEL_ID)

    if ch:
        await ch.send(text)


# ---------------- CALENDAR ----------------

async def build_calendar():

    async with aiosqlite.connect("slots.db") as db:
        async with db.execute(
            "SELECT day, slot, user_id FROM bookings"
        ) as c:
            rows = await c.fetchall()

    bookings = {(d, s): u for d, s, u in rows}

    now_de, now_ingame = now_times()

    text = "🎯 **Vault Slot Calendar**\n\n"

    text += f"📅 Current Week: `{now_de.strftime('%d.%m.%Y')}`\n"
    text += f"🇩🇪 German Time: `{now_de.strftime('%H:%M')}`\n"
    text += f"🎮 Ingame Time: `{now_ingame.strftime('%H:%M')}`\n\n"

    text += "ℹ️ Ingame Time = German Time -4 Hours\n\n"

    for day in DAYS:

        text += f"## {day}\n"

        for slot in SLOTS:

            if (day, slot) in bookings:

                user_id = bookings[(day, slot)]

                text += f"🔴 `{slot}` → Booked by <@{user_id}>\n"

            else:

                text += f"🟢 `{slot}` → Free\n"

        text += "\n"

    return text, bookings


# ---------------- BUTTONS ----------------

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

            cur = await db.execute(
                "SELECT user_id FROM bookings WHERE day=? AND slot=?",
                (self.day, self.slot)
            )

            existing = await cur.fetchone()

            # RELEASE OWN SLOT
            if existing:

                if existing[0] == user_id:

                    await db.execute(
                        "DELETE FROM bookings WHERE day=? AND slot=?",
                        (self.day, self.slot)
                    )

                    await log_action(
                        f"❌ Released {self.day} {self.slot} by <@{user_id}>"
                    )

                else:
                    await interaction.response.defer()
                    return

            # BOOK SLOT
            else:

                start = get_slot_datetime(self.day, self.slot)

                end = start + timedelta(minutes=30)

                await db.execute("""
                INSERT INTO bookings
                (
                    day,
                    slot,
                    user_id,
                    start_ts,
                    end_ts,
                    reminded_60,
                    reminded_30,
                    reminded_start
                )
                VALUES (?, ?, ?, ?, ?, 0, 0, 0)
                """, (
                    self.day,
                    self.slot,
                    user_id,
                    int(start.timestamp()),
                    int(end.timestamp())
                ))

                await log_action(
                    f"📌 Booked {self.day} {self.slot} by <@{user_id}>"
                )

            await db.commit()

        await update_calendar()

        await interaction.response.defer()


# ---------------- VIEW ----------------

class SlotView(discord.ui.View):

    def __init__(self, bookings):

        super().__init__(timeout=None)

        for d in DAYS:
            for s in SLOTS:

                booked = (d, s) in bookings

                self.add_item(
                    SlotButton(d, s, booked)
                )


# ---------------- PERSISTENT CALENDAR ----------------

async def update_calendar():

    channel = bot.get_channel(CHANNEL_ID)

    if not channel:
        return

    msg_id = await get_setting("calendar_msg")

    text, bookings = await build_calendar()

    view = SlotView(bookings)

    if msg_id:

        try:

            msg = await channel.fetch_message(int(msg_id))

            await msg.edit(
                content=text,
                view=view
            )

            return

        except:
            pass

    msg = await channel.send(
        text,
        view=view
    )

    await set_setting(
        "calendar_msg",
        msg.id
    )


# ---------------- AUTO REFRESH ----------------

@tasks.loop(minutes=1)
async def refresh_loop():

    await update_calendar()


# ---------------- REMINDER LOOP ----------------

@tasks.loop(seconds=30)
async def reminder_loop():

    now_de, _ = now_times()

    async with aiosqlite.connect("slots.db") as db:

        async with db.execute(
            "SELECT * FROM bookings"
        ) as c:

            rows = await c.fetchall()

            for r in rows:

                (
                    day,
                    slot,
                    uid,
                    start_ts,
                    end_ts,
                    r60,
                    r30,
                    rstart
                ) = r

                start = datetime.fromtimestamp(start_ts)
                end = datetime.fromtimestamp(end_ts)

                ch = bot.get_channel(CHANNEL_ID)

                # 1 HOUR REMINDER
                if not r60 and start - timedelta(hours=1) <= now_de:

                    msg = await ch.send(
                        f"⏰ @everyone Vault Slot in 1 hour:\n"
                        f"📅 {day}\n"
                        f"🕒 {slot}\n"
                        f"👤 <@{uid}>"
                    )

                    await db.execute(
                        "UPDATE bookings SET reminded_60=1 WHERE day=? AND slot=?",
                        (day, slot)
                    )

                    await log_action(
                        f"⏰ 1h Reminder sent for {day} {slot}"
                    )

                    await msg.delete(delay=60)

                # 30 MIN REMINDER
                if not r30 and start - timedelta(minutes=30) <= now_de:

                    msg = await ch.send(
                        f"⏰ @everyone Vault Slot in 30 minutes:\n"
                        f"📅 {day}\n"
                        f"🕒 {slot}\n"
                        f"👤 <@{uid}>"
                    )

                    await db.execute(
                        "UPDATE bookings SET reminded_30=1 WHERE day=? AND slot=?",
                        (day, slot)
                    )

                    await log_action(
                        f"⏰ 30m Reminder sent for {day} {slot}"
                    )

                    await msg.delete(delay=60)

                # START
                if not rstart and start <= now_de:

                    msg = await ch.send(
                        f"🚀 @everyone Vault Slot STARTING NOW\n"
                        f"📅 {day}\n"
                        f"🕒 {slot}\n"
                        f"👤 <@{uid}>"
                    )

                    await db.execute(
                        "UPDATE bookings SET reminded_start=1 WHERE day=? AND slot=?",
                        (day, slot)
                    )

                    await log_action(
                        f"🚀 Start reminder sent for {day} {slot}"
                    )

                    await msg.delete(delay=120)

                # AUTO RELEASE
                if end <= now_de:

                    await db.execute(
                        "DELETE FROM bookings WHERE day=? AND slot=?",
                        (day, slot)
                    )

                    await log_action(
                        f"♻️ Auto release {day} {slot}"
                    )

                    await update_calendar()

        await db.commit()


# ---------------- EVENTS ----------------

@bot.event
async def on_ready():

    print(f"Bot online as {bot.user}")

    await init_db()

    await update_calendar()

    if not refresh_loop.is_running():
        refresh_loop.start()

    if not reminder_loop.is_running():
        reminder_loop.start()


# ---------------- START ----------------

keep_alive()

bot.run(TOKEN)
