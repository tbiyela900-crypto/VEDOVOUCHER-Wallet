"""
Vedo Ai Investment  -  advanced interface edition
=================================================================

Features
  * Accounts, login/logout, members start at Level 0 and earn 0 until top up
  * Sign-up uses an 11-digit Bank Account  number + a CELLPHONE NUMBER that must be
    verified with an SMS one-time code (OTP) before the first login.
    The verified cellphone number IS the login ID.
  * TOP-UP page looks like the check-in page: the member taps a level tile,
    enters an amount and presses Enter. The tiles close and a WHITE details
    tab opens for 10 minutes showing the Bank Name, Reference, Amount,
    PayShap and Bank Account  number, a small Top-up flag and a red message. ALL of
    that information is entered by the admin only (Admin page > Top-up
    details, for everyone or for one member). The member can copy every line.
  * Admin approval of a top-up only marks it approved. It does NOT add any
    money to the balance.
  * The balance only grows through the daily check-in tasks (and referral
    bonuses, savings-pot interest, game rewards, etc.), or when the admin
    adds balance to a member by hand (Admin page > All members > Add balance).
  * Daily check-in (level reward / 24)
  * PHASE 1 GAMES (Games tab, admin controls on Admin > Games admin):
      - Vedo Spin & Win      : weighted prize spin. The admin sets the prizes,
                               the odds (weights), the stock (= how many
                               members can win each prize), spin limits and
                               the event schedule. Odds are shown to members.
      - Vedo Brain Battle    : answer questions, earn points per correct
                               answer. Admin manages questions, points,
                               round size, limits and schedule.
      - Vedo Mystery Envelope: every eligible member gets the guaranteed
                               minimum reward set by the admin, plus a
                               published chance of a bonus voucher while the
                               voucher stock lasts. One claim per member
                               per event.
    Every result is decided on the server, written to the points wallet
    (transaction ledger) and to the reward history / winner records.
    Daily Streak is NOT part of Phase 1 (hidden; can be added later).
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

STREAK_DAYS = 7                           # streak is tracked but hidden (not Phase 1)
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
    "Enter the amount and the reason for the top-up, then tap the level you "
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

# --- Phase 1 games ---------------------------------------------------------
GAMES = {
    "SPIN": "Vedo Spin & Win",
    "BRAIN": "Vedo Brain Battle",
    "ENVELOPE": "Vedo Mystery Envelope",
}
PRIZE_KINDS = {
    "POINTS": "Points",
    "VOUCHER": "Level voucher",
    "ITEM": "Item / prize",
    "NONE": "No prize",
}
STATUS_TEXT = {
    "OPEN": "Open",
    "PAUSED": "Paused",
    "UPCOMING": "Not started",
    "CLOSED": "Closed",
}

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
    "games": ('<rect x="3" y="8" width="18" height="13" rx="2"/>'
              '<path d="M12 8v13M3 13h18"/>'
              '<path d="M12 8c-3 0-4-4-1.5-4S12 8 12 8z"/>'
              '<path d="M12 8c3 0 4-4 1.5-4S12 8 12 8z"/>'),
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
    amount = Decimal(int(cents or 0)) / Decimal(100)
    return f"R{amount:,.2f}"


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


def parse_amount(text, minimum, maximum=None):
    """Parse a Rand amount string into cents. Returns None if invalid."""
    try:
        amount = Decimal((text or "").strip()).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None
    cents = int(amount * 100)
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
    amount INTEGER NOT NULL,
    bank_name TEXT NOT NULL DEFAULT 'DEMO',
    account_number TEXT NOT NULL DEFAULT 'N/A',
    account_holder TEXT NOT NULL DEFAULT 'N/A',
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount INTEGER NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS topup_details (
    user_id INTEGER PRIMARY KEY,          -- 0 = everyone, otherwise a member
    Bank_name TEXT NOT NULL DEFAULT '',
    Reference TEXT NOT NULL DEFAULT '',
    Amount TEXT NOT NULL DEFAULT '',
    PayShap TEXT NOT NULL DEFAULT '',
    BA_number TEXT NOT NULL DEFAULT '',
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
    amount INTEGER NOT NULL,
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

-- ---------------------------------------------------------------- games ---
-- One row per game: schedule, limits and (for the envelope) reward rules.
-- Times are stored in UTC as 'YYYY-MM-DD HH:MM'. event_no goes up when the
-- admin starts a new event, which resets everybody's play limits.
CREATE TABLE IF NOT EXISTS games (
    game TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1,
    starts_at TEXT NOT NULL DEFAULT '',
    ends_at TEXT NOT NULL DEFAULT '',
    max_plays INTEGER NOT NULL DEFAULT 1,
    period TEXT NOT NULL DEFAULT 'EVENT',          -- EVENT or DAY
    event_no INTEGER NOT NULL DEFAULT 1,
    min_level INTEGER NOT NULL DEFAULT 0,
    min_reward INTEGER NOT NULL DEFAULT 0,         -- envelope guaranteed cents
    bonus_level INTEGER NOT NULL DEFAULT 1,        -- envelope bonus voucher level
    bonus_stock INTEGER NOT NULL DEFAULT 0,        -- envelope vouchers left
    bonus_chance INTEGER NOT NULL DEFAULT 0,       -- envelope bonus chance %
    questions_per_round INTEGER NOT NULL DEFAULT 5
);

-- Spin & Win prizes. weight = odds. stock = max winners (NULL = unlimited).
CREATE TABLE IF NOT EXISTS spin_prizes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'POINTS',
    value INTEGER NOT NULL DEFAULT 0,              -- cents, or voucher level
    weight INTEGER NOT NULL DEFAULT 1,
    stock INTEGER,
    won INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS brain_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    a TEXT NOT NULL, b TEXT NOT NULL, c TEXT NOT NULL, d TEXT NOT NULL,
    correct INTEGER NOT NULL,                      -- 1..4
    points INTEGER NOT NULL DEFAULT 0,             -- cents per correct answer
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS brain_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    event_no INTEGER NOT NULL DEFAULT 1,
    question_ids TEXT NOT NULL,
    answers TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    correct_n INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'OPEN',
    created_at TEXT NOT NULL,
    finished_at TEXT
);

-- Reward history / winner records for all three games.
CREATE TABLE IF NOT EXISTS game_plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    game TEXT NOT NULL,
    event_no INTEGER NOT NULL DEFAULT 1,
    result TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'NONE',
    amount INTEGER NOT NULL DEFAULT 0,
    voucher TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
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


def add_tx(db, user_id, kind, amount_cents, note=""):
    """Write a ledger line (positive = credit, negative = debit)."""
    db.execute(
        "INSERT INTO transactions (user_id, kind, amount, note, created_at) "
        "VALUES (?,?,?,?,?)",
        (user_id, kind, int(amount_cents), note, now_iso()))


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

def seed_games(db):
    """Starter settings, prizes and questions (only when the tables are empty).
    The admin can change or delete all of it on Admin > Games admin."""
    defaults = {
        "SPIN": dict(max_plays=3, period="DAY"),
        "BRAIN": dict(max_plays=1, period="DAY", questions_per_round=5),
        "ENVELOPE": dict(max_plays=1, period="EVENT", min_reward=500,
                         bonus_level=1, bonus_stock=2, bonus_chance=10),
    }
    for game, d in defaults.items():
        db.execute(
            "INSERT OR IGNORE INTO games (game, max_plays, period, "
            "min_reward, bonus_level, bonus_stock, bonus_chance, "
            "questions_per_round) VALUES (?,?,?,?,?,?,?,?)",
            (game, d.get("max_plays", 1), d.get("period", "EVENT"),
             d.get("min_reward", 0), d.get("bonus_level", 1),
             d.get("bonus_stock", 0), d.get("bonus_chance", 0),
             d.get("questions_per_round", 5)))

    if not db.execute("SELECT 1 FROM spin_prizes").fetchone():
        for label, kind, value, weight, stock in (
                ("Better luck next time", "NONE", 0, 50, None),
                ("R5 points", "POINTS", 500, 30, None),
                ("R20 points", "POINTS", 2000, 15, 50),
                ("R100 points", "POINTS", 10000, 4, 10),
                ("Level 1 voucher", "VOUCHER", 1, 1, 2)):
            db.execute(
                "INSERT INTO spin_prizes (label, kind, value, weight, stock) "
                "VALUES (?,?,?,?,?)", (label, kind, value, weight, stock))

    if not db.execute("SELECT 1 FROM brain_questions").fetchone():
        for q, a, b, c, d, correct in (
                ("Which ocean lies on the east coast of South Africa?",
                 "Indian Ocean", "Atlantic Ocean", "Pacific Ocean",
                 "Arctic Ocean", 1),
                ("How many days are there in a week?", "5", "6", "7", "8", 3),
                ("What is 12 x 3?", "24", "36", "33", "39", 2),
                ("Which planet is closest to the Sun?", "Venus", "Mercury",
                 "Earth", "Mars", 2),
                ("What does ATM stand for?", "Automated Teller Machine",
                 "Annual Transfer Method", "Account Tax Manager",
                 "Automatic Trade Market", 1)):
            db.execute(
                "INSERT INTO brain_questions (question, a, b, c, d, correct, "
                "points) VALUES (?,?,?,?,?,?,200)", (q, a, b, c, d, correct))


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
        "topups": {"level": "INTEGER NOT NULL DEFAULT 0"},
        "vouchers": {"issued_to": "INTEGER"},
        "otps": {"code_plain": "TEXT NOT NULL DEFAULT ''"},
        # Older deployed databases may already have this table without the
        # newer fields. Add missing columns safely before the admin POST uses them.
        "topup_details": {
            "Bank_name": "TEXT NOT NULL DEFAULT ''",
            "Reference": "TEXT NOT NULL DEFAULT ''",
            "Amount": "TEXT NOT NULL DEFAULT ''",
            "PayShap": "TEXT NOT NULL DEFAULT ''",
            "BA_number": "TEXT NOT NULL DEFAULT ''",
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
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    db.commit()

    admin_user = os.environ.get("ADMIN_USER", "admin")
    admin_pass = os.environ.get("ADMIN_PASS", "admin12345")

    # Create admin account
    if not db.execute(
        "SELECT 1 FROM users WHERE is_admin=1"
    ).fetchone():

        # Admin does not use a real Bank Account  number
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

    # Phase 1 games: starter settings, prizes and questions
    seed_games(db)

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
        "GAMES": GAMES,
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

        # Validate Bank Account  number (exactly 11 digits)
        elif (not serial_number.isascii()
                or not serial_number.isdigit()
                or len(serial_number) != 11):
            error = "Bank Account  number must contain exactly 11 digits."

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

        # Check duplicate Bank Account  number
        if not error:
            existing_serial = db.execute(
                "SELECT 1 FROM users WHERE serial_number=?",
                (serial_number,)
            ).fetchone()

            if existing_serial:
                error = "That Bank Account  number is already registered."

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
                "That cellphone number or Bank Account  number is already registered.",
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
                reward=reward)


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
        # All tasks done: the day counts as checked in (history).
        _done, streak = checkin_state(user)
        inserted = db.execute(
            "INSERT OR IGNORE INTO checkins (user_id, day, reward, bonus) "
            "VALUES (?,?,?,0)", (user["id"], day, earned)).rowcount
        if inserted == 1:
            finished = True
            db.execute("UPDATE users SET streak=?, last_checkin=? WHERE id=?",
                       (streak, day, user["id"]))
            # Daily Streak is not part of Phase 1: no streak celebration.
    db.commit()

    points = db.execute("SELECT points FROM users WHERE id=?",
                        (user["id"],)).fetchone()["points"]
    return jsonify(ok=True, reward_text=money(reward),
                   balance=money(points), completed=completed, total=total,
                   earned_text=money(earned), done=finished,
                   milestone=milestone, streak=streak)


# ---------------------------------------------------------------------------
# Admin photo posts (shown on the dashboard)
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
    """Create a pending top-up request after the member selects a level and amount."""
    db = get_db()
    user = current_user()
    try:
        level = int(request.form.get("level", "0"))
    except (TypeError, ValueError):
        return jsonify(ok=False, message="Choose a valid level."), 400

    cents = parse_amount(request.form.get("amount"),
                         TOPUP_MIN_CENTS, TOPUP_MAX_CENTS)
    if cents is None:
        return jsonify(
            ok=False,
            message=(f"Enter an amount between {money(TOPUP_MIN_CENTS)} "
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
        "INSERT INTO topups (user_id, amount, level, note, status, created_at) "
        "VALUES (?,?,?, '', 'PENDING', ?)",
        (user["id"], cents, level, now_iso()))
    db.commit()
    return jsonify(ok=True, level=level, amount=money(cents),
                   message=f"Request for Level {level} sent.")


@app.route("/topup", methods=["GET", "POST"])
@login_required
def topup():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        cents = parse_amount(request.form.get("amount"),
                             TOPUP_MIN_CENTS, TOPUP_MAX_CENTS)
        note = request.form.get("note", "").strip()[:100]
        try:
            level = int(request.form.get("level", 0))
        except ValueError:
            level = 0

        if cents is None:
            flash(f"Enter an amount between {money(TOPUP_MIN_CENTS)} and "
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
            "INSERT INTO topups (user_id, amount, level, note, status, "
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
            ("Bank Name", val("Bank_name")),
            ("Reference", val("Reference")),
            ("Amount", val("Amount")),
            ("PayShap", val("PayShap")),
            ("Bank Account  number", val("BA_number")),
        ]
        req_rows = [
            ("Level", f"Level {active['level']}"),
            ("Amount", money(active["amount"])),
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
        cents = parse_amount(request.form.get("amount"), WITHDRAW_MIN_CENTS)
        if cents is None:
            flash(f"Enter a valid amount (minimum {money(WITHDRAW_MIN_CENTS)}).",
                  "error")
            return redirect(url_for("withdraw"))

        if not debit(db, user["id"], cents, "WITHDRAW",
                     "Withdrawal request (pending)"):
            db.rollback()
            flash("Insufficient play balance.", "error")
            return redirect(url_for("withdraw"))

        db.execute(
            "INSERT INTO withdrawals (user_id, amount, bank_name, "
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
        "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
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
        cents = parse_amount(request.form.get("amount"), POT_MIN_CENTS)
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
              for key in ("Bank_name", "Reference", "Amount",
                          "PayShap", "BA_number")}
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
        "INSERT INTO topup_details (user_id, Bank_name, Reference, "
        "Amount, PayShap, BA_number, message, flag, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "Bank_name=excluded.Bank_name, "
        "Reference=excluded.Reference, "
        "Amount=excluded.Amount, "
        "PayShap=excluded.PayShap, "
        "BA_number=excluded.BA_number, message=excluded.message, "
        "flag=excluded.flag, updated_at=excluded.updated_at",
        (target_id, fields["Bank_name"], fields["Reference"],
         fields["Amount"], fields["PayShap"],
         fields["BA_number"], message, flag, now_iso()))
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
    cents = parse_amount(request.form.get("amount"), 1, ADMIN_ADD_MAX_CENTS)
    note = request.form.get("note", "").strip()[:100]

    if cents is None:
        flash(f"Enter an amount from R0.01 to {money(ADMIN_ADD_MAX_CENTS)}.",
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
        credit(db, w["user_id"], w["amount"], "WITHDRAW_REFUND",
               f"Withdrawal #{withdrawal_id} rejected - refunded")
    db.commit()
    flash(f"Withdrawal #{withdrawal_id} {new_status.lower()}.", "ok")
    return redirect(url_for("admin"))


# ============================================================================
# PHASE 1 GAMES: Spin & Win, Brain Battle, Mystery Envelope
# ============================================================================
# Fair-play rules built into the code:
#   * Every result is decided on the SERVER (secrets module). The browser only
#     shows the answer, it can never choose or change it.
#   * Limits, stock and rewards are checked inside one write transaction
#     (BEGIN IMMEDIATE), so a double-click or two phones cannot beat them.
#   * Every reward goes through credit() into the points wallet / ledger and
#     into game_plays (reward history + admin winner records).
#   * The odds, stock and rules are shown to members on each game page.
#   * The admin sets prizes, odds, stock, limits and schedule. There is no
#     feature to secretly change the outcome of one member.

def _int(value, default=0, lo=0, hi=10 ** 9):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def begin_write(db):
    """Start a write transaction that locks the database right away."""
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN IMMEDIATE")


def game_status(row):
    now = now_iso()[:16]
    if not row["active"]:
        return "PAUSED"
    if row["starts_at"] and now < row["starts_at"]:
        return "UPCOMING"
    if row["ends_at"] and now >= row["ends_at"]:
        return "CLOSED"
    return "OPEN"


def plays_used(db, user_id, row):
    if row["game"] == "BRAIN":
        sql = ("SELECT COUNT(*) FROM brain_rounds "
               "WHERE user_id=? AND event_no=?")
        args = [user_id, row["event_no"]]
    else:
        sql = ("SELECT COUNT(*) FROM game_plays "
               "WHERE user_id=? AND event_no=? AND game=?")
        args = [user_id, row["event_no"], row["game"]]
    if row["period"] == "DAY":
        sql += " AND substr(created_at,1,10)=?"
        args.append(today().isoformat())
    return db.execute(sql, args).fetchone()[0]


def game_gate(db, user, game):
    """Can this member play now? Returns a dict with the settings row, the
    status, the number of plays left and an error text (None = can play)."""
    row = db.execute("SELECT * FROM games WHERE game=?", (game,)).fetchone()
    status = game_status(row)
    used = plays_used(db, user["id"], row)
    left = max(0, row["max_plays"] - used)
    error = None
    if status == "PAUSED":
        error = "This game is closed."
    elif status == "UPCOMING":
        error = f"This game starts on {row['starts_at']} UTC."
    elif status == "CLOSED":
        error = "This game has closed."
    elif user["level"] < row["min_level"]:
        error = f"You need Level {row['min_level']} or higher to play."
    elif left <= 0:
        error = ("You already claimed your envelope." if game == "ENVELOPE"
                 else "You have used all your plays.")
    return {"g": row, "status": status, "left": left, "used": used,
            "error": error}


def log_play(db, user_id, game, event_no, result, kind, amount=0, voucher=""):
    db.execute(
        "INSERT INTO game_plays (user_id, game, event_no, result, kind, "
        "amount, voucher, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (user_id, game, event_no, result, kind, amount, voucher, now_iso()))


def issue_voucher(db, user_id, level):
    """A fresh level code that only this member can redeem."""
    code = new_voucher_code(level)
    db.execute(
        "INSERT INTO vouchers (code, level, issued_to, created_at) "
        "VALUES (?,?,?,?)", (code, level, user_id, now_iso()))
    return code


def prize_text(p):
    if p["kind"] == "POINTS":
        return f"{money(p['value'])} points"
    if p["kind"] == "VOUCHER":
        return f"Level {p['value']} voucher"
    return ""


def _window_text(row):
    if row["starts_at"] and row["ends_at"]:
        return f"{row['starts_at']} to {row['ends_at']} UTC"
    if row["starts_at"]:
        return f"from {row['starts_at']} UTC"
    if row["ends_at"]:
        return f"until {row['ends_at']} UTC"
    return "no closing time set"


def _limit_text(row):
    per = "per day" if row["period"] == "DAY" else "per event"
    return f"{row['max_plays']} play(s) per member {per}"


# ---------------------------------------------------------------- hub -------

@app.route("/games")
@login_required
def games_hub():
    db = get_db()
    user = current_user()
    meta = (
        ("SPIN", "🎰", "Spin for points, vouchers and prizes.", "spin_page"),
        ("BRAIN", "🧠", "Answer questions and earn points for every correct answer.", "brain_page"),
        ("ENVELOPE", "✉️", "Open your envelope.", "envelope_page"),
    )
    cards = []
    for key, emoji, blurb, endpoint in meta:
        info = game_gate(db, user, key)
        cards.append({
            "emoji": emoji, "title": GAMES[key], "blurb": blurb,
            "url": url_for(endpoint), "status": info["status"],
            "status_text": STATUS_TEXT[info["status"]],
            "left": info["left"], "error": info["error"],
        })
    history = db.execute(
        "SELECT * FROM game_plays WHERE user_id=? ORDER BY id DESC LIMIT 30",
        (user["id"],)).fetchall()
    return page(GAMES_BODY, cards=cards, history=history)


# --------------------------------------------------------- Spin & Win -------

@app.route("/games/spin")
@login_required
def spin_page():
    db = get_db()
    user = current_user()
    info = game_gate(db, user, "SPIN")
    prizes = db.execute(
        "SELECT * FROM spin_prizes WHERE enabled=1 ORDER BY weight DESC, id"
    ).fetchall()
    avail = {p["id"] for p in prizes
             if p["weight"] > 0 and (p["stock"] is None or p["won"] < p["stock"])}
    total = sum(p["weight"] for p in prizes if p["id"] in avail)
    odds = []
    for p in prizes:
        ok = p["id"] in avail
        odds.append({
            "label": p["label"], "what": prize_text(p),
            "chance": (round(p["weight"] * 100 / total, 1)
                       if ok and total else 0),
            "left": (None if p["stock"] is None
                     else max(0, p["stock"] - p["won"])),
            "sold_out": not ok,
        })
    mine = db.execute(
        "SELECT * FROM game_plays WHERE user_id=? AND game='SPIN' "
        "ORDER BY id DESC LIMIT 10", (user["id"],)).fetchall()
    labels = [p["label"] for p in prizes] or ["..."]
    return page(SPIN_BODY, info=info, gm=info["g"], odds=odds, mine=mine,
                labels=labels, window=_window_text(info["g"]),
                limit_text=_limit_text(info["g"]), user=user,
                status_text=STATUS_TEXT[info["status"]])


@app.route("/games/spin/play", methods=["POST"])
@login_required
def spin_play():
    db = get_db()
    user = current_user()
    begin_write(db)
    info = game_gate(db, user, "SPIN")
    if info["error"]:
        db.rollback()
        return jsonify(ok=False, error=info["error"]), 400

    prizes = db.execute(
        "SELECT * FROM spin_prizes WHERE enabled=1 AND weight>0 "
        "AND (stock IS NULL OR won<stock)").fetchall()
    if not prizes:
        db.rollback()
        return jsonify(ok=False, error="No prizes are available right now."), 400

    roll = secrets.randbelow(sum(p["weight"] for p in prizes))
    pick = prizes[-1]
    for p in prizes:
        if roll < p["weight"]:
            pick = p
            break
        roll -= p["weight"]

    event_no = info["g"]["event_no"]
    amount, voucher = 0, ""
    if pick["kind"] == "POINTS":
        amount = pick["value"]
        credit(db, user["id"], amount, "SPIN", f"Spin & Win: {pick['label']}")
        text = f"You won {money(amount)} in points!"
    elif pick["kind"] == "VOUCHER":
        voucher = issue_voucher(db, user["id"], pick["value"])
        text = (f"You won a Level {pick['value']} voucher! Code: {voucher}. "
                "Redeem it on the Upgrade page.")
    elif pick["kind"] == "ITEM":
        text = f"You won: {pick['label']}! The admin will contact you."
    else:
        text = "No prize this time. Better luck next time!"

    if pick["kind"] != "NONE":
        db.execute("UPDATE spin_prizes SET won=won+1 WHERE id=?", (pick["id"],))
    log_play(db, user["id"], "SPIN", event_no, pick["label"], pick["kind"],
             amount, voucher)
    db.commit()

    points = db.execute("SELECT points FROM users WHERE id=?",
                        (user["id"],)).fetchone()["points"]
    return jsonify(ok=True, label=pick["label"], kind=pick["kind"],
                   text=text, balance=money(points), left=info["left"] - 1)


# ------------------------------------------------------- Brain Battle -------

@app.route("/games/brain")
@login_required
def brain_page():
    db = get_db()
    user = current_user()
    info = game_gate(db, user, "BRAIN")
    open_round = db.execute(
        "SELECT id FROM brain_rounds WHERE user_id=? AND status='OPEN' "
        "ORDER BY id DESC", (user["id"],)).fetchone()
    pool = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(MAX(points),0) AS mx "
        "FROM brain_questions WHERE enabled=1").fetchone()
    rounds = db.execute(
        "SELECT * FROM brain_rounds WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (user["id"],)).fetchall()
    return page(BRAIN_BODY, info=info, gm=info["g"], open_round=open_round,
                pool=pool, rounds=rounds, window=_window_text(info["g"]),
                limit_text=_limit_text(info["g"]),
                status_text=STATUS_TEXT[info["status"]])


@app.route("/games/brain/start", methods=["POST"])
@login_required
def brain_start():
    db = get_db()
    user = current_user()
    begin_write(db)

    # Resume a round that was started but not finished (uses no new play).
    open_round = db.execute(
        "SELECT id FROM brain_rounds WHERE user_id=? AND status='OPEN' "
        "ORDER BY id DESC", (user["id"],)).fetchone()
    if open_round:
        db.rollback()
        return redirect(url_for("brain_round", rid=open_round["id"]))

    info = game_gate(db, user, "BRAIN")
    if info["error"]:
        db.rollback()
        flash(info["error"], "error")
        return redirect(url_for("brain_page"))

    ids = [r["id"] for r in db.execute(
        "SELECT id FROM brain_questions WHERE enabled=1 "
        "ORDER BY RANDOM() LIMIT ?", (info["g"]["questions_per_round"],))]
    if not ids:
        db.rollback()
        flash("No questions are available right now.", "error")
        return redirect(url_for("brain_page"))

    cur = db.execute(
        "INSERT INTO brain_rounds (user_id, event_no, question_ids, created_at) "
        "VALUES (?,?,?,?)",
        (user["id"], info["g"]["event_no"], ",".join(map(str, ids)), now_iso()))
    db.commit()
    return redirect(url_for("brain_round", rid=cur.lastrowid))


def _round_questions(db, rnd):
    ids = [int(x) for x in rnd["question_ids"].split(",") if x]
    if not ids:
        return []
    rows = {r["id"]: r for r in db.execute(
        "SELECT * FROM brain_questions WHERE id IN (%s)"
        % ",".join("?" * len(ids)), ids)}
    return [rows[i] for i in ids if i in rows]


@app.route("/games/brain/round/<int:rid>", methods=["GET", "POST"])
@login_required
def brain_round(rid):
    db = get_db()
    user = current_user()
    rnd = db.execute("SELECT * FROM brain_rounds WHERE id=? AND user_id=?",
                     (rid, user["id"])).fetchone()
    if rnd is None:
        abort(404)
    questions = _round_questions(db, rnd)

    if request.method == "POST":
        if rnd["status"] != "OPEN":
            return redirect(url_for("brain_round", rid=rid))
        picked, score, right = [], 0, 0
        for q in questions:
            a = _int(request.form.get(f"q{q['id']}"), 0, 0, 4)
            picked.append(f"{q['id']}:{a}")
            if a == q["correct"]:
                score += q["points"]
                right += 1
        begin_write(db)
        claimed = db.execute(
            "UPDATE brain_rounds SET status='DONE', answers=?, score=?, "
            "correct_n=?, finished_at=? WHERE id=? AND status='OPEN'",
            (",".join(picked), score, right, now_iso(), rid)).rowcount
        if claimed == 1:
            result = f"{right}/{len(questions)} correct"
            if score > 0:
                credit(db, user["id"], score, "BRAIN",
                       f"Brain Battle round #{rid}: {result}")
            log_play(db, user["id"], "BRAIN", rnd["event_no"], result,
                     "POINTS" if score else "NONE", score)
            db.commit()
            flash(f"You got {result} and earned {money(score)}.", "ok")
        else:
            db.rollback()
        return redirect(url_for("brain_round", rid=rid))

    answers = {}
    for part in (rnd["answers"] or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            answers[_int(k)] = _int(v)
    return page(BRAIN_ROUND_BODY, rnd=rnd, questions=questions,
                answers=answers)


# ------------------------------------------------- Mystery Envelope --------

@app.route("/games/envelope")
@login_required
def envelope_page():
    db = get_db()
    user = current_user()
    info = game_gate(db, user, "ENVELOPE")
    gm = info["g"]
    claimed = db.execute(
        "SELECT * FROM game_plays WHERE user_id=? AND game='ENVELOPE' "
        "AND event_no=? ORDER BY id DESC LIMIT 1",
        (user["id"], gm["event_no"])).fetchone()
    return page(ENVELOPE_BODY, info=info, gm=gm, claimed=claimed,
                window=_window_text(gm), user=user,
                status_text=STATUS_TEXT[info["status"]])


@app.route("/games/envelope/open", methods=["POST"])
@login_required
def envelope_open():
    db = get_db()
    user = current_user()
    begin_write(db)
    info = game_gate(db, user, "ENVELOPE")
    if info["error"]:
        db.rollback()
        return jsonify(ok=False, error=info["error"]), 400

    gm = info["g"]
    reward = gm["min_reward"]
    voucher, level = "", gm["bonus_level"]

    # Bonus voucher: published chance, only while the voucher stock lasts.
    if (gm["bonus_chance"] > 0 and gm["bonus_stock"] > 0
            and secrets.randbelow(100) < gm["bonus_chance"]):
        took = db.execute(
            "UPDATE games SET bonus_stock=bonus_stock-1 "
            "WHERE game='ENVELOPE' AND bonus_stock>0").rowcount
        if took == 1:
            voucher = issue_voucher(db, user["id"], level)

    if reward > 0:
        credit(db, user["id"], reward, "ENVELOPE",
               "Mystery Envelope.")
    result = money(reward) + " points"
    if voucher:
        result += f" + Level {level} voucher"
    log_play(db, user["id"], "ENVELOPE", gm["event_no"], result,
             "VOUCHER" if voucher else ("POINTS" if reward else "NONE"),
             reward, voucher)
    db.commit()

    points = db.execute("SELECT points FROM users WHERE id=?",
                        (user["id"],)).fetchone()["points"]
    text = f"Your envelope held {money(reward)}!"
    if voucher:
        text += (f" Bonus: a Level {level} voucher. Code: {voucher}. "
                 "Redeem it on the Upgrade page.")
    return jsonify(ok=True, text=text, balance=money(points),
                   voucher=voucher)


# ------------------------------------------------------ Admin controls -----

@app.route("/admin/games")
@admin_required
def admin_games():
    db = get_db()
    rows = {r["game"]: r for r in db.execute("SELECT * FROM games")}
    cards = [{"key": k, "title": v, "g": rows[k],
              "status": STATUS_TEXT[game_status(rows[k])]}
             for k, v in GAMES.items() if k in rows]
    prizes = db.execute("SELECT * FROM spin_prizes ORDER BY id").fetchall()
    questions = db.execute(
        "SELECT * FROM brain_questions ORDER BY id").fetchall()
    records = db.execute(
        "SELECT p.*, u.username FROM game_plays p "
        "JOIN users u ON u.id = p.user_id ORDER BY p.id DESC LIMIT 100"
    ).fetchall()
    return page(ADMIN_GAMES_BODY, cards=cards, prizes=prizes,
                questions=questions, records=records,
                PRIZE_KINDS=PRIZE_KINDS)


@app.route("/admin/games/<game>/settings", methods=["POST"])
@admin_required
def admin_game_settings(game):
    if game not in GAMES:
        abort(404)
    f = request.form

    def when(name):
        v = f.get(name, "").strip().replace("T", " ")[:16]
        return v if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", v) else ""

    starts, ends = when("starts_at"), when("ends_at")
    if starts and ends and ends <= starts:
        flash("The closing time must be after the start time.", "error")
        return redirect(url_for("admin_games"))

    max_plays = _int(f.get("max_plays"), 1, 1, 1000)
    period = f.get("period") if f.get("period") in ("EVENT", "DAY") else "EVENT"
    if game == "ENVELOPE":              # one envelope per member per event
        max_plays, period = 1, "EVENT"

    db = get_db()
    db.execute(
        "UPDATE games SET active=?, starts_at=?, ends_at=?, max_plays=?, "
        "period=?, min_level=? WHERE game=?",
        (1 if f.get("active") else 0, starts, ends, max_plays, period,
         _int(f.get("min_level"), 0, 0, max(LEVELS)), game))

    if game == "ENVELOPE":
        reward = parse_amount(f.get("min_reward"), 0, ADMIN_ADD_MAX_CENTS)
        if reward is None:
            db.rollback()
            flash("Enter the guaranteed reward in Rand (0 or more).", "error")
            return redirect(url_for("admin_games"))
        db.execute(
            "UPDATE games SET min_reward=?, bonus_level=?, bonus_stock=?, "
            "bonus_chance=? WHERE game='ENVELOPE'",
            (reward, _int(f.get("bonus_level"), 1, 1, max(LEVELS)),
             _int(f.get("bonus_stock"), 0, 0, 100000),
             _int(f.get("bonus_chance"), 0, 0, 100)))
    if game == "BRAIN":
        db.execute("UPDATE games SET questions_per_round=? WHERE game='BRAIN'",
                   (_int(f.get("questions_per_round"), 5, 1, 50),))
    db.commit()
    flash(f"{GAMES[game]} settings saved.", "ok")
    return redirect(url_for("admin_games"))


@app.route("/admin/games/<game>/new-event", methods=["POST"])
@admin_required
def admin_game_new_event(game):
    if game not in GAMES:
        abort(404)
    db = get_db()
    db.execute("UPDATE games SET event_no=event_no+1 WHERE game=?", (game,))
    db.commit()
    flash(f"New {GAMES[game]} event started. Everyone's play limit is reset. "
          "The old winner records are kept.", "ok")
    return redirect(url_for("admin_games"))


@app.route("/admin/games/prize", methods=["POST"])
@admin_required
def admin_prize_save():
    f = request.form
    label = f.get("label", "").strip()[:60]
    kind = f.get("kind", "NONE")
    if not label or kind not in PRIZE_KINDS:
        flash("A prize needs a name and a type.", "error")
        return redirect(url_for("admin_games"))

    raw = f.get("value", "").strip()
    if kind == "POINTS":
        value = parse_amount(raw, 1, ADMIN_ADD_MAX_CENTS)
        if value is None:
            flash("Points prizes need an amount in Rand, for example 20.00.",
                  "error")
            return redirect(url_for("admin_games"))
    elif kind == "VOUCHER":
        value = _int(raw, 0, 0, 99)
        if value not in LEVELS:
            flash("Voucher prizes need a level from 1 to 7 in the value box.",
                  "error")
            return redirect(url_for("admin_games"))
    else:
        value = 0

    weight = _int(f.get("weight"), 0, 0, 1000000)
    stock_raw = f.get("stock", "").strip()
    stock = None if stock_raw == "" else _int(stock_raw, 0, 0, 10 ** 7)
    enabled = 1 if f.get("enabled") else 0
    pid = _int(f.get("id"), 0)

    db = get_db()
    if pid:
        db.execute(
            "UPDATE spin_prizes SET label=?, kind=?, value=?, weight=?, "
            "stock=?, enabled=? WHERE id=?",
            (label, kind, value, weight, stock, enabled, pid))
        flash("Prize saved.", "ok")
    else:
        db.execute(
            "INSERT INTO spin_prizes (label, kind, value, weight, stock, "
            "enabled) VALUES (?,?,?,?,?,?)",
            (label, kind, value, weight, stock, enabled))
        flash("Prize added.", "ok")
    db.commit()
    return redirect(url_for("admin_games"))


@app.route("/admin/games/prize/<int:prize_id>/delete", methods=["POST"])
@admin_required
def admin_prize_delete(prize_id):
    db = get_db()
    db.execute("DELETE FROM spin_prizes WHERE id=?", (prize_id,))
    db.commit()
    flash("Prize deleted.", "ok")
    return redirect(url_for("admin_games"))


@app.route("/admin/games/question", methods=["POST"])
@admin_required
def admin_question_add():
    f = request.form
    texts = [f.get(k, "").strip()[:200] for k in ("question", "a", "b", "c", "d")]
    correct = _int(f.get("correct"), 0, 0, 4)
    points = parse_amount(f.get("points"), 0, ADMIN_ADD_MAX_CENTS)
    if not all(texts) or correct not in (1, 2, 3, 4) or points is None:
        flash("Fill in the question, 4 answers, the correct answer and the "
              "points in Rand.", "error")
        return redirect(url_for("admin_games"))
    db = get_db()
    db.execute(
        "INSERT INTO brain_questions (question, a, b, c, d, correct, points) "
        "VALUES (?,?,?,?,?,?,?)", (*texts, correct, points))
    db.commit()
    flash("Question added.", "ok")
    return redirect(url_for("admin_games"))


@app.route("/admin/games/question/<int:qid>/<action>", methods=["POST"])
@admin_required
def admin_question_action(qid, action):
    if action not in ("toggle", "delete"):
        abort(404)
    db = get_db()
    if action == "delete":
        db.execute("DELETE FROM brain_questions WHERE id=?", (qid,))
    else:
        db.execute("UPDATE brain_questions SET enabled=1-enabled WHERE id=?",
                   (qid,))
    db.commit()
    flash("Question updated.", "ok")
    return redirect(url_for("admin_games"))


# ---------------------------------------------------- Games templates ------

GAMES_BODY = """
<style>
.games-hero{position:relative;overflow:hidden;padding:28px!important;border:1px solid #293b65!important;background:radial-gradient(circle at 90% 0%,rgba(129,92,255,.30),transparent 34%),linear-gradient(135deg,#10172d,#1b2450 58%,#251849)!important;color:#fff}
.games-hero h1{font-size:clamp(28px,4vw,38px);margin:0 0 8px;letter-spacing:-1px}
.games-hero p{max-width:680px;color:#c9d2f4;margin:0}
.games-kicker{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:800;letter-spacing:1.4px;text-transform:uppercase;color:#c8baff;margin-bottom:12px}
.games-kicker:before{content:"";width:7px;height:7px;border-radius:50%;background:#9c82ff;box-shadow:0 0 12px #9c82ff}
.games-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(235px,1fr));gap:18px;margin:20px 0 24px}
.game-tile{position:relative;overflow:hidden;display:flex;flex-direction:column;min-height:280px;padding:22px;border-radius:20px;border:1px solid #303b5c;background:linear-gradient(155deg,#1a2340,#111827 72%);color:#fff;box-shadow:0 12px 30px rgba(0,0,0,.16);transition:transform .18s ease,border-color .18s ease}
.game-tile:hover{transform:translateY(-4px);border-color:#8272ff}
.game-tile:after{content:"";position:absolute;width:150px;height:150px;border-radius:50%;right:-65px;top:-65px;background:rgba(130,104,255,.13);pointer-events:none}
.game-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.game-icon{display:grid;place-items:center;width:66px;height:66px;border-radius:18px;font-size:35px;background:linear-gradient(135deg,#45338e,#252c5c);border:1px solid #6252a8;box-shadow:inset 0 1px 0 rgba(255,255,255,.15)}
.game-tile:nth-child(2) .game-icon{background:linear-gradient(135deg,#0d776f,#17435a);border-color:#258f87}
.game-tile:nth-child(3) .game-icon{background:linear-gradient(135deg,#a45a19,#61371d);border-color:#c17a32}
.game-status{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.7px;padding:6px 9px;border-radius:999px;background:#27314b;color:#d7def6;white-space:nowrap}
.game-status.live{background:#123e35;color:#83f0c3}
.game-tile h2{font-size:21px;line-height:1.25;margin:20px 0 8px;color:#fff}
.game-tile p{font-size:14px;line-height:1.6;color:#b7c2dc;margin:0 0 18px}
.game-bottom{margin-top:auto;display:flex;align-items:center;justify-content:space-between;gap:10px}
.game-plays{font-size:12px;color:#aab6d4}
.game-action{display:inline-flex;align-items:center;justify-content:center;gap:8px;text-decoration:none!important;padding:11px 15px;border-radius:11px;background:linear-gradient(135deg,#8d76ff,#6b54dc);color:#fff!important;font-weight:800;font-size:13px;box-shadow:0 6px 16px rgba(108,82,220,.25)}
.game-action:hover{filter:brightness(1.1)}
.game-action.disabled{opacity:.45;pointer-events:none}
.game-alert{font-size:12px!important;color:#ffcf9d!important;background:rgba(158,91,32,.14);border:1px solid rgba(216,146,70,.25);padding:9px 11px;border-radius:10px;margin:0 0 14px!important}
.game-history{overflow-x:auto}
.game-history table{min-width:600px}
@media(max-width:560px){.games-hero{padding:22px!important}.game-tile{min-height:250px;padding:18px}.game-tile h2{margin-top:16px}}
</style>
<div class="card games-hero">
  <div class="games-kicker">Vedo Play Zone</div>
  <h1>Pick your game. Play for rewards.</h1>
  <p>Spin, test your knowledge, or open a mystery envelope. Your game results and eligible rewards are added to your Vedo balance.</p>
</div>

<div class="games-grid">
{% for c in cards %}
  <article class="game-tile">
    <div class="game-top">
      <div class="game-icon">{{ c.emoji }}</div>
      <span class="game-status {{ 'live' if c.status == 'OPEN' else '' }}">{{ c.status_text }}</span>
    </div>
    <h2>{{ c.title }}</h2>
    <p>{{ c.blurb }}</p>
    {% if c.error %}<p class="game-alert">{{ c.error }}</p>{% endif %}
    <div class="game-bottom">
      <span class="game-plays">{% if c.status == 'OPEN' %}{{ c.left }} play(s) left{% else %}Check availability{% endif %}</span>
      <a class="game-action {{ 'disabled' if c.status != 'OPEN' else '' }}" href="{{ c.url }}">Play now <span aria-hidden="true">→</span></a>
    </div>
  </article>
{% endfor %}
</div>

<div class="card">
  <h2>🏆 My reward history</h2>
  <p class="muted small">Your recent game results and rewards.</p>
  {% if history %}
  <div class="game-history">
  <table>
    <tr><th>Date</th><th>Game</th><th>Result</th><th>Points</th><th>Voucher</th></tr>
    {% for h in history %}
    <tr>
      <td>{{ h['created_at'][:16] }}</td>
      <td>{{ GAMES[h['game']] }}</td>
      <td>{{ h['result'] }}</td>
      <td class="{{ 'pos' if h['amount'] > 0 }}">{{ money(h['amount']) if h['amount'] > 0 else '-' }}</td>
      <td>{% if h['voucher'] %}<code>{{ h['voucher'] }}</code>{% else %}-{% endif %}</td>
    </tr>
    {% endfor %}
  </table>
  </div>
  {% else %}
  <p class="muted">No game rewards yet. Choose a game above to get started.</p>
  {% endif %}
</div>
"""

SPIN_BODY = """
<style>
.spin-layout{display:grid;grid-template-columns:minmax(280px,1fr) minmax(260px,.8fr);gap:22px;align-items:center}
.spin-stage{display:flex;flex-direction:column;align-items:center;padding:18px 8px 24px;overflow:hidden}
.wheel-shell{position:relative;width:min( min(78vw,390px),100%);aspect-ratio:1;margin:16px auto 22px;filter:drop-shadow(0 14px 20px #0003)}
.wheel-canvas{display:block;width:100%;height:100%;border-radius:50%;border:8px solid #f5d77a;box-shadow:inset 0 0 0 4px #342456;transform:rotate(0deg);transition:transform 4.2s cubic-bezier(.12,.72,.08,1);background:#241b45}
.wheel-pointer{position:absolute;z-index:2;top:-8px;left:50%;transform:translateX(-50%);width:0;height:0;border-left:19px solid transparent;border-right:19px solid transparent;border-top:43px solid #fff1a7;filter:drop-shadow(0 3px 3px #0008)}
.wheel-hub{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:76px;height:76px;border-radius:50%;display:grid;place-items:center;background:radial-gradient(circle at 35% 25%,#fff4b8,#f5b92d 55%,#9d4e16);border:5px solid #fff2b1;color:#38204f;font-weight:1000;font-size:13px;box-shadow:0 4px 12px #0006;pointer-events:none}
.spin-title{text-align:center;font-size:13px;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:var(--muted)}
.spin-action{min-width:180px;font-size:17px!important;padding:14px 28px!important;border-radius:999px!important;box-shadow:0 7px 0 #0002,0 12px 24px #0002}
.spin-prize{font-size:18px;font-weight:800;text-align:center;padding:14px;border-radius:14px;background:var(--s2);border:1px solid var(--line);min-height:52px}
@media(max-width:720px){.spin-layout{grid-template-columns:1fr}.wheel-shell{width:min(78vw,340px)}}
</style>
<div class="card note">
<h1>🎰 Vedo Spin &amp; Win</h1><p class="muted"><a href="{{ url_for('games_hub') }}">← All games</a></p>
<p><span class="badge {{ 'ok' if info.status == 'OPEN' else 'wait' }}">{{ status_text }}</span> <span class="muted small">{{ window }} · {{ limit_text }}{% if gm['min_level'] %} · Level {{ gm['min_level'] }}+{% endif %}</span></p>
<div class="spin-layout"><div class="spin-stage">
<div class="spin-title">Give the wheel a spin!</div>
<div class="wheel-shell"><div class="wheel-pointer"></div><canvas id="realwheel" class="wheel-canvas" width="600" height="600" aria-label="Prize wheel"></canvas><div class="wheel-hub">VEDO<br>PLAY</div></div>
<button type="button" class="primary spin-action" id="spinbtn" {% if info.error %}disabled{% endif %}>SPIN NOW</button>
<p class="muted small">Tap the button and watch the wheel turn. The server confirms your result.</p>
</div><div>
<div class="spin-prize" id="spinout" aria-live="polite">{{ 'Ready when you are!' if not info.error else info.error }}</div>
<p class="muted">Plays left: <b id="left">{{ info.left }}</b><br>Points balance: <b id="bal">{{ money(user['points']) }}</b></p>
{% if info.error %}<p class="muted small">{{ info.error }}</p>{% endif %}
<h3>Prize odds</h3><p class="muted small">Odds are shown before you play. Sold-out prizes are removed from the draw.</p>
<div style="max-height:300px;overflow:auto"><table><tr><th>Prize</th><th>Chance</th><th>Left</th></tr>{% for o in odds %}<tr><td>{{ o.label }}<br><span class="muted small">{{ o.what or '-' }}</span></td><td>{% if o.sold_out %}Unavailable{% else %}{{ o.chance }}%{% endif %}</td><td>{{ 'Unlimited' if o.left is none else o.left }}</td></tr>{% endfor %}</table></div>
</div></div></div>
<div class="card"><h2>🏆 My spins</h2>{% if mine %}<div style="overflow-x:auto"><table><tr><th>Date</th><th>Result</th><th>Voucher</th></tr>{% for m in mine %}<tr><td>{{ m['created_at'][:16] }}</td><td>{{ m['result'] }}</td><td>{% if m['voucher'] %}<code>{{ m['voucher'] }}</code>{% else %}—{% endif %}</td></tr>{% endfor %}</table></div>{% else %}<p class="muted">Your spin history will appear here.</p>{% endif %}</div>
<script>
(function(){
 const canvas=document.getElementById('realwheel'),ctx=canvas.getContext('2d'),btn=document.getElementById('spinbtn'),out=document.getElementById('spinout');
 const labels={{ labels|tojson }},csrf={{ csrf_token()|tojson }}; if(!canvas||!btn)return;
 const colors=['#6b4eff','#f04e8a','#ffb52e','#16a995','#3878f5','#a44be0','#ed6c37','#1a9cce'];
 const step=Math.PI*2/Math.max(labels.length,1); ctx.clearRect(0,0,600,600);
 labels.forEach((label,i)=>{let a=-Math.PI/2+i*step;ctx.beginPath();ctx.moveTo(300,300);ctx.arc(300,300,284,a,a+step);ctx.closePath();ctx.fillStyle=colors[i%colors.length];ctx.fill();ctx.strokeStyle='#fff4c4';ctx.lineWidth=4;ctx.stroke();ctx.save();ctx.translate(300,300);ctx.rotate(a+step/2);ctx.textAlign='right';ctx.fillStyle='#fff';ctx.font='bold '+(labels.length>8?'19':'23')+'px system-ui,sans-serif';let txt=String(label);if(txt.length>23)txt=txt.slice(0,21)+'…';ctx.shadowColor='#0007';ctx.shadowBlur=3;ctx.fillText(txt,250,7,205);ctx.restore();});
 let busy=false;
 btn.addEventListener('click',async()=>{if(busy)return;busy=true;btn.disabled=true;out.textContent='The wheel is spinning…';out.className='spin-prize';
  const fd=new FormData();fd.append('csrf_token',csrf);let res;
  try{const r=await fetch('{{ url_for("spin_play") }}',{method:'POST',body:fd,credentials:'same-origin'});res=await r.json();}catch(e){res={ok:false,error:'Connection problem. Please try again.'};}
  if(!res.ok){out.textContent=res.error||'Unable to spin right now.';busy=false;btn.disabled=false;return;}
  let idx=labels.indexOf(res.label);if(idx<0)idx=0;const target=360*5-(idx+.5)*(360/labels.length);canvas.style.transform='rotate('+target+'deg)';
  setTimeout(()=>{out.textContent='🎉 '+res.text;out.style.borderColor='#28b989';document.getElementById('bal').textContent=res.balance;document.getElementById('left').textContent=res.left;busy=false;if(res.left>0)btn.disabled=false;else btn.textContent='NO PLAYS LEFT';},4300);
 });
})();
</script>
"""

BRAIN_BODY = """
<style>
.brain-hero{padding:24px!important;background:linear-gradient(130deg,#173d39,#152a45 60%,#382654)!important;color:white;border:1px solid #2b7268!important;border-radius:18px}
.brain-hero h1{color:white}.brain-hero p{color:#d2e9e6}
.book-wrap{max-width:850px;margin:20px auto;perspective:1000px}
.question-book{position:relative;padding:24px 26px 24px 43px;border-radius:8px 18px 18px 8px;background:repeating-linear-gradient(0deg,#fffdf2 0,#fffdf2 31px,#dce8ef 32px);color:#203044;box-shadow:inset 8px 0 #194e47,inset 12px 0 #2e8b77,0 14px 30px #0002;border:1px solid #c8d2d7}
.question-book:before{content:'VEDO BRAIN BATTLE  •  CHALLENGE BOOK';display:block;font-size:11px;letter-spacing:1.5px;font-weight:900;color:#247466;margin-bottom:16px}
.question-book .muted{color:#5f6e7d}.question-book h2{color:#173d39;font-size:19px;line-height:1.45}
.question-number{display:inline-grid;place-items:center;width:34px;height:34px;border-radius:50%;background:#1b806e;color:white;font-weight:900;margin-right:8px}
.question-book .opt{background:#fffef8;color:#203044;border-color:#c9d6d5;box-shadow:0 2px 0 #dce4e2}.question-book .opt:hover{border-color:#198c78;background:#eafff8}
@media(max-width:560px){.question-book{padding:18px 15px 18px 28px}.brain-hero{padding:18px!important}}
</style>
<div class="card brain-hero"><h1>🧠 Vedo Brain Battle</h1><p>Open your challenge book, choose your answers, and collect points for correct answers.</p><p><a href="{{ url_for('games_hub') }}" style="color:#bff8e9">← All games</a></p><p><span class="badge {{ 'ok' if info.status == 'OPEN' else 'wait' }}">{{ status_text }}</span> <span class="small">{{ window }} · {{ limit_text }}{% if gm['min_level'] %} · Level {{ gm['min_level'] }}+{% endif %}</span></p></div>
<div class="card"><h2>📘 Your challenge book</h2><p>Each round has up to <b>{{ gm['questions_per_round'] }}</b> questions. Every correct answer earns points (up to {{ money(pool['mx']) }} per question). Incorrect answers earn no points; no points are deducted.</p><p class="muted">Plays left: <b>{{ info.left }}</b> · Questions available: {{ pool['n'] }}</p>{% if open_round %}<a class="btnlink" href="{{ url_for('brain_round', rid=open_round['id']) }}">Continue reading →</a>{% else %}<form method="post" action="{{ url_for('brain_start') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="primary" {% if info.error %}disabled{% endif %}>📖 Open question book</button></form>{% if info.error %}<p class="muted small">{{ info.error }}</p>{% endif %}{% endif %}</div>
<div class="card"><h2>🏅 My completed rounds</h2>{% if rounds %}<div style="overflow-x:auto"><table><tr><th>Date</th><th>Correct</th><th>Points earned</th><th>Status</th><th></th></tr>{% for r in rounds %}<tr><td>{{ r['created_at'][:16] }}</td><td>{{ r['correct_n'] if r['status']=='DONE' else '—' }}</td><td>{{ money(r['score']) }}</td><td>{{ r['status'] }}</td><td><a href="{{ url_for('brain_round', rid=r['id']) }}">{{ 'Review' if r['status']=='DONE' else 'Continue' }}</a></td></tr>{% endfor %}</table></div>{% else %}<p class="muted">Your results will be recorded here.</p>{% endif %}</div>
"""

BRAIN_ROUND_BODY = """
<style>
.round-top{background:linear-gradient(130deg,#153b36,#1b3150);color:#fff;padding:22px!important}.round-top h1{color:#fff}
.book-wrap{max-width:850px;margin:18px auto}.question-book{padding:22px 25px 24px 42px;border-radius:8px 16px 16px 8px;background:repeating-linear-gradient(0deg,#fffdf2 0,#fffdf2 31px,#dce8ef 32px);color:#203044;box-shadow:inset 8px 0 #194e47,inset 12px 0 #2e8b77,0 12px 28px #0002;border:1px solid #c8d2d7;margin-bottom:18px}
.question-book .muted{color:#657585}.question-book h2{color:#173d39;line-height:1.5}.question-book .opt{background:#fffef8;color:#203044;border-color:#c9d6d5}.question-book .opt:hover{border-color:#198c78;background:#eafff8}.question-book .opt.right{background:#d7f9e8;border-color:#21a779}.question-book .opt.wrong{background:#ffe2e2;border-color:#d85b65}
@media(max-width:560px){.question-book{padding:18px 14px 18px 27px}}
</style>
<div class="card round-top"><h1>📘 Question Book · Round #{{ rnd['id'] }}</h1><p><a href="{{ url_for('brain_page') }}" style="color:#c5f7e9">← Back to Brain Battle</a></p>{% if rnd['status']=='DONE' %}<p>Round complete! You answered <b>{{ rnd['correct_n'] }} of {{ questions|length }}</b> correctly and earned <b>{{ money(rnd['score']) }}</b>.</p>{% else %}<p>Read each question and select one answer. Turn the page by submitting your answers.</p>{% endif %}</div>
<form method="post" action="{{ url_for('brain_round', rid=rnd['id']) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
{% for q in questions %}{% set mine = answers.get(q['id'], 0) %}<div class="book-wrap"><section class="question-book"><div style="font-size:11px;font-weight:900;letter-spacing:1.5px;color:#25806e">PAGE {{ loop.index }} OF {{ questions|length }}</div><h2><span class="question-number">{{ loop.index }}</span>{{ q['question'] }}</h2><p class="muted small">Reward for correct answer: <b>{{ money(q['points']) }}</b></p>{% for n, text in [(1,q['a']),(2,q['b']),(3,q['c']),(4,q['d'])] %}<label class="opt{% if rnd['status']=='DONE' %}{{ ' right' if n==q['correct'] }}{{ ' wrong' if n==mine and n!=q['correct'] }}{% endif %}"><input type="radio" name="q{{ q['id'] }}" value="{{ n }}" {% if rnd['status']=='DONE' %}disabled{% if n==mine %} checked{% endif %}{% endif %}><span><b>{{ ['A','B','C','D'][n-1] }}.</b> {{ text }}</span></label>{% endfor %}</section></div>{% endfor %}
{% if rnd['status']=='OPEN' %}<button class="primary" style="display:block;margin:18px auto;padding:14px 26px">✓ Submit answers &amp; reveal score</button>{% else %}<a class="btnlink" href="{{ url_for('brain_page') }}">Back to game lobby</a>{% endif %}</form>
"""


ENVELOPE_BODY = """
<style>
.env-scene{display:flex;flex-direction:column;align-items:center;padding:26px 12px 32px;min-height:300px;overflow:hidden}
.env-stage{position:relative;width:260px;height:190px;margin:22px auto 14px;perspective:900px}
.real-envelope{position:absolute;left:0;bottom:0;width:260px;height:160px;cursor:pointer;filter:drop-shadow(0 15px 14px #0003);transition:transform .25s ease}
.real-envelope:hover{transform:translateY(-4px)}
.env-back{position:absolute;inset:0;background:linear-gradient(145deg,#ffdf8e,#d99439);border:2px solid #b87528;border-radius:8px}
.env-paper{position:absolute;z-index:1;left:19px;bottom:15px;width:222px;height:120px;padding:13px 10px;box-sizing:border-box;background:#fffdf1;color:#45321b;text-align:center;border-radius:5px;box-shadow:0 2px 9px #0002;transform:translateY(0);transition:transform 1.1s cubic-bezier(.2,.8,.2,1);font-weight:800}
.env-paper .paper-small{font-size:10px;letter-spacing:1.5px;color:#96702c}.env-paper .paper-result{font-size:18px;margin-top:12px;color:#167858;overflow-wrap:anywhere}
.env-front{position:absolute;z-index:3;inset:0;background:linear-gradient(135deg,#f7c65e,#d98d2c);clip-path:polygon(0 0,50% 56%,100% 0,100% 100%,0 100%);border-radius:8px}
.env-flap{position:absolute;z-index:4;top:0;left:0;width:100%;height:100%;background:linear-gradient(145deg,#ffe6a0,#e5a640);clip-path:polygon(0 0,100% 0,50% 61%);transform-origin:top center;transition:transform .7s ease;filter:drop-shadow(0 3px 2px #8b551b55)}
.env-seal{position:absolute;z-index:5;left:50%;top:58%;transform:translate(-50%,-50%);width:48px;height:48px;border-radius:50%;display:grid;place-items:center;background:radial-gradient(circle at 30% 25%,#fff0b9,#c83b4b);border:3px solid #fff0b9;color:#fff;font-size:21px;box-shadow:0 3px 7px #0003;transition:opacity .25s}
.env-stage.opened .env-flap{transform:rotateX(180deg);z-index:0}.env-stage.opened .env-paper{transform:translateY(-105px);z-index:6}.env-stage.opened .env-seal{opacity:0}.env-stage.opened .real-envelope{cursor:default}
.env-open-btn{border-radius:999px!important;padding:13px 26px!important;font-size:16px!important;background:linear-gradient(135deg,#f5c34f,#ed8e2b)!important;color:#38230b!important;font-weight:900!important;box-shadow:0 6px 0 #a35c1e55}
.env-confetti{font-size:25px;letter-spacing:7px;text-align:center;min-height:34px}
@media(max-width:420px){.env-stage{transform:scale(.9);margin:8px auto}.env-scene{padding-left:0;padding-right:0}}
</style>
<div class="card note"><h1>✉️ Vedo Mystery Envelope</h1><p class="muted"><a href="{{ url_for('games_hub') }}">← All games</a></p><p><span class="badge {{ 'ok' if info.status=='OPEN' else 'wait' }}">{{ status_text }}</span> <span class="muted small">{{ window }}{% if gm['min_level'] %} · Level {{ gm['min_level'] }}+{% endif %}</span></p><p>There’s a surprise waiting inside. Tap the envelope and open your reward letter.</p>
<div class="env-scene"><div class="env-stage {% if claimed %}opened{% endif %}" id="envstage"><div class="real-envelope" id="env" role="img" aria-label="Mystery reward envelope"><div class="env-back"></div><div class="env-paper" id="envpaper"><div class="paper-small">VEDO REWARD LETTER</div><div class="paper-result" id="paperresult">YOUR SURPRISE<br>IS INSIDE</div></div><div class="env-front"></div><div class="env-flap"></div><div class="env-seal">V</div></div></div><div class="env-confetti" id="envconfetti">✦ ✉ ✦</div><div id="envout" aria-live="polite">{% if claimed %}<div class="flash ok">Already opened: <b>{{ claimed['result'] }}</b>{% if claimed['voucher'] %}<br>Voucher: <code>{{ claimed['voucher'] }}</code>{% endif %}</div>{% else %}<p class="muted">Your sealed envelope is ready.</p>{% endif %}</div><button type="button" class="primary env-open-btn" id="envbtn" {% if info.error %}disabled{% endif %}>{{ 'Envelope opened' if claimed else '🎁 Open my envelope' }}</button><p class="muted">Points balance: <b id="bal">{{ money(user['points']) }}</b></p>{% if info.error and not claimed %}<p class="muted small">{{ info.error }}</p>{% endif %}</div></div>
</script>
(function(){const btn=document.getElementById('envbtn'),stage=document.getElementById('envstage'),out=document.getElementById('envout'),paper=document.getElementById('paperresult'),confetti=document.getElementById('envconfetti'),csrf={{ csrf_token()|tojson }};if(!btn||!stage)return;
{% if claimed %}btn.disabled=true;paper.textContent={{ claimed['result']|tojson }};{% else %}
let busy=false;btn.addEventListener('click',async()=>{if(busy)return;busy=true;btn.disabled=true;btn.textContent='Opening your surprise…';stage.classList.add('opened');confetti.textContent='✦ ✨ ✦';const fd=new FormData();fd.append('csrf_token',csrf);let res;try{const r=await fetch('{{ url_for("envelope_open") }}',{method:'POST',body:fd,credentials:'same-origin'});res=await r.json();}catch(e){res={ok:false,error:'Connection problem. Please try again.'};}
setTimeout(()=>{if(res.ok){paper.textContent=res.text;out.innerHTML='';const d=document.createElement('div');d.className='flash ok';d.textContent='🎉 '+res.text;out.appendChild(d);document.getElementById('bal').textContent=res.balance;btn.textContent='Reward revealed!';confetti.textContent='🎉 ✨ 🎁 ✨ 🎉';}else{stage.classList.remove('opened');btn.disabled=false;btn.textContent='🎁 Open my envelope';out.textContent=res.error||'Please try again.';busy=false;}},900);
});{% endif %}
})();
</script>
"""


ADMIN_GAMES_BODY = """
<div class="card note">
<h1>Games admin</h1>
<p class="muted"><a href="{{ url_for('admin') }}">&larr; Back to admin</a></p>
<p class="muted">Control the three Phase 1 games. All times are <b>UTC</b> (South Africa is UTC+2, so 10:00 in SA is 08:00 here). Results are always chosen by the server, rules are shown to members, and every play is recorded below. There is no setting to change the result of one member in secret.</p>
</div>

{% for c in cards %}
{% set gm = c.g %}
<div class="card">
<h2>{{ c.title }} <span class="badge {{ 'ok' if c.status == 'Open' else 'wait' }}">{{ c.status }}</span> <span class="muted small">event #{{ gm['event_no'] }}</span></h2>
<form method="post" action="{{ url_for('admin_game_settings', game=c.key) }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label class="chk"><input type="checkbox" name="active" value="1" {{ 'checked' if gm['active'] }}> Event active (untick to pause)</label>
<div class="two">
<div><label>Starts (UTC)</label><input type="datetime-local" name="starts_at" value="{{ gm['starts_at']|replace(' ', 'T') }}"></div>
<div><label>Closes (UTC)</label><input type="datetime-local" name="ends_at" value="{{ gm['ends_at']|replace(' ', 'T') }}"></div>
</div>
{% if c.key != 'ENVELOPE' %}
<div class="two">
<div><label>Plays per member</label><input type="number" name="max_plays" min="1" max="1000" value="{{ gm['max_plays'] }}"></div>
<div><label>Limit applies</label><select name="period">
<option value="DAY" {{ 'selected' if gm['period'] == 'DAY' }}>Per day</option>
<option value="EVENT" {{ 'selected' if gm['period'] == 'EVENT' }}>Per event (total)</option>
</select></div>
</div>
{% endif %}
<label>Minimum member level (0 = everyone)</label>
<input type="number" name="min_level" min="0" max="7" value="{{ gm['min_level'] }}">
{% if c.key == 'ENVELOPE' %}
<p class="muted small" style="margin-top:10px">Each envelope can be claimed once per member per event.</p>
<div class="two">
<div><label>Guaranteed reward (R)</label><input type="number" name="min_reward" step="0.01" min="0" value="{{ '%.2f'|format(gm['min_reward'] / 100) }}"></div>
<div><label>Bonus chance (%)</label><input type="number" name="bonus_chance" min="0" max="100" value="{{ gm['bonus_chance'] }}"></div>
<div><label>Bonus voucher level</label><input type="number" name="bonus_level" min="1" max="7" value="{{ gm['bonus_level'] }}"></div>
<div><label>Bonus vouchers left (stock)</label><input type="number" name="bonus_stock" min="0" value="{{ gm['bonus_stock'] }}"></div>
</div>
{% endif %}
{% if c.key == 'BRAIN' %}
<label>Questions per round</label>
<input type="number" name="questions_per_round" min="1" max="50" value="{{ gm['questions_per_round'] }}">
{% endif %}
<button class="primary">Save {{ c.title }}</button>
</form>
<form method="post" action="{{ url_for('admin_game_new_event', game=c.key) }}" onsubmit="return confirm('Start a new event? Everyone gets their plays back. Old records are kept.');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button class="ghost" style="margin-top:10px">Start a new event (reset play limits)</button>
</form>
</div>
{% endfor %}

<div class="card">
<h2>Spin &amp; Win prizes</h2>
<p class="muted small">Odds = weight compared with the other available prizes. Stock = the most members who can win it (empty = unlimited). For points prizes the value is Rand, for voucher prizes it is the level 1-7. Prizes with weight 0, stock used up or switched off are left out of the draw.</p>
{% for p in prizes %}
<form method="post" action="{{ url_for('admin_prize_save') }}" class="prow">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<input type="hidden" name="id" value="{{ p['id'] }}">
<input class="pwbox" style="width:170px" name="label" value="{{ p['label'] }}" maxlength="60" placeholder="Prize name">
<select class="pwbox" style="width:130px" name="kind">
{% for k, v in PRIZE_KINDS.items() %}<option value="{{ k }}" {{ 'selected' if p['kind'] == k }}>{{ v }}</option>{% endfor %}
</select>
<input class="pwbox" style="width:90px" name="value" value="{{ '%.2f'|format(p['value'] / 100) if p['kind'] == 'POINTS' else p['value'] }}" placeholder="R / level">
<input class="pwbox" style="width:80px" name="weight" type="number" min="0" value="{{ p['weight'] }}" title="Odds weight">
<input class="pwbox" style="width:90px" name="stock" type="number" min="0" value="{{ p['stock'] if p['stock'] is not none else '' }}" placeholder="unlimited" title="Stock (max winners)">
<label class="chk" style="margin:0"><input type="checkbox" name="enabled" value="1" {{ 'checked' if p['enabled'] }}> on</label>
<span class="muted small">won {{ p['won'] }}</span>
<button class="success">Save</button>
<button class="danger" formaction="{{ url_for('admin_prize_delete', prize_id=p['id']) }}" onclick="return confirm('Delete this prize?');">Delete</button>
</form>
{% endfor %}
<h2 style="margin-top:18px">Add a prize</h2>
<form method="post" action="{{ url_for('admin_prize_save') }}" class="prow">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<input class="pwbox" style="width:170px" name="label" maxlength="60" placeholder="Prize name" required>
<select class="pwbox" style="width:130px" name="kind">
{% for k, v in PRIZE_KINDS.items() %}<option value="{{ k }}">{{ v }}</option>{% endfor %}
</select>
<input class="pwbox" style="width:90px" name="value" placeholder="R / level">
<input class="pwbox" style="width:80px" name="weight" type="number" min="0" value="1" title="Odds weight">
<input class="pwbox" style="width:90px" name="stock" type="number" min="0" placeholder="unlimited" title="Stock (max winners)">
<label class="chk" style="margin:0"><input type="checkbox" name="enabled" value="1" checked> on</label>
<button class="success">Add</button>
</form>
</div>

<div class="card">
<h2>Brain Battle questions</h2>
{% if questions %}
<table>
<tr><th>#</th><th>Question</th><th>Answers</th><th>Points</th><th>Status</th><th></th></tr>
{% for q in questions %}
<tr><td>{{ q['id'] }}</td><td>{{ q['question'] }}</td>
<td class="muted small">
{% for n, text in [(1, q['a']), (2, q['b']), (3, q['c']), (4, q['d'])] %}{% if n == q['correct'] %}<b class="pos">{{ n }}. {{ text }}</b>{% else %}{{ n }}. {{ text }}{% endif %}<br>{% endfor %}
</td>
<td>{{ money(q['points']) }}</td>
<td><span class="badge {{ 'ok' if q['enabled'] else 'wait' }}">{{ 'on' if q['enabled'] else 'off' }}</span></td>
<td>
<form class="inline" method="post" action="{{ url_for('admin_question_action', qid=q['id'], action='toggle') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="ghost">{{ 'Switch off' if q['enabled'] else 'Switch on' }}</button></form>
<form class="inline" method="post" action="{{ url_for('admin_question_action', qid=q['id'], action='delete') }}" onsubmit="return confirm('Delete this question?');"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="danger">Delete</button></form>
</td></tr>
{% endfor %}
</table>
{% else %}<p class="muted">No questions yet.</p>{% endif %}
<h2 style="margin-top:18px">Add a question</h2>
<form method="post" action="{{ url_for('admin_question_add') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Question</label><input name="question" maxlength="200" required>
<div class="two">
<div><label>Answer 1</label><input name="a" maxlength="200" required></div>
<div><label>Answer 2</label><input name="b" maxlength="200" required></div>
<div><label>Answer 3</label><input name="c" maxlength="200" required></div>
<div><label>Answer 4</label><input name="d" maxlength="200" required></div>
</div>
<div class="two">
<div><label>Correct answer</label><select name="correct"><option value="1">1</option><option value="2">2</option><option value="3">3</option><option value="4">4</option></select></div>
<div><label>Points for a correct answer (R)</label><input name="points" type="number" step="0.01" min="0" value="2.00" required></div>
</div>
<button class="primary">Add question</button>
</form>
</div>

<div class="card">
<h2>Winner records (latest 100)</h2>
{% if records %}
<table>
<tr><th>Date</th><th>Member</th><th>Game</th><th>Result</th><th>Points</th><th>Voucher</th></tr>
{% for r in records %}
<tr class="{{ 'hl' if r['kind'] == 'ITEM' }}"><td>{{ r['created_at'][:16] }}</td><td>{{ r['username'] }}</td><td>{{ GAMES[r['game']] }}</td><td>{{ r['result'] }}{% if r['kind'] == 'ITEM' %} <span class="badge wait">item to hand over</span>{% endif %}</td>
<td>{{ money(r['amount']) if r['amount'] > 0 else '-' }}</td>
<td>{% if r['voucher'] %}<code>{{ r['voucher'] }}</code>{% else %}-{% endif %}</td></tr>
{% endfor %}
</table>
{% else %}<p class="muted">No game plays yet.</p>{% endif %}
</div>
"""


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
.actions{display:grid;grid-template-columns:repeat(4,1fr);gap:14px 8px;background:rgba(3,12,18,.45);border-radius:18px;padding:12px 6px;}
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

@keyframes pulse{
  0%,100%{box-shadow:0 0 0 0 rgba(31,214,164,.55);}
  50%{box-shadow:0 0 0 9px rgba(31,214,164,0);}
}

#lvlmsg:empty{display:none;}
#lvlmsg{margin-bottom:14px;}

.feedhead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:14px;}
.feedhead h2{margin:0;}

.pill{background:var(--s2);border:1px solid var(--line);color:var(--muted);padding:3px 12px;border-radius:999px;font-size:.78rem;font-weight:700;}

/* ---------- Story / post slideshow ---------- */
.hs{--dur:5s;position:relative;border-radius:0;overflow:hidden;background:transparent;border:0;outline:none;}
.hs:focus-visible{outline:2px solid var(--brand);outline-offset:4px;}

.hs-stage{position:relative;height:clamp(320px,55vw,560px);background:transparent;}

.hs-slide{position:absolute;inset:0;margin:0;opacity:0;visibility:hidden;transition:opacity .7s ease,visibility .7s;}
.hs-slide.active{opacity:1;visibility:visible;}

.hs-bg{position:absolute;inset:-24px;background-size:cover;background-position:center;filter:blur(24px) brightness(.5) saturate(1.25);}

.hs-slide img{position:relative;display:block;width:100%;height:100%;object-fit:contain;transform:scale(1);transition:transform 7s ease-out;cursor:zoom-in;}
.hs-slide.active img{transform:scale(1.05);}

.hs-shade{position:absolute;inset:0;pointer-events:none;background:linear-gradient(180deg,rgba(3,10,16,.55) 0%,transparent 26%,transparent 50%,rgba(3,10,16,.9) 100%);}

/* Hide post number */
.hs-count{display:none !important;}

/* Keep delete control in its existing position */
.hs-del{position:absolute;top:10px;right:10px;margin:0;}

/* Post captions */
.hs figcaption{position:absolute;left:0;right:0;bottom:0;padding:16px 20px 30px;display:flex;flex-direction:column;gap:4px;pointer-events:none;}
.hs figcaption small{color:var(--brand);font-weight:700;letter-spacing:.5px;text-transform:uppercase;font-size:.72rem;}
.hs figcaption strong{font-size:1.2rem;line-height:1.3;color:#fff;text-shadow:0 2px 12px rgba(0,0,0,.7);}

/* Hide left and right navigation buttons */
.hs-nav,
.hs-nav.prev,
.hs-nav.next{
  display:none !important;
}

/* Slideshow progress bars */
.hs-bars{position:absolute;left:16px;right:16px;bottom:12px;display:flex;gap:6px;}
.hs-bars i{flex:1;height:4px;border-radius:99px;background:rgba(255,255,255,.28);overflow:hidden;cursor:pointer;}
.hs-bars b{display:block;height:100%;width:0;background:var(--brand);}
.hs-bars i.done b{width:100%;}
.hs-bars i.active b{animation:hsfill var(--dur) linear forwards;}
.hs.paused .hs-bars i.active b{animation-play-state:paused;}

@keyframes hsfill{
  from{width:0;}
  to{width:100%;}
}

/* Empty posts message */
.hs-empty{padding:34px 16px;text-align:center;color:var(--muted);background:var(--s2);border:1px dashed var(--line);border-radius:14px;}

/* File input */
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
  .envelope.shake{animation:none;}
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

/* ---------- Phase 1 games ---------- */
.gcards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-bottom:16px;}
.gcard{background:linear-gradient(180deg,var(--s1),#0a141d);border:1px solid var(--line);border-radius:var(--r);padding:22px;display:flex;flex-direction:column;gap:2px;}
.gcard h2{margin:0 0 8px;}
.gemoji{font-size:2.4rem;line-height:1;margin-bottom:10px;}
.btnlink{display:inline-block;align-self:flex-start;margin-top:8px;background:linear-gradient(135deg,var(--brand),#18b98f);color:#031016;border-radius:12px;padding:10px 20px;font-weight:700;text-decoration:none;}
.btnlink:hover{filter:brightness(1.07);}
.reel{font-size:1.5rem;font-weight:800;text-align:center;padding:26px 12px;margin:16px 0;background:var(--s2);border:1px solid var(--line);border-radius:18px;color:var(--brand);min-height:92px;display:flex;align-items:center;justify-content:center;}
.envelope{font-size:5rem;text-align:center;margin:14px 0;line-height:1.1;}
.envelope.shake{animation:envshake .6s ease-in-out infinite;}
@keyframes envshake{0%,100%{transform:rotate(0);}25%{transform:rotate(-8deg);}75%{transform:rotate(8deg);}}
.opt{display:flex;align-items:center;gap:10px;margin:0 0 8px;padding:11px 14px;border:1px solid var(--line);border-radius:12px;background:var(--s2);color:var(--ink);font-size:.95rem;font-weight:500;cursor:pointer;}
.opt input{width:auto;margin:0;}
.opt.right{border-color:rgba(39,214,155,.6);background:rgba(39,214,155,.14);}
.opt.wrong{border-color:rgba(255,92,108,.6);background:rgba(255,92,108,.12);}
label.chk{display:flex;align-items:center;gap:8px;color:var(--ink);}
label.chk input{width:auto;margin:0;}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:0 14px;}
.prow{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:8px;}
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
<a class="{{ 'active' if ep in ['games_hub','spin_page','brain_page','brain_round','envelope_page'] }}" href="{{ url_for('games_hub') }}">{{ icon('games') }}Games</a>
<a class="{{ 'active' if ep in ['team','pots'] }}" href="{{ url_for('team') }}">{{ icon('team') }}My Team</a>
<a class="{{ 'active' if ep in ['profile'] }}" href="{{ url_for('profile') }}">{{ icon('profile') }}Profile</a>
{% if me['is_admin'] %}<a class="{{ 'active' if ep in ['admin','admin_user_profile','admin_games'] }}" href="{{ url_for('admin') }}">{{ icon('admin') }}Admin</a>{% endif %}
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
{% elif me and ep in ['games_hub','spin_page','brain_page','brain_round','envelope_page'] %}
<div class="subnav"><div class="seg">
<a class="{{ 'active' if ep=='games_hub' }}" href="{{ url_for('games_hub') }}">All games</a>
<a class="{{ 'active' if ep=='spin_page' }}" href="{{ url_for('spin_page') }}">Spin &amp; Win</a>
<a class="{{ 'active' if ep in ['brain_page','brain_round'] }}" href="{{ url_for('brain_page') }}">Brain Battle</a>
<a class="{{ 'active' if ep=='envelope_page' }}" href="{{ url_for('envelope_page') }}">Mystery Envelope</a>
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
<a class="{{ 'active' if ep in ['games_hub','spin_page','brain_page','brain_round','envelope_page'] }}" href="{{ url_for('games_hub') }}">{{ icon('games') }}Games</a>
<a class="{{ 'active' if ep in ['team','pots'] }}" href="{{ url_for('team') }}">{{ icon('team') }}My Team</a>
<a class="{{ 'active' if ep=='profile' }}" href="{{ url_for('profile') }}">{{ icon('profile') }}Profile</a>
{% if me['is_admin'] %}<a class="{{ 'active' if ep in ['admin','admin_user_profile','admin_games'] }}" href="{{ url_for('admin') }}">{{ icon('admin') }}Admin</a>{% endif %}
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
<label>Bank Account  number (11 digits)</label>
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
<span class="chip">{{ money(locked) }} in pots</span>
</div>
<div class="actions closed" id="heroactions">
<a class="act" href="{{ url_for('withdraw') }}"><span>{{ icon('withdraw') }}</span>Withdraw</a>
<a class="act" href="{{ url_for('topup') }}"><span>{{ icon('topup') }}</span>Top up</a>
<a class="act" href="{{ url_for('upgrade') }}"><span>{{ icon('upgrade') }}</span>Upgrade</a>
<a class="act" href="{{ url_for('games_hub') }}"><span>{{ icon('games') }}</span>Games</a>
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
<p class="reward-line">Today's reward: <strong>{{ money(reward) }}</strong></p>
<form method="get" action="{{ url_for('checkin') }}">
<button class="primary">{{ 'Checked in today ✓' if done else ('Continue check-in (' ~ task_done ~ '/' ~ task_total ~ ')' if task_done else 'Check in now') }}</button>
</form>
</div>

<div class="card">
<h2>&#9993; Mystery Envelope</h2>
<p class="muted">Open your envelope for guaranteed points or a bonus voucher. Also try Spin &amp; Win and Brain Battle.</p>
<a class="btnlink" href="{{ url_for('envelope_page') }}">Open my envelope</a>
<a class="btnlink" href="{{ url_for('games_hub') }}" style="margin-left:6px">All games</a>
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
{% set mx = txs|map(attribute='amount')|map('abs')|max %}
<div class="bars" aria-hidden="true">
{% for t in txs %}<i class="{{ 'up' if t['amount'] > 0 else 'dn' }}" style="height:{{ [((t['amount']|abs) / mx * 100)|round|int, 8]|max }}%"></i>{% endfor %}
</div>
{% for t in recent_tx %}
<div class="tx">
<div class="av">{{ t['kind'][:1] }}</div>
<div class="who"><b>{{ t['kind']|replace('_',' ')|title }}</b><span>{{ t['created_at'][:16] }}{% if t['note'] %} - {{ t['note'] }}{% endif %}</span></div>
<div class="amt {{ 'pos' if t['amount'] > 0 else 'neg' }}">{{ '+' if t['amount'] > 0 }}{{ money(t['amount']) }}</div>
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
            if (r.done) { setTimeout(function () { window.location.reload(); }, 2500); }
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
<h2>{{ details['Bank_name'] if details and details['Bank_name'] else ' Top-up details' }}</h2>
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
<p class="join-level" id="joinTitle">Choose an amount for this level</p>
<p class="level-hint" id="joinInfo">Enter your top-up amount then press Enter.</p>
<form id="topupAmountForm" novalidate>
<label for="topupAmount">Amount (R)</label>
<input id="topupAmount" name="amount" type="number" inputmode="decimal" min="10" max="50000" step="0.01" placeholder="Enter amount (R10–R50,000)" required>
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
  var form=document.getElementById('topupAmountForm');
  var amountInput=document.getElementById('topupAmount');
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
      info.textContent='Enter your amount, then press Enter. The Top up details will open for 10 minutes.';
      status.textContent='';
      amountInput.value='';
      amountInput.focus();
      step.scrollIntoView({behavior:'smooth',block:'nearest'});
    });
  });

  form.addEventListener('submit',function(ev){
    ev.preventDefault();
    if(!selectedLevel){status.textContent='Tap a level first.';return;}
    var amount=amountInput.value.trim();
    var numeric=Number(amount);
    if(!amount || !Number.isFinite(numeric) || numeric<10 || numeric>50000){
      status.textContent='Enter an amount from R10 to R50,000.';
      amountInput.focus();
      return;
    }
    enterButton.disabled=true;
    status.textContent='Sending your request…';
    var fd=new FormData();
    fd.append('level',selectedLevel);
    fd.append('amount',amount);
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
<label>Amount (R) - minimum {{ money(WITHDRAW_MIN_CENTS) }}</label>
<input name="amount" type="number" step="0.01" min="10" placeholder="e.g. 200.00" required>
<button class="primary">Request withdrawal</button>
</form>
</div>

<div class="card">
<h2>Withdrawal history</h2>
{% if withdrawals %}
<table>
<tr><th>Date</th><th>Amount</th><th>Status</th></tr>
{% for w in withdrawals %}
<tr><td>{{ w['created_at'][:16] }}</td><td>{{ money(w['amount']) }}</td>
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
<tr><th>Member</th><th>Level</th><th>Points</th><th>Joined</th></tr>
{% for m in members %}
<tr><td>{{ mask_phone(m['username']) }}</td>
<td><span class="badge">L{{ m['level'] }}</span></td>
<td>{{ money(m['points']) }}</td>
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
<p class="muted">Enter a level code from the admin or one you won in a game to move to that level.</p>
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
annual rate shown below</b>. This is not a multiplier: interest = amount x annual rate x weeks / 52.
Collect after the lock period ends. Closing a pot early returns your money but forfeits the interest.</p>
<p>Available balance: <strong>{{ money(user['points']) }}</strong></p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Amount to lock (R) - minimum {{ money(POT_MIN_CENTS) }}</label>
<input name="amount" type="number" step="0.01" min="10" placeholder="e.g. 1000.00" required>
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
<div><dt>Bank Account  number</dt><dd>{{ member['serial_number'] }}</dd></div>
<div><dt>Cellphone number</dt><dd>{{ member['phone'] or '-' }}{% if member['phone'] %} <span class="badge {{ 'ok' if member['phone_verified'] else 'wait' }}">{{ 'verified' if member['phone_verified'] else 'unverified' }}</span>{% endif %}</dd></div>
<div><dt>Level</dt><dd>Level {{ member['level'] }}</dd></div>
<div><dt>Balance</dt><dd>{{ money(member['points']) }}</dd></div>
<div><dt>Referral code</dt><dd><code>{{ member['referral_code'] }}</code></dd></div>
<div><dt>Referred by</dt><dd>{{ (mask_phone(referrer['username']) if is_self else referrer['username']) if referrer else '-' }}</dd></div>
<div><dt>Joined</dt><dd>{{ member['created_at'][:16] }}</dd></div>
</dl>
{% if not is_self %}<p style="margin-top:16px"><a href="{{ url_for('admin') }}">&larr; Back to admin</a></p>{% endif %}
</div>
"""

ADMIN_BODY = """
<div class="card note">
<h2>Phase 1 games</h2>
<p class="muted">Spin &amp; Win, Brain Battle and Mystery Envelope: prizes, odds, stock, limits, schedule and winner records.</p>
<a class="btnlink" href="{{ url_for('admin_games') }}">Open Games admin</a>
</div>

<div class="card">
<h2>Search users</h2>
<form method="get" action="{{ url_for('admin') }}" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
<input name="q" value="{{ q }}" maxlength="30" autocomplete="off" placeholder="Cellphone, Bank Account  number or referral code" style="flex:1;min-width:220px">
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
<label>Bank name</label>
<input name="Bank_name" maxlength="100" value="{{ gd['Bank_name'] if gd else '' }}">
<label>Reference</label>
<input name="Reference" maxlength="100" value="{{ gd['Reference'] if gd else '' }}">
<label>Amount</label>
<input name="Amount" maxlength="100" value="{{ gd['Amount'] if gd else '' }}">
<label>PayShap</label>
<input name="PayShap" maxlength="100" value="{{ gd['PayShap'] if gd else '' }}">
<label>Bank Account  number</label>
<input name="BA_number" maxlength="100" value="{{ gd['BA_number'] if gd else '' }}">
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
<tr><th>For</th><th>Bank name</th><th>Reference</th><th>Amount</th><th>PayShap</th><th>Account no.</th><th>Flag</th><th>Updated</th><th></th></tr>
{% if gd %}
<tr><td><b>Everyone</b></td><td>{{ gd['Bank_name'] or '-' }}</td><td>{{ gd['Reference'] or '-' }}</td><td>{{ gd['Amount'] or '-' }}</td><td>{{ gd['PayShap'] or '-' }}</td><td>{{ gd['BA_number'] or '-' }}</td>
<td>{% if gd['flag'] %}<img src="{{ url_for('media', filename=gd['flag']) }}" alt="flag" style="height:22px;border-radius:3px">{% else %}-{% endif %}</td>
<td>{{ gd['updated_at'][:16] }}</td>
<td><form class="inline" method="post" action="{{ url_for('admin_topup_details_delete', user_id=0) }}" onsubmit="return confirm('Remove the global Top-up details?');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="danger">Remove</button></form></td></tr>
{% endif %}
{% for s in specific_details %}
<tr><td>{{ s['username'] }} (ID {{ s['user_id'] }})</td><td>{{ s['Bank_name'] or '-' }}</td><td>{{ s['Reference'] or '-' }}</td><td>{{ s['Amount'] or '-' }}</td><td>{{ s['PayShap'] or '-' }}</td><td>{{ s['BA_number'] or '-' }}</td>
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
<tr><th>#</th><th>Date</th><th>User</th><th>Amount</th><th>Level</th><th>Reason</th><th>Status</th><th>Action</th></tr>
{% for t in topups %}
<tr><td>{{ t['id'] }}</td><td>{{ t['created_at'][:16] }}</td><td>{{ t['username'] }}</td><td>{{ money(t['amount']) }}</td>
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
<tr><th>#</th><th>Date</th><th>User</th><th>Amount</th><th>Status</th><th>Action</th></tr>
{% for w in withdrawals %}
<tr><td>{{ w['id'] }}</td><td>{{ w['created_at'][:16] }}</td><td>{{ w['username'] }}</td><td>{{ money(w['amount']) }}</td>
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
<tr><th>ID</th><th>Login ID</th><th>Cellphone</th><th>Level</th><th>Selected top-up level</th><th>Balance</th><th>Referred by</th><th>Joined</th><th>Add balance</th><th>Reset password</th></tr>
{% for u in users %}
<tr><td>{{ u['id'] }}</td><td><a href="{{ url_for('admin_user_profile', user_id=u['id']) }}">{{ u['username'] }}</a>{% if u['is_admin'] %} <span class="badge">admin</span>{% endif %}</td>
<td>{{ u['phone'] or '-' }}{% if u['phone'] and not u['phone_verified'] %} <span class="badge wait">unverified</span>{% endif %}</td>
<td>L{{ u['level'] }}</td><td>{% if u['selected_level'] %}L{{ u['selected_level'] }}{% else %}-{% endif %}</td><td>{{ money(u['points']) }}</td>
<td>{{ u['referrer'] or '-' }}</td><td>{{ u['created_at'][:10] }}</td>
<td>
<form class="inline" method="post" action="{{ url_for('admin_add_balance', user_id=u['id']) }}"
onsubmit="return confirm('Add this amount to account {{ u['username'] }}?');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<input type="hidden" name="q" value="{{ q }}">
<input class="pwbox" style="width:105px" name="amount" type="number" step="0.01" min="0.01" placeholder="Amount (R)" required>
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
<tr><th>Date</th><th>User</th><th>Type</th><th>Amount</th><th>Details</th></tr>
{% for t in ledger %}
<tr><td>{{ t['created_at'][:16] }}</td><td>{{ t['username'] }}</td><td>{{ t['kind'] }}</td>
<td class="{{ 'pos' if t['amount'] > 0 else 'neg' }}">{{ '+' if t['amount'] > 0 }}{{ money(t['amount']) }}</td>
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

app.jinja_loader = DictLoader({"base": BASE})


# ============================================================================
# START
# ============================================================================

if __name__ == "__main__":
    init_db()
    print("\n======================================")
    print(" VEDO INVEST")
    print("======================================")
    print("Open: http://127.0.0.1:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False)

