import feedparser
import re
import time
import sqlite3
import os
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from openai import OpenAI

# ---------------- CONFIG ----------------
POLL_SECONDS = 30
DB_PATH = "news.db"
FEEDS = [
    {"name": "BBC", "url": "http://feeds.bbci.co.uk/news/rss.xml"},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/rss"},
    {"name": "Google News (Trump/Election 24h)", "url": "https://news.google.com/rss/search?q=trump+OR+election+when:1d&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Google News (AI 24h)", "url": "https://news.google.com/rss/search?q=artificial+intelligence+when:1d&hl=en-US&gl=US&ceid=US:en"},
]
TOPICS = [
    "election", "trump", "artificial intelligence", "bitcoin", "russia", "putin",
    "israel", "saudi", "tulsi", "intelligence community", "fbi", "executive order",
    "china", "dni", "maduro", "mandelson"
]
AI_SUMMARY_TOPICS = ["election", "trump", "artificial intelligence", "bitcoin", "russia"]

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

def grok_summarize(title: str, description: str, topic: str) -> str:
    if not os.getenv("XAI_API_KEY"):
        return f"Summary temporarily unavailable.\n\nMatched topic: {topic}"
    
    client = OpenAI(
        api_key=os.getenv("XAI_API_KEY"),
        base_url="https://api.x.ai/v1"
    )
    
    prompt = f"""Write a short, clear summary in 2-4 sentences.
Title: {title}
Description: {description}
Focus on key facts and why it matters. No opinions."""
    
    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
            temperature=0.6
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Grok error: {e}")
        return f"Could not summarize right now.\n\nMatched topic: {topic}"

def is_new_article(title: str, link: str, topic: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # First: check exact link
    c.execute("SELECT 1 FROM articles WHERE link = ?", (link,))
    if c.fetchone() is not None:
        conn.close()
        return False
    
    # Second: check similar title in last 48 hours for this topic
    cleaned_title = normalize_text(title)
    two_days_ago = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    
    c.execute("""
        SELECT title FROM articles 
        WHERE topic = ? 
        AND added_at > ? 
        AND link != ?
    """, (topic, two_days_ago, link))
    
    existing_titles = [normalize_text(row[0]) for row in c.fetchall()]
    conn.close()
    
    if not existing_titles:
        return True
    
    for existing in existing_titles:
        similarity = SequenceMatcher(None, cleaned_title, existing).ratio()
        if similarity > 0.85:
            print(f"Skipping similar title (similarity {similarity:.2f}): {title}")
            return False
    
    return True

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
            print("Generating Grok summary...")
            summary = grok_summarize(title, desc, matched_topic)
            print("Generated summary:")
            print(summary)
            print("---")
        if is_new_article(title, link, matched_topic):
            save_article(title, link, desc, pub_date, matched_topic, summary)
            print(f"SAVED: {link}")
        else:
            print(f"Already seen or too similar: {link}")

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