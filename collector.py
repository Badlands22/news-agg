import feedparser
import re
import time
import sqlite3
from datetime import datetime, timezone
from difflib import SequenceMatcher

# ---------------- CONFIG ----------------
POLL_SECONDS = 30
DB_PATH = "news.db"

FEEDS = [
    {"name": "BBC", "url": "http://feeds.bbci.co.uk/news/rss.xml"},
    {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/rss"},
    {"name": "RT (Russia Today)", "url": "https://www.rt.com/rss/"},
    {"name": "The Jerusalem Post", "url": "https://www.jpost.com/rss/rssfeedsfrontpage.aspx"},
    {"name": "Just the News", "url": "https://justthenews.com/rss.xml"},
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "Reuters", "url": "http://feeds.reuters.com/reuters/topNews"},
    {"name": "Google News (Trump/Election 24h)", "url": "https://news.google.com/rss/search?q=trump+OR+election+when:1d&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Google News (AI 24h)", "url": "https://news.google.com/rss/search?q=artificial+intelligence+when:1d&hl=en-US&gl=US&ceid=US:en"},
]

TOPICS = [
    "election", "trump", "bitcoin", "russia", "putin",
    "israel", "saudi", "tulsi", "intelligence community", "fbi", "executive order",
    "china", "dni", "maduro",
    "lawsuit", "injunction", "court", "voter", "rico", "conspiracy", "corruption",
    "election fraud", "conspiracy theory", "qanon", "ufo", "nuclear", "maha",
    "netanyahu", "erdogan", "lavrov", "iran", "board of peace", "congo", "sahel"
]

AI_SUMMARY_TOPICS = []

# ---------------- DATABASE SETUP ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS articles
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  title TEXT,
                  link TEXT UNIQUE,
                  description TEXT,
                  pub_date TEXT,
                  topic TEXT,
                  summary TEXT,
                  added_at TEXT)''')
    conn.commit()
    conn.close()

init_db()

# ---------------- HELPERS ----------------
def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'[\xa0\u200b\u200c\u200d]', ' ', text)
    publishers = r'(?:AOL\.com|The New York Times|CNN\.com|MSN|Oman Observer|매일경제|openPR\.com|Facebook|China Daily|The Motley Fool|The Guardian|The Times of Israel|CTech|igor´sLAB|AOL|Springer Professional|Bitget|Unite\.AI|Vocal\.media|Press of Atlantic City|Stock Traders Daily|The Globe and Mail|Haaretz|abudhabi-news\.com|Insider Monkey|Far Out Magazine|Telegrafi|Reuters)'
    text = re.sub(rf'\s*(?:-|\||–|—)?\s*{publishers}$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'(\b\w+\b\s*)\1+', r'\1', text)
    return text

def normalize_text(text: str) -> str:
    text = clean_text(text)
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)
    return text

def headline_only_summary(title: str, desc: str, topic: str):
    title_clean = normalize_text(title)
    desc_clean = clean_text(desc or "")

    bullets = [f"- {title_clean}"]

    if desc_clean and len(desc_clean) > 30:
        sentences = re.split(r'[.!?]+', desc_clean)
        added = 0
        for sent in sentences:
            sent = sent.strip()
            if len(sent) > 15 and added < 1:
                similarity = SequenceMatcher(None, sent.lower(), title_clean).ratio()
                if similarity < 0.55:
                    bullets.append(f"- {sent}")
                    added += 1

    bullets.append(f"- Matched topic: {topic}")
    bullets.append("Why it matters: This may be relevant to your tracked topic; open the link for full details.")

    return "\n".join(bullets)

def is_new_article(link: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM articles WHERE link = ?", (link,))
    exists = c.fetchone() is not None
    conn.close()
    return not exists

def save_article(title, link, desc, pub_date, topic, summary):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    added_at = datetime.now(timezone.utc).isoformat()
    c.execute('''INSERT OR IGNORE INTO articles
                 (title, link, description, pub_date, topic, summary, added_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
              (title, link, desc, pub_date, topic, summary, added_at))
    conn.commit()
    conn.close()

# ---------------- MAIN LOGIC ----------------
def process_feed(feed_name, url):
    print(f"Processing feed: {feed_name}")
    feed = feedparser.parse(url)
    if not hasattr(feed, 'entries') or not feed.entries:
        print(f"No entries found in {feed_name}")
        return

    for entry in feed.entries:
        title = entry.get('title', '').strip()
        link = entry.get('link', '').strip()
        desc = entry.get('description', entry.get('summary', '')).strip()
        pub_date = entry.get('published', entry.get('updated', datetime.now(timezone.utc).isoformat()))

        if not title or not link:
            continue

        matched_topic = None
        norm_title = normalize_text(title)
        norm_desc = normalize_text(desc)
        for topic in TOPICS:
            if topic.lower() in norm_title or topic.lower() in norm_desc:
                matched_topic = topic
                break

        if not matched_topic:
            continue

        print(f"NEW (topic) [{feed_name}]: {title}")

        summary = None
        if matched_topic in AI_SUMMARY_TOPICS:
            summary = headline_only_summary(title, desc, matched_topic)
            print("Generated summary:")
            print(summary)
            print("---")

        if is_new_article(link):
            save_article(title, link, desc, pub_date, matched_topic, summary)
            print(f"SAVED: {link}")
        else:
            print(f"Already seen: {link}")

def main():
    print("TEST: Collector started - this should appear in the log!")
    print(f"Collector running every {POLL_SECONDS} seconds… (CTRL+C to stop)")
    print(f"Feeds: {', '.join(f['name'] for f in FEEDS)}")
    print(f"Topics: {', '.join(TOPICS)}")
    print(f"AI summaries only for: {', '.join(AI_SUMMARY_TOPICS)}")

    while True:
        try:
            for feed in FEEDS:
                process_feed(feed["name"], feed["url"])
            print(f"Cycle complete. Sleeping {POLL_SECONDS}s...")
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as e:
            print(f"Error in main loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()