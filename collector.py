import os
import re
import time
import sqlite3
import hashlib
from datetime import datetime, timezone
from difflib import SequenceMatcher

import feedparser

# ---------------- Postgres (Render) ----------------
# NOTE: requirements.txt must include: psycopg[binary]
try:
    import psycopg
except Exception:
    psycopg = None

# ---------------- HTTP (for fetching article text) ----------------
# requirements.txt: requests, beautifulsoup4, lxml
try:
    import requests
    from bs4 import BeautifulSoup
except Exception:
    requests = None
    BeautifulSoup = None

# ---------------- xAI (Grok) summaries ----------------
# requirements.txt: xai-sdk
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

XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4")
_xai_client = None

USER_AGENT = os.getenv(
    "COLLECTOR_UA",
    "Mozilla/5.0 (compatible; NewsAggCollector/1.0; +https://example.com)"
)

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

# Topics where we request xAI summaries
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


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def normalize_for_fingerprint(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[\u200b\u200c\u200d\uFEFF]", "", text)  # zero-width junk
    text = re.sub(r"\s+", " ", text)
    return text


def make_fingerprint(title: str, topic: str) -> str:
    base = normalize_for_fingerprint(title) + "|" + normalize_for_fingerprint(topic)
    return sha256_hex(base)


def init_db():
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                # Create table (includes fingerprint)
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
                # Ensure column exists if table existed before
                c.execute("ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
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
                added_at TEXT,
                fingerprint TEXT
            )
        """)
        conn.commit()
        conn.close()


def dedupe_and_lock():
    """
    One-time (safe to run every startup):
    - backfill fingerprint
    - delete duplicates (keep newest)
    - add unique index so duplicates can't return
    """
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                # Backfill NULL fingerprints
                c.execute("""
                    UPDATE public.articles
                    SET fingerprint = encode(digest(lower(regexp_replace(coalesce(title,''), '\s+', ' ', 'g')) || '|' || lower(coalesce(topic,'')), 'sha256'), 'hex')
                    WHERE fingerprint IS NULL;
                """)

                # Delete duplicates: keep newest (by added_at then id)
                c.execute("""
                    WITH ranked AS (
                        SELECT
                            id,
                            fingerprint,
                            ROW_NUMBER() OVER (PARTITION BY fingerprint ORDER BY added_at DESC NULLS LAST, id DESC) AS rn
                        FROM public.articles
                        WHERE fingerprint IS NOT NULL
                    )
                    DELETE FROM public.articles a
                    USING ranked r
                    WHERE a.id = r.id
                      AND r.rn > 1;
                """)

                # Create unique index
                c.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_indexes
                            WHERE schemaname = 'public'
                              AND indexname = 'articles_fingerprint_uniq'
                        ) THEN
                            CREATE UNIQUE INDEX articles_fingerprint_uniq
                            ON public.articles(fingerprint);
                        END IF;
                    END$$;
                """)
            conn.commit()
    else:
        # SQLite: best-effort, lighter
        conn = sqlite_connect()
        c = conn.cursor()
        c.execute("UPDATE articles SET fingerprint = ? WHERE fingerprint IS NULL", ("",))
        conn.commit()
        conn.close()


def is_new_article(link: str, fingerprint: str) -> bool:
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                # Check link OR fingerprint
                c.execute("SELECT 1 FROM public.articles WHERE link = %s LIMIT 1;", (link,))
                if c.fetchone() is not None:
                    return False
                c.execute("SELECT 1 FROM public.articles WHERE fingerprint = %s LIMIT 1;", (fingerprint,))
                return c.fetchone() is None
    else:
        conn = sqlite_connect()
        c = conn.cursor()
        c.execute("SELECT 1 FROM articles WHERE link = ?", (link,))
        if c.fetchone() is not None:
            conn.close()
            return False
        c.execute("SELECT 1 FROM articles WHERE fingerprint = ?", (fingerprint,))
        exists = c.fetchone() is not None
        conn.close()
        return not exists


def save_article(title, link, desc, pub_date, topic, summary, fingerprint):
    if using_postgres():
        added_at = datetime.now(timezone.utc)
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO public.articles (title, link, description, pub_date, topic, summary, added_at, fingerprint)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (link) DO NOTHING;
                """, (title, link, desc, pub_date, topic, summary, added_at, fingerprint))
            conn.commit()
    else:
        conn = sqlite_connect()
        c = conn.cursor()
        added_at = datetime.now(timezone.utc).isoformat()
        c.execute("""
            INSERT OR IGNORE INTO articles
            (title, link, description, pub_date, topic, summary, added_at, fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (title, link, desc, pub_date, topic, summary, added_at, fingerprint))
        conn.commit()
        conn.close()


# Create tables & enforce dedupe rules on startup
init_db()
try:
    dedupe_and_lock()
except Exception as e:
    print(f"[WARN] dedupe_and_lock failed (continuing): {e}")


# ---------------- TEXT HELPERS ----------------
def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"[\xa0\u200b\u200c\u200d\uFEFF]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_text(text: str) -> str:
    text = clean_text(text)
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    return text


def fallback_summary(title: str, desc: str, topic: str) -> str:
    """
    Non-AI fallback that's not useless:
    - one sentence from snippet if it exists
    - includes "unknowns" when snippet is thin
    """
    title_clean = clean_text(title)
    desc_clean = clean_text(desc or "")

    if len(desc_clean) >= 80:
        # Use first 1-2 sentences
        parts = re.split(r"(?<=[.!?])\s+", desc_clean)
        sentence = parts[0][:220].strip()
        return f"{sentence}\n\nKey: Topic={topic}"
    else:
        return (
            f"This headline matches '{topic}', but the feed snippet is thin.\n"
            f"Headline: {title_clean}\n"
            f"Key: Topic={topic}"
        )


# ---------------- ARTICLE TEXT FETCH ----------------
def fetch_article_text(url: str, timeout: int = 8, max_chars: int = 6000) -> str:
    """
    Quick-and-dirty extractor:
    - downloads HTML
    - strips scripts/styles/nav
    - pulls text from <article> if present, else from body
    """
    if requests is None or BeautifulSoup is None:
        return ""

    # Google News RSS links sometimes redirect; requests will follow by default.
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            return ""
        html = resp.text or ""
        if not html:
            return ""

        soup = BeautifulSoup(html, "lxml")

        # Remove junk
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
            tag.decompose()

        # Prefer <article>
        article = soup.find("article")
        container = article if article else soup.body
        if not container:
            return ""

        text = container.get_text(separator=" ", strip=True)
        text = clean_text(text)

        # Heuristic: remove very short garbage
        if len(text) < 400:
            return ""

        return text[:max_chars]
    except Exception as e:
        print(f"[fetch_article_text] failed: {e}")
        return ""


# ---------------- xAI SUMMARIZER ----------------
def xai_summary(title: str, snippet: str, article_text: str, source: str, topic: str) -> str | None:
    """
    Uses xAI (Grok) to generate a useful, high-signal summary.
    Returns None if not configured.
    """
    global _xai_client

    if not XAI_API_KEY or Client is None or user is None or system is None:
        return None

    if _xai_client is None:
        _xai_client = Client(api_key=XAI_API_KEY, timeout=60)

    title_c = clean_text(title)
    snippet_c = clean_text(snippet or "")
    body_c = clean_text(article_text or "")

    # Keep model context tight
    if len(body_c) > 4500:
        body_c = body_c[:4500]

    prompt = f"""
You are writing summaries for a news monitoring dashboard.

Goal: be useful, not fluffy.

Rules:
- Do NOT restate the headline.
- Use specifics: who/what/where/when, and the key claim.
- If info is missing, say what is unknown.
- No hype, no moralizing, no filler.
- Output EXACTLY this format:

Summary: <one sentence, <= 30 words>
Key details:
- <bullet 1>
- <bullet 2>
Why it matters:
- <bullet 1>
What to watch:
- <bullet 1>

INPUT
Source: {source}
Matched topic: {topic}
Headline: {title_c}
Feed snippet: {snippet_c}

Article text (may be empty):
{body_c}
"""

    try:
        chat = _xai_client.chat.create(model=XAI_MODEL)
        chat.append(system("You write concise, high-signal news summaries for analysts."))
        chat.append(user(prompt))
        resp = chat.sample()
        text = (resp.content or "").strip()
        return text if text else None
    except Exception as e:
        print(f"[xAI] summary error: {e}")
        return None


# ---------------- MAIN LOGIC ----------------
def process_feed(feed_name, url):
    print(f"Processing feed: {feed_name}")
    feed = feedparser.parse(url)
    if not hasattr(feed, "entries") or not feed.entries:
        print(f"No entries found in {feed_name}")
        return

    ai_topics_lower = {t.lower() for t in AI_SUMMARY_TOPICS}

    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        desc = (entry.get("description") or entry.get("summary") or "").strip()
        pub_date = entry.get("published") or entry.get("updated") or datetime.now(timezone.utc).isoformat()

        if not title or not link:
            continue

        norm_title = normalize_text(title)
        norm_desc = normalize_text(desc)

        matched_topic = None
        for topic in TOPICS:
            t = topic.lower()
            if t in norm_title or t in norm_desc:
                matched_topic = topic
                break

        if not matched_topic:
            continue

        fp = make_fingerprint(title, matched_topic)

        # HARD STOP duplicates BEFORE we waste time summarizing
        if not is_new_article(link, fp):
            print(f"Already seen (link or fingerprint): {link}")
            continue

        print(f"NEW (topic) [{feed_name}]: {title}")

        # Build summary
        summary = None
        if matched_topic.lower() in ai_topics_lower:
            article_text = fetch_article_text(link)
            summary = xai_summary(title, desc, article_text, feed_name, matched_topic)

            if summary:
                print("Generated xAI summary:")
                print(summary)
                print("---")
            else:
                # fallback non-AI
                summary = fallback_summary(title, desc, matched_topic)

        # Save
        save_article(title, link, desc, pub_date, matched_topic, summary, fp)
        print(f"SAVED: {link}")


def main():
    db_mode = "Postgres (DATABASE_URL)" if using_postgres() else f"SQLite ({DB_PATH})"
    print("Collector started.")
    print(f"DB mode: {db_mode}")

    if using_postgres():
        safe = re.sub(r"://([^:]+):([^@]+)@", r"://\\1:***@", DATABASE_URL or "")
        print(f"DATABASE_URL seen by collector: {safe}")

    # xAI diagnostics (does not reveal key)
    print("=== xAI diagnostics ===")
    print(f"XAI_API_KEY set: {bool(XAI_API_KEY)}")
    print(f"XAI_API_KEY length: {len(XAI_API_KEY)}")
    print(f"XAI_MODEL: {XAI_MODEL}")
    print(f"xai_sdk import OK: {Client is not None}")
    print("=======================")

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
