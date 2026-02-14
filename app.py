# 1) Overwrite app.py with the correct Python code
$path = "app.py"

$py = @'
import os
import sqlite3
import time
from datetime import datetime, timezone
from flask import Flask, render_template_string, request, jsonify, url_for

# Postgres (Render)
try:
    import psycopg
except Exception:
    psycopg = None

app = Flask(__name__)

DB_PATH = os.getenv("DB_PATH", "news.db")
DATABASE_URL = os.getenv("DATABASE_URL")
PAGE_SIZE_DEFAULT = 12

# Small in-process cache
CACHE_TTL_SECONDS = 10
_cache = {}  # key -> (expires_epoch, value)

def using_postgres() -> bool:
    return bool(DATABASE_URL)

def pg_connect():
    if psycopg is None:
        raise RuntimeError("psycopg is not installed. Add psycopg[binary] to requirements.txt")
    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=5,
        options="-c statement_timeout=5000",
        application_name="news_agg",
    )

def sqlite_connect():
    return sqlite3.connect(DB_PATH)

def _cache_get(key):
    item = _cache.get(key)
    if not item:
        return None
    exp, val = item
    if time.time() > exp:
        _cache.pop(key, None)
        return None
    return val

def _cache_set(key, val, ttl=CACHE_TTL_SECONDS):
    _cache[key] = (time.time() + ttl, val)

def fetch_rows(query: str, params: tuple = ()):
    try:
        if using_postgres():
            with pg_connect() as conn:
                with conn.cursor() as c:
                    c.execute(query, params)
                    cols = [d[0] for d in c.description]
                    return [dict(zip(cols, row)) for row in c.fetchall()]
        else:
            conn = sqlite_connect()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(query, params)
            rows = [dict(r) for r in c.fetchall()]
            conn.close()
            return rows
    except Exception as e:
        print(f"[DB] fetch_rows error: {e}")
        return []

def fetch_one(query: str, params: tuple = ()):
    try:
        if using_postgres():
            with pg_connect() as conn:
                with conn.cursor() as c:
                    c.execute(query, params)
                    row = c.fetchone()
                    return row[0] if row else None
        else:
            conn = sqlite_connect()
            c = conn.cursor()
            c.execute(query, params)
            row = c.fetchone()
            conn.close()
            return row[0] if row else None
    except Exception as e:
        print(f"[DB] fetch_one error: {e}")
        return None

def get_recent_stories(limit=12, search=None, page=1):
    offset = max(page - 1, 0) * limit
    cache_key = ("recent", limit, search or "", page, "pg" if using_postgres() else "sqlite")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if search:
        term = f"%{search}%"
        if using_postgres():
            q = """
                SELECT title, link, topic, summary, added_at
                FROM public.articles
                WHERE title ILIKE %s OR topic ILIKE %s OR summary ILIKE %s
                ORDER BY added_at DESC
                LIMIT %s OFFSET %s
            """
            rows = fetch_rows(q, (term, term, term, limit, offset))
        else:
            q = """
                SELECT title, link, topic, summary, added_at
                FROM articles
                WHERE title LIKE ? OR topic LIKE ? OR summary LIKE ?
                ORDER BY added_at DESC
                LIMIT ? OFFSET ?
            """
            rows = fetch_rows(q, (term, term, term, limit, offset))
    else:
        if using_postgres():
            q = """
                SELECT title, link, topic, summary, added_at
                FROM public.articles
                ORDER BY added_at DESC
                LIMIT %s OFFSET %s
            """
            rows = fetch_rows(q, (limit, offset))
        else:
            q = """
                SELECT title, link, topic, summary, added_at
                FROM articles
                ORDER BY added_at DESC
                LIMIT ? OFFSET ?
            """
            rows = fetch_rows(q, (limit, offset))

    _cache_set(cache_key, rows)
    return rows

def get_topic_stories(topic, limit=12, page=1):
    offset = max(page - 1, 0) * limit
    cache_key = ("topic", topic.lower(), limit, page, "pg" if using_postgres() else "sqlite")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if using_postgres():
        q = """
            SELECT title, link, topic, summary, added_at
            FROM public.articles
            WHERE lower(topic) = lower(%s)
            ORDER BY added_at DESC
            LIMIT %s OFFSET %s
        """
        rows = fetch_rows(q, (topic, limit, offset))
    else:
        q = """
            SELECT title, link, topic, summary, added_at
            FROM articles
            WHERE lower(topic) = lower(?)
            ORDER BY added_at DESC
            LIMIT ? OFFSET ?
        """
        rows = fetch_rows(q, (topic, limit, offset))

    _cache_set(cache_key, rows)
    return rows

def get_latest_update_iso():
    cache_key = ("latest_update", "pg" if using_postgres() else "sqlite")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    q = "SELECT MAX(added_at) FROM public.articles" if using_postgres() else "SELECT MAX(added_at) FROM articles"
    val = fetch_one(q)
    if not val:
        _cache_set(cache_key, None)
        return None

    if isinstance(val, datetime):
        dt = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        iso = dt.astimezone(timezone.utc).isoformat()
    else:
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            iso = dt.astimezone(timezone.utc).isoformat()
        except Exception:
            iso = None

    _cache_set(cache_key, iso)
    return iso

def get_all_topics():
    return [
        "Bitcoin","China","Conspiracy","Corruption","Court","Election","Executive order","Fbi","Iran","Israel",
        "Lawsuit","Nuclear","Putin","Russia","Saudi","Trump","Voter","Injunction","Rico","Conspiracy theory",
        "Qanon","Ufo","Maha","Netanyahu","Erdogan","Lavrov","Board of peace","Congo","Sahel"
    ]

def serialize_story(s):
    ts = s.get("added_at")
    if isinstance(ts, datetime):
        ts = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    elif ts is not None:
        ts = str(ts)
    return {
        "title": s.get("title"),
        "link": s.get("link"),
        "topic": s.get("topic") or "",
        "summary": s.get("summary") or "",
        "added_at": ts or "",
    }

@app.get("/health")
def health():
    return "ok", 200

@app.get("/api/stories")
def api_stories():
    q = request.args.get("q", "").strip() or None
    topic = request.args.get("topic", "").strip() or None
    page = max(int(request.args.get("page", "1") or "1"), 1)
    limit = max(int(request.args.get("limit", str(PAGE_SIZE_DEFAULT)) or PAGE_SIZE_DEFAULT), 1)

    if topic:
        rows = get_topic_stories(topic, limit=limit, page=page)
    else:
        rows = get_recent_stories(limit=limit, search=q, page=page)

    return jsonify({
        "page": page,
        "limit": limit,
        "count": len(rows),
        "stories": [serialize_story(r) for r in rows],
    })

BASE_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{{ page_title }}</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; margin:0; line-height:1.45; }
    header { position: sticky; top: 0; backdrop-filter: blur(8px); background: rgba(255,255,255,.85); border-bottom: 1px solid rgba(0,0,0,.08); }
    @media (prefers-color-scheme: dark){ header{ background: rgba(0,0,0,.55); border-bottom-color: rgba(255,255,255,.10);} }
    .wrap { max-width: 980px; margin: 0 auto; padding: 14px 16px; }
    .row { display:flex; gap:10px; flex-wrap: wrap; align-items:center; justify-content: space-between; }
    .brand { font-weight: 800; letter-spacing: .3px; }
    .muted { opacity: .75; font-size: 12px; }
    .topics { display:flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .chip { font-size: 12px; padding: 6px 10px; border-radius: 999px; border:1px solid rgba(0,0,0,.12); text-decoration:none; }
    @media (prefers-color-scheme: dark){ .chip{ border-color: rgba(255,255,255,.18);} }
    form { display:flex; gap:8px; align-items:center; }
    input[type="search"] { padding: 10px 12px; border-radius: 10px; border:1px solid rgba(0,0,0,.15); min-width: 260px; }
    @media (prefers-color-scheme: dark){ input[type="search"]{ border-color: rgba(255,255,255,.20);} }
    button { padding: 10px 12px; border-radius: 10px; border:1px solid rgba(0,0,0,.15); cursor:pointer; }
    @media (prefers-color-scheme: dark){ button{ border-color: rgba(255,255,255,.20);} }
    main { padding: 18px 0 44px; }
    .ad { border:1px dashed rgba(0,0,0,.25); border-radius: 14px; padding: 12px; margin: 14px 0 18px; opacity:.75; }
    @media (prefers-color-scheme: dark){ .ad{ border-color: rgba(255,255,255,.25);} }
    .card { border:1px solid rgba(0,0,0,.10); border-radius: 16px; padding: 14px 14px 12px; margin: 10px 0; box-shadow: 0 6px 18px rgba(0,0,0,.04); }
    @media (prefers-color-scheme: dark){ .card{ border-color: rgba(255,255,255,.12); box-shadow:none;} }
    .title { font-weight: 700; text-decoration:none; }
    .meta { margin-top: 8px; display:flex; gap:10px; flex-wrap: wrap; align-items:center; }
    .pill { font-size: 12px; padding: 3px 8px; border-radius: 999px; border:1px solid rgba(0,0,0,.12); opacity:.9; }
    @media (prefers-color-scheme: dark){ .pill{ border-color: rgba(255,255,255,.18);} }
    .summary { margin-top: 10px; opacity: .95; }
    .footer { margin-top: 26px; opacity:.7; font-size: 12px; }
    .center { text-align:center; }
  </style>
</head>
<body>
<header>
  <div class="wrap">
    <div class="row">
      <div>
        <div class="brand">News Aggregator</div>
        <div class="muted">Real-time updates on your tracked topics{% if last_updated_iso %} • Last updated: {{ last_updated_iso }}{% endif %}</div>
      </div>

      <form method="get" action="{{ search_action }}">
        <input type="search" name="q" placeholder="Search titles, topics, summaries" value="{{ q }}" />
        <button type="submit">Search</button>
      </form>
    </div>

    <div class="topics">
      <a class="chip" href="{{ url_for('home') }}">All Topics</a>
      {% for t in topics %}
        <a class="chip" href="{{ url_for('topic_page', topic=t) }}">{{ t|capitalize }}</a>
      {% endfor %}
    </div>
  </div>
</header>

<div class="wrap">
  <div class="ad">Advertisement / Sponsored Content Placeholder (728×90)</div>

  <main>
    <h2 style="margin: 0 0 10px;">{{ heading }}</h2>

    <div id="stories">
      {% for story in stories %}
        <article class="card">
          <a class="title" href="{{ story['link'] }}" target="_blank" rel="noopener noreferrer">{{ story['title'] }}</a>
          {% if story['summary'] %}
            <div class="summary">{{ story['summary'] | replace('\\n','<br>') | safe }}</div>
          {% endif %}
          <div class="meta">
            <span class="pill">{{ (story['topic'] or '') | capitalize }}</span>
            {% if story['added_at'] %}<span class="muted">{{ story['added_at'] }}</span>{% endif %}
          </div>
        </article>
      {% endfor %}
    </div>

    <div class="center" style="margin-top: 14px;">
      <button id="loadMore" data-page="{{ page }}" data-topic="{{ topic or '' }}" data-q="{{ q }}">Load more</button>
      <div id="loadStatus" class="muted" style="margin-top:8px;"></div>
    </div>

    <div class="footer">© {{ now_year }} News Aggregator</div>
  </main>
</div>

<script>
(function() {
  const btn = document.getElementById('loadMore');
  const status = document.getElementById('loadStatus');
  const list = document.getElementById('stories');

  function escapeHtml(s) {
    return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  async function loadNext() {
    const nextPage = (parseInt(btn.dataset.page || '1', 10) + 1);
    const q = btn.dataset.q || '';
    const topic = btn.dataset.topic || '';
    const params = new URLSearchParams({ page: String(nextPage) });
    if (q) params.set('q', q);
    if (topic) params.set('topic', topic);

    btn.disabled = true;
    status.textContent = 'Loading…';

    try {
      const res = await fetch('/api/stories?' + params.toString(), { headers: { 'Accept': 'application/json' } });
      const data = await res.json();

      if (!data.stories || data.stories.length === 0) {
        status.textContent = 'No more stories.';
        btn.style.display = 'none';
        return;
      }

      for (const s of data.stories) {
        const el = document.createElement('article');
        el.className = 'card';
        el.innerHTML = `
          <a class="title" href="${escapeHtml(s.link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(s.title)}</a>
          ${s.summary ? `<div class="summary">${escapeHtml(s.summary).replace(/\\n/g,'<br>')}</div>` : ''}
          <div class="meta">
            <span class="pill">${escapeHtml((s.topic||'').toString()).replace(/^./, c => c.toUpperCase())}</span>
            ${s.added_at ? `<span class="muted">${escapeHtml(s.added_at)}</span>` : ''}
          </div>
        `;
        list.appendChild(el);
      }

      btn.dataset.page = String(nextPage);
      status.textContent = '';
    } catch (e) {
      status.textContent = 'Error loading more. Try again.';
      console.log(e);
    } finally {
      btn.disabled = false;
    }
  }

  btn.addEventListener('click', loadNext);
})();
</script>
</body>
</html>
"""

@app.route("/")
def home():
    q = request.args.get("q", "").strip()
    page = max(int(request.args.get("page", "1") or "1"), 1)

    topics = get_all_topics()
    last_updated_iso = get_latest_update_iso()
    stories = get_recent_stories(limit=PAGE_SIZE_DEFAULT, search=q if q else None, page=page)
    now_year = datetime.now().year

    return render_template_string(
        BASE_HTML,
        page_title="News Aggregator",
        heading="Latest Stories",
        stories=stories,
        topics=topics,
        q=q,
        page=page,
        topic=None,
        search_action=url_for("home"),
        last_updated_iso=last_updated_iso,
        now_year=now_year,
    )

@app.route("/topic/<topic>")
def topic_page(topic):
    page = max(int(request.args.get("page", "1") or "1"), 1)
    topics = get_all_topics()
    last_updated_iso = get_latest_update_iso()
    stories = get_topic_stories(topic, limit=PAGE_SIZE_DEFAULT, page=page)
    now_year = datetime.now().year

    return render_template_string(
        BASE_HTML,
        page_title=f"{topic.capitalize()} - News Aggregator",
        heading=f"{topic.capitalize()} News",
        stories=stories,
        topics=topics,
        q="",
        page=page,
        topic=topic,
        search_action=url_for("topic_page", topic=topic),
        last_updated_iso=last_updated_iso,
        now_year=now_year,
    )

if __name__ == "__main__":
    app.run(debug=True, port=5000)
'@

Set-Content -Path $path -Value $py -Encoding utf8
"OK: app.py overwritten with Python (no PowerShell inside)"
