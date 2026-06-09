import os
import re
import gc
import time
import html
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import feedparser

try:
    import psycopg  # type: ignore
except Exception:
    psycopg = None

try:
    from openai import OpenAI  # type: ignore
except Exception:
    OpenAI = None

# ── Config ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = os.getenv("DB_PATH", "news.db")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "900"))  # 15 minutes default
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# How similar two titles must be to count as the same story (0.0–1.0)
TITLE_SIMILARITY_THRESHOLD = 0.82

_openai_client = None

# ── Feeds ───────────────────────────────────────────────────────────────────
FEEDS = [
    # ── Wire / Mainstream ──
    {"name": "Reuters",              "url": "https://feeds.reuters.com/reuters/topNews"},
    {"name": "AP News",              "url": "https://rsshub.app/apnews/topics/apf-topnews"},
    {"name": "The Hill",             "url": "https://thehill.com/news/feed/"},
    {"name": "Fox News",             "url": "https://feeds.foxnews.com/foxnews/latest"},
    {"name": "Fox Politics",         "url": "https://feeds.foxnews.com/foxnews/politics"},
    {"name": "NY Post",              "url": "https://nypost.com/feed/"},
    {"name": "Washington Examiner",  "url": "https://www.washingtonexaminer.com/rss/politics-and-policy"},
    {"name": "Washington Times",     "url": "https://www.washingtontimes.com/rss/headlines/news/politics/"},
    {"name": "Newsmax",              "url": "https://www.newsmax.com/rss/Politics/16/"},
    {"name": "Politico",             "url": "https://rss.politico.com/politics-news.xml"},
    {"name": "Just the News",        "url": "https://justthenews.com/rss.xml"},
    {"name": "Google News",          "url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"},

    # ── Conservative / Alt Media ──
    {"name": "Breitbart",            "url": "https://feeds.feedburner.com/breitbart"},
    {"name": "Daily Wire",           "url": "https://www.dailywire.com/feeds/rss.xml"},
    {"name": "Epoch Times",          "url": "https://www.theepochtimes.com/c-us/feed"},
    {"name": "The Federalist",       "url": "https://thefederalist.com/feed/"},
    {"name": "Zero Hedge",           "url": "https://feeds.feedburner.com/zerohedge/feed"},
    {"name": "Revolver News",        "url": "https://revolver.news/feed/"},
    {"name": "Gateway Pundit",       "url": "https://www.thegatewaypundit.com/feed/"},
    {"name": "PJ Media",             "url": "https://pjmedia.com/feed"},
    {"name": "American Thinker",     "url": "https://www.americanthinker.com/feeds/rss.xml"},
    {"name": "The Blaze",            "url": "https://www.theblaze.com/feeds/feed.rss"},
    {"name": "Townhall",             "url": "https://townhall.com/rss/tipsheet"},
    {"name": "National Review",      "url": "https://www.nationalreview.com/feed/"},
    {"name": "CoinDesk",             "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},

    # ── Legal / Accountability ──
    {"name": "Judicial Watch",       "url": "https://www.judicialwatch.org/feed/"},
    {"name": "Conservative Treehouse","url": "https://theconservativetreehouse.com/feed/"},

    # ── International ──
    {"name": "BBC World",            "url": "http://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "Al Jazeera",           "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "Jerusalem Post",       "url": "https://www.jpost.com/rss/rssfeedsfrontpage.aspx"},
    {"name": "Times of Israel",      "url": "https://www.timesofisrael.com/feed/"},
    {"name": "RT",                   "url": "https://www.rt.com/rss/"},
    {"name": "TASS",                 "url": "https://tass.com/rss/v2.xml"},
    {"name": "South China Morning Post", "url": "https://www.scmp.com/rss/91/feed"},
    {"name": "Middle East Eye",      "url": "https://www.middleeasteye.net/rss"},
]

# ── Topics ───────────────────────────────────────────────────────────────────
# key (lowercase, used for matching) -> display label
# Order matters: first match wins for an article.
TOPICS = {
    # ── People / Admin ──
    "trump":            "Trump",
    "musk":             "Musk / DOGE",
    "rfk":              "RFK Jr",
    "epstein":          "Epstein",
    "pelosi":           "Pelosi",
    "obama":            "Obama",

    # ── Domestic Politics ──
    "election":         "Election",
    "doge":             "DOGE",
    "deep state":       "Deep State",
    "fbi":              "FBI",
    "cia":              "CIA",
    "doj":              "DOJ",
    "dni":              "DNI",
    "executive order":  "Executive Order",
    "impeach":          "Impeachment",
    "congress":         "Congress",
    "senate":           "Senate",
    "supreme court":    "Supreme Court",
    "injunction":       "Injunction",
    "lawsuit":          "Lawsuit",
    "court":            "Court",
    "indictment":       "Indictment",
    "rico":             "RICO",
    "voter":            "Voter / Election",
    "censorship":       "Censorship",
    "corruption":       "Corruption",
    "whistleblower":    "Whistleblower",

    # ── Policy / Economy ──
    "tariff":           "Tariffs",
    "immigration":      "Immigration",
    "border":           "Border",
    "inflation":        "Economy",
    "federal reserve":  "Federal Reserve",
    "maha":             "MAHA",
    "pentagon":         "Pentagon / Military",
    "nato":             "NATO",

    # ── Crypto / Finance ──
    "bitcoin":          "Bitcoin",
    "crypto":           "Crypto",
    "cbdc":             "CBDC",

    # ── International ──
    "russia":           "Russia",
    "putin":            "Putin",
    "ukraine":          "Ukraine",
    "zelensky":         "Zelensky",
    "israel":           "Israel",
    "netanyahu":        "Netanyahu",
    "gaza":             "Gaza",
    "iran":             "Iran",
    "china":            "China",
    "taiwan":           "Taiwan",
    "north korea":      "North Korea",
    "saudi":            "Saudi Arabia",
    "erdogan":          "Erdogan",
    "lavrov":           "Lavrov",
    "congo":            "Congo",
    "sahel":            "Sahel",
    "brics":            "BRICS",

    # ── Other ──
    "nuclear":          "Nuclear",
    "ufo":              "UFO / UAP",
    "uap":              "UFO / UAP",
    "qanon":            "QAnon",
    "conspiracy":       "Conspiracy",
    "board of peace":   "Board of Peace",
    "devolution":       "Devolution",
}


# ── DB helpers ───────────────────────────────────────────────────────────────

def using_postgres():
    return bool(DATABASE_URL)


def pg_connect():
    if psycopg is None:
        raise RuntimeError("psycopg not installed. Add psycopg[binary] to requirements.txt")
    conn = psycopg.connect(DATABASE_URL, connect_timeout=5)
    conn.autocommit = True
    return conn


def sqlite_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS public.articles (
                        id          SERIAL PRIMARY KEY,
                        title       TEXT,
                        link        TEXT UNIQUE,
                        source      TEXT,
                        description TEXT,
                        pub_date    TEXT,
                        topic       TEXT,
                        summary     TEXT,
                        added_at    TIMESTAMPTZ,
                        fingerprint TEXT UNIQUE
                    );
                """)
                # Add new columns to existing tables without breaking old installs
                c.execute("ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS source TEXT;")
                c.execute("ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS articles_fingerprint_uniq ON public.articles (fingerprint);")
                c.execute("CREATE INDEX IF NOT EXISTS articles_added_at_idx ON public.articles (added_at DESC);")
                c.execute("CREATE INDEX IF NOT EXISTS articles_topic_idx ON public.articles (topic, added_at DESC);")
    else:
        conn = sqlite_connect()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT,
                link        TEXT UNIQUE,
                source      TEXT,
                description TEXT,
                pub_date    TEXT,
                topic       TEXT,
                summary     TEXT,
                added_at    TEXT,
                fingerprint TEXT UNIQUE
            );
        """)
        # Add 'source' column if upgrading from old schema
        try:
            c.execute("ALTER TABLE articles ADD COLUMN source TEXT;")
        except Exception:
            pass
        c.execute("CREATE INDEX IF NOT EXISTS articles_added_at_idx ON articles (added_at DESC);")
        c.execute("CREATE INDEX IF NOT EXISTS articles_topic_idx ON articles (topic, added_at DESC);")
        conn.commit()
        conn.close()


# ── Text utilities ───────────────────────────────────────────────────────────

def clean_text(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", str(text))
    text = html.unescape(text)
    text = re.sub(r"[\xa0​‌‍⁠﻿]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url):
    """Strip fragments and common tracking params for cleaner dedup."""
    url = (url or "").strip().split("#")[0]
    url = re.sub(r"[?&](utm_\w+|ref|source|fbclid|gclid|campaign)=[^&]*", "", url)
    return url.rstrip("?& /")


def make_fingerprint(url):
    """One fingerprint per URL — topic is irrelevant to identity."""
    return hashlib.md5(normalize_url(url).encode("utf-8", errors="ignore")).hexdigest()


def normalize_for_compare(title):
    t = clean_text(title).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_similarity(a, b):
    return SequenceMatcher(None, normalize_for_compare(a), normalize_for_compare(b)).ratio()


# ── Deduplication ────────────────────────────────────────────────────────────

def get_recent_titles(hours=24):
    """Load titles from the last N hours to power similarity checks."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("SELECT title FROM public.articles WHERE added_at > %s;", (cutoff,))
                return [row[0] for row in c.fetchall() if row[0]]
    conn = sqlite_connect()
    c = conn.cursor()
    c.execute("SELECT title FROM articles WHERE added_at > ?;", (cutoff.isoformat(),))
    titles = [row[0] for row in c.fetchall() if row[0]]
    conn.close()
    return titles


def is_duplicate_title(new_title, recent_titles):
    for existing in recent_titles:
        if title_similarity(new_title, existing) >= TITLE_SIMILARITY_THRESHOLD:
            return True
    return False


def find_topic(title, desc):
    """Return the FIRST matching topic key. One article = one topic."""
    norm_title = normalize_for_compare(title)
    norm_desc = clean_text(desc or "").lower()
    for key in TOPICS:
        if key in norm_title or key in norm_desc:
            return key
    return None


# ── DB writes ────────────────────────────────────────────────────────────────

def insert_article(title, link, source, desc, pub_date, topic_label, fingerprint):
    added_at = datetime.now(timezone.utc)
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO public.articles
                        (title, link, source, description, pub_date, topic, summary, added_at, fingerprint)
                    VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id;
                """, (title, link, source, desc, pub_date, topic_label, added_at, fingerprint))
                row = c.fetchone()
                return row[0] if row else None
    conn = sqlite_connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO articles
            (title, link, source, description, pub_date, topic, summary, added_at, fingerprint)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?);
    """, (title, link, source, desc, pub_date, topic_label, added_at.isoformat(), fingerprint))
    conn.commit()
    new_id = cur.lastrowid if cur.rowcount == 1 else None
    conn.close()
    return new_id


def update_summary(article_id, summary):
    if not summary:
        return
    if using_postgres():
        with pg_connect() as conn:
            with conn.cursor() as c:
                c.execute("UPDATE public.articles SET summary = %s WHERE id = %s;", (summary, article_id))
        return
    conn = sqlite_connect()
    conn.execute("UPDATE articles SET summary = ? WHERE id = ?;", (summary, article_id))
    conn.commit()
    conn.close()


# ── AI summary ───────────────────────────────────────────────────────────────

def get_openai_client():
    global _openai_client
    if _openai_client is None and OpenAI and OPENAI_API_KEY:
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


def ai_summary(title, desc, source, topic_label):
    client = get_openai_client()
    if not client:
        return None

    prompt = f"""You are briefing a podcast host on a news story. Be concise and sharp.

Topic: {topic_label}
Source: {source}
Headline: {title}
Snippet: {desc}

Respond in this exact plain-text format (no HTML, no markdown):

SUMMARY
[1-2 sentences: what happened and why it matters]

KEY POINTS
• [most important fact]
• [second fact, if it adds something new]
• [third fact, only if genuinely useful]

If the snippet is thin, say so honestly. Omit bullet points you can't fill with real information."""

    try:
        resp = get_openai_client().chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:
        print(f"  [AI ERROR] {e}")
        return None


def fallback_summary(title, desc, topic_label):
    d = clean_text(desc or "")
    if d and len(d) > 60:
        first_sentence = re.split(r"[.!?]\s+", d)[0].strip()
        if first_sentence and first_sentence.lower() not in clean_text(title).lower():
            return f"SUMMARY\n{first_sentence}\n\nKEY POINTS\n• Topic: {topic_label}\n• Open the source link for full details."
    return f"SUMMARY\n{clean_text(title)}\n\nKEY POINTS\n• Topic: {topic_label}\n• Open the source link for full details."


# ── Feed processing ──────────────────────────────────────────────────────────

MAX_ENTRIES_PER_FEED = 25  # cap to keep memory usage low on free tier

def process_feed(feed_name, url, recent_titles):
    print(f"[{feed_name}] Fetching...")
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"  [FETCH ERROR] {e}")
        return 0

    entries = (getattr(feed, "entries", None) or [])[:MAX_ENTRIES_PER_FEED]
    del feed  # release the full parsed feed object immediately
    new_count = 0

    for entry in entries:
        try:
            title = clean_text(entry.get("title") or "")
            link = normalize_url(entry.get("link") or "")
            desc = clean_text(entry.get("description") or entry.get("summary") or "")
            pub_date = str(entry.get("published") or entry.get("updated") or datetime.now(timezone.utc).isoformat())

            if not title or not link:
                continue

            # One topic per article — first match wins
            topic_key = find_topic(title, desc)
            if not topic_key:
                continue

            # Primary dedup: URL fingerprint (same article from two feeds = one row)
            fp = make_fingerprint(link)

            # Secondary dedup: title similarity catches same story, different URL/wording
            if is_duplicate_title(title, recent_titles):
                continue

            topic_label = TOPICS[topic_key]
            new_id = insert_article(title, link, feed_name, desc, pub_date, topic_label, fp)

            if not new_id:
                # URL already in DB — still register title so this cycle's similarity check works
                recent_titles.append(title)
                continue

            # Add to in-memory list immediately so the next feed doesn't duplicate it
            recent_titles.append(title)

            print(f"  [NEW] ({topic_label}) {title[:72]}")
            summary = ai_summary(title, desc, feed_name, topic_label) or fallback_summary(title, desc, topic_label)
            update_summary(new_id, summary)
            new_count += 1

        except Exception as e:
            print(f"  [ENTRY ERROR] {e}")

    print(f"  → {new_count} new articles from {feed_name}")
    return new_count


# ── Main loop ────────────────────────────────────────────────────────────────

def main():
    init_db()
    db_label = "Postgres" if using_postgres() else f"SQLite ({DB_PATH})"
    ai_label = f"OpenAI {OPENAI_MODEL}" if OPENAI_API_KEY and OpenAI else "fallback (no API key)"
    print(f"╔══ Collector started ══════════════════════════")
    print(f"║  DB:   {db_label}")
    print(f"║  AI:   {ai_label}")
    print(f"║  Poll: every {POLL_SECONDS}s ({POLL_SECONDS // 60}m)")
    print(f"║  Feeds: {len(FEEDS)} | Topics: {len(TOPICS)}")
    print(f"╚═══════════════════════════════════════════════")

    while True:
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            print(f"\n─── Cycle: {stamp} ───")
            recent_titles = get_recent_titles(hours=24)
            total = 0
            for f in FEEDS:
                total += process_feed(f["name"], f["url"], recent_titles)
                gc.collect()  # free memory between feeds
            print(f"─── Done. {total} new articles. Next run in {POLL_SECONDS}s ───\n")
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as e:
            print(f"[MAIN ERROR] {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
