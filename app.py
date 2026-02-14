import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, render_template_string, request

# Postgres (Render)
try:
    import psycopg
except Exception:
    psycopg = None

app = Flask(__name__)

DB_PATH = os.getenv("DB_PATH", "news.db")
DATABASE_URL = os.getenv("DATABASE_URL")


# ---------------- DB helpers ----------------
def using_postgres() -> bool:
    return bool(DATABASE_URL)


def pg_connect():
    if psycopg is None:
        raise RuntimeError("psycopg is not installed. Add psycopg[binary] to requirements.txt")
    # connect_timeout prevents long hangs that can make Render think the service is down
    return psycopg.connect(DATABASE_URL, connect_timeout=5)


def sqlite_connect():
    return sqlite3.connect(DB_PATH)


def fetch_rows(query: str, params: tuple = ()):
    """
    Return list[dict] rows for both Postgres and SQLite.
    """
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


def fetch_one(query: str, params: tuple = ()):
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


# ---------------- Queries ----------------
PAGE_SIZE_DEFAULT = 12


def get_recent_stories(limit=12, search=None, page=1):
    offset = max(page - 1, 0) * limit

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
            return fetch_rows(q, (term, term, term, limit, offset))
        else:
            q = """
                SELECT title, link, topic, summary, added_at
                FROM articles
                WHERE title LIKE ? OR topic LIKE ? OR summary LIKE ?
                ORDER BY added_at DESC
                LIMIT ? OFFSET ?
            """
            return fetch_rows(q, (term, term, term, limit, offset))
    else:
        if using_postgres():
            q = """
                SELECT title, link, topic, summary, added_at
                FROM public.articles
                ORDER BY added_at DESC
                LIMIT %s OFFSET %s
            """
            return fetch_rows(q, (limit, offset))
        else:
            q = """
                SELECT title, link, topic, summary, added_at
                FROM articles
                ORDER BY added_at DESC
                LIMIT ? OFFSET ?
            """
            return fetch_rows(q, (limit, offset))


def get_topic_stories(topic, limit=12, page=1):
    offset = max(page - 1, 0) * limit

    if using_postgres():
        q = """
            SELECT title, link, topic, summary, added_at
            FROM public.articles
            WHERE topic = %s
            ORDER BY added_at DESC
            LIMIT %s OFFSET %s
        """
        return fetch_rows(q, (topic, limit, offset))
    else:
        q = """
            SELECT title, link, topic, summary, added_at
            FROM articles
            WHERE topic = ?
            ORDER BY added_at DESC
            LIMIT ? OFFSET ?
        """
        return fetch_rows(q, (topic, limit, offset))


def get_latest_update_iso():
    if using_postgres():
        q = "SELECT MAX(added_at) FROM public.articles"
    else:
        q = "SELECT MAX(added_at) FROM articles"
    val = fetch_one(q)
    if not val:
        return None

    # Postgres returns datetime already; SQLite returns string
    if isinstance(val, datetime):
        # ensure tz-aware and ISO
        dt = val
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    else:
        # assume ISO-ish string
        try:
            # SQLite often stores ISO string
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            return None


def get_all_topics():
    # Keep your full list (including All Topics shown separately in UI)
    return [
        "Bitcoin", "China", "Conspiracy", "Corruption", "Court", "Election",
        "Executive order", "Fbi", "Iran", "Israel", "Lawsuit", "Nuclear",
        "Putin", "Russia", "Saudi", "Trump", "Voter",
        "Injunction", "Rico", "Conspiracy theory", "Qanon", "Ufo", "Maha",
        "Netanyahu", "Erdogan", "Lavrov", "Board of peace", "Congo", "Sahel"
    ]


# ---------------- Fast health endpoint (Render) ----------------
@app.get("/health")
def health():
    return "ok", 200


# ---------------- Routes ----------------
@app.route("/")
def home():
    q = request.args.get("q", "").strip()
    page = int(request.args.get("page", "1") or "1")
    page = max(page, 1)

    topics = get_all_topics()
    last_updated_iso = get_latest_update_iso()

    stories = get_recent_stories(
        limit=PAGE_SIZE_DEFAULT,
        search=q if q else None,
        page=page
    )

    now_year = datetime.now().year

    html = """
    <!DOCTYPE html>
    <html lang="en" class="dark">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>News Aggregator</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script>
            tailwind.config = { darkMode: 'class' }
        </script>
    </head>
    <body class="bg-gray-950 text-gray-200 min-h-screen">
        <header class="bg-gradient-to-r from-blue-700 to-indigo-800 py-8 shadow-lg">
            <div class="container mx-auto px-6 text-center">
                <h1 class="text-5xl font-bold">News Aggregator</h1>
                <p class="mt-2 text-blue-200">Real-time updates on your tracked topics</p>

                {% if last_updated_iso %}
                  <p class="mt-1 text-sm text-blue-300">
                    Last updated:
                    <span class="js-local-dt" data-iso="{{ last_updated_iso }}">{{ last_updated_iso }}</span>
                  </p>
                {% else %}
                  <p class="mt-1 text-sm text-blue-300">Last updated: Never</p>
                {% endif %}
            </div>
        </header>

        <!-- Topic Tabs / Dropdown -->
        <div class="bg-gray-900 border-b border-gray-800">
            <div class="container mx-auto px-6">
                <div class="hidden md:flex flex-wrap gap-3 py-4 justify-center">
                    <a href="/"
                       class="px-5 py-2.5 rounded-full font-medium transition {{ 'bg-red-600 text-white' if (active_topic is none) else 'bg-gray-800 hover:bg-gray-700' }}">
                        All Topics
                    </a>
                    {% for t in topics %}
                    <a href="/topic/{{ t | urlencode }}"
                       class="px-5 py-2.5 rounded-full font-medium transition {{ 'bg-red-600 text-white' if t == active_topic else 'bg-gray-800 hover:bg-gray-700' }}">
                        {{ t | capitalize }}
                    </a>
                    {% endfor %}
                </div>
                <div class="md:hidden py-4">
                    <select onchange="window.location.href=this.value"
                            class="w-full bg-gray-800 border border-gray-700 rounded-full px-5 py-3 text-lg">
                        <option value="/" {{ 'selected' if (active_topic is none) else '' }}>All Topics</option>
                        {% for t in topics %}
                        <option value="/topic/{{ t | urlencode }}" {{ 'selected' if t == active_topic else '' }}>
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
                        <h3 class="text-xl font-semibold mb-4">{{ story['title'] }}</h3>

                        {% if story['summary'] %}
                        <div class="text-gray-300 text-sm whitespace-pre-line leading-relaxed mb-6">
                            {{ story['summary'] | replace('\\n', '<br>') | safe }}
                        </div>
                        {% endif %}

                        <div class="flex items-center justify-between">
                            <span class="bg-blue-900 text-blue-300 px-4 py-1 rounded-full text-xs font-medium">
                                {{ (story['topic'] or '') | capitalize }}
                            </span>

                            {% if story['added_at'] %}
                              <span class="text-gray-500 text-sm js-local-dt"
                                    data-iso="{{ story['added_at'].isoformat() if story['added_at'].__class__.__name__ == 'datetime' else story['added_at'] }}">
                                {{ story['added_at'] }}
                              </span>
                            {% endif %}
                        </div>

                        <a href="{{ story['link'] }}" target="_blank"
                           class="mt-6 block text-center bg-red-600 hover:bg-red-500 transition py-3 rounded-xl font-medium">
                            Read Original →
                        </a>
                    </div>
                </div>
                {% endfor %}
            </div>

            <!-- Pagination (Load more) -->
            <div class="flex justify-center gap-4 mt-10">
                {% if page > 1 %}
                  <a class="px-6 py-3 rounded-xl bg-gray-800 hover:bg-gray-700 transition"
                     href="/?q={{ q | urlencode }}&page={{ page - 1 }}">Newer</a>
                {% endif %}
                <a class="px-6 py-3 rounded-xl bg-gray-800 hover:bg-gray-700 transition"
                   href="/?q={{ q | urlencode }}&page={{ page + 1 }}">Load more</a>
            </div>

            {% else %}
            <p class="text-center text-gray-400">No stories found. Try another search.</p>
            {% endif %}
        </main>

        <footer class="bg-gray-950 py-8 text-center text-gray-500 text-sm">
            © {{ now_year }} News Aggregator
        </footer>

        <script>
        // Convert all ISO timestamps rendered by server (UTC) into viewer's local time.
        // This is the ONLY correct way to show "each user's local time".
        function formatLocal(iso) {
          try {
            const d = new Date(iso);
            if (isNaN(d.getTime())) return null;
            // e.g. "2026-02-13 10:14 PM"
            const datePart = d.toLocaleDateString(undefined, { year:'numeric', month:'2-digit', day:'2-digit' });
            const timePart = d.toLocaleTimeString(undefined, { hour:'2-digit', minute:'2-digit' });
            return datePart + " " + timePart;
          } catch(e) { return null; }
        }

        document.querySelectorAll(".js-local-dt").forEach(el => {
          const iso = el.getAttribute("data-iso") || el.textContent;
          const v = formatLocal(iso);
          if (v) el.textContent = v;
        });
        </script>
    </body>
    </html>
    """

    return render_template_string(
        html,
        stories=stories,
        topics=topics,
        q=q,
        page=page,
        active_topic=None,
        last_updated_iso=last_updated_iso,
        now_year=now_year
    )


@app.route("/topic/<topic>")
def topic_page(topic):
    page = int(request.args.get("page", "1") or "1")
    page = max(page, 1)

    topics = get_all_topics()
    last_updated_iso = get_latest_update_iso()
    stories = get_topic_stories(topic, limit=PAGE_SIZE_DEFAULT, page=page)
    now_year = datetime.now().year

    html = """
    <!DOCTYPE html>
    <html lang="en" class="dark">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{{ topic | capitalize }} - News Aggregator</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script>
            tailwind.config = { darkMode: 'class' }
        </script>
    </head>
    <body class="bg-gray-950 text-gray-200 min-h-screen">
        <header class="bg-gradient-to-r from-blue-700 to-indigo-800 py-8 shadow-lg">
            <div class="container mx-auto px-6 text-center">
                <h1 class="text-5xl font-bold">News Aggregator</h1>
                <p class="mt-2 text-blue-200">Real-time updates on your tracked topics</p>

                {% if last_updated_iso %}
                  <p class="mt-1 text-sm text-blue-300">
                    Last updated:
                    <span class="js-local-dt" data-iso="{{ last_updated_iso }}">{{ last_updated_iso }}</span>
                  </p>
                {% else %}
                  <p class="mt-1 text-sm text-blue-300">Last updated: Never</p>
                {% endif %}
            </div>
        </header>

        <div class="bg-gray-900 border-b border-gray-800">
            <div class="container mx-auto px-6">
                <div class="hidden md:flex flex-wrap gap-3 py-4 justify-center">
                    <a href="/" class="px-6 py-2.5 rounded-full font-medium transition bg-gray-800 hover:bg-gray-700">
                        All Topics
                    </a>
                    {% for t in topics %}
                    <a href="/topic/{{ t | urlencode }}"
                       class="px-6 py-2.5 rounded-full font-medium transition {{ 'bg-red-600 text-white' if t == topic else 'bg-gray-800 hover:bg-gray-700' }}">
                        {{ t | capitalize }}
                    </a>
                    {% endfor %}
                </div>
                <div class="md:hidden py-4">
                    <select onchange="window.location.href=this.value"
                            class="w-full bg-gray-800 border border-gray-700 rounded-full px-5 py-3 text-lg">
                        <option value="/">All Topics</option>
                        {% for t in topics %}
                        <option value="/topic/{{ t | urlencode }}" {{ 'selected' if t == topic else '' }}>
                            {{ t | capitalize }}
                        </option>
                        {% endfor %}
                    </select>
                </div>
            </div>
        </div>

        <main class="container mx-auto px-6 py-10">
            <h2 class="text-3xl font-bold mb-8">{{ topic | capitalize }} News</h2>

            {% if stories %}
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {% for story in stories %}
                <div class="bg-gray-900 rounded-2xl overflow-hidden border border-gray-800 hover:border-blue-600 transition">
                    <div class="p-6">
                        <h3 class="text-xl font-semibold mb-4">{{ story['title'] }}</h3>

                        {% if story['summary'] %}
                        <div class="text-gray-300 text-sm whitespace-pre-line leading-relaxed mb-6">
                            {{ story['summary'] | replace('\\n', '<br>') | safe }}
                        </div>
                        {% endif %}

                        <div class="flex items-center justify-between">
                            <span class="bg-blue-900 text-blue-300 px-4 py-1 rounded-full text-xs font-medium">
                                {{ (story['topic'] or '') | capitalize }}
                            </span>
                            {% if story['added_at'] %}
                              <span class="text-gray-500 text-sm js-local-dt"
                                    data-iso="{{ story['added_at'].isoformat() if story['added_at'].__class__.__name__ == 'datetime' else story['added_at'] }}">
                                {{ story['added_at'] }}
                              </span>
                            {% endif %}
                        </div>

                        <a href="{{ story['link'] }}" target="_blank"
                           class="mt-6 block text-center bg-red-600 hover:bg-red-500 transition py-3 rounded-xl font-medium">
                            Read Original →
                        </a>
                    </div>
                </div>
                {% endfor %}
            </div>

            <!-- Pagination -->
            <div class="flex justify-center gap-4 mt-10">
                {% if page > 1 %}
                  <a class="px-6 py-3 rounded-xl bg-gray-800 hover:bg-gray-700 transition"
                     href="/topic/{{ topic | urlencode }}?page={{ page - 1 }}">Newer</a>
                {% endif %}
                <a class="px-6 py-3 rounded-xl bg-gray-800 hover:bg-gray-700 transition"
                   href="/topic/{{ topic | urlencode }}?page={{ page + 1 }}">Load more</a>
            </div>

            {% else %}
              <p class="text-center text-gray-400">No stories found.</p>
            {% endif %}
        </main>

        <footer class="bg-gray-950 py-8 text-center text-gray-500 text-sm">
            © {{ now_year }} News Aggregator
        </footer>

        <script>
        function formatLocal(iso) {
          try {
            const d = new Date(iso);
            if (isNaN(d.getTime())) return null;
            const datePart = d.toLocaleDateString(undefined, { year:'numeric', month:'2-digit', day:'2-digit' });
            const timePart = d.toLocaleTimeString(undefined, { hour:'2-digit', minute:'2-digit' });
            return datePart + " " + timePart;
          } catch(e) { return null; }
        }

        document.querySelectorAll(".js-local-dt").forEach(el => {
          const iso = el.getAttribute("data-iso") || el.textContent;
          const v = formatLocal(iso);
          if (v) el.textContent = v;
        });
        </script>
    </body>
    </html>
    """

    return render_template_string(
        html,
        stories=stories,
        topics=topics,
        topic=topic,
        page=page,
        last_updated_iso=last_updated_iso,
        now_year=now_year
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
