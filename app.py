import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, render_template_string, request
try:
    import psycopg
except Exception:
    psycopg = None

app = Flask(__name__)

# ---------------- CONFIG ----------------
PAGE_SIZE = 12
DB_PATH = "news.db"  # used only when DATABASE_URL is not set
DATABASE_URL = os.getenv("DATABASE_URL")  # set on Render for Postgres


# ---------------- DB HELPERS ----------------
def using_postgres() -> bool:
    return bool(DATABASE_URL)


def pg_connect():
    if psycopg is None:
        raise RuntimeError("psycopg is not installed. Add psycopg[binary] to requirements.txt")
    return psycopg.connect(DATABASE_URL)


def sqlite_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS public.articles (
                        id SERIAL PRIMARY KEY,
                        title TEXT,
                        link TEXT UNIQUE,
                        description TEXT,
                        pub_date TEXT,
                        topic TEXT,
                        summary TEXT,
                        added_at TIMESTAMPTZ
                    );
                """)
            conn.commit()
    else:
        conn = sqlite_connect()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                link TEXT UNIQUE,
                description TEXT,
                pub_date TEXT,
                topic TEXT,
                summary TEXT,
                added_at TEXT
            )
        """)
        conn.commit()
        conn.close()


init_db()


def _dt_to_iso(dt_val):
    """
    Normalize Postgres TIMESTAMPTZ datetimes and SQLite ISO strings into ISO8601 strings.
    """
    if dt_val is None:
        return None
    if isinstance(dt_val, datetime):
        # ensure tz-aware
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=timezone.utc)
        return dt_val.astimezone(timezone.utc).isoformat()
    # assume string
    try:
        # allow "YYYY-MM-DD HH:MM:SS+00" etc
        s = str(dt_val).replace(" ", "T")
        return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat()
    except Exception:
        return str(dt_val)


def get_latest_update_iso():
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("SELECT MAX(added_at) FROM public.articles;")
                val = c.fetchone()[0]
        return _dt_to_iso(val)
    else:
        conn = sqlite_connect()
        c = conn.cursor()
        c.execute("SELECT MAX(added_at) FROM articles;")
        val = c.fetchone()[0]
        conn.close()
        return _dt_to_iso(val)


def get_recent_stories(limit=PAGE_SIZE, offset=0, search=None, topic=None):
    """
    Returns list of dicts:
    {title, link, topic, summary, added_at_iso}
    """
    if using_postgres():
        where = []
        params = []

        if topic and topic != "All Topics":
            where.append("topic = %s")
            params.append(topic)

        if search:
            where.append("(title ILIKE %s OR topic ILIKE %s OR summary ILIKE %s)")
            term = f"%{search}%"
            params.extend([term, term, term])

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT title, link, topic, summary, added_at
            FROM public.articles
            {where_sql}
            ORDER BY added_at DESC
            LIMIT %s OFFSET %s;
        """
        params.extend([limit, offset])

        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute(sql, tuple(params))
                rows = c.fetchall()

        out = []
        for r in rows:
            out.append({
                "title": r[0],
                "link": r[1],
                "topic": r[2],
                "summary": r[3],
                "added_at_iso": _dt_to_iso(r[4]),
            })
        return out

    else:
        conn = sqlite_connect()
        c = conn.cursor()

        where = []
        params = []

        if topic and topic != "All Topics":
            where.append("topic = ?")
            params.append(topic)

        if search:
            where.append("(title LIKE ? OR topic LIKE ? OR summary LIKE ?)")
            term = f"%{search}%"
            params.extend([term, term, term])

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT title, link, topic, summary, added_at
            FROM articles
            {where_sql}
            ORDER BY added_at DESC
            LIMIT ? OFFSET ?;
        """
        params.extend([limit, offset])
        c.execute(sql, tuple(params))
        rows = c.fetchall()
        conn.close()

        out = []
        for r in rows:
            out.append({
                "title": r["title"],
                "link": r["link"],
                "topic": r["topic"],
                "summary": r["summary"],
                "added_at_iso": _dt_to_iso(r["added_at"]),
            })
        return out


def get_total_count(search=None, topic=None):
    if using_postgres():
        where = []
        params = []

        if topic and topic != "All Topics":
            where.append("topic = %s")
            params.append(topic)

        if search:
            where.append("(title ILIKE %s OR topic ILIKE %s OR summary ILIKE %s)")
            term = f"%{search}%"
            params.extend([term, term, term])

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"SELECT COUNT(*) FROM public.articles {where_sql};"

        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute(sql, tuple(params))
                return int(c.fetchone()[0])

    else:
        conn = sqlite_connect()
        c = conn.cursor()

        where = []
        params = []

        if topic and topic != "All Topics":
            where.append("topic = ?")
            params.append(topic)

        if search:
            where.append("(title LIKE ? OR topic LIKE ? OR summary LIKE ?)")
            term = f"%{search}%"
            params.extend([term, term, term])

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"SELECT COUNT(*) FROM articles {where_sql};"
        c.execute(sql, tuple(params))
        n = int(c.fetchone()[0])
        conn.close()
        return n


def get_all_topics():
    # Keep this identical to your current list (so UI stays the same)
    return [
        "All Topics", "Bitcoin", "China", "Conspiracy", "Corruption", "Court", "Election",
        "Executive order", "Fbi", "Iran", "Israel", "Lawsuit", "Nuclear",
        "Putin", "Russia", "Saudi", "Trump", "Voter",
        "Injunction", "Rico", "Conspiracy theory", "Qanon", "Ufo", "Maha",
        "Netanyahu", "Erdogan", "Lavrov", "Board of peace", "Congo", "Sahel"
    ]


# ---------------- UI ----------------
TEMPLATE = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>News Aggregator</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' }</script>
</head>

<body class="bg-gray-950 text-gray-200 min-h-screen">
  <header class="bg-gradient-to-r from-blue-700 to-indigo-800 py-8 shadow-lg">
    <div class="container mx-auto px-6 text-center">
      <h1 class="text-5xl font-bold">News Aggregator</h1>
      <p class="mt-2 text-blue-200">Real-time updates on your tracked topics</p>
      <p class="mt-1 text-sm text-blue-300">
        Last updated:
        <span id="lastUpdated" data-iso="{{ last_updated_iso or '' }}"></span>
      </p>
    </div>
  </header>

  <!-- Topic Tabs / Dropdown -->
  <div class="bg-gray-900 border-b border-gray-800">
    <div class="container mx-auto px-6">
      <div class="hidden md:flex flex-wrap gap-3 py-4 justify-center">
        <a href="/?{{ qs_no_page }}"
           class="px-5 py-2.5 rounded-full font-medium transition {{ 'bg-red-600 text-white' if active_topic == 'All Topics' else 'bg-gray-800 hover:bg-gray-700' }}">
          All Topics
        </a>
        {% for t in topics if t != 'All Topics' %}
          <a href="/topic/{{ t | urlencode }}?{{ qs_no_page }}"
             class="px-5 py-2.5 rounded-full font-medium transition {{ 'bg-red-600 text-white' if t == active_topic else 'bg-gray-800 hover:bg-gray-700' }}">
            {{ t | capitalize }}
          </a>
        {% endfor %}
      </div>

      <div class="md:hidden py-4">
        <select onchange="window.location.href=this.value"
                class="w-full bg-gray-800 border border-gray-700 rounded-full px-5 py-3 text-lg">
          <option value="/?{{ qs_no_page }}" {{ 'selected' if active_topic == 'All Topics' else '' }}>All Topics</option>
          {% for t in topics if t != 'All Topics' %}
            <option value="/topic/{{ t | urlencode }}?{{ qs_no_page }}" {{ 'selected' if t == active_topic else '' }}>
              {{ t | capitalize }}
            </option>
          {% endfor %}
        </select>
      </div>
    </div>
  </div>

  <!-- Search Bar -->
  <div class="container mx-auto px-6 py-6">
    <form method="GET" class="max-w-3xl mx-auto">
      <input type="hidden" name="page" value="1" />
      <input type="text" name="q" value="{{ q }}"
             placeholder="Search titles or topics..."
             class="w-full bg-gray-900 border border-gray-700 rounded-2xl px-6 py-4 text-lg focus:outline-none focus:border-blue-500">
    </form>
  </div>

  <!-- Ad Banner -->
  <div class="container mx-auto px-6 pb-6">
    <div class="max-w-3xl mx-auto mb-8 bg-gray-900 rounded-2xl p-6 text-center text-gray-500 border border-gray-800">
      <p>Advertisement / Sponsored Content Placeholder</p>
      <p class="text-xs mt-1 text-gray-500">(728x90 leaderboard - static, below search bar)</p>
    </div>
  </div>

  <!-- Stories Grid -->
  <main class="container mx-auto px-6 pb-12">
    <h2 class="text-3xl font-bold mb-8">Latest Stories</h2>

    {% if stories %}
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {% for story in stories %}
          <div class="bg-gray-900 rounded-2xl overflow-hidden border border-gray-800 hover:border-blue-600 transition">
            <div class="p-6">
              <h3 class="text-xl font-semibold mb-4">{{ story.title }}</h3>

              {# Hide junk summaries (too short or basically title) #}
              {% set s = (story.summary or '').strip() %}
              {% set t = (story.title or '').strip() %}
              {% if s and (s|length > 60) and (s|lower != t|lower) %}
                <div class="text-gray-400 text-sm whitespace-pre-line leading-relaxed mb-6">
                  {{ s | replace('\\n', '<br>') | safe }}
                </div>
              {% endif %}

              <div class="flex items-center justify-between">
                <span class="bg-blue-900 text-blue-300 px-4 py-1 rounded-full text-xs font-medium">
                  {{ (story.topic or '') | capitalize }}
                </span>
                <span class="text-gray-500 text-sm storyTime"
                      data-iso="{{ story.added_at_iso or '' }}"></span>
              </div>

              <a href="{{ story.link }}" target="_blank"
                 class="mt-6 block text-center bg-red-600 hover:bg-red-500 transition py-3 rounded-xl font-medium">
                Read Original →
              </a>
            </div>
          </div>
        {% endfor %}
      </div>

      <!-- Load More -->
      {% if has_more %}
        <div class="mt-10 flex justify-center">
          <a href="{{ next_url }}"
             class="bg-gray-800 hover:bg-gray-700 border border-gray-700 px-8 py-4 rounded-2xl text-lg font-medium transition">
            Load more
          </a>
        </div>
      {% endif %}

    {% else %}
      <p class="text-center text-gray-400">No stories found. Try another search.</p>
    {% endif %}
  </main>

  <footer class="bg-gray-950 py-8 text-center text-gray-500 text-sm">
    © {{ now_year }} News Aggregator
  </footer>

  <script>
    function formatLocal(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return "";
      // local timezone automatically
      return new Intl.DateTimeFormat(undefined, {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false
      }).format(d);
    }

    // Last updated (local)
    const lu = document.getElementById("lastUpdated");
    if (lu) {
      lu.textContent = formatLocal(lu.dataset.iso);
    }

    // Each story time (local)
    document.querySelectorAll(".storyTime").forEach(el => {
      el.textContent = formatLocal(el.dataset.iso);
    });
  </script>
</body>
</html>
"""


def _build_qs_no_page(q: str):
    # preserve search term only (no page)
    from urllib.parse import urlencode
    params = {}
    if q:
        params["q"] = q
    return urlencode(params)


def _clamp_page(p):
    try:
        p = int(p)
    except Exception:
        p = 1
    return max(1, p)


@app.route("/")
def home():
    q = (request.args.get("q", "") or "").strip()
    page = _clamp_page(request.args.get("page", "1"))
    offset = (page - 1) * PAGE_SIZE

    stories = get_recent_stories(limit=PAGE_SIZE, offset=offset, search=q if q else None, topic="All Topics")
    total = get_total_count(search=q if q else None, topic="All Topics")

    has_more = offset + PAGE_SIZE < total
    qs_no_page = _build_qs_no_page(q)
    next_url = f"/?{qs_no_page}&page={page+1}" if qs_no_page else f"/?page={page+1}"

    return render_template_string(
        TEMPLATE,
        stories=stories,
        topics=get_all_topics(),
        active_topic="All Topics",
        q=q,
        page=page,
        has_more=has_more,
        next_url=next_url,
        qs_no_page=qs_no_page,
        last_updated_iso=get_latest_update_iso(),
        now_year=datetime.now().year
    )


@app.route("/topic/<topic>")
def topic_page(topic):
    q = (request.args.get("q", "") or "").strip()
    page = _clamp_page(request.args.get("page", "1"))
    offset = (page - 1) * PAGE_SIZE

    stories = get_recent_stories(limit=PAGE_SIZE, offset=offset, search=q if q else None, topic=topic)
    total = get_total_count(search=q if q else None, topic=topic)

    has_more = offset + PAGE_SIZE < total
    qs_no_page = _build_qs_no_page(q)
    next_url = f"/topic/{topic}?{qs_no_page}&page={page+1}" if qs_no_page else f"/topic/{topic}?page={page+1}"

    return render_template_string(
        TEMPLATE,
        stories=stories,
        topics=get_all_topics(),
        active_topic=topic,
        q=q,
        page=page,
        has_more=has_more,
        next_url=next_url,
        qs_no_page=qs_no_page,
        last_updated_iso=get_latest_update_iso(),
        now_year=datetime.now().year
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
