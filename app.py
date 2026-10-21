"""
ZEDAVouchers  -  advanced interface edition
=================================================================

Features
  * Accounts, login/logout, level code needed to join (levels 1-7)
  * Sign-up uses an ACCOUNT NUMBER (any number) + 11-digit serial number
  * Daily check-in (level reward / 24) with streak bonus
  * Referral codes and team
  * Simulated TOP-UPS   : user requests money, admin approves/rejects.
                          Admin can edit the demo instruction text.
  * Simulated WITHDRAWALS: user requests a cash-out of money,
                          admin approves/rejects (rejected = refunded).
  * TEAM VIEW           : each team member's level and points (balance)
  * LEVEL UPGRADES      : spend balance to move up one level, costs are public
  * SAVINGS POTS        : lock part of the balance for 1-9 weeks and earn a
                          clearly labelled, realistic annual interest rate
  * PROFILE             : details entered when the account was created
  * Admin dashboard     : monitors users, balances, top-ups, withdrawals,
                          pots, a full transaction ledger, and can RESET
                          a member's password

Interface
  * Dark market-terminal look with a live reward ticker
  * Banking-app dashboard: balance card, quick actions, balance ring,
    level progress, streak dots, activity bars and transaction list
  * Three main tabs (Withdraw, My Team, Profile) with sub-menus
  * Bottom tab bar on phones

Run:
  pip install flask
  python app.py
  http://127.0.0.1:5000
"""

import os
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps

from flask import (
    Flask, abort, flash, g, redirect, render_template_string,
    request, session, url_for,
)
from jinja2 import DictLoader
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash


# ============================================================================
# CONFIGURATION
# ============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "rewards.db"))

LEVELS = list(range(1, 8))

# Total reward per level (daily check-in = total / 24)
LEVEL_REWARDS_CENTS = {
    1: 36000, 2: 120000, 3: 360000, 4: 1200000,
    5: 2000000, 6: 3000000, 7: 4000000,
}

DAILY_REWARD_CENTS = {
    lvl: int((Decimal(LEVEL_REWARDS_CENTS[lvl]) / 24).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))
    for lvl in LEVELS
}

STREAK_DAYS = 7
STREAK_BONUS_PER_LEVEL_CENTS = 1000     # R10 x level
REFERRAL_BONUS_PER_LEVEL_CENTS = 20000    # R2000 x new member's level

# Account number rules (any number, digits only)
ACCOUNT_NUMBER_MAX_LEN = 20

# --- Level upgrades --------------------------------------------------------
# Cost to move from level N-1 to level N = 50% of the difference between the
# two levels' total rewards. One level at a time, paid from balance.
UPGRADE_COST_CENTS = {
    lvl: (LEVEL_REWARDS_CENTS[lvl] - LEVEL_REWARDS_CENTS[lvl - 1]) // 2
    for lvl in LEVELS if lvl > 1
}

# --- Top-ups / withdrawals -------------------------------------------------
TOPUP_MIN_CENTS = 1000          # R10
TOPUP_MAX_CENTS = 5000000       # R50,000
WITHDRAW_MIN_CENTS = 1000       # R10

DEFAULT_TOPUP_INSTRUCTIONS = (
    "Enter Amount"
    "like and submit the request. An admin will review it and approve or "
    "reject it."
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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")


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
    amount = Decimal(int(cents or 0)) / Decimal(100)
    return f"R{amount:,.2f}"


def daily_reward_cents(level):
    return DAILY_REWARD_CENTS.get(int(level), 0)


def level_reward(level):
    return money(LEVEL_REWARDS_CENTS.get(int(level), 0))


def daily_reward(level):
    return money(daily_reward_cents(level))


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
    level INTEGER DEFAULT 1,
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
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    decided_at TEXT
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
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
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
    return f"L{level}-{random_block(4)}-{random_block(4)}"


def new_referral_code(db):
    while True:
        code = "REF-" + random_block(6)
        if not db.execute("SELECT 1 FROM users WHERE referral_code=?",
                          (code,)).fetchone():
            return code


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
    }

    for column, definition in required_columns.items():
        if column not in existing_columns:
            db.execute(
                f"ALTER TABLE users ADD COLUMN {column} {definition}"
            )

    db.commit()

    admin_user = os.environ.get("ADMIN_USER", "admin")
    admin_pass = os.environ.get("ADMIN_PASS", "admin12345")

    # Create admin account
    if not db.execute(
        "SELECT 1 FROM users WHERE is_admin=1"
    ).fetchone():

        # Admin does not use a real serial number
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
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
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
    }


# ============================================================================
# REGISTER / LOGIN / LOGOUT
# ============================================================================

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None

    if request.method == "POST":
        # The ACCOUNT NUMBER is the account identifier (any number).
        # It is stored in the `username` column so login keeps working.
        account_number = request.form.get("account_number", "").strip()
        serial_number = request.form.get("serial_number", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        voucher = request.form.get("voucher", "").strip().upper()
        ref_code = request.form.get("referral", "").strip().upper()

        db = get_db()
        v = None
        referrer = None

        # Validate account number (any number: digits only)
        if (not account_number.isascii()
                or not account_number.isdigit()
                or len(account_number) > ACCOUNT_NUMBER_MAX_LEN):
            error = (f"Account number must be a number "
                     f"(digits only, up to {ACCOUNT_NUMBER_MAX_LEN} digits).")

        # Validate serial number (exactly 11 digits)
        elif (not serial_number.isascii()
                or not serial_number.isdigit()
                or len(serial_number) != 11):
            error = "Serial number must contain exactly 11 digits."

        # Validate password
        elif len(password) < 8:
            error = "Password must be at least 8 characters."

        elif password != confirm:
            error = "Passwords do not match."

        # Validate voucher
        elif not voucher:
            error = "A level code is required to join."

        # Check voucher exists and is unused
        if not error:
            v = db.execute(
                "SELECT * FROM vouchers WHERE code=?",
                (voucher,)
            ).fetchone()

            if v is None:
                error = "That level code is not valid."
            elif v["used_by"] is not None:
                error = "That level code has already been used."

        # Check referral code
        if not error and ref_code:
            referrer = db.execute(
                "SELECT * FROM users WHERE referral_code=?",
                (ref_code,)
            ).fetchone()

            if referrer is None:
                error = "That referral code does not exist."

        # Check duplicate account number
        if not error:
            existing_user = db.execute(
                "SELECT 1 FROM users WHERE username=?",
                (account_number,)
            ).fetchone()

            if existing_user:
                error = "That account number is already registered."

        # Check duplicate serial number
        if not error:
            existing_serial = db.execute(
                "SELECT 1 FROM users WHERE serial_number=?",
                (serial_number,)
            ).fetchone()

            if existing_serial:
                error = "That serial number is already registered."

        # Display validation errors
        if error:
            flash(error, "error")
            return page(REGISTER_BODY, form=request.form), 400

        try:
            # Create account
            cur = db.execute(
                """
                INSERT INTO users (
                    username,
                    pw_hash,
                    level,
                    referral_code,
                    referred_by,
                    serial_number,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_number,
                    generate_password_hash(password),
                    v["level"],
                    new_referral_code(db),
                    referrer["id"] if referrer else None,
                    serial_number,
                    now_iso()
                )
            )

            uid = cur.lastrowid

            # Claim voucher only if it is still unused
            claimed = db.execute(
                """
                UPDATE vouchers
                SET used_by=?, used_at=?
                WHERE code=? AND used_by IS NULL
                """,
                (uid, now_iso(), voucher)
            ).rowcount

            if claimed != 1:
                db.rollback()
                flash("That level code has already been used.", "error")
                return redirect(url_for("register"))

            # Referral bonus
            if referrer:
                credit(
                    db,
                    referrer["id"],
                    v["level"] * REFERRAL_BONUS_PER_LEVEL_CENTS,
                    "REFERRAL",
                    f"Referral: {account_number} joined"
                )

            db.commit()

        except sqlite3.IntegrityError:
            db.rollback()
            flash(
                "That account number or serial number is already registered.",
                "error"
            )
            return redirect(url_for("register"))

        except Exception:
            db.rollback()
            app.logger.exception("Registration failed")
            flash("Registration failed due to a server error. Please try again.", "error")
            return redirect(url_for("register"))

        # -----------------------------
        # LOG USER IN
        # -----------------------------
        session.clear()
        session["uid"] = uid

        flash(f"Welcome! You joined at Level {v['level']}.", "ok")
        return redirect(url_for("dashboard"))

    # GET request: show the registration page
    return page(REGISTER_BODY, form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?",
            (username,)
        ).fetchone()

        if user and check_password_hash(user["pw_hash"], password):
            session["uid"] = user["id"]
            return redirect(url_for("dashboard"))

        flash("Wrong account number or password.", "error")
        return page(LOGIN_BODY), 401

    return page(LOGIN_BODY)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    bonus = (user["level"] * STREAK_BONUS_PER_LEVEL_CENTS
             if next_streak % STREAK_DAYS == 0 else 0)

    return page(DASH_BODY, user=user, done=done, next_streak=next_streak,
                history=history, recent_tx=recent_tx, locked=locked,
                reward=reward, bonus=bonus, STREAK_DAYS=STREAK_DAYS)


@app.route("/checkin", methods=["POST"])
@login_required
def checkin():
    db = get_db()
    user = current_user()
    done, streak = checkin_state(user)

    if done:
        flash("You already checked in today. Come back tomorrow!", "error")
        return redirect(url_for("dashboard"))

    reward = daily_reward_cents(user["level"])
    bonus = (user["level"] * STREAK_BONUS_PER_LEVEL_CENTS
             if streak % STREAK_DAYS == 0 else 0)
    day = today().isoformat()

    try:
        db.execute(
            "INSERT INTO checkins (user_id, day, reward, bonus) "
            "VALUES (?,?,?,?)", (user["id"], day, reward, bonus))
    except sqlite3.IntegrityError:
        db.rollback()
        flash("You already checked in today.", "error")
        return redirect(url_for("dashboard"))

    db.execute("UPDATE users SET streak=?, last_checkin=? WHERE id=?",
               (streak, day, user["id"]))
    credit(db, user["id"], reward, "CHECKIN", f"Daily check-in {day}")
    if bonus:
        credit(db, user["id"], bonus, "CHECKIN", f"{streak}-day streak bonus")
    db.commit()

    msg = f"Checked in! +{money(reward)} added to your play balance."
    if bonus:
        msg += f" Streak bonus: +{money(bonus)}."
    flash(msg, "ok")
    return redirect(url_for("dashboard"))


# ============================================================================
# TOP-UPS (simulated deposits)
# ============================================================================

@app.route("/topup", methods=["GET", "POST"])
@login_required
def topup():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        cents = parse_amount(request.form.get("amount"),
                             TOPUP_MIN_CENTS, TOPUP_MAX_CENTS)
        note = request.form.get("note", "").strip()[:100]

        if cents is None:
            flash(f"Enter an amount between {money(TOPUP_MIN_CENTS)} and "
                  f"{money(TOPUP_MAX_CENTS)}.", "error")
            return redirect(url_for("topup"))

        pending = db.execute(
            "SELECT COUNT(*) AS n FROM topups WHERE user_id=? "
            "AND status='PENDING'", (user["id"],)).fetchone()["n"]
        if pending >= 3:
            flash("You already have 3 pending top-up requests.", "error")
            return redirect(url_for("topup"))

        db.execute(
            "INSERT INTO topups (user_id, amount, note, status, created_at) "
            "VALUES (?,?,?, 'PENDING', ?)",
            (user["id"], cents, note, now_iso()))
        db.commit()
        flash(f"Top-up request for {money(cents)} sent to the admin.", "ok")
        return redirect(url_for("topup"))

    history = db.execute(
        "SELECT * FROM topups WHERE user_id=? ORDER BY id DESC",
        (user["id"],)).fetchall()
    return page(TOPUP_BODY, user=user, history=history,
                instructions=get_setting(db, "topup_instructions",
                                         DEFAULT_TOPUP_INSTRUCTIONS),
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
              "It will be reviewed by the admin.", "ok")
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
        "WHERE referred_by=? ORDER BY level DESC, points DESC",
        (user["id"],)).fetchall()

    earned = db.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
        "WHERE user_id=? AND kind='REFERRAL'", (user["id"],)).fetchone()["s"]

    team_points = sum(m["points"] for m in members)

    referrer = None
    if user["referred_by"]:
        referrer = db.execute("SELECT username FROM users WHERE id=?",
                              (user["referred_by"],)).fetchone()

    return page(TEAM_BODY, user=user, members=members, earned=earned,
                team_points=team_points, referrer=referrer)


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

        db.commit()
        flash(f"Upgraded to Level {next_level} for {money(cost)}!", "ok")
        return redirect(url_for("upgrade"))

    return page(UPGRADE_BODY, user=user, next_level=next_level)


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

    users = db.execute(
        "SELECT u.*, r.username AS referrer FROM users u "
        "LEFT JOIN users r ON r.id = u.referred_by "
        "ORDER BY u.is_admin DESC, u.level DESC, u.points DESC").fetchall()
    topups = db.execute(
        "SELECT topups.*, users.username FROM topups "
        "JOIN users ON users.id = topups.user_id "
        "ORDER BY (topups.status='PENDING') DESC, topups.id DESC "
        "LIMIT 50").fetchall()
    withdrawals = db.execute(
        "SELECT withdrawals.*, users.username FROM withdrawals "
        "JOIN users ON users.id = withdrawals.user_id "
        "ORDER BY (withdrawals.status='PENDING') DESC, withdrawals.id DESC "
        "LIMIT 50").fetchall()
    pot_rows = db.execute(
        "SELECT savings_pots.*, users.username FROM savings_pots "
        "JOIN users ON users.id = savings_pots.user_id "
        "ORDER BY savings_pots.id DESC LIMIT 50").fetchall()
    ledger = db.execute(
        "SELECT transactions.*, users.username FROM transactions "
        "JOIN users ON users.id = transactions.user_id "
        "ORDER BY transactions.id DESC LIMIT 60").fetchall()
    unused = db.execute(
        "SELECT * FROM vouchers WHERE used_by IS NULL "
        "ORDER BY level, created_at").fetchall()

    return page(ADMIN_BODY, summary=summary, users=users, topups=topups,
                withdrawals=withdrawals, pot_rows=pot_rows, ledger=ledger,
                unused=unused,
                instructions=get_setting(db, "topup_instructions",
                                         DEFAULT_TOPUP_INSTRUCTIONS))


@app.route("/admin/instructions", methods=["POST"])
@admin_required
def admin_instructions():
    text = request.form.get("instructions", "").strip()
    if not text:
        flash("Instruction text cannot be empty.", "error")
    elif len(text) > 1000:
        flash("Instruction text is too long (max 1000 characters).", "error")
    else:
        db = get_db()
        set_setting(db, "topup_instructions", text)
        db.commit()
        flash("Top-up instruction text updated.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/user/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    """Admin sets a NEW password for a member (replaces 'forgot password').
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

    if action == "approve":
        credit(db, t["user_id"], t["amount"], "TOPUP",
               f"Top-up #{topup_id} approved (play money)")
    db.commit()
    flash(f"Top-up #{topup_id} {new_status.lower()}.", "ok")
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
# TEMPLATES
# ============================================================================

BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#050c13">
<title>VEDOVOUCHERS Wallets</title>
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
.dash{display:grid;grid-template-columns:1.3fr 1fr;gap:16px;margin-bottom:16px;}
.dash .card{margin-bottom:0;}
.hero{border-radius:26px;padding:24px;color:#eafff8;border:1px solid rgba(31,214,164,.35);
  background:
    radial-gradient(500px 220px at 100% 0%,rgba(31,214,164,.35),transparent 65%),
    linear-gradient(135deg,#0f766e 0%,#0b3d49 55%,#08242f 100%);}
.hero-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;}
.hello{margin:0 0 18px;font-weight:600;opacity:.9;}
.lbl{margin:0 0 2px;font-size:.84rem;opacity:.75;}
.balance{font-size:2.7rem;font-weight:800;letter-spacing:-1px;line-height:1.1;}
.fp{width:54px;height:54px;border-radius:50%;display:grid;place-items:center;background:rgba(255,255,255,.10);border:1px solid rgba(255,255,255,.25);}
.fp .ic{width:28px;height:28px;}
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

/* ---------- login / register ---------- */
.auth{max-width:440px;margin:30px auto 0;}
.auth .logo{width:54px;height:54px;border-radius:17px;font-size:1.4rem;margin-bottom:16px;}

/* ---------- bottom tab bar (phones) ---------- */
.tabbar{display:none;}

@media (max-width:860px){
  .dash{grid-template-columns:1fr;}
}
@media (max-width:720px){
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
  .ticker{overflow-x:auto;}
}
</style>
</head>
<body>
<div class="ticker" aria-label="Daily rewards by level"><div class="track">
{% for k in range(2) %}
<span class="tk">Welcome, let's grow together.</span>
{% for l in LEVELS %}<span class="tk">Level {{ l }} <b>{{ daily_reward(l) }}/day</b></span>{% endfor %}
{% endfor %}
</div></div>

{% set ep = request.endpoint %}
<header class="top">
<nav>
<a class="brand" href="{{ url_for('dashboard') if me else url_for('login') }}"><span class="logo">V</span><span>My VEDOVOUCHER Wallet</span></a>
{% if me %}
<div class="tabs">
<a class="{{ 'active' if ep in ['withdraw','topup','upgrade'] }}" href="{{ url_for('withdraw') }}">{{ icon('withdraw') }}Withdraw</a>
<a class="{{ 'active' if ep in ['team','pots'] }}" href="{{ url_for('team') }}">{{ icon('team') }}My Team</a>
<a class="{{ 'active' if ep in ['profile'] }}" href="{{ url_for('profile') }}">{{ icon('profile') }}Profile</a>
{% if me['is_admin'] %}<a class="{{ 'active' if ep in ['admin','admin_user_profile'] }}" href="{{ url_for('admin') }}">{{ icon('admin') }}Admin</a>{% endif %}
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
<p class="muted">Log in with your account number.</p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Account number</label>
<input name="username" inputmode="numeric" required autofocus>
<label>Password</label>
<input name="password" type="password" required>
<button class="primary" style="width:100%">Log in</button>
</form>
<p class="muted" style="margin-top:16px">No account? <a href="{{ url_for('register') }}">Create one</a> with a level code.</p>
<p class="muted small">Lost your password? Please contact the admin to reset it.</p>
</div>
</div>
"""

REGISTER_BODY = """
<div class="auth">
<span class="logo">V</span>
<div class="card">
<h1>Create account</h1>
<p class="muted">You need a level code to join.</p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Account number (any number)</label>
<input
    type="text"
    name="account_number"
    value="{{ form.get('account_number','') }}"
    maxlength="20"
    inputmode="numeric"
    pattern="[0-9]+"
    placeholder="Enter your account number"
    required
    autofocus
>
<label>Cereal/Serial Number</label>
<input
    type="text"
    name="serial_number"
    value="{{ form.get('serial_number','') }}"
    maxlength="11"
    minlength="11"
    inputmode="numeric"
    pattern="[0-9]{11}"
    placeholder="Enter 11-digit cereal/serial number"
    required
>
<label>Password (minimum 8 characters)</label>
<input name="password" type="password" required>
<label>Confirm password</label>
<input name="confirm" type="password" required>
<label>Level code (required) - Level 1 to Level 7</label>
<input name="voucher" value="{{ form.get('voucher','') }}" placeholder="e.g. L3-ABCD-EFGH" required>
<label>Referral code (optional)</label>
<input name="referral" value="{{ form.get('referral','') }}" placeholder="e.g. REF-ABC234">
<button class="primary" style="width:100%">Join</button>
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
<div class="fp">{{ icon('fingerprint') }}</div>
</div>
<div class="chips">
<span class="chip">Level {{ user['level'] }}</span>
<span class="chip">{{ user['streak'] }} day streak</span>
<span class="chip">{{ money(locked) }} in pots</span>
</div>
<div class="actions">
<a class="act" href="{{ url_for('withdraw') }}"><span>{{ icon('withdraw') }}</span>Withdraw</a>
<a class="act" href="{{ url_for('topup') }}"><span>{{ icon('topup') }}</span>Top up</a>
<a class="act" href="{{ url_for('upgrade') }}"><span>{{ icon('upgrade') }}</span>Upgrade</a>
<a class="act" href="{{ url_for('pots') }}"><span>{{ icon('pots') }}</span>Pots</a>
<a class="act" href="{{ url_for('team') }}"><span>{{ icon('team') }}</span>My Team</a>
<a class="act" href="{{ url_for('profile') }}"><span>{{ icon('profile') }}</span>Profile</a>
</div>
</section>

<section class="card">
<h2>Where your money is</h2>
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
</section>
</div>

<div class="card">
<h2>Daily check-in</h2>
<div class="dots" aria-label="Streak progress">
{% for i in range(1, STREAK_DAYS + 1) %}<i class="{{ 'on' if i <= sf }}"></i>{% endfor %}
</div>
<p>Today's reward: <strong>{{ money(reward) }}</strong></p>
{% if bonus %}<p>Streak bonus: <strong class="pos">+{{ money(bonus) }}</strong></p>{% endif %}
<p class="muted">Your Level {{ user['level'] }} reward is <strong>{{ level_reward(user['level']) }}</strong>;
the daily check-in amount is the level reward divided by 24.</p>
<p class="muted">Check in {{ STREAK_DAYS }} days in a row for a streak bonus.</p>
<form method="post" action="{{ url_for('checkin') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button class="primary" {% if done %}disabled{% endif %}>{{ 'Checked in today ✓' if done else 'Check in now' }}</button>
</form>
</div>

<div class="card">
<h2>Reward by level</h2>
<div class="tiles">
{% for l in LEVELS %}
<div class="tile t{{ l }} {{ 'me' if l == user['level'] }}">
<b>Level {{ l }}</b><strong>{{ level_reward(l) }}</strong><span>{{ daily_reward(l) }} per day</span>
</div>
{% endfor %}
</div>
</div>

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
"""

TOPUP_BODY = """
<div class="card note">
<h1>Top up (money)</h1>
<p class="muted">Balance: <strong>{{ money(user['points']) }}</strong></p>
<h2>Instructions from the admin</h2>
<p style="white-space:pre-wrap">{{ instructions }}</p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Amount (R) - {{ money(TOPUP_MIN_CENTS) }} to {{ money(TOPUP_MAX_CENTS) }}</label>
<input name="amount" type="number" step="0.01" min="10" placeholder="e.g. 500.00" required>
<label>Note for the admin (optional)</label>
<input name="note" maxlength="100" placeholder="e.g. for my Level 2 upgrade">
<button class="primary">Request top-up</button>
</form>
</div>

<div class="card">
<h2>Top-up history</h2>
{% if history %}
<table>
<tr><th>Date</th><th>Amount</th><th>Note</th><th>Status</th></tr>
{% for t in history %}
<tr><td>{{ t['created_at'][:16] }}</td><td>{{ money(t['amount']) }}</td><td class="muted">{{ t['note'] }}</td>
<td><span class="badge {{ 'ok' if t['status']=='APPROVED' else 'err' if t['status']=='REJECTED' else 'wait' }}">{{ t['status'] }}</span></td></tr>
{% endfor %}
</table>
{% else %}<p class="muted">No top-up requests yet.</p>{% endif %}
</div>
"""

WITHDRAW_BODY = """
<div class="card note">
<h1>Withdraw (money)</h1>
<p class="muted">A withdrawal request only moves money
out of your balance for the admin to approve. No ban. Rejected requests are refunded.</p>
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
<p class="muted">New members who enter your code when joining become part of your team.</p>
{% if referrer %}<p>You joined <b>{{ referrer['username'] }}</b>'s team.</p>{% endif %}
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
<tr><td>{{ m['username'] }}</td>
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
<h2>Rules</h2>
<ul>
<li>You can move up <b>one level at a time</b>.</li>
<li>The cost is paid from your balance (top up first if you need more).</li>
<li>The cost of a level = 50% of the difference between that level's total reward and the level below it.</li>
<li>Upgrades are final: they cannot be refunded or reversed.</li>
<li>A higher level means a higher daily check-in reward (level reward / 24).</li>
</ul>
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
<p class="muted small" style="margin-top:10px">Not enough balance. <a href="{{ url_for('topup') }}">Request a top-up</a>.</p>
{% endif %}
{% else %}
<p class="muted">You are already at the highest level.</p>
{% endif %}
</div>

<div class="card">
<h2>Upgrade costs</h2>
<table>
<tr><th>Move to</th><th>Cost</th><th>Level reward</th><th>Daily check-in</th></tr>
{% for l in LEVELS if l > 1 %}
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
<div><dt>Account number</dt><dd>{{ member['username'] }}{% if member['is_admin'] %} <span class="badge">admin</span>{% endif %}</dd></div>
<div><dt>Serial number</dt><dd>{{ member['serial_number'] }}</dd></div>
<div><dt>Level</dt><dd>Level {{ member['level'] }}</dd></div>
<div><dt>Balance</dt><dd>{{ money(member['points']) }}</dd></div>
<div><dt>Referral code</dt><dd><code>{{ member['referral_code'] }}</code></dd></div>
<div><dt>Referred by</dt><dd>{{ referrer['username'] if referrer else '-' }}</dd></div>
<div><dt>Day streak</dt><dd>{{ member['streak'] }}</dd></div>
<div><dt>Joined</dt><dd>{{ member['created_at'][:16] }}</dd></div>
</dl>
{% if not is_self %}<p style="margin-top:16px"><a href="{{ url_for('admin') }}">&larr; Back to admin</a></p>{% endif %}
</div>
"""

ADMIN_BODY = """
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
<h2>Top-up instruction text</h2>
<p class="muted">Shown to every member on the Top up page. Keep it a play-money demo message.</p>
<form method="post" action="{{ url_for('admin_instructions') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<textarea name="instructions" rows="4" maxlength="1000" required>{{ instructions }}</textarea>
<button class="primary">Save instructions</button>
</form>
</div>

<div class="card">
<h2>Top-up requests</h2>
{% if topups %}
<table>
<tr><th>#</th><th>Date</th><th>User</th><th>Amount</th><th>Note</th><th>Status</th><th>Action</th></tr>
{% for t in topups %}
<tr><td>{{ t['id'] }}</td><td>{{ t['created_at'][:16] }}</td><td>{{ t['username'] }}</td><td>{{ money(t['amount']) }}</td>
<td class="muted">{{ t['note'] }}</td>
<td><span class="badge {{ 'ok' if t['status']=='APPROVED' else 'err' if t['status']=='REJECTED' else 'wait' }}">{{ t['status'] }}</span></td>
<td>
{% if t['status'] == 'PENDING' %}
<form class="inline" method="post" action="{{ url_for('admin_topup', topup_id=t['id'], action='approve') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="success">Approve</button></form>
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
<tr><th>Account number</th><th>Level</th><th>Balance</th><th>Streak</th><th>Referred by</th><th>Joined</th><th>Reset password</th></tr>
{% for u in users %}
<tr><td><a href="{{ url_for('admin_user_profile', user_id=u['id']) }}">{{ u['username'] }}</a>{% if u['is_admin'] %} <span class="badge">admin</span>{% endif %}</td>
<td>L{{ u['level'] }}</td><td>{{ money(u['points']) }}</td><td>{{ u['streak'] }}</td>
<td>{{ u['referrer'] or '-' }}</td><td>{{ u['created_at'][:10] }}</td>
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
    print(" ZEDAVOUCHERS")
    print("======================================")
    print("Open: http://127.0.0.1:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
