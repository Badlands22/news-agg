import os
import re
import time
import hashlib
import sqlite3
from datetime import datetime, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

# Optional: newspaper3k for better article extraction
try:
    from newspaper import Article
except Exception:
    Article = None

# ---------------- Postgres (Render) ----------------
# NOTE: requirements.txt must include: psycopg[binary]
try:
    import psycopg
except Exception:
    psycopg = None

# ---------------- xAI (Grok) summaries ----------------
# NOTE: requirements.txt must include: xai-sdk
try:
    from xai_sdk import Client
    from xai_sdk.chat import user, system
except Exception:
    Client = None
    user = None
    system = None

# ---------------- CONFIG ----------------
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))

DB_PATH = "news.db"  # used only when DATABASE_URL is not set
DATABASE_URL = os.getenv("DATABASE_URL")  # set on Render for Postgres

XAI_API_KEY = os.getenv("XAI_API_KEY")
# Pick whatever you actually have enabled in your xAI console.
# The docs show grok-4-1-fast-reasoning as an example. :contentReference[oaicite:1]{index=1}
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4-1-fast-reasoning")
_xai_client = None

FETCH_FULL_ARTICLE = os.getenv("FETCH_FULL_ARTICLE", "1") == "1"
MAX_ARTICLE_CHARS = int(os.getenv("MAX_ARTICLE_CHARS", "7000"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "12"))

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

# Turn on xAI summaries only for these topics (easy to adjust)
AI_SUMMARY_TOPICS = [
    "election", "trump", "russia", "china", "israel", "iran", "bitcoin", "nuclear"
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

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
                # Ensure column exists if table already existed
                c.execute("ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
            conn.commit()

        # Try to add unique index. If duplicates exist, this will error — that’s fine.
        try:
            with pg_connect() as conn:
                with conn.cursor() as c:
                    c.execute("""
                        CREATE UNIQUE INDEX IF NOT EXISTS articles_fingerprint_uniq
                        ON public.articles(fingerprint);
                    """)
                conn.commit()
        except Exception as e:
            print(f"NOTE: could not create unique index on fingerprint yet (likely duplicates still exist): {e}")

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
                added_at TEXT,
                fingerprint TEXT
            )
        """)
        conn.commit()
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS articles_fingerprint_uniq ON articles(fingerprint)")
            conn.commit()
        except Exception:
            pass
        conn.close()


def article_exists_by_fingerprint(fingerprint: str) -> bool:
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("SELECT 1 FROM public.articles WHERE fingerprint = %s LIMIT 1;", (fingerprint,))
                return c.fetchone() is not None
    else:
        conn = sqlite_connect()
        c = conn.cursor()
        c.execute("SELECT 1 FROM articles WHERE fingerprint = ?", (fingerprint,))
        exists = c.fetchone() is not None
        conn.close()
        return exists


def save_article(title, link, desc, pub_date, topic, summary, fingerprint):
    if using_postgres():
        added_at = datetime.now(timezone.utc)
        with pg_connect() as conn:
            with conn.cursor() as c:
                # De-dupe by fingerprint. If we see the same story again via a different URL,
                # we update link/desc/pub_date and fill summary if missing.
                c.execute("""
                    INSERT INTO public.articles (title, link, description, pub_date, topic, summary, added_at, fingerprint)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (fingerprint) DO UPDATE
                    SET
                        link = EXCLUDED.link,
                        description = CASE
                            WHEN public.articles.description IS NULL OR length(public.articles.description) < 30
                            THEN EXCLUDED.description
                            ELSE public.articles.description
                        END,
                        pub_date = COALESCE(public.articles.pub_date, EXCLUDED.pub_date),
                        summary = COALESCE(public.articles.summary, EXCLUDED.summary),
                        added_at = GREATEST(public.articles.added_at, EXCLUDED.added_at);
                """, (title, link, desc, pub_date, topic, summary, added_at, fingerprint))
            conn.commit()
    else:
        conn = sqlite_connect()
        c = conn.cursor()
        added_at = datetime.now(timezone.utc).isoformat()

        # SQLite UPSERT if fingerprint is unique-indexed; otherwise OR IGNORE covers link.
        try:
            c.execute("""
                INSERT INTO articles (title, link, description, pub_date, topic, summary, added_at, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    link=excluded.link,
                    description=CASE
                        WHEN articles.description IS NULL OR length(articles.description) < 30
                        THEN excluded.description
                        ELSE articles.description
                    END,
                    pub_date=COALESCE(articles.pub_date, excluded.pub_date),
                    summary=COALESCE(articles.summary, excluded.summary),
                    added_at=CASE
                        WHEN articles.added_at > excluded.added_at THEN articles.added_at
                        ELSE excluded.added_at
                    END;
            """, (title, link, desc, pub_date, topic, summary, added_at, fingerprint))
        except Exception:
            c.execute("""
                INSERT OR IGNORE INTO articles
                (title, link, description, pub_date, topic, summary, added_at, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (title, link, desc, pub_date, topic, summary, added_at, fingerprint))

        conn.commit()
        conn.close()


# Create tables on startup
init_db()

# ---------------- TEXT HELPERS ----------------
def clean_text(text: str) -> str:
    if not text:
        return ""
    # strip html tags quickly
    text = re.sub(r"<[^>]+>", " ", text)
    # unescape common entities-ish
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    # remove zero-widths and NBSP variants
    text = re.sub(r"[\xa0\u200b\u200c\u200d]", " ", text)

    # collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_for_fingerprint(title: str) -> str:
    t = clean_text(title).lower()
    # normalize apostrophes/dashes
    t = t.replace("’", "'").replace("–", "-").replace("—", "-")
    # remove punctuation except spaces
    t = re.sub(r"[^\w\s]", "", t)
    # collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def make_fingerprint(title: str, topic: str) -> str:
    base = f"{normalize_for_fingerprint(title)}|{(topic or '').strip().lower()}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def short_fallback_summary(title: str, desc: str, topic: str) -> str:
    # A MUCH better fallback than "matched topic/why it matters tracked topics"
    title_clean = clean_text(title)
    desc_clean = clean_text(desc or "")

    parts = []
    if desc_clean:
        parts.append(desc_clean[:280].rstrip())
    else:
        parts.append("Details are limited in the RSS snippet for this item.")

    parts.append(f"Topic: {topic}")
    parts.append("Open the link for full context/source details.")
    return "\n".join(parts)


# ---------------- ARTICLE FETCH ----------------
def fetch_article_text(url: str) -> str:
    """
    Try newspaper3k first (best), then fallback to requests+bs4.
    Returns "" on failure.
    """
    if not url or not FETCH_FULL_ARTICLE:
        return ""

    # 1) newspaper3k
    if Article is not None:
        try:
            a = Article(url, language="en")
            a.download()
            a.parse()
            txt = clean_text(a.text or "")
            if len(txt) >= 400:
                return txt[:MAX_ARTICLE_CHARS]
        except Exception:
            pass

    # 2) requests + bs4 fallback
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if r.status_code >= 400:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        # Remove junk
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
            tag.decompose()
        # Grab paragraphs
        paras = [clean_text(p.get_text(" ", strip=True)) for p in soup.find_all("p")]
        paras = [p for p in paras if len(p) > 60]
        txt = clean_text(" ".join(paras))
        if len(txt) >= 400:
            return txt[:MAX_ARTICLE_CHARS]
    except Exception:
        return ""

    return ""


# ---------------- xAI SUMMARIZER ----------------
def xai_summary(title: str, desc: str, full_text: str, feed_name: str, topic: str) -> str | None:
    """
    Uses xAI SDK to generate a short, high-signal summary.
    Returns None if not configured, so we can safely fall back.
    """
    global _xai_client

    if not XAI_API_KEY or Client is None:
        return None

    if _xai_client is None:
        # The xAI docs show this exact SDK usage pattern. :contentReference[oaicite:2]{index=2}
        _xai_client = Client(api_key=XAI_API_KEY, timeout=60)

    # Prefer full_text; fallback to desc.
    source_block = full_text if (full_text and len(full_text) > 300) else (desc or "")

    prompt = f"""You are writing a high-signal news brief for a monitoring dashboard.

Hard rules:
- Do NOT restate the headline.
- Do NOT mention "tracked topics".
- If the source content is thin/paywalled, say what is unknown.
- Be concrete: who did what, where, when, and why it matters.

Output format (exact):
TLDR: <one sentence>
Key facts:
- <bullet>
- <bullet>
- <bullet>
Why it matters:
- <bullet>
What to watch:
- <bullet>

INPUT
Source: {feed_name}
Topic matched: {topic}
Headline: {title}

Content:
{source_block}
"""

    try:
        chat = _xai_client.chat.create(model=XAI_MODEL)
        chat.append(system("You write concise, high-signal news briefs."))
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
    if not getattr(feed, "entries", None):
        print(f"No entries found in {feed_name}")
        return

    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        desc = (entry.get("description") or entry.get("summary") or "").strip()
        pub_date = entry.get("published") or entry.get("updated") or datetime.now(timezone.utc).isoformat()

        if not title or not link:
            continue

        # Match topic from normalized title/desc
        norm_title = normalize_for_fingerprint(title)
        norm_desc = normalize_for_fingerprint(desc)

        matched_topic = None
        for topic in TOPICS:
            t = topic.lower()
            if t in norm_title or t in norm_desc:
                matched_topic = topic
                break

        if not matched_topic:
            continue

        fingerprint = make_fingerprint(title, matched_topic)

        # IMPORTANT: bail early if we've already seen this fingerprint.
        # This prevents repeated summaries + duplicate inserts via different URLs.
        if article_exists_by_fingerprint(fingerprint):
            print(f"Already seen (fingerprint): {title}")
            continue

        print(f"NEW [{feed_name}] (topic={matched_topic}): {title}")

        summary = None
        if matched_topic in AI_SUMMARY_TOPICS:
            full_text = fetch_article_text(link)
            summary = xai_summary(title, desc, full_text, feed_name, matched_topic)

            if summary:
                print("Generated xAI summary.")
            else:
                summary = short_fallback_summary(title, desc, matched_topic)

        # Save (UPSERT by fingerprint)
        save_article(title, link, clean_text(desc), pub_date, matched_topic, summary, fingerprint)
        print(f"SAVED: {link}")


def main():
    db_mode = "Postgres (DATABASE_URL)" if using_postgres() else f"SQLite ({DB_PATH})"
    print("Collector started.")
    print(f"DB mode: {db_mode}")
    print(f"Full-article fetch: {FETCH_FULL_ARTICLE} (newspaper3k={'yes' if Article else 'no'})")

    if using_postgres():
        safe = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", DATABASE_URL or "")
        print(f"DATABASE_URL seen by collector: {safe}")

    if XAI_API_KEY and Client is not None:
        print(f"xAI enabled. Model: {XAI_MODEL}")
    else:
        print("xAI not enabled (XAI_API_KEY not set OR xai-sdk not installed).")

    print(f"Collector running every {POLL_SECONDS} seconds… (CTRL+C to stop)")
    print(f"Feeds: {', '.join(f['name'] for f in FEEDS)}")
    print(f"Topics: {', '.join(TOPICS)}")
    print(f"AI summaries for: {', '.join(AI_SUMMARY_TOPICS)}")

    while True:
        try:
            for f in FEEDS:
                process_feed(f["name"], f["url"])
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
