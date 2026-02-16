import os
import re
import time
import html
import hashlib
import sqlite3
from datetime import datetime, timezone, timedelta
import feedparser
import difflib

# ---------------- Postgres (Render) ----------------
try:
    import psycopg  # type: ignore
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

# Topics you match against (these are "keys" used for matching)
TOPICS = [
    "election",
    "trump",
    "bitcoin",
    "russia",
    "putin",
    "israel",
    "saudi",
    "tulsi",
    "intelligence community",
    "fbi",
    "executive order",
    "china",
    "dni",
    "maduro",
    "lawsuit",
    "injunction",
    "court",
    "voter",
    "rico",
    "conspiracy",
    "corruption",
    "election fraud",
    "conspiracy theory",
    "qanon",
    "ufo",
    "nuclear",
    "maha",
    "netanyahu",
    "erdogan",
    "lavrov",
    "iran",
    "board of peace",
    "congo",
    "sahel",
]

# Which topic keys should get AI summaries
AI_SUMMARY_TOPICS = [
    "election",
    "trump",
    "russia",
    "china",
    "israel",
    "iran",
    "bitcoin",
    "ai",
    "nuclear",
]
AI_SUMMARY_TOPICS_SET = {t.strip().lower() for t in AI_SUMMARY_TOPICS}

# Canonical display labels for topic storage + UI consistency
CANON_TOPIC = {
    "fbi": "FBI",
    "ufo": "UFO",
    "qanon": "QAnon",
    "rico": "RICO",
    "executive order": "Executive Order",
    "conspiracy theory": "Conspiracy Theory",
    "election fraud": "Election Fraud",
    "board of peace": "Board of Peace",
    "maha": "MAHA",
    "trump": "Trump",
    "putin": "Putin",
    "russia": "Russia",
    "china": "China",
    "court": "Court",
    "election": "Election",
    "voter": "Voter",
    "injunction": "Injunction",
    "lawsuit": "Lawsuit",
    "nuclear": "Nuclear",
    "corruption": "Corruption",
    "conspiracy": "Conspiracy",
    "bitcoin": "Bitcoin",
    "iran": "Iran",
    "israel": "Israel",
    "saudi": "Saudi",
    "netanyahu": "Netanyahu",
    "erdogan": "Erdogan",
    "lavrov": "Lavrov",
    "congo": "Congo",
    "sahel": "Sahel",
    "dni": "DNI",
}

def canonical_topic_label(topic_key: str) -> str:
    t = (topic_key or "").strip()
    if not t:
        return ""
    k = t.lower()
    if k in CANON_TOPIC:
        return CANON_TOPIC[k]
    if t.isupper() and len(t) <= 8:
        return t
    return t.title()

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
                        image_url TEXT
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
                image_url TEXT
            );
            """
        )
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS articles_fingerprint_uniq ON articles(fingerprint);")
        c.execute("CREATE INDEX IF NOT EXISTS articles_added_at_idx ON articles(added_at);")
        c.execute("CREATE INDEX IF NOT EXISTS articles_topic_added_at_idx ON articles(topic, added_at);")
        conn.commit()
        conn.close()

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("\xa0", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    text = re.sub(r"[\xa0\u200b\u200c\u200d\u2060\ufeff]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def normalize_for_fingerprint(title: str) -> str:
    t = clean_text(title).lower()
    prefixes = [
        r'^(trump\s+(says|announces|claims|stated|told|reveals|declares|said|says)\s+)',
        r'^(president\s+donald\s+trump\s+)',
        r'^(president\s+trump\s+)',
        r'^trump\s+',
        r'\s+-\s+the\s+.*?$',
        r'\s+-\s+.*?$',
        r'\s*\|\s+.*?$',
    ]
    for p in prefixes:
        t = re.sub(p, '', t, flags=re.IGNORECASE)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def make_fingerprint(title: str, topic_key: str) -> str:
    base = f"{normalize_for_fingerprint(title)}|{(topic_key or '').strip().lower()}"
    return hashlib.md5(base.encode("utf-8", errors="ignore")).hexdigest()

def sanitize_summary(text: str) -> str:
    if not text:
        return ""
    t = str(text)
    t = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", t).strip()
    for _ in range(8):
        t2 = html.unescape(t)
        if t2 == t:
            break
        t = t2
    t = re.sub(r"(?i)^\s*summary\s*:\s*<\s*br\b[^>]*>\s*", "Summary:\n", t)
    t = re.sub(r"(?is)<\s*br\b[^>]*>", "\n", t)
    t = re.sub(r"(?is)<[^>]+>", "", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    t = re.sub(r"\n{4,}", "\n\n\n", t).strip()
    return t

def insert_stub(title: str, link: str, desc: str, pub_date: str, topic_label: str, fingerprint: str, image_url: str = None):
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

def update_summary(article_id: int, summary: str):
    if summary is None:
        return
    summary = sanitize_summary(summary)
    if not summary:
        return
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("UPDATE public.articles SET summary = %s WHERE id = %s;", (summary, article_id))
            conn.commit()
        return
    conn = sqlite_connect()
    cur = conn.cursor()
    cur.execute("UPDATE articles SET summary = ? WHERE id = ?;", (summary, article_id))
    conn.commit()
    conn.close()

def xai_summary(title: str, desc: str, feed_name: str, topic_label: str):
    global _xai_client
    if not XAI_API_KEY or Client is None:
        return None
    if _xai_client is None:
        _xai_client = Client(api_key=XAI_API_KEY, timeout=60)
    prompt = f"""Summarize this news item for a monitoring dashboard.
Rules:
- Output PLAIN TEXT only. No HTML. No tags. Do not output <br> in any form.
- Use real newlines for spacing.
- Do NOT restate the headline verbatim.
- If the snippet is thin/unclear, explicitly say what's unknown.
- Keep it high-signal and compact.
Output format (plain text with newlines only):
Summary:
<1-2 sentences>
Key details:
- ...
- ...
- ...
Why it matters:
- ...
What to watch:
- ...
INPUT:
Source: {feed_name}
Topic matched: {topic_label}
Title: {title}
Snippet: {desc}
"""
    try:
        chat = _xai_client.chat.create(model=XAI_MODEL)
        chat.append(system("You write concise, high-signal news summaries for analysts. Plain text only."))
        chat.append(user(prompt))
        resp = chat.sample()
        text = (resp.content or "").strip()
        return sanitize_summary(text) if text else None
    except Exception as e:
        print(f"xAI summary error: {e}")
        return None

def fallback_summary(title: str, desc: str, topic_label: str):
    t = clean_text(title)
    d = clean_text(desc or "")
    if d and len(d) > 40:
        first = re.split(r"[.!?]\s+", d)[0].strip()
        if first and first.lower() not in t.lower():
            return sanitize_summary(
                f"Summary:\n{first}\n\nKey details:\n- Topic: {topic_label}\n\nWhy it matters:\n- Worth monitoring under {topic_label}.\n\nWhat to watch:\n- Open the source for full context."
            )
    return sanitize_summary(
        f"Summary:\n{t}\n\nKey details:\n- Topic: {topic_label}\n\nWhy it matters:\n- Worth monitoring under {topic_label}.\n\nWhat to watch:\n- Open the source for full context."
    )

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

            # Fuzzy duplicate check
            is_duplicate = False
            if using_postgres():
                with pg_connect() as conn:
                    with conn.cursor() as c:
                        c.execute(
                            """
                            SELECT title FROM public.articles 
                            WHERE topic = %s AND added_at > %s 
                            ORDER BY added_at DESC LIMIT 10;
                            """,
                            (topic_label, cutoff_str)
                        )
                        recent_titles = [row[0] for row in c.fetchall()]
            else:
                conn = sqlite_connect()
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT title FROM articles 
                    WHERE topic = ? AND added_at > ? 
                    ORDER BY added_at DESC LIMIT 10;
                    """,
                    (topic_label, cutoff_str)
                )
                recent_titles = [row[0] for row in cur.fetchall()]
                conn.close()

            for recent_title in recent_titles:
                if recent_title:
                    recent_norm = normalize_for_fingerprint(recent_title)
                    similarity = difflib.SequenceMatcher(None, norm_title, recent_norm).ratio()
                    if similarity > 0.85:
                        print(f"Fuzzy dupe skipped (sim={similarity:.2f}): {title} vs {recent_title}")
                        is_duplicate = True
                        break

            if is_duplicate:
                continue

            # Extract best image URL
            image_url = None
            if 'media_thumbnail' in entry and entry.media_thumbnail:
                image_url = entry.media_thumbnail[0].get('url')
            elif 'media_content' in entry:
                for mc in entry.media_content:
                    if mc.get('medium') == 'image' or 'image' in mc.get('type', ''):
                        image_url = mc.get('url')
                        break
            elif 'enclosures' in entry:
                for enc in entry.enclosures:
                    if 'image' in enc.get('type', ''):
                        image_url = enc.get('href')
                        break

            if not image_url:
                combined = desc or content
                img_match = re.search(r'<img[^>]+src=["\'](.*?)["\']', combined, re.IGNORECASE)
                if img_match:
                    image_url = img_match.group(1)

            if image_url and not image_url.startswith(('http://', 'https://')):
                from urllib.parse import urljoin
                image_url = urljoin(link, image_url)

            new_id = insert_stub(
                title=clean_text(title),
                link=link,
                desc=clean_text(desc),
                pub_date=str(pub_date),
                topic_label=topic_label,
                fingerprint=fp,
                image_url=image_url
            )
            if not new_id:
                print(f"Already seen (dedup): {link}")
                continue

            print(f"NEW [{feed_name}] (topic={topic_label}): {title}")
            summary = None
            if (matched_key or "").strip().lower() in AI_SUMMARY_TOPICS_SET:
                summary = xai_summary(title, desc, feed_name, topic_label)
            if not summary:
                summary = fallback_summary(title, desc, topic_label)
            if summary:
                update_summary(new_id, summary)
                print("Saved + summarized.")
            else:
                print("Saved (no summary).")

        except Exception as e:
            print(f"Entry error: {e}")

def main():
    init_db()
    db_mode = "Postgres (DATABASE_URL)" if using_postgres() else f"SQLite ({DB_PATH})"
    print("Collector started.")
    print(f"DB mode: {db_mode}")
    print(f"Poll: every {POLL_SECONDS}s")
    if XAI_API_KEY:
        print(f"xAI enabled. Model: {XAI_MODEL}")
    else:
        print("xAI not enabled (XAI_API_KEY not set).")
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
            time.sleep(30)

if __name__ == "__main__":
    main()
