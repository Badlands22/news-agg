import os
import re
import time
import html
import hashlib
import sqlite3
from datetime import datetime, timezone, timedelta
import feedparser
import difflib  # For fuzzy similarity

# ---------------- Postgres (Render) ----------------
try:
    import psycopg # type: ignore
except Exception:
    psycopg = None
DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = "news.db"
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))

# ---------------- xAI (Grok) summaries ----------------
try:
    from xai_sdk import Client
    from xai_sdk.chat import user, system
except Exception:
    Client = None
    user = None
    system = None
XAI_API_KEY = os.getenv("XAI_API_KEY")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4")
_xai_client = None

# ---------------- Feeds ----------------
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

# Topics, AI_SUMMARY_TOPICS, CANON_TOPIC unchanged...

def canonical_topic_label(topic_key: str) -> str:
    # unchanged...

# ---------------- DATABASE HELPERS ----------------
def using_postgres() -> bool:
    return bool(DATABASE_URL)

def pg_connect():
    if psycopg is None:
        raise RuntimeError("psycopg is not installed. Add psycopg[binary] to requirements.txt")
    return psycopg.connect(DATABASE_URL)

def sqlite_connect():
    return sqlite3.connect(DB_PATH)

def init_db():
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.articles (
                        id SERIAL PRIMARY KEY,
                        title TEXT,
                        link TEXT UNIQUE,
                        description TEXT,
                        pub_date TEXT,
                        topic TEXT,
                        summary TEXT,
                        added_at TIMESTAMPTZ,
                        fingerprint TEXT,
                        image_url TEXT  -- NEW: for featured image
                    );
                    """
                )
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS articles_fingerprint_uniq ON public.articles(fingerprint);")
                c.execute("CREATE INDEX IF NOT EXISTS articles_added_at_idx ON public.articles (added_at DESC);")
                c.execute("CREATE INDEX IF NOT EXISTS articles_topic_added_at_idx ON public.articles (topic, added_at DESC);")
            conn.commit()
    else:
        conn = sqlite_connect()
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                link TEXT UNIQUE,
                description TEXT,
                pub_date TEXT,
                topic TEXT,
                summary TEXT,
                added_at TEXT,
                fingerprint TEXT,
                image_url TEXT  -- NEW
            );
            """
        )
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS articles_fingerprint_uniq ON articles(fingerprint);")
        c.execute("CREATE INDEX IF NOT EXISTS articles_added_at_idx ON articles(added_at);")
        c.execute("CREATE INDEX IF NOT EXISTS articles_topic_added_at_idx ON articles(topic, added_at);")
        conn.commit()
        conn.close()

# clean_text, normalize_for_fingerprint, make_fingerprint unchanged (keep your latest fuzzy version)...

# sanitize_summary unchanged...

def insert_stub(title: str, link: str, desc: str, pub_date: str, topic_label: str, fingerprint: str, image_url: str = None):
    """
    Insert row with summary=NULL first. Now accepts optional image_url.
    """
    added_at = datetime.now(timezone.utc)
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute(
                    """
                    INSERT INTO public.articles (title, link, description, pub_date, topic, summary, added_at, fingerprint, image_url)
                    VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id;
                    """,
                    (title, link, desc, pub_date, topic_label, added_at, fingerprint, image_url),
                )
                row = c.fetchone()
            conn.commit()
            return row[0] if row else None
    conn = sqlite_connect()
    cur = conn.cursor()
    added_at_str = added_at.isoformat()
    cur.execute(
        """
        INSERT OR IGNORE INTO articles (title, link, description, pub_date, topic, summary, added_at, fingerprint, image_url)
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?);
        """,
        (title, link, desc, pub_date, topic_label, added_at_str, fingerprint, image_url),
    )
    conn.commit()
    new_id = cur.lastrowid if cur.rowcount == 1 else None
    conn.close()
    return new_id

# update_summary unchanged...

# xai_summary, fallback_summary unchanged...

def process_feed(feed_name: str, url: str):
    print(f"Processing feed: {feed_name}")
    feed = feedparser.parse(url)
    entries = getattr(feed, "entries", None) or []
    if not entries:
        print(f"No entries found in {feed_name}")
        return

    recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    cutoff_str = recent_cutoff.isoformat()

    for entry in entries:
        try:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            desc = (entry.get("description") or entry.get("summary") or "").strip()
            content = entry.get("content", [{}])[0].get("value", "") if entry.get("content") else ""
            pub_date = entry.get("published") or entry.get("updated") or datetime.now(timezone.utc).isoformat()

            if not title or not link:
                continue

            norm_title = normalize_for_fingerprint(title)
            norm_desc = clean_text(desc).lower()
            matched_key = None
            for topic_key in TOPICS:
                t = topic_key.lower()
                if t in norm_title or t in norm_desc:
                    matched_key = topic_key
                    break
            if not matched_key:
                continue

            fp = make_fingerprint(title, matched_key)
            topic_label = canonical_topic_label(matched_key)

            # Fuzzy dupe check (your latest version)...
            is_duplicate = False
            # ... (keep your existing fuzzy block here unchanged)

            if is_duplicate:
                continue

            # === NEW: Extract best image URL ===
            image_url = None

            # 1. media:thumbnail (preferred for previews)
            if 'media_thumbnail' in entry:
                image_url = entry.media_thumbnail[0].get('url')

            # 2. media:content with medium=image
            elif 'media_content' in entry:
                for mc in entry.media_content:
                    if mc.get('medium') == 'image' or 'image' in mc.get('type', ''):
                        image_url = mc.get('url')
                        break

            # 3. enclosure if image
            elif 'enclosures' in entry:
                for enc in entry.enclosures:
                    if 'image' in enc.get('type', ''):
                        image_url = enc.get('href')
                        break

            # 4. Fallback: first <img> in description or content
            if not image_url:
                combined = desc or content
                img_match = re.search(r'<img[^>]+src=["\'](.*?)["\']', combined, re.IGNORECASE)
                if img_match:
                    image_url = img_match.group(1)

            # Clean up any query params or make absolute if needed (optional)
            if image_url and not image_url.startswith(('http://', 'https://')):
                # Handle relative URLs (rare in RSS)
                from urllib.parse import urljoin
                image_url = urljoin(link, image_url)

            new_id = insert_stub(
                title=clean_text(title),
                link=link,
                desc=clean_text(desc),
                pub_date=str(pub_date),
                topic_label=topic_label,
                fingerprint=fp,
                image_url=image_url  # NEW param
            )
            if not new_id:
                print(f"Already seen (dedup): {link}")
                continue

            print(f"NEW [{feed_name}] (topic={topic_label}): {title}")
            # ... summary logic unchanged ...

        except Exception as e:
            print(f"Entry error: {e}")

# main() unchanged...
