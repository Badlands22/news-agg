from flask import Flask, render_template_string, request
import sqlite3
from datetime import datetime

app = Flask(__name__)

DB_PATH = "news.db"

def get_recent_stories(limit=12, search=None):
    conn = sqlite3.connect(DB_PATH)
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
    return rows

def get_topic_stories(topic, limit=12):
    conn = sqlite3.connect(DB_PATH)
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
    return rows

def get_all_topics():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT topic FROM articles ORDER BY topic")
    topics = [row[0] for row in c.fetchall()]
    conn.close()
    return topics

def get_latest_update():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT MAX(added_at) FROM articles")
    result = c.fetchone()[0]
    conn.close()
    if result:
        return datetime.fromisoformat(result).strftime("%Y-%m-%d %H:%M UTC")
    return "Never"

@app.route('/')
def home():
    search = request.args.get('q', '').strip()
    stories = get_recent_stories(12, search if search else None)
    topics = get_all_topics()
    last_updated = get_latest_update()
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
        <style>
            .tab-active { @apply bg-blue-600 text-white; }
            .tab-inactive { @apply bg-gray-800 hover:bg-gray-700 text-gray-200; }
        </style>
    </head>
    <body class="bg-gray-950 text-gray-200 min-h-screen">
        <header class="bg-gradient-to-r from-blue-700 to-indigo-800 py-8">
            <div class="container mx-auto px-6 text-center">
                <h1 class="text-5xl font-bold">News Aggregator</h1>
                <p class="mt-2 text-blue-200">Real-time updates on your tracked topics</p>
                <p class="mt-1 text-sm text-blue-300">Last updated: {{ last_updated }}</p>
            </div>
        </header>

        <!-- Topic Tabs / Dropdown -->
        <div class="bg-gray-900 border-b border-gray-800">
            <div class="container mx-auto px-6">
                <div class="hidden md:flex flex-wrap gap-3 py-4 justify-center">
                    <a href="/" class="px-5 py-2.5 rounded-full font-medium transition {{ 'tab-active' if not request.args.get('q') else 'tab-inactive' }}">
                        All Topics
                    </a>
                    {% for t in topics %}
                    <a href="/topic/{{ t | urlencode }}" class="px-5 py-2.5 rounded-full font-medium transition {{ 'tab-active' if t == request.view_args.get('topic') else 'tab-inactive' }}">
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

        <!-- Stories Grid -->
        <main class="container mx-auto px-6 pb-12">
            <h2 class="text-3xl font-bold mb-8">Latest Stories</h2>
            {% if stories %}
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {% for story in stories %}
                <div class="bg-gray-900 rounded-2xl overflow-hidden border border-gray-800 hover:border-blue-600 transition">
                    <div class="p-6">
                        <h3 class="text-xl font-semibold mb-4">{{ story['title'] }}</h3> <!-- No line-clamp - full title -->
                        {% if story['summary'] %}
                        <div class="text-gray-400 text-sm whitespace-pre-line leading-relaxed mb-6">
                            {{ story['summary'] | replace('\n', '<br>') | safe }}
                        </div>
                        {% endif %}
                        <div class="flex items-center justify-between">
                            <span class="bg-blue-900 text-blue-300 px-4 py-1 rounded-full text-xs font-medium">
                                {{ story['topic'] | capitalize }}
                            </span>
                            <span class="text-gray-500 text-sm">{{ story['added_at'][:10] }}</span>
                        </div>
                        <a href="{{ story['link'] }}" target="_blank" 
                           class="mt-6 block text-center bg-blue-600 hover:bg-blue-500 transition py-3 rounded-xl font-medium">
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
    </body>
    </html>
    """
    now_year = datetime.now().year
    return render_template_string(html, stories=stories, topics=topics, last_updated=last_updated, now_year=now_year)

@app.route('/topic/<topic>')
def topic_page(topic):
    stories = get_topic_stories(topic, limit=12)
    topics = get_all_topics()
    last_updated = get_latest_update()
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
                <p class="mt-1 text-sm text-blue-300">Last updated: {{ last_updated }}</p>
            </div>
        </header>

        <div class="bg-gray-900 border-b border-gray-800">
            <div class="container mx-auto px-6 py-4">
                <div class="flex flex-wrap gap-3 justify-center">
                    <a href="/" class="px-6 py-2.5 rounded-full font-medium transition bg-gray-800 hover:bg-gray-700">
                        All Topics
                    </a>
                    {% for t in topics %}
                    <a href="/topic/{{ t | urlencode }}" class="px-6 py-2.5 rounded-full font-medium transition {{ 'bg-blue-600 text-white' if t == topic else 'bg-gray-800 hover:bg-gray-700' }}">
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
                        <h3 class="text-xl font-semibold mb-4">{{ story['title'] }}</h3> <!-- No line-clamp - full title -->
                        {% if story['summary'] %}
                        <div class="text-gray-400 text-sm whitespace-pre-line leading-relaxed mb-6">
                            {{ story['summary'] | replace('\n', '<br>') | safe }}
                        </div>
                        {% endif %}
                        <div class="flex items-center justify-between">
                            <span class="bg-blue-900 text-blue-300 px-4 py-1 rounded-full text-xs font-medium">
                                {{ story['topic'] | capitalize }}
                            </span>
                            <span class="text-gray-500 text-sm">{{ story['added_at'][:10] }}</span>
                        </div>
                        <a href="{{ story['link'] }}" target="_blank" 
                           class="mt-6 block text-center bg-blue-600 hover:bg-blue-500 transition py-3 rounded-xl font-medium">
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
    </body>
    </html>
    """
    now_year = datetime.now().year
    return render_template_string(html, stories=stories, topics=topics, topic=topic, last_updated=last_updated, now_year=now_year)

if __name__ == '__main__':
    app.run(debug=True, port=5000)