import re
import os
import asyncpg
import threading
import logging
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

logging.basicConfig(level=logging.INFO)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set!")

db_pool = None

# ================= HEALTH CHECK =================

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

async def init_db(app):
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5
    )

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

        await conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON users(status);")

# ================= ERROR HANDLER =================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error(msg="Exception while handling update:", exc_info=context.error)

# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text("👋 Subscription Bot မှ ကြိုဆိုပါတယ်။")

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
    remaining = max((expire_date - datetime.utcnow()).days, 0)

    await update.message.reply_text(
        f"📅 Expire: {expire_date.date()}\n⏳ Remaining: {remaining} days"
    )

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

        try:
            if days_left == 2 and not user["reminder_2"]:
                await context.bot.send_message(user_id, "⚠ 2 days left.")
                async with db_pool.acquire() as conn:
                    await conn.execute("UPDATE users SET reminder_2=TRUE WHERE user_id=$1", user_id)

            elif days_left == 1 and not user["reminder_1"]:
                await context.bot.send_message(user_id, "⚠ 1 day left.")
                async with db_pool.acquire() as conn:
                    await conn.execute("UPDATE users SET reminder_1=TRUE WHERE user_id=$1", user_id)

            elif days_left < 0:
                await context.bot.ban_chat_member(CHANNEL_ID, user_id)
                await context.bot.unban_chat_member(CHANNEL_ID, user_id)

                async with db_pool.acquire() as conn:
                    await conn.execute("UPDATE users SET status='expired' WHERE user_id=$1", user_id)

                await context.bot.send_message(user_id, "⛔ Subscription Expired.")

        except Exception as e:
            logging.error(f"Expire error for {user_id}: {e}")

# ================= RUN =================

app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .post_init(init_db)
    .build()
)

app.add_error_handler(error_handler)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("mysub", mysub))
app.add_handler(CallbackQueryHandler(admin_buttons))
app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_payment))

app.job_queue.run_repeating(check_expire, interval=3600)

print("🔥 Production Bot Running...")
app.run_polling(drop_pending_updates=True)