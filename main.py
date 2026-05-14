import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from keep_alive import keep_alive
import os
from supabase import create_client, Client

# ---------------- CONFIG ----------------

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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

# ---------------- TIME ----------------

def now_times():
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
    target_weekday = DAYS.index(day_name)
    target_date = get_next_weekday_date(target_weekday)

    hour = int(slot.split(":")[0])
    minute = int(slot.split(":")[1].split("-")[0])

    real_hour = (hour + 4) % 24

    return target_date.replace(
        hour=real_hour,
        minute=minute,
        second=0,
        microsecond=0
    )

# ---------------- LOG ----------------

async def log_action(text):
    ch = bot.get_channel(LOG_CHANNEL_ID)
    if ch:
        await ch.send(text)

# ---------------- MESSAGE RECOVERY ----------------

async def get_or_create_calendar_message(channel):

    res = supabase.table("settings") \
        .select("value") \
        .eq("key", "calendar_msg") \
        .execute().data

    msg_id = res[0]["value"] if res else None

    if msg_id:
        try:
            return await channel.fetch_message(int(msg_id))
        except:
            pass

    async for msg in channel.history(limit=50):
        if msg.author == bot.user:
            return msg

    return None

# ---------------- CALENDAR ----------------

async def build_calendar():

    res = supabase.table("bookings").select("*").execute()
    rows = res.data

    bookings = {(r["day"], r["slot"]): r["user_id"] for r in rows}

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
                text += f"🔴 `{slot}` → Booked by <@{bookings[(day, slot)]}>\n"
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

    # ---------------- FIXED CALLBACK (NO DOUBLE LOG) ----------------
    async def callback(self, interaction: discord.Interaction):

        # 🔒 FIX: sofort ACK verhindert doppelte Ausführung
        if interaction.response.is_done():
            return

        await interaction.response.defer()

        user_id = interaction.user.id

        existing = supabase.table("bookings") \
            .select("*") \
            .eq("day", self.day) \
            .eq("slot", self.slot) \
            .execute().data

        # RELEASE
        if existing:

            if existing[0]["user_id"] == user_id:

                supabase.table("bookings") \
                    .delete() \
                    .eq("day", self.day) \
                    .eq("slot", self.slot) \
                    .execute()

                await log_action(f"❌ Released {self.day} {self.slot} by <@{user_id}>")

            else:
                return

        # BOOK
        else:

            start = get_slot_datetime(self.day, self.slot)
            end = start + timedelta(minutes=30)

            supabase.table("bookings").insert({
                "day": self.day,
                "slot": self.slot,
                "user_id": user_id,
                "start_ts": int(start.timestamp()),
                "end_ts": int(end.timestamp()),
                "reminded_60": False,
                "reminded_30": False,
                "reminded_start": False
            }).execute()

            await log_action(f"📌 Booked {self.day} {self.slot} by <@{user_id}>")

        await update_calendar()

# ---------------- VIEW ----------------

class SlotView(discord.ui.View):

    def __init__(self, bookings):
        super().__init__(timeout=None)

        for d in DAYS:
            for s in SLOTS:
                self.add_item(SlotButton(d, s, (d, s) in bookings))

# ---------------- CALENDAR UPDATE ----------------

async def update_calendar():

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    text, bookings = await build_calendar()
    view = SlotView(bookings)

    msg = await get_or_create_calendar_message(channel)

    if msg:
        try:
            await msg.edit(content=text, view=view)
        except:
            msg = await channel.send(content=text, view=view)

            supabase.table("settings").upsert({
                "key": "calendar_msg",
                "value": str(msg.id)
            }).execute()
            return

        supabase.table("settings").upsert({
            "key": "calendar_msg",
            "value": str(msg.id)
        }).execute()

    else:
        msg = await channel.send(content=text, view=view)

        supabase.table("settings").upsert({
            "key": "calendar_msg",
            "value": str(msg.id)
        }).execute()

# ---------------- LOOPS ----------------

@tasks.loop(minutes=1)
async def refresh_loop():
    await update_calendar()

@tasks.loop(seconds=30)
async def reminder_loop():

    now_de, _ = now_times()

    rows = supabase.table("bookings").select("*").execute().data

    for r in rows:

        start = datetime.fromtimestamp(r["start_ts"])
        end = datetime.fromtimestamp(r["end_ts"])

        day = r["day"]
        slot = r["slot"]
        uid = r["user_id"]

        ch = bot.get_channel(CHANNEL_ID)

        # ---------------- 1 HOUR REMINDER ----------------

        if not r["reminded_60"] and start - timedelta(hours=1) <= now_de:

            reminder_text = (
                f"⏰ @everyone Vault Slot in 1 hour\n"
                f"📅 {day}\n"
                f"🕒 {slot}\n"
                f"👤 <@{uid}>"
            )

            msg = await ch.send(reminder_text)

            # AUTO DELETE AFTER 120s
            await msg.delete(delay=120)

            # LOG CHANNEL
            await log_action(
                f"⏰ 1h Reminder sent for {day} {slot} (<@{uid}>)"
            )

            supabase.table("bookings") \
                .update({"reminded_60": True}) \
                .eq("day", day) \
                .eq("slot", slot) \
                .execute()

        # ---------------- 30 MIN REMINDER ----------------

        if not r["reminded_30"] and start - timedelta(minutes=30) <= now_de:

            reminder_text = (
                f"⏰ @everyone Vault Slot in 30 minutes\n"
                f"📅 {day}\n"
                f"🕒 {slot}\n"
                f"👤 <@{uid}>"
            )

            msg = await ch.send(reminder_text)

            # AUTO DELETE AFTER 120s
            await msg.delete(delay=120)

            # LOG CHANNEL
            await log_action(
                f"⏰ 30m Reminder sent for {day} {slot} (<@{uid}>)"
            )

            supabase.table("bookings") \
                .update({"reminded_30": True}) \
                .eq("day", day) \
                .eq("slot", slot) \
                .execute()

        # ---------------- START REMINDER ----------------

        if not r["reminded_start"] and start <= now_de:

            reminder_text = (
                f"🚀 @everyone Vault Slot STARTING NOW\n"
                f"📅 {day}\n"
                f"🕒 {slot}\n"
                f"👤 <@{uid}>"
            )

            msg = await ch.send(reminder_text)

            # AUTO DELETE AFTER 120s
            await msg.delete(delay=120)

            # LOG CHANNEL
            await log_action(
                f"🚀 Start Reminder sent for {day} {slot} (<@{uid}>)"
            )

            supabase.table("bookings") \
                .update({"reminded_start": True}) \
                .eq("day", day) \
                .eq("slot", slot) \
                .execute()

        # ---------------- AUTO RELEASE ----------------

        if end <= now_de:

            supabase.table("bookings") \
                .delete() \
                .eq("day", day) \
                .eq("slot", slot) \
                .execute()

            await log_action(
                f"♻️ Auto release {day} {slot}"
            )

# ---------------- READY ----------------

@bot.event
async def on_ready():

    print(f"Bot online as {bot.user}")

    await update_calendar()

    if not refresh_loop.is_running():
        refresh_loop.start()

    if not reminder_loop.is_running():
        reminder_loop.start()

# ---------------- START ----------------

keep_alive()
bot.run(TOKEN)
