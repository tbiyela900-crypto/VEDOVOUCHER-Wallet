LATEST POSTS - NO FRAMES (2 edits to your app.py)
==================================================

EDIT 1 - CSS
In the BASE template, find the closing </style> line and paste this just ABOVE it:
--------------------------------------------------

/* ---------- Latest posts: no frames ---------- */
.card.feed{background:none;border:0;border-radius:0;padding:0;overflow:visible;}
.feed h2{margin:0 0 14px;font-size:1.3rem;}
.post{margin:0 0 28px;}
.post:last-child{margin-bottom:0;}
.post-date{display:block;color:var(--muted);font-size:.8rem;font-weight:600;margin:0 0 6px;}
.post-caption{margin:0 0 10px;font-size:1.05rem;line-height:1.5;color:var(--ink);white-space:pre-wrap;overflow-wrap:anywhere;}
.post img{display:block;width:100%;height:auto;border-radius:0;cursor:zoom-in;}
.post-del{margin:8px 0 0;}
.feed-empty{color:var(--muted);}

EDIT 2 - DASHBOARD TEMPLATE (DASH_BODY)
Find the block that starts with:
    <div class="card">
    <div class="feedhead">
and ends with the </div> that closes that card (the one right after the
admin 'Post photo' form's {% endif %}). Do NOT delete the extra </div> that
follows it - that one closes <div class="dash">. Replace the whole block with:
--------------------------------------------------
<div class="card feed">
<h2>Latest</h2>
{% if posts %}
{% for p in posts %}
<article class="post">
<span class="post-date">{{ p['created_at'][:10] }}</span>
{% if p['caption'] %}<p class="post-caption">{{ p['caption'] }}</p>{% endif %}
<img src="{{ url_for('media', filename=p['image']) }}" alt="{{ p['caption'] }}" loading="lazy">
{% if me['is_admin'] %}
<form class="post-del" method="post" action="{{ url_for('admin_post_delete', post_id=p['id']) }}" onsubmit="return confirm('Delete this post?');">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button class="danger">Delete</button>
</form>
{% endif %}
</article>
{% endfor %}

<div class="lb" id="lb" tabindex="-1" role="dialog" aria-modal="true" aria-label="Posts from the admin">
<button type="button" class="lb-x" aria-label="Close">&times;</button>
<span class="lb-count" id="lbcount"></span>
<div class="lb-track" id="lbtrack">
{% for p in posts %}
<figure class="lb-item">
<img src="{{ url_for('media', filename=p['image']) }}" alt="{{ p['caption'] }}" loading="lazy">
<figcaption>
<small>{{ p['created_at'][:10] }}</small>
{% if p['caption'] %}<strong>{{ p['caption'] }}</strong>{% endif %}
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
  var imgs = document.querySelectorAll('.post img');
  var lb = document.getElementById('lb');
  var track = document.getElementById('lbtrack');
  var count = document.getElementById('lbcount');
  var lbIdx = 0;
  if (!lb || !imgs.length) { return; }

  function lbGo(n, smooth) {
    lbIdx = Math.max(0, Math.min(imgs.length - 1, n));
    track.scrollTo({ left: lbIdx * track.clientWidth, behavior: smooth ? 'smooth' : 'auto' });
    count.textContent = (lbIdx + 1) + ' / ' + imgs.length;
  }
  function openLb(n) {
    lb.classList.add('open');
    document.body.style.overflow = 'hidden';
    lbGo(n, false);
    lb.focus();
  }
  function closeLb() {
    lb.classList.remove('open');
    document.body.style.overflow = '';
  }

  imgs.forEach(function (im, k) {
    im.addEventListener('click', function () { openLb(k); });
  });
  track.addEventListener('scroll', function () {
    var n = Math.round(track.scrollLeft / track.clientWidth);
    if (n !== lbIdx) {
      lbIdx = n;
      count.textContent = (lbIdx + 1) + ' / ' + imgs.length;
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
})();
</script>
{% else %}
<p class="feed-empty">Updates will show up here.</p>
{% endif %}
{% if me['is_admin'] %}
<div class="card" style="margin-top:20px">
<form method="post" action="{{ url_for('admin_post') }}" enctype="multipart/form-data">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Post a photo (PNG, JPG, GIF or WEBP, max 30 MB)</label>
<input type="file" name="photo" accept="image/png,image/jpeg,image/gif,image/webp" required>
<label>Caption</label>
<input name="caption" maxlength="300" placeholder="Write a caption for this photo">
<button class="primary">Post photo</button>
</form>
</div>
{% endif %}
</div>
