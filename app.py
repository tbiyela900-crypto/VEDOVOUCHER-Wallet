"""
Vedo Ai Investment  -  advanced interface edition
=================================================================

Features
  * Accounts, login/logout, members start at Level 0 and earn 0 until top up
  * Sign-up uses an 11-digit BA_A Number + a CELLPHONE NUMBER that must be
    verified with an SMS one-time code (OTP) before the first login.
    The verified cellphone number IS the login ID.
  * TOP-UP page looks like the check-in page: the member taps a level tile,
    enters an A_m and presses Enter. The tiles close and a WHITE details
    tab opens for 10 minutes showing the BA_n, Reference, A_m,
    P_s and BA_A Number, a small Top-up flag and a red message. ALL of
    that information is entered by the admin only (Admin page > Top-up
    details, for everyone or for one member). The member can copy every line.
  * Admin approval of a top-up only marks it approved. It does NOT add any
    money to the balance.
  * The balance only grows through the daily check-in tasks (and referral
    bonuses, savings-pot interest, etc.), or when the admin adds balance to
    a member by hand (Admin page > All members > Add balance).
  * Daily check-in (level reward / 24) with a special 7-day streak congratulation
  * Referral codes and team
  * Simulated WITHDRAWALS: user requests a cash-out of money,
                          admin approves/rejects (rejected = refunded).
  * TEAM VIEW           : each team member's level and points (balance)
  * LEVEL UPGRADES      : spend balance to move up one level, costs are public
  * SAVINGS POTS        : lock part of the balance for 1-9 weeks and earn a
                          clearly labelled, realistic annual interest rate
                          (simple interest, NOT a multiplier)
  * PROFILE             : user (and admin) can see the details entered when
                          the account was created
  * Admin dashboard     : monitors users, balances, top-ups, withdrawals,
                          pots, a full transaction ledger, can ADD BALANCE to
                          a member and RESET a member's password

Data safety
  * The database and the uploaded photos are stored on persistent storage:
      DATA_DIR env var, else /var/data if it exists, else the app folder.
    (You can still set DB_PATH / UPLOAD_DIR yourself.)
  * SECRET_KEY: set it as an environment variable. If you do not, a random
    key is generated once and saved next to the database, so sessions and
    SMS codes keep working after a restart.
  * A backup of the database is made every time the app starts (the last 10
    are kept in the "backups" folder next to the database).
  * The app never deletes member data by itself. Only sign-ups that never
    verified their cellphone (older than 1 hour) are cleaned up.

Verification code
  No SMS provider is used. The 6-digit code is shown inside the app on the
  verify page (numbers only).

Run:
  pip install flask
  python app.py
  http://127.0.0.1:5000
"""

import hashlib
import json
import hmac
import os
import re
import secrets
import shutil
import sqlite3
import string
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template_string,
    request, send_from_directory, session, url_for,
)
from jinja2 import DictLoader
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash


# ============================================================================
# CONFIGURATION
# ============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _pick_data_dir():
    """Persistent folder for the database, uploads and backups."""
    env = os.environ.get("DATA_DIR", "").strip()
    if env:
        return os.path.abspath(env)
    if os.path.isdir("/var/data") and os.access("/var/data", os.W_OK):
        return "/var/data"
    return BASE_DIR


DATA_DIR = _pick_data_dir()
DB_PATH = os.path.abspath(os.environ.get("DB_PATH")
                          or os.path.join(DATA_DIR, "rewards.db"))
UPLOAD_DIR = os.path.abspath(os.environ.get(
    "UPLOAD_DIR", os.path.join(os.path.dirname(DB_PATH), "uploads")))
BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH), "backups")
BACKUPS_KEEP = 10

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# If an older database / photos sit in the app folder and the persistent
# folder is still empty, copy them over once (nothing is deleted).
_legacy_db = os.path.join(BASE_DIR, "rewards.db")
if (not os.path.exists(DB_PATH) and os.path.exists(_legacy_db)
        and os.path.abspath(_legacy_db) != DB_PATH):
    shutil.copy2(_legacy_db, DB_PATH)
_legacy_up = os.path.join(BASE_DIR, "uploads")
if (not os.path.exists(UPLOAD_DIR) and os.path.isdir(_legacy_up)
        and os.path.abspath(_legacy_up) != UPLOAD_DIR):
    shutil.copytree(_legacy_up, UPLOAD_DIR)


def _load_secret_key():
    """SECRET_KEY env var, else a random key saved once next to the database."""
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env
    path = os.path.join(os.path.dirname(DB_PATH), ".secret_key")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                saved = fh.read().strip()
            if saved:
                return saved
        key = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key
    except OSError:
        return "dev-only-change-me"


def backup_database():
    """Create a consistent SQLite backup and keep only the newest ones."""
    os.makedirs(BACKUP_DIR, exist_ok=True)

    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"rewards_{stamp}.db")

    source = sqlite3.connect(DB_PATH, timeout=30)
    destination = sqlite3.connect(backup_path)

    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    old = sorted(f for f in os.listdir(BACKUP_DIR)
                 if f.startswith("rewards_") and f.endswith(".db"))
    for name in old[:-BACKUPS_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, name))
        except OSError:
            pass

    return backup_path


LEVELS = list(range(1, 8))

# Total reward per level (daily check-in = total / 24)
LEVEL_REWARDS_CENTS = {
    0: 0, 1: 36000, 2: 120000, 3: 360000, 4: 1200000,
    5: 2000000, 6: 3000000, 7: 4000000,
}

DAILY_REWARD_CENTS = {
    lvl: int((Decimal(LEVEL_REWARDS_CENTS[lvl]) / 24).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))
    for lvl in LEVELS
}

STREAK_DAYS = 7
REFERRAL_BONUS_PER_LEVEL_CENTS = 20000    # R2000 x new member's level

# --- Team tiers ------------------------------------------------------------
# At each member count the team gets a name and a team account number that
# the member can view on the My Team page.
TEAM_TIERS = [
    (5, "Bronze Team"),
    (10, "Silver Team"),
    (15, "Gold Team"),
    (20, "Platinum Team"),
]

DEFAULT_WELCOME_TEXT = "Welcome, let's grow together."

# --- SMS one-time code (OTP) -----------------------------------------------
OTP_TTL_SECONDS = 600           # a code is valid for 10 minutes
OTP_RESEND_SECONDS = 60         # wait between two codes
OTP_MAX_ATTEMPTS = 5            # wrong guesses per code
OTP_MAX_SENDS = 5               # codes per 30 minutes
OTP_WINDOW_SECONDS = 1800

# --- Level upgrades --------------------------------------------------------
# Cost to move from level N-1 to level N = 50% of the difference between the
# two levels' total rewards. One level at a time, paid from balance.
UPGRADE_COST_CENTS = {
    lvl: (LEVEL_REWARDS_CENTS[lvl] - LEVEL_REWARDS_CENTS[lvl - 1]) // 2
    for lvl in LEVELS
}

# --- Top-ups / withdrawals -------------------------------------------------
TOPUP_MIN_CENTS = 1000          # R10
TOPUP_MAX_CENTS = 5000000       # R50,000
TOPUP_PANEL_SECONDS = 600       # the Top-up details tab stays open 10 minutes
WITHDRAW_MIN_CENTS = 1000       # R10
ADMIN_ADD_MAX_CENTS = 100000000  # R1,000,000 per admin balance addition

DEFAULT_TOPUP_INSTRUCTIONS = (
    "Enter the A_m and the reason for the top-up, then tap the level you "
    "want. A tab with the top up details will open for 10 minutes. An admin "
    "will review your request and approve or reject it."
)

# --- Savings pots ----------------------------------------------------------
# Annual interest rate (simple interest) by lock period. Realistic, not a
# multiplier. Interest = principal x APR x weeks / 52.
POT_APR_PERCENT = {
    1: Decimal("4.0"), 2: Decimal("4.5"), 3: Decimal("5.0"),
    4: Decimal("5.5"), 5: Decimal("6.0"), 6: Decimal("6.5"),
    7: Decimal("7.0"), 8: Decimal("7.5"), 9: Decimal("8.0"),
}
POT_WEEKS = sorted(POT_APR_PERCENT)
POT_MIN_CENTS = 1000            # R10

# --- Check-in tasks / admin photos ----------------------------------------
TASK_WAIT_SECONDS = 5           # each tap loads for 5 seconds

app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024    # photo uploads (30 MB)


# ============================================================================
# ICONS (inline SVG, used in the templates as {{ icon('name') }})
# ============================================================================

ICONS = {
    "home": '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/>',
    "withdraw": '<path d="M7 17L17 7"/><path d="M8 7h9v9"/>',
    "topup": '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    "upgrade": '<path d="M6 13l6-6 6 6"/><path d="M6 19l6-6 6 6"/>',
    "team": ('<circle cx="9" cy="8" r="3.5"/>'
             '<path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6"/>'
             '<path d="M16 4.7a3.5 3.5 0 010 6.6"/>'
             '<path d="M18 14.3c2.2.7 3.5 2.6 3.5 5.7"/>'),
    "pots": '<rect x="4" y="10" width="16" height="10" rx="3"/><path d="M8 10V7a4 4 0 018 0v3"/>',
    "profile": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7"/>',
    "admin": '<path d="M12 3l8 3v6c0 5-3.4 8-8 9-4.6-1-8-4-8-9V6z"/>',
    "fingerprint": ('<path d="M3.5 10A9 9 0 0120.5 10"/>'
                    '<path d="M6 13a6 6 0 0112 0v1.5"/>'
                    '<path d="M9 20c.6-1.5 1-3 1-5v-3a2 2 0 014 0v3c0 2.5.5 4 1.5 5.5"/>'),
}


def icon(name):
    body = ICONS.get(name, "")
    return Markup(
        '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
        'aria-hidden="true">' + body + "</svg>")


# ============================================================================
# MONEY HELPERS
# ============================================================================

def money(cents):
    A_m = Decimal(int(cents or 0)) / Decimal(100)
    return f"R{A_m:,.2f}"


def daily_reward_cents(level):
    return DAILY_REWARD_CENTS.get(int(level), 0)


def level_reward(level):
    return money(LEVEL_REWARDS_CENTS.get(int(level), 0))


def daily_reward(level):
    return money(daily_reward_cents(level))


def tasks_for_level(level):
    """Check-in taps per day: 3 at Level 1, plus 2 more for every level up."""
    level = int(level)
    return 3 + 2 * (level - 1) if level >= 1 else 0


def task_reward_cents(level, task_no):
    """Reward of one tap. The daily reward is split evenly over the taps;
    the last tap also takes any leftover cents so the total is exact."""
    total = daily_reward_cents(level)
    n = tasks_for_level(level)
    if n == 0:
        return 0
    base = total // n
    return base + (total - base * n if task_no == n else 0)


def pot_interest_cents(principal_cents, weeks):
    apr = POT_APR_PERCENT[int(weeks)]
    value = Decimal(int(principal_cents)) * apr / 100 * int(weeks) / 52
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_A_m(text, minimum, maximum=None):
    """Parse a Rand A_m string into cents. Returns None if invalid."""
    try:
        A_m = Decimal((text or "").strip()).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None
    cents = int(A_m * 100)
    if cents < minimum:
        return None
    if maximum is not None and cents > maximum:
        return None
    return cents


# ============================================================================
# DATABASE
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    level INTEGER DEFAULT 0,
    selected_level INTEGER NOT NULL DEFAULT 0,
    referral_code TEXT UNIQUE,
    referred_by INTEGER,
    serial_number TEXT NOT NULL UNIQUE,

    last_checkin TEXT,
    streak INTEGER DEFAULT 0,

    is_admin INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vouchers (
    code TEXT PRIMARY KEY,
    level INTEGER NOT NULL,
    used_by INTEGER REFERENCES users(id),
    used_at TEXT,
    issued_to INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    day TEXT NOT NULL,
    reward INTEGER NOT NULL,
    bonus INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, day)
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    A_m INTEGER NOT NULL,
    BA_n TEXT NOT NULL DEFAULT 'DEMO',
    account_number TEXT NOT NULL DEFAULT 'N/A',
    account_holder TEXT NOT NULL DEFAULT 'N/A',
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    A_m INTEGER NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS topup_details (
    user_id INTEGER PRIMARY KEY,          -- 0 = everyone, otherwise a member
    BA_n TEXT NOT NULL DEFAULT '',
    Reference TEXT NOT NULL DEFAULT '',
    A_m TEXT NOT NULL DEFAULT '',
    P_s TEXT NOT NULL DEFAULT '',
    BA_Account_number TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    flag TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS savings_pots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    principal INTEGER NOT NULL,
    weeks INTEGER NOT NULL,
    apr_percent TEXT NOT NULL,
    interest INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    matures_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'LOCKED',
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    A_m INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkin_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    day TEXT NOT NULL,
    task_no INTEGER NOT NULL,
    started_ts REAL,
    completed_at TEXT,
    reward INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, day, task_no)
);

CREATE TABLE IF NOT EXISTS otps (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    code_hash TEXT NOT NULL,
    code_plain TEXT NOT NULL DEFAULT '',
    expires_ts REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_ts REAL NOT NULL,
    sends INTEGER NOT NULL DEFAULT 1,
    window_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS team_tiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    members INTEGER NOT NULL,
    name TEXT NOT NULL,
    account_number TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, members)
);

CREATE TABLE IF NOT EXISTS admin_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image TEXT NOT NULL,
    caption TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_instructions (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vedo_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
    starts_at TEXT NOT NULL DEFAULT '', ends_at TEXT NOT NULL DEFAULT '',
    entry_cost INTEGER NOT NULL DEFAULT 0, correct_reward INTEGER NOT NULL DEFAULT 50,
    incorrect_deduction INTEGER NOT NULL DEFAULT 10, referral_target INTEGER NOT NULL DEFAULT 10,
    referral_entry_points INTEGER NOT NULL DEFAULT 0, referral_from_level INTEGER NOT NULL DEFAULT 1,
    referral_to_level INTEGER NOT NULL DEFAULT 2, voucher_points_min INTEGER NOT NULL DEFAULT 50,
    voucher_points_max INTEGER NOT NULL DEFAULT 200, spins_per_day INTEGER NOT NULL DEFAULT 1,
    prizes_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vedo_event_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES vedo_events(id),
    user_id INTEGER NOT NULL REFERENCES users(id), joined_at TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ACTIVE',
    metadata TEXT NOT NULL DEFAULT '{}', UNIQUE(event_id,user_id)
);
CREATE TABLE IF NOT EXISTS vedo_event_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES vedo_events(id),
    user_id INTEGER NOT NULL REFERENCES users(id), result_type TEXT NOT NULL,
    points_delta INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vedo_quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES vedo_events(id),
    user_id INTEGER NOT NULL REFERENCES users(id), question_key TEXT NOT NULL,
    answer_key TEXT NOT NULL, correct INTEGER NOT NULL, points_delta INTEGER NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(event_id,user_id,question_key)
);
CREATE TABLE IF NOT EXISTS vedo_spin_daily (
    user_id INTEGER NOT NULL REFERENCES users(id), event_id INTEGER NOT NULL REFERENCES vedo_events(id),
    spin_day TEXT NOT NULL, spins INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,event_id,spin_day)
);
CREATE TABLE IF NOT EXISTS vedo_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA synchronous = NORMAL")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ============================================================================
# GENERAL HELPERS
# ============================================================================

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def today():
    return datetime.now(timezone.utc).date()


def random_block(n):
    alphabet = (string.ascii_uppercase.replace("O", "").replace("I", "")
                + "23456789")
    return "".join(secrets.choice(alphabet) for _ in range(n))


def new_voucher_code(level):
    return f"S{level}-{random_block(4)}-{random_block(4)}"


def new_referral_code(db):
    while True:
        code = "REF-" + random_block(6)
        if not db.execute("SELECT 1 FROM users WHERE referral_code=?",
                          (code,)).fetchone():
            return code


def new_team_account_number(db):
    while True:
        number = "TEAM-" + "".join(secrets.choice(string.digits) for _ in range(8))
        if not db.execute("SELECT 1 FROM team_tiers WHERE account_number=?",
                          (number,)).fetchone():
            return number


def normalize_phone(raw):
    """Return the cellphone number as +E.164, or None if it is not valid."""
    s = re.sub(r"[\s\-().]", "", (raw or "").strip())
    if s.startswith("00"):
        s = "+" + s[2:]
    if re.fullmatch(r"0[6-8]\d{8}", s):            # South African cell, local
        return "+27" + s[1:]
    if re.fullmatch(r"\+?27[6-8]\d{8}", s):        # South African cell, intl
        return "+" + s.lstrip("+")
    if s.startswith("+27"):                        # SA number, not a cellphone
        return None
    if re.fullmatch(r"\+[1-9]\d{7,14}", s):        # other countries
        return s
    return None


def normalize_sa_phone(raw):
    """Sign-up only: accept a South African cellphone number, return +27...
    Accepted: 082 123 4567, 0821234567, +27 82 123 4567, 27821234567,
    0027821234567. Anything else (other countries, landlines) -> None."""
    phone = normalize_phone(raw)
    if phone and re.fullmatch(r"\+27[6-8]\d{8}", phone):
        return phone
    return None


def mask_phone(phone):
    phone = phone or ""
    return phone[:3] + "*" * max(0, len(phone) - 7) + phone[-4:]


def otp_hash(code):
    """Keyed hash of a one-time code (the plain code is never stored)."""
    return hmac.new(app.secret_key.encode(), str(code).encode(),
                    hashlib.sha256).hexdigest()


def issue_otp(db, user):
    """Create a 6-digit code, shown inside the app. Returns (ok, error)."""
    now = time.time()
    row = db.execute("SELECT * FROM otps WHERE user_id=?",
                     (user["id"],)).fetchone()
    sends, window = 0, now
    if row:
        wait = OTP_RESEND_SECONDS - (now - row["sent_ts"])
        if wait > 0:
            return False, (f"Please wait {int(wait) + 1} seconds before "
                           "asking for a new code.")
        if now - row["window_ts"] <= OTP_WINDOW_SECONDS:
            sends, window = row["sends"], row["window_ts"]
        if sends >= OTP_MAX_SENDS:
            return False, "Too many codes requested. Please try again later."

    code = f"{secrets.randbelow(10 ** 6):06d}"
    db.execute(
        "INSERT INTO otps (user_id, code_hash, code_plain, expires_ts, "
        "attempts, sent_ts, sends, window_ts) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash, "
        "code_plain=excluded.code_plain, expires_ts=excluded.expires_ts, "
        "attempts=0, sent_ts=excluded.sent_ts, sends=excluded.sends, "
        "window_ts=excluded.window_ts",
        (user["id"], otp_hash(code), code, now + OTP_TTL_SECONDS, 0, now,
         sends + 1, window))
    db.commit()
    return True, None


def generate_vouchers(db, level, count):
    codes = []
    for _ in range(count):
        code = new_voucher_code(level)
        db.execute(
            "INSERT INTO vouchers (code, level, created_at) VALUES (?,?,?)",
            (code, level, now_iso()))
        codes.append(code)
    db.commit()
    return codes


def get_setting(db, key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?",
                     (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def add_tx(db, user_id, kind, A_m_cents, note=""):
    """Write a ledger line (positive = credit, negative = debit)."""
    db.execute(
        "INSERT INTO transactions (user_id, kind, A_m, note, created_at) "
        "VALUES (?,?,?,?,?)",
        (user_id, kind, int(A_m_cents), note, now_iso()))


def credit(db, user_id, cents, kind, note=""):
    db.execute("UPDATE users SET points = points + ? WHERE id=?",
               (cents, user_id))
    add_tx(db, user_id, kind, cents, note)


def debit(db, user_id, cents, kind, note=""):
    """Atomic debit. Returns False (and changes nothing) if funds are short."""
    done = db.execute(
        "UPDATE users SET points = points - ? WHERE id=? AND points >= ?",
        (cents, user_id, cents)).rowcount
    if done != 1:
        return False
    add_tx(db, user_id, kind, -cents, note)
    return True


def pay_referral_bonus(db, user, new_level):
    """Pay the referrer once, when a member first leaves Level 0."""
    if user["level"] == 0 and user["referred_by"] and new_level > 0:
        credit(db, user["referred_by"],
               new_level * REFERRAL_BONUS_PER_LEVEL_CENTS, "REFERRAL",
               f"Referral: {user['username']} reached Level {new_level}")


def page(body, **ctx):
    return render_template_string(
        "{% extends 'base' %}{% block body %}" + body + "{% endblock %}",
        **ctx)


# ============================================================================
# DATABASE INITIALISATION
# ============================================================================


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)

    # Safely add columns missing from older databases (never deletes data).
    existing_columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(users)").fetchall()
    }

    required_columns = {
        "last_checkin": "TEXT",
        "streak": "INTEGER NOT NULL DEFAULT 0",
        "points": "INTEGER NOT NULL DEFAULT 0",
        "serial_number": "TEXT",
        "phone": "TEXT",
        "phone_verified": "INTEGER NOT NULL DEFAULT 1",
        "selected_level": "INTEGER NOT NULL DEFAULT 0",
    }

    for column, definition in required_columns.items():
        if column not in existing_columns:
            db.execute(
                f"ALTER TABLE users ADD COLUMN {column} {definition}"
            )

    # Same safe upgrade for the other tables.
    extra_columns = {
        # Ensure legacy databases have the money columns expected by the
        # admin templates. Some earlier deployments used a different schema.
        "withdrawals": {"A_m": "INTEGER NOT NULL DEFAULT 0"},
        "topups": {
            "A_m": "INTEGER NOT NULL DEFAULT 0",
            "level": "INTEGER NOT NULL DEFAULT 0",
        },
        "transactions": {"A_m": "INTEGER NOT NULL DEFAULT 0"},
        "vouchers": {"issued_to": "INTEGER"},
        "otps": {"code_plain": "TEXT NOT NULL DEFAULT ''"},
        # Older deployed databases may already have this table without the
        # newer fields. Add missing columns safely before the admin POST uses them.
        "topup_details": {
            "BA_n": "TEXT NOT NULL DEFAULT ''",
            "Reference": "TEXT NOT NULL DEFAULT ''",
            "A_m": "TEXT NOT NULL DEFAULT ''",
            "P_s": "TEXT NOT NULL DEFAULT ''",
            "BA_Account_number": "TEXT NOT NULL DEFAULT ''",
            "message": "TEXT NOT NULL DEFAULT ''",
            "flag": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in extra_columns.items():
        have = {r["name"]
                for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in columns.items():
            if column not in have:
                # If a legacy table used `amount`, preserve it when adding A_m.
                legacy_amount = next(
                    (name for name in ("amount", "amount_cents") if name in have),
                    None,
                ) if column == "A_m" else None
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                if legacy_amount:
                    db.execute(
                        f"UPDATE {table} SET A_m = COALESCE({legacy_amount}, 0)")
                have.add(column)

    db.commit()

    admin_user = os.environ.get("ADMIN_USER", "admin")
    admin_pass = os.environ.get("ADMIN_PASS", "admin12345")

    # Create admin account
    if not db.execute(
        "SELECT 1 FROM users WHERE is_admin=1"
    ).fetchone():

        # Admin does not use a real BA_A Number
        admin_serial_number = "00000000000"

        db.execute(
            """
            INSERT INTO users (
                username,
                pw_hash,
                level,
                referral_code,
                serial_number,
                is_admin,
                created_at
            )
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                admin_user,
                generate_password_hash(admin_pass),
                7,
                new_referral_code(db),
                admin_serial_number,
                1,
                now_iso()
            )
        )

        print(f"[setup] Admin account created: {admin_user}")
        print(f"[setup] Default admin password: {admin_pass} (change it!)")

    # Create starter vouchers
    if not db.execute(
        "SELECT 1 FROM vouchers"
    ).fetchone():

        print("[setup] Starter voucher codes:")

        for level in LEVELS:
            code = new_voucher_code(level)

            db.execute(
                """
                INSERT INTO vouchers (
                    code,
                    level,
                    created_at
                )
                VALUES (?,?,?)
                """,
                (
                    code,
                    level,
                    now_iso()
                )
            )

            print(f"  Level {level}: {code}")

    # Top-up instructions
    if not db.execute(
        "SELECT 1 FROM settings WHERE key='topup_instructions'"
    ).fetchone():

        set_setting(
            db,
            "topup_instructions",
            DEFAULT_TOPUP_INSTRUCTIONS
        )

    db.commit()
    db.close()


init_db()

# Safety copy of the database every time the app starts.
try:
    backup_database()
except Exception as exc:                       # never stop the app for this
    print(f"[backup] skipped: {exc}")

# ============================================================================
# AUTHENTICATION / CSRF
# ============================================================================

def current_user():
    uid = session.get("uid")
    if uid is None:
        return None
    return get_db().execute("SELECT * FROM users WHERE id=?",
                            (uid,)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Please log in first.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user()["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def check_csrf():
    if request.method != "POST":
        return None
    sent = request.form.get("csrf_token", "")
    good = session.get("csrf", "")
    if sent and good and hmac.compare_digest(sent.encode(), good.encode()):
        return None

    # Expired page / lost session cookie: recover nicely on the sign-in pages.
    ep = request.endpoint
    if ep == "verify_resend" and request.headers.get(
            "X-Requested-With") == "fetch":
        return jsonify(ok=False, redirect=url_for("login"),
                       message="Your page expired. Please log in again.")
    if ep in ("login", "register", "verify", "verify_resend"):
        flash("Your page expired. Please try again.", "error")
        return redirect(url_for("register" if ep == "register" else "login"))
    abort(400, "Invalid or missing CSRF token. Go back and reload.")


@app.context_processor
def inject_globals():
    return {
        "csrf_token": csrf_token,
        "me": current_user(),
        "LEVELS": LEVELS,
        "money": money,
        "level_reward": level_reward,
        "daily_reward": daily_reward,
        "UPGRADE_COST_CENTS": UPGRADE_COST_CENTS,
        "POT_APR_PERCENT": POT_APR_PERCENT,
        "POT_WEEKS": POT_WEEKS,
        "pot_interest": pot_interest_cents,
        "icon": icon,
        "mask_phone": mask_phone,
        "welcome_text": get_setting(get_db(), "welcome_text",
                                    DEFAULT_WELCOME_TEXT),
    }


# ============================================================================
# REGISTER / LOGIN / LOGOUT
# ============================================================================

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None

    if request.method == "POST":
        # The verified CELLPHONE NUMBER is the login ID. It is stored in the
        # `username` column (and in `phone`) so the rest of the app keeps
        # working unchanged.
        serial_number = request.form.get("serial_number", "").strip()
        phone = normalize_sa_phone(request.form.get("phone", ""))
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        ref_code = request.form.get("referral", "").strip().upper()
        level_code = request.form.get("level_code", "").strip().upper()

        db = get_db()

        # Free up numbers held by sign-ups that never verified (over 1 hour old)
        db.execute("DELETE FROM otps WHERE user_id IN (SELECT id FROM users "
                   "WHERE phone_verified=0 AND is_admin=0 "
                   "AND created_at < datetime('now','-1 hour'))")
        db.execute("DELETE FROM users WHERE phone_verified=0 AND is_admin=0 "
                   "AND created_at < datetime('now','-1 hour')")
        db.commit()

        referrer = None
        voucher = None
        start_level = 0

        # Validate cellphone number (an SMS code is sent to it)
        if phone is None:
            error = ("Enter a valid South African cellphone number, "
                     "for example 082 123 4567 or +27 82 123 4567.")

        # Validate BA_A Number (exactly 11 digits)
        elif (not serial_number.isascii()
                or not serial_number.isdigit()
                or len(serial_number) != 11):
            error = "BA_A Number must contain exactly 11 digits."

        # Validate password
        elif len(password) < 8:
            error = "Password must be at least 8 characters."

        elif password != confirm:
            error = "Passwords do not match."

        # Check referral code
        if not error and ref_code:
            referrer = db.execute(
                "SELECT * FROM users WHERE referral_code=?",
                (ref_code,)
            ).fetchone()

            if referrer is None:
                error = "That referral code does not exist."

        # Check level code from the admin (optional)
        if not error and level_code:
            voucher = db.execute(
                "SELECT * FROM vouchers WHERE code=?",
                (level_code,)
            ).fetchone()

            if (voucher is None
                    or voucher["issued_to"] is not None
                    or voucher["used_by"] is not None):
                error = "That level code is not valid or was already used."
            else:
                start_level = voucher["level"]

        # Check duplicate cellphone number
        if not error:
            existing_user = db.execute(
                "SELECT phone_verified FROM users "
                "WHERE username=? OR phone=?",
                (phone, phone)
            ).fetchone()

            if existing_user:
                if existing_user["phone_verified"]:
                    error = ("That cellphone number is already registered. "
                             "Please log in.")
                else:
                    error = ("That cellphone number is already waiting to be "
                             "verified. Log in with it and your password to "
                             "get a new SMS code.")

        # Check duplicate BA_A Number
        if not error:
            existing_serial = db.execute(
                "SELECT 1 FROM users WHERE serial_number=?",
                (serial_number,)
            ).fetchone()

            if existing_serial:
                error = "That BA_A Number is already registered."

        # Display validation errors
        if error:
            flash(error, "error")
            return page(REGISTER_BODY, form=request.form), 400

        try:
            # Create account (starts at the level of the admin's level code)
            cur = db.execute(
                """
                INSERT INTO users (
                    username,
                    pw_hash,
                    level,
                    referral_code,
                    referred_by,
                    serial_number,
                    phone,
                    phone_verified,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    phone,
                    generate_password_hash(password),
                    start_level,
                    new_referral_code(db),
                    referrer["id"] if referrer else None,
                    serial_number,
                    phone,
                    0,
                    now_iso()
                )
            )

            uid = cur.lastrowid

            # Use up the level code (one use only)
            if voucher is not None:
                claimed = db.execute(
                    "UPDATE vouchers SET used_by=?, used_at=? "
                    "WHERE code=? AND used_by IS NULL AND issued_to IS NULL",
                    (uid, now_iso(), voucher["code"])).rowcount
                if claimed != 1:
                    db.rollback()
                    flash("That level code was just used by someone else.",
                          "error")
                    return page(REGISTER_BODY, form=request.form), 400

                # Same referral bonus rule as redeeming a code at Level 0
                if referrer:
                    pay_referral_bonus(
                        db,
                        {"level": 0, "referred_by": referrer["id"],
                         "username": phone},
                        start_level)

            db.commit()

        except sqlite3.IntegrityError:
            db.rollback()
            flash(
                "That cellphone number or BA_A Number is already registered.",
                "error"
            )
            return redirect(url_for("register"))

        except Exception:
            db.rollback()
            app.logger.exception("Registration failed")
            flash("Registration failed due to a server error. Please try again.", "error")
            return redirect(url_for("register"))

        # -----------------------------
        # SEND THE SMS CODE (the member is NOT logged in until it is entered)
        # -----------------------------
        session.clear()
        session["pending_uid"] = uid
        _send_code_and_flash(db, uid)
        return redirect(url_for("verify"))

    # GET request: show the registration page
    # (a referral link /register?ref=CODE fills in the referral code)
    return page(
        REGISTER_BODY,
        form={"referral": request.args.get("ref", "").strip().upper()})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        raw = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # Members log in with their cellphone number (any usual format).
        # Anything that is not a phone number (e.g. the admin username) is
        # looked up as typed.
        candidate = normalize_phone(raw) or raw

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username=? OR username=?",
            (candidate, raw)).fetchone()
        if user is None:
            user = db.execute("SELECT * FROM users WHERE phone=?",
                              (candidate,)).fetchone()

        if user and check_password_hash(user["pw_hash"], password):
            if not user["phone_verified"]:
                # Cellphone not verified yet: the SMS code comes first.
                session.clear()
                session["pending_uid"] = user["id"]
                row = db.execute("SELECT expires_ts FROM otps "
                                 "WHERE user_id=?", (user["id"],)).fetchone()
                if row is None or time.time() > row["expires_ts"]:
                    _send_code_and_flash(db, user["id"])
                return redirect(url_for("verify"))
            session["uid"] = user["id"]
            return redirect(url_for("dashboard"))

        flash("Wrong cellphone number or password.", "error")
        return page(LOGIN_BODY), 401

    return page(LOGIN_BODY)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def _send_code_and_flash(db, uid):
    user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    ok, error = issue_otp(db, user)
    if not ok:
        flash(error, "error")


@app.route("/verify", methods=["GET", "POST"])
def verify():
    """The new member must enter the SMS code before being logged in."""
    uid = session.get("pending_uid")
    if not uid:
        return redirect(url_for("login"))

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if user is None or user["phone_verified"]:
        session.pop("pending_uid", None)
        return redirect(url_for("login"))

    if request.method == "POST":
        code = re.sub(r"\D", "", request.form.get("code", ""))
        row = db.execute("SELECT * FROM otps WHERE user_id=?",
                         (uid,)).fetchone()

        if row is None or time.time() > row["expires_ts"]:
            flash("That code has expired. Tap Resend code to get a new one.",
                  "error")
        elif row["attempts"] >= OTP_MAX_ATTEMPTS:
            flash("Too many wrong attempts. Tap Resend code to get a new one.",
                  "error")
        elif len(code) == 6 and hmac.compare_digest(otp_hash(code),
                                                    row["code_hash"]):
            db.execute("UPDATE users SET phone_verified=1 WHERE id=?", (uid,))
            db.execute("DELETE FROM otps WHERE user_id=?", (uid,))
            db.commit()
            session.clear()
            session["uid"] = uid
            if user["level"]:
                flash(f"Welcome! You start at Level {user['level']}.", "ok")
            else:
                flash("Welcome! You start at Level 0. "
                      "Top up to start earning.", "ok")
            return redirect(url_for("dashboard"))
        else:
            db.execute("UPDATE otps SET attempts=attempts+1 WHERE user_id=?",
                       (uid,))
            db.commit()
            left = OTP_MAX_ATTEMPTS - row["attempts"] - 1
            flash("Wrong code. " + (f"{left} attempt(s) left." if left > 0
                                    else "Tap Resend code to get a new one."),
                  "error")
        return redirect(url_for("verify"))

    # Code shown in the app + seconds left before a new one may be requested
    otp_row = db.execute("SELECT sent_ts, code_plain, expires_ts FROM otps "
                         "WHERE user_id=?", (uid,)).fetchone()
    resend_in, code_shown = 0, ""
    if otp_row:
        wait = OTP_RESEND_SECONDS - (time.time() - otp_row["sent_ts"])
        resend_in = int(wait) + 1 if wait > 0 else 0
        if time.time() <= otp_row["expires_ts"]:
            code_shown = otp_row["code_plain"]

    return page(VERIFY_BODY, masked=mask_phone(user["phone"]),
                resend_in=resend_in, code_shown=code_shown)


@app.route("/verify/resend", methods=["POST"])
def verify_resend():
    ajax = request.headers.get("X-Requested-With") == "fetch"
    uid = session.get("pending_uid")
    if not uid:
        if ajax:
            return jsonify(ok=False, message="Session expired.",
                           redirect=url_for("login"))
        return redirect(url_for("login"))

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if user is None or user["phone_verified"]:
        session.pop("pending_uid", None)
        if ajax:
            return jsonify(ok=False, message="Already verified.",
                           redirect=url_for("login"))
        return redirect(url_for("login"))

    ok, error = issue_otp(db, user)
    if ajax:
        if not ok:
            m = re.search(r"(\d+) seconds", error or "")
            return jsonify(ok=False, message=error,
                           wait=int(m.group(1)) if m else 0)
        row = db.execute("SELECT code_plain FROM otps WHERE user_id=?",
                         (uid,)).fetchone()
        return jsonify(ok=True, message="New code ready.",
                       code=row["code_plain"], wait=OTP_RESEND_SECONDS)

    if not ok:
        flash(error, "error")
    return redirect(url_for("verify"))


@app.route("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# ============================================================================
# DASHBOARD + CHECK-IN
# ============================================================================

def checkin_state(user):
    last = user["last_checkin"]
    t = today()
    if last == t.isoformat():
        return True, user["streak"]
    if last == (t - timedelta(days=1)).isoformat():
        return False, user["streak"] + 1
    return False, 1


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    user = current_user()
    done, next_streak = checkin_state(user)

    history = db.execute(
        "SELECT * FROM checkins WHERE user_id=? ORDER BY day DESC LIMIT 10",
        (user["id"],)).fetchall()
    recent_tx = db.execute(
        "SELECT * FROM transactions WHERE user_id=? "
        "ORDER BY id DESC LIMIT 10", (user["id"],)).fetchall()
    locked = db.execute(
        "SELECT COALESCE(SUM(principal),0) AS s FROM savings_pots "
        "WHERE user_id=? AND status='LOCKED'", (user["id"],)).fetchone()["s"]

    reward = daily_reward_cents(user["level"])
    milestone = bool(done and user["streak"] > 0
                     and user["streak"] % STREAK_DAYS == 0)

    task_total = tasks_for_level(user["level"])
    task_done = db.execute(
        "SELECT COUNT(*) AS n FROM checkin_tasks WHERE user_id=? AND day=? "
        "AND completed_at IS NOT NULL",
        (user["id"], today().isoformat())).fetchone()["n"]
    posts = db.execute(
        "SELECT * FROM admin_posts ORDER BY id DESC LIMIT 20").fetchall()

    return page(DASH_BODY, task_total=task_total, task_done=task_done,
                posts=posts, user=user, done=done, next_streak=next_streak,
                history=history, recent_tx=recent_tx, locked=locked,
                reward=reward, milestone=milestone, STREAK_DAYS=STREAK_DAYS)


@app.route("/checkin")
@login_required
def checkin():
    """Check-in page: reward by level; tapping your own level opens the tasks."""
    db = get_db()
    user = current_user()

    done, _streak = checkin_state(user)
    day = today().isoformat()
    total = tasks_for_level(user["level"])

    rows = db.execute(
        "SELECT task_no, reward FROM checkin_tasks "
        "WHERE user_id=? AND day=? AND completed_at IS NOT NULL",
        (user["id"], day)).fetchall()
    paid = {r["task_no"]: r["reward"] for r in rows}

    tasks = [
        {"no": i,
         "reward": task_reward_cents(user["level"], i),
         "done": i in paid}
        for i in range(1, total + 1)
    ]

    return page(CHECKIN_BODY, user=user, done=done, total=total,
                tasks=tasks, completed_n=len(paid),
                earned=sum(paid.values()),
                daily=daily_reward_cents(user["level"]),
                wait=TASK_WAIT_SECONDS)


def _task_guard(user, n):
    """Return an error message if this task cannot be used, else None."""
    if user["level"] == 0:
        return "Top up and move to Level 1 to start earning."
    done, _streak = checkin_state(user)
    if done:
        return "You already checked in today. Come back tomorrow!"
    if not 1 <= n <= tasks_for_level(user["level"]):
        return "That task does not exist."
    return None


@app.route("/checkin/<int:n>/start", methods=["POST"])
@login_required
def checkin_start(n):
    db = get_db()
    user = current_user()

    error = _task_guard(user, n)
    if error:
        return jsonify(ok=False, error=error), 400

    day = today().isoformat()
    now = time.time()

    # Only one task may be loading at a time.
    loading = db.execute(
        "SELECT 1 FROM checkin_tasks WHERE user_id=? AND day=? "
        "AND task_no<>? AND completed_at IS NULL AND started_ts>?",
        (user["id"], day, n, now - TASK_WAIT_SECONDS)).fetchone()
    if loading:
        return jsonify(ok=False,
                       error="Wait for the loading task to finish first."), 400

    existing = db.execute(
        "SELECT completed_at FROM checkin_tasks "
        "WHERE user_id=? AND day=? AND task_no=?",
        (user["id"], day, n)).fetchone()
    if existing and existing["completed_at"]:
        return jsonify(ok=False, error="You already did this task."), 400

    db.execute(
        "INSERT INTO checkin_tasks (user_id, day, task_no, started_ts) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, day, task_no) DO UPDATE "
        "SET started_ts=excluded.started_ts "
        "WHERE checkin_tasks.completed_at IS NULL",
        (user["id"], day, n, now))
    db.commit()
    return jsonify(ok=True, wait=TASK_WAIT_SECONDS)


@app.route("/checkin/<int:n>/complete", methods=["POST"])
@login_required
def checkin_complete(n):
    db = get_db()
    user = current_user()

    error = _task_guard(user, n)
    if error:
        return jsonify(ok=False, error=error), 400

    day = today().isoformat()
    total = tasks_for_level(user["level"])
    reward = task_reward_cents(user["level"], n)

    row = db.execute(
        "SELECT * FROM checkin_tasks WHERE user_id=? AND day=? AND task_no=?",
        (user["id"], day, n)).fetchone()
    if row is None or row["started_ts"] is None:
        return jsonify(ok=False, error="Tap the task first."), 400
    if row["completed_at"] is not None:
        return jsonify(ok=False, error="You already did this task."), 400
    if time.time() - row["started_ts"] < TASK_WAIT_SECONDS:
        return jsonify(ok=False,
                       error="The task is still loading. Please wait."), 400

    claimed = db.execute(
        "UPDATE checkin_tasks SET completed_at=?, reward=? "
        "WHERE user_id=? AND day=? AND task_no=? AND completed_at IS NULL",
        (now_iso(), reward, user["id"], day, n)).rowcount
    if claimed != 1:
        db.rollback()
        return jsonify(ok=False, error="You already did this task."), 400

    # Each finished task adds its reward to the balance straight away.
    credit(db, user["id"], reward, "CHECKIN",
           f"Check-in task {n}/{total} ({day})")

    summary = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(reward),0) AS s "
        "FROM checkin_tasks WHERE user_id=? AND day=? "
        "AND completed_at IS NOT NULL", (user["id"], day)).fetchone()
    completed, earned = summary["n"], summary["s"]

    finished = False
    milestone = False
    streak = 0
    if completed >= total:
        # All tasks done: the day counts as checked in (streak, history).
        _done, streak = checkin_state(user)
        inserted = db.execute(
            "INSERT OR IGNORE INTO checkins (user_id, day, reward, bonus) "
            "VALUES (?,?,?,0)", (user["id"], day, earned)).rowcount
        if inserted == 1:
            finished = True
            db.execute("UPDATE users SET streak=?, last_checkin=? WHERE id=?",
                       (streak, day, user["id"]))
            # Every 7th day in a row earns a special congratulation (no money).
            milestone = streak % STREAK_DAYS == 0
    db.commit()

    points = db.execute("SELECT points FROM users WHERE id=?",
                        (user["id"],)).fetchone()["points"]
    return jsonify(ok=True, reward_text=money(reward),
                   balance=money(points), completed=completed, total=total,
                   earned_text=money(earned), done=finished,
                   milestone=milestone, streak=streak)


# ---------------------------------------------------------------------------
# Admin photo posts (shown on the check-in page)
# ---------------------------------------------------------------------------

def _image_ext(data):
    """Detect the real image type from the file bytes (never trust names)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _save_image(data, ext):
    """Write an uploaded image to UPLOAD_DIR and return its file name."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = f"{secrets.token_hex(12)}.{ext}"
    with open(os.path.join(UPLOAD_DIR, name), "wb") as fh:
        fh.write(data)
    return name


def _remove_upload(name):
    if not name:
        return
    try:
        os.remove(os.path.join(UPLOAD_DIR, name))
    except OSError:
        pass


@app.route("/admin/post", methods=["POST"])
@admin_required
def admin_post():
    caption = request.form.get("caption", "").strip()[:300]
    photo = request.files.get("photo")

    if photo is None or not photo.filename:
        flash("Choose a photo to post.", "error")
        return redirect(url_for("dashboard"))

    data = photo.read()
    ext = _image_ext(data)
    if not data or ext is None:
        flash("The photo must be a PNG, JPG, GIF or WEBP image.", "error")
        return redirect(url_for("dashboard"))

    name = _save_image(data, ext)

    db = get_db()
    db.execute(
        "INSERT INTO admin_posts (image, caption, created_at) VALUES (?,?,?)",
        (name, caption, now_iso()))
    db.commit()
    flash("Photo posted.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/admin/post/<int:post_id>/delete", methods=["POST"])
@admin_required
def admin_post_delete(post_id):
    db = get_db()
    post = db.execute("SELECT * FROM admin_posts WHERE id=?",
                      (post_id,)).fetchone()
    if post is None:
        flash("Post not found.", "error")
        return redirect(url_for("dashboard"))

    db.execute("DELETE FROM admin_posts WHERE id=?", (post_id,))
    db.commit()
    _remove_upload(post["image"])
    flash("Post deleted.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/media/<path:filename>")
@login_required
def media(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ============================================================================
# TOP-UPS (simulated deposits)
# ============================================================================

def _parse_ts(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc)


def get_topup_details(db, user_id):
    """Top up details written by the admin. A member-specific entry replaces
    the global one (user_id 0) for that member."""
    row = db.execute("SELECT * FROM topup_details WHERE user_id=?",
                     (user_id,)).fetchone()
    if row is None:
        row = db.execute("SELECT * FROM topup_details WHERE user_id=0"
                         ).fetchone()
    return row


@app.route("/topup/select-level", methods=["POST"])
@login_required
def topup_select_level():
    """Create a pending top-up request after the member selects a level and A_m."""
    db = get_db()
    user = current_user()
    try:
        level = int(request.form.get("level", "0"))
    except (TypeError, ValueError):
        return jsonify(ok=False, message="Choose a valid level."), 400

    cents = parse_A_m(request.form.get("A_m"),
                         TOPUP_MIN_CENTS, TOPUP_MAX_CENTS)
    if cents is None:
        return jsonify(
            ok=False,
            message=(f"Enter an A_m between {money(TOPUP_MIN_CENTS)} "
                     f"and {money(TOPUP_MAX_CENTS)}.")
        ), 400
    if level not in LEVELS or level <= user["level"]:
        return jsonify(ok=False, message="Tap a level above your current level."), 400

    pending = db.execute(
        "SELECT COUNT(*) AS n FROM topups WHERE user_id=? AND status='PENDING'",
        (user["id"],)).fetchone()["n"]
    if pending >= 3:
        return jsonify(ok=False, message="You already have 3 pending top-up requests."), 400

    db.execute("UPDATE users SET selected_level=? WHERE id=?",
               (level, user["id"]))
    db.execute(
        "INSERT INTO topups (user_id, A_m, level, note, status, created_at) "
        "VALUES (?,?,?, '', 'PENDING', ?)",
        (user["id"], cents, level, now_iso()))
    db.commit()
    return jsonify(ok=True, level=level, A_m=money(cents),
                   message=f"Request for Level {level} sent.")


@app.route("/topup", methods=["GET", "POST"])
@login_required
def topup():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        cents = parse_A_m(request.form.get("A_m"),
                             TOPUP_MIN_CENTS, TOPUP_MAX_CENTS)
        note = request.form.get("note", "").strip()[:100]
        try:
            level = int(request.form.get("level", 0))
        except ValueError:
            level = 0

        if cents is None:
            flash(f"Enter an A_m between {money(TOPUP_MIN_CENTS)} and "
                  f"{money(TOPUP_MAX_CENTS)}.", "error")
            return redirect(url_for("topup"))

        if level not in LEVELS or level <= user["level"]:
            flash("Tap a level above your current level.", "error")
            return redirect(url_for("topup"))

        pending = db.execute(
            "SELECT COUNT(*) AS n FROM topups WHERE user_id=? "
            "AND status='PENDING'", (user["id"],)).fetchone()["n"]
        if pending >= 3:
            flash("You already have 3 pending top-up requests.", "error")
            return redirect(url_for("topup"))

        db.execute(
            "INSERT INTO topups (user_id, A_m, level, note, status, "
            "created_at) VALUES (?,?,?,?, 'PENDING', ?)",
            (user["id"], cents, level, note, now_iso()))
        db.commit()
        flash(f"Top-up request for {money(cents)} (Level {level}) sent.", "ok")
        return redirect(url_for("topup"))

    history = db.execute(
        "SELECT * FROM topups WHERE user_id=? ORDER BY id DESC",
        (user["id"],)).fetchall()
    # A member-specific instruction replaces the global one for that member.
    own = db.execute("SELECT text FROM user_instructions WHERE user_id=?",
                     (user["id"],)).fetchone()
    instructions = own["text"] if own else get_setting(
        db, "topup_instructions", DEFAULT_TOPUP_INSTRUCTIONS)

    # The Top-up details tab stays open for 10 minutes after a request.
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=TOPUP_PANEL_SECONDS)
              ).strftime("%Y-%m-%d %H:%M:%S")
    active = db.execute(
        "SELECT * FROM topups WHERE user_id=? AND status='PENDING' "
        "AND created_at>=? ORDER BY id DESC LIMIT 1",
        (user["id"], cutoff)).fetchone()

    left, details, info_rows, req_rows = 0, None, [], []
    if active:
        age = (datetime.now(timezone.utc)
               - _parse_ts(active["created_at"])).total_seconds()
        left = int(TOPUP_PANEL_SECONDS - age)
        if left <= 0:
            active = None
    if active:
        details = get_topup_details(db, user["id"])

        def val(key):
            return (details[key] if details and details[key] else "-")

        info_rows = [
            ("BA_n", val("BA_n")),
            ("Reference", val("Reference")),
            ("A_m", val("A_m")),
            ("P_s", val("P_s")),
            ("BA_A Number", val("BA_Account_number")),
        ]
        req_rows = [
            ("Level", f"Level {active['level']}"),
            ("A_m", money(active["A_m"])),
            ("Reason", active["note"] or "-"),
        ]

    return page(TOPUP_BODY, user=user, history=history,
                instructions=instructions, active=active, left=left,
                details=details, info_rows=info_rows, req_rows=req_rows,
                TOPUP_MIN_CENTS=TOPUP_MIN_CENTS,
                TOPUP_MAX_CENTS=TOPUP_MAX_CENTS)


# ============================================================================
# WITHDRAWALS
# ============================================================================

@app.route("/withdraw", methods=["GET", "POST"])
@login_required
def withdraw():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        cents = parse_A_m(request.form.get("A_m"), WITHDRAW_MIN_CENTS)
        if cents is None:
            flash(f"Enter a valid A_m (minimum {money(WITHDRAW_MIN_CENTS)}).",
                  "error")
            return redirect(url_for("withdraw"))

        if not debit(db, user["id"], cents, "WITHDRAW",
                     "Withdrawal request (pending)"):
            db.rollback()
            flash("Insufficient play balance.", "error")
            return redirect(url_for("withdraw"))

        db.execute(
            "INSERT INTO withdrawals (user_id, A_m, BA_n, "
            "account_number, account_holder, status, created_at) "
            "VALUES (?, ?, 'DEMO', 'N/A', ?, 'PENDING', ?)",
            (user["id"], cents, user["username"], now_iso()))
        db.commit()
        flash(f"Withdrawal request for {money(cents)} created. "
              "It will be reviewed", "ok")
        return redirect(url_for("withdraw"))

    withdrawals = db.execute(
        "SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC",
        (user["id"],)).fetchall()
    return page(WITHDRAW_BODY, user=user, withdrawals=withdrawals,
                WITHDRAW_MIN_CENTS=WITHDRAW_MIN_CENTS)


# ============================================================================
# TEAM VIEW
# ============================================================================

@app.route("/team")
@login_required
def team():
    db = get_db()
    user = current_user()

    members = db.execute(
        "SELECT username, level, points, streak, created_at FROM users "
        "WHERE referred_by=? AND phone_verified=1 "
        "ORDER BY level DESC, points DESC",
        (user["id"],)).fetchall()

    earned = db.execute(
        "SELECT COALESCE(SUM(A_m),0) AS s FROM transactions "
        "WHERE user_id=? AND kind='REFERRAL'", (user["id"],)).fetchone()["s"]

    team_points = sum(m["points"] for m in members)

    referrer = None
    if user["referred_by"]:
        referrer = db.execute("SELECT username FROM users WHERE id=?",
                              (user["referred_by"],)).fetchone()

    # Team tiers: a name and a team account number unlock at 5/10/15/20 members
    count = len(members)
    tiers = []
    for size, name in TEAM_TIERS:
        row = db.execute(
            "SELECT account_number FROM team_tiers WHERE user_id=? AND members=?",
            (user["id"], size)).fetchone()
        if row is None and count >= size:
            db.execute(
                "INSERT OR IGNORE INTO team_tiers "
                "(user_id, members, name, account_number, created_at) "
                "VALUES (?,?,?,?,?)",
                (user["id"], size, name, new_team_account_number(db), now_iso()))
            db.commit()
            row = db.execute(
                "SELECT account_number FROM team_tiers "
                "WHERE user_id=? AND members=?", (user["id"], size)).fetchone()
        tiers.append({"size": size, "name": name,
                      "number": row["account_number"] if row else None})
    reached = [t for t in tiers if t["number"]]
    team_name = reached[-1]["name"] if reached else None
    next_tier = next((t for t in tiers if not t["number"]), None)
    tier_pct = (min(100, int(count / next_tier["size"] * 100))
                if next_tier else 100)

    return page(TEAM_BODY, user=user, members=members, earned=earned,
                team_points=team_points, referrer=referrer, tiers=tiers,
                team_name=team_name, next_tier=next_tier, tier_pct=tier_pct)


# ============================================================================
# LEVEL UPGRADES
# ============================================================================

@app.route("/upgrade", methods=["GET", "POST"])
@login_required
def upgrade():
    db = get_db()
    user = current_user()
    next_level = user["level"] + 1 if user["level"] < max(LEVELS) else None

    if request.method == "POST":
        if next_level is None:
            flash("You are already at the highest level.", "error")
            return redirect(url_for("upgrade"))

        cost = UPGRADE_COST_CENTS[next_level]
        # Level check is part of the UPDATE so a double-click can't upgrade twice
        if not debit(db, user["id"], cost, "UPGRADE",
                     f"Upgrade to Level {next_level}"):
            db.rollback()
            flash(f"You need {money(cost)} to upgrade. Top up first.", "error")
            return redirect(url_for("upgrade"))

        moved = db.execute(
            "UPDATE users SET level=? WHERE id=? AND level=?",
            (next_level, user["id"], user["level"])).rowcount
        if moved != 1:
            db.rollback()
            flash("Upgrade failed. Please try again.", "error")
            return redirect(url_for("upgrade"))

        pay_referral_bonus(db, user, next_level)
        db.commit()
        flash(f"Upgraded to Level {next_level} for {money(cost)}!", "ok")
        return redirect(url_for("upgrade"))

    return page(UPGRADE_BODY, user=user, next_level=next_level)


# ============================================================================
# LEVEL CODE REDEEM (codes generated by the admin)
# ============================================================================

@app.route("/redeem", methods=["POST"])
@login_required
def redeem():
    db = get_db()
    user = current_user()
    code = request.form.get("voucher", "").strip().upper()

    v = db.execute("SELECT * FROM vouchers WHERE code=?", (code,)).fetchone()
    if v is None or (v["issued_to"] is not None
                     and v["issued_to"] != user["id"]):
        flash("That code is not valid.", "error")
        return redirect(url_for("upgrade"))
    if v["used_by"] is not None:
        flash("That code has already been used.", "error")
        return redirect(url_for("upgrade"))
    if v["level"] <= user["level"]:
        flash(f"That code is for Level {v['level']}, which is not above "
              f"your current level.", "error")
        return redirect(url_for("upgrade"))

    claimed = db.execute(
        "UPDATE vouchers SET used_by=?, used_at=? "
        "WHERE code=? AND used_by IS NULL",
        (user["id"], now_iso(), code)).rowcount
    moved = db.execute(
        "UPDATE users SET level=? WHERE id=? AND level<?",
        (v["level"], user["id"], v["level"])).rowcount
    if claimed != 1 or moved != 1:
        db.rollback()
        flash("Code could not be redeemed. Please try again.", "error")
        return redirect(url_for("upgrade"))

    pay_referral_bonus(db, user, v["level"])
    db.commit()
    flash(f"Code redeemed. You are now Level {v['level']}.", "ok")
    return redirect(url_for("upgrade"))


# ============================================================================
# SAVINGS POTS
# ============================================================================

@app.route("/pots", methods=["GET", "POST"])
@login_required
def pots():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        cents = parse_A_m(request.form.get("A_m"), POT_MIN_CENTS)
        try:
            weeks = int(request.form.get("weeks", 0))
        except ValueError:
            weeks = 0

        if cents is None or weeks not in POT_APR_PERCENT:
            flash(f"Enter at least {money(POT_MIN_CENTS)} and pick 1-9 weeks.",
                  "error")
            return redirect(url_for("pots"))

        if not debit(db, user["id"], cents, "POT_LOCK",
                     f"Locked in savings pot for {weeks} week(s)"):
            db.rollback()
            flash("Insufficient play balance.", "error")
            return redirect(url_for("pots"))

        interest = pot_interest_cents(cents, weeks)
        matures = (datetime.now(timezone.utc) + timedelta(weeks=weeks)
                   ).strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO savings_pots (user_id, principal, weeks, "
            "apr_percent, interest, created_at, matures_at, status) "
            "VALUES (?,?,?,?,?,?,?, 'LOCKED')",
            (user["id"], cents, weeks, str(POT_APR_PERCENT[weeks]),
             interest, now_iso(), matures))
        db.commit()
        flash(f"Locked {money(cents)} for {weeks} week(s). "
              f"Interest at maturity: {money(interest)}.", "ok")
        return redirect(url_for("pots"))

    my_pots = db.execute(
        "SELECT * FROM savings_pots WHERE user_id=? ORDER BY id DESC",
        (user["id"],)).fetchall()
    return page(POTS_BODY, user=user, my_pots=my_pots, now=now_iso(),
                POT_MIN_CENTS=POT_MIN_CENTS)


@app.route("/pots/<int:pot_id>/collect", methods=["POST"])
@login_required
def pot_collect(pot_id):
    db = get_db()
    user = current_user()
    pot = db.execute("SELECT * FROM savings_pots WHERE id=? AND user_id=?",
                     (pot_id, user["id"])).fetchone()
    if pot is None:
        abort(404)

    claimed = db.execute(
        "UPDATE savings_pots SET status='PAID', closed_at=? "
        "WHERE id=? AND user_id=? AND status='LOCKED' AND matures_at<=?",
        (now_iso(), pot_id, user["id"], now_iso())).rowcount
    if claimed != 1:
        db.rollback()
        flash("This pot is not ready to collect yet.", "error")
        return redirect(url_for("pots"))

    credit(db, user["id"], pot["principal"], "POT_PAYOUT",
           f"Pot #{pot_id} principal returned")
    credit(db, user["id"], pot["interest"], "POT_PAYOUT",
           f"Pot #{pot_id} interest ({pot['apr_percent']}% p.a.)")
    db.commit()
    flash(f"Collected {money(pot['principal'] + pot['interest'])} "
          "from your savings pot.", "ok")
    return redirect(url_for("pots"))


@app.route("/pots/<int:pot_id>/break", methods=["POST"])
@login_required
def pot_break(pot_id):
    db = get_db()
    user = current_user()
    pot = db.execute("SELECT * FROM savings_pots WHERE id=? AND user_id=?",
                     (pot_id, user["id"])).fetchone()
    if pot is None:
        abort(404)

    claimed = db.execute(
        "UPDATE savings_pots SET status='BROKEN', closed_at=? "
        "WHERE id=? AND user_id=? AND status='LOCKED'",
        (now_iso(), pot_id, user["id"])).rowcount
    if claimed != 1:
        db.rollback()
        flash("This pot is already closed.", "error")
        return redirect(url_for("pots"))

    credit(db, user["id"], pot["principal"], "POT_BREAK",
           f"Pot #{pot_id} closed early (interest forfeited)")
    db.commit()
    flash(f"Pot closed early. {money(pot['principal'])} returned, "
          "interest forfeited.", "ok")
    return redirect(url_for("pots"))


# ============================================================================
# PROFILE
# ============================================================================

def _profile_page(member, is_self):
    db = get_db()
    referrer = None
    if member["referred_by"]:
        referrer = db.execute("SELECT username FROM users WHERE id=?",
                              (member["referred_by"],)).fetchone()
    return page(PROFILE_BODY, member=member, referrer=referrer,
                is_self=is_self)


@app.route("/profile")
@login_required
def profile():
    return _profile_page(current_user(), True)


@app.route("/admin/user/<int:user_id>")
@admin_required
def admin_user_profile(user_id):
    member = get_db().execute("SELECT * FROM users WHERE id=?",
                              (user_id,)).fetchone()
    if member is None:
        abort(404)
    return _profile_page(member, False)


# ============================================================================
# ADMIN
# ============================================================================

@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    db = get_db()

    if request.method == "POST":
        try:
            level = int(request.form.get("level", 0))
            count = int(request.form.get("count", 0))
        except ValueError:
            level, count = 0, 0

        if level not in LEVELS or not 1 <= count <= 100:
            flash("Pick a level 1-7 and a count from 1 to 100.", "error")
        else:
            codes = generate_vouchers(db, level, count)
            flash(f"Created {len(codes)} Level {level} code(s).", "ok")
        return redirect(url_for("admin"))

    one = lambda sql: db.execute(sql).fetchone()[0]
    summary = {
        "users": one("SELECT COUNT(*) FROM users WHERE is_admin=0"),
        "balances": one("SELECT COALESCE(SUM(points),0) FROM users "
                        "WHERE is_admin=0"),
        "locked": one("SELECT COALESCE(SUM(principal),0) FROM savings_pots "
                      "WHERE status='LOCKED'"),
        "interest_owed": one("SELECT COALESCE(SUM(interest),0) FROM "
                             "savings_pots WHERE status='LOCKED'"),
        "pending_topups": one("SELECT COUNT(*) FROM topups "
                              "WHERE status='PENDING'"),
        "pending_withdrawals": one("SELECT COUNT(*) FROM withdrawals "
                                   "WHERE status='PENDING'"),
    }

    # Search box: filters members, top-ups, withdrawals, pots and ledger
    q = request.args.get("q", "").strip()[:30]
    like = f"%{q}%"
    where = "WHERE users.username LIKE ? " if q else ""
    args = (like,) if q else ()

    users_sql = (
        "SELECT u.*, r.username AS referrer FROM users u "
        "LEFT JOIN users r ON r.id = u.referred_by ")
    if q:
        digits = re.sub(r"\D", "", q).lstrip("0")
        phone_like = f"%{digits}%" if digits else like
        users_sql += ("WHERE u.username LIKE ? OR u.serial_number LIKE ? "
                      "OR u.referral_code LIKE ? OR u.phone LIKE ? ")
        users_args = (like, like, like, phone_like)
    else:
        users_args = ()
    users_sql += "ORDER BY u.is_admin DESC, u.level DESC, u.points DESC"
    users = db.execute(users_sql, users_args).fetchall()

    topups = db.execute(
        "SELECT topups.*, users.username FROM topups "
        "JOIN users ON users.id = topups.user_id " + where +
        "ORDER BY (topups.status='PENDING') DESC, topups.id DESC "
        "LIMIT 50", args).fetchall()
    withdrawals = db.execute(
        "SELECT withdrawals.*, users.username FROM withdrawals "
        "JOIN users ON users.id = withdrawals.user_id " + where +
        "ORDER BY (withdrawals.status='PENDING') DESC, withdrawals.id DESC "
        "LIMIT 50", args).fetchall()
    pot_rows = db.execute(
        "SELECT savings_pots.*, users.username FROM savings_pots "
        "JOIN users ON users.id = savings_pots.user_id " + where +
        "ORDER BY savings_pots.id DESC LIMIT 50", args).fetchall()
    ledger = db.execute(
        "SELECT transactions.*, users.username FROM transactions "
        "JOIN users ON users.id = transactions.user_id " + where +
        "ORDER BY transactions.id DESC LIMIT 60", args).fetchall()
    unused = db.execute(
        "SELECT * FROM vouchers WHERE used_by IS NULL "
        "ORDER BY level, created_at").fetchall()

    # Member-specific top-up instructions (override the global text)
    specific = db.execute(
        "SELECT ui.*, u.username FROM user_instructions ui "
        "JOIN users u ON u.id = ui.user_id "
        "ORDER BY ui.updated_at DESC").fetchall()

    # Top-up details shown in the white top-up tab (global + per member)
    global_details = db.execute(
        "SELECT * FROM topup_details WHERE user_id=0").fetchone()
    specific_details = db.execute(
        "SELECT d.*, u.username FROM topup_details d "
        "JOIN users u ON u.id = d.user_id WHERE d.user_id<>0 "
        "ORDER BY d.updated_at DESC").fetchall()

    return page(ADMIN_BODY, summary=summary, users=users, topups=topups,
                withdrawals=withdrawals, pot_rows=pot_rows, ledger=ledger,
                unused=unused, q=q, specific=specific,
                gd=global_details, specific_details=specific_details,
                instructions=get_setting(db, "topup_instructions",
                                         DEFAULT_TOPUP_INSTRUCTIONS))


def _find_member(raw):
    """Find a member by numeric ID or by cellphone number."""
    db = get_db()
    raw = (raw or "").strip()
    phone = normalize_phone(raw)
    if phone:
        return db.execute(
            "SELECT id, username FROM users WHERE username=? OR phone=?",
            (phone, phone)).fetchone()
    if raw.isascii() and raw.isdigit():
        return db.execute("SELECT id, username FROM users WHERE id=?",
                          (int(raw),)).fetchone()
    return None


@app.route("/admin/instructions", methods=["POST"])
@admin_required
def admin_instructions():
    text = request.form.get("instructions", "").strip()
    scope = request.form.get("scope", "global")

    if not text:
        flash("Instruction text cannot be empty.", "error")
        return redirect(url_for("admin"))
    if len(text) > 1000:
        flash("Instruction text is too long (max 1000 characters).", "error")
        return redirect(url_for("admin"))

    db = get_db()
    if scope == "user":
        member = _find_member(request.form.get("target", ""))
        if member is None:
            flash("No member found with that ID.", "error")
            return redirect(url_for("admin"))
        db.execute(
            "INSERT INTO user_instructions (user_id, text, updated_at) "
            "VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "text=excluded.text, updated_at=excluded.updated_at",
            (member["id"], text, now_iso()))
        db.commit()
        flash(f"Instruction saved for member {member['username']} "
              f"(ID {member['id']}).", "ok")
    else:
        set_setting(db, "topup_instructions", text)
        db.commit()
        flash("Global top-up instruction text updated.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/instructions/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_instruction_delete(user_id):
    db = get_db()
    db.execute("DELETE FROM user_instructions WHERE user_id=?", (user_id,))
    db.commit()
    flash("Member instruction removed. They now see the global text.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/topup-details", methods=["POST"])
@admin_required
def admin_topup_details():
    """Admin writes the Top up details, red message and flag that members
    see (and can copy) in the white tab that opens after they tap a level on
    the Top up page. Members cannot enter or change any of this."""
    db = get_db()
    scope = request.form.get("scope", "global")

    if scope == "user":
        member = _find_member(request.form.get("target", ""))
        if member is None:
            flash("No member found with that ID or cellphone number.", "error")
            return redirect(url_for("admin"))
        target_id, label = member["id"], f"member {member['username']}"
    else:
        target_id, label = 0, "everyone"

    fields = {key: request.form.get(key, "").strip()[:100]
              for key in ("BA_n", "Reference", "A_m",
                          "P_s", "BA_Account_number")}
    message = request.form.get("message", "").strip()[:500]

    old = db.execute("SELECT * FROM topup_details WHERE user_id=?",
                     (target_id,)).fetchone()
    flag = old["flag"] if old else ""
    new_flag = ""

    photo = request.files.get("flag")
    if photo is not None and photo.filename:
        data = photo.read()
        ext = _image_ext(data)
        if not data or ext is None:
            flash("The flag must be a PNG, JPG, GIF or WEBP image.", "error")
            return redirect(url_for("admin"))
        new_flag = _save_image(data, ext)
        flag = new_flag

    db.execute(
        "INSERT INTO topup_details (user_id, BA_n, Reference, "
        "A_m, P_s, BA_A,ccount_number, message, flag, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "BA_n=excluded.BA_n, "
        "Reference=excluded.Reference, "
        "A_m=excluded.A_m, "
        "P_s=excluded.P_s, "
        "BA_Account_number=excluded.BA_Account_number, message=excluded.message, "
        "flag=excluded.flag, updated_at=excluded.updated_at",
        (target_id, fields["BA_n"], fields["Reference"],
         fields["A_m"], fields["P_s"],
         fields["BA_Account_number"], message, flag, now_iso()))
    try:
        db.commit()
    except sqlite3.Error:
        db.rollback()
        _remove_upload(new_flag)
        app.logger.exception("Failed to save admin top-up details")
        flash("Could not save top-up details because of a database error.", "error")
        return redirect(url_for("admin"))

    if new_flag and old and old["flag"]:
        _remove_upload(old["flag"])
    flash(f"Top-up details saved for {label}.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/topup-details/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_topup_details_delete(user_id):
    db = get_db()
    old = db.execute("SELECT flag FROM topup_details WHERE user_id=?",
                     (user_id,)).fetchone()
    db.execute("DELETE FROM topup_details WHERE user_id=?", (user_id,))
    db.commit()
    if old:
        _remove_upload(old["flag"])
    flash("Top-up details removed.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/welcome", methods=["POST"])
@admin_required
def admin_welcome():
    text = request.form.get("welcome_text", "").strip()
    if not text:
        flash("Welcome text cannot be empty.", "error")
    elif len(text) > 120:
        flash("Welcome text is too long (max 120 characters).", "error")
    else:
        db = get_db()
        set_setting(db, "welcome_text", text)
        db.commit()
        flash("Welcome text updated.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/user/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    """ Sets a NEW password for a member (replaces 'forgot password').
    Passwords are never stored or shown in readable form."""
    new_password = request.form.get("new_password", "")
    if len(new_password) < 8:
        flash("New password must be at least 8 characters.", "error")
        return redirect(url_for("admin"))

    db = get_db()
    target = db.execute("SELECT id, username FROM users WHERE id=?",
                        (user_id,)).fetchone()
    if target is None:
        flash("Member not found.", "error")
        return redirect(url_for("admin"))

    db.execute("UPDATE users SET pw_hash=? WHERE id=?",
               (generate_password_hash(new_password), user_id))
    db.commit()
    flash(f"Password for account {target['username']} was reset. "
          "Give them the new password.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/user/<int:user_id>/add-balance", methods=["POST"])
@admin_required
def admin_add_balance(user_id):
    """Admin adds balance to a member. It is written to the transaction
    ledger, so it shows in the member's activity and the admin ledger.
    This is the ONLY manual way money is added to a balance."""
    back = request.form.get("q", "").strip()[:30] or None
    cents = parse_A_m(request.form.get("A_m"), 1, ADMIN_ADD_MAX_CENTS)
    note = request.form.get("note", "").strip()[:100]

    if cents is None:
        flash(f"Enter an A_m from R0.01 to {money(ADMIN_ADD_MAX_CENTS)}.",
              "error")
        return redirect(url_for("admin", q=back))

    db = get_db()
    target = db.execute("SELECT id, username FROM users WHERE id=?",
                        (user_id,)).fetchone()
    if target is None:
        flash("Member not found.", "error")
        return redirect(url_for("admin", q=back))

    credit(db, user_id, cents, "CREDIT",
           note or "Balance added by admin")
    db.commit()
    flash(f"Added {money(cents)} to account {target['username']}.", "ok")
    return redirect(url_for("admin", q=back))


@app.route("/admin/topup/<int:topup_id>/<action>", methods=["POST"])
@admin_required
def admin_topup(topup_id, action):
    if action not in ("approve", "reject"):
        abort(404)
    db = get_db()
    t = db.execute("SELECT * FROM topups WHERE id=?", (topup_id,)).fetchone()
    if t is None:
        flash("Top-up request not found.", "error")
        return redirect(url_for("admin"))

    new_status = "APPROVED" if action == "approve" else "REJECTED"
    claimed = db.execute(
        "UPDATE topups SET status=?, decided_at=? "
        "WHERE id=? AND status='PENDING'",
        (new_status, now_iso(), topup_id)).rowcount
    if claimed != 1:
        db.rollback()
        flash("This top-up was already processed.", "error")
        return redirect(url_for("admin"))

    # Approving only marks the request. The balance is NOT increased here:
    # it grows through check-in, or when the admin uses "Add balance".
    db.commit()
    msg = f"Top-up #{topup_id} {new_status.lower()}."
    if action == "approve":
        msg += " No balance was added."
    flash(msg, "ok")
    return redirect(url_for("admin"))


@app.route("/admin/withdrawal/<int:withdrawal_id>/<action>", methods=["POST"])
@admin_required
def admin_withdrawal(withdrawal_id, action):
    if action not in ("complete", "reject"):
        abort(404)
    db = get_db()
    w = db.execute("SELECT * FROM withdrawals WHERE id=?",
                   (withdrawal_id,)).fetchone()
    if w is None:
        flash("Withdrawal request not found.", "error")
        return redirect(url_for("admin"))

    new_status = "COMPLETED" if action == "complete" else "REJECTED"
    claimed = db.execute(
        "UPDATE withdrawals SET status=? WHERE id=? AND status='PENDING'",
        (new_status, withdrawal_id)).rowcount
    if claimed != 1:
        db.rollback()
        flash("This withdrawal was already processed.", "error")
        return redirect(url_for("admin"))

    # Completing only changes the demo status. NO real money moves.
    if action == "reject":
        credit(db, w["user_id"], w["A_m"], "WITHDRAW_REFUND",
               f"Withdrawal #{withdrawal_id} rejected - refunded")
    db.commit()
    flash(f"Withdrawal #{withdrawal_id} {new_status.lower()}.", "ok")
    return redirect(url_for("admin"))


# ============================================================================
# TEMPLATES
# ============================================================================

BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#050c13">
<title>Vedo Invest</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#050c13; --s1:#0b1721; --s2:#10212d; --s3:#152c3b; --line:#1a3140;
  --ink:#e6f0f4; --muted:#7d95a3; --brand:#1fd6a4; --brand-d:#0f766e;
  --blue:#2b7fff; --up:#27d69b; --down:#ff5c6c; --amber:#f2b84b; --violet:#8b7cf6;
  --r:20px;
}
*{box-sizing:border-box;}
html{-webkit-text-size-adjust:100%;}
body{
  margin:0; min-height:100vh; color:var(--ink);
  font-family:'Plus Jakarta Sans',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  font-variant-numeric:tabular-nums; line-height:1.45;
  background:
    radial-gradient(1000px 480px at 88% -8%,rgba(31,214,164,.13),transparent 60%),
    radial-gradient(800px 420px at -8% 6%,rgba(43,127,255,.10),transparent 60%),
    var(--bg);
  background-attachment:fixed;
}
a{color:var(--brand);}
.ic{width:22px;height:22px;display:block;}

/* ---------- ticker ---------- */
.ticker{background:#030a10;border-bottom:1px solid var(--line);overflow:hidden;white-space:nowrap;font-size:.78rem;color:var(--muted);}
.track{display:inline-flex;width:max-content;animation:tick 42s linear infinite;}
.ticker:hover .track{animation-play-state:paused;}
.tk{padding:7px 22px;border-right:1px solid var(--line);}
.tk b{color:var(--up);font-weight:700;}
@keyframes tick{to{transform:translateX(-50%);}}

/* ---------- header / tabs ---------- */
.top{position:sticky;top:0;z-index:30;background:rgba(5,12,19,.82);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid var(--line);}
nav{max-width:1080px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:8px;}
.brand{display:flex;align-items:center;gap:10px;margin-right:auto;font-weight:800;color:var(--ink);text-decoration:none;letter-spacing:.2px;}
.logo{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;color:#031016;font-weight:800;background:linear-gradient(135deg,var(--brand),var(--blue));}
.tabs{display:flex;gap:4px;}
.tabs a{display:flex;align-items:center;gap:8px;padding:8px 14px;border-radius:12px;color:var(--muted);text-decoration:none;font-weight:600;font-size:.92rem;}
.tabs a .ic{width:18px;height:18px;}
.tabs a:hover{color:var(--ink);background:var(--s1);}
.tabs a.active{color:var(--ink);background:var(--s2);box-shadow:inset 0 -2px 0 var(--brand);}
nav form{margin:0;}
nav button{background:var(--s1);border:1px solid var(--line);color:var(--muted);border-radius:12px;padding:8px 12px;cursor:pointer;font:inherit;font-size:.85rem;}
nav button:hover{color:var(--ink);}
nav .auth-link{color:var(--muted);text-decoration:none;font-weight:600;padding:8px 12px;border-radius:12px;}
nav .auth-link:hover{color:var(--ink);background:var(--s1);}

.subnav{max-width:1080px;margin:16px auto 0;padding:0 16px;}
.seg{display:inline-flex;gap:4px;background:var(--s1);border:1px solid var(--line);padding:4px;border-radius:999px;max-width:100%;overflow-x:auto;}
.seg a{padding:7px 16px;border-radius:999px;color:var(--muted);text-decoration:none;font-weight:600;font-size:.88rem;white-space:nowrap;}
.seg a:hover{color:var(--ink);}
.seg a.active{background:linear-gradient(135deg,var(--brand),#18b98f);color:#031016;}

main{max-width:1080px;margin:20px auto 40px;padding:0 16px;}

/* ---------- cards & text ---------- */
.card{background:linear-gradient(180deg,var(--s1),#0a141d);border:1px solid var(--line);border-radius:var(--r);padding:22px;margin-bottom:16px;overflow-x:auto;}
.card.note{border-left:3px solid var(--brand);}
h1{margin:0 0 6px;font-size:1.55rem;letter-spacing:-.3px;}
h2{margin:0 0 14px;font-size:1.08rem;letter-spacing:-.1px;}
p{margin:0 0 10px;}
.muted{color:var(--muted);} .small{font-size:.85rem;}
.pos{color:var(--up);} .neg{color:var(--down);}
code{background:var(--s2);border:1px solid var(--line);padding:2px 8px;border-radius:8px;color:var(--brand);}
ul{padding-left:20px;color:var(--muted);} li{margin-bottom:4px;}

/* ---------- forms ---------- */
label{display:block;margin:14px 0 6px;font-weight:600;font-size:.88rem;color:var(--muted);}
input,select,textarea{width:100%;padding:12px 14px;background:var(--s2);color:var(--ink);border:1px solid var(--line);border-radius:14px;font-size:1rem;font-family:inherit;}
input::placeholder,textarea::placeholder{color:#506a7a;}
input:focus,select:focus,textarea:focus{outline:2px solid var(--brand);outline-offset:1px;border-color:transparent;}
button.primary{margin-top:18px;background:linear-gradient(135deg,var(--brand),#18b98f);color:#031016;border:0;border-radius:14px;padding:13px 22px;font-size:1rem;font-weight:700;cursor:pointer;font-family:inherit;}
button.primary:hover{filter:brightness(1.07);}
button.primary:disabled{background:var(--s3);color:#5f7888;cursor:not-allowed;filter:none;}
button.danger{background:rgba(255,92,108,.14);color:var(--down);border:1px solid rgba(255,92,108,.35);border-radius:10px;padding:6px 12px;cursor:pointer;font-family:inherit;font-weight:600;}
button.success{background:rgba(39,214,155,.14);color:var(--up);border:1px solid rgba(39,214,155,.35);border-radius:10px;padding:6px 12px;cursor:pointer;font-family:inherit;font-weight:600;}
button.ghost{background:var(--s2);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:7px 14px;cursor:pointer;font-family:inherit;font-weight:600;}
button.ghost:disabled{opacity:.55;cursor:not-allowed;}
button:focus-visible,a:focus-visible{outline:2px solid var(--brand);outline-offset:2px;}
.inline{display:inline-block;margin:0 2px;}
.pwbox{width:150px;padding:7px 10px;font-size:.85rem;border-radius:10px;}

/* ---------- flash ---------- */
.flash{padding:12px 16px;border-radius:14px;margin-bottom:14px;border:1px solid;font-weight:500;}
.flash.ok{background:rgba(39,214,155,.10);color:var(--up);border-color:rgba(39,214,155,.3);}
.flash.error{background:rgba(255,92,108,.10);color:var(--down);border-color:rgba(255,92,108,.3);}

/* ---------- tables ---------- */
table{width:100%;border-collapse:collapse;}
th{color:var(--muted);font-weight:600;font-size:.8rem;}
th,td{text-align:left;padding:11px 10px;border-bottom:1px solid var(--line);font-size:.92rem;}
tr:last-child td{border-bottom:0;}
tr.hl td{background:rgba(31,214,164,.08);}
.badge{display:inline-block;background:var(--brand-d);color:#d9fff3;border-radius:999px;padding:2px 11px;font-size:.78rem;font-weight:700;}
.badge.ok{background:rgba(39,214,155,.18);color:var(--up);}
.badge.err{background:rgba(255,92,108,.18);color:var(--down);}
.badge.wait{background:rgba(242,184,75,.18);color:var(--amber);}

/* ---------- stats ---------- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;}
.stat{background:var(--s2);border:1px solid var(--line);border-radius:16px;padding:16px;}
.stat b{display:block;font-size:1.35rem;letter-spacing:-.3px;}
.stat span{color:var(--muted);font-size:.84rem;}

/* ---------- dashboard ---------- */
.dash{display:grid;grid-template-columns:1fr 1.15fr;gap:16px;margin-bottom:16px;}
.dash .card{margin-bottom:0;}
.hero{border-radius:26px;padding:24px;color:#eafff8;border:1px solid rgba(31,214,164,.35);
  background:
    radial-gradient(500px 220px at 100% 0%,rgba(31,214,164,.35),transparent 65%),
    linear-gradient(135deg,#0f766e 0%,#0b3d49 55%,#08242f 100%);}
.hero-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;}
.hello{margin:0 0 18px;font-weight:600;opacity:.9;}
.lbl{margin:0 0 2px;font-size:.84rem;opacity:.75;}
.balance{font-size:2.7rem;font-weight:800;letter-spacing:-1px;line-height:1.1;}
.fpwrap{display:flex;flex-direction:column;align-items:center;gap:6px;flex:none;}
.fp{width:78px;height:78px;padding:0;border-radius:50%;display:grid;place-items:center;cursor:pointer;color:#031016;background:linear-gradient(135deg,#1fd6a4,#8ff5dc);border:3px solid rgba(255,255,255,.9);box-shadow:0 8px 24px rgba(0,0,0,.35);animation:fppulse 2s ease-out infinite;font:inherit;}
.fp .ic{width:44px;height:44px;stroke-width:2.2;}
.fp:hover{filter:brightness(1.08);}
.fp:active{transform:scale(.94);}
.fp.open{animation:none;background:#fff;box-shadow:0 0 22px rgba(31,214,164,.85);}
.fpcap{font-size:.74rem;font-weight:800;letter-spacing:.3px;color:#eafff8;text-align:center;}
@keyframes fppulse{0%{box-shadow:0 0 0 0 rgba(31,214,164,.75),0 8px 24px rgba(0,0,0,.35);}70%{box-shadow:0 0 0 20px rgba(31,214,164,0),0 8px 24px rgba(0,0,0,.35);}100%{box-shadow:0 0 0 0 rgba(31,214,164,0),0 8px 24px rgba(0,0,0,.35);}}
.actions.closed{display:none;}
.actions.reveal{animation:revealin .35s ease-out;}
@keyframes revealin{from{opacity:0;transform:translateY(10px);}to{opacity:1;transform:none;}}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 20px;}
.chip{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);padding:4px 12px;border-radius:999px;font-size:.8rem;font-weight:600;}
.actions{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;background:rgba(3,12,18,.45);border-radius:18px;padding:12px 6px;}
.act{display:flex;flex-direction:column;align-items:center;gap:7px;color:#d9f5ec;text-decoration:none;font-size:.76rem;font-weight:600;}
.act span{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;background:rgba(31,214,164,.16);color:var(--brand);border:1px solid rgba(31,214,164,.35);}
.act:hover span{background:var(--brand);color:#031016;}

.donutwrap{position:relative;width:170px;height:170px;margin:4px auto 14px;}
.donut{width:100%;height:100%;transform:rotate(-90deg);}
.donut circle{fill:none;stroke-width:11;}
.donut .ring{stroke:var(--s3);}
.donut .a1{stroke:var(--brand);}
.donut .a2{stroke:var(--violet);}
.donut-mid{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;}
.donut-mid b{font-size:1.05rem;} .donut-mid span{font-size:.74rem;color:var(--muted);}
.legend{display:flex;justify-content:space-between;gap:10px;font-size:.84rem;color:var(--muted);margin-bottom:14px;}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;}
.bar{height:9px;border-radius:99px;background:var(--s3);overflow:hidden;}
.bar b{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--brand),var(--blue));}
.progtxt{display:flex;justify-content:space-between;font-size:.82rem;color:var(--muted);margin:0 0 7px;}

.dots{display:flex;gap:8px;margin:6px 0 14px;}
.dots i{width:30px;height:30px;border-radius:50%;background:var(--s2);border:1px solid var(--line);}
.dots i.on{background:var(--brand);border-color:var(--brand);box-shadow:0 0 14px rgba(31,214,164,.45);}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;}
.tile{border-radius:14px;padding:14px;border:1px solid var(--line);}
.tile b{display:block;font-size:.8rem;opacity:.85;}
.tile strong{display:block;font-size:1.05rem;margin:4px 0 2px;}
.tile span{font-size:.78rem;opacity:.8;}
.t1{background:rgba(39,214,155,.10);} .t2{background:rgba(39,214,155,.16);} .t3{background:rgba(39,214,155,.23);}
.t4{background:rgba(39,214,155,.30);} .t5{background:rgba(39,214,155,.38);} .t6{background:rgba(39,214,155,.46);}
.t7{background:rgba(39,214,155,.56);}
.tile.me{outline:2px solid #fff;outline-offset:1px;}

.bars{display:flex;align-items:flex-end;gap:6px;height:90px;margin:4px 0 16px;padding:8px;background:var(--s2);border-radius:14px;border:1px solid var(--line);}
.bars i{flex:1;border-radius:4px 4px 0 0;min-height:6px;}
.bars i.up{background:var(--up);} .bars i.dn{background:var(--down);}
.tx{display:flex;align-items:center;gap:12px;padding:11px 0;border-bottom:1px solid var(--line);}
.tx:last-child{border-bottom:0;}
.av{width:42px;height:42px;flex:none;border-radius:50%;display:grid;place-items:center;font-weight:800;background:var(--s3);color:var(--brand);}
.tx .who{flex:1;min-width:0;}
.tx .who b{display:block;font-size:.95rem;}
.tx .who span{display:block;color:var(--muted);font-size:.8rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.tx .amt{font-weight:700;white-space:nowrap;}

/* ---------- profile ---------- */
.phead{display:flex;align-items:center;gap:16px;margin-bottom:18px;}
.avatar{width:68px;height:68px;border-radius:50%;display:grid;place-items:center;font-size:1.4rem;font-weight:800;color:#031016;background:linear-gradient(135deg,var(--brand),var(--blue));}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin:0;}
.kv div{background:var(--s2);border:1px solid var(--line);border-radius:14px;padding:13px 15px;}
.kv dt{color:var(--muted);font-size:.8rem;margin-bottom:3px;}
.kv dd{margin:0;font-weight:700;word-break:break-all;}

/* ---------- team ---------- */
.refcode{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px;}
.refcode code{font-size:1.4rem;padding:8px 16px;}

/* ---------- check-in tasks & admin posts ---------- */
.taskgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-top:14px;}
.task{position:relative;overflow:hidden;text-align:left;background:var(--s2);color:var(--ink);border:1px solid var(--line);border-radius:14px;padding:16px;cursor:pointer;font-family:inherit;font-size:.95rem;}
.task:hover{border-color:var(--brand);}
.task .fill{position:absolute;left:0;top:0;bottom:0;width:0;background:rgba(31,214,164,.28);}
.task .tl{position:relative;display:flex;flex-direction:column;gap:4px;}
.task .tr{color:var(--muted);font-size:.85rem;}
.task.loading .tr{color:var(--brand);}
.task.done{background:rgba(39,214,155,.12);border-color:rgba(39,214,155,.4);cursor:default;}
.task.done .tr{color:var(--up);}
.task.locked{opacity:.45;cursor:not-allowed;}
button.tile{font-family:inherit;font-size:inherit;color:var(--ink);text-align:left;cursor:pointer;width:100%;}
button.tile:hover{border-color:var(--brand);}
button.tile:disabled{opacity:.4;cursor:not-allowed;border-color:var(--line);}
button.tile.pulse{animation:pulse 1.8s ease-in-out infinite;}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(31,214,164,.55);}50%{box-shadow:0 0 0 9px rgba(31,214,164,0);}}
#lvlmsg:empty{display:none;}
#lvlmsg{margin-bottom:14px;}
.feedhead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:14px;}
.feedhead h2{margin:0;}
.pill{background:var(--s2);border:1px solid var(--line);color:var(--muted);padding:3px 12px;border-radius:999px;font-size:.78rem;font-weight:700;}
.hs{--dur:5s;position:relative;border-radius:0;overflow:hidden;background:transparent;border:0;outline:none;}
.hs:focus-visible{outline:2px solid var(--brand);outline-offset:4px;}
.hs-stage{position:relative;height:clamp(340px,58vw,600px);background:transparent;}
.hs-slide{position:absolute;inset:0;margin:0;opacity:0;visibility:hidden;transition:opacity .5s ease,visibility .5s;}
.hs-slide.active{opacity:1;visibility:visible;}
.hs-bg{display:none;}
.hs-slide img{position:relative;display:block;width:100%;height:100%;object-fit:contain;transform:none;transition:none;cursor:zoom-in;}
.hs-slide.active img{transform:none;}
.hs-shade{position:absolute;inset:0;pointer-events:none;background:linear-gradient(180deg,transparent 55%,rgba(3,10,16,.78) 100%);}
.hs-count{position:absolute;top:12px;left:12px;background:rgba(3,10,16,.72);border:0;color:#fff;padding:5px 10px;border-radius:999px;font-size:.78rem;font-weight:700;}
.hs-del{position:absolute;top:10px;right:10px;margin:0;}
.hs figcaption{position:absolute;left:0;right:0;bottom:0;padding:18px 20px 28px;display:flex;flex-direction:column;gap:4px;pointer-events:none;}
.hs figcaption small{color:var(--brand);font-weight:700;letter-spacing:.5px;text-transform:uppercase;font-size:.72rem;}
.hs figcaption strong{font-size:1.12rem;line-height:1.3;color:#fff;text-shadow:0 2px 10px rgba(0,0,0,.75);}
.hs-nav{position:absolute;top:50%;transform:translateY(-50%);width:40px;height:40px;border-radius:50%;border:0;background:rgba(3,10,16,.42);color:#fff;font-size:1.6rem;line-height:1;cursor:pointer;backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);}
.hs-nav:hover{background:rgba(31,214,164,.5);}
.hs-nav.prev{left:10px;} .hs-nav.next{right:10px;}
.hs-bars{position:absolute;left:16px;right:16px;bottom:10px;display:flex;gap:5px;}
.hs-bars i{flex:1;height:3px;border-radius:99px;background:rgba(255,255,255,.35);overflow:hidden;cursor:pointer;}
.hs-bars b{display:block;height:100%;width:0;background:var(--brand);}
.hs-bars i.done b{width:100%;}
.hs-bars i.active b{animation:hsfill var(--dur) linear forwards;}
.hs.paused .hs-bars i.active b{animation-play-state:paused;}
@keyframes hsfill{from{width:0;}to{width:100%;}}
.hs-empty{padding:34px 16px;text-align:center;color:var(--muted);background:transparent;border:0;border-radius:0;}

.hs-stage{position:relative;height:clamp(320px,55vw,560px);}
.hs-slide{position:absolute;inset:0;margin:0;opacity:0;visibility:hidden;transition:opacity .7s ease,visibility .7s;}
.hs-slide.active{opacity:1;visibility:visible;}
.hs-bg{position:absolute;inset:-24px;background-size:cover;background-position:center;filter:blur(24px) brightness(.5) saturate(1.25);}
.hs-slide img{position:relative;display:block;width:100%;height:100%;object-fit:contain;transform:scale(1);transition:transform 7s ease-out;}
.hs-slide.active img{transform:scale(1.05);}
.hs-shade{position:absolute;inset:0;pointer-events:none;background:linear-gradient(180deg,rgba(3,10,16,.55) 0%,transparent 26%,transparent 50%,rgba(3,10,16,.9) 100%);}
.hs-count{position:absolute;top:12px;left:12px;background:rgba(3,10,16,.62);border:1px solid rgba(255,255,255,.2);color:#fff;padding:3px 11px;border-radius:999px;font-size:.78rem;font-weight:700;}
.hs-del{position:absolute;top:10px;right:10px;margin:0;}
.hs figcaption{position:absolute;left:0;right:0;bottom:0;padding:16px 20px 30px;display:flex;flex-direction:column;gap:4px;pointer-events:none;}
.hs figcaption small{color:var(--brand);font-weight:700;letter-spacing:.5px;text-transform:uppercase;font-size:.72rem;}
.hs figcaption strong{font-size:1.2rem;line-height:1.3;color:#fff;text-shadow:0 2px 12px rgba(0,0,0,.7);}
.hs-nav{position:absolute;top:calc(clamp(260px,52vw,480px) / 2 - 20px);width:40px;height:40px;border-radius:50%;border:1px solid rgba(255,255,255,.28);background:rgba(3,10,16,.55);color:#fff;font-size:1.6rem;line-height:1;cursor:pointer;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);}
.hs-nav:hover{background:rgba(31,214,164,.5);}
.hs-nav.prev{left:10px;} .hs-nav.next{right:10px;}
.hs-bars{position:absolute;left:16px;right:16px;bottom:12px;display:flex;gap:6px;}
.hs-bars i{flex:1;height:4px;border-radius:99px;background:rgba(255,255,255,.28);overflow:hidden;cursor:pointer;}
.hs-bars b{display:block;height:100%;width:0;background:var(--brand);}
.hs-bars i.done b{width:100%;}
.hs-bars i.active b{animation:hsfill var(--dur) linear forwards;}
.hs.paused .hs-bars i.active b{animation-play-state:paused;}
@keyframes hsfill{from{width:0;}to{width:100%;}}
.hs-empty{padding:34px 16px;text-align:center;color:var(--muted);background:var(--s2);border:1px dashed var(--line);border-radius:14px;}
input[type=file]{padding:10px;}

/* ---------- compact dashboard check-in ---------- */
.dashboard-checkin{padding:16px 18px;}
.dashboard-checkin h2{margin-bottom:10px;}
.dashboard-checkin .dots{margin:8px 0 10px;}
.dashboard-checkin .reward-line{margin:0 0 10px;font-size:.95rem;}
.dashboard-checkin form{margin:0;}
.dashboard-checkin button.primary{
  margin-top:4px;
  padding:9px 16px;
  min-height:40px;
  border-radius:11px;
  font-size:.9rem;
  width:auto;
}
.hero{display:flex;flex-direction:column;}
.hero .actions{margin-top:auto;}
.hs-slide{cursor:zoom-in;}
.hs-hint{margin:10px 0 0;}
.moneywrap{display:flex;align-items:center;gap:28px;flex-wrap:wrap;}
.moneywrap .donutwrap{margin:4px 0;flex:none;}
.moneyside{flex:1;min-width:240px;}
.lb{display:none;position:fixed;inset:0;z-index:100;flex-direction:column;background:rgba(2,7,11,.95);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);outline:none;}
.lb.open{display:flex;}
.lb-track{flex:1;display:flex;overflow-x:auto;overflow-y:hidden;scroll-snap-type:x mandatory;scrollbar-width:none;-webkit-overflow-scrolling:touch;}
.lb-track::-webkit-scrollbar{display:none;}
.lb-item{flex:0 0 100%;min-width:0;margin:0;scroll-snap-align:center;scroll-snap-stop:always;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;padding:64px 16px 56px;}
.lb-item img{max-width:100%;max-height:calc(100vh - 240px);max-height:calc(100dvh - 240px);object-fit:contain;border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,.6);}
.lb-item figcaption{max-width:720px;text-align:center;display:flex;flex-direction:column;gap:6px;}
.lb-item figcaption small{color:var(--brand);font-weight:700;letter-spacing:.5px;text-transform:uppercase;font-size:.72rem;}
.lb-item figcaption strong{font-size:1.1rem;line-height:1.4;color:#fff;}
.lb-x{position:absolute;top:14px;right:14px;z-index:2;width:42px;height:42px;border-radius:50%;border:1px solid rgba(255,255,255,.3);background:rgba(255,255,255,.1);color:#fff;font-size:1.6rem;line-height:1;cursor:pointer;}
.lb-count{position:absolute;top:22px;left:50%;transform:translateX(-50%);z-index:2;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);color:#fff;padding:3px 12px;border-radius:999px;font-size:.8rem;font-weight:700;}
.lb-nav{position:absolute;top:50%;margin-top:-22px;z-index:2;width:44px;height:44px;border-radius:50%;border:1px solid rgba(255,255,255,.28);background:rgba(255,255,255,.1);color:#fff;font-size:1.7rem;line-height:1;cursor:pointer;}
.lb-nav:hover{background:rgba(31,214,164,.5);}
.lb-prev{left:12px;} .lb-next{right:12px;}
.lb-hint{position:absolute;left:0;right:0;bottom:16px;text-align:center;color:var(--muted);font-size:.8rem;pointer-events:none;}
@media (max-width:720px){ .lb-nav{display:none;} }

.tiers{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;}
.tier{display:flex;flex-direction:column;gap:6px;border-radius:14px;padding:16px;border:1px solid var(--line);background:var(--s2);}
.tier b{font-size:1rem;}
.tier span{color:var(--muted);font-size:.82rem;}
.tier small{color:var(--muted);font-size:.74rem;text-transform:uppercase;letter-spacing:.4px;}
.tier code{font-size:.95rem;word-break:break-all;}
.tier.on{border-color:rgba(31,214,164,.55);background:rgba(31,214,164,.10);}
.tier.lock{opacity:.55;}
.cg{position:fixed;inset:0;z-index:120;display:none;align-items:center;justify-content:center;background:rgba(2,7,11,.85);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);overflow:hidden;}
.cg.open{display:flex;}
.cg-box{position:relative;max-width:420px;margin:16px;text-align:center;padding:34px 26px;border-radius:26px;border:1px solid rgba(31,214,164,.5);background:radial-gradient(420px 200px at 50% 0%,rgba(31,214,164,.35),transparent 70%),linear-gradient(160deg,#0f766e,#0b3d49 60%,#08242f);box-shadow:0 30px 80px rgba(0,0,0,.6);animation:cgpop .5s cubic-bezier(.2,1.4,.4,1);}
.cg-box .big{font-size:3rem;line-height:1;margin-bottom:10px;}
.cg-box h2{font-size:1.6rem;margin:0 0 8px;}
.cg-box p{margin:0 0 18px;color:#d9f5ec;}
.cg i.cf{position:absolute;top:-16px;width:9px;height:14px;opacity:.9;animation:cfall linear forwards;}
@keyframes cgpop{from{transform:scale(.6);opacity:0;}to{transform:scale(1);opacity:1;}}
@keyframes cfall{to{transform:translateY(110vh) rotate(720deg);}}

/* ---------- top-up: white Top-up details tab ---------- */
.card.Top-upcard{background:#fff;color:#1b1b1b;border:1px solid #dcdfe2;overflow:visible;box-shadow:0 10px 40px rgba(0,0,0,.35);}
.sc-head{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:16px;}
.sc-flag{height:34px;width:auto;max-width:64px;object-fit:contain;border-radius:4px;border:1px solid #d5d8db;background:#fff;}
.sc-title h2{margin:0;color:#111;font-size:1.2rem;}
.sc-title small{color:#666;}
.sc-timer{margin-left:auto;text-align:right;}
.sc-timer small{display:block;color:#666;font-size:.74rem;}
.sc-timer b{display:block;font-size:1.6rem;color:#c40000;letter-spacing:.5px;}
.sc-msg{color:#d00000;font-weight:700;background:#fff5f5;border:1px solid #f1b5b5;border-radius:12px;padding:12px 14px;margin-bottom:16px;white-space:pre-wrap;}
.sc-sec{margin:14px 0 8px;font-size:.78rem;font-weight:800;letter-spacing:.5px;text-transform:uppercase;color:#555;}
.sc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px;}
.sc-row{display:flex;align-items:center;justify-content:space-between;gap:10px;background:#f5f6f7;border:1px solid #e3e5e8;border-radius:12px;padding:10px 12px;}
.sc-row div{min-width:0;}
.sc-row small{display:block;color:#666;font-size:.74rem;}
.sc-row b{display:block;color:#111;word-break:break-word;}
.sc-copy{flex:none;background:#fff;color:#111;border:1px solid #c9cdd1;border-radius:9px;padding:6px 12px;font-family:inherit;font-weight:700;font-size:.8rem;cursor:pointer;}
.sc-copy:hover{background:#111;color:#fff;}
.sc-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;}
.sc-actions .sc-copy{padding:9px 16px;font-size:.88rem;}
.sc-note{color:#666;font-size:.82rem;margin:12px 0 0;}

/* ---------- login / register ---------- */
.auth{max-width:440px;margin:30px auto 0;}
.auth .logo{width:54px;height:54px;border-radius:17px;font-size:1.4rem;margin-bottom:16px;}

/* ---------- bottom tab bar (phones) ---------- */
.tabbar{display:none;}

@media (max-width:860px){
  .dash{grid-template-columns:1fr;}
}
@media (max-width:720px){
  .hs-stage{height:clamp(300px,82vw,500px);}
  .dashboard-checkin{padding:14px 16px;}
  .dashboard-checkin button.primary{width:auto;min-width:150px;}
  .tabs,.uname{display:none;}
  body{padding-bottom:calc(84px + env(safe-area-inset-bottom,0px));}
  .balance{font-size:2.2rem;}
  .actions{grid-template-columns:repeat(3,1fr);row-gap:14px;}
  .tabbar{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:40;justify-content:space-around;
    padding:8px 6px calc(8px + env(safe-area-inset-bottom,0px));
    background:rgba(8,18,27,.94);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-top:1px solid var(--line);}
  .tabbar a{display:flex;flex-direction:column;align-items:center;gap:3px;flex:1;padding:6px 0;color:var(--muted);text-decoration:none;font-size:.7rem;font-weight:600;}
  .tabbar a .ic{width:22px;height:22px;}
  .tabbar a.active{color:var(--brand);}
  .card{padding:18px;}
}
@media (prefers-reduced-motion:reduce){
  .track{animation:none;}
  .hs-slide img,.hs-slide.active img{transition:none;transform:none;}
  button.tile.pulse{animation:none;}
  .cg-box{animation:none;}
  .fp,.actions.reveal{animation:none;}
  .cg i.cf{display:none;}
  .ticker{overflow-x:auto;}
}

/* ---------- full-bleed community posts ---------- */
.posts-section{
  position:relative; width:100%; margin:0 0 22px; padding:0;
  border:0; border-radius:0; background:transparent; box-shadow:none;
  overflow:visible;
}
.posts-section .feedhead{padding:0 2px 12px;margin:0;}
.posts-section .feedhead h2{font-size:1.25rem;letter-spacing:-.3px;}
.posts-section .pill{background:rgba(31,214,164,.1);border:0;color:var(--brand);}
.posts-section .hs{border:0;border-radius:0;background:transparent;box-shadow:none;}
.posts-section .hs-stage{height:clamp(300px,66vw,690px);background:transparent;}
.posts-section .hs-slide{border:0;border-radius:0;overflow:hidden;background:transparent;}
.posts-section .hs-slide img{width:100%;height:100%;object-fit:contain;object-position:center;display:block;border:0;border-radius:0;box-shadow:none;}
.posts-section .hs-shade{background:linear-gradient(180deg,rgba(2,7,11,.24) 0%,transparent 28%,rgba(2,7,11,.12) 48%,rgba(2,7,11,.88) 100%);}
.posts-section .hs-count{background:rgba(3,10,16,.72);border:0;backdrop-filter:blur(8px);}
.posts-section .hs figcaption{padding:54px 22px 34px;gap:6px;}
.posts-section .post-label{align-self:flex-start;display:inline-flex;align-items:center;gap:7px;margin-bottom:2px;padding:5px 10px;border-radius:999px;background:var(--brand);color:#031016;font-size:.68rem;font-weight:800;letter-spacing:1.2px;}
.posts-section .post-label:before{content:"";width:6px;height:6px;border-radius:50%;background:#031016;}
.posts-section .hs figcaption small{color:#8ff5d8;font-size:.72rem;letter-spacing:1px;}
.posts-section .hs figcaption strong{max-width:760px;font-size:clamp(1.05rem,2.4vw,1.65rem);line-height:1.3;font-weight:800;text-wrap:pretty;}
.posts-section .hs-nav{width:44px;height:44px;background:rgba(3,10,16,.56);border:1px solid rgba(255,255,255,.18);}
.posts-section .hs-bars{left:0;right:0;bottom:0;}
.posts-section .hs-hint{margin:10px 2px 0;font-size:.78rem;}
.posts-section .hs-empty{padding:48px 16px;text-align:center;border:1px dashed var(--line);border-radius:16px;background:rgba(11,23,33,.35);}
.posts-section form[enctype="multipart/form-data"]{max-width:620px;margin:22px 0 0!important;padding:18px 0 0;border:0;border-top:1px solid var(--line);border-radius:0;background:transparent;}
.lb-item img{border-radius:0!important;box-shadow:none!important;}
@media(max-width:600px){
  .posts-section .hs-stage{height:clamp(300px,100vw,520px);}
  .posts-section .hs figcaption{padding:48px 15px 28px;}
  .posts-section .hs-nav{width:36px;height:36px;}
}

</style>
</head>
<body>
<div class="ticker" aria-label="Daily rewards by level"><div class="track">
{% for k in range(2) %}
<span class="tk">{{ welcome_text|e }}</span>
{% for l in LEVELS %}<span class="tk">Level {{ l }} <b>{{ daily_reward(l) }}/day</b></span>{% endfor %}
{% endfor %}
</div></div>

{% set ep = request.endpoint %}
<header class="top">
<nav>
<a class="brand" href="{{ url_for('dashboard') if me else url_for('login') }}"><span class="logo">V</span><span>Vedo Invest</span></a>
{% if me %}
<div class="tabs">
<a class="{{ 'active' if ep in ['withdraw','topup','upgrade'] }}" href="{{ url_for('withdraw') }}">{{ icon('withdraw') }}Withdraw</a>
<a class="{{ 'active' if ep in ['team','pots'] }}" href="{{ url_for('team') }}">{{ icon('team') }}My Team</a>
<a class="{{ 'active' if ep in ['events_home','event_detail'] }}" href="{{ url_for('events_home') }}">Events</a>
<a class="{{ 'active' if ep in ['profile'] }}" href="{{ url_for('profile') }}">{{ icon('profile') }}Profile</a>
{% if me['is_admin'] %}<a class="{{ 'active' if ep in ['admin','admin_user_profile','admin_events'] }}" href="{{ url_for('admin') }}">{{ icon('admin') }}Admin</a><a href="{{ url_for('admin_events') }}">Event Control</a>{% endif %}
</div>
<form method="post" action="{{ url_for('logout') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button>Log out<span class="uname"> ({{ me['username'] }})</span></button>
</form>
{% else %}
<a class="auth-link" href="{{ url_for('login') }}">Log in</a>
<a class="auth-link" href="{{ url_for('register') }}">Create account</a>
{% endif %}
</nav>
</header>

{% if me and ep in ['withdraw','topup','upgrade'] %}
<div class="subnav"><div class="seg">
<a class="{{ 'active' if ep=='withdraw' }}" href="{{ url_for('withdraw') }}">Withdraw</a>
<a class="{{ 'active' if ep=='topup' }}" href="{{ url_for('topup') }}">Top up</a>
<a class="{{ 'active' if ep=='upgrade' }}" href="{{ url_for('upgrade') }}">Upgrade</a>
</div></div>
{% elif me and ep in ['team','pots'] %}
<div class="subnav"><div class="seg">
<a class="{{ 'active' if ep=='team' }}" href="{{ url_for('team') }}">My Team</a>
<a class="{{ 'active' if ep=='pots' }}" href="{{ url_for('pots') }}">Savings pots</a>
</div></div>
{% endif %}

<main>
{% with msgs = get_flashed_messages(with_categories=true) %}
{% for cat, m in msgs %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}
{% endwith %}
{% block body %}{% endblock %}
</main>

{% if me %}
<div class="tabbar">
<a class="{{ 'active' if ep=='dashboard' }}" href="{{ url_for('dashboard') }}">{{ icon('home') }}Home</a>
<a class="{{ 'active' if ep in ['withdraw','topup','upgrade'] }}" href="{{ url_for('withdraw') }}">{{ icon('withdraw') }}Withdraw</a>
<a class="{{ 'active' if ep in ['team','pots'] }}" href="{{ url_for('team') }}">{{ icon('team') }}My Team</a>
<a class="{{ 'active' if ep=='profile' }}" href="{{ url_for('profile') }}">{{ icon('profile') }}Profile</a>
{% if me['is_admin'] %}<a class="{{ 'active' if ep in ['admin','admin_user_profile'] }}" href="{{ url_for('admin') }}">{{ icon('admin') }}Admin</a>{% endif %}
</div>
{% endif %}
</body>
</html>
"""

LOGIN_BODY = """
<div class="auth">
<span class="logo">V</span>
<div class="card">
<h1>Welcome back</h1>
<p class="muted">Log in with your verified cellphone number.</p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Cellphone number</label>
<input name="username" type="text" inputmode="tel" autocomplete="username" placeholder="e.g. 082 123 4567" required autofocus>
<label>Password</label>
<input name="password" type="password" autocomplete="current-password" required>
<button class="primary" style="width:100%">Log in</button>
</form>
<p class="muted" style="margin-top:16px">No account? <a href="{{ url_for('register') }}">Create one</a>.</p>
<p class="muted small">If password contact our support team.</p>
</div>
</div>
"""

VERIFY_BODY = """
<div class="auth">
<span class="logo">V</span>
<div class="card">
<h1>Verify your number</h1>
<p class="muted">Type the 6-digit code shown below for <strong>{{ masked }}</strong>. It is submitted automatically after the 6th digit.</p>
<div id="codebox" aria-label="Your verification code" style="font-size:2.4rem;font-weight:800;letter-spacing:.35em;text-align:center;padding:16px 8px 16px 20px;margin:14px 0 4px;background:var(--s2);border:1px solid var(--line);border-radius:16px;color:var(--brand)">{{ code_shown or '------' }}</div>
<form method="post" action="{{ url_for('verify') }}" id="vform">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label for="code">6-digit code</label>
<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code"
 pattern="[0-9]{6}" minlength="6" maxlength="6" placeholder="------" required autofocus
 style="font-size:1.8rem;letter-spacing:.5em;text-align:center;font-weight:700">
<button class="primary" style="width:100%" id="vbtn">Verify and continue</button>
</form>
<form method="post" action="{{ url_for('verify_resend') }}" id="rform" style="margin-top:12px">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button class="ghost" style="width:100%" id="rbtn" disabled>Resend code</button>
</form>
<div id="rmsg" class="small muted" style="margin-top:10px" aria-live="polite"></div>
<p class="muted small" style="margin-top:14px">The code expires in 10 minutes. <a href="{{ url_for('login') }}">Back to login</a></p>
</div>
</div>
<script>
(function () {
  var code = document.getElementById('code');
  var vform = document.getElementById('vform');
  var vbtn = document.getElementById('vbtn');
  var rform = document.getElementById('rform');
  var rbtn = document.getElementById('rbtn');
  var rmsg = document.getElementById('rmsg');
  var left = {{ resend_in|default(0)|int }};
  var timer = null, sent = false;

  function tick() {
    if (left > 0) {
      rbtn.disabled = true;
      rbtn.textContent = 'Resend code in ' + left + 's';
      left--;
      timer = setTimeout(tick, 1000);
    } else {
      rbtn.disabled = false;
      rbtn.textContent = 'Resend code';
    }
  }
  function startTimer(n) { clearTimeout(timer); left = n; tick(); }

  code.addEventListener('input', function () {
    code.value = code.value.replace(/\\D/g, '').slice(0, 6);
    if (code.value.length === 6 && !sent) {
      sent = true;
      vbtn.disabled = true;
      vbtn.textContent = 'Verifying...';
      vform.submit();
    }
  });

  rform.addEventListener('submit', function (e) {
    e.preventDefault();
    if (rbtn.disabled) { return; }
    rbtn.disabled = true;
    rbtn.textContent = 'Sending...';
    fetch(rform.action, {
      method: 'POST', body: new FormData(rform), credentials: 'same-origin',
      headers: { 'X-Requested-With': 'fetch' }
    }).then(function (r) { return r.json(); }).then(function (r) {
      if (r.redirect) { window.location = r.redirect; return; }
      if (r.code) { document.getElementById('codebox').textContent = r.code; }
      rmsg.textContent = r.message || '';
      rmsg.style.color = r.ok ? 'var(--up)' : 'var(--down)';
      startTimer(r.wait || 0);
      code.value = ''; code.focus();
    }).catch(function () {
      rmsg.textContent = 'Network problem. Please try again.';
      rmsg.style.color = 'var(--down)';
      startTimer(0);
    });
  });

  tick();
})();
</script>
"""

REGISTER_BODY = """
<div class="auth">
<span class="logo">V</span>
<div class="card">
<h1>Create account</h1>
<p class="muted">You start at Level 0 top up after logged in.</p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>BA_A Number (11 digits)</label>
<input name="serial_number" value="{{ form.get('serial_number','') }}" inputmode="numeric" pattern="[0-9]{11}" minlength="11" maxlength="11" placeholder="11 digits" autocomplete="off" required autofocus>
<label>South African cellphone number (this will be your login)</label>
<input type="tel" name="phone" value="{{ form.get('phone','') }}" inputmode="tel" autocomplete="tel" maxlength="20" placeholder="e.g. 082 123 4567" required>
<p class="muted small" style="margin-top:6px">A 6-digit code will be shown on the next page to verify this number.</p>
<label>Password (minimum 8 characters)</label>
<input name="password" type="password" autocomplete="new-password" required>
<label>Confirm password</label>
<input name="confirm" type="password" autocomplete="new-password" required>
<label>Referral code (optional)</label>
<input name="referral" value="{{ form.get('referral','') }}" placeholder="e.g. REF-ABC234">
<label>Level code  (optional)</label>
<input name="level_code" value="{{ form.get('level_code','') }}" placeholder="e.g. S1-ABCD-EFGH" autocomplete="off">
<button class="primary" style="width:100%">Create account</button>
</form>
<p class="muted" style="margin-top:16px">Already a member? <a href="{{ url_for('login') }}">Log in</a></p>
</div>
</div>
"""

DASH_BODY = """
{% set total = user['points'] + locked %}
{% set pct = (user['points'] / total * 100) if total else 0 %}
{% set sf = STREAK_DAYS if (user['streak'] > 0 and user['streak'] % STREAK_DAYS == 0) else user['streak'] % STREAK_DAYS %}
{% set next_cost = UPGRADE_COST_CENTS[user['level'] + 1] if user['level'] < 7 else 0 %}
{% set lvl_pct = ([(user['points'] / next_cost * 100)|int, 100]|min) if next_cost else 100 %}

<div class="dash">
<section class="hero">
<div class="hero-top">
<div>
<p class="hello">Hi, {{ user['username'] }}</p>
<p class="lbl">Available balance</p>
<div class="balance">{{ money(user['points']) }}</div>
</div>
<div class="fpwrap">
<button type="button" class="fp" id="fp" aria-expanded="false" aria-controls="heroactions" aria-label="Open menu">{{ icon('fingerprint') }}</button>
<span class="fpcap" id="fpcap">Tap to open menu</span>
</div>
</div>
<div class="chips">
<span class="chip">Level {{ user['level'] }}</span>
<span class="chip">{{ user['streak'] }} day streak</span>
<span class="chip">{{ money(locked) }} in pots</span>
</div>
<div class="actions closed" id="heroactions">
<a class="act" href="{{ url_for('withdraw') }}"><span>{{ icon('withdraw') }}</span>Withdraw</a>
<a class="act" href="{{ url_for('topup') }}"><span>{{ icon('topup') }}</span>Top up</a>
<a class="act" href="{{ url_for('upgrade') }}"><span>{{ icon('upgrade') }}</span>Upgrade</a>
<a class="act" href="{{ url_for('pots') }}"><span>{{ icon('pots') }}</span>Pots</a>
<a class="act" href="{{ url_for('team') }}"><span>{{ icon('team') }}</span>My Team</a>
<a class="act" href="{{ url_for('profile') }}"><span>{{ icon('profile') }}</span>Profile</a>
</div>
</section>

<section class="posts-section" aria-label="Latest community posts">
<div class="feedhead">
<h2>Latest updates</h2>
{% if posts %}<span class="pill">{{ posts|length }} post{{ 's' if posts|length > 1 }}</span>{% endif %}
</div>
{% if posts %}
<div class="hs" id="hs" tabindex="0" aria-roledescription="carousel" aria-label="Posts from the admin">
<div class="hs-stage">
{% for p in posts %}
<figure class="hs-slide{{ ' active' if loop.first }}">
<div class="hs-bg" style="background-image:url('{{ url_for('media', filename=p['image']) }}')"></div>
<img src="{{ url_for('media', filename=p['image']) }}" alt="{{ p['caption'] }}">
<div class="hs-shade"></div>
<span class="hs-count">{{ loop.index }} / {{ posts|length }}</span>
{% if me['is_admin'] %}
<form class="hs-del" method="post" action="{{ url_for('admin_post_delete', post_id=p['id']) }}" onsubmit="return confirm('Delete this post?');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button class="danger">Delete</button>
</form>
{% endif %}
<figcaption>
<span class="post-label">VEDO COMMUNITY</span>
<small>{{ p['created_at'][:10] }}</small>
{% if p['caption'] %}<strong>{{ p['caption'] }}</strong>{% else %}<strong>Latest update</strong>{% endif %}
</figcaption>
</figure>
{% endfor %}
</div>
{% if posts|length > 1 %}
<button type="button" class="hs-nav prev" aria-label="Previous post">&#8249;</button>
<button type="button" class="hs-nav next" aria-label="Next post">&#8250;</button>
<div class="hs-bars">{% for p in posts %}<i><b></b></i>{% endfor %}</div>
{% endif %}
</div>
<p class="muted small hs-hint">Tap a photo to open it full screen{% if posts|length > 1 %} and swipe through all posts{% endif %}.</p>

<div class="lb" id="lb" tabindex="-1" role="dialog" aria-modal="true" aria-label="Posts from the admin">
<button type="button" class="lb-x" aria-label="Close">&times;</button>
<span class="lb-count" id="lbcount"></span>
<div class="lb-track" id="lbtrack">
{% for p in posts %}
<figure class="lb-item">
<img src="{{ url_for('media', filename=p['image']) }}" alt="{{ p['caption'] }}" loading="lazy">
<figcaption>
<span class="post-label">VEDO COMMUNITY</span>
<small>{{ p['created_at'][:10] }}</small>
{% if p['caption'] %}<strong>{{ p['caption'] }}</strong>{% else %}<strong>Latest update</strong>{% endif %}
</figcaption>
</figure>
{% endfor %}
</div>
{% if posts|length > 1 %}
<button type="button" class="lb-nav lb-prev" aria-label="Previous post">&#8249;</button>
<button type="button" class="lb-nav lb-next" aria-label="Next post">&#8250;</button>
<div class="lb-hint">Swipe left or right</div>
{% endif %}
</div>

<script>
(function () {
  var hs = document.getElementById('hs');
  if (!hs) { return; }
  var slides = hs.querySelectorAll('.hs-slide');
  var bars = hs.querySelectorAll('.hs-bars i');
  var many = slides.length > 1;
  var i = 0;

  var lb = document.getElementById('lb');
  var track = document.getElementById('lbtrack');
  var count = document.getElementById('lbcount');
  var lbIdx = 0;

  function show(n) {
    if (!many) { return; }
    slides[i].classList.remove('active');
    i = (n + slides.length) % slides.length;
    slides[i].classList.add('active');
    bars.forEach(function (bar, k) {
      bar.classList.remove('active');
      bar.classList.toggle('done', k < i);
    });
    void hs.offsetWidth;
    bars[i].classList.add('active');
  }

  function lbGo(n, smooth) {
    lbIdx = Math.max(0, Math.min(slides.length - 1, n));
    track.scrollTo({ left: lbIdx * track.clientWidth, behavior: smooth ? 'smooth' : 'auto' });
    count.textContent = (lbIdx + 1) + ' / ' + slides.length;
  }

  function openLb(n) {
    lb.classList.add('open');
    document.body.style.overflow = 'hidden';
    hs.classList.add('paused');
    lbGo(n, false);
    lb.focus();
  }

  function closeLb() {
    lb.classList.remove('open');
    document.body.style.overflow = '';
    hs.classList.remove('paused');
    show(lbIdx);
  }

  /* open the full-screen viewer by tapping the picture */
  slides.forEach(function (s, k) {
    s.addEventListener('click', function (e) {
      if (e.target.closest('.hs-del')) { return; }
      openLb(k);
    });
  });

  /* full-screen viewer: swipe (native scroll-snap), arrows, keys, close */
  track.addEventListener('scroll', function () {
    var n = Math.round(track.scrollLeft / track.clientWidth);
    if (n !== lbIdx) {
      lbIdx = n;
      count.textContent = (lbIdx + 1) + ' / ' + slides.length;
    }
  });
  lb.querySelector('.lb-x').addEventListener('click', closeLb);
  lb.addEventListener('click', function (e) {
    if (e.target === lb || e.target.classList.contains('lb-item')) { closeLb(); }
  });
  var lbPrev = lb.querySelector('.lb-prev');
  var lbNext = lb.querySelector('.lb-next');
  if (lbPrev) { lbPrev.addEventListener('click', function () { lbGo(lbIdx - 1, true); }); }
  if (lbNext) { lbNext.addEventListener('click', function () { lbGo(lbIdx + 1, true); }); }
  document.addEventListener('keydown', function (e) {
    if (!lb.classList.contains('open')) { return; }
    if (e.key === 'Escape') { closeLb(); }
    if (e.key === 'ArrowLeft') { lbGo(lbIdx - 1, true); }
    if (e.key === 'ArrowRight') { lbGo(lbIdx + 1, true); }
  });

  if (!many) { return; }

  /* auto-sliding showcase on the dashboard */
  hs.addEventListener('animationend', function (e) {
    if (e.animationName === 'hsfill') { show(i + 1); }
  });
  hs.querySelector('.prev').addEventListener('click', function () { show(i - 1); });
  hs.querySelector('.next').addEventListener('click', function () { show(i + 1); });
  bars.forEach(function (bar, k) { bar.addEventListener('click', function () { show(k); }); });

  hs.addEventListener('mouseenter', function () { hs.classList.add('paused'); });
  hs.addEventListener('mouseleave', function () {
    if (!lb.classList.contains('open')) { hs.classList.remove('paused'); }
  });
  hs.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowLeft') { show(i - 1); }
    if (e.key === 'ArrowRight') { show(i + 1); }
  });

  var x0 = null;
  hs.addEventListener('touchstart', function (e) { x0 = e.touches[0].clientX; hs.classList.add('paused'); }, { passive: true });
  hs.addEventListener('touchend', function (e) {
    if (!lb.classList.contains('open')) { hs.classList.remove('paused'); }
    if (x0 === null) { return; }
    var dx = e.changedTouches[0].clientX - x0;
    x0 = null;
    if (Math.abs(dx) > 40) { show(dx < 0 ? i + 1 : i - 1); }
  });

  show(0);
})();
</script>
{% else %}
<div class="hs-empty">Updates will show up here.</div>
{% endif %}
{% if me['is_admin'] %}
<form method="post" action="{{ url_for('admin_post') }}" enctype="multipart/form-data" style="margin-top:16px">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Post a photo (PNG, JPG, GIF or WEBP, max 30 MB)</label>
<input type="file" name="photo" accept="image/png,image/jpeg,image/gif,image/webp" required>
<label>Caption</label>
<input name="caption" maxlength="300" placeholder="Write a caption for this photo">
<button class="primary">Post photo</button>
</form>
{% endif %}
</section>


</div>

<div class="card dashboard-checkin">
<h2>Daily check-in</h2>
{% if milestone %}<div class="flash ok">&#127881; <strong>Congratulations!</strong> You are on a {{ user['streak'] }}-day streak. Amazing consistency, well done!</div>{% endif %}
<div class="dots" aria-label="Streak progress">
{% for i in range(1, STREAK_DAYS + 1) %}<i class="{{ 'on' if i <= sf }}"></i>{% endfor %}
</div>
<p class="reward-line">Today's reward: <strong>{{ money(reward) }}</strong></p>
<form method="get" action="{{ url_for('checkin') }}">
<button class="primary">{{ 'Checked in today ✓' if done else ('Continue check-in (' ~ task_done ~ '/' ~ task_total ~ ')' if task_done else 'Check in now') }}</button>
</form>
</div>

<section class="card">
<h2>Where your money is</h2>
<div class="moneywrap">
<div class="donutwrap">
<svg class="donut" viewBox="0 0 100 100" role="img" aria-label="Available versus locked balance">
<circle class="ring" cx="50" cy="50" r="40"/>
{% if total %}
<circle class="a1" cx="50" cy="50" r="40" stroke-dasharray="{{ '%.2f'|format(pct * 2.5133) }} 251.33"/>
<circle class="a2" cx="50" cy="50" r="40" stroke-dasharray="{{ '%.2f'|format((100 - pct) * 2.5133) }} 251.33" stroke-dashoffset="-{{ '%.2f'|format(pct * 2.5133) }}"/>
{% endif %}
</svg>
<div class="donut-mid"><b>{{ money(total) }}</b><span>Total</span></div>
</div>
<div class="moneyside">
<div class="legend">
<span><i style="background:var(--brand)"></i>Available {{ money(user['points']) }}</span>
<span><i style="background:var(--violet)"></i>Locked {{ money(locked) }}</span>
</div>
{% if next_cost %}
<div class="progtxt"><span>Next: Level {{ user['level'] + 1 }}</span><span>{{ lvl_pct }}% of {{ money(next_cost) }}</span></div>
<div class="bar"><b style="width:{{ lvl_pct }}%"></b></div>
{% else %}
<p class="muted small">You are at the highest level.</p>
{% endif %}
</div>
</div>
</section>

<div class="card">
<h2>Recent activity</h2>
{% if recent_tx %}
{% set txs = recent_tx|reverse|list %}
{% set mx = txs|map(attribute='A_m')|map('abs')|max %}
<div class="bars" aria-hidden="true">
{% for t in txs %}<i class="{{ 'up' if t['A_m'] > 0 else 'dn' }}" style="height:{{ [((t['A_m']|abs) / mx * 100)|round|int, 8]|max }}%"></i>{% endfor %}
</div>
{% for t in recent_tx %}
<div class="tx">
<div class="av">{{ t['kind'][:1] }}</div>
<div class="who"><b>{{ t['kind']|replace('_',' ')|title }}</b><span>{{ t['created_at'][:16] }}{% if t['note'] %} - {{ t['note'] }}{% endif %}</span></div>
<div class="amt {{ 'pos' if t['A_m'] > 0 else 'neg' }}">{{ '+' if t['A_m'] > 0 }}{{ money(t['A_m']) }}</div>
</div>
{% endfor %}
{% else %}<p class="muted">No activity yet. Check in to earn your first reward.</p>{% endif %}
</div>

<script>
(function () {
  var btn = document.getElementById('fp');
  var menu = document.getElementById('heroactions');
  var cap = document.getElementById('fpcap');
  if (!btn || !menu) { return; }
  btn.addEventListener('click', function () {
    var opening = menu.classList.contains('closed');
    menu.classList.toggle('closed', !opening);
    menu.classList.toggle('reveal', opening);
    btn.classList.toggle('open', opening);
    btn.setAttribute('aria-expanded', opening ? 'true' : 'false');
    btn.setAttribute('aria-label', opening ? 'Close menu' : 'Open menu');
    cap.textContent = opening ? 'Tap to close' : 'Tap to open menu';
  });
})();
</script>
"""

CHECKIN_BODY = """
<div class="card note">
<h1>Daily check-in</h1>
<p class="muted"><a href="{{ url_for('dashboard') }}">&larr; Back to dashboard</a></p>
{% if user['level'] == 0 %}
<p>You are at <strong>Level 0</strong>. Top up and move up to Level 1 to start earning check-in rewards.</p>
{% else %}
<p>Tap <strong>Level {{ user['level'] }}</strong>, your level, below to open today's tasks.</p>
{% endif %}
<div id="lvlmsg"></div>
<h2>Reward by level</h2>
<div class="tiles">
{% for l in LEVELS %}
<button type="button" class="tile t{{ l }}{{ ' me' if l == user['level'] }}{{ ' pulse' if l == user['level'] and not done }}" data-level="{{ l }}">
<b>Level {{ l }}{% if l == user['level'] %} - your level{% endif %}</b><strong>{{ level_reward(l) }}</strong><span>{{ daily_reward(l) }} per day</span>
</button>
{% endfor %}
</div>
</div>

{% if user['level'] >= 1 %}
<div class="card" id="taskpanel" hidden>
{% if done %}
<h2>Today's tasks</h2>
<p><strong class="pos">Today's check-in is complete.</strong> You earned {{ money(earned) }}. Come back tomorrow!</p>
{% else %}
{% set pct = (completed_n / total * 100)|int %}
<h2>Today's tasks - Level {{ user['level'] }}</h2>
<p>Finish all <strong>{{ total }}</strong> tasks to earn today's <strong>{{ money(daily) }}</strong>.
Tap to complete tasks.</p>
<p class="muted">Balance: <b id="bal">{{ money(user['points']) }}</b></p>
<div class="progtxt"><span id="progtxt">{{ completed_n }} of {{ total }} tasks done</span><span>Earned today: <b id="earned">{{ money(earned) }}</b></span></div>
<div class="bar"><b id="progbar" style="width:{{ pct }}%"></b></div>
<div id="msg" style="margin-top:14px"></div>
<div class="cg" id="cg" role="dialog" aria-modal="true" aria-label="Congratulations"><div class="cg-box"><div class="big">&#127881;</div><h2>Congratulations!</h2><p id="cgtext"></p><button type="button" class="primary" id="cgok" style="margin-top:0">Awesome!</button></div></div>
<div class="taskgrid">
{% for t in tasks %}
<button type="button" class="task{{ ' done' if t.done }}" data-label="+{{ money(t.reward) }}"
data-start="{{ url_for('checkin_start', n=t.no) }}" data-complete="{{ url_for('checkin_complete', n=t.no) }}">
<span class="fill"></span>
<span class="tl"><b>Task {{ t.no }}</b><span class="tr">{{ 'Done \u2713' if t.done else '+' ~ money(t.reward) }}</span></span>
</button>
{% endfor %}
</div>
<script>
(function () {
  var CSRF = "{{ csrf_token() }}";
  var WAIT_MS = {{ wait }} * 1000;
  var busy = false;
  var buttons = document.querySelectorAll('.task');
  var msg = document.getElementById('msg');

  function say(text, kind) {
    msg.innerHTML = '';
    if (!text) { return; }
    var d = document.createElement('div');
    d.className = 'flash ' + kind;
    d.textContent = text;
    msg.appendChild(d);
  }

  function post(url) {
    var fd = new FormData();
    fd.append('csrf_token', CSRF);
    return fetch(url, { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .catch(function () { return { ok: false, error: 'Something went wrong. Reload the page and try again.' }; });
  }

  function celebrate(streak) {
    var cg = document.getElementById('cg');
    var colors = ['#1fd6a4', '#2b7fff', '#f2b84b', '#8b7cf6', '#ff5c6c'];
    document.getElementById('cgtext').textContent = 'You checked in ' + streak + ' days in a row. That is a special achievement!';
    for (var k = 0; k < 60; k++) {
      var c = document.createElement('i');
      c.className = 'cf';
      c.style.left = (Math.random() * 100) + '%';
      c.style.background = colors[k % colors.length];
      c.style.animationDuration = (2.5 + Math.random() * 2.5) + 's';
      c.style.animationDelay = (Math.random() * 0.8) + 's';
      cg.appendChild(c);
    }
    cg.classList.add('open');
    document.getElementById('cgok').addEventListener('click', function () { window.location.reload(); });
  }

  function lockOthers(on, current) {
    buttons.forEach(function (b) {
      if (b !== current && !b.classList.contains('done')) { b.classList.toggle('locked', on); }
    });
  }

  function reset(btn) {
    var f = btn.querySelector('.fill');
    btn.classList.remove('loading');
    f.style.transition = 'none';
    f.style.width = '0%';
    btn.querySelector('.tr').textContent = btn.getAttribute('data-label');
    busy = false;
    lockOthers(false, btn);
  }

  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (busy || btn.classList.contains('done')) { return; }
      busy = true;
      lockOthers(true, btn);
      say('', 'ok');

      post(btn.getAttribute('data-start')).then(function (res) {
        if (!res.ok) { reset(btn); say(res.error, 'error'); return; }

        var f = btn.querySelector('.fill');
        btn.classList.add('loading');
        btn.querySelector('.tr').textContent = 'Loading...';
        f.style.transition = 'none';
        f.style.width = '0%';
        void f.offsetWidth;
        f.style.transition = 'width ' + (WAIT_MS / 1000) + 's linear';
        f.style.width = '100%';

        setTimeout(function () {
          post(btn.getAttribute('data-complete')).then(function (r) {
            if (!r.ok) { reset(btn); say(r.error, 'error'); return; }
            btn.classList.remove('loading');
            btn.classList.add('done');
            btn.querySelector('.tr').textContent = 'Done \u2713';
            busy = false;
            lockOthers(false, btn);
            document.getElementById('progtxt').textContent = r.completed + ' of ' + r.total + ' tasks done';
            document.getElementById('progbar').style.width = Math.round(r.completed / r.total * 100) + '%';
            document.getElementById('earned').textContent = r.earned_text;
            document.getElementById('bal').textContent = r.balance;
            var text = '+' + r.reward_text + ' added to your balance.';
            if (r.done) { text += ' All tasks done for today!'; }
            say(text, 'ok');
            if (r.done && r.milestone) { celebrate(r.streak); }
            else if (r.done) { setTimeout(function () { window.location.reload(); }, 2500); }
          });
        }, WAIT_MS + 300);
      });
    });
  });
})();
</script>
{% endif %}
</div>
{% endif %}

<script>
(function () {
  var myLevel = {{ user['level'] }};
  var panel = document.getElementById('taskpanel');
  var lm = document.getElementById('lvlmsg');

  function note(text, kind) {
    lm.innerHTML = '';
    if (!text) { return; }
    var d = document.createElement('div');
    d.className = 'flash ' + kind;
    d.textContent = text;
    lm.appendChild(d);
  }

  document.querySelectorAll('button.tile').forEach(function (t) {
    t.addEventListener('click', function () {
      var lvl = parseInt(t.getAttribute('data-level'), 10);
      if (myLevel === 0) {
        note('You are at Level 0. Top up and move up to Level 1 to start earning.', 'error');
        return;
      }
      if (lvl !== myLevel) {
        note('That is not your level. Tap Level ' + myLevel + ' to open your tasks.', 'error');
        return;
      }
      note('', 'ok');
      t.classList.remove('pulse');
      if (panel) {
        panel.hidden = false;
        panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });
})();
</script>
"""

TOPUP_BODY = """
{% if active %}
<div class="card Top-upcard" id="Top-uppanel" data-left="{{ left }}">
<div class="sc-head">
{% if details and details['flag'] %}<img class="sc-flag" src="{{ url_for('media', filename=details['flag']) }}" alt="Top-up flag">{% endif %}
<div class="sc-title">
<h2>{{ details['BA_n'] if details and details['BA_n'] else ' Top-up details' }}</h2>
<small>Top-up request #{{ active['id'] }}</small>
</div>
<div class="sc-timer"><small>Closes in</small><b id="sctimer">--:--</b></div>
</div>
{% if details and details['message'] %}<div class="sc-msg">{{ details['message'] }}</div>{% endif %}
<div class="sc-sec">Top up information</div>
<div class="sc-grid">
{% for label, value in info_rows %}
<div class="sc-row"><div><small>{{ label }}</small><b>{{ value }}</b></div><button type="button" class="sc-copy" data-copy="{{ value }}">Copy</button></div>
{% endfor %}
</div>
<div class="sc-sec">Your request</div>
<div class="sc-grid">
{% for label, value in req_rows %}
<div class="sc-row"><div><small>{{ label }}</small><b>{{ value }}</b></div><button type="button" class="sc-copy" data-copy="{{ value }}">Copy</button></div>
{% endfor %}
</div>
<div class="sc-actions"><button type="button" class="sc-copy" id="scall">Copy all</button></div>
<p class="sc-note">This tab closes by itself when the time runs out.</p>
</div>
<script>
(function () {
  var panel = document.getElementById('Top-uppanel');
  var timerEl = document.getElementById('sctimer');
  var left = parseInt(panel.getAttribute('data-left'), 10) || 0;
  function fmt(s) { var m=Math.floor(s/60), r=s%60; return (m<10?'0':'')+m+':'+(r<10?'0':'')+r; }
  function tick() { if(left<=0){panel.style.display='none';window.location.reload();return;} timerEl.textContent=fmt(left);left--;setTimeout(tick,1000); }
  tick();
  function copyText(text,btn){var old=btn.textContent;function done(){btn.textContent='Copied';setTimeout(function(){btn.textContent=old;},1500);}function fallback(){var ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();try{document.execCommand('copy');}catch(e){}document.body.removeChild(ta);done();}if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(done,fallback);}else{fallback();}}
  panel.querySelectorAll('.sc-row .sc-copy').forEach(function(b){b.addEventListener('click',function(){copyText(b.getAttribute('data-copy'),b);});});
  document.getElementById('scall').addEventListener('click',function(){var lines=[];panel.querySelectorAll('.sc-row').forEach(function(row){lines.push(row.querySelector('small').textContent+': '+row.querySelector('b').textContent);});copyText(lines.join('\\n'),this);});
})();
</script>
{% else %}
<style>
.topup-wrap{max-width:620px;margin:auto}
.level-pick{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:14px 0}
.level-pick .tile{padding:10px 7px;min-height:76px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;cursor:pointer;transition:transform .15s,border-color .15s,box-shadow .15s}
.level-pick .tile:hover{transform:translateY(-2px);border-color:var(--accent,#27d69b)}
.level-pick .tile.selected{outline:2px solid #27d69b;box-shadow:0 0 0 3px rgba(39,214,155,.13)}
.level-pick .tile b{font-size:.78rem}.level-pick .tile strong{font-size:.95rem;margin:2px 0}.level-pick .tile span{font-size:.7rem}
.join-step{margin-top:14px;padding:14px;border:1px solid var(--line);border-radius:16px;background:var(--s2)}
.join-step[hidden]{display:none}.join-level{font-size:1rem;font-weight:750;margin:0 0 6px}.level-hint{font-size:.84rem;color:var(--muted);margin:0}
@media(max-width:380px){.level-pick{gap:6px}.level-pick .tile{padding:8px 3px;min-height:70px}.level-pick .tile b{font-size:.7rem}}
</style>
<div class="card note topup-wrap">
<h1>Tap the level you want</h1>
<div class="level-pick" id="levelPick">
{% for l in LEVELS %}
<button type="button" class="tile t{{ l }}{% if user['selected_level'] == l %} selected{% endif %}" {% if l <= user['level'] %}disabled title="Already at or below your current level"{% endif %} data-level="{{ l }}" data-reward="{{ level_reward(l) }}" data-daily="{{ daily_reward(l) }}">
<b>✦ LEVEL {{ l }}</b><strong>{{ level_reward(l) }}</strong><span>{{ daily_reward(l) }} daily</span>
</button>
{% endfor %}
</div>
<section class="join-step" id="joinStep" hidden>
<p class="join-level" id="joinTitle">Choose an A_m for this level</p>
<p class="level-hint" id="joinInfo">Enter your top-up A_m then press Enter.</p>
<form id="topupA_mForm" novalidate>
<label for="topupA_m">A_m (R)</label>
<input id="topupA_m" name="A_m" type="number" inputmode="decimal" min="10" max="50000" step="0.01" placeholder="Enter A_m (R10–R50,000)" required>
<button type="submit" class="primary" id="enterTopup">Enter</button>
</form>
<p id="saveStatus" class="level-hint" role="status" aria-live="polite"></p>
</section>
</div>
<script>
(function(){
  var pick=document.getElementById('levelPick');
  var step=document.getElementById('joinStep');
  var title=document.getElementById('joinTitle');
  var info=document.getElementById('joinInfo');
  var status=document.getElementById('saveStatus');
  var form=document.getElementById('topupA_mForm');
  var A_mInput=document.getElementById('topupA_m');
  var enterButton=document.getElementById('enterTopup');
  var selectedLevel=null;
  var csrf={{ csrf_token()|tojson }};
  if(!pick || !form)return;

  pick.querySelectorAll('button[data-level]').forEach(function(btn){
    btn.addEventListener('click',function(){
      selectedLevel=btn.getAttribute('data-level');
      pick.querySelectorAll('.tile').forEach(function(tile){tile.classList.remove('selected');});
      btn.classList.add('selected');
      step.hidden=false;
      title.textContent='Level '+selectedLevel+' selected';
      info.textContent='Enter your A_m, then press Enter. The Top up details will open for 10 minutes.';
      status.textContent='';
      A_mInput.value='';
      A_mInput.focus();
      step.scrollIntoView({behavior:'smooth',block:'nearest'});
    });
  });

  form.addEventListener('submit',function(ev){
    ev.preventDefault();
    if(!selectedLevel){status.textContent='Tap a level first.';return;}
    var A_m=A_mInput.value.trim();
    var numeric=Number(A_m);
    if(!A_m || !Number.isFinite(numeric) || numeric<10 || numeric>50000){
      status.textContent='Enter an A_m from R10 to R50,000.';
      A_mInput.focus();
      return;
    }
    enterButton.disabled=true;
    status.textContent='Sending your request…';
    var fd=new FormData();
    fd.append('level',selectedLevel);
    fd.append('A_m',A_m);
    fd.append('csrf_token',csrf);
    fetch('{{ url_for("topup_select_level") }}',{method:'POST',body:fd,headers:{'X-Requested-With':'fetch'}})
      .then(function(r){return r.json().then(function(data){if(!r.ok)throw new Error(data.message||'Could not send request.');return data;});})
      .then(function(data){status.textContent=data.message||'Request sent. Opening  details…';window.location.reload();})
      .catch(function(err){status.textContent=err.message||'Could not send request. Please try again.';enterButton.disabled=false;});
  });
})();
</script>
{% endif %}
"""

WITHDRAW_BODY = """
<div class="card note">
<h1>Withdraw (money)</h1>
<p class="muted">Enter withdrawal request. Rejected requests are refunded.</p>
<p class="muted">Withdrawals R150.</p>
<p class="muted">Withdrawals R500.</p>
<p class="muted">Withdrawals R1500.</p>
<p class="muted">Withdrawals R4500.</p>
<p class="muted">Withdrawals R12000.</p>
<p class="muted">Withdrawals R25000.</p>
<p class="muted">Withdrawals R30000.</p>
<p class="muted">Withdrawals R50000.</p>
<p class="muted">Withdraws takes 24 hours.</p>
<h2>Available balance: {{ money(user['points']) }}</h2>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>A_m (R) - minimum {{ money(WITHDRAW_MIN_CENTS) }}</label>
<input name="A_m" type="number" step="0.01" min="10" placeholder="e.g. 200.00" required>
<button class="primary">Request withdrawal</button>
</form>
</div>

<div class="card">
<h2>Withdrawal history</h2>
{% if withdrawals %}
<table>
<tr><th>Date</th><th>A_m</th><th>Status</th></tr>
{% for w in withdrawals %}
<tr><td>{{ w['created_at'][:16] }}</td><td>{{ money(w['A_m']) }}</td>
<td><span class="badge {{ 'ok' if w['status']=='COMPLETED' else 'err' if w['status']=='REJECTED' else 'wait' }}">{{ w['status'] }}</span></td></tr>
{% endfor %}
</table>
{% else %}<p class="muted">No withdrawal requests yet.</p>{% endif %}
</div>
"""

TEAM_BODY = """
<div class="card">
<h2>Your referral code</h2>
<div class="refcode">
<code>{{ user['referral_code'] }}</code>
<button type="button" class="ghost" onclick="navigator.clipboard.writeText('{{ user['referral_code'] }}');this.textContent='Copied';">Copy</button>
</div>
<h2 style="margin-top:14px">Your referral link</h2>
<div class="refcode">
<code id="reflink" data-path="{{ url_for('register', ref=user['referral_code']) }}" style="font-size:.95rem;word-break:break-all">{{ url_for('register', ref=user['referral_code']) }}</code>
<button type="button" class="ghost" onclick="navigator.clipboard.writeText(document.getElementById('reflink').textContent);this.textContent='Copied';">Copy link</button>
</div>
<script>
(function(){
  var el = document.getElementById('reflink');
  el.textContent = window.location.origin + el.getAttribute('data-path');
})();
</script>
<p class="muted">Anyone who opens your link gets your code filled in automatically and joins your team.</p>
{% if referrer %}<p>You joined <b>{{ mask_phone(referrer['username']) }}</b>'s team.</p>{% endif %}
</div>

<div class="card">
<h2>Team levels{% if team_name %} - you are <span class="pos">{{ team_name }}</span>{% endif %}</h2>
{% if next_tier %}
<p class="muted">{{ members|length }} of {{ next_tier['size'] }} members. Reach {{ next_tier['size'] }} members to unlock <b>{{ next_tier['name'] }}</b> and its team account number.</p>
<div class="bar" style="margin-bottom:14px"><b style="width:{{ tier_pct }}%"></b></div>
{% else %}<p class="muted">Your team reached the top level.</p>{% endif %}
<div class="tiers">
{% for t in tiers %}
<div class="tier {{ 'on' if t['number'] else 'lock' }}">
<b>{{ t['name'] }}</b>
<span>{{ t['size'] }} members</span>
{% if t['number'] %}
<small>Team account number</small>
<code>{{ t['number'] }}</code>
<button type="button" class="ghost" onclick="navigator.clipboard.writeText('{{ t['number'] }}');this.textContent='Copied';">Copy</button>
{% else %}
<small>Locked - reach {{ t['size'] }} members</small>
{% endif %}
</div>
{% endfor %}
</div>
</div>

<div class="card">
<h2>Team members ({{ members|length }})</h2>
<div class="stats" style="margin-bottom:12px">
<div class="stat"><b>{{ members|length }}</b><span>Members</span></div>
<div class="stat"><b>{{ money(team_points) }}</b><span>Team total points</span></div>
<div class="stat"><b class="pos">{{ money(earned) }}</b><span>Referral rewards earned</span></div>
</div>
{% if members %}
<table>
<tr><th>Member</th><th>Level</th><th>Points</th><th>Streak</th><th>Joined</th></tr>
{% for m in members %}
<tr><td>{{ mask_phone(m['username']) }}</td>
<td><span class="badge">L{{ m['level'] }}</span></td>
<td>{{ money(m['points']) }}</td>
<td>{{ m['streak'] }}</td>
<td>{{ m['created_at'][:10] }}</td></tr>
{% endfor %}
</table>
{% else %}<p class="muted">No team members yet. Share your code!</p>{% endif %}
</div>
"""

UPGRADE_BODY = """
<div class="card note">
<h1>Level upgrade</h1>
<p>You are <strong>Level {{ user['level'] }}</strong>. Balance: <strong>{{ money(user['points']) }}</strong></p>
{% if next_level %}
{% set cost = UPGRADE_COST_CENTS[next_level] %}
{% set pct = [(user['points'] / cost * 100)|int, 100]|min %}
<p>Next: <strong>Level {{ next_level }}</strong> costs <strong>{{ money(cost) }}</strong>
(daily check-in becomes {{ daily_reward(next_level) }}).</p>
<div class="progtxt"><span>Your balance</span><span>{{ pct }}%</span></div>
<div class="bar"><b style="width:{{ pct }}%"></b></div>
<form method="post" onsubmit="return confirm('Upgrade to Level {{ next_level }}?');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button class="primary" {% if user['points'] < cost %}disabled{% endif %}>
Upgrade to Level {{ next_level }}</button>
</form>
{% if user['points'] < cost %}
<p class="muted small" style="margin-top:10px">Not enough balance. Keep checking in to earn more, or <a href="{{ url_for('topup') }}">request a top-up</a>.</p>
{% endif %}
{% else %}
<p class="muted">You are already at the highest level.</p>
{% endif %}
</div>

<div class="card">
<h2>Redeem a level code</h2>
<p class="muted">Enter a level code from the admin to move to that level.</p>
<form method="post" action="{{ url_for('redeem') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Level code</label>
<input name="voucher" placeholder="e.g. S3-ABCD-EFGH" autocomplete="off" required>
<button class="primary">Redeem</button>
</form>
</div>

<div class="card">
<h2>Upgrade costs</h2>
<table>
<tr><th>Move to</th><th>Cost</th><th>Level reward</th><th>Daily check-in</th></tr>
{% for l in LEVELS %}
<tr class="{{ 'hl' if l == user['level'] + 1 }}"><td>Level {{ l }}</td><td>{{ money(UPGRADE_COST_CENTS[l]) }}</td>
<td>{{ level_reward(l) }}</td><td>{{ daily_reward(l) }}</td></tr>
{% endfor %}
</table>
</div>
"""

POTS_BODY = """
<div class="card note">
<h1>Savings pots</h1>
<p class="muted">Lock part of your balance for 1 to 9 weeks and earn <b>simple interest at the
annual rate shown below</b>. This is not a multiplier: interest = A_m x annual rate x weeks / 52.
Collect after the lock period ends. Closing a pot early returns your money but forfeits the interest.</p>
<p>Available balance: <strong>{{ money(user['points']) }}</strong></p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>A_m to lock (R) - minimum {{ money(POT_MIN_CENTS) }}</label>
<input name="A_m" type="number" step="0.01" min="10" placeholder="e.g. 1000.00" required>
<label>Lock period</label>
<select name="weeks">
{% for w in POT_WEEKS %}
<option value="{{ w }}">{{ w }} week{{ 's' if w > 1 }} - {{ POT_APR_PERCENT[w] }}% per year</option>
{% endfor %}
</select>
<button class="primary">Lock into savings pot</button>
</form>
</div>

<div class="card">
<h2>Interest rates</h2>
<table>
<tr><th>Weeks</th><th>Annual rate</th><th>Interest on R1,000.00</th></tr>
{% for w in POT_WEEKS %}
<tr><td>{{ w }}</td><td>{{ POT_APR_PERCENT[w] }}%</td><td>{{ money(pot_interest(100000, w)) }}</td></tr>
{% endfor %}
</table>
</div>

<div class="card">
<h2>My pots</h2>
{% if my_pots %}
<table>
<tr><th>#</th><th>Locked</th><th>Weeks</th><th>Rate</th><th>Interest</th><th>Matures</th><th>Status</th><th></th></tr>
{% for p in my_pots %}
<tr>
<td>{{ p['id'] }}</td><td>{{ money(p['principal']) }}</td><td>{{ p['weeks'] }}</td>
<td>{{ p['apr_percent'] }}%</td><td>{{ money(p['interest']) }}</td><td>{{ p['matures_at'][:10] }}</td>
<td><span class="badge {{ 'ok' if p['status']=='PAID' else 'err' if p['status']=='BROKEN' else 'wait' }}">{{ p['status'] }}</span></td>
<td>
{% if p['status'] == 'LOCKED' %}
{% if p['matures_at'] <= now %}
<form class="inline" method="post" action="{{ url_for('pot_collect', pot_id=p['id']) }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="success">Collect</button></form>
{% endif %}
<form class="inline" method="post" action="{{ url_for('pot_break', pot_id=p['id']) }}"
onsubmit="return confirm('Close early? You will lose the interest.');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="danger">Close early</button></form>
{% endif %}
</td>
</tr>
{% endfor %}
</table>
{% else %}<p class="muted">No savings pots yet.</p>{% endif %}
</div>
"""

PROFILE_BODY = """
<div class="card">
<div class="phead">
<div class="avatar">{{ member['username'][-2:] }}</div>
<div>
<h1>{{ 'My profile' if is_self else 'Member profile' }}</h1>
<p class="muted" style="margin:0">Details entered when the account was created.</p>
</div>
</div>
<dl class="kv">
<div><dt>Login (cellphone number)</dt><dd>{{ member['username'] }}{% if member['is_admin'] %} <span class="badge">admin</span>{% endif %}</dd></div>
<div><dt>BA_A Number</dt><dd>{{ member['serial_number'] }}</dd></div>
<div><dt>Cellphone number</dt><dd>{{ member['phone'] or '-' }}{% if member['phone'] %} <span class="badge {{ 'ok' if member['phone_verified'] else 'wait' }}">{{ 'verified' if member['phone_verified'] else 'unverified' }}</span>{% endif %}</dd></div>
<div><dt>Level</dt><dd>Level {{ member['level'] }}</dd></div>
<div><dt>Balance</dt><dd>{{ money(member['points']) }}</dd></div>
<div><dt>Referral code</dt><dd><code>{{ member['referral_code'] }}</code></dd></div>
<div><dt>Referred by</dt><dd>{{ (mask_phone(referrer['username']) if is_self else referrer['username']) if referrer else '-' }}</dd></div>
<div><dt>Day streak</dt><dd>{{ member['streak'] }}</dd></div>
<div><dt>Joined</dt><dd>{{ member['created_at'][:16] }}</dd></div>
</dl>
{% if not is_self %}<p style="margin-top:16px"><a href="{{ url_for('admin') }}">&larr; Back to admin</a></p>{% endif %}
</div>
"""

ADMIN_BODY = """
<div class="card">
<h2>Search users</h2>
<form method="get" action="{{ url_for('admin') }}" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
<input name="q" value="{{ q }}" maxlength="30" autocomplete="off" placeholder="Cellphone, BA_A Number or referral code" style="flex:1;min-width:220px">
<button class="primary" style="margin-top:0">Search</button>
{% if q %}<a href="{{ url_for('admin') }}">Clear</a>{% endif %}
</form>
{% if q %}<p class="muted small" style="margin:10px 0 0">Showing results for <b>{{ q }}</b> in members, top-ups, withdrawals, pots and the ledger.</p>{% endif %}
</div>

<div class="card">
<h2>Overview</h2>
<div class="stats">
<div class="stat"><b>{{ summary.users }}</b><span>Members</span></div>
<div class="stat"><b>{{ money(summary.balances) }}</b><span>Total member balances</span></div>
<div class="stat"><b>{{ money(summary.locked) }}</b><span>Locked in pots</span></div>
<div class="stat"><b>{{ money(summary.interest_owed) }}</b><span>Interest owed on open pots</span></div>
<div class="stat"><b>{{ summary.pending_topups }}</b><span>Pending top-ups</span></div>
<div class="stat"><b>{{ summary.pending_withdrawals }}</b><span>Pending withdrawals</span></div>
</div>
</div>

<div class="card">
<h2>Top-up details (white tab)</h2>
<p class="muted">This is what a member sees, and can copy, in the white tab that opens for 10 minutes after they tap a level on the Top up page. Only you can enter it. A member-specific entry replaces the global one for that member only. Leaving the flag empty keeps the current flag.</p>
<form method="post" action="{{ url_for('admin_topup_details') }}" enctype="multipart/form-data">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Send to</label>
<select name="scope" id="dscope">
<option value="global">Everyone (global)</option>
<option value="user">A specific member</option>
</select>
<div id="dtarget" hidden>
<label>Member ID (see the ID column in All members) or cellphone number</label>
<input name="target" id="dtarget_in" autocomplete="off" placeholder="e.g. 12 or 082 123 4567">
</div>
<label>BA_n</label>
<input name="BA_n" maxlength="100" value="{{ gd['BA_n'] if gd else '' }}">
<label>Reference</label>
<input name="Reference" maxlength="100" value="{{ gd['Reference'] if gd else '' }}">
<label>A_m</label>
<input name="A_m" maxlength="100" value="{{ gd['A_m'] if gd else '' }}">
<label>P_s</label>
<input name="P_s" maxlength="100" value="{{ gd['P_s'] if gd else '' }}">
<label>BA_A Number</label>
<input name="BA_Account_number" maxlength="100" value="{{ gd['BA_Account_number'] if gd else '' }}">
<label>Message (shown in red)</label>
<textarea name="message" rows="3" maxlength="500" placeholder="Write the message members must read">{{ gd['message'] if gd else '' }}</textarea>
<label>Small Top-up flag (PNG, JPG, GIF or WEBP, max 30 MB)</label>
<input type="file" name="flag" accept="image/png,image/jpeg,image/gif,image/webp">
{% if gd and gd['flag'] %}<p class="muted small" style="margin-top:8px">Current global flag: <img src="{{ url_for('media', filename=gd['flag']) }}" alt="flag" style="height:24px;vertical-align:middle;border-radius:3px"></p>{% endif %}
<button class="primary">Save Top-up details</button>
</form>
<script>
(function () {
  var s = document.getElementById('dscope');
  var box = document.getElementById('dtarget');
  var inp = document.getElementById('dtarget_in');
  function sync() {
    var on = s.value === 'user';
    box.hidden = !on;
    inp.required = on;
  }
  s.addEventListener('change', sync);
  sync();
})();
</script>

{% if gd or specific_details %}
<h2 style="margin-top:22px">Saved Top-up details</h2>
<table>
<tr><th>For</th><th>BA_n</th><th>Reference</th><th>A_m</th><th>P_s</th><th>Account no.</th><th>Flag</th><th>Updated</th><th></th></tr>
{% if gd %}
<tr><td><b>Everyone</b></td><td>{{ gd['BA_n'] or '-' }}</td><td>{{ gd['Reference'] or '-' }}</td><td>{{ gd['A_m'] or '-' }}</td><td>{{ gd['P_s'] or '-' }}</td><td>{{ gd['BA_Account_number'] or '-' }}</td>
<td>{% if gd['flag'] %}<img src="{{ url_for('media', filename=gd['flag']) }}" alt="flag" style="height:22px;border-radius:3px">{% else %}-{% endif %}</td>
<td>{{ gd['updated_at'][:16] }}</td>
<td><form class="inline" method="post" action="{{ url_for('admin_topup_details_delete', user_id=0) }}" onsubmit="return confirm('Remove the global Top-up details?');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="danger">Remove</button></form></td></tr>
{% endif %}
{% for s in specific_details %}
<tr><td>{{ s['username'] }} (ID {{ s['user_id'] }})</td><td>{{ s['BA_n'] or '-' }}</td><td>{{ s['Reference'] or '-' }}</td><td>{{ s['A_m'] or '-' }}</td><td>{{ s['P_s'] or '-' }}</td><td>{{ s['BA_Account_number'] or '-' }}</td>
<td>{% if s['flag'] %}<img src="{{ url_for('media', filename=s['flag']) }}" alt="flag" style="height:22px;border-radius:3px">{% else %}-{% endif %}</td>
<td>{{ s['updated_at'][:16] }}</td>
<td><form class="inline" method="post" action="{{ url_for('admin_topup_details_delete', user_id=s['user_id']) }}" onsubmit="return confirm('Remove the details for this member?');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="danger">Remove</button></form></td></tr>
{% endfor %}
</table>
{% endif %}
</div>

<div class="card">
<h2>Top-up instruction text</h2>
<p class="muted">Choose who sees this text on the Top up page. A member-specific instruction replaces the global one for that member only.</p>
<form method="post" action="{{ url_for('admin_instructions') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Send to</label>
<select name="scope" id="iscope">
<option value="global">Everyone (global message)</option>
<option value="user">A specific member</option>
</select>
<div id="itarget" hidden>
<label>Member ID (see the ID column in All members) or cellphone number</label>
<input name="target" id="itarget_in" autocomplete="off" placeholder="e.g. 12 or 082 123 4567">
</div>
<label>Instruction text</label>
<textarea name="instructions" rows="4" maxlength="1000" required>{{ instructions }}</textarea>
<button class="primary">Save instructions</button>
</form>
<script>
(function () {
  var s = document.getElementById('iscope');
  var box = document.getElementById('itarget');
  var inp = document.getElementById('itarget_in');
  function sync() {
    var on = s.value === 'user';
    box.hidden = !on;
    inp.required = on;
  }
  s.addEventListener('change', sync);
  sync();
})();
</script>

{% if specific %}
<h2 style="margin-top:22px">Member-specific instructions</h2>
<table>
<tr><th>ID</th><th>Member</th><th>Instruction</th><th>Updated</th><th></th></tr>
{% for s in specific %}
<tr><td>{{ s['user_id'] }}</td><td>{{ s['username'] }}</td>
<td class="muted" style="white-space:pre-wrap">{{ s['text'] }}</td>
<td>{{ s['updated_at'][:16] }}</td>
<td>
<form class="inline" method="post" action="{{ url_for('admin_instruction_delete', user_id=s['user_id']) }}"
onsubmit="return confirm('Remove this member instruction?');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button class="danger">Remove</button>
</form>
</td></tr>
{% endfor %}
</table>
{% endif %}
</div>

<div class="card">
<h2>Welcome text (moving banner)</h2>
<p class="muted">Shown in the moving banner at the top of every page.</p>
<form method="post" action="{{ url_for('admin_welcome') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<input name="welcome_text" value="{{ welcome_text }}" maxlength="120" required>
<button class="primary">Save welcome text</button>
</form>
</div>

<div class="card">
<h2>Top-up requests</h2>
<p class="muted small">Approving a top-up only marks it approved. It does not change the member's balance. To add money to a member's balance use <b>Add balance</b> in the All members table below.</p>
{% if topups %}
<table>
<tr><th>#</th><th>Date</th><th>User</th><th>A_m</th><th>Level</th><th>Reason</th><th>Status</th><th>Action</th></tr>
{% for t in topups %}
<tr><td>{{ t['id'] }}</td><td>{{ t['created_at'][:16] }}</td><td>{{ t['username'] }}</td><td>{{ money(t['A_m']) }}</td>
<td>{% if t['level'] %}<span class="badge">L{{ t['level'] }}</span>{% else %}-{% endif %}</td>
<td class="muted">{{ t['note'] }}</td>
<td><span class="badge {{ 'ok' if t['status']=='APPROVED' else 'err' if t['status']=='REJECTED' else 'wait' }}">{{ t['status'] }}</span></td>
<td>
{% if t['status'] == 'PENDING' %}
<form class="inline" method="post" action="{{ url_for('admin_topup', topup_id=t['id'], action='approve') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button class="success">Approve</button></form>
<form class="inline" method="post" action="{{ url_for('admin_topup', topup_id=t['id'], action='reject') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="danger">Reject</button></form>
{% endif %}
</td></tr>
{% endfor %}
</table>
{% else %}<p class="muted">No top-up requests.</p>{% endif %}
</div>

<div class="card">
<h2>Withdrawal requests</h2>
{% if withdrawals %}
<table>
<tr><th>#</th><th>Date</th><th>User</th><th>A_m</th><th>Status</th><th>Action</th></tr>
{% for w in withdrawals %}
<tr><td>{{ w['id'] }}</td><td>{{ w['created_at'][:16] }}</td><td>{{ w['username'] }}</td><td>{{ money(w['A_m']) }}</td>
<td><span class="badge {{ 'ok' if w['status']=='COMPLETED' else 'err' if w['status']=='REJECTED' else 'wait' }}">{{ w['status'] }}</span></td>
<td>
{% if w['status'] == 'PENDING' %}
<form class="inline" method="post" action="{{ url_for('admin_withdrawal', withdrawal_id=w['id'], action='complete') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="success">Approve</button></form>
<form class="inline" method="post" action="{{ url_for('admin_withdrawal', withdrawal_id=w['id'], action='reject') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="danger">Reject</button></form>
{% endif %}
</td></tr>
{% endfor %}
</table>
{% else %}<p class="muted">No withdrawal requests.</p>{% endif %}
</div>

<div class="card">
<h2>All members</h2>
<table>
<tr><th>ID</th><th>Login ID</th><th>Cellphone</th><th>Level</th><th>Selected top-up level</th><th>Balance</th><th>Streak</th><th>Referred by</th><th>Joined</th><th>Add balance</th><th>Reset password</th></tr>
{% for u in users %}
<tr><td>{{ u['id'] }}</td><td><a href="{{ url_for('admin_user_profile', user_id=u['id']) }}">{{ u['username'] }}</a>{% if u['is_admin'] %} <span class="badge">admin</span>{% endif %}</td>
<td>{{ u['phone'] or '-' }}{% if u['phone'] and not u['phone_verified'] %} <span class="badge wait">unverified</span>{% endif %}</td>
<td>L{{ u['level'] }}</td><td>{% if u['selected_level'] %}L{{ u['selected_level'] }}{% else %}-{% endif %}</td><td>{{ money(u['points']) }}</td><td>{{ u['streak'] }}</td>
<td>{{ u['referrer'] or '-' }}</td><td>{{ u['created_at'][:10] }}</td>
<td>
<form class="inline" method="post" action="{{ url_for('admin_add_balance', user_id=u['id']) }}"
onsubmit="return confirm('Add this A_m to account {{ u['username'] }}?');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<input type="hidden" name="q" value="{{ q }}">
<input class="pwbox" style="width:105px" name="A_m" type="number" step="0.01" min="0.01" placeholder="A_m (R)" required>
<input class="pwbox" name="note" maxlength="100" placeholder="Note (optional)">
<button class="success">Add</button>
</form>
</td>
<td>
<form class="inline" method="post" action="{{ url_for('admin_reset_password', user_id=u['id']) }}"
onsubmit="return confirm('Set a new password for {{ u['username'] }}?');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<input class="pwbox" name="new_password" type="text" minlength="8" placeholder="New password" autocomplete="off" required>
<button class="danger">Reset</button>
</form>
</td></tr>
{% endfor %}
</table>
{% if not users %}<p class="muted">No members match your search.</p>{% endif %}
</div>

<div class="card">
<h2>Savings pots (latest 50)</h2>
{% if pot_rows %}
<table>
<tr><th>#</th><th>User</th><th>Locked</th><th>Weeks</th><th>Rate</th><th>Interest</th><th>Matures</th><th>Status</th></tr>
{% for p in pot_rows %}
<tr><td>{{ p['id'] }}</td><td>{{ p['username'] }}</td><td>{{ money(p['principal']) }}</td><td>{{ p['weeks'] }}</td>
<td>{{ p['apr_percent'] }}%</td><td>{{ money(p['interest']) }}</td><td>{{ p['matures_at'][:10] }}</td><td>{{ p['status'] }}</td></tr>
{% endfor %}
</table>
{% else %}<p class="muted">No savings pots.</p>{% endif %}
</div>

<div class="card">
<h2>Transaction ledger (latest 60)</h2>
{% if ledger %}
<table>
<tr><th>Date</th><th>User</th><th>Type</th><th>A_m</th><th>Details</th></tr>
{% for t in ledger %}
<tr><td>{{ t['created_at'][:16] }}</td><td>{{ t['username'] }}</td><td>{{ t['kind'] }}</td>
<td class="{{ 'pos' if t['A_m'] > 0 else 'neg' }}">{{ '+' if t['A_m'] > 0 }}{{ money(t['A_m']) }}</td>
<td class="muted">{{ t['note'] }}</td></tr>
{% endfor %}
</table>
{% else %}<p class="muted">No transactions yet.</p>{% endif %}
</div>

<div class="card">
<h2>Generate level codes</h2>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Level</label>
<select name="level">
{% for l in LEVELS %}<option value="{{ l }}">Level {{ l }} - {{ level_reward(l) }}</option>{% endfor %}
</select>
<label>How many (1-100)</label>
<input name="count" type="number" min="1" max="100" value="5">
<button class="primary">Generate</button>
</form>
</div>

<div class="card">
<h2>Unused codes ({{ unused|length }})</h2>
{% if unused %}
<table>
<tr><th>Level</th><th>Code</th></tr>
{% for v in unused %}<tr><td>Level {{ v['level'] }}</td><td><code>{{ v['code'] }}</code></td></tr>{% endfor %}
</table>
{% else %}<p class="muted">None left. Generate some above.</p>{% endif %}
</div>
"""


# VEDO EVENTS: admin-controlled event configuration and server-side outcomes.
EVENT_DEFAULTS = [
    ("wheel","Spin the Wheel",100,50,10,10,0,1,2,50,200,1,
     '[{"name":"Big Prize","points":500,"weight":2,"limit":2},{"name":"Medium Prize","points":100,"weight":8,"limit":8},{"name":"Small Prize","points":25,"weight":30,"limit":30},{"name":"No Prize","points":0,"weight":60,"limit":60}]'),
    ("quiz","Educational Quiz",0,50,10,10,0,1,2,50,200,1,"[]"),
    ("referral","Referral Challenge",0,50,10,10,0,1,4,50,200,1,"[]"),
    ("savings","Savings Challenge",0,50,10,10,0,1,2,50,200,1,"[]"),
    ("voucher","Voucher Day",0,50,10,10,0,1,7,50,200,1,"[]"),
]
QUIZ_QUESTIONS = [
 {"key":"q1","category":"General Knowledge","question":"Which country is known as the Land of the Rising Sun?","answers":[("a","China"),("b","Japan"),("c","South Korea"),("d","Thailand")],"correct":"b","explanation":"Japan is commonly known as the Land of the Rising Sun."},
 {"key":"q2","category":"Science","question":"Which planet is known as the Red Planet?","answers":[("a","Earth"),("b","Mars"),("c","Venus"),("d","Jupiter")],"correct":"b","explanation":"Iron oxide on Mars gives it a reddish appearance."},
 {"key":"q3","category":"Mathematics","question":"What is 8 × 7?","answers":[("a","54"),("b","56"),("c","64"),("d","48")],"correct":"b","explanation":"Eight multiplied by seven equals 56."},
 {"key":"q4","category":"Environment","question":"Which gas do plants absorb during photosynthesis?","answers":[("a","Oxygen"),("b","Nitrogen"),("c","Carbon dioxide"),("d","Hydrogen")],"correct":"c","explanation":"Plants use carbon dioxide during photosynthesis."},
 {"key":"q5","category":"Technology","question":"What does URL stand for?","answers":[("a","Uniform Resource Locator"),("b","Universal Record Link"),("c","User Reference List"),("d","Unified Route Line")],"correct":"a","explanation":"URL stands for Uniform Resource Locator."},
]
def ensure_vedo_events():
    db=sqlite3.connect(DB_PATH,timeout=30)
    try:
        for x in EVENT_DEFAULTS:
            typ,title,cost,correct,deduct,target,entry,fromlvl,tolvl,vmin,vmax,spins,prizes=x
            db.execute("""INSERT OR IGNORE INTO vedo_events
            (event_type,title,enabled,entry_cost,correct_reward,incorrect_deduction,
             referral_target,referral_entry_points,referral_from_level,referral_to_level,
             voucher_points_min,voucher_points_max,spins_per_day,prizes_json,updated_at)
            VALUES (?,?,0,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (typ,title,cost,correct,deduct,target,entry,fromlvl,tolvl,vmin,vmax,spins,prizes,now_iso()))
        db.commit()
    finally: db.close()
ensure_vedo_events()

def event_row(db, typ):
    return db.execute("SELECT * FROM vedo_events WHERE event_type=?",(typ,)).fetchone()
def event_is_live(ev):
    if not ev or not ev["enabled"]: return False
    now=datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        start=datetime.fromisoformat(ev["starts_at"]) if ev["starts_at"] else None
        end=datetime.fromisoformat(ev["ends_at"]) if ev["ends_at"] else None
    except ValueError: return False
    return not ((start and start>now) or (end and end<now))
def audit_event(db, action, details):
    u=current_user()
    if u: db.execute("INSERT INTO vedo_audit_log(admin_id,action,details,created_at) VALUES (?,?,?,?)",
                     (u["id"],action,details[:1000],now_iso()))

@app.route("/events")
@login_required
def events_home():
    db=get_db(); rows=db.execute("SELECT * FROM vedo_events ORDER BY id").fetchall()
    ent={r["event_id"]:r for r in db.execute("SELECT event_id,progress,status FROM vedo_event_entries WHERE user_id=?",(current_user()["id"],)).fetchall()}
    events=[]
    for r in rows:
        d=dict(r); d["live"]=event_is_live(r); d["entry"]=ent.get(r["id"]); events.append(d)
    pots=db.execute("SELECT * FROM savings_pots WHERE user_id=? AND status='LOCKED' ORDER BY matures_at",(current_user()["id"],)).fetchall()
    return page(EVENTS_BODY,events=events,pots=pots)

@app.route("/events/<event_type>",methods=["GET","POST"])
@login_required
def event_detail(event_type):
    if event_type not in {"wheel","quiz","referral","savings","voucher"}: abort(404)
    db=get_db(); ev=event_row(db,event_type)
    if ev is None: abort(404)
    user=current_user()
    entry=db.execute("SELECT * FROM vedo_event_entries WHERE event_id=? AND user_id=?",(ev["id"],user["id"])).fetchone()
    if request.method=="POST":
        if not event_is_live(ev):
            flash("This event is not currently open.","error")
            return redirect(url_for("event_detail",event_type=event_type))
        if event_type=="referral":
            if entry:
                flash("You have already joined this challenge.","info")
            elif int(user["level"] or 0)<int(ev["referral_from_level"]):
                flash(f"You must be at least Level {ev['referral_from_level']} to join.","error")
            else:
                cost=max(0,int(ev["referral_entry_points"]))*100
                if cost and not debit(db,user["id"],cost,"REFERRAL_ENTRY",f"Joined Referral Challenge #{ev['id']}"):
                    db.rollback(); flash("Not enough points to join.","error")
                else:
                    db.execute("INSERT OR IGNORE INTO vedo_event_entries(event_id,user_id,joined_at,progress,status,metadata) VALUES (?,?,?,0,'ACTIVE','{}')",(ev["id"],user["id"],now_iso()))
                    db.commit(); flash("You joined the Referral Challenge.","ok")
        elif event_type=="voucher":
            if entry: flash("You have already opened an envelope for this event.","error")
            else:
                low=max(0,int(ev["voucher_points_min"])); high=max(low,int(ev["voucher_points_max"]))
                reward=secrets.randbelow(high-low+1)+low
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute("INSERT INTO vedo_event_entries(event_id,user_id,joined_at,progress,status,metadata) VALUES (?,?,?,1,'CLAIMED','{}')",(ev["id"],user["id"],now_iso()))
                    credit(db,user["id"],reward*100,"VOUCHER_DAY",f"Guaranteed points envelope, event #{ev['id']}")
                    db.execute("INSERT INTO vedo_event_results(event_id,user_id,result_type,points_delta,detail,created_at) VALUES (?,?,?,?,?,?)",(ev["id"],user["id"],"voucher",reward*100,"Guaranteed points envelope",now_iso()))
                    db.commit(); flash(f"Your envelope contained {reward:,} points. Your balance has been updated.","ok")
                except sqlite3.IntegrityError:
                    db.rollback(); flash("This event has already been claimed.","error")
        elif event_type=="wheel":
            today_key=today().isoformat()
            db.execute("BEGIN IMMEDIATE")
            try:
                sr=db.execute("SELECT spins FROM vedo_spin_daily WHERE user_id=? AND event_id=? AND spin_day=?",(user["id"],ev["id"],today_key)).fetchone()
                used=sr["spins"] if sr else 0
                if used>=max(1,int(ev["spins_per_day"])):
                    db.rollback(); flash("You have used all spins for today.","error")
                    return redirect(url_for("event_detail",event_type=event_type))
                prizes=json.loads(ev["prizes_json"] or "[]"); available=[]
                for pr in prizes:
                    used_n=db.execute("SELECT COUNT(*) n FROM vedo_event_results WHERE event_id=? AND result_type=?",(ev["id"],"wheel:"+str(pr.get("name","")))).fetchone()["n"]
                    if used_n<max(0,int(pr.get("limit",0))): available.append(pr)
                total=sum(max(0,int(pr.get("weight",1))) for pr in available)
                if not available or total<=0:
                    db.rollback(); flash("No configured wheel outcomes remain.","error")
                    return redirect(url_for("event_detail",event_type=event_type))
                pick=secrets.randbelow(total); prize=available[-1]
                for pr in available:
                    w=max(0,int(pr.get("weight",1)))
                    if pick<w: prize=pr; break
                    pick-=w
                cost=max(0,int(ev["entry_cost"]))*100
                if cost and not debit(db,user["id"],cost,"WHEEL_SPIN",f"Wheel spin event #{ev['id']}"):
                    db.rollback(); flash("Not enough points to spin.","error")
                    return redirect(url_for("event_detail",event_type=event_type))
                points=max(0,int(prize.get("points",0)))
                if points: credit(db,user["id"],points*100,"WHEEL_PRIZE",f"{prize['name']} from event #{ev['id']}")
                db.execute("INSERT INTO vedo_spin_daily(user_id,event_id,spin_day,spins) VALUES (?,?,?,1) ON CONFLICT(user_id,event_id,spin_day) DO UPDATE SET spins=spins+1",(user["id"],ev["id"],today_key))
                db.execute("INSERT INTO vedo_event_results(event_id,user_id,result_type,points_delta,detail,created_at) VALUES (?,?,?,?,?,?)",(ev["id"],user["id"],"wheel:"+str(prize.get("name","Prize")),points*100,str(prize.get("name","Prize")),now_iso()))
                db.commit(); flash(f"You received {prize.get('name','Prize')}: {points:,} points.","ok")
            except Exception:
                db.rollback(); app.logger.exception("Wheel outcome failed"); flash("Spin could not be processed. Please try again.","error")
        elif event_type=="quiz":
            qkey=request.form.get("question_key",""); answer=request.form.get("answer","")
            q=next((q for q in QUIZ_QUESTIONS if q["key"]==qkey),None)
            if not q: flash("Question not found.","error")
            elif db.execute("SELECT 1 FROM vedo_quiz_attempts WHERE event_id=? AND user_id=? AND question_key=?",(ev["id"],user["id"],qkey)).fetchone():
                flash("You have already answered that question.","error")
            else:
                correct=answer==q["correct"]; delta=int(ev["correct_reward"]) if correct else -int(ev["incorrect_deduction"])
                db.execute("BEGIN IMMEDIATE")
                try:
                    if delta<0 and not debit(db,user["id"],-delta,"QUIZ_DEDUCTION",f"Incorrect quiz answer {qkey}"):
                        db.rollback(); flash("Your points balance is too low for this disclosed deduction.","error")
                        return redirect(url_for("event_detail",event_type=event_type))
                    if delta>0: credit(db,user["id"],delta,"QUIZ_REWARD",f"Correct quiz answer {qkey}")
                    db.execute("INSERT INTO vedo_quiz_attempts(event_id,user_id,question_key,answer_key,correct,points_delta,created_at) VALUES (?,?,?,?,?,?,?)",(ev["id"],user["id"],qkey,answer,int(correct),delta,now_iso()))
                    db.execute("INSERT INTO vedo_event_results(event_id,user_id,result_type,points_delta,detail,created_at) VALUES (?,?,?,?,?,?)",(ev["id"],user["id"],"quiz",delta,qkey,now_iso()))
                    db.commit(); flash(("Correct!" if correct else "Incorrect.")+f" {delta:+d} points.","ok" if correct else "error")
                except sqlite3.IntegrityError:
                    db.rollback(); flash("Answer already recorded.","error")
        else:
            flash("Your savings pots continue independently. Manage them on the Savings Pots page.","info")
        return redirect(url_for("event_detail",event_type=event_type))
    user=current_user()
    entry=db.execute("SELECT * FROM vedo_event_entries WHERE event_id=? AND user_id=?",(ev["id"],user["id"])).fetchone()
    balance=db.execute("SELECT points FROM users WHERE id=?",(user["id"],)).fetchone()["points"]
    sr=db.execute("SELECT spins FROM vedo_spin_daily WHERE user_id=? AND event_id=? AND spin_day=?",(user["id"],ev["id"],today().isoformat())).fetchone() if event_type=="wheel" else None
    questions=[]
    if event_type=="quiz":
        for q in QUIZ_QUESTIONS:
            ans=db.execute("SELECT correct,points_delta FROM vedo_quiz_attempts WHERE event_id=? AND user_id=? AND question_key=?",(ev["id"],user["id"],q["key"])).fetchone()
            questions.append({**q,"answered":bool(ans),"result":ans})
    progress=entry["progress"] if entry else 0
    if event_type=="referral":
        progress=db.execute("SELECT COUNT(*) n FROM users WHERE referred_by=? AND level>=?",(user["id"],max(1,int(ev["referral_from_level"])))).fetchone()["n"]
        if entry:
            if progress >= int(ev["referral_target"]) and entry["status"] == "ACTIVE":
                # Admin-configured promotion destination; apply once after target verification.
                destination = max(1, min(7, int(ev["referral_to_level"])))
                db.execute("UPDATE users SET level=? WHERE id=? AND level<?",
                           (destination, user["id"], destination))
                db.execute("UPDATE vedo_event_entries SET progress=?,status='COMPLETED' WHERE id=?",
                           (progress, entry["id"]))
                db.execute("INSERT INTO vedo_event_results(event_id,user_id,result_type,points_delta,detail,created_at) VALUES (?,?,?,?,?,?)",
                           (ev["id"], user["id"], "referral_promotion", 0,
                            f"Promotion to Level {destination}", now_iso()))
                db.commit()
            else:
                db.execute("UPDATE vedo_event_entries SET progress=? WHERE id=?",(progress,entry["id"])); db.commit()
    results=db.execute("SELECT * FROM vedo_event_results WHERE event_id=? AND user_id=? ORDER BY id DESC LIMIT 10",(ev["id"],user["id"])).fetchall()
    pots=db.execute("SELECT * FROM savings_pots WHERE user_id=? AND status='LOCKED' ORDER BY matures_at",(user["id"],)).fetchall()
    return page(EVENT_DETAIL_BODY,event=ev,live=event_is_live(ev),entry=entry,balance=balance,
                used_spins=sr["spins"] if sr else 0,questions=questions,progress=progress,results=results,savings_pots=pots)

@app.route("/admin/events",methods=["GET","POST"])
@admin_required
def admin_events():
    db=get_db()
    if request.method=="POST":
        typ=request.form.get("event_type",""); ev=event_row(db,typ)
        if not ev: abort(400)
        try:
            enabled=int(request.form.get("enabled")=="1")
            starts=request.form.get("starts_at","").strip()[:16].replace("T"," ")
            ends=request.form.get("ends_at","").strip()[:16].replace("T"," ")
            if starts and ends and starts>=ends: raise ValueError("End must be after start.")
            cost=max(0,int(request.form.get("entry_cost","0")))
            correct=max(0,int(request.form.get("correct_reward","50")))
            deduct=max(0,int(request.form.get("incorrect_deduction","10")))
            target=max(1,min(1000,int(request.form.get("referral_target","10"))))
            entry=max(0,int(request.form.get("referral_entry_points","0")))
            fromlvl=max(1,min(7,int(request.form.get("referral_from_level","1"))))
            tolvl=max(1,min(7,int(request.form.get("referral_to_level","2"))))
            vmin=max(0,int(request.form.get("voucher_points_min","50")))
            vmax=max(vmin,int(request.form.get("voucher_points_max","200")))
            spins=max(1,min(100,int(request.form.get("spins_per_day","1"))))
            prizes=json.loads(request.form.get("prizes_json","[]"))
            if not isinstance(prizes,list) or len(prizes)>20: raise ValueError("Wheel prizes must be a list of up to 20 outcomes.")
            for pr in prizes:
                if not isinstance(pr,dict) or not str(pr.get("name","")).strip(): raise ValueError("Every prize needs a name.")
                pr["points"]=max(0,min(1000000,int(pr.get("points",0))))
                pr["weight"]=max(0,min(1000000,int(pr.get("weight",1))))
                pr["limit"]=max(0,min(1000000,int(pr.get("limit",0))))
            if typ=="wheel" and not prizes: raise ValueError("Configure at least one wheel outcome.")
        except (ValueError,TypeError,json.JSONDecodeError) as exc:
            flash(f"Settings not saved: {exc}","error"); return redirect(url_for("admin_events"))
        db.execute("""UPDATE vedo_events SET enabled=?,starts_at=?,ends_at=?,entry_cost=?,correct_reward=?,
        incorrect_deduction=?,referral_target=?,referral_entry_points=?,referral_from_level=?,referral_to_level=?,
        voucher_points_min=?,voucher_points_max=?,spins_per_day=?,prizes_json=?,updated_at=? WHERE event_type=?""",
        (enabled,starts,ends,cost,correct,deduct,target,entry,fromlvl,tolvl,vmin,vmax,spins,json.dumps(prizes),now_iso(),typ))
        audit_event(db,"EVENT_SETTINGS",f"{typ}: enabled={enabled}; starts={starts}; ends={ends}")
        db.commit(); flash(f"{ev['title']} settings saved.","ok"); return redirect(url_for("admin_events"))
    rows=db.execute("SELECT * FROM vedo_events ORDER BY id").fetchall()
    stats={r["event_type"]:{"entries":db.execute("SELECT COUNT(*) n FROM vedo_event_entries WHERE event_id=?",(r["id"],)).fetchone()["n"],
                           "results":db.execute("SELECT COUNT(*) n FROM vedo_event_results WHERE event_id=?",(r["id"],)).fetchone()["n"]} for r in rows}
    logs=db.execute("SELECT a.*,u.username FROM vedo_audit_log a JOIN users u ON u.id=a.admin_id ORDER BY a.id DESC LIMIT 20").fetchall()
    return page(ADMIN_EVENTS_BODY,events=rows,stats=stats,logs=logs)


EVENTS_BODY = """
<div class="card note"><h1>Vedo Events & Challenges</h1><p class="muted">Join weekly events, track progress, and collect eligible rewards. Active savings pots continue independently.</p></div>
<div class="grid">
{% for e in events %}<div class="card">
<div style="display:flex;justify-content:space-between;gap:10px;align-items:center"><h2>{{ e['title'] }}</h2><span class="pill">{{ 'OPEN' if e['live'] else ('SCHEDULED' if e['enabled'] else 'CLOSED') }}</span></div>
{% if e['event_type']=='wheel' %}<p class="muted">Spin for admin-configured prizes. The server enforces outcome limits.</p>
{% elif e['event_type']=='quiz' %}<p class="muted">Correct answers earn points; incorrect answers deduct the amount shown before you start.</p>
{% elif e['event_type']=='referral' %}<p class="muted">Opt in, refer members and track progress toward the admin-selected destination level.</p>
{% elif e['event_type']=='savings' %}<p class="muted">Your existing savings pots keep progressing even when this event is closed.</p>
{% else %}<p class="muted">Open one envelope and receive guaranteed points, with bonus vouchers subject to published availability.</p>{% endif %}
{% if e['entry'] %}<p><b>Your status:</b> {{ e['entry']['status'] }} · Progress {{ e['entry']['progress'] }}</p>{% endif %}
{% if e['starts_at'] %}<p class="small muted">Starts: {{ e['starts_at'] }} UTC</p>{% endif %}{% if e['ends_at'] %}<p class="small muted">Closes: {{ e['ends_at'] }} UTC</p>{% endif %}
<a class="button primary" href="{{ url_for('event_detail',event_type=e['event_type']) }}">View event</a></div>{% endfor %}
</div>
{% if pots %}<div class="card"><h2>My active savings progress</h2>{% for p in pots %}<p><b>{{ money(p['principal']) }}</b> committed · {{ p['weeks'] }} weeks · Matures {{ p['matures_at'][:10] }} · <span class="pill">ACTIVE</span></p>{% endfor %}<p class="muted small">Savings progress is independent of Referral Challenge participation.</p></div>{% endif %}
"""

EVENT_DETAIL_BODY = """
<style>
.event-games{--game-ink:#172033;--game-muted:#64748b;--game-line:#e2e8f0;color:var(--game-ink)}
.event-games .game-card{background:#fff;color:var(--game-ink);border:1px solid #e5eaf2;border-radius:22px;padding:clamp(18px,3vw,30px);box-shadow:0 14px 38px rgba(15,23,42,.09);overflow:hidden}
.event-games .game-card h2{color:#172033;margin-top:0}.event-games .game-muted{color:var(--game-muted)}
.event-games .game-pill{display:inline-flex;align-items:center;gap:6px;background:#eff6ff;color:#1d4ed8;border-radius:999px;padding:7px 11px;font-size:12px;font-weight:800}
.event-games .wheel-stage{position:relative;width:min(100%,340px);aspect-ratio:1;margin:22px auto 26px;filter:drop-shadow(0 12px 14px #0f172a25)}
.event-games .prize-wheel{position:absolute;inset:0;border-radius:50%;border:10px solid #f8d24b;outline:4px solid #a66b18;background:conic-gradient(#fb7185 0 45deg,#8b5cf6 45deg 90deg,#22c55e 90deg 135deg,#fb923c 135deg 180deg,#3b82f6 180deg 225deg,#06b6d4 225deg 270deg,#facc15 270deg 315deg,#f43f5e 315deg 360deg);transition:transform 4s cubic-bezier(.12,.68,.08,1)}
.event-games .prize-wheel:after{content:'';position:absolute;inset:0;border-radius:50%;background:repeating-conic-gradient(transparent 0 44deg,#ffffffc9 44deg 45deg)}
.event-games .wheel-prize{position:absolute;z-index:2;left:50%;top:50%;width:70px;margin-left:-35px;margin-top:-15px;text-align:center;font-size:12px;font-weight:900;color:white;text-shadow:0 2px 4px #172033a8}
.event-games .wheel-prize b{display:block;font-size:24px}.event-games .wheel-prize:nth-child(1){transform:rotate(22.5deg) translateY(-112px) rotate(-22.5deg)}.event-games .wheel-prize:nth-child(2){transform:rotate(67.5deg) translateY(-112px) rotate(-67.5deg)}.event-games .wheel-prize:nth-child(3){transform:rotate(112.5deg) translateY(-112px) rotate(-112.5deg)}.event-games .wheel-prize:nth-child(4){transform:rotate(157.5deg) translateY(-112px) rotate(-157.5deg)}.event-games .wheel-prize:nth-child(5){transform:rotate(202.5deg) translateY(-112px) rotate(-202.5deg)}.event-games .wheel-prize:nth-child(6){transform:rotate(247.5deg) translateY(-112px) rotate(-247.5deg)}.event-games .wheel-prize:nth-child(7){transform:rotate(292.5deg) translateY(-112px) rotate(-292.5deg)}.event-games .wheel-prize:nth-child(8){transform:rotate(337.5deg) translateY(-112px) rotate(-337.5deg)}
.event-games .wheel-hub{position:absolute;z-index:4;left:50%;top:50%;transform:translate(-50%,-50%);width:23%;aspect-ratio:1;border-radius:50%;background:radial-gradient(circle at 30% 25%,#fff7bf,#fbbf24 65%,#b45309);border:6px solid #fff0a8;box-shadow:0 0 0 4px #9a670d}.event-games .wheel-pointer{position:absolute;z-index:5;left:50%;top:-7px;transform:translateX(-50%);border-left:18px solid transparent;border-right:18px solid transparent;border-top:38px solid #fef08a;filter:drop-shadow(0 3px 3px #33415588)}
.event-games .game-action{display:flex;justify-content:center}.event-games .game-action .primary{min-width:190px}.event-games .envelope-stage{display:grid;place-items:center;padding:25px 0 16px;perspective:900px}.event-games .mystery-envelope{position:relative;width:min(100%,260px);height:175px;border:0;background:transparent;filter:drop-shadow(0 15px 13px #0f172a25);transform-style:preserve-3d}.event-games .envelope-back{position:absolute;inset:0;border-radius:12px;background:#f8c75b;border:2px solid #d89b27}.event-games .envelope-paper{position:absolute;left:12%;right:12%;top:10%;height:78%;border-radius:8px;background:#fffdf5;border:1px solid #e8dfc9;display:grid;place-items:center;font-size:42px;transition:transform .7s ease;z-index:1}.event-games .envelope-front{position:absolute;inset:0;background:linear-gradient(145deg,#f9d477,#e9a82c);clip-path:polygon(0 0,50% 55%,100% 0,100% 100%,0 100%);border-radius:10px;z-index:2}.event-games .envelope-flap{position:absolute;inset:0;background:#ffdf91;clip-path:polygon(0 0,100% 0,50% 63%);transform-origin:top;transition:transform .6s ease;z-index:3}.event-games .envelope-seal{position:absolute;z-index:4;left:50%;top:50%;transform:translate(-50%,-10%);width:46px;height:46px;border-radius:50%;display:grid;place-items:center;background:#dc3b50;color:#fff;border:3px solid #ffb3bc;font-size:23px}
.event-games .envelope-form.opening .envelope-flap{transform:rotateX(180deg)}.event-games .envelope-form.opening .envelope-paper{transform:translateY(-55px)}.event-games .envelope-form.opening .mystery-envelope{animation:envShake .45s ease-in-out 2}.event-games .envelope-form .primary{margin-top:8px}
@keyframes envShake{0%,100%{transform:rotate(0)}25%{transform:rotate(-4deg)}75%{transform:rotate(4deg)}}
.event-games .paper-quiz{background:#fffefa;color:#202938;border:1px solid #e5dfd1;border-radius:5px;padding:20px;box-shadow:0 8px 20px #0f172a12;background-image:linear-gradient(#ffffff 31px,#e7edf5 32px);background-size:100% 32px}
.event-games .paper-quiz .question-card{background:rgba(255,255,255,.93);border:1px solid #e4e8ee;border-radius:14px;padding:18px;margin:15px 0;box-shadow:0 3px 9px #0f172a0b}
.event-games .answer-option{display:flex!important;align-items:center;gap:10px;background:#fff;border:1px solid #d8e0ea;border-radius:11px;padding:12px;margin:9px 0!important;cursor:pointer;transition:border-color .15s,background .15s}.event-games .answer-option:hover{border-color:#2563eb;background:#eff6ff}.event-games .answer-option input{accent-color:#2563eb;width:17px;height:17px}.event-games .paper-quiz .primary{background:#2563eb;color:#fff;box-shadow:0 4px 0 #1d4ed8}.event-games .prize-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-top:18px}.event-games .prize-chip{background:#f8fafc;border:1px solid #e2e8f0;border-radius:13px;padding:12px}.event-games .prize-chip b{display:block}.event-games .prize-chip span{color:#64748b;font-size:12px}
@media(max-width:520px){.event-games .wheel-stage{width:min(100%,290px)}.event-games .wheel-prize{transform-origin:center}.event-games .wheel-prize:nth-child(n){font-size:10px}.event-games .wheel-prize b{font-size:20px}.event-games .wheel-prize:nth-child(1){transform:rotate(22.5deg) translateY(-94px) rotate(-22.5deg)}.event-games .wheel-prize:nth-child(2){transform:rotate(67.5deg) translateY(-94px) rotate(-67.5deg)}.event-games .wheel-prize:nth-child(3){transform:rotate(112.5deg) translateY(-94px) rotate(-112.5deg)}.event-games .wheel-prize:nth-child(4){transform:rotate(157.5deg) translateY(-94px) rotate(-157.5deg)}.event-games .wheel-prize:nth-child(5){transform:rotate(202.5deg) translateY(-94px) rotate(-202.5deg)}.event-games .wheel-prize:nth-child(6){transform:rotate(247.5deg) translateY(-94px) rotate(-247.5deg)}.event-games .wheel-prize:nth-child(7){transform:rotate(292.5deg) translateY(-94px) rotate(-292.5deg)}.event-games .wheel-prize:nth-child(8){transform:rotate(337.5deg) translateY(-94px) rotate(-337.5deg)}}
</style>
<div class="event-games">
<div class="card note"><a href="{{ url_for('events_home') }}">← All events</a><h1 style="margin-top:12px">{{ event['title'] }}</h1><p class="muted">{{ 'Event is open' if live else 'Event is currently closed to new actions.' }}</p>
{% if event['starts_at'] or event['ends_at'] %}<p class="small muted">Starts {{ event['starts_at'] or 'immediately' }} UTC · Closes {{ event['ends_at'] or 'not scheduled' }} UTC</p>{% endif %}
<div class="stats"><div class="stat"><b>{{ money(balance) }}</b><span>Points balance</span></div>
{% if event['event_type']=='wheel' %}<div class="stat"><b>{{ used_spins }}/{{ event['spins_per_day'] }}</b><span>Spins used today</span></div>{% endif %}
{% if event['event_type']=='referral' %}<div class="stat"><b>{{ progress }}/{{ event['referral_target'] }}</b><span>Referral progress</span></div>{% endif %}
{% if event['event_type']=='quiz' %}<div class="stat"><b>+{{ event['correct_reward'] }}</b><span>Correct answer</span></div><div class="stat"><b>-{{ event['incorrect_deduction'] }}</b><span>Incorrect answer</span></div>{% endif %}</div></div>
{% if event['event_type']=='wheel' %}<div class="card game-card"><div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap"><div><span class="game-pill">🎮 VEDO PLAY</span><h2 style="margin:12px 0 5px">Spin &amp; Win</h2><p class="game-muted">One spin could unlock your next surprise.</p></div><span class="game-pill">{{ used_spins }}/{{ event['spins_per_day'] }} used</span></div>
<div class="wheel-stage" aria-label="Prize wheel"><div class="wheel-pointer"></div><div class="prize-wheel" id="vedoPrizeWheel">{% for p in (event['prizes_json']|from_json)[:8] %}<div class="wheel-prize"><b>{% if loop.index0==0 %}🎁{% elif loop.index0==1 %}⭐{% elif loop.index0==2 %}🎉{% elif loop.index0==3 %}💎{% elif loop.index0==4 %}🎟️{% elif loop.index0==5 %}☕{% elif loop.index0==6 %}🏆{% else %}✨{% endif %}</b>{{ p['name'] }}</div>{% endfor %}</div><div class="wheel-hub"> </div></div>
<div style="text-align:center"><p class="game-muted">Cost <b>{{ event['entry_cost'] }} points</b> per spin · Limit {{ event['spins_per_day'] }} per day</p><form method="post" class="game-action" id="vedoSpinForm"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="primary" id="vedoSpinButton" {% if not live or used_spins>=event['spins_per_day'] %}disabled{% endif %}>🎡 Spin the wheel</button></form><p class="game-muted" style="font-size:12px">The server securely selects and records each result. The animation is for presentation only.</p></div>
<h3>Prizes in this event</h3><div class="prize-grid">{% for p in event['prizes_json']|from_json %}<div class="prize-chip"><b>{{ p['name'] }}</b><span>{{ p['points'] }} points · limit {{ p['limit'] }}</span></div>{% endfor %}</div></div>
{% elif event['event_type']=='quiz' %}<div class="card game-card"><span class="game-pill">🧠 BRAIN BATTLE</span><div class="paper-quiz" style="margin-top:16px"><h2>Brain Battle</h2><p class="game-muted">Choose your answer like a real quiz. Think carefully before submitting.</p><p><b>Correct answer:</b> +{{ event['correct_reward'] }} points &nbsp; · &nbsp; <b>Incorrect:</b> −{{ event['incorrect_deduction'] }} points</p>
{% for q in questions %}<div class="question-card"><div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><b>Question {{ loop.index }} of {{ questions|length }}</b><span class="game-pill">{{ q['category'] }}</span></div><p style="font-size:17px;font-weight:750;line-height:1.5">{{ q['question'] }}</p>
{% if q['answered'] %}<p class="{{ 'pos' if q['result']['correct'] else 'neg' }}"><b>{{ '✓ Correct' if q['result']['correct'] else '✕ Incorrect' }}</b> · {{ '%+d'|format(q['result']['points_delta']) }} points</p>
{% else %}<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="question_key" value="{{ q['key'] }}">{% for key,label in q['answers'] %}<label class="answer-option"><input type="radio" name="answer" value="{{ key }}" required><span>{{ label }}</span></label>{% endfor %}<button class="primary" {% if not live %}disabled{% endif %}>Check my answer</button></form>{% endif %}</div>{% endfor %}</div></div>
{% elif event['event_type']=='referral' %}<div class="card"><h2>Referral Challenge</h2><p>Entry commitment: <b>{{ event['referral_entry_points'] }} points</b>. Target: <b>{{ event['referral_target'] }} verified referrals</b>. Eligible starting level: {{ event['referral_from_level'] }}. Destination level: {{ event['referral_to_level'] }}.</p><p class="muted">This version counts direct referrals who reach the configured minimum level. Recharge-based qualification needs to be connected to a verified payment workflow.</p>
{% if entry %}<div class="progress"><div style="width:{{ [100,progress*100/event['referral_target']]|min }}%"></div></div><p><b>{{ progress }} / {{ event['referral_target'] }}</b> qualifying referrals</p><p>Status: {{ entry['status'] }}</p>
{% else %}<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><label><input type="checkbox" required> I agree to the point commitment and published rules.</label><button class="primary" {% if not live %}disabled{% endif %}>Join Referral Challenge</button></form>{% endif %}</div>
{% elif event['event_type']=='voucher' %}<div class="card game-card" style="text-align:center"><span class="game-pill">✉️ MYSTERY REWARD</span><h2 style="margin:14px 0 5px">Choose your mystery envelope</h2><p class="game-muted">Every eligible member receives guaranteed points. Bonus vouchers depend on the event's published stock.</p><div class="envelope-stage"><div class="mystery-envelope" aria-label="Sealed mystery envelope"><div class="envelope-back"></div><div class="envelope-paper">🎁</div><div class="envelope-front"></div><div class="envelope-flap"></div><div class="envelope-seal">?</div></div></div><p><b>{{ event['voucher_points_min'] }}–{{ event['voucher_points_max'] }} points guaranteed</b></p><form method="post" class="envelope-form" id="vedoEnvelopeForm"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="primary" id="vedoEnvelopeButton" {% if not live or entry %}disabled{% endif %}>✨ Open my envelope</button></form>{% if entry %}<p class="game-muted">You have already claimed your envelope for this event.</p>{% else %}<p class="game-muted" style="font-size:12px">The reward is selected and credited securely when you open it.</p>{% endif %}</div>
{% else %}<div class="card"><h2>Savings continue independently</h2><p>Existing savings pots continue toward maturity even if this event is paused. Visit <a href="{{ url_for('pots') }}">Savings pots</a> to manage them.</p>{% for p in savings_pots %}<p>{{ money(p['principal']) }} committed · {{ p['weeks'] }} weeks · maturity {{ p['matures_at'][:10] }}</p>{% else %}<p class="muted">No active savings pots.</p>{% endfor %}</div>{% endif %}
{% if results %}<div class="card"><h2>Your recent results</h2><table><tr><th>Date</th><th>Result</th><th>Points delta (cents)</th><th>Details</th></tr>{% for r in results %}<tr><td>{{ r['created_at'][:16] }}</td><td>{{ r['result_type'] }}</td><td>{{ r['points_delta'] }}</td><td>{{ r['detail'] }}</td></tr>{% endfor %}</table></div>{% endif %}
</div>
<script>
(function(){
  var spinForm=document.getElementById('vedoSpinForm'), spinWheel=document.getElementById('vedoPrizeWheel'), spinButton=document.getElementById('vedoSpinButton');
  if(spinForm && spinWheel && spinButton){spinForm.addEventListener('submit',function(e){if(spinForm.dataset.animating==='1'){return;}e.preventDefault();if(spinButton.disabled)return;spinForm.dataset.animating='1';spinButton.disabled=true;spinButton.textContent='✨ Spinning…';var turns=360*5+Math.floor(Math.random()*360);spinWheel.style.transform='rotate('+turns+'deg)';window.setTimeout(function(){HTMLFormElement.prototype.submit.call(spinForm);},4100);});}
  var envForm=document.getElementById('vedoEnvelopeForm');
  if(envForm){envForm.addEventListener('submit',function(e){if(envForm.dataset.animating==='1'){return;}e.preventDefault();var button=document.getElementById('vedoEnvelopeButton');if(button && button.disabled)return;envForm.dataset.animating='1';envForm.classList.add('opening');if(button){button.disabled=true;button.textContent='🎉 Opening…';}window.setTimeout(function(){HTMLFormElement.prototype.submit.call(envForm);},1200);});}
})();
</script>
"""

ADMIN_EVENTS_BODY = """
<div class="card note"><h1>Event Control Centre</h1><p class="muted">Enable, schedule and configure each event independently. Closing enrolment does not stop existing savings pots or delete results.</p></div>
{% for e in events %}<div class="card"><h2>{{ e['title'] }}</h2><p class="muted small">Entries {{ stats[e['event_type']]['entries'] }} · Results {{ stats[e['event_type']]['results'] }} · Updated {{ e['updated_at'] }}</p>
<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="event_type" value="{{ e['event_type'] }}">
<label><input type="checkbox" name="enabled" value="1" {% if e['enabled'] %}checked{% endif %}> Enable event</label>
<div class="grid"><div><label>Starts at (UTC)</label><input type="datetime-local" name="starts_at" value="{{ e['starts_at'].replace(' ','T') if e['starts_at'] else '' }}"></div>
<div><label>Closes at (UTC)</label><input type="datetime-local" name="ends_at" value="{{ e['ends_at'].replace(' ','T') if e['ends_at'] else '' }}"></div>
<div><label>Wheel cost (points)</label><input type="number" min="0" name="entry_cost" value="{{ e['entry_cost'] }}"></div><div><label>Spins per day</label><input type="number" min="1" max="100" name="spins_per_day" value="{{ e['spins_per_day'] }}"></div>
<div><label>Correct answer reward</label><input type="number" min="0" name="correct_reward" value="{{ e['correct_reward'] }}"></div><div><label>Incorrect answer deduction</label><input type="number" min="0" name="incorrect_deduction" value="{{ e['incorrect_deduction'] }}"></div>
<div><label>Referral entry commitment (points)</label><input type="number" min="0" name="referral_entry_points" value="{{ e['referral_entry_points'] }}"></div><div><label>Referral target</label><input type="number" min="1" max="1000" name="referral_target" value="{{ e['referral_target'] }}"></div>
<div><label>Starting level</label><select name="referral_from_level">{% for l in LEVELS %}<option value="{{ l }}" {% if e['referral_from_level']==l %}selected{% endif %}>Level {{ l }}</option>{% endfor %}</select></div>
<div><label>Promotion destination</label><select name="referral_to_level">{% for l in LEVELS %}<option value="{{ l }}" {% if e['referral_to_level']==l %}selected{% endif %}>Level {{ l }}</option>{% endfor %}</select></div>
<div><label>Voucher guaranteed minimum points</label><input type="number" min="0" name="voucher_points_min" value="{{ e['voucher_points_min'] }}"></div><div><label>Voucher guaranteed maximum points</label><input type="number" min="0" name="voucher_points_max" value="{{ e['voucher_points_max'] }}"></div></div>
{% if e['event_type']=='wheel' %}<label>Wheel prize JSON: name, points, weight, limit</label><textarea name="prizes_json" rows="7" spellcheck="false">{{ e['prizes_json'] }}</textarea>{% else %}<input type="hidden" name="prizes_json" value="{{ e['prizes_json'] }}">{% endif %}
<button class="primary">Save settings</button></form></div>{% endfor %}
<div class="card"><h2>Recent admin audit log</h2>{% if logs %}<table><tr><th>When</th><th>Admin</th><th>Action</th><th>Details</th></tr>{% for l in logs %}<tr><td>{{ l['created_at'] }}</td><td>{{ l['username'] }}</td><td>{{ l['action'] }}</td><td>{{ l['details'] }}</td></tr>{% endfor %}</table>{% else %}<p class="muted">No changes recorded yet.</p>{% endif %}</div>
"""

app.jinja_loader = DictLoader({"base": BASE})
app.jinja_env.filters["from_json"] = json.loads
app.jinja_env.filters["from_json"] = json.loads




if __name__ == "__main__":
    init_db()
    print("\n======================================")
    print(" VEDO INVEST")
    print("======================================")
    print("Open: http://127.0.0.1:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
