import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from keep_alive import keep_alive
import os
from supabase import create_client, Client
import traceback

# ---------------- CONFIG ----------------

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

DAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday"
]

SLOTS = [
    "00:00-00:30",
    "08:00-08:30",
    "16:00-16:30"
]

# ---------------- BOT ----------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# ---------------- TIME ----------------

def now_times():
    """
    Aktuelle deutsche Zeit und Ingame-Zeit.
    Aktuell weiterhin mit UTC+2 wie in der bisherigen Version.
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
    target_weekday = DAYS.index(day_name)

    target_date = get_next_weekday_date(target_weekday)

    hour = int(slot.split(":")[0])
    minute = int(
        slot.split(":")[1].split("-")[0]
    )

    real_hour = (hour + 4) % 24

    return target_date.replace(
        hour=real_hour,
        minute=minute,
        second=0,
        microsecond=0
    )

# ---------------- LOG ----------------

async def log_action(text):
    try:
        ch = bot.get_channel(LOG_CHANNEL_ID)

        if ch:
            await ch.send(text)

    except Exception as e:
        print(
            f"❌ LOG CHANNEL ERROR: "
            f"{type(e).__name__}: {e}"
        )

# ---------------- MESSAGE RECOVERY ----------------

async def get_or_create_calendar_message(channel):

    try:
        res = (
            supabase
            .table("settings")
            .select("value")
            .eq("key", "calendar_msg")
            .execute()
            .data
        )

        msg_id = res[0]["value"] if res else None

        print(
            f"🔎 Stored calendar message ID: {msg_id}"
        )

        # ---------------- STORED MESSAGE ----------------

        if msg_id:

            try:

                msg = await channel.fetch_message(
                    int(msg_id)
                )

                print(
                    f"✅ Calendar message found: {msg.id}"
                )

                return msg

            except discord.NotFound:

                print(
                    "⚠️ Stored calendar message "
                    "no longer exists."
                )

            except discord.Forbidden:

                print(
                    "❌ No permission to fetch "
                    "calendar message."
                )

            except discord.HTTPException as e:

                print(
                    f"❌ Discord HTTP error while "
                    f"fetching calendar message: {e}"
                )

            except Exception as e:

                print(
                    f"❌ Unexpected error while "
                    f"fetching calendar message: "
                    f"{type(e).__name__}: {e}"
                )

        # ---------------- SEARCH HISTORY ----------------

        print(
            "🔎 Searching channel history "
            "for calendar message..."
        )

        async for msg in channel.history(limit=50):

            if msg.author == bot.user:

                print(
                    f"✅ Existing calendar message "
                    f"found in history: {msg.id}"
                )

                return msg

        print(
            "⚠️ No existing calendar message found."
        )

        return None

    except Exception as e:

        print(
            f"❌ MESSAGE RECOVERY ERROR: "
            f"{type(e).__name__}: {e}"
        )

        traceback.print_exc()

        return None

# ---------------- CALENDAR ----------------

async def build_calendar():

    print("🔄 Building calendar...")

    res = (
        supabase
        .table("bookings")
        .select("*")
        .execute()
    )

    rows = res.data

    print(
        f"📊 Loaded {len(rows)} booking(s) "
        f"from Supabase."
    )

    bookings = {
        (r["day"], r["slot"]): r["user_id"]
        for r in rows
    }

    now_de, now_ingame = now_times()

    text = "🎯 **Vault Slot Calendar**\n\n"

    text += (
        f"📅 Current Week: "
        f"`{now_de.strftime('%d.%m.%Y')}`\n"
    )

    text += (
        f"🇩🇪 German Time: "
        f"`{now_de.strftime('%H:%M')}`\n"
    )

    text += (
        f"🎮 Ingame Time: "
        f"`{now_ingame.strftime('%H:%M')}`\n\n"
    )

    text += (
        "ℹ️ Ingame Time = German Time -4 Hours\n\n"
    )

    for day in DAYS:

        text += f"## {day}\n"

        for slot in SLOTS:

            if (day, slot) in bookings:

                text += (
                    f"🔴 `{slot}` → "
                    f"Booked by "
                    f"<@{bookings[(day, slot)]}>\n"
                )

            else:

                text += (
                    f"🟢 `{slot}` → Free\n"
                )

        text += "\n"

    return text, bookings

# ---------------- BUTTONS ----------------

class SlotButton(discord.ui.Button):

    def __init__(
        self,
        day,
        slot,
        booked=False
    ):

        self.day = day
        self.slot = slot

        super().__init__(
            label=f"{day[:3]} {slot}",
            style=(
                discord.ButtonStyle.danger
                if booked
                else discord.ButtonStyle.success
            ),
            custom_id=f"{day}-{slot}"
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        # 🔒 Prevent double execution
        if interaction.response.is_done():
            return

        await interaction.response.defer()

        user_id = interaction.user.id

        try:

            existing = (
                supabase
                .table("bookings")
                .select("*")
                .eq("day", self.day)
                .eq("slot", self.slot)
                .execute()
                .data
            )

            # ---------------- RELEASE ----------------

            if existing:

                if existing[0]["user_id"] == user_id:

                    (
                        supabase
                        .table("bookings")
                        .delete()
                        .eq("day", self.day)
                        .eq("slot", self.slot)
                        .execute()
                    )

                    await log_action(
                        f"❌ Released "
                        f"{self.day} {self.slot} "
                        f"by <@{user_id}>"
                    )

                else:

                    print(
                        f"⚠️ User {user_id} attempted "
                        f"to release someone else's slot."
                    )

                    return

            # ---------------- BOOK ----------------

            else:

                start = get_slot_datetime(
                    self.day,
                    self.slot
                )

                end = (
                    start +
                    timedelta(minutes=30)
                )

                (
                    supabase
                    .table("bookings")
                    .insert({
                        "day": self.day,
                        "slot": self.slot,
                        "user_id": user_id,
                        "start_ts": int(
                            start.timestamp()
                        ),
                        "end_ts": int(
                            end.timestamp()
                        ),
                        "reminded_60": False,
                        "reminded_30": False,
                        "reminded_start": False
                    })
                    .execute()
                )

                await log_action(
                    f"📌 Booked "
                    f"{self.day} {self.slot} "
                    f"by <@{user_id}>"
                )

            await update_calendar()

        except Exception as e:

            print(
                f"❌ BUTTON CALLBACK ERROR: "
                f"{type(e).__name__}: {e}"
            )

            traceback.print_exc()

# ---------------- VIEW ----------------

class SlotView(discord.ui.View):

    def __init__(self, bookings):

        super().__init__(timeout=None)

        for d in DAYS:

            for s in SLOTS:

                self.add_item(
                    SlotButton(
                        d,
                        s,
                        (d, s) in bookings
                    )
                )

# ---------------- CALENDAR UPDATE ----------------

async def update_calendar():

    print(
        f"🔄 Calendar update started "
        f"at {datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )

    try:

        channel = bot.get_channel(CHANNEL_ID)

        if not channel:

            print(
                f"❌ Calendar channel "
                f"{CHANNEL_ID} not found."
            )

            return

        text, bookings = await build_calendar()

        view = SlotView(bookings)

        msg = await get_or_create_calendar_message(
            channel
        )

        # ---------------- EDIT EXISTING ----------------

        if msg:

            print(
                f"✏️ Updating calendar message "
                f"{msg.id}..."
            )

            try:

                await msg.edit(
                    content=text,
                    view=view
                )

                print(
                    f"✅ Calendar message "
                    f"{msg.id} updated successfully."
                )

            except discord.NotFound:

                print(
                    "⚠️ Calendar message no longer "
                    "exists. Creating a new one..."
                )

                msg = await channel.send(
                    content=text,
                    view=view
                )

                print(
                    f"✅ New calendar message created: "
                    f"{msg.id}"
                )

            except discord.Forbidden:

                print(
                    "❌ Discord denied permission "
                    "to edit calendar message."
                )

                return

            except discord.HTTPException as e:

                print(
                    f"❌ Discord HTTP error while "
                    f"editing calendar: {e}"
                )

                return

            except Exception as e:

                print(
                    f"❌ Unexpected error while "
                    f"editing calendar: "
                    f"{type(e).__name__}: {e}"
                )

                traceback.print_exc()

                return

        # ---------------- CREATE NEW ----------------

        else:

            print(
                "🆕 Creating new calendar message..."
            )

            msg = await channel.send(
                content=text,
                view=view
            )

            print(
                f"✅ New calendar message created: "
                f"{msg.id}"
            )

        # ---------------- SAVE MESSAGE ID ----------------

        try:

            (
                supabase
                .table("settings")
                .upsert({
                    "key": "calendar_msg",
                    "value": str(msg.id)
                })
                .execute()
            )

            print(
                f"💾 Calendar message ID saved: "
                f"{msg.id}"
            )

        except Exception as e:

            print(
                f"❌ Failed to save calendar "
                f"message ID: "
                f"{type(e).__name__}: {e}"
            )

            traceback.print_exc()

    except Exception as e:

        print(
            f"🚨 CALENDAR UPDATE FAILED: "
            f"{type(e).__name__}: {e}"
        )

        traceback.print_exc()

# ---------------- REFRESH LOOP ----------------

@tasks.loop(minutes=1)
async def refresh_loop():

    print(
        f"⏱️ Refresh loop tick at "
        f"{datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )

    try:

        await update_calendar()

    except Exception as e:

        print(
            f"❌ REFRESH LOOP ERROR: "
            f"{type(e).__name__}: {e}"
        )

        traceback.print_exc()


@refresh_loop.before_loop
async def before_refresh_loop():

    print(
        "⏳ Waiting for Discord bot to become ready..."
    )

    await bot.wait_until_ready()

    print(
        "✅ Refresh loop starting..."
    )


@refresh_loop.error
async def refresh_loop_error(error):

    print(
        f"🚨 REFRESH LOOP STOPPED: "
        f"{type(error).__name__}: {error}"
    )

    traceback.print_exception(
        type(error),
        error,
        error.__traceback__
    )

# ---------------- REMINDER LOOP ----------------

@tasks.loop(seconds=30)
async def reminder_loop():

    print(
        f"🔔 Reminder loop tick at "
        f"{datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )

    try:

        now_de, _ = now_times()

        rows = (
            supabase
            .table("bookings")
            .select("*")
            .execute()
            .data
        )

        ch = bot.get_channel(CHANNEL_ID)

        if not ch:

            print(
                f"❌ Reminder channel "
                f"{CHANNEL_ID} not found."
            )

            return

        for r in rows:

            start = datetime.fromtimestamp(
                r["start_ts"]
            )

            end = datetime.fromtimestamp(
                r["end_ts"]
            )

            day = r["day"]
            slot = r["slot"]
            uid = r["user_id"]

            # ---------------- 1 HOUR REMINDER ----------------

            if (
                not r["reminded_60"]
                and start - timedelta(hours=1)
                <= now_de
            ):

                reminder_text = (
                    f"⏰ @everyone Vault Slot in 1 hour\n"
                    f"📅 {day}\n"
                    f"🕒 {slot}\n"
                    f"👤 <@{uid}>"
                )

                msg = await ch.send(
                    reminder_text
                )

                bot.loop.create_task(
                    msg.delete(delay=60)
                )

                await log_action(
                    f"⏰ 1h Reminder sent for "
                    f"{day} {slot} "
                    f"(<@{uid}>)"
                )

                (
                    supabase
                    .table("bookings")
                    .update({
                        "reminded_60": True
                    })
                    .eq("day", day)
                    .eq("slot", slot)
                    .execute()
                )

            # ---------------- 30 MIN REMINDER ----------------

            if (
                not r["reminded_30"]
                and start - timedelta(minutes=30)
                <= now_de
            ):

                reminder_text = (
                    f"⏰ @everyone "
                    f"Vault Slot in 30 minutes\n"
                    f"📅 {day}\n"
                    f"🕒 {slot}\n"
                    f"👤 <@{uid}>"
                )

                msg = await ch.send(
                    reminder_text
                )

                bot.loop.create_task(
                    msg.delete(delay=60)
                )

                await log_action(
                    f"⏰ 30m Reminder sent for "
                    f"{day} {slot} "
                    f"(<@{uid}>)"
                )

                (
                    supabase
                    .table("bookings")
                    .update({
                        "reminded_30": True
                    })
                    .eq("day", day)
                    .eq("slot", slot)
                    .execute()
                )

            # ---------------- START REMINDER ----------------

            if (
                not r["reminded_start"]
                and start <= now_de
            ):

                reminder_text = (
                    f"🚀 @everyone "
                    f"Vault Slot STARTING NOW\n"
                    f"📅 {day}\n"
                    f"🕒 {slot}\n"
                    f"👤 <@{uid}>"
                )

                msg = await ch.send(
                    reminder_text
                )

                bot.loop.create_task(
                    msg.delete(delay=60)
                )

                await log_action(
                    f"🚀 Start Reminder sent for "
                    f"{day} {slot} "
                    f"(<@{uid}>)"
                )

                (
                    supabase
                    .table("bookings")
                    .update({
                        "reminded_start": True
                    })
                    .eq("day", day)
                    .eq("slot", slot)
                    .execute()
                )

            # ---------------- AUTO RELEASE ----------------

            if end <= now_de:

                (
                    supabase
                    .table("bookings")
                    .delete()
                    .eq("day", day)
                    .eq("slot", slot)
                    .execute()
                )

                await log_action(
                    f"♻️ Auto release "
                    f"{day} {slot}"
                )

    except Exception as e:

        print(
            f"❌ REMINDER LOOP ERROR: "
            f"{type(e).__name__}: {e}"
        )

        traceback.print_exc()


@reminder_loop.before_loop
async def before_reminder_loop():

    print(
        "⏳ Waiting for Discord bot before "
        "starting reminder loop..."
    )

    await bot.wait_until_ready()

    print(
        "✅ Reminder loop starting..."
    )


@reminder_loop.error
async def reminder_loop_error(error):

    print(
        f"🚨 REMINDER LOOP STOPPED: "
        f"{type(error).__name__}: {error}"
    )

    traceback.print_exception(
        type(error),
        error,
        error.__traceback__
    )

# ---------------- READY ----------------

@bot.event
async def on_ready():

    print(
        f"🟢 Bot online as {bot.user} "
        f"(ID: {bot.user.id})"
    )

    print(
        f"📡 Connected to "
        f"{len(bot.guilds)} server(s)"
    )

    # Calendar immediately update
    try:

        await update_calendar()

    except Exception as e:

        print(
            f"❌ Initial calendar update failed: "
            f"{type(e).__name__}: {e}"
        )

        traceback.print_exc()

    # Start refresh loop
    if not refresh_loop.is_running():

        print(
            "▶️ Starting refresh loop..."
        )

        refresh_loop.start()

    else:

        print(
            "ℹ️ Refresh loop already running."
        )

    # Start reminder loop
    if not reminder_loop.is_running():

        print(
            "▶️ Starting reminder loop..."
        )

        reminder_loop.start()

    else:

        print(
            "ℹ️ Reminder loop already running."
        )

# ---------------- START ----------------

print("🚀 Starting bot...")

keep_alive()

bot.run(TOKEN)
