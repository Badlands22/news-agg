import os
import re
import time
import sqlite3
import hashlib
from datetime import datetime, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

# Article extraction
from newspaper import Article

# ---------------- Postgres (Render) ----------------
try:
    import psycopg
except Exception:
    psycopg = None

POLL_SECONDS = 30
DB_PATH = "news.db"
DATABASE_URL = os.getenv("DATABASE_URL")

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

# ---- Controls ----
SUMMARY_MIN_CHARS = 800      # don't call AI unless we have this much article text
EXTRACT_TIMEOUT = 18         # seconds for article fetch
REQUEST_TIMEOUT = 15

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
                c.execute("ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
                c.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS articles_fingerprint_uniq
                    ON public.articles(fingerprint);
                """)
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
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS articles_fingerprint_uniq ON articles(fingerprint)")
        conn.commit()
        conn.close()


def is_new_by_fingerprint(fp: str) -> bool:
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
        c.execute("""
            INSERT OR IGNORE INTO articles
            (title, link, description, pub_date, topic, summary, added_at, fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (title, link, desc, pub_date, topic, summary, added_at, fingerprint))
        conn.commit()
        conn.close()


init_db()


# ---------------- TEXT HELPERS ----------------
def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[\xa0\u200b\u200c\u200d]', ' ', text)  # strip invisible chars
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def normalize_for_match(text: str) -> str:
    text = clean_text(text).lower()
    text = re.sub(r'[^\w\s]', '', text)
    return text


def normalize_for_fingerprint(text: str) -> str:
    t = clean_text(text).lower()
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def make_fingerprint(title: str, topic: str) -> str:
    base = f"{normalize_for_fingerprint(title)}|{(topic or '').strip()}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


# ---------------- LINK RESOLUTION ----------------
def resolve_final_url(url: str) -> str:
    """
    Resolve redirects to reach the publisher page.
    Helps a lot with Google News wrapper links.
    """
    if not url:
        return url
    try:
        r = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (NewsAggBot/1.0)"}
        )
        # If it redirected, requests will give final URL
        return r.url or url
    except Exception:
        return url


# ---------------- ARTICLE EXTRACTION ----------------
def extract_article_text(url: str) -> str:
    """
    Try to pull main article text. Works on many sites.
    Will be weak on hard paywalls.
    """
    if not url:
        return ""

    try:
        a = Article(url, language="en")
        a.download()
        a.parse()
        txt = clean_text(a.text)
        return txt
    except Exception:
        return ""


def extract_fallback_snippet(url: str) -> str:
    """
    If newspaper fails, try meta description + first paragraphs.
    """
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (NewsAggBot/1.0)"})
        if r.status_code >= 400:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        parts = []
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            parts.append(md["content"].strip())
        og = soup.find("meta", attrs={"property": "og:description"})
        if og and og.get("content"):
            parts.append(og["content"].strip())

        paras = soup.find_all("p")
        collected = []
        for p in paras[:8]:
            t = clean_text(p.get_text(" ", strip=True))
            if len(t) >= 60:
                collected.append(t)
        if collected:
            parts.append(" ".join(collected))

        return clean_text(" ".join(parts))
    except Exception:
        return ""


# ---------------- xAI SUMMARIZER ----------------
def xai_summarize(title: str, topic: str, source: str, article_text: str) -> str | None:
    global _xai_client

    if not XAI_API_KEY or Client is None:
        return None

    if not article_text or len(article_text) < SUMMARY_MIN_CHARS:
        return None

    if _xai_client is None:
        _xai_client = Client(api_key=XAI_API_KEY, timeout=60)

    prompt = f"""You are writing a high-signal news brief for a monitoring dashboard.

Do NOT restate the headline.
Use ONLY the provided article text. If details are missing, say "unclear".

Output EXACTLY:
1) One sentence: what happened (who/what/where)
2) Three bullets:
- Key details: (names/places/numbers)
- Why it matters: (real impact)
- What to watch next: (next likely development)

Topic matched: {topic}
Source feed: {source}
Headline: {title}

Article text:
{article_text}
"""

    try:
        chat = _xai_client.chat.create(model=XAI_MODEL)
        chat.append(system("You are concise and factual. No fluff. No generic 'why it matters'."))
        chat.append(user(prompt))
        resp = chat.sample()
        out = (resp.content or "").strip()
        return out if out else None
    except Exception as e:
        print(f"xAI error: {e}")
        return None


# ---------------- MAIN LOGIC ----------------
def process_feed(feed_name, url):
    print(f"Processing feed: {feed_name}")
    feed = feedparser.parse(url)
    if not hasattr(feed, "entries") or not feed.entries:
        print(f"No entries found in {feed_name}")
        return

    for entry in feed.entries:
        title = clean_text(entry.get("title", "")).strip()
        link = clean_text(entry.get("link", "")).strip()
        desc = clean_text(entry.get("description", entry.get("summary", ""))).strip()
        pub_date = entry.get("published", entry.get("updated", datetime.now(timezone.utc).isoformat()))

        if not title or not link:
            continue

        norm_title = normalize_for_match(title)
        norm_desc = normalize_for_match(desc)

        matched_topic = None
        for topic in TOPICS:
            t = topic.lower()
            if t in norm_title or t in norm_desc:
                matched_topic = topic
                break

        if not matched_topic:
            continue

        fp = make_fingerprint(title, matched_topic)

        # Skip duplicates BEFORE doing any network-heavy work
        if not is_new_by_fingerprint(fp):
            continue

        # Resolve the final publisher URL (helps Google News)
        final_url = resolve_final_url(link)

        # Extract article text
        article_text = extract_article_text(final_url)
        if len(article_text) < 400:
            # fallback extraction if newspaper3k failed
            article_text = extract_fallback_snippet(final_url)

        summary = None
        if matched_topic in AI_SUMMARY_TOPICS:
            summary = xai_summarize(title, matched_topic, feed_name, article_text)

        # If xAI couldn't run (paywall / thin text), store a better fallback than “headline only”
        if matched_topic in AI_SUMMARY_TOPICS and not summary:
            if article_text and len(article_text) > 120:
                summary = " ".join(article_text.split()[:60]).strip() + "…"
                summary += "\n- Why it matters: Open the link for full details (source may be paywalled or limited)."
                summary += "\n- What to watch next: Updates from other outlets covering the same event."
            else:
                summary = None  # leave empty rather than junk

        save_article(
            title=title,
            link=final_url,          # store final URL instead of wrapper
            desc=desc,
            pub_date=pub_date,
            topic=matched_topic,
            summary=summary,
            fingerprint=fp
        )
        print(f"SAVED: {title}")


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
        print("xAI not enabled (XAI_API_KEY not set).")

    print(f"Collector running every {POLL_SECONDS} seconds…")
    print(f"AI summaries for: {', '.join(AI_SUMMARY_TOPICS)}")

    while True:
        try:
            for f in FEEDS:
                process_feed(f["name"], f["url"])
            print(f"Cycle complete. Sleeping {POLL_SECONDS}s...")
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"Error in main loop: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
