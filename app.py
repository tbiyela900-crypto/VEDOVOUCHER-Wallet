"""
Vedo Ai Investment  -  advanced interface edition
=================================================================

Features
  * Accounts, login/logout, members start at Level 0 and earn 0 until top up
  * Sign-up uses an 11-digit serial number + a CELLPHONE NUMBER that must be
    verified with an SMS one-time code (OTP) before the first login.
    The verified cellphone number IS the login ID (no account number).
  * TOP-UP page looks like the check-in page: the member enters an amount and
    a reason, then taps a level tile. The tiles close and a WHITE details tab
    opens for 10 minutes showing the school name, teacher's name, student
    number, reference number and book number, a small school flag and a red
    message. ALL of that information is entered by the admin only (Admin
    page > School top-up details, for everyone or for one member). The member
    can copy every line.
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
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(BASE_DIR, "rewards.db")
)

def backup_database():
    """Create a consistent SQLite backup on persistent storage."""
    backup_dir = "/var/data/backups"
    os.makedirs(backup_dir, exist_ok=True)

    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"Database not found: {DB_PATH}"
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(
        backup_dir, f"rewards_{stamp}.db"
    )

    source = sqlite3.connect(DB_PATH, timeout=30)
    destination = sqlite3.connect(backup_path)

    try:
