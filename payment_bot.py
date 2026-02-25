import re
import os
import sqlite3
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

DB_FILE = "database.db"

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

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        username TEXT,
        transaction_id TEXT UNIQUE,
        status TEXT,
        start_date TEXT,
        expire_date TEXT,
        reminder_1 INTEGER DEFAULT 0,
        reminder_2 INTEGER DEFAULT 0,
        created_at TEXT
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON users(status)")
    conn.commit()
    conn.close()

def db_execute(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(query, params)
    result = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return result

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

    user_id = update.effective_user.id
    result = db_execute(
        "SELECT expire_date FROM users WHERE user_id=? AND status='active'",
        (user_id,),
        fetch=True
    )

    if not result:
        await update.message.reply_text("❌ Active subscription မရှိပါ။")
        return

    expire_date = datetime.fromisoformat(result[0][0])
    remaining = (expire_date - datetime.now()).days

    await update.message.reply_text(
        f"📅 Expire Date: {expire_date.date()}\n"
        f"⏳ Remaining: {remaining} days"
    )

# ================= PAYMENT =================

async def handle_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_chat.type != "private":
        return

    text = re.sub(r"\s+", "", update.message.text)

    if not re.fullmatch(r"\d{9}|\d{20}", text):
        return

    user = update.effective_user

    try:
        db_execute("""
        INSERT INTO users (user_id, name, username, transaction_id, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """, (
            user.id,
            user.full_name,
            user.username,
            text,
            datetime.now().isoformat()
        ))
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ ဒီ Transaction ID ကို အသုံးပြုပြီးသား ဖြစ်ပါတယ်။")
        return

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

        now = datetime.now()
        existing = db_execute(
            "SELECT expire_date FROM users WHERE user_id=?",
            (user_id,),
            fetch=True
        )

        if existing and existing[0][0]:
            old_expire = datetime.fromisoformat(existing[0][0])
            new_expire = old_expire + timedelta(days=30) if old_expire > now else now + timedelta(days=30)
        else:
            new_expire = now + timedelta(days=30)

        db_execute("""
        UPDATE users
        SET status='active',
            start_date=?,
            expire_date=?,
            reminder_1=0,
            reminder_2=0
        WHERE user_id=?
        """, (now.isoformat(), new_expire.isoformat(), user_id))

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

    users = db_execute(
        "SELECT user_id, expire_date, reminder_1, reminder_2 FROM users WHERE status='active'",
        fetch=True
    )

    now = datetime.now()

    for user_id, expire_str, r1, r2 in users:
        expire = datetime.fromisoformat(expire_str)
        days_left = (expire - now).days

        if days_left == 2 and not r2:
            await context.bot.send_message(user_id, "⚠ 2 days left.")
            db_execute("UPDATE users SET reminder_2=1 WHERE user_id=?", (user_id,))

        elif days_left == 1 and not r1:
            await context.bot.send_message(user_id, "⚠ 1 day left.")
            db_execute("UPDATE users SET reminder_1=1 WHERE user_id=?", (user_id,))

        elif now > expire:
            try:
                await context.bot.ban_chat_member(CHANNEL_ID, user_id)
                await context.bot.unban_chat_member(CHANNEL_ID, user_id)
            except:
                pass

            db_execute("UPDATE users SET status='expired' WHERE user_id=?", (user_id,))
            await context.bot.send_message(user_id, "⛔ Subscription Expired.")

# ================= RUN =================

init_db()

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("mysub", mysub))
app.add_handler(CallbackQueryHandler(admin_buttons))
app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_payment))

app.job_queue.run_repeating(check_expire, interval=3600)

print("🔥 SQLite Subscription Bot Running (Render Ready)...")
app.run_polling()