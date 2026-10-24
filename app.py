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
