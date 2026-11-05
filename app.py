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
      - Vedo Brain Battle    : answer questions, earn balance per correct
                               answer. Admin manages questions, points,
                               round size, limits and schedule.
      - Vedo Mystery Envelope: every eligible member gets the guaranteed
                               minimum reward set by the admin, plus a
                               published chance of a bonus voucher while the
                               voucher stock lasts. One claim per member
                               per event.
    Every result is decided on the server, written to the balance wallet
    (transaction ledger) and to the reward history / winner records.
    Daily Streak is NOT part of Phase 1 (hidden; can be added later).
  * Referral codes and team
  * Simulated WITHDRAWALS: user requests a cash-out of money,
                          admin approves/rejects (rejected = refunded).
  * TEAM VIEW           : each team member's level and balance
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
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template_string,
    request, send_from_directory, session, url_for,
)
from jinja2 import DictLoader
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

