import os
import re
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

# small in-process cache (helps during deploy/health checks)
CACHE_TTL_SECONDS = 10
_cache = {}  # key -> (expires_epoch, value)


# ---------------- Topic normalization ----------------
CANON_TOPIC = {
    "fbi": "FBI",
    "ufo": "UFO",
    "qanon": "QAnon",
    "rico": "RICO",
    "executive order": "Executive Order",
    "conspiracy theory": "Conspiracy Theory",
    "board of peace": "Board of Peace",
    "sahel": "Sahel",
    "congo": "Congo",
    "maha": "MAHA",
    "dni": "DNI",
}


def normalize_topic_label(topic: str) -> str:
    t = (topic or "").strip()
    if not t:
        return ""
    key = t.lower()
    if key in CANON_TOPIC:
        return CANON_TOPIC[key]
    if t.isupper() and len(t) <= 8:
        return t
    return t.title()


def normalize_summary_for_display(text: str) -> str:
    """
    Bulletproof conversion of any <br> variants (and escaped &lt;br&gt;) into real newlines.
    This ensures the UI never shows literal '<br>'.
    """
    if not text:
        return ""

    t = str(text)

    # handle escaped <br> forms first
    t = t.replace("&lt;br&gt;", "\n").replace("&lt;br/&gt;", "\n").replace("&lt;br /&gt;", "\n")

    # handle ANY <br> tag variant: <br>, <br/>, <br />, <br >, any case
    t = re.sub(r"(?i)<br\s*/?\s*>", "\n", t)

    # normalize line endings
    t = t.replace("\r\n", "\n").replace("\r", "\n")

    return t.strip()


# ---------------- DB helpers ----------------
def using_postgres() -> bool:
    return bool(DATABASE_URL)


def pg_connect():
    """
    Fail-fast. statement_timeout is ms.
    """
    if psycopg is None:
        raise RuntimeError("psycopg not installed. Add psycopg[binary] to requirements.txt")
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
    """
    Case-insensitive match so clicking 'Trump' includes rows where topic is 'trump', 'TRUMP', etc.
    """
    offset = max(page - 1, 0) * limit
    cache_key = ("topic", (topic or "").lower(), limit, page, "pg" if using_postgres() else "sqlite")
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
        "Bitcoin",
        "China",
        "Conspiracy",
        "Corruption",
        "Court",
        "Election",
        "Executive Order",
        "FBI",
        "Iran",
        "Israel",
        "Lawsuit",
        "Nuclear",
        "Putin",
        "Russia",
        "Saudi",
        "Trump",
        "Voter",
        "Injunction",
        "RICO",
        "Conspiracy Theory",
        "QAnon",
        "UFO",
        "MAHA",
        "Netanyahu",
        "Erdogan",
        "Lavrov",
        "Board of Peace",
        "Congo",
        "Sahel",
    ]


def serialize_story(s):
    ts = s.get("added_at")
    if isinstance(ts, datetime):
        ts = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    elif ts is not None:
        ts = str(ts)

    topic_raw = s.get("topic") or ""
    summary_raw = s.get("summary") or ""

    return {
        "title": s.get("title") or "",
        "link": s.get("link") or "",
        "topic": topic_raw,
        "topic_label": normalize_topic_label(topic_raw),
        "summary": normalize_summary_for_display(summary_raw),
        "added_at": ts or "",
    }


# ---------------- Fast health endpoint (NO DB) ----------------
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

    return jsonify(
        {
            "page": page,
            "limit": limit,
            "count": len(rows),
            "stories": [serialize_story(r) for r in rows],
        }
    )


BASE_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{{ page_title }}</title>
  <style>
    :root{
      --bg:#0a0f16;
      --panel:#0f1824;
      --panel2:#0c131d;

      --text:#e7eef7;
      --muted:#9fb0c5;

      --pillBorder:rgba(255,255,255,.16);

      --red:#ff2a2a;
      --red2:#d91f1f;

      --link:#8ab4ff;
      --shadow: 0 12px 40px rgba(0,0,0,.55);
    }

    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",Arial;
      color:var(--text);
      background:
        radial-gradient(1100px 600px at 20% -10%, rgba(255,42,42,.12), transparent 60%),
        radial-gradient(900px 520px at 80% 0%, rgba(138,180,255,.10), transparent 55%),
        linear-gradient(180deg, var(--bg), #070b10 60%);
      line-height:1.45;
    }
    a{color:var(--link);text-decoration:none}
    a:hover{text-decoration:underline}

    .wrap{max-width:1100px;margin:0 auto;padding:16px}

    .top{
      border-radius:22px;
      padding:16px 16px 14px;
      border:1px solid rgba(255,255,255,.10);
      background: linear-gradient(180deg, rgba(15,24,36,.92), rgba(12,19,29,.72));
      box-shadow: var(--shadow);
    }
    .title{font-size:28px;font-weight:900;margin:0;letter-spacing:.2px}
    .sub{margin:6px 0 0;color:var(--muted);font-size:13px}

    .searchbar{margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    .searchbar input{
      flex:1;
      min-width:min(520px, 78vw);
      padding:12px 14px;
      border-radius:14px;
      border:1px solid rgba(255,255,255,.14);
      background: rgba(7,11,16,.75);
      color:var(--text);
      outline:none;
    }
    .searchbar input:focus{
      border-color: rgba(138,180,255,.35);
      box-shadow: 0 0 0 3px rgba(138,180,255,.12);
    }
    .searchbar button{
      padding:12px 16px;
      border-radius:14px;
      border:1px solid rgba(255,42,42,.55);
      background:linear-gradient(180deg,var(--red),var(--red2));
      color:white;
      font-weight:900;
      cursor:pointer;
      box-shadow: 0 10px 26px rgba(255,42,42,.18);
    }
    .searchbar button:hover{filter:brightness(1.05)}
    .searchbar button:active{transform:translateY(1px)}

    .pills{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}
    .pill{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding:9px 12px;
      border-radius:999px;
      border:1px solid var(--pillBorder);
      background: linear-gradient(180deg, rgba(18,31,46,.95), rgba(10,18,28,.85));
      color:var(--text);
      font-size:13px;
      transition: all .12s ease;
    }
    .pill:hover{
      background: linear-gradient(180deg, rgba(27,44,63,.95), rgba(12,21,33,.85));
      border-color: rgba(138,180,255,.22);
      transform: translateY(-1px);
    }
    .pill.active{
      border-color: rgba(255,42,42,.70);
      box-shadow: 0 0 0 3px rgba(255,42,42,.12) inset, 0 10px 26px rgba(255,42,42,.12);
    }

    .ad{
      margin:16px 0 10px;
      border:1px dashed rgba(255,255,255,.26);
      border-radius:18px;
      padding:14px;
      background: rgba(255,255,255,.03);
      color:var(--muted);
      text-align:center;
    }

    h2{margin:18px 0 10px;font-size:28px}

    .cards{margin-top:10px}
    .card{
      border:1px solid rgba(255,255,255,.10);
      border-radius:22px;
      padding:16px;
      background: linear-gradient(180deg, rgba(13,22,33,.82), rgba(10,16,24,.72));
      box-shadow: 0 16px 44px rgba(0,0,0,.38);
      margin:14px 0;
    }
    .card h3{margin:0 0 10px;font-size:18px;line-height:1.25}
    .summary{
      margin-top:6px;
      color:#d7e2f1;
    }
    .meta{
      margin-top:12px;
      color:var(--muted);
      font-size:12px;
      display:flex;
      gap:12px;
      flex-wrap:wrap;
      align-items:center;
    }
    .topicTag{
      padding:4px 10px;
      border-radius:999px;
      border:1px solid rgba(255,255,255,.14);
      background: rgba(255,255,255,.04);
      color:#cfe0f7;
      font-weight:700;
      letter-spacing:.2px;
    }

    .btnrow{margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    .readbtn{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding:11px 14px;
      border-radius:14px;
      border:1px solid rgba(255,42,42,.55);
      background:linear-gradient(180deg,var(--red),var(--red2));
      color:white;
      font-weight:900;
    }
    .readbtn:hover{filter:brightness(1.05)}
    .readbtn:active{transform:translateY(1px)}

    .loadwrap{margin:18px 0;display:flex;justify-content:center}
    #loadMore{
      padding:12px 18px;
      border-radius:16px;
      border:1px solid rgba(255,42,42,.55);
      background:linear-gradient(180deg,var(--red),var(--red2));
      color:white;
      font-weight:950;
      cursor:pointer;
      box-shadow: 0 14px 34px rgba(255,42,42,.16);
    }
    #loadMore:hover{filter:brightness(1.05)}
    #loadMore:active{transform:translateY(1px)}
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

      <div class="pills">
        <a class="pill {% if not active_topic %}active{% endif %}" href="{{ url_for('home') }}">All Topics</a>
        {% for t in topics %}
          <a class="pill {% if active_topic and active_topic|lower == t|lower %}active{% endif %}"
             href="{{ url_for('topic_page', topic=t) }}">{{ t }}</a>
        {% endfor %}
      </div>
    </div>

    <div class="ad">Advertisement / Sponsored Content Placeholder (728×90)</div>

    <h2>{{ heading }}</h2>

    <div id="stories" class="cards">
      {% if stories %}
        {% for story in stories %}
          <div class="card">
            <h3>{{ story['title'] }}</h3>

            {% if story['summary'] %}
              <div class="summary">
                {{ (story['summary'] or '') | e | replace('\n','<br>') | safe }}
              </div>
            {% endif %}

            <div class="meta">
              <span class="topicTag">{{ story['topic_label'] }}</span>
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
    const summaryText = (s.summary || '');
    const summaryHtml = summaryText
      ? `<div class="summary">${escapeHtml(summaryText).replace(/\\n/g,'<br>')}</div>`
      : '';

    const topicLabel = escapeHtml(s.topic_label || s.topic || '');
    const added = escapeHtml(s.added_at || '');
    const title = escapeHtml(s.title || '');
    const link = escapeHtml(s.link || '#');

    return `
      <div class="card">
        <h3>${title}</h3>
        ${summaryHtml}
        <div class="meta">
          <span class="topicTag">${topicLabel}</span>
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

      // APPEND
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
    stories = [serialize_story(s) for s in stories]
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
    stories = [serialize_story(s) for s in stories]
    now_year = datetime.now().year

    return render_template_string(
        BASE_HTML,
        page_title=f"{normalize_topic_label(topic)} - News Aggregator",
        heading=f"{normalize_topic_label(topic)} News",
        stories=stories,
        topics=topics,
        q="",
        page=page,
        active_topic=topic,
        search_action=url_for("topic_page", topic=topic),
        last_updated_iso=last_updated_iso,
        now_year=now_year,
    )


# ---------------- Entrypoint ----------------
if __name__ == "__main__":
    app.run(debug=True, port=5000)
