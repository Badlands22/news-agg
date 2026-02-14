import os
import sqlite3
import time
from datetime import datetime, timezone
from flask import Flask, render_template_string, request, jsonify, url_for

# Postgres (Render) - psycopg v3
try:
    import psycopg  # type: ignore
except Exception:
    psycopg = None

app = Flask(__name__)

DB_PATH = os.getenv("DB_PATH", "news.db")
DATABASE_URL = os.getenv("DATABASE_URL")

PAGE_SIZE_DEFAULT = 12

# Small in-process cache to prevent hammering DB during deploy/health checks
CACHE_TTL_SECONDS = 10
_cache = {}  # key -> (expires_epoch, value)


# ---------------- DB helpers ----------------
def using_postgres() -> bool:
    return bool(DATABASE_URL)


def pg_connect():
    """
    Fail fast. Render will kill the service if requests hang.
    statement_timeout is in ms.
    """
    if psycopg is None:
        raise RuntimeError("psycopg is not installed. Add psycopg[binary] to requirements.txt")

    conn = psycopg.connect(
        DATABASE_URL,
        connect_timeout=5,
        options="-c statement_timeout=5000",
        application_name="news_agg",
    )
    conn.autocommit = True
    return conn


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
    """Return list[dict] rows for both Postgres and SQLite. Fail-fast on errors."""
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


# ---------------- Queries ----------------
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
    # Keep your full, always-visible topic list
    return [
        "Bitcoin", "China", "Conspiracy", "Corruption", "Court", "Election", "Executive order",
        "Fbi", "Iran", "Israel", "Lawsuit", "Nuclear", "Putin", "Russia", "Saudi", "Trump",
        "Voter", "Injunction", "Rico", "Conspiracy theory", "Qanon", "Ufo", "Maha",
        "Netanyahu", "Erdogan", "Lavrov", "Board of peace", "Congo", "Sahel"
    ]


def serialize_story(s):
    ts = s.get("added_at")
    if isinstance(ts, datetime):
        ts = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    elif ts is not None:
        ts = str(ts)

    return {
        "title": s.get("title") or "",
        "link": s.get("link") or "",
        "topic": s.get("topic") or "",
        "summary": s.get("summary") or "",
        "added_at": ts or "",
    }


# ---------------- Fast health endpoint (NO DB!) ----------------
@app.get("/health")
def health():
    return "ok", 200


# ---------------- API for append-style pagination ----------------
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


BASE_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{{ page_title }}</title>
  <style>
    :root{
      --bg:#0b0f14; --panel:#101823; --card:#0f1722; --text:#e9eef6; --muted:#9fb0c5;
      --border:rgba(255,255,255,.10); --red:#ff2a2a; --red2:#d91f1f; --link:#8ab4ff;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",Arial;color:var(--text);background:var(--bg);line-height:1.45}
    a{color:var(--link);text-decoration:none}
    a:hover{text-decoration:underline}
    .wrap{max-width:1040px;margin:0 auto;padding:16px}
    .top{
      display:flex;align-items:flex-end;justify-content:space-between;gap:12px;flex-wrap:wrap;
      padding:14px;border:1px solid var(--border);border-radius:18px;background:linear-gradient(180deg,var(--panel),rgba(16,24,35,.55));
    }
    .title{font-size:22px;font-weight:800;margin:0}
    .sub{margin:4px 0 0;color:var(--muted);font-size:13px}
    .searchbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    .searchbar input{
      width:min(520px,70vw);padding:10px 12px;border-radius:12px;border:1px solid var(--border);
      background:#0b121c;color:var(--text);outline:none
    }
    .searchbar button{
      padding:10px 12px;border-radius:12px;border:1px solid rgba(255,42,42,.5);
      background:linear-gradient(180deg,var(--red),var(--red2));color:white;font-weight:700;cursor:pointer
    }
    .pills{margin-top:12px;display:flex;gap:8px;flex-wrap:wrap}
    .pill{
      display:inline-flex;align-items:center;gap:8px;
      padding:8px 10px;border-radius:999px;border:1px solid var(--border);background:rgba(255,255,255,.03);
      color:var(--text);font-size:13px
    }
    .pill.active{border-color:rgba(255,42,42,.75);box-shadow:0 0 0 2px rgba(255,42,42,.15) inset}
    .ad{
      margin:14px 0;border:1px dashed rgba(255,255,255,.25);border-radius:18px;padding:14px;
      background:rgba(255,255,255,.03);color:var(--muted);text-align:center
    }
    .cards{margin-top:10px}
    .card{
      border:1px solid var(--border);border-radius:18px;padding:14px;background:rgba(255,255,255,.02);
      margin:10px 0
    }
    .card h3{margin:0 0 8px;font-size:16px;line-height:1.25}
    .summary{margin-top:8px;color:#d7e2f1}
    .meta{margin-top:10px;color:var(--muted);font-size:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
    .btnrow{margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    .readbtn{
      display:inline-flex;align-items:center;gap:8px;
      padding:10px 12px;border-radius:12px;border:1px solid rgba(255,42,42,.5);
      background:linear-gradient(180deg,var(--red),var(--red2));color:white;font-weight:800
    }
    .loadwrap{margin:16px 0;display:flex;justify-content:center}
    #loadMore{
      padding:12px 16px;border-radius:14px;border:1px solid rgba(255,42,42,.55);
      background:linear-gradient(180deg,var(--red),var(--red2));color:white;font-weight:900;cursor:pointer
    }
    #loadStatus{margin-top:10px;text-align:center;color:var(--muted);font-size:12px}
    footer{margin:26px 0 10px;color:var(--muted);font-size:12px;text-align:center}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1 class="title">News Aggregator</h1>
        <div class="sub">
          Real-time updates on your tracked topics
          {% if last_updated_iso %} • Last updated: {{ last_updated_iso }}{% endif %}
        </div>
      </div>

      <form class="searchbar" method="get" action="{{ search_action }}">
        <input name="q" placeholder="Search titles, topics, summaries…" value="{{ q }}" />
        <button type="submit">Search</button>
      </form>

      <div class="pills" style="flex-basis:100%">
        <a class="pill {% if not active_topic %}active{% endif %}" href="{{ url_for('home') }}">All Topics</a>
        {% for t in topics %}
          <a class="pill {% if active_topic and active_topic|lower == t|lower %}active{% endif %}"
             href="{{ url_for('topic_page', topic=t) }}">{{ t|capitalize }}</a>
        {% endfor %}
      </div>
    </div>

    <div class="ad">Advertisement / Sponsored Content Placeholder (728×90)</div>

    <h2 style="margin:10px 0 6px;">{{ heading }}</h2>

    <div id="stories" class="cards">
      {% if stories %}
        {% for story in stories %}
          <div class="card">
            <h3>{{ story['title'] }}</h3>

            {% if story['summary'] %}
              <div class="summary">{{ story['summary'] | e | replace('\n','<br>') | safe }}</div>
            {% endif %}

            <div class="meta">
              <span>{{ (story['topic'] or '') | capitalize }}</span>
              {% if story['added_at'] %}<span>{{ story['added_at'] }}</span>{% endif %}
            </div>

            <div class="btnrow">
              <a class="readbtn" href="{{ story['link'] }}" target="_blank" rel="noopener noreferrer">Read Original →</a>
            </div>
          </div>
        {% endfor %}
      {% else %}
        <div class="card">No stories found (or DB timed out briefly). Refresh in a few seconds.</div>
      {% endif %}
    </div>

    <div class="loadwrap">
      <button id="loadMore" data-page="{{ page }}" data-topic="{{ active_topic or '' }}" data-q="{{ q }}">Load more</button>
    </div>
    <div id="loadStatus"></div>

    <footer>© {{ now_year }} News Aggregator</footer>
  </div>

<script>
(function(){
  const btn = document.getElementById('loadMore');
  const status = document.getElementById('loadStatus');
  const list = document.getElementById('stories');

  function escapeHtml(s){
    return (s || '').replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  function storyCard(s){
    const summary = s.summary ? `<div class="summary">${escapeHtml(s.summary).replace(/\\n/g,'<br>')}</div>` : '';
    const topic = escapeHtml(s.topic || '');
    const added = escapeHtml(s.added_at || '');
    const title = escapeHtml(s.title || '');
    const link = escapeHtml(s.link || '#');

    return `
      <div class="card">
        <h3>${title}</h3>
        ${summary}
        <div class="meta">
          <span>${topic}</span>
          ${added ? `<span>${added}</span>` : ``}
        </div>
        <div class="btnrow">
          <a class="readbtn" href="${link}" target="_blank" rel="noopener noreferrer">Read Original →</a>
        </div>
      </div>`;
  }

  async function loadNext(){
    const nextPage = (parseInt(btn.dataset.page || '1', 10) + 1);
    const q = btn.dataset.q || '';
    const topic = btn.dataset.topic || '';

    const params = new URLSearchParams({ page: String(nextPage) });
    if(q) params.set('q', q);
    if(topic) params.set('topic', topic);

    btn.disabled = true;
    status.textContent = 'Loading…';

    try{
      const res = await fetch('/api/stories?' + params.toString(), { headers: { 'Accept':'application/json' }});
      const data = await res.json();

      if(!data.stories || data.stories.length === 0){
        status.textContent = 'No more stories.';
        btn.style.display = 'none';
        return;
      }

      // APPEND (not replace)
      const html = data.stories.map(storyCard).join('');
      list.insertAdjacentHTML('beforeend', html);

      btn.dataset.page = String(nextPage);
      status.textContent = '';
    }catch(e){
      console.log(e);
      status.textContent = 'Error loading more. Try again.';
    }finally{
      btn.disabled = false;
    }
  }

  btn.addEventListener('click', loadNext);
})();
</script>
</body>
</html>
"""


# ---------------- Routes ----------------
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
        active_topic=None,
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
        active_topic=topic,
        search_action=url_for("topic_page", topic=topic),
        last_updated_iso=last_updated_iso,
        now_year=now_year,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
