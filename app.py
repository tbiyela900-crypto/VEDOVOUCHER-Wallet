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

    # The school details tab stays open for 10 minutes after a request.
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
            ("School name", val("school_name")),
            ("Teacher's name", val("teacher_name")),
            ("Student number", val("student_number")),
            ("Reference number", val("reference_number")),
            ("Book number", val("book_number")),
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
