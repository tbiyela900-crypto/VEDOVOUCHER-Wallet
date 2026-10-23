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

        # Validate serial number (exactly 11 digits)
        elif (not serial_number.isascii()
                or not serial_number.isdigit()
                or len(serial_number) != 11):
            error = "Serial number must contain exactly 11 digits."

        # Validate password
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
