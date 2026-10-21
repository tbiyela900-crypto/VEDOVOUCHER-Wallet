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
