import os
import re
import time
import sqlite3
import hashlib
from datetime import datetime, timezone
from difflib import SequenceMatcher

import feedparser

# ---- optional: better summaries by fetching article page text ----
import requests
from bs4 import BeautifulSoup

# ---------------- Postgres (Render) ----------------
# NOTE: requirements.txt must include: psycopg[binary]
try:
    import psycopg
except Exception:
    psycopg = None

POLL_SECONDS = 30
DB_PATH = "news.db"  # used only when DATABASE_URL is not set
DATABASE_URL = os.getenv("DATABASE_URL")  # set on Render for Postgres


# ---------------- xAI (Grok) summaries ----------------
# NOTE: requirements.txt must include: xai-sdk
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

# Topics that should get AI summaries (exact strings must match your TOPICS)
AI_SUMMARY_TOPICS = [
    "election", "trump", "russia", "china", "israel", "iran", "bitcoin", "nuclear"
]


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
    """
    Ensure tables + fingerprint column exist (Postgres or SQLite).
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
                        added_at TIMESTAMPTZ,
                        fingerprint TEXT
                    );
                """)
                # In case table existed without fingerprint
                c.execute("ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
                # Unique index should already exist, but harmless if it does
                c.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS articles_fingerprint_uniq
                    ON public.articles(fingerprint);
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
                added_at TEXT,
                fingerprint TEXT
            )
        ''')
        # SQLite unique index on fingerprint to prevent duplicates
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS articles_fingerprint_uniq ON articles(fingerprint)")
        conn.commit()
        conn.close()


def is_new_article_by_fingerprint(fp: str) -> bool:
    """
    True if we do NOT already have this fingerprint.
    """
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("SELECT 1 FROM public.articles WHERE fingerprint = %s LIMIT 1;", (fp,))
                return c.fetchone() is None
    else:
        conn = sqlite_connect()
        c = conn.cursor()
        c.execute("SELECT 1 FROM articles WHERE fingerprint = ? LIMIT 1;", (fp,))
        exists = c.fetchone() is not None
        conn.close()
        return not exists


def save_article(title, link, desc, pub_date, topic, summary, fingerprint):
    if using_postgres():
        added_at = datetime.now(timezone.utc)
        with pg_connect() as conn:
            with conn.cursor() as c:
                # Dedupe on fingerprint (NOT link). Link can differ for the same story.
                c.execute("""
                    INSERT INTO public.articles (title, link, description, pub_date, topic, summary, added_at, fingerprint)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (fingerprint) DO NOTHING;
                """, (title, link, desc, pub_date, topic, summary, added_at, fingerprint))
            conn.commit()
    else:
        conn = sqlite_connect()
        c = conn.cursor()
        added_at = datetime.now(timezone.utc).isoformat()
        c.execute('''
            INSERT OR IGNORE INTO articles
            (title, link, description, pub_date, topic, summary, added_at, fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (title, link, desc, pub_date, topic, summary, added_at, fingerprint))
        conn.commit()
        conn.close()


# Create tables on startup
init_db()


# ---------------- TEXT + FINGERPRINT HELPERS ----------------
def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    # strip zero-width chars that cause hidden duplicates
    text = re.sub(r'[\xa0\u200b\u200c\u200d]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def normalize_for_fingerprint(text: str) -> str:
    """
    Match your SQL approach:
    md5(lower(regexp_replace(title, '\s+', ' ', 'g')) || '|' || topic)
    """
    t = clean_text(text)
    t = t.lower()
    # collapse whitespace like regexp_replace(title, '\s+', ' ', 'g')
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def make_fingerprint(title: str, topic: str) -> str:
    base = f"{normalize_for_fingerprint(title)}|{(topic or '').strip()}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def normalize_text_for_match(text: str) -> str:
    text = clean_text(text).lower()
    text = re.sub(r'[^\w\s]', '', text)
    return text


def headline_only_summary(title: str, desc: str, topic: str) -> str:
    """
    Fallback if xAI isn't available.
    Tries to use RSS snippet without being useless.
    """
    desc_clean = clean_text(desc or "")
    lines = []

    # One sentence "what happened" attempt
    if desc_clean and len(desc_clean) > 40:
        # take the first sentence-ish chunk
        first = re.split(r'[.!?]+', desc_clean)[0].strip()
        if first:
            lines.append(first)
    else:
        lines.append("Summary unavailable from RSS snippet.")

    # 2 bullets: why it matters / watch next (non-generic)
    lines.append(f"- Why it matters: Track updates related to {topic}.")
    lines.append("- Watch next: Details may be behind a paywall or require opening the source link.")

    return "\n".join(lines)


# ---------------- Page fetch + extraction (better input for AI) ----------------
def fetch_article_text(url: str, max_chars: int = 1500) -> str:
    """
    Fetch a page and extract meta description + first paragraphs.
    This makes summaries MUCH better than RSS snippets.
    """
    if not url:
        return ""

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NewsAggBot/1.0; +https://example.com)"
    }

    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        if r.status_code >= 400:
            return ""
        html = r.text
    except Exception:
        return ""

    try:
        soup = BeautifulSoup(html, "html.parser")

        # Remove junk
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        parts = []

        # meta description
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            parts.append(md["content"].strip())

        og = soup.find("meta", attrs={"property": "og:description"})
        if og and og.get("content"):
            parts.append(og["content"].strip())

        # first few paragraphs
        paras = soup.find_all("p")
        collected = []
        for p in paras[:8]:
            txt = clean_text(p.get_text(" ", strip=True))
            if len(txt) >= 60:
                collected.append(txt)
            if sum(len(x) for x in collected) > max_chars:
                break

        if collected:
            parts.append(" ".join(collected))

        text = clean_text(" ".join(parts))
        return text[:max_chars]
    except Exception:
        return ""


# ---------------- xAI SUMMARIZER ----------------
def xai_summary(title: str, desc: str, feed_name: str, topic: str, resolved_text: str):
    """
    Uses xAI (Grok) to generate a short, actually-useful monitoring summary.
    If we don't have enough text, we return None and fall back.
    """
    global _xai_client

    if not XAI_API_KEY or Client is None:
        return None

    # Don’t ask the model to hallucinate from nothing.
    usable = clean_text(resolved_text or desc or "")
    if len(usable) < 120:
        return None

    if _xai_client is None:
        _xai_client = Client(api_key=XAI_API_KEY, timeout=60)

    prompt = f"""You are writing a HIGH-SIGNAL news brief for a monitoring dashboard.

Write:
- 1 sentence: what happened (do NOT repeat the headline)
- 3 bullets:
  • Key details (names/places/figures if present)
  • Why it matters (real-world impact, not generic)
  • What to watch next (the next thing that could happen)

Rules:
- If the source text is thin/unclear, say what's unknown instead of guessing.
- Keep it short and sharp.

INPUT:
Source: {feed_name}
Topic matched: {topic}
Headline: {title}

Context text:
{usable}
"""

    try:
        chat = _xai_client.chat.create(model=XAI_MODEL)
        chat.append(system("You write concise, accurate, high-signal news summaries."))
        chat.append(user(prompt))
        resp = chat.sample()
        text = (resp.content or "").strip()
        return text if text else None
    except Exception as e:
        print(f"xAI summary error: {e}")
        return None


# ---------------- MAIN LOGIC ----------------
def process_feed(feed_name, url):
    print(f"Processing feed: {feed_name}")
    feed = feedparser.parse(url)
    if not hasattr(feed, 'entries') or not feed.entries:
        print(f"No entries found in {feed_name}")
        return

    for entry in feed.entries:
        title = clean_text(entry.get('title', '')).strip()
        link = clean_text(entry.get('link', '')).strip()
        desc = clean_text(entry.get('description', entry.get('summary', ''))).strip()
        pub_date = entry.get('published', entry.get('updated', datetime.now(timezone.utc).isoformat()))

        if not title or not link:
            continue

        # Topic match
        matched_topic = None
        norm_title = normalize_text_for_match(title)
        norm_desc = normalize_text_for_match(desc)

        for topic in TOPICS:
            t = topic.lower()
            if t in norm_title or t in norm_desc:
                matched_topic = topic
                break

        if not matched_topic:
            continue

        # Fingerprint (dedupe key)
        fp = make_fingerprint(title, matched_topic)

        # If we've already got it, skip BEFORE doing expensive summary fetch/AI.
        if not is_new_article_by_fingerprint(fp):
            # print(f"Duplicate fingerprint (skipping): {title}")
            continue

        print(f"NEW (topic) [{feed_name}]: {title}")

        # Better source text: try fetching the page if RSS snippet is weak
        page_text = ""
        if len(desc) < 200:
            page_text = fetch_article_text(link)

        summary = None
        if matched_topic in AI_SUMMARY_TOPICS:
            summary = xai_summary(title, desc, feed_name, matched_topic, page_text)
            if summary:
                print("Generated xAI summary.")
            else:
                summary = headline_only_summary(title, desc, matched_topic)

        # Save (Postgres: ON CONFLICT fingerprint DO NOTHING)
        save_article(title, link, desc, pub_date, matched_topic, summary, fp)
        print("SAVED.")


def main():
    db_mode = "Postgres (DATABASE_URL)" if using_postgres() else f"SQLite ({DB_PATH})"
    print("Collector started.")
    print(f"DB mode: {db_mode}")

    if using_postgres():
        safe = re.sub(r"://([^:]+):([^@]+)@", r"://\\1:***@", DATABASE_URL or "")
        print(f"DATABASE_URL seen by collector: {safe}")

    if XAI_API_KEY:
        print(f"xAI enabled. Model: {XAI_MODEL}")
    else:
        print("xAI not enabled (XAI_API_KEY not set). Summaries will use fallback text.")

    print(f"Collector running every {POLL_SECONDS} seconds…")
    print(f"Feeds: {', '.join(f['name'] for f in FEEDS)}")
    print(f"Topics: {', '.join(TOPICS)}")
    print(f"AI summaries for: {', '.join(AI_SUMMARY_TOPICS)}")

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
