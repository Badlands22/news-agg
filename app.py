from flask import Flask, render_template_string
import sqlite3
from datetime import datetime
import os

app = Flask(__name__)

DB_PATH = "news.db"

def get_recent_stories(limit=10):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT title, link, topic, summary, added_at
        FROM articles
        ORDER BY added_at DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_topic_stories(topic, limit=10):
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

@app.route('/')
def home():
    stories = get_recent_stories(50)
    topics = get_all_topics()
    html = """
    <!DOCTYPE html>
    <html lang="en" class="dark">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>News Aggregator - Latest Stories</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script>
            tailwind.config = {
                darkMode: 'class',
                theme: { extend: { colors: { primary: '#3b82f6' } } }
            }
        </script>
    </head>
    <body class="bg-gray-100 dark:bg-gray-900 text-gray-900 dark:text-gray-100 min-h-screen">
        <header class="bg-gradient-to-r from-blue-600 to-indigo-700 text-white py-6 shadow-lg">
            <div class="container mx-auto px-4">
                <h1 class="text-4xl font-bold text-center">News Aggregator</h1>
                <p class="text-center mt-2 opacity-90">Real-time updates on your tracked topics</p>
            </div>
        </header>

        <main class="container mx-auto px-4 py-8">
            <section class="mb-12">
                <h2 class="text-3xl font-bold mb-6 text-center md:text-left">Latest Stories (All Topics)</h2>
                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                    {% for story in stories %}
                    <div class="bg-white dark:bg-gray-800 rounded-xl shadow-md overflow-hidden hover:shadow-xl transition-shadow duration-300">
                        <div class="p-6">
                            <h3 class="text-xl font-semibold mb-3 line-clamp-2">{{ story['title'] }}</h3>
                            {% if story['summary'] %}
                            <div class="text-gray-700 dark:text-gray-300 mb-4 text-sm whitespace-pre-line leading-relaxed">
                                {{ story['summary'] | replace('\n', '<br>') | safe }}
                            </div>
                            {% endif %}
                            <div class="flex justify-between items-center text-sm">
                                <span class="bg-primary text-white px-3 py-1 rounded-full font-medium">
                                    {{ story['topic'] | capitalize }}
                                </span>
                                <span class="text-gray-500 dark:text-gray-400">
                                    {{ story['added_at'][:10] }}
                                </span>
                            </div>
                            <a href="{{ story['link'] }}" target="_blank" rel="noopener noreferrer"
                               class="mt-4 inline-block bg-primary text-white px-5 py-3 rounded-lg font-medium transition transform hover:scale-105">
                                Read Original Article →
                            </a>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </section>
        </main>

        <footer class="bg-gray-900 text-gray-400 py-8 mt-12 border-t border-gray-800">
            <div class="container mx-auto px-4 text-center">
                <p>© {{ now_year }} News Aggregator. Powered by real-time feeds.</p>
            </div>
        </footer>
    </body>
    </html>
    """
    now_year = datetime.now().year
    return render_template_string(html, stories=stories, now_year=now_year)

@app.route('/topic/<topic>')
def topic_page(topic):
    stories = get_topic_stories(topic, limit=50)
    topics = get_all_topics()
    html = """
    <!DOCTYPE html>
    <html lang="en" class="dark">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{{ topic | capitalize }} News - Aggregator</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script>
            tailwind.config = {
                darkMode: 'class',
                theme: { extend: { colors: { primary: '#3b82f6' } } }
            }
        </script>
    </head>
    <body class="bg-gray-100 dark:bg-gray-900 text-gray-900 dark:text-gray-100 min-h-screen">
        <nav class="bg-gradient-to-r from-blue-700 to-indigo-800 text-white shadow-lg">
            <div class="container mx-auto px-4">
                <div class="flex items-center justify-between py-4">
                    <a href="/" class="text-2xl font-bold">News Aggregator</a>
                    <div class="hidden md:flex space-x-6">
                        {% for t in topics %}
                        <a href="/topic/{{ t | urlencode }}" class="hover:text-blue-200 transition {% if t == topic %}underline font-bold{% endif %}">
                            {{ t | capitalize }}
                        </a>
                        {% endfor %}
                    </div>
                </div>
            </div>
        </nav>

        <header class="bg-gradient-to-br from-blue-600 to-indigo-700 text-white py-10">
            <div class="container mx-auto px-4 text-center">
                <h1 class="text-4xl md:text-5xl font-bold mb-3">{{ topic | capitalize }} News</h1>
                <p class="text-xl opacity-90">Latest updates on {{ topic }}</p>
            </div>
        </header>

        <main class="container mx-auto px-4 py-10">
            <section>
                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                    {% for story in stories %}
                    <div class="bg-white dark:bg-gray-800 rounded-xl shadow-md overflow-hidden hover:shadow-xl transition-shadow duration-300">
                        <div class="p-6">
                            <h3 class="text-xl font-semibold mb-3 line-clamp-2">{{ story['title'] }}</h3>
                            {% if story['summary'] %}
                            <div class="text-gray-700 dark:text-gray-300 mb-4 text-sm whitespace-pre-line leading-relaxed">
                                {{ story['summary'] | replace('\n', '<br>') | safe }}
                            </div>
                            {% endif %}
                            <div class="flex justify-between items-center text-sm">
                                <span class="bg-primary text-white px-3 py-1 rounded-full font-medium">
                                    {{ story['topic'] | capitalize }}
                                </span>
                                <span class="text-gray-500 dark:text-gray-400">
                                    {{ story['added_at'][:10] }}
                                </span>
                            </div>
                            <a href="{{ story['link'] }}" target="_blank" rel="noopener noreferrer"
                               class="mt-4 inline-block bg-primary text-white px-5 py-3 rounded-lg font-medium transition transform hover:scale-105">
                                Read Original Article →
                            </a>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </section>
        </main>

        <footer class="bg-gray-900 text-gray-400 py-8 mt-12 border-t border-gray-800">
            <div class="container mx-auto px-4 text-center">
                <p>© {{ now_year }} News Aggregator. Powered by real-time feeds.</p>
            </div>
        </footer>
    </body>
    </html>
    """
    now_year = datetime.now().year
    return render_template_string(html, stories=stories, topics=topics, topic=topic, now_year=now_year)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)