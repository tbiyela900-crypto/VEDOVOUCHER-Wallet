        expected = session.get("csrf", "")
        if not sent or not expected or not secrets.compare_digest(sent, expected):
            abort(400, "Invalid or missing CSRF token. Reload the page and try again.")


@app.context_processor
def inject_globals():
    db = get_db()
    return {
        "csrf_token": csrf_token,
        "me": current_user(),
        "LEVELS": LEVELS,
        "money": money,
        "level_reward": level_reward,
        "daily_reward": daily_reward,
        "UPGRADE_COST_CENTS": UPGRADE_COST_CENTS,
        "POT_APR_PERCENT": POT_APR_PERCENT,
        "POT_WEEKS": sorted(POT_APR_PERCENT),
        "pot_interest": pot_interest_cents,
        "site_name": get_setting(db, "site_name", "ZEDAVouchers"),
        "brand_color": get_setting(db, "brand_color", "#4f46e5"),
    }


def page(body, **context):
    return render_template_string("{% extends 'base' %}{% block body %}" + body + "{% endblock %}", **context)


@app.route("/")
def index():
    return redirect(url_for("dashboard" if current_user() else "login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        serial = request.form.get("serial_number", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        voucher = request.form.get("voucher", "").strip().upper()
        referral_code = request.form.get("referral", "").strip().upper()
        error = None

        if not username.isdigit() or len(username) != 10:
            error = "Student number must contain exactly 10 digits."
        elif not serial.isdigit() or len(serial) != 11:
            error = "Serial number must contain exactly 11 digits."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif not voucher:
            error = "A level code is required to join."

        db = get_db()
        v = referrer = None
        if not error:
            v = db.execute("SELECT * FROM vouchers WHERE code=?", (voucher,)).fetchone()
            if not v:
                error = "That level code is not valid."
            elif v["used_by"] is not None:
                error = "That level code has already been used."
        if not error and referral_code:
            referrer = db.execute("SELECT * FROM users WHERE referral_code=?", (referral_code,)).fetchone()
            if not referrer:
                error = "That referral code does not exist."
        if not error and referrer and referrer["username"] == username:
            error = "You cannot use your own referral code."
        if not error and db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            error = "That student number is already registered."
        if not error and db.execute("SELECT 1 FROM users WHERE serial_number=?", (serial,)).fetchone():
            error = "That serial number is already registered."
        if error:
            flash(error, "error")
            return page(REGISTER_BODY, form=form), 400

        try:
            with db:
                cur = db.execute(
                    "INSERT INTO users(username,pw_hash,level,referral_code,referred_by,serial_number,created_at,points,streak,is_admin) "
                    "VALUES(?,?,?,?,?,?,?,0,0,0)",
                    (username, generate_password_hash(password), v["level"],
                     new_referral_code(db), referrer["id"] if referrer else None,
                     serial, now_iso()),
                )
                uid = cur.lastrowid
                claimed = db.execute(
                    "UPDATE vouchers SET used_by=?,used_at=? WHERE code=? AND used_by IS NULL",
                    (uid, now_iso(), voucher),
                ).rowcount
                if claimed != 1:
                    raise sqlite3.IntegrityError("Voucher already used")
                # This is a simulated demo reward only; no cash is transferred.
                if referrer:
                    credit(db, referrer["id"], v["level"] * REFERRAL_BONUS_PER_LEVEL_CENTS,
                           "REFERRAL", f"Demo referral reward for member {username}")
        except sqlite3.IntegrityError:
            flash("Registration could not be completed. Check that the student and serial numbers are unused and the code is still available.", "error")
            return redirect(url_for("register"))

        session.clear()
        session["uid"] = uid
        session["csrf"] = secrets.token_hex(24)
        flash(f"Welcome! You joined at Level {v['level']}.", "ok")
        return redirect(url_for("dashboard"))
    return page(REGISTER_BODY, form=form)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        serial = request.form.get("serial_number", "").strip()
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not username.isdigit() or len(username) != 10:
            flash("Student number must contain exactly 10 digits.", "error")
        elif not serial.isdigit() or len(serial) != 11:
            flash("Serial number must contain exactly 11 digits.", "error")
        elif len(new_password) < 8:
            flash("New password must be at least 8 characters.", "error")
        elif new_password != confirm:
            flash("Passwords do not match.", "error")
        else:
            db = get_db()
            user = db.execute(
                "SELECT * FROM users WHERE username=? AND serial_number=? AND is_admin=0",
                (username, serial),
            ).fetchone()
            if not user:
                flash("Those details do not match a member account.", "error")
            else:
                db.execute("UPDATE users SET pw_hash=? WHERE id=?",
                           (generate_password_hash(new_password), user["id"]))
                db.commit()
                flash("Password reset. You can now log in.", "ok")
                return redirect(url_for("login"))
        return redirect(url_for("forgot_password"))
    return page(FORGOT_BODY)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["pw_hash"], password):
            session.clear()
            session["uid"] = user["id"]
            session["csrf"] = secrets.token_hex(24)
            return redirect(url_for("dashboard"))
        flash("Wrong username or password.", "error")
        return page(LOGIN_BODY), 401
    return page(LOGIN_BODY)


@app.route("/logout", methods=["POST"])
