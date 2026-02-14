import os
import sqlite3
from datetime import datetime, timezone
from flask import Flask, render_template_string, request

# Optional Postgres driver (Render)
try:
    import psycopg
except Exception:
    psycopg = None

app = Flask(__name__)

DB_PATH = "news.db"
DATABASE_URL = os.getenv("DATABASE_URL")  # set on Render


# ---------------- DB CONNECTIONS ----------------
def using_postgres() -> bool:
    return bool(DATABASE_URL)


def pg_connect():
    if psycopg is None:
        raise RuntimeError("psycopg is not installed. Add psycopg[binary] to requirements.txt")
    return psycopg.connect(DATABASE_URL)


def sqlite_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    """
    Ensure the table exists in whichever DB we're using.
    """
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
        c.execute('''
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
        ''')
        conn.commit()
        conn.close()


init_db()


# ---------------- TIME HELPERS ----------------
def utc_ms_from_dt(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_from_any_added_at(value):
    """
    Convert Postgres TIMESTAMPTZ or SQLite ISO string to milliseconds since epoch (UTC).
    """
    if value is None:
        return None

    # Postgres TIMESTAMPTZ comes back as datetime
    if isinstance(value, datetime):
        return utc_ms_from_dt(value)

    # SQLite string isoformat
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return utc_ms_from_dt(dt)
        except Exception:
            return None

    return None


def display_utc_fallback(ms: int) -> str:
    """
    Server-side fallback string (UTC). Browser JS will replace with local time.
    """
    if not ms:
        return "Never"
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# ---------------- QUERIES ----------------
def get_recent_stories(limit=12, search=None):
    if using_postgres():
        q_base = """
            SELECT title, link, topic, summary, added_at
            FROM public.articles
        """
        params = []
        if search:
            q_base += " WHERE title ILIKE %s OR topic ILIKE %s OR summary ILIKE %s "
            term = f"%{search}%"
            params += [term, term, term]
        q_base += " ORDER BY added_at DESC NULLS LAST LIMIT %s;"
        params.append(limit)

        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute(q_base, params)
                rows = c.fetchall()

        stories = []
        for r in rows:
            title, link, topic, summary, added_at = r
            ms = ms_from_any_added_at(added_at)
            stories.append({
                "title": title,
                "link": link,
                "topic": topic,
                "summary": summary,
                "added_at_ms": ms,
                "added_at_display": display_utc_fallback(ms),
            })
        return stories

    # SQLite
    conn = sqlite_connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if search:
        query = """
            SELECT title, link, topic, summary, added_at
            FROM articles
            WHERE title LIKE ? OR topic LIKE ? OR summary LIKE ?
            ORDER BY added_at DESC
            LIMIT ?
        """
        term = f"%{search}%"
        c.execute(query, (term, term, term, limit))
    else:
        c.execute("""
            SELECT title, link, topic, summary, added_at
            FROM articles
            ORDER BY added_at DESC
            LIMIT ?
        """, (limit,))

    rows = c.fetchall()
    conn.close()

    stories = []
    for row in rows:
        ms = ms_from_any_added_at(row["added_at"])
        stories.append({
            "title": row["title"],
            "link": row["link"],
            "topic": row["topic"],
            "summary": row["summary"],
            "added_at_ms": ms,
            "added_at_display": display_utc_fallback(ms),
        })
    return stories


def get_topic_stories(topic, limit=12):
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT title, link, topic, summary, added_at
                    FROM public.articles
                    WHERE topic ILIKE %s
                    ORDER BY added_at DESC NULLS LAST
                    LIMIT %s;
                """, (topic, limit))
                rows = c.fetchall()

        stories = []
        for r in rows:
            title, link, topic_db, summary, added_at = r
            ms = ms_from_any_added_at(added_at)
            stories.append({
                "title": title,
                "link": link,
                "topic": topic_db,
                "summary": summary,
                "added_at_ms": ms,
                "added_at_display": display_utc_fallback(ms),
            })
        return stories

    # SQLite
    conn = sqlite_connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT title, link, topic, summary, added_at
        FROM articles
        WHERE topic = ?
        ORDER BY added_at DESC
        LIMIT ?
    """, (topic, limit))
    rows = c.fetchall()
    conn.close()

    stories = []
    for row in rows:
        ms = ms_from_any_added_at(row["added_at"])
        stories.append({
            "title": row["title"],
            "link": row["link"],
            "topic": row["topic"],
            "summary": row["summary"],
            "added_at_ms": ms,
            "added_at_display": display_utc_fallback(ms),
        })
    return stories


def get_all_topics():
    # Keep your hardcoded list exactly as you had it
    return [
        "All Topics", "Bitcoin", "China", "Conspiracy", "Corruption", "Court", "Election",
        "Executive order", "Fbi", "Iran", "Israel", "Lawsuit", "Nuclear",
        "Putin", "Russia", "Saudi", "Trump", "Voter",
        "Injunction", "Rico", "Conspiracy theory", "Qanon", "Ufo", "Maha",
        "Netanyahu", "Erdogan", "Lavrov", "Board of peace", "Congo", "Sahel"
    ]


def get_latest_update():
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("SELECT MAX(added_at) FROM public.articles;")
                val = c.fetchone()[0]
        ms = ms_from_any_added_at(val)
        return {"ms": ms, "display": display_utc_fallback(ms)}

    conn = sqlite_connect()
    c = conn.cursor()
    c.execute("SELECT MAX(added_at) FROM articles")
    val = c.fetchone()[0]
    conn.close()
    ms = ms_from_any_added_at(val)
    return {"ms": ms, "display": display_utc_fallback(ms)}


# ---------------- ROUTES ----------------
@app.route('/')
def home():
    search = request.args.get('q', '').strip()
    stories = get_recent_stories(12, search if search else None)
    topics = get_all_topics()
    last = get_latest_update()

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
                <p class="mt-1 text-sm text-blue-300">
                    Last updated:
                    <span id="last-updated" data-ms="{{ last_updated_ms }}">{{ last_updated }}</span>
                </p>
            </div>
        </header>

        <!-- Topic Tabs / Dropdown -->
        <div class="bg-gray-900 border-b border-gray-800">
            <div class="container mx-auto px-6">
                <div class="hidden md:flex flex-wrap gap-3 py-4 justify-center">
                    <a href="/" class="px-5 py-2.5 rounded-full font-medium transition {{ 'bg-red-600 text-white' if not request.args.get('q') else 'bg-gray-800 hover:bg-gray-700' }}">
                        All Topics
                    </a>
                    {% for t in topics %}
                    <a href="/topic/{{ t | urlencode }}" class="px-5 py-2.5 rounded-full font-medium transition {{ 'bg-red-600 text-white' if t == request.view_args.get('topic') else 'bg-gray-800 hover:bg-gray-700' }}">
                        {{ t | capitalize }}
                    </a>
                    {% endfor %}
                </div>
                <div class="md:hidden">
                    <select onchange="window.location.href=this.value" class="w-full bg-gray-800 border border-gray-700 rounded-full px-5 py-3 text-lg">
                        <option value="/">All Topics</option>
                        {% for t in topics %}
                        <option value="/topic/{{ t | urlencode }}" {{ 'selected' if t == request.view_args.get('topic') else '' }}>{{ t | capitalize }}</option>
                        {% endfor %}
                    </select>
                </div>
            </div>
        </div>

        <!-- Search Bar -->
        <div class="container mx-auto px-6 py-6">
            <form method="GET" class="max-w-3xl mx-auto">
                <input type="text" name="q" value="{{ request.args.get('q', '') }}"
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
                        <div class="text-gray-400 text-sm whitespace-pre-line leading-relaxed mb-6">
                            {{ story['summary'] | replace('\\n', '<br>') | safe }}
                        </div>
                        {% endif %}

                        <div class="flex items-center justify-between">
                            <span class="bg-blue-900 text-blue-300 px-4 py-1 rounded-full text-xs font-medium">
                                {{ story['topic'] | capitalize }}
                            </span>
                            <span class="text-gray-500 text-sm story-time" data-ms="{{ story['added_at_ms'] }}">
                                {{ story['added_at_display'] }}
                            </span>
                        </div>

                        <a href="{{ story['link'] }}" target="_blank"
                           class="mt-6 block text-center bg-red-600 hover:bg-red-500 transition py-3 rounded-xl font-medium">
                            Read Original →
                        </a>
                    </div>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <p class="text-center text-gray-400">No stories found. Try another search.</p>
            {% endif %}
        </main>

        <footer class="bg-gray-950 py-8 text-center text-gray-500 text-sm">
            © {{ now_year }} News Aggregator
        </footer>

        <script>
        function pad(n){ return String(n).padStart(2, "0"); }
        function formatLocalFromMs(ms){
            if (!ms) return "";
            const d = new Date(Number(ms));
            if (isNaN(d.getTime())) return "";
            return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
        }

        const lu = document.getElementById("last-updated");
        if (lu) {
            const ms = lu.getAttribute("data-ms");
            const local = formatLocalFromMs(ms);
            if (local) lu.textContent = local;
        }

        document.querySelectorAll(".story-time").forEach(el => {
            const ms = el.getAttribute("data-ms");
            const local = formatLocalFromMs(ms);
            if (local) el.textContent = local;
        });
        </script>
    </body>
    </html>
    """
    now_year = datetime.now().year
    return render_template_string(
        html,
        stories=stories,
        topics=topics,
        last_updated=last["display"],
        last_updated_ms=last["ms"],
        now_year=now_year
    )


@app.route('/topic/<topic>')
def topic_page(topic):
    stories = get_topic_stories(topic, limit=12)
    topics = get_all_topics()
    last = get_latest_update()

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
        <header class="bg-gradient-to-r from-blue-700 to-indigo-800 py-8">
            <div class="container mx-auto px-6 text-center">
                <h1 class="text-5xl font-bold">News Aggregator</h1>
                <p class="mt-2 text-blue-200">Real-time updates on your tracked topics</p>
                <p class="mt-1 text-sm text-blue-300">
                    Last updated:
                    <span id="last-updated" data-ms="{{ last_updated_ms }}">{{ last_updated }}</span>
                </p>
            </div>
        </header>

        <div class="bg-gray-900 border-b border-gray-800">
            <div class="container mx-auto px-6 py-4">
                <div class="flex flex-wrap gap-3 justify-center">
                    <a href="/" class="px-6 py-2.5 rounded-full font-medium transition bg-gray-800 hover:bg-gray-700">
                        All Topics
                    </a>
                    {% for t in topics %}
                    <a href="/topic/{{ t | urlencode }}" class="px-6 py-2.5 rounded-full font-medium transition {{ 'bg-red-600 text-white' if t == topic else 'bg-gray-800 hover:bg-gray-700' }}">
                        {{ t | capitalize }}
                    </a>
                    {% endfor %}
                </div>
            </div>
        </div>

        <main class="container mx-auto px-6 py-10">
            <h2 class="text-3xl font-bold mb-8">{{ topic | capitalize }} News</h2>
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {% for story in stories %}
                <div class="bg-gray-900 rounded-2xl overflow-hidden border border-gray-800 hover:border-blue-600 transition">
                    <div class="p-6">
                        <h3 class="text-xl font-semibold mb-4">{{ story['title'] }}</h3>

                        {% if story['summary'] %}
                        <div class="text-gray-400 text-sm whitespace-pre-line leading-relaxed mb-6">
                            {{ story['summary'] | replace('\\n', '<br>') | safe }}
                        </div>
                        {% endif %}

                        <div class="flex items-center justify-between">
                            <span class="bg-blue-900 text-blue-300 px-4 py-1 rounded-full text-xs font-medium">
                                {{ story['topic'] | capitalize }}
                            </span>
                            <span class="text-gray-500 text-sm story-time" data-ms="{{ story['added_at_ms'] }}">
                                {{ story['added_at_display'] }}
                            </span>
                        </div>

                        <a href="{{ story['link'] }}" target="_blank"
                           class="mt-6 block text-center bg-red-600 hover:bg-red-500 transition py-3 rounded-xl font-medium">
                            Read Original →
                        </a>
                    </div>
                </div>
                {% endfor %}
            </div>
        </main>

        <footer class="bg-gray-950 py-8 text-center text-gray-500 text-sm">
            © {{ now_year }} News Aggregator
        </footer>

        <script>
        function pad(n){ return String(n).padStart(2, "0"); }
        function formatLocalFromMs(ms){
            if (!ms) return "";
            const d = new Date(Number(ms));
            if (isNaN(d.getTime())) return "";
            return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
        }

        const lu = document.getElementById("last-updated");
        if (lu) {
            const ms = lu.getAttribute("data-ms");
            const local = formatLocalFromMs(ms);
            if (local) lu.textContent = local;
        }

        document.querySelectorAll(".story-time").forEach(el => {
            const ms = el.getAttribute("data-ms");
            const local = formatLocalFromMs(ms);
            if (local) el.textContent = local;
        });
        </script>
    </body>
    </html>
    """
    now_year = datetime.now().year
    return render_template_string(
        html,
        stories=stories,
        topics=topics,
        topic=topic,
        last_updated=last["display"],
        last_updated_ms=last["ms"],   # <-- ONLY ONCE (fixes your crash)
        now_year=now_year
    )


if __name__ == '__main__':
    app.run(debug=True, port=5000)
