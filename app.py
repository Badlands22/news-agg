import os
import re
import sqlite3
import time
import html
from datetime import datetime, timezone

import pytz
from flask import Flask, render_template_string, request, jsonify, url_for

try:
    import psycopg  # type: ignore
except Exception:
    psycopg = None

app = Flask(__name__)

APP_BUILD = "v2-2026-06"
DB_PATH = os.getenv("DB_PATH", "news.db")
DATABASE_URL = os.getenv("DATABASE_URL")
PAGE_SIZE = 15
CACHE_TTL = 30  # seconds

_cache: dict = {}

# ── Topic display labels (keep in sync with collector.py) ────────────────────
ALL_TOPICS = [
    "Trump", "Election", "Bitcoin", "Russia", "Putin", "Israel", "Netanyahu",
    "Iran", "China", "Saudi", "Nuclear", "FBI", "Executive Order", "Injunction",
    "Lawsuit", "Court", "Voter", "Conspiracy", "Corruption", "QAnon", "UFO",
    "RICO", "MAHA", "DNI", "Erdogan", "Lavrov", "Congo", "Sahel", "Board of Peace",
]


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_get(key):
    item = _cache.get(key)
    if not item:
        return None
    exp, val = item
    if time.time() > exp:
        _cache.pop(key, None)
        return None
    return val


def _cache_set(key, val, ttl=CACHE_TTL):
    _cache[key] = (time.time() + ttl, val)


# ── DB helpers ────────────────────────────────────────────────────────────────

def using_postgres():
    return bool(DATABASE_URL)


def pg_connect():
    if psycopg is None:
        raise RuntimeError("psycopg not installed")
    conn = psycopg.connect(
        DATABASE_URL, connect_timeout=5,
        options="-c statement_timeout=5000",
        application_name="news_agg",
    )
    conn.autocommit = True
    return conn


def fetch_rows(query, params=()):
    try:
        if using_postgres():
            with pg_connect() as conn:
                with conn.cursor() as c:
                    c.execute(query, params)
                    cols = [d[0] for d in c.description]
                    return [dict(zip(cols, row)) for row in c.fetchall()]
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB] {e}")
        return []


def fetch_one(query, params=()):
    try:
        if using_postgres():
            with pg_connect() as conn:
                with conn.cursor() as c:
                    c.execute(query, params)
                    row = c.fetchone()
                    return row[0] if row else None
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(query, params)
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] {e}")
        return None


# ── Queries ───────────────────────────────────────────────────────────────────

def get_stories(limit=PAGE_SIZE, page=1, search=None, topic=None):
    offset = max(page - 1, 0) * limit
    ck = ("stories", limit, page, search or "", topic or "", "pg" if using_postgres() else "sq")
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    if using_postgres():
        tbl = "public.articles"
        ph = "%s"
    else:
        tbl = "articles"
        ph = "?"

    if topic:
        q = f"SELECT title,link,source,topic,summary,added_at FROM {tbl} WHERE lower(topic)=lower({ph}) ORDER BY added_at DESC LIMIT {ph} OFFSET {ph}"
        rows = fetch_rows(q, (topic, limit, offset))
    elif search:
        term = f"%{search}%"
        like = "ILIKE" if using_postgres() else "LIKE"
        q = f"SELECT title,link,source,topic,summary,added_at FROM {tbl} WHERE title {like} {ph} OR topic {like} {ph} OR summary {like} {ph} ORDER BY added_at DESC LIMIT {ph} OFFSET {ph}"
        rows = fetch_rows(q, (term, term, term, limit, offset))
    else:
        q = f"SELECT title,link,source,topic,summary,added_at FROM {tbl} ORDER BY added_at DESC LIMIT {ph} OFFSET {ph}"
        rows = fetch_rows(q, (limit, offset))

    _cache_set(ck, rows)
    return rows


def get_latest_update():
    ck = ("latest",)
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    tbl = "public.articles" if using_postgres() else "articles"
    val = fetch_one(f"SELECT MAX(added_at) FROM {tbl}")
    if not val:
        result = ""
    else:
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            result = dt.isoformat()
        except Exception:
            result = str(val)
    _cache_set(ck, result, ttl=60)
    return result


def get_article_counts():
    """Return total count and per-topic counts for the sidebar."""
    ck = ("counts",)
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    tbl = "public.articles" if using_postgres() else "articles"
    rows = fetch_rows(f"SELECT topic, COUNT(*) as cnt FROM {tbl} GROUP BY topic ORDER BY cnt DESC")
    result = {r["topic"]: r["cnt"] for r in rows}
    _cache_set(ck, result, ttl=120)
    return result


# ── Serialization ─────────────────────────────────────────────────────────────

def parse_summary(text):
    """
    Parse the structured summary format into sections:
    {'summary': str, 'bullets': [str]}
    """
    if not text:
        return {"summary": "", "bullets": []}

    # Unescape and strip HTML
    for _ in range(4):
        t2 = html.unescape(text)
        if t2 == text:
            break
        text = t2
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = re.sub(r"[​‌‍⁠﻿]", "", text).strip()

    summary_text = ""
    bullets = []

    # Try to parse structured format
    summary_match = re.search(r"SUMMARY\s*\n(.*?)(?=\nKEY POINTS|\Z)", text, re.DOTALL | re.IGNORECASE)
    bullets_match = re.search(r"KEY POINTS\s*\n(.*)", text, re.DOTALL | re.IGNORECASE)

    if summary_match:
        summary_text = summary_match.group(1).strip()
    if bullets_match:
        raw_bullets = bullets_match.group(1).strip()
        for line in raw_bullets.splitlines():
            line = re.sub(r"^[\s•\-\*]+", "", line).strip()
            if line:
                bullets.append(line)

    # Fallback: treat the whole thing as the summary
    if not summary_text and not bullets:
        summary_text = text

    return {"summary": summary_text, "bullets": bullets[:3]}


def serialize_story(s):
    ts = s.get("added_at")
    time_str = ""
    if ts:
        try:
            if isinstance(ts, str):
                dt_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            elif isinstance(ts, datetime):
                dt_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            else:
                dt_utc = None
            if dt_utc:
                cst = pytz.timezone("America/Chicago")
                dt_local = dt_utc.astimezone(cst)
                time_str = dt_local.strftime("%b %d, %Y %I:%M %p %Z")
        except Exception:
            time_str = str(ts)

    topic = (s.get("topic") or "").strip()
    summary_raw = s.get("summary") or ""
    parsed = parse_summary(summary_raw)

    return {
        "title":   (s.get("title") or "").strip(),
        "link":    (s.get("link") or "").strip(),
        "source":  (s.get("source") or "").strip(),
        "topic":   topic,
        "summary": parsed["summary"],
        "bullets": parsed["bullets"],
        "added_at": time_str,
    }


# ── HTML template ─────────────────────────────────────────────────────────────

BASE_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{{ page_title }}</title>

  <!-- Google AdSense — replace ca-pub-XXXXXXXXXXXXXXXX with your publisher ID -->
  <!-- <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-XXXXXXXXXXXXXXXX" crossorigin="anonymous"></script> -->

  <style>
    /* ── Tokens ── */
    :root {
      --bg:        #0c1117;
      --surface:   #131b24;
      --surface2:  #1a2535;
      --border:    rgba(255,255,255,.09);
      --text:      #e4ecf5;
      --muted:     #7a93ad;
      --accent:    #e63946;
      --accent2:   #c1121f;
      --blue:      #6fa8dc;
      --pill-bg:   rgba(255,255,255,.05);
      --shadow:    0 8px 32px rgba(0,0,0,.5);
      --radius:    14px;
    }
    body.light {
      --bg:        #f0f2f5;
      --surface:   #ffffff;
      --surface2:  #f8f9fa;
      --border:    rgba(0,0,0,.10);
      --text:      #1a202c;
      --muted:     #64748b;
      --blue:      #1d4ed8;
      --pill-bg:   rgba(0,0,0,.05);
      --shadow:    0 4px 20px rgba(0,0,0,.12);
    }

    /* ── Reset ── */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.55;
      transition: background .25s, color .25s;
    }
    a { color: var(--blue); text-decoration: none; }
    a:hover { text-decoration: underline; }

    /* ── Layout ── */
    .page { max-width: 1160px; margin: 0 auto; padding: 20px 16px 60px; }
    .grid { display: grid; grid-template-columns: 1fr 280px; gap: 24px; align-items: start; }
    @media (max-width: 820px) { .grid { grid-template-columns: 1fr; } .sidebar { display: none; } }

    /* ── Header ── */
    .header {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 20px 22px 18px;
      margin-bottom: 20px;
      box-shadow: var(--shadow);
    }
    .header-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
    .site-name { font-size: 26px; font-weight: 800; letter-spacing: -.5px; }
    .site-name span { color: var(--accent); }
    .header-meta { font-size: 12px; color: var(--muted); margin-top: 4px; }
    .theme-btn {
      background: none; border: 1px solid var(--border);
      border-radius: 8px; padding: 6px 10px;
      font-size: 16px; cursor: pointer; color: var(--text);
      flex-shrink: 0;
    }
    .search-row { display: flex; gap: 8px; margin-top: 16px; }
    .search-row input {
      flex: 1; padding: 10px 14px; border-radius: 10px;
      border: 1px solid var(--border);
      background: var(--surface2); color: var(--text);
      font-size: 14px; outline: none;
    }
    .search-row input:focus { border-color: var(--accent); }
    .search-row button {
      padding: 10px 18px; border-radius: 10px;
      background: var(--accent); border: none;
      color: #fff; font-weight: 700; font-size: 14px; cursor: pointer;
    }

    /* ── Topic pills ── */
    .pills { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 14px; }
    .pill {
      padding: 5px 12px; border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--pill-bg);
      font-size: 12px; font-weight: 600; color: var(--muted);
      transition: all .15s;
    }
    .pill:hover { color: var(--text); border-color: rgba(255,255,255,.2); }
    .pill.active { background: var(--accent); border-color: var(--accent); color: #fff; }

    /* ── Ad slots ── */
    .ad-banner {
      background: var(--surface2);
      border: 1px dashed var(--border);
      border-radius: var(--radius);
      height: 90px;
      display: flex; align-items: center; justify-content: center;
      color: var(--muted); font-size: 12px;
      margin-bottom: 20px;
    }
    .ad-rect {
      background: var(--surface2);
      border: 1px dashed var(--border);
      border-radius: var(--radius);
      height: 250px;
      display: flex; align-items: center; justify-content: center;
      color: var(--muted); font-size: 12px;
      margin-bottom: 16px;
    }

    /* ── Section heading ── */
    .section-head {
      font-size: 13px; font-weight: 700; color: var(--muted);
      text-transform: uppercase; letter-spacing: .06em;
      margin-bottom: 12px;
    }

    /* ── Story card ── */
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px 20px;
      margin-bottom: 14px;
      box-shadow: var(--shadow);
      transition: border-color .15s;
    }
    .card:hover { border-color: rgba(230,57,70,.35); }
    .card-topic {
      display: inline-block;
      font-size: 11px; font-weight: 700; text-transform: uppercase;
      letter-spacing: .07em; color: var(--accent);
      margin-bottom: 8px;
    }
    .card h3 {
      font-size: 17px; font-weight: 700; line-height: 1.3;
      margin-bottom: 10px;
    }

    /* ── Structured summary ── */
    .summary-section { margin-top: 8px; }
    .summary-label {
      font-size: 10px; font-weight: 700; text-transform: uppercase;
      letter-spacing: .08em; color: var(--muted);
      margin-bottom: 4px;
    }
    .summary-text { font-size: 14px; color: var(--text); line-height: 1.55; }
    .bullets { list-style: none; margin-top: 10px; display: flex; flex-direction: column; gap: 5px; }
    .bullets li {
      font-size: 13px; color: var(--text);
      padding-left: 16px; position: relative;
    }
    .bullets li::before {
      content: "→";
      position: absolute; left: 0;
      color: var(--accent); font-size: 12px;
    }

    /* ── Card footer ── */
    .card-footer {
      display: flex; align-items: center; justify-content: space-between;
      flex-wrap: wrap; gap: 10px;
      margin-top: 14px; padding-top: 12px;
      border-top: 1px solid var(--border);
    }
    .card-meta { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .source-tag {
      font-size: 12px; color: var(--muted); font-weight: 600;
    }
    .time-tag { font-size: 12px; color: var(--muted); }
    .read-btn {
      display: inline-flex; align-items: center; gap: 5px;
      padding: 7px 14px; border-radius: 8px;
      background: var(--accent); color: #fff;
      font-size: 13px; font-weight: 700;
      transition: background .15s;
    }
    .read-btn:hover { background: var(--accent2); text-decoration: none; }

    /* ── Load more ── */
    .load-wrap { text-align: center; margin-top: 20px; }
    #loadMore {
      padding: 11px 28px; border-radius: 10px;
      background: var(--surface); border: 1px solid var(--border);
      color: var(--text); font-weight: 700; font-size: 14px; cursor: pointer;
      transition: border-color .15s;
    }
    #loadMore:hover { border-color: var(--accent); }
    #loadStatus { font-size: 12px; color: var(--muted); margin-top: 8px; }

    /* ── Sidebar ── */
    .sidebar-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px 18px;
      margin-bottom: 14px;
      box-shadow: var(--shadow);
    }
    .sidebar-title {
      font-size: 12px; font-weight: 700; text-transform: uppercase;
      letter-spacing: .07em; color: var(--muted); margin-bottom: 12px;
    }
    .topic-row {
      display: flex; align-items: center; justify-content: space-between;
      padding: 6px 0; border-bottom: 1px solid var(--border);
      font-size: 13px;
    }
    .topic-row:last-child { border-bottom: none; }
    .topic-row a { color: var(--text); font-weight: 600; }
    .topic-row a:hover { color: var(--blue); text-decoration: none; }
    .topic-count {
      font-size: 11px; color: var(--muted);
      background: var(--pill-bg); padding: 2px 7px; border-radius: 999px;
    }

    /* ── Empty state ── */
    .empty {
      text-align: center; padding: 60px 20px;
      color: var(--muted); font-size: 15px;
    }
    .empty strong { display: block; font-size: 20px; margin-bottom: 8px; color: var(--text); }
  </style>
</head>
<body>

{% macro story_card(s) %}
<div class="card">
  <div class="card-topic">{{ s.topic }}</div>
  <h3>{{ s.title }}</h3>

  {% if s.summary %}
    <div class="summary-section">
      <div class="summary-label">Summary</div>
      <div class="summary-text">{{ s.summary }}</div>
    </div>
  {% endif %}

  {% if s.bullets %}
    <ul class="bullets">
      {% for b in s.bullets %}<li>{{ b }}</li>{% endfor %}
    </ul>
  {% endif %}

  <div class="card-footer">
    <div class="card-meta">
      {% if s.source %}<span class="source-tag">{{ s.source }}</span>{% endif %}
      {% if s.added_at %}<span class="time-tag">{{ s.added_at }}</span>{% endif %}
    </div>
    <a class="read-btn" href="{{ s.link }}" target="_blank" rel="noopener noreferrer">
      Read story →
    </a>
  </div>
</div>
{% endmacro %}

<div class="page">

  <!-- Header -->
  <div class="header">
    <div class="header-top">
      <div>
        <div class="site-name">News<span>Wire</span></div>
        <div class="header-meta">
          Tracking {{ total_topics }} topics across {{ feed_count }} sources
          {% if last_updated %}· Updated <span id="last-updated" data-utc="{{ last_updated }}"></span>{% endif %}
        </div>
      </div>
      <button class="theme-btn" id="theme-btn" title="Toggle theme">🌙</button>
    </div>

    <form class="search-row" method="get" action="{{ url_for('home') }}">
      <input name="q" placeholder="Search stories, topics, keywords…" value="{{ q }}" autocomplete="off"/>
      <button type="submit">Search</button>
    </form>

    <div class="pills">
      <a class="pill {% if not active_topic %}active{% endif %}" href="{{ url_for('home') }}">All</a>
      {% for t in all_topics %}
        <a class="pill {% if active_topic and active_topic|lower == t|lower %}active{% endif %}"
           href="{{ url_for('topic_page', topic=t) }}">{{ t }}</a>
      {% endfor %}
    </div>
  </div>

  <!-- Leaderboard ad slot (728×90 desktop / responsive) -->
  <div class="ad-banner">
    Advertisement
    <!-- Replace with your AdSense unit:
    <ins class="adsbygoogle" style="display:block" data-ad-client="ca-pub-XXXX"
         data-ad-slot="XXXXXXXXXX" data-ad-format="auto" data-full-width-responsive="true"></ins>
    <script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
    -->
  </div>

  <div class="grid">
    <!-- Main feed -->
    <div>
      <div class="section-head">{{ heading }}</div>

      <div id="stories">
        {% if stories %}
          {% for s in stories %}
            {{ story_card(s) }}
          {% endfor %}
        {% else %}
          <div class="empty">
            <strong>No stories found</strong>
            {% if q %}Try a different search term.{% else %}Check back soon — articles update every 15 minutes.{% endif %}
          </div>
        {% endif %}
      </div>

      <div class="load-wrap">
        <button id="loadMore" data-page="{{ page }}" data-topic="{{ active_topic or '' }}" data-q="{{ q }}">
          Load more stories
        </button>
        <div id="loadStatus"></div>
      </div>
    </div>

    <!-- Sidebar -->
    <div class="sidebar">
      <!-- Rectangle ad (300×250) -->
      <div class="ad-rect">
        Advertisement
        <!-- Replace with your AdSense unit -->
      </div>

      <div class="sidebar-card">
        <div class="sidebar-title">Topics</div>
        {% for t in all_topics %}
          <div class="topic-row">
            <a href="{{ url_for('topic_page', topic=t) }}">{{ t }}</a>
            {% if topic_counts.get(t) %}
              <span class="topic-count">{{ topic_counts[t] }}</span>
            {% endif %}
          </div>
        {% endfor %}
      </div>
    </div>
  </div>

</div><!-- /page -->

<script>
(function () {
  // ── Theme toggle ──
  const btn = document.getElementById('theme-btn');
  const saved = localStorage.getItem('theme') || 'dark';
  if (saved === 'light') { document.body.classList.add('light'); btn.textContent = '☀️'; }
  btn.addEventListener('click', () => {
    const isLight = document.body.classList.toggle('light');
    btn.textContent = isLight ? '☀️' : '🌙';
    localStorage.setItem('theme', isLight ? 'light' : 'dark');
  });

  // ── Last updated ──
  const lu = document.getElementById('last-updated');
  if (lu) {
    try {
      const d = new Date(lu.dataset.utc);
      if (!isNaN(d)) lu.textContent = d.toLocaleString(undefined, {
        year:'numeric', month:'short', day:'numeric',
        hour:'numeric', minute:'2-digit', hour12:true
      });
    } catch(_) {}
  }

  // ── HTML escape ──
  const esc = s => (s || '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // ── Render a card from API JSON ──
  function renderCard(s) {
    const topic    = esc(s.topic || '');
    const title    = esc(s.title || '');
    const source   = esc(s.source || '');
    const time     = esc(s.added_at || '');
    const link     = esc(s.link || '#');
    const summary  = esc(s.summary || '');
    const bullets  = (s.bullets || []).map(b => `<li>${esc(b)}</li>`).join('');

    const summaryHtml = summary
      ? `<div class="summary-section">
           <div class="summary-label">Summary</div>
           <div class="summary-text">${summary}</div>
         </div>`
      : '';
    const bulletsHtml = bullets
      ? `<ul class="bullets">${bullets}</ul>`
      : '';
    const metaHtml = (source || time)
      ? `<div class="card-meta">${source ? `<span class="source-tag">${source}</span>` : ''}${time ? `<span class="time-tag">${time}</span>` : ''}</div>`
      : '';

    return `<div class="card">
      <div class="card-topic">${topic}</div>
      <h3>${title}</h3>
      ${summaryHtml}${bulletsHtml}
      <div class="card-footer">
        ${metaHtml}
        <a class="read-btn" href="${link}" target="_blank" rel="noopener noreferrer">Read story →</a>
      </div>
    </div>`;
  }

  // ── Load more ──
  const loadBtn = document.getElementById('loadMore');
  const status  = document.getElementById('loadStatus');
  const list    = document.getElementById('stories');

  loadBtn.addEventListener('click', async () => {
    const nextPage = parseInt(loadBtn.dataset.page || '1', 10) + 1;
    const params = new URLSearchParams({ page: nextPage });
    if (loadBtn.dataset.topic) params.set('topic', loadBtn.dataset.topic);
    if (loadBtn.dataset.q)     params.set('q', loadBtn.dataset.q);

    loadBtn.disabled = true;
    status.textContent = 'Loading…';

    try {
      const res  = await fetch('/api/stories?' + params, { headers: { Accept: 'application/json' } });
      const data = await res.json();
      if (!data.stories?.length) {
        status.textContent = 'No more stories.';
        loadBtn.style.display = 'none';
        return;
      }
      list.insertAdjacentHTML('beforeend', data.stories.map(renderCard).join(''));
      loadBtn.dataset.page = nextPage;
      status.textContent = '';
    } catch (e) {
      status.textContent = 'Error loading. Try again.';
    } finally {
      loadBtn.disabled = false;
    }
  });
})();
</script>
</body>
</html>
"""


# ── Template helper ───────────────────────────────────────────────────────────

def render(heading, stories, page, active_topic=None, q=""):
    topic_counts = get_article_counts()
    return render_template_string(
        BASE_HTML,
        page_title=f"{active_topic or 'Latest'} – NewsWire" if active_topic else "NewsWire – News Aggregator",
        heading=heading,
        stories=stories,
        page=page,
        active_topic=active_topic,
        q=q,
        all_topics=ALL_TOPICS,
        total_topics=len(ALL_TOPICS),
        feed_count=10,
        last_updated=get_latest_update(),
        topic_counts=topic_counts,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return "ok", 200


@app.get("/version")
def version():
    return {"build": APP_BUILD, "utc": datetime.now(timezone.utc).isoformat()}


@app.get("/api/stories")
def api_stories():
    q     = request.args.get("q", "").strip() or None
    topic = request.args.get("topic", "").strip() or None
    page  = max(int(request.args.get("page", "1") or "1"), 1)
    limit = max(int(request.args.get("limit", str(PAGE_SIZE)) or PAGE_SIZE), 1)
    rows  = get_stories(limit=limit, page=page, search=q, topic=topic)
    return jsonify({"page": page, "count": len(rows), "stories": [serialize_story(r) for r in rows]})


@app.route("/")
def home():
    q      = request.args.get("q", "").strip()
    page   = max(int(request.args.get("page", "1") or "1"), 1)
    rows   = get_stories(limit=PAGE_SIZE, page=page, search=q or None)
    stories = [serialize_story(r) for r in rows]
    heading = f'Search results for "{q}"' if q else "Latest Stories"
    return render(heading, stories, page, q=q)


@app.route("/topic/<topic>")
def topic_page(topic):
    page    = max(int(request.args.get("page", "1") or "1"), 1)
    rows    = get_stories(limit=PAGE_SIZE, page=page, topic=topic)
    stories = [serialize_story(r) for r in rows]
    return render(f"{topic} News", stories, page, active_topic=topic)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
