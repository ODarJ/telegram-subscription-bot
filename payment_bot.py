import re
import os
import asyncpg
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from config import BOT_TOKEN, ADMIN_GROUP_ID, CHANNEL_ID

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set!")
db_pool = None

# ================= PORT BIND FOR RENDER =================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()

# ================= DATABASE INIT =================

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            name TEXT,
            username TEXT,
            transaction_id TEXT UNIQUE,
            status TEXT,
            start_date TIMESTAMP,
            expire_date TIMESTAMP,
            reminder_1 BOOLEAN DEFAULT FALSE,
            reminder_2 BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP
        );
        """)

        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_status
        ON users(status);
        """)

# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "👋 ကြိုဆိုပါတယ်!\n\n"
        "💰 Channel ဝင်ကြေး — ၁၀၀၀ ကျပ် (30 days)\n"
        "📲 Kpay / Wave\n"
        "09971249026 (wyh)\n\n"
        "💳 ငွေလွဲပြီးပါက\n"
        "Wave — (9 လုံး)\n"
        "Kpay — (20 လုံး)\n"
        "လုပ်ငန်းစဉ်အမှတ်ကို ပို့ပါ။"
    )

# ================= MY SUB =================

async def mysub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    async with db_pool.acquire() as conn:
        result = await conn.fetchrow(
            "SELECT expire_date FROM users WHERE user_id=$1 AND status='active'",
            update.effective_user.id
        )

    if not result:
        await update.message.reply_text("❌ Active subscription မရှိပါ။")
        return

    expire_date = result["expire_date"]
    remaining = (expire_date - datetime.utcnow()).days

    await update.message.reply_text(
        f"📅 Expire Date: {expire_date.date()}\n"
        f"⏳ Remaining: {remaining} days"
    )

# ================= SMART PAYMENT =================

async def handle_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_chat.type != "private":
        return

    text = re.sub(r"\s+", "", update.message.text)

    # Smart validation
    if not re.fullmatch(r"\d{9}|\d{20}", text):
        await update.message.reply_text(
            "❌ Invalid Transaction ID.\n\n"
            "Wave — 9 လုံး\n"
            "Kpay — 20 လုံး\n\n"
            "မှန်ကန်သော ID ပို့ပါ။"
        )
        return

    user = update.effective_user

    async with db_pool.acquire() as conn:

        # Check duplicate transaction
        exists = await conn.fetchrow(
            "SELECT transaction_id FROM users WHERE transaction_id=$1",
            text
        )

        if exists:
            await update.message.reply_text("❌ ဒီ Transaction ID ကို အသုံးပြုပြီးသား ဖြစ်ပါတယ်။")
            return

        # Check if user already active
        active = await conn.fetchrow(
            "SELECT status FROM users WHERE user_id=$1 AND status='active'",
            user.id
        )

        if active:
            await update.message.reply_text(
                "ℹ️ သင့်မှာ Active subscription ရှိနေပါတယ်။\n"
                "Renew လုပ်လိုပါက Transaction ID ပို့နိုင်ပါတယ်။"
            )

        await conn.execute("""
        INSERT INTO users (user_id, name, username, transaction_id, status, created_at)
        VALUES ($1,$2,$3,$4,'pending',$5)
        ON CONFLICT (user_id)
        DO UPDATE SET
            transaction_id=EXCLUDED.transaction_id,
            status='pending',
            created_at=EXCLUDED.created_at
        """,
        user.id,
        user.full_name,
        user.username,
        text,
        datetime.utcnow()
        )

    await update.message.reply_text("✅ ငွေလက်ခံပြီးပါပြီ။ Admin စစ်ဆေးနေပါသည်။")

    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user.id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user.id}")
    ]]

    await context.bot.send_message(
        chat_id=ADMIN_GROUP_ID,
        text=f"💳 New Payment\n👤 {user.full_name}\n🆔 {user.id}\nID: {text}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================= ADMIN =================

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    action, user_id = query.data.split("_")
    user_id = int(user_id)

    if action == "approve":

        now = datetime.utcnow()

        async with db_pool.acquire() as conn:

            existing = await conn.fetchrow(
                "SELECT expire_date FROM users WHERE user_id=$1",
                user_id
            )

            if existing and existing["expire_date"]:
                old_expire = existing["expire_date"]
                new_expire = old_expire + timedelta(days=30) if old_expire > now else now + timedelta(days=30)
            else:
                new_expire = now + timedelta(days=30)

            await conn.execute("""
            UPDATE users
            SET status='active',
                start_date=$1,
                expire_date=$2,
                reminder_1=FALSE,
                reminder_2=FALSE
            WHERE user_id=$3
            """, now, new_expire, user_id)

        try:
            member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
            already = member.status in ["member", "administrator", "creator"]
        except:
            already = False

        if already:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ Renewed!\nExpire: {new_expire.date()}"
            )
        else:
            invite = await context.bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                member_limit=1
            )
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🎉 Approved!\nExpire: {new_expire.date()}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔓 Join Channel", url=invite.invite_link)]]
                )
            )

        await query.edit_message_text("✅ Approved ✔")

# ================= EXPIRE CHECK =================

async def check_expire(context: ContextTypes.DEFAULT_TYPE):

    async with db_pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT user_id, expire_date, reminder_1, reminder_2 FROM users WHERE status='active'"
        )

    now = datetime.utcnow()

    for user in users:
        user_id = user["user_id"]
        expire = user["expire_date"]
        days_left = (expire - now).days

        if days_left == 2 and not user["reminder_2"]:
            await context.bot.send_message(user_id, "⚠ 2 days left.")
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET reminder_2=TRUE WHERE user_id=$1", user_id)

        elif days_left == 1 and not user["reminder_1"]:
            await context.bot.send_message(user_id, "⚠ 1 day left.")
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET reminder_1=TRUE WHERE user_id=$1", user_id)

        elif now > expire:
            try:
                await context.bot.ban_chat_member(CHANNEL_ID, user_id)
                await context.bot.unban_chat_member(CHANNEL_ID, user_id)
            except:
                pass

            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET status='expired' WHERE user_id=$1", user_id)

            await context.bot.send_message(user_id, "⛔ Subscription Expired.")

# ================= RUN =================

import asyncio

init_loop = asyncio.new_event_loop()
asyncio.set_event_loop(init_loop)
init_loop.run_until_complete(init_db())

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("mysub", mysub))
app.add_handler(CallbackQueryHandler(admin_buttons))
app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_payment))

app.job_queue.run_repeating(check_expire, interval=3600)

print("🔥 PostgreSQL Production Bot Running...")
app.run_polling(drop_pending_updates=True)