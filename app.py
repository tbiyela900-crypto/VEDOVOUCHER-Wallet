"""
ZEDAVouchers
=================================================================


Features
  * Accounts, login/logout, members start at Level 0
  * Sign-up uses an ACCOUNT NUMBER (any number) + 11-digit serial number
  * Top-ups collect 24 digits + 5 alphanumeric + 4 alphanumeric voucher information; only admin can read it
  * Admin approval issues a voucher code to the member
  * Daily check-in (level reward / 24) with streak bonus
  * Referral codes and team
  * Simulated TOP-UPS   : user requests  money, admin approves/rejects.
                          Admin can edit the demo instruction text.
  * Simulated WITHDRAWALS: user requests a cash-out of  money,
                          admin approves/rejects (rejected = refunded).
  * TEAM VIEW           : each team member's level and points (balance)
  * LEVEL UPGRADES      : spend balance to move up one level, costs are public
  * SAVINGS POTS        : lock part of the balance for 1-9 weeks and earn a
                          clearly labelled, realistic annual interest rate
                          (simple interest, NOT a multiplier)
  * PROFILE             : user (and admin) can see the details entered when
                          the account was created
  * Admin dashboard     : monitors users, balances, top-ups, withdrawals,
                          pots, a full transaction ledger, and can RESET
                          a member's password

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
from werkzeug.security import check_password_hash, generate_password_hash


# ============================================================================
# CONFIGURATION
# ============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "rewards.db"))

LEVELS = list(range(0, 8))

# Total reward per level (daily check-in = total / 24)
LEVEL_REWARDS_CENTS = {
    0: 000, 1: 36000, 2: 120000, 3: 360000, 4: 1200000,
    5: 2000000, 6: 3000000, 7: 4000000,
}

DAILY_REWARD_CENTS = {
    lvl: int((Decimal(LEVEL_REWARDS_CENTS[lvl]) / 24).quantize(
        Decimal("0"), rounding=ROUND_HALF_UP))
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
    for lvl in LEVELS if lvl > 0
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
    voucher_24 TEXT NOT NULL DEFAULT '',
    voucher_5 TEXT NOT NULL DEFAULT '',
    voucher_4 TEXT NOT NULL DEFAULT '',
    voucher_code TEXT NOT NULL DEFAULT '',
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

    topup_columns = {
        "voucher_24": "TEXT NOT NULL DEFAULT ''",
        "voucher_5": "TEXT NOT NULL DEFAULT ''",
        "voucher_4": "TEXT NOT NULL DEFAULT ''",
        "voucher_code": "TEXT NOT NULL DEFAULT ''",
    }
    existing_topup_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(topups)").fetchall()
    }
    for column, definition in topup_columns.items():
        if column not in existing_topup_columns:
            db.execute(
                f"ALTER TABLE topups ADD COLUMN {column} {definition}"
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

    # Level vouchers were moved into the Top-up workflow.
    # Existing voucher rows are kept so old databases remain compatible.

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
                    1,
                    new_referral_code(db),
                    referrer["id"] if referrer else None,
                    serial_number,
                    now_iso()
                )
            )

            uid = cur.lastrowid

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

        flash("Welcome! Your account was created at Level 0.", "ok")
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
        voucher_24 = request.form.get("voucher_24", "").strip()
        voucher_5 = request.form.get("voucher_5", "").strip().upper()
        voucher_4 = request.form.get("voucher_4", "").strip().upper()

        if cents is None:
            flash(f"Enter an amount between {money(TOPUP_MIN_CENTS)} and "
                  f"{money(TOPUP_MAX_CENTS)}.", "error")
            return redirect(url_for("topup"))

        # The three reference fields are intentionally visible only to the admin.
        if not voucher_24.isascii() or not voucher_24.isdigit() or len(voucher_24) != 24:
            flash("The first voucher field must contain exactly 24 numbers.", "error")
            return redirect(url_for("topup"))
        if not voucher_5.isascii() or len(voucher_5) != 5 or not voucher_5.isalnum():
            flash("The second voucher field must contain exactly 5 letters/numbers.", "error")
            return redirect(url_for("topup"))
        if not voucher_4.isascii() or len(voucher_4) != 4 or not voucher_4.isalnum():
            flash("The third voucher field must contain exactly 4 letters/numbers.", "error")
            return redirect(url_for("topup"))

        pending = db.execute(
            "SELECT COUNT(*) AS n FROM topups WHERE user_id=? "
            "AND status='PENDING'", (user["id"],)).fetchone()["n"]
        if pending >= 3:
            flash("You already have 3 pending top-up requests.", "error")
            return redirect(url_for("topup"))

        db.execute(
            "INSERT INTO topups (user_id, amount, note, voucher_24, voucher_5, voucher_4, "
            "voucher_code, status, created_at) VALUES (?,?,?,?,?,?,?, 'PENDING', ?)",
            (user["id"], cents, note, voucher_24, voucher_5, voucher_4, "", now_iso()))
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
    return page(ADMIN_BODY, summary=summary, users=users, topups=topups,
                withdrawals=withdrawals, pot_rows=pot_rows, ledger=ledger,
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

    if t["status"] != "PENDING":
        flash("This top-up was already processed.", "error")
        return redirect(url_for("admin"))

    if action == "approve":
        voucher_code = request.form.get("voucher_code", "").strip().upper()
        if not voucher_code:
            flash("Enter the voucher code before approving this top-up.", "error")
            return redirect(url_for("admin"))
        if len(voucher_code) > 100:
            flash("Voucher code is too long.", "error")
            return redirect(url_for("admin"))
        new_status = "APPROVED"
        claimed = db.execute(
            "UPDATE topups SET status=?, voucher_code=?, decided_at=? "
            "WHERE id=? AND status='PENDING'",
            (new_status, voucher_code, now_iso(), topup_id)).rowcount
        if claimed != 1:
            db.rollback()
            flash("This top-up was already processed.", "error")
            return redirect(url_for("admin"))
        credit(db, t["user_id"], t["amount"], "TOPUP",
               f"Top-up #{topup_id} approved (play money)")
    else:
        new_status = "REJECTED"
        claimed = db.execute(
            "UPDATE topups SET status=?, decided_at=? "
            "WHERE id=? AND status='PENDING'",
            (new_status, now_iso(), topup_id)).rowcount
        if claimed != 1:
            db.rollback()
            flash("This top-up was already processed.", "error")
            return redirect(url_for("admin"))

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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VEDOVOUCHERS Wallets</title>
<style>
:root{--bg:#f5f6fa;--card:#fff;--ink:#1d2433;--muted:#6b7385;--brand:#4f46e5;
--ok:#0f9d58;--err:#d93025;--line:#e3e6ef;}
*{box-sizing:border-box;}
body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink);}
.demo{background:#fff4cc;color:#6b5400;text-align:center;padding:6px;font-size:.85rem;}
nav{background:var(--ink);color:#fff;padding:12px 20px;display:flex;gap:16px;align-items:center;flex-wrap:wrap;}
nav a{color:#cfd5ff;text-decoration:none;}
nav a:hover{color:#fff;}
nav .brand{font-weight:700;color:#fff;margin-right:auto;}
nav form{margin:0;}
nav button{background:none;border:1px solid #556;color:#cfd5ff;border-radius:6px;padding:4px 10px;cursor:pointer;}
main{max-width:960px;margin:24px auto;padding:0 16px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px;overflow-x:auto;}
h1,h2{margin:0 0 12px;}
label{display:block;margin:12px 0 4px;font-weight:600;font-size:.9rem;}
input,select,textarea{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;font-size:1rem;font-family:inherit;}
button.primary{margin-top:16px;background:var(--brand);color:#fff;border:0;border-radius:8px;padding:11px 18px;font-size:1rem;cursor:pointer;}
button.primary:disabled{background:#b8bbd6;cursor:not-allowed;}
button.danger{background:#d93025;color:#fff;border:0;border-radius:8px;padding:6px 10px;cursor:pointer;}
button.success{background:#0f9d58;color:#fff;border:0;border-radius:8px;padding:6px 10px;cursor:pointer;}
.inline{display:inline-block;margin:0 2px;}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:12px;}
.flash.ok{background:#e6f6ec;color:var(--ok);}
.flash.error{background:#fdeceb;color:var(--err);}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;}
.stat{background:var(--bg);border-radius:10px;padding:14px;}
.stat b{display:block;font-size:1.4rem;}
.stat span{color:var(--muted);font-size:.85rem;}
.balance{font-size:2.4rem;font-weight:800;color:var(--brand);}
table{width:100%;border-collapse:collapse;}
th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);font-size:.92rem;}
.badge{background:var(--brand);color:#fff;border-radius:999px;padding:2px 10px;font-size:.8rem;}
.badge.ok{background:var(--ok);} .badge.err{background:var(--err);} .badge.wait{background:#b58900;}
code{background:var(--bg);padding:2px 6px;border-radius:6px;}
.muted{color:var(--muted);} .hl{background:#eef0ff;} .small{font-size:.85rem;}
.note{border-left:4px solid var(--brand);}
.voucher-grid{display:grid;grid-template-columns:2fr 1fr 1fr;gap:12px;}
@media(max-width:700px){.voucher-grid{grid-template-columns:1fr;}}
.pos{color:var(--ok);} .neg{color:var(--err);}
.pwbox{width:150px;padding:6px;font-size:.85rem;}
nav a.active{color:#fff;border-bottom:2px solid #fff;}
.subnav{background:#e9ebf5;padding:8px 20px;display:flex;gap:18px;flex-wrap:wrap;}
.subnav a{color:var(--brand);text-decoration:none;font-size:.92rem;}
.subnav a.active{font-weight:700;text-decoration:underline;}
</style>
</head>
<body>
<div class="demo">Welcome let's grow together.</div>
{% set ep = request.endpoint %}
<nav>
<a class="brand" href="{{ url_for('dashboard') if me else url_for('login') }}">My VEDOVOUCHER Wallet</a>
{% if me %}
<a class="{{ 'active' if ep in ['withdraw','topup','upgrade'] }}" href="{{ url_for('withdraw') }}">Withdraw</a>
<a class="{{ 'active' if ep in ['team','pots'] }}" href="{{ url_for('team') }}">My Team</a>
<a class="{{ 'active' if ep in ['profile'] }}" href="{{ url_for('profile') }}">Profile</a>
{% if me['is_admin'] %}<a class="{{ 'active' if ep in ['admin','admin_user_profile'] }}" href="{{ url_for('admin') }}">Admin</a>{% endif %}
<form method="post" action="{{ url_for('logout') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button>Log out ({{ me['username'] }})</button>
</form>
{% else %}
<a href="{{ url_for('login') }}">Log in</a>
<a href="{{ url_for('register') }}">Create account</a>
{% endif %}
</nav>

{% if me and ep in ['withdraw','topup','upgrade'] %}
<div class="subnav">
<a class="{{ 'active' if ep=='withdraw' }}" href="{{ url_for('withdraw') }}">Withdraw</a>
<a class="{{ 'active' if ep=='topup' }}" href="{{ url_for('topup') }}">Top up</a>
<a class="{{ 'active' if ep=='upgrade' }}" href="{{ url_for('upgrade') }}">Upgrade</a>
</div>
{% elif me and ep in ['team','pots'] %}
<div class="subnav">
<a class="{{ 'active' if ep=='team' }}" href="{{ url_for('team') }}">My Team</a>
<a class="{{ 'active' if ep=='pots' }}" href="{{ url_for('pots') }}">Savings pots</a>
</div>
{% endif %}
<main>
{% with msgs = get_flashed_messages(with_categories=true) %}
{% for cat, m in msgs %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}
{% endwith %}
{% block body %}{% endblock %}
</main>
</body>
</html>
"""

LOGIN_BODY = """
<div class="card">
<h1>Log in</h1>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Account number</label>
<input name="username" required autofocus>
<label>Password</label>
<input name="password" type="password" required>
<button class="primary">Log in</button>
</form>
<p class="muted">No account? <a href="{{ url_for('register') }}">Create one</a>.</p>
<p class="muted small">Lost your password? Please contact the admin to reset it.</p>
</div>
"""

REGISTER_BODY = """
<div class="card">
<h1>Create account</h1>
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
<label>Referral code (optional)</label>
<input name="referral" value="{{ form.get('referral','') }}" placeholder="e.g. REF-ABC234">
<button class="primary">Join</button>
</form>
<p class="muted">Already a member? <a href="{{ url_for('login') }}">Log in</a></p>
</div>
"""

DASH_BODY = """
<div class="card">
<h1>Hi, {{ user['username'] }}</h1>
<p class="muted">Your balance</p>
<div class="balance">{{ money(user['points']) }}</div>
<div class="stats">
<div class="stat"><b>Level {{ user['level'] }}</b><span>Your level</span></div>
<div class="stat"><b>{{ money(user['points']) }}</b><span>Available balance</span></div>
<div class="stat"><b>{{ money(locked) }}</b><span>Locked in savings pots</span></div>
<div class="stat"><b>{{ user['streak'] }}</b><span>Day streak</span></div>
</div>
</div>

<div class="card">
<h2>Daily check-in</h2>
<p>Today's reward: <strong>{{ money(reward) }}</strong></p>
{% if bonus %}<p>Streak bonus: <strong>+{{ money(bonus) }}</strong></p>{% endif %}
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
<table>
<tr><th>Level</th><th>Level Reward</th><th>Daily Check-in</th></tr>
{% for l in LEVELS %}
<tr class="{{ 'hl' if l == user['level'] }}"><td>Level {{ l }}</td><td>{{ level_reward(l) }}</td><td>{{ daily_reward(l) }}</td></tr>
{% endfor %}
</table>
</div>

<div class="card">
<h2>Recent activity</h2>
{% if recent_tx %}
<table>
<tr><th>Date</th><th>Type</th><th>Amount</th><th>Details</th></tr>
{% for t in recent_tx %}
<tr><td>{{ t['created_at'][:16] }}</td><td>{{ t['kind'] }}</td>
<td class="{{ 'pos' if t['amount'] > 0 else 'neg' }}">{{ '+' if t['amount'] > 0 }}{{ money(t['amount']) }}</td>
<td class="muted">{{ t['note'] }}</td></tr>
{% endfor %}
</table>
{% else %}<p class="muted">No activity yet.</p>{% endif %}
</div>
"""

TOPUP_BODY = """
<div class="card note">
<h1>Top up</h1>
<p class="muted">Balance: <strong>{{ money(user['points']) }}</strong></p>
<h2>Instructions from the admin</h2>
<p style="white-space:pre-wrap">{{ instructions }}</p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Amount (R) - {{ money(TOPUP_MIN_CENTS) }} to {{ money(TOPUP_MAX_CENTS) }}</label>
<input name="amount" type="number" step="0.01" min="10" placeholder="e.g. 500.00" required>

<div class="voucher-grid">
<div>
<label>Voucher information 1</label>
<input name="voucher_24" type="text" inputmode="numeric" maxlength="24" minlength="24" pattern="[0-9]{24}" placeholder="24 numbers" required>
</div>
<div>
<label>Voucher information 2</label>
<input name="voucher_5" type="text" maxlength="5" minlength="5" pattern="[A-Za-z0-9]{5}" placeholder="5 letters/numbers" required>
</div>
<div>
<label>Voucher information 3</label>
<input name="voucher_4" type="text" maxlength="4" minlength="4" pattern="[A-Za-z0-9]{4}" placeholder="4 letters/numbers" required>
</div>
</div>
<p class="muted small">These three voucher-information fields are private to the admin. They are not shown in your top-up history.</p>

<label>Note for the admin (optional)</label>
<input name="note" maxlength="100" placeholder="Optional note">
<button class="primary">Request top-up</button>
</form>
</div>

<div class="card">
<h2>Top-up history</h2>
{% if history %}
<table>
<tr><th>Date</th><th>Amount</th><th>Note</th><th>Status</th><th>Voucher</th></tr>
{% for t in history %}
<tr><td>{{ t['created_at'][:16] }}</td><td>{{ money(t['amount']) }}</td><td class="muted">{{ t['note'] }}</td>
<td><span class="badge {{ 'ok' if t['status']=='APPROVED' else 'err' if t['status']=='REJECTED' else 'wait' }}">{{ t['status'] }}</span></td>
<td>{% if t['status']=='APPROVED' and t['voucher_code'] %}<code>{{ t['voucher_code'] }}</code>{% elif t['status']=='PENDING' %}<span class="muted">Waiting for admin</span>{% else %}<span class="muted">-</span>{% endif %}</td></tr>
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
<p><code style="font-size:1.3rem">{{ user['referral_code'] }}</code></p>
<p class="muted">New members who enter your code when joining become part of your team.</p>
{% if referrer %}<p>You joined <b>{{ referrer['username'] }}</b>'s team.</p>{% endif %}
</div>

<div class="card">
<h2>Team members ({{ members|length }})</h2>
<div class="stats" style="margin-bottom:12px">
<div class="stat"><b>{{ members|length }}</b><span>Members</span></div>
<div class="stat"><b>{{ money(team_points) }}</b><span>Team total points</span></div>
<div class="stat"><b>{{ money(earned) }}</b><span>Referral rewards earned</span></div>
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
<p>Next: <strong>Level {{ next_level }}</strong> costs <strong>{{ money(UPGRADE_COST_CENTS[next_level]) }}</strong>
(daily check-in becomes {{ daily_reward(next_level) }}).</p>
<form method="post" onsubmit="return confirm('Upgrade to Level {{ next_level }}?');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button class="primary" {% if user['points'] < UPGRADE_COST_CENTS[next_level] %}disabled{% endif %}>
Upgrade to Level {{ next_level }}</button>
</form>
{% if user['points'] < UPGRADE_COST_CENTS[next_level] %}
<p class="muted small">Not enough balance. <a href="{{ url_for('topup') }}">Request a top-up</a>.</p>
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
<div class="card note">
<h1>{{ 'My profile' if is_self else 'Member profile' }}</h1>
<p class="muted">Details entered when the account was created.</p>
<table>
<tr><th>Account number</th><td>{{ member['username'] }}{% if member['is_admin'] %} <span class="badge">admin</span>{% endif %}</td></tr>
<tr><th>Serial number</th><td>{{ member['serial_number'] }}</td></tr>
<tr><th>Level</th><td>Level {{ member['level'] }}</td></tr>
<tr><th>Balance</th><td>{{ money(member['points']) }}</td></tr>
<tr><th>Referral code</th><td><code>{{ member['referral_code'] }}</code></td></tr>
<tr><th>Referred by</th><td>{{ referrer['username'] if referrer else '-' }}</td></tr>
<tr><th>Day streak</th><td>{{ member['streak'] }}</td></tr>
<tr><th>Joined</th><td>{{ member['created_at'][:16] }}</td></tr>
</table>
{% if not is_self %}<p><a href="{{ url_for('admin') }}">&larr; Back to admin</a></p>{% endif %}
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
<h2>Top-up requests & voucher information</h2>
<p class="muted">Only the admin can see the three voucher-information fields entered by members. When approving, enter the voucher code that should be released to the member. The member sees that voucher only after approval.</p>
{% if topups %}
<table>
<tr><th>#</th><th>Date</th><th>User</th><th>Amount</th><th>24 numbers</th><th>5 chars</th><th>4 chars</th><th>Note</th><th>Status / Action</th></tr>
{% for t in topups %}
<tr>
<td>{{ t['id'] }}</td><td>{{ t['created_at'][:16] }}</td><td>{{ t['username'] }}</td><td>{{ money(t['amount']) }}</td>
<td><code>{{ t['voucher_24'] }}</code></td><td><code>{{ t['voucher_5'] }}</code></td><td><code>{{ t['voucher_4'] }}</code></td>
<td class="muted">{{ t['note'] }}</td>
<td>
{% if t['status'] == 'PENDING' %}
<form method="post" action="{{ url_for('admin_topup', topup_id=t['id'], action='approve') }}" style="min-width:220px">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<input name="voucher_code" type="text" maxlength="100" placeholder="Enter voucher code" required>
<button class="success" style="margin-top:6px">Approve + issue voucher</button>
</form>
<form method="post" action="{{ url_for('admin_topup', topup_id=t['id'], action='reject') }}" style="margin-top:6px">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="danger">Reject</button>
</form>
{% else %}
<span class="badge {{ 'ok' if t['status']=='APPROVED' else 'err' }}">{{ t['status'] }}</span>
{% if t['status']=='APPROVED' and t['voucher_code'] %}<br><code>{{ t['voucher_code'] }}</code>{% endif %}
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
