import os
import re
import time
import hashlib
import sqlite3
from datetime import datetime, timezone

import feedparser

# ---------------- Postgres (Render) ----------------
# requirements: psycopg[binary]
try:
    import psycopg  # type: ignore
except Exception:
    psycopg = None

DATABASE_URL = os.getenv("DATABASE_URL")  # set on Render for Postgres
DB_PATH = "news.db"  # used only when DATABASE_URL is not set
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))

# ---------------- xAI (Grok) summaries ----------------
# requirements: xai-sdk
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
    # default: title-case, but preserve short all-caps
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
    """
    Ensures schema exists + fingerprint support.
    IMPORTANT: ON CONFLICT DO NOTHING handles duplicate link or duplicate fingerprint safely.
    """
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
                        added_at TIMESTAMPTZ
                    );
                    """
                )
                # fingerprint column + unique index for cross-feed dedupe
                c.execute("ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
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
                fingerprint TEXT
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
    # strip HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # decode a few common entities (keep it simple/cheap)
    text = (
        text.replace("\xa0", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    # remove zero-width and NBSP variants
    text = re.sub(r"[\xa0\u200b\u200c\u200d]", " ", text)
    # collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_for_fingerprint(title: str) -> str:
    t = clean_text(title).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def make_fingerprint(title: str, topic_key: str) -> str:
    # fingerprint should be stable regardless of display casing
    base = f"{normalize_for_fingerprint(title)}|{(topic_key or '').strip().lower()}"
    return hashlib.md5(base.encode("utf-8", errors="ignore")).hexdigest()


def sanitize_summary(text: str) -> str:
    """
    Make summaries safe + consistent:
    - convert <br> tags to newlines
    - strip any stray HTML tags
    - normalize whitespace but KEEP newlines
    """
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)  # remove any other HTML tags
    # normalize line endings
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    # trim trailing spaces per line
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    # collapse huge blank gaps
    t = re.sub(r"\n{4,}", "\n\n\n", t).strip()
    return t


def insert_stub(title: str, link: str, desc: str, pub_date: str, topic_label: str, fingerprint: str):
    """
    Insert row with summary=NULL first.
    Returns inserted id if inserted, else None if it already existed (by link or fingerprint).
    """
    added_at = datetime.now(timezone.utc)

    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute(
                    """
                    INSERT INTO public.articles (title, link, description, pub_date, topic, summary, added_at, fingerprint)
                    VALUES (%s, %s, %s, %s, %s, NULL, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id;
                    """,
                    (title, link, desc, pub_date, topic_label, added_at, fingerprint),
                )
                row = c.fetchone()
            conn.commit()
            return row[0] if row else None

    conn = sqlite_connect()
    cur = conn.cursor()
    added_at_str = added_at.isoformat()
    cur.execute(
        """
        INSERT OR IGNORE INTO articles (title, link, description, pub_date, topic, summary, added_at, fingerprint)
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?);
        """,
        (title, link, desc, pub_date, topic_label, added_at_str, fingerprint),
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


# ---------------- xAI SUMMARIZER ----------------
def xai_summary(title: str, desc: str, feed_name: str, topic_label: str):
    """
    Returns a high-signal formatted summary, or None if xAI is not configured.
    IMPORTANT: plain text only; no HTML.
    """
    global _xai_client
    if not XAI_API_KEY or Client is None:
        return None

    if _xai_client is None:
        _xai_client = Client(api_key=XAI_API_KEY, timeout=60)

    prompt = f"""Summarize this news item for a monitoring dashboard.

Rules:
- Plain text only. NO HTML. NO <br>.
- Do NOT restate the headline verbatim.
- If the snippet is thin/unclear, explicitly say what's unknown.
- Keep it high-signal and compact.

Output EXACTLY this format (use newlines):
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
        chat.append(system("You write concise, high-signal news summaries for analysts."))
        chat.append(user(prompt))
        resp = chat.sample()
        text = (resp.content or "").strip()
        return sanitize_summary(text) if text else None
    except Exception as e:
        print(f"xAI summary error: {e}")
        return None


def fallback_summary(title: str, desc: str, topic_label: str):
    """
    Cheap fallback that uses cleaned snippet and calls out unknowns.
    """
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


# ---------------- MAIN LOGIC ----------------
def process_feed(feed_name: str, url: str):
    print(f"Processing feed: {feed_name}")
    feed = feedparser.parse(url)
    entries = getattr(feed, "entries", None) or []

    if not entries:
        print(f"No entries found in {feed_name}")
        return

    for entry in entries:
        try:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            desc = (entry.get("description") or entry.get("summary") or "").strip()
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

            # Stable fingerprint uses topic KEY (lowercased inside make_fingerprint)
            fp = make_fingerprint(title, matched_key)

            # Canonical label stored to DB (fixes 'trump' vs 'Trump' mismatch)
            topic_label = canonical_topic_label(matched_key)

            # INSERT STUB FIRST: prevents duplicate crashes AND avoids wasting xAI tokens on duplicates.
            new_id = insert_stub(
                title=clean_text(title),
                link=link,
                desc=clean_text(desc),
                pub_date=str(pub_date),
                topic_label=topic_label,
                fingerprint=fp,
            )

            if not new_id:
                # duplicate by link or fingerprint -> safe skip
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
            # Per-entry safety: never let one bad row kill the loop
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
            # Safety net: if something goes wrong at the cycle level
            print(f"Error in main loop: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
