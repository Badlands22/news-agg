import os
import re
import sqlite3
import time
import html
from datetime import datetime, timezone

import pytz
from flask import Flask, render_template_string, request, jsonify, url_for, make_response

try:
    import psycopg  # type: ignore
except Exception:
    psycopg = None

app = Flask(__name__)

APP_BUILD = "v2-2026-06"
DB_PATH = os.getenv("DB_PATH", "news.db")
DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
BRIEF_PASSWORD = os.getenv("BRIEF_PASSWORD", "badlands")
PAGE_SIZE = 15
CACHE_TTL = 30  # seconds

try:
    from openai import OpenAI as _OpenAI
    _brief_client = _OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception:
    _brief_client = None

_cache: dict = {}

# ── Topic display labels (keep in sync with collector.py) ────────────────────
ALL_TOPICS = [
    # People / Admin
    "Trump", "Musk / DOGE", "RFK Jr", "Epstein", "Pelosi", "Obama",
    # Domestic Politics
    "Election", "DOGE", "Deep State", "FBI", "CIA", "DOJ", "DNI",
    "Executive Order", "Impeachment", "Congress", "Senate", "Supreme Court",
    "Injunction", "Lawsuit", "Court", "Indictment", "RICO",
    "Voter / Election", "Censorship", "Corruption", "Whistleblower",
    # Policy / Economy
    "Tariffs", "Immigration", "Border", "Economy", "Federal Reserve",
    "MAHA", "Pentagon / Military", "NATO",
    # Crypto / Finance
    "Bitcoin", "Crypto", "CBDC",
    # International
    "Russia", "Putin", "Ukraine", "Zelensky", "Israel", "Netanyahu",
    "Gaza", "Iran", "China", "Taiwan", "North Korea", "Saudi Arabia",
    "Erdogan", "Lavrov", "Congo", "Sahel", "BRICS",
    # Other
    "Nuclear", "UFO / UAP", "QAnon", "Conspiracy", "Board of Peace", "Devolution",
]

# Curated shortlist shown in the sticky nav bar — keep this to ~15 max
NAV_TOPICS = [
    "Trump", "Election", "Deep State", "FBI", "DOJ",
    "Russia", "Ukraine", "Israel", "Gaza", "China",
    "Immigration", "Economy", "Bitcoin", "UFO / UAP", "Devolution",
]


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_get(key):
    item = _cache.get(key)
    if not item:
        return None
    exp, val = item
    if time.time() > exp:
        _cache.pop(key, None)
        return None
    return val


def _cache_set(key, val, ttl=CACHE_TTL):
    _cache[key] = (time.time() + ttl, val)


# ── DB helpers ────────────────────────────────────────────────────────────────

def using_postgres():
    return bool(DATABASE_URL)


def pg_connect():
    if psycopg is None:
        raise RuntimeError("psycopg not installed")
    conn = psycopg.connect(
        DATABASE_URL, connect_timeout=5,
        options="-c statement_timeout=5000",
        application_name="news_agg",
    )
    conn.autocommit = True
    return conn


def fetch_rows(query, params=()):
    try:
        if using_postgres():
            with pg_connect() as conn:
                with conn.cursor() as c:
                    c.execute(query, params)
                    cols = [d[0] for d in c.description]
                    return [dict(zip(cols, row)) for row in c.fetchall()]
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB] {e}")
        return []


def fetch_one(query, params=()):
    try:
        if using_postgres():
            with pg_connect() as conn:
                with conn.cursor() as c:
                    c.execute(query, params)
                    row = c.fetchone()
                    return row[0] if row else None
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(query, params)
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] {e}")
        return None


# ── Queries ───────────────────────────────────────────────────────────────────

def get_stories(limit=PAGE_SIZE, page=1, search=None, topic=None):
    offset = max(page - 1, 0) * limit
    ck = ("stories", limit, page, search or "", topic or "", "pg" if using_postgres() else "sq")
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    if using_postgres():
        tbl = "public.articles"
        ph = "%s"
    else:
        tbl = "articles"
        ph = "?"

    if topic:
        q = f"SELECT title,link,source,topic,summary,added_at,image_url FROM {tbl} WHERE lower(topic)=lower({ph}) ORDER BY added_at DESC LIMIT {ph} OFFSET {ph}"
        rows = fetch_rows(q, (topic, limit, offset))
    elif search:
        term = f"%{search}%"
        like = "ILIKE" if using_postgres() else "LIKE"
        q = f"SELECT title,link,source,topic,summary,added_at,image_url FROM {tbl} WHERE title {like} {ph} OR topic {like} {ph} OR summary {like} {ph} ORDER BY added_at DESC LIMIT {ph} OFFSET {ph}"
        rows = fetch_rows(q, (term, term, term, limit, offset))
    else:
        q = f"SELECT title,link,source,topic,summary,added_at,image_url FROM {tbl} ORDER BY added_at DESC LIMIT {ph} OFFSET {ph}"
        rows = fetch_rows(q, (limit, offset))

    _cache_set(ck, rows)
    return rows


def get_latest_update():
    ck = ("latest",)
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    tbl = "public.articles" if using_postgres() else "articles"
    val = fetch_one(f"SELECT MAX(added_at) FROM {tbl}")
    if not val:
        result = ""
    else:
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            result = dt.isoformat()
        except Exception:
            result = str(val)
    _cache_set(ck, result, ttl=60)
    return result


def get_article_counts():
    """Return total count and per-topic counts for the sidebar."""
    ck = ("counts",)
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    tbl = "public.articles" if using_postgres() else "articles"
    rows = fetch_rows(f"SELECT topic, COUNT(*) as cnt FROM {tbl} GROUP BY topic ORDER BY cnt DESC")
    result = {r["topic"]: r["cnt"] for r in rows}
    _cache_set(ck, result, ttl=120)
    return result


# ── Brief DB helpers ──────────────────────────────────────────────────────────

_image_col_ensured = False

def ensure_image_column():
    global _image_col_ensured
    if _image_col_ensured:
        return
    try:
        if using_postgres():
            with pg_connect() as conn:
                with conn.cursor() as c:
                    c.execute("ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS image_url TEXT;")
        else:
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.execute("ALTER TABLE articles ADD COLUMN image_url TEXT;")
            except Exception:
                pass
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[DB migrate image_url] {e}")
    _image_col_ensured = True


_brief_cols_ensured = False

def ensure_brief_columns():
    """Add saved_brief / briefed_at columns if they don't exist yet."""
    global _brief_cols_ensured
    if _brief_cols_ensured:
        return
    try:
        if using_postgres():
            with pg_connect() as conn:
                with conn.cursor() as c:
                    c.execute("ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS saved_brief TEXT;")
                    c.execute("ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS briefed_at TIMESTAMPTZ;")
        else:
            conn = sqlite3.connect(DB_PATH)
            for col in ["saved_brief TEXT", "briefed_at TEXT"]:
                try:
                    conn.execute(f"ALTER TABLE articles ADD COLUMN {col};")
                except Exception:
                    pass
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[DB migrate brief cols] {e}")
    _brief_cols_ensured = True


def save_brief_to_db(link, brief_text):
    """Persist a generated brief back to the article row."""
    briefed_at = datetime.now(timezone.utc)
    try:
        if using_postgres():
            with pg_connect() as conn:
                with conn.cursor() as c:
                    c.execute(
                        "UPDATE public.articles SET saved_brief=%s, briefed_at=%s WHERE link=%s",
                        (brief_text, briefed_at, link)
                    )
        else:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "UPDATE articles SET saved_brief=?, briefed_at=? WHERE link=?",
                (brief_text, briefed_at.isoformat(), link)
            )
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[DB save brief] {e}")


def get_saved_briefs():
    """Return all articles that have a saved brief, newest first."""
    tbl = "public.articles" if using_postgres() else "articles"
    return fetch_rows(
        f"SELECT title, link, source, topic, saved_brief, briefed_at "
        f"FROM {tbl} WHERE saved_brief IS NOT NULL ORDER BY briefed_at DESC"
    )


# ── Serialization ─────────────────────────────────────────────────────────────

def parse_summary(text):
    """
    Parse the structured summary format into sections:
    {'summary': str, 'bullets': [str]}
    """
    if not text:
        return {"summary": "", "bullets": []}

    # Unescape and strip HTML
    for _ in range(4):
        t2 = html.unescape(text)
        if t2 == text:
            break
        text = t2
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = re.sub(r"[​‌‍⁠﻿]", "", text).strip()

    summary_text = ""
    bullets = []

    # Try to parse structured format
    summary_match = re.search(r"SUMMARY\s*\n(.*?)(?=\nKEY POINTS|\Z)", text, re.DOTALL | re.IGNORECASE)
    bullets_match = re.search(r"KEY POINTS\s*\n(.*)", text, re.DOTALL | re.IGNORECASE)

    if summary_match:
        summary_text = summary_match.group(1).strip()
    if bullets_match:
        raw_bullets = bullets_match.group(1).strip()
        for line in raw_bullets.splitlines():
            line = re.sub(r"^[\s•\-\*]+", "", line).strip()
            if line:
                bullets.append(line)

    # Fallback: treat the whole thing as the summary
    if not summary_text and not bullets:
        summary_text = text

    return {"summary": summary_text, "bullets": bullets[:3]}


def _parse_dt(ts):
    """Parse a timestamp value into a UTC-aware datetime, or None."""
    if not ts:
        return None
    try:
        if isinstance(ts, datetime):
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def time_ago(dt_utc):
    """Return a human-friendly relative time string."""
    if not dt_utc:
        return ""
    now = datetime.now(timezone.utc)
    diff = now - dt_utc
    mins = int(diff.total_seconds() / 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    cst = pytz.timezone("America/Chicago")
    return dt_utc.astimezone(cst).strftime("%b %d")


def serialize_story(s):
    dt_utc   = _parse_dt(s.get("added_at"))
    topic    = (s.get("topic") or "").strip()
    parsed   = parse_summary(s.get("summary") or "")
    img      = (s.get("image_url") or "").strip()
    age_mins = int((datetime.now(timezone.utc) - dt_utc).total_seconds() / 60) if dt_utc else 9999

    return {
        "title":      (s.get("title") or "").strip(),
        "link":       (s.get("link") or "").strip(),
        "source":     (s.get("source") or "").strip(),
        "topic":      topic,
        "summary":    parsed["summary"],
        "bullets":    parsed["bullets"],
        "added_at":   time_ago(dt_utc),
        "image_url":  img,
        "is_breaking": age_mins < 20,   # only truly fresh stories
        "is_new":      age_mins < 90,   # under 90 min gets a subtle "new" dot
    }


# ── HTML template ─────────────────────────────────────────────────────────────

BASE_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{{ page_title }}</title>
  <meta name="description" content="Breaking news aggregator tracking the stories that matter."/>

  <!-- Google AdSense — replace ca-pub-XXXXXXXXXXXXXXXX with your publisher ID -->
  <!-- <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-XXXXXXXXXXXXXXXX" crossorigin="anonymous"></script> -->

  <style>
    /* ════════════════════════════════════════════
       TOKENS
    ════════════════════════════════════════════ */
    :root {
      --bg:        #0b0d0f;
      --surface:   #111418;
      --surface2:  #161b20;
      --surface3:  #1e2530;
      --border:    rgba(255,255,255,.07);
      --border2:   rgba(255,255,255,.12);
      --text:      #f0ebe0;
      --text2:     #b8a898;
      --muted:     #5a6672;
      --accent:    #c8972a;
      --accent2:   #a87a1c;
      --gold:      #c8972a;
      --green:     #16a34a;
      --shadow:    0 4px 20px rgba(0,0,0,.7);
      --radius:    8px;
    }
    body.light {
      --bg:        #f4f5f7;
      --surface:   #ffffff;
      --surface2:  #f0f2f5;
      --surface3:  #e8eaed;
      --border:    rgba(0,0,0,.09);
      --border2:   rgba(0,0,0,.15);
      --text:      #111827;
      --text2:     #374151;
      --muted:     #6b7280;
      --shadow:    0 2px 12px rgba(0,0,0,.1);
    }

    /* ════════════════════════════════════════════
       RESET + BASE
    ════════════════════════════════════════════ */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html { scroll-behavior: smooth; }
    body {
      font-family: "Georgia", "Times New Roman", serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }
    a { color: inherit; text-decoration: none; }

    /* ════════════════════════════════════════════
       MASTHEAD
    ════════════════════════════════════════════ */
    .masthead {
      background: var(--surface);
      border-bottom: 3px solid var(--accent);
      position: sticky; top: 0; z-index: 100;
      box-shadow: 0 2px 16px rgba(0,0,0,.6);
    }
    .masthead-top {
      max-width: 1280px; margin: 0 auto;
      padding: 12px 20px 10px;
      display: flex; align-items: center; justify-content: space-between; gap: 16px;
    }
    .site-name {
      font-size: 32px; font-weight: 900; letter-spacing: -1px;
      font-family: system-ui, -apple-system, sans-serif;
      line-height: 1;
    }
    .site-name em { color: var(--accent); font-style: normal; }
    .masthead-tagline { font-size: 11px; color: var(--muted); margin-top: 3px;
      font-family: system-ui; letter-spacing: .04em; text-transform: uppercase; }
    .masthead-right { display: flex; align-items: center; gap: 10px; }
    .search-form { display: flex; gap: 0; }
    .search-form input {
      padding: 8px 14px; font-size: 13px; font-family: system-ui;
      background: var(--surface2); border: 1px solid var(--border2);
      border-right: none; border-radius: var(--radius) 0 0 var(--radius);
      color: var(--text); outline: none; width: 200px;
    }
    .search-form input:focus { border-color: var(--accent); }
    .search-form button {
      padding: 8px 14px; background: var(--accent); border: none;
      border-radius: 0 var(--radius) var(--radius) 0;
      color: #111; font-weight: 700; font-size: 13px;
      font-family: system-ui; cursor: pointer;
    }
    .theme-btn {
      background: none; border: 1px solid var(--border2);
      border-radius: var(--radius); padding: 7px 10px;
      font-size: 15px; cursor: pointer; color: var(--text); line-height: 1;
    }

    /* ── Ticker ── */
    .ticker {
      background: var(--accent);
      overflow: hidden; white-space: nowrap;
      font-family: system-ui; font-size: 12px; font-weight: 700;
      letter-spacing: .03em;
    }
    .ticker-inner {
      display: flex; align-items: stretch;
    }
    .ticker-label {
      background: #000; color: var(--accent);
      padding: 5px 14px; flex-shrink: 0;
      font-size: 11px; letter-spacing: .1em;
      display: flex; align-items: center;
    }
    .ticker-track {
      padding: 5px 0;
      overflow: hidden; flex: 1;
    }
    .ticker-scroll {
      display: inline-block;
      animation: ticker 60s linear infinite;
      padding-left: 100%;
    }
    .ticker-scroll:hover { animation-play-state: paused; }
    .ticker-item { display: inline; margin-right: 60px; color: #111; }
    .ticker-item a { color: #111; }
    .ticker-item a:hover { text-decoration: underline; }
    @keyframes ticker { from { transform: translateX(0); } to { transform: translateX(-100%); } }

    /* ── Topic nav ── */
    .topic-nav {
      background: var(--surface2);
      border-bottom: 1px solid var(--border);
      position: relative;
    }
    .topic-nav::after {
      content: '';
      position: absolute; right: 0; top: 0; bottom: 0; width: 60px;
      background: linear-gradient(to right, transparent, var(--surface2));
      pointer-events: none;
    }
    .topic-nav-scroll {
      overflow-x: auto; scrollbar-width: none;
    }
    .topic-nav-scroll::-webkit-scrollbar { display: none; }
    .topic-nav-inner {
      max-width: 1280px; margin: 0 auto;
      padding: 0 20px;
      display: flex; gap: 0; width: max-content; min-width: 100%;
    }
    .tnav-pill {
      padding: 10px 16px; font-size: 12px; font-weight: 700;
      font-family: system-ui; letter-spacing: .03em;
      color: var(--muted); white-space: nowrap;
      border-bottom: 3px solid transparent;
      transition: color .15s, border-color .15s; flex-shrink: 0;
    }
    .tnav-pill:hover { color: var(--text); }
    .tnav-pill.active { color: var(--accent); border-bottom-color: var(--accent); }

    /* ════════════════════════════════════════════
       PAGE WRAPPER
    ════════════════════════════════════════════ */
    .page { max-width: 1280px; margin: 0 auto; padding: 24px 20px 80px; }

    /* ════════════════════════════════════════════
       HERO
    ════════════════════════════════════════════ */
    .hero {
      display: grid;
      grid-template-columns: 1fr 420px;
      gap: 0;
      background: var(--surface);
      border: 1px solid var(--border2);
      border-radius: var(--radius);
      overflow: hidden;
      margin-bottom: 24px;
      box-shadow: var(--shadow);
      min-height: 320px;
    }
    @media (max-width: 860px) { .hero { grid-template-columns: 1fr; } .hero-img { max-height: 220px; } }
    .hero-body {
      padding: 28px 30px;
      display: flex; flex-direction: column; justify-content: space-between;
      border-right: 1px solid var(--border);
    }
    .hero-badges { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; }
    .badge-breaking {
      background: #c8102e; color: #fff;
      font-size: 10px; font-weight: 900; letter-spacing: .12em;
      padding: 3px 8px; border-radius: 3px;
      font-family: system-ui; text-transform: uppercase;
      animation: pulse 2s infinite;
    }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.7} }
    .badge-new {
      background: var(--green); color: #fff;
      font-size: 10px; font-weight: 800; letter-spacing: .08em;
      padding: 3px 8px; border-radius: 3px;
      font-family: system-ui; text-transform: uppercase;
    }
    .badge-topic {
      font-size: 11px; font-weight: 800; letter-spacing: .1em;
      text-transform: uppercase; font-family: system-ui;
    }
    .hero h1 {
      font-size: 28px; font-weight: 700; line-height: 1.25;
      margin-bottom: 14px; color: var(--text);
    }
    .hero h1 a:hover { color: var(--accent); }
    .hero-summary {
      font-size: 15px; color: var(--text2); line-height: 1.6;
      margin-bottom: 20px; flex: 1;
    }
    .hero-meta {
      font-size: 12px; color: var(--muted); font-family: system-ui;
      display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
    }
    .hero-source { font-weight: 700; color: var(--text2); }
    .hero-read {
      display: inline-block; margin-top: 16px;
      background: var(--accent); color: #111;
      padding: 10px 20px; border-radius: var(--radius);
      font-size: 13px; font-weight: 700; font-family: system-ui;
      transition: background .15s; align-self: flex-start;
    }
    .hero-read:hover { background: var(--accent2); }
    .hero-img {
      overflow: hidden; background: var(--surface2);
    }
    .hero-img img {
      width: 100%; height: 100%;
      object-fit: cover; display: block;
      transition: transform .4s;
    }
    .hero:hover .hero-img img { transform: scale(1.03); }
    .hero-img-placeholder {
      width: 100%; height: 100%; min-height: 280px;
      background: linear-gradient(135deg, #111208 0%, #1a1510 50%, #0c0e12 100%);
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      color: var(--muted); gap: 10px;
    }
    .hero-img-placeholder span { font-size: 11px; letter-spacing: .1em; text-transform: uppercase; font-family: system-ui; }

    /* ════════════════════════════════════════════
       AD BANNER
    ════════════════════════════════════════════ */
    .ad-banner {
      background: var(--surface2); border: 1px dashed var(--border2);
      border-radius: var(--radius); height: 90px;
      display: flex; align-items: center; justify-content: center;
      color: var(--muted); font-size: 11px; font-family: system-ui;
      margin-bottom: 24px; letter-spacing: .05em;
    }

    /* ════════════════════════════════════════════
       CONTENT GRID
    ════════════════════════════════════════════ */
    .content-grid {
      display: grid;
      grid-template-columns: 1fr 260px;
      gap: 24px;
      align-items: start;
    }
    @media (max-width: 900px) { .content-grid { grid-template-columns: 1fr; } .sidebar { display: none; } }

    /* ── Section label ── */
    .section-label {
      font-size: 11px; font-weight: 800; letter-spacing: .12em;
      text-transform: uppercase; color: var(--muted);
      font-family: system-ui; margin-bottom: 14px;
      padding-bottom: 8px; border-bottom: 1px solid var(--border);
      display: flex; align-items: center; justify-content: space-between;
    }

    /* ════════════════════════════════════════════
       STORY CARDS (3-col grid)
    ════════════════════════════════════════════ */
    .story-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 14px;
    }
    @media (max-width: 700px) { .story-grid { grid-template-columns: 1fr; } }
    @media (min-width: 701px) and (max-width: 1000px) { .story-grid { grid-template-columns: repeat(2, 1fr); } }

    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
      display: flex; flex-direction: column;
      transition: border-color .2s, transform .2s, box-shadow .2s;
      box-shadow: 0 2px 10px rgba(0,0,0,.4);
    }
    .card:hover {
      border-color: rgba(255,255,255,.18);
      transform: translateY(-3px);
      box-shadow: 0 10px 30px rgba(0,0,0,.55);
    }

    /* card image */
    .card-img {
      aspect-ratio: 16 / 9;
      overflow: hidden; background: var(--surface2); flex-shrink: 0;
    }
    .card-img img {
      width: 100%; height: 100%; object-fit: cover; display: block;
      transition: transform .4s;
    }
    .card:hover .card-img img { transform: scale(1.05); }

    /* no-image card gets a colored left accent bar */
    .card.no-img { border-left: 3px solid var(--accent); }
    .card.no-img.tc-blue  { border-left-color: #3b82f6; }
    .card.no-img.tc-purple{ border-left-color: #8b5cf6; }
    .card.no-img.tc-orange{ border-left-color: #f97316; }
    .card.no-img.tc-green { border-left-color: #22c55e; }
    .card.no-img.tc-steel { border-left-color: #64748b; }
    .card.no-img.tc-sky   { border-left-color: #38bdf8; }
    .card.no-img.tc-pink  { border-left-color: #e879f9; }

    /* card body */
    .card-body { padding: 14px 15px 15px; display: flex; flex-direction: column; flex: 1; }

    /* topic badge only — no breaking badge on cards */
    .card-topic-badge {
      display: inline-block; margin-bottom: 9px;
      font-size: 10px; font-weight: 800; letter-spacing: .1em;
      text-transform: uppercase; font-family: system-ui;
    }
    /* new dot — subtle indicator for fresh stories */
    .new-dot {
      display: inline-block; width: 7px; height: 7px;
      border-radius: 50%; background: var(--green);
      margin-left: 6px; vertical-align: middle;
      flex-shrink: 0;
    }

    .card h2 {
      font-size: 15px; font-weight: 700; line-height: 1.38;
      margin-bottom: 8px; color: var(--text);
    }
    .card h2 a:hover { color: var(--accent); }
    .card-summary {
      font-size: 13px; color: var(--text2); line-height: 1.52;
      margin-bottom: 10px; flex: 1;
      display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
      font-family: system-ui;
    }
    .card-meta {
      font-size: 11px; color: var(--muted); font-family: system-ui;
      display: flex; align-items: center; gap: 8px; margin-top: auto;
      padding-top: 10px; border-top: 1px solid var(--border);
    }
    .card-source { font-weight: 700; color: var(--text2); }
    .card-dot { color: var(--border2); }

    /* category color coding */
    .t-trump, .t-election, .t-deep-state, .t-deep_state,
    .t-fbi, .t-cia, .t-doj, .t-dni,
    .t-indictment, .t-impeachment, .t-corruption { color: #ef4444; }
    .t-russia, .t-ukraine, .t-zelensky, .t-nato, .t-brics { color: #60a5fa; }
    .t-israel, .t-netanyahu, .t-gaza, .t-iran,
    .t-saudi, .t-saudi-arabia { color: #a78bfa; }
    .t-china, .t-taiwan, .t-north-korea { color: #fb923c; }
    .t-bitcoin, .t-crypto, .t-cbdc,
    .t-economy, .t-federal-reserve { color: #34d399; }
    .t-military, .t-pentagon { color: #94a3b8; }
    .t-ufo { color: #e879f9; }
    .t-musk, .t-doge { color: #38bdf8; }

    /* ════════════════════════════════════════════
       SIDEBAR
    ════════════════════════════════════════════ */
    .sidebar-widget {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
      margin-bottom: 16px;
    }
    .widget-head {
      font-size: 11px; font-weight: 800; letter-spacing: .1em;
      text-transform: uppercase; font-family: system-ui;
      padding: 10px 14px; border-bottom: 1px solid var(--border);
      color: var(--muted);
    }
    .topic-row {
      display: flex; align-items: center; justify-content: space-between;
      padding: 8px 14px; border-bottom: 1px solid var(--border);
      font-size: 13px; font-family: system-ui;
      transition: background .1s;
    }
    .topic-row:last-child { border-bottom: none; }
    .topic-row:hover { background: var(--surface2); }
    .topic-row a { color: var(--text2); font-weight: 600; }
    .topic-row a:hover { color: var(--text); }
    .topic-count {
      font-size: 11px; color: var(--muted);
      background: var(--surface2); padding: 2px 7px; border-radius: 999px;
    }
    .ad-rect {
      background: var(--surface2); border: 1px dashed var(--border2);
      border-radius: var(--radius); height: 250px;
      display: flex; align-items: center; justify-content: center;
      color: var(--muted); font-size: 11px; font-family: system-ui;
      margin-bottom: 16px;
    }

    /* ════════════════════════════════════════════
       LOAD MORE
    ════════════════════════════════════════════ */
    .load-wrap { grid-column: 1/-1; text-align: center; margin-top: 24px; }
    #loadMore {
      padding: 11px 32px; border-radius: var(--radius);
      background: var(--surface); border: 1px solid var(--border2);
      color: var(--text); font-weight: 700; font-size: 13px;
      font-family: system-ui; cursor: pointer; transition: all .15s;
    }
    #loadMore:hover { border-color: var(--accent); color: var(--accent); }
    #loadStatus { font-size: 12px; color: var(--muted); margin-top: 8px; font-family: system-ui; }

    /* ════════════════════════════════════════════
       EMPTY
    ════════════════════════════════════════════ */
    .empty {
      grid-column: 1/-1;
      text-align: center; padding: 80px 20px;
      color: var(--muted); font-family: system-ui;
    }
    .empty strong { display: block; font-size: 20px; margin-bottom: 8px; color: var(--text); }
  </style>
</head>
<body>

<!-- ═══════════════ MASTHEAD ═══════════════ -->
<header class="masthead">
  <div class="masthead-top">
    <div>
      <div class="site-name">News<em>Wire</em></div>
      <div class="masthead-tagline">
        {{ total_topics }} topics · {{ feed_count }} sources
        {% if last_updated %}· <span id="last-updated" data-utc="{{ last_updated }}"></span>{% endif %}
      </div>
    </div>
    <div class="masthead-right">
      <form class="search-form" method="get" action="{{ url_for('home') }}">
        <input name="q" placeholder="Search headlines…" value="{{ q }}" autocomplete="off"/>
        <button type="submit">Search</button>
      </form>
      <button class="theme-btn" id="theme-btn" title="Toggle theme">🌙</button>
    </div>
  </div>

  <!-- Ticker -->
  {% if stories %}
  <div class="ticker">
    <div class="ticker-inner">
      <div class="ticker-label">LATEST</div>
      <div class="ticker-track">
        <div class="ticker-scroll">
          {% for s in stories[:12] %}
            <span class="ticker-item"><a href="{{ s.link }}" target="_blank" rel="noopener">{{ s.title }}</a></span>
          {% endfor %}
        </div>
      </div>
    </div>
  </div>
  {% endif %}

  <!-- Topic nav — curated shortlist only -->
  <nav class="topic-nav">
    <div class="topic-nav-scroll">
      <div class="topic-nav-inner">
        <a class="tnav-pill {% if not active_topic %}active{% endif %}" href="{{ url_for('home') }}">All</a>
        {% for t in nav_topics %}
          <a class="tnav-pill {% if active_topic and active_topic|lower == t|lower %}active{% endif %}"
             href="{{ url_for('topic_page', topic=t) }}">{{ t }}</a>
        {% endfor %}
      </div>
    </div>
  </nav>
</header>

<div class="page">

  <!-- ═══════════════ HERO ═══════════════ -->
  {% if hero %}
  <section class="hero">
    <div class="hero-body">
      <div>
        <div class="hero-badges">
          {% if hero.is_breaking %}<span class="badge-breaking">Breaking</span>{% elif hero.is_new %}<span class="badge-new">New</span>{% endif %}
          <span class="badge-topic {{ ('t-' + hero.topic|lower|replace(' / ','_')|replace(' ','-')|replace('/','')) if hero.topic else '' }}">{{ hero.topic }}</span>
        </div>
        <h1><a href="{{ hero.link }}" target="_blank" rel="noopener noreferrer">{{ hero.title }}</a></h1>
        {% if hero.summary %}
          <div class="hero-summary">{{ hero.summary[:280] }}{% if hero.summary|length > 280 %}…{% endif %}</div>
        {% endif %}
      </div>
      <div>
        <div class="hero-meta">
          <span class="hero-source">{{ hero.source }}</span>
          <span>{{ hero.added_at }}</span>
        </div>
        <a class="hero-read" href="{{ hero.link }}" target="_blank" rel="noopener noreferrer">Read full story →</a>
      </div>
    </div>
    <div class="hero-img">
      {% if hero.image_url %}
        <img src="{{ hero.image_url }}" alt="{{ hero.title }}" loading="eager"/>
      {% else %}
        <div class="hero-img-placeholder">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" opacity=".3"><path d="M4 4h16v16H4zM4 9h16M9 9v11"/></svg>
          <span>No image available</span>
        </div>
      {% endif %}
    </div>
  </section>
  {% endif %}

  <!-- Ad banner -->
  <div class="ad-banner">
    Advertisement
    <!-- <ins class="adsbygoogle" style="display:block" data-ad-client="ca-pub-XXXX"
         data-ad-slot="XXXXXXXXXX" data-ad-format="auto" data-full-width-responsive="true"></ins>
    <script>(adsbygoogle = window.adsbygoogle || []).push({});</script> -->
  </div>

  <!-- ═══════════════ CONTENT ═══════════════ -->
  <div class="content-grid">
    <div>
      <div class="section-label">
        <span>{{ heading }}</span>
      </div>

      <div class="story-grid" id="stories">
        {% if stories %}
          {% for s in stories %}
          {% set tc = '' %}
          {% if s.topic %}
            {% set tl = s.topic|lower %}
            {% if 'russia' in tl or 'ukraine' in tl or 'nato' in tl or 'putin' in tl or 'zelensky' in tl or 'brics' in tl %}{% set tc = 'tc-blue' %}
            {% elif 'israel' in tl or 'gaza' in tl or 'iran' in tl or 'netanyahu' in tl or 'saudi' in tl %}{% set tc = 'tc-purple' %}
            {% elif 'china' in tl or 'taiwan' in tl or 'korea' in tl %}{% set tc = 'tc-orange' %}
            {% elif 'bitcoin' in tl or 'crypto' in tl or 'cbdc' in tl or 'economy' in tl or 'federal' in tl %}{% set tc = 'tc-green' %}
            {% elif 'military' in tl or 'pentagon' in tl %}{% set tc = 'tc-steel' %}
            {% elif 'musk' in tl or 'doge' in tl %}{% set tc = 'tc-sky' %}
            {% elif 'ufo' in tl or 'uap' in tl %}{% set tc = 'tc-pink' %}
            {% endif %}
          {% endif %}
          <article class="card {% if not s.image_url %}no-img {{ tc }}{% endif %}">
            {% if s.image_url %}
            <div class="card-img">
              <img src="{{ s.image_url }}" alt="{{ s.title }}" loading="lazy"/>
            </div>
            {% endif %}
            <div class="card-body">
              <div style="display:flex;align-items:center;gap:6px;margin-bottom:9px;">
                {% if s.topic %}
                <span class="card-topic-badge {{ ('t-' + s.topic|lower|replace(' / ','_')|replace(' ','-')|replace('/','')) }}">{{ s.topic }}</span>
                {% endif %}
                {% if s.is_breaking %}<span class="badge-breaking" style="font-size:9px;padding:2px 6px;">Breaking</span>
                {% elif s.is_new %}<span class="new-dot" title="Recent"></span>{% endif %}
              </div>
              <h2><a href="{{ s.link }}" target="_blank" rel="noopener noreferrer">{{ s.title }}</a></h2>
              {% if s.summary %}
                <div class="card-summary">{{ s.summary }}</div>
              {% endif %}
              <div class="card-meta">
                <span class="card-source">{{ s.source }}</span>
                <span class="card-dot">·</span>
                <span>{{ s.added_at }}</span>
              </div>
            </div>
          </article>
          {% endfor %}
        {% else %}
          <div class="empty">
            <strong>No stories found</strong>
            {% if q %}Try a different search term.{% else %}Check back soon — the feed updates every 15 minutes.{% endif %}
          </div>
        {% endif %}
      </div>

      <div class="load-wrap">
        <button id="loadMore" data-page="{{ page }}" data-topic="{{ active_topic or '' }}" data-q="{{ q }}">
          Load more stories
        </button>
        <div id="loadStatus"></div>
      </div>
    </div>

    <!-- Sidebar -->
    <div class="sidebar">
      <div class="ad-rect">Advertisement</div>
      <div class="sidebar-widget">
        <div class="widget-head">Topics</div>
        {% for t in all_topics %}
          <div class="topic-row">
            <a href="{{ url_for('topic_page', topic=t) }}">{{ t }}</a>
            {% if topic_counts.get(t) %}
              <span class="topic-count">{{ topic_counts[t] }}</span>
            {% endif %}
          </div>
        {% endfor %}
      </div>
    </div>
  </div>

</div><!-- /page -->

<script>
(function () {
  // ── Theme ──
  const btn = document.getElementById('theme-btn');
  const saved = localStorage.getItem('nw-theme') || 'dark';
  if (saved === 'light') { document.body.classList.add('light'); btn.textContent = '☀️'; }
  btn.addEventListener('click', () => {
    const light = document.body.classList.toggle('light');
    btn.textContent = light ? '☀️' : '🌙';
    localStorage.setItem('nw-theme', light ? 'light' : 'dark');
  });

  // ── Last updated ──
  const lu = document.getElementById('last-updated');
  if (lu) {
    try {
      const d = new Date(lu.dataset.utc);
      if (!isNaN(d)) lu.textContent = 'Updated ' + d.toLocaleTimeString(undefined, {hour:'numeric',minute:'2-digit',hour12:true});
    } catch(_) {}
  }

  // ── HTML escape ──
  const esc = s => (s||'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // ── Topic CSS class ──
  const topicClass = t => t ? 't-' + t.toLowerCase().replace(/ \/ /g,'_').replace(/ /g,'-').replace(/\//g,'') : '';

  // ── Topic color class ──
  function tcClass(topic) {
    const t = (topic||'').toLowerCase();
    if (/russia|ukraine|nato|putin|zelensky|brics/.test(t)) return 'tc-blue';
    if (/israel|gaza|iran|netanyahu|saudi/.test(t)) return 'tc-purple';
    if (/china|taiwan|korea/.test(t)) return 'tc-orange';
    if (/bitcoin|crypto|cbdc|economy|federal/.test(t)) return 'tc-green';
    if (/military|pentagon/.test(t)) return 'tc-steel';
    if (/musk|doge/.test(t)) return 'tc-sky';
    if (/ufo|uap/.test(t)) return 'tc-pink';
    return '';
  }

  // ── Render card from API JSON ──
  function renderCard(s) {
    const hasImg = !!s.image_url;
    const imgHtml = hasImg
      ? `<div class="card-img"><img src="${esc(s.image_url)}" alt="${esc(s.title)}" loading="lazy"/></div>`
      : '';
    const newDot = s.is_breaking
      ? '<span class="badge-breaking" style="font-size:9px;padding:2px 6px;">Breaking</span>'
      : (s.is_new ? '<span class="new-dot" title="Recent"></span>' : '');
    const topicBadge = s.topic
      ? `<span class="card-topic-badge ${topicClass(s.topic)}">${esc(s.topic)}</span>`
      : '';
    const summaryHtml = s.summary
      ? `<div class="card-summary">${esc(s.summary)}</div>`
      : '';
    const noImgClass = hasImg ? '' : `no-img ${tcClass(s.topic)}`;
    return `<article class="card ${noImgClass}">
      ${imgHtml}
      <div class="card-body">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:9px;">${topicBadge}${newDot}</div>
        <h2><a href="${esc(s.link)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a></h2>
        ${summaryHtml}
        <div class="card-meta">
          <span class="card-source">${esc(s.source)}</span>
          <span class="card-dot">·</span>
          <span>${esc(s.added_at)}</span>
        </div>
      </div>
    </article>`;
  }

  // ── Load more ──
  const loadBtn = document.getElementById('loadMore');
  const status  = document.getElementById('loadStatus');
  const list    = document.getElementById('stories');

  loadBtn.addEventListener('click', async () => {
    const nextPage = parseInt(loadBtn.dataset.page || '1', 10) + 1;
    const params = new URLSearchParams({ page: nextPage });
    if (loadBtn.dataset.topic) params.set('topic', loadBtn.dataset.topic);
    if (loadBtn.dataset.q)     params.set('q', loadBtn.dataset.q);
    loadBtn.disabled = true;
    status.textContent = 'Loading…';
    try {
      const res  = await fetch('/api/stories?' + params, { headers: { Accept: 'application/json' } });
      const data = await res.json();
      if (!data.stories?.length) {
        status.textContent = 'No more stories.';
        loadBtn.style.display = 'none';
        return;
      }
      list.insertAdjacentHTML('beforeend', data.stories.map(renderCard).join(''));
      loadBtn.dataset.page = nextPage;
      status.textContent = '';
    } catch (e) {
      status.textContent = 'Error loading. Try again.';
    } finally {
      loadBtn.disabled = false;
    }
  });
})();
</script>
</body>
</html>
"""


# ── Template helper ───────────────────────────────────────────────────────────

def render(heading, stories, page, active_topic=None, q=""):
    topic_counts = get_article_counts()
    # Pull hero from first story, rest go into the grid
    hero = stories[0] if stories else None
    grid = stories[1:] if stories else []
    return render_template_string(
        BASE_HTML,
        page_title=f"{active_topic} – NewsWire" if active_topic else "NewsWire – Breaking News Aggregator",
        heading=heading,
        hero=hero,
        stories=grid,
        page=page,
        active_topic=active_topic,
        q=q,
        all_topics=ALL_TOPICS,
        nav_topics=NAV_TOPICS,
        total_topics=len(ALL_TOPICS),
        feed_count=35,
        last_updated=get_latest_update(),
        topic_counts=topic_counts,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.before_request
def _ensure_columns():
    ensure_image_column()
    ensure_brief_columns()


@app.get("/health")
def health():
    return "ok", 200


@app.get("/version")
def version():
    return {"build": APP_BUILD, "utc": datetime.now(timezone.utc).isoformat()}


@app.get("/api/stories")
def api_stories():
    q     = request.args.get("q", "").strip() or None
    topic = request.args.get("topic", "").strip() or None
    page  = max(int(request.args.get("page", "1") or "1"), 1)
    limit = max(int(request.args.get("limit", str(PAGE_SIZE)) or PAGE_SIZE), 1)
    rows  = get_stories(limit=limit, page=page, search=q, topic=topic)
    return jsonify({"page": page, "count": len(rows), "stories": [serialize_story(r) for r in rows]})


@app.route("/")
def home():
    q      = request.args.get("q", "").strip()
    page   = max(int(request.args.get("page", "1") or "1"), 1)
    rows   = get_stories(limit=PAGE_SIZE, page=page, search=q or None)
    stories = [serialize_story(r) for r in rows]
    heading = f'Search results for "{q}"' if q else "Latest Stories"
    return render(heading, stories, page, q=q)


@app.route("/topic/<topic>")
def topic_page(topic):
    page    = max(int(request.args.get("page", "1") or "1"), 1)
    rows    = get_stories(limit=PAGE_SIZE, page=page, topic=topic)
    stories = [serialize_story(r) for r in rows]
    return render(f"{topic} News", stories, page, active_topic=topic)


# ── Daily Herold Brief ────────────────────────────────────────────────────────

DAILY_HERALD_PROMPT = """You are a research assistant for Jon Herold (aka Patel Patriot), host of The Daily Herold on Badlands Media's Rumble channel.

Jon's analytical framework:
- The 2020 election was illegitimate — foreign interference and systemic institutional corruption are documented, not theory
- The federal government has metastasized far beyond its constitutional mandate. The administrative/deep state is the structural problem, not any particular party
- Trump is not a conventional politician. He is dismantling a multi-generational entrenched power structure that controls both parties, most institutions, and much of the media
- Devolution theory: before leaving office in January 2021, Trump may have invoked Continuity of Government (COG) frameworks as a constitutional backstop, devolving certain authorities due to documented foreign interference
- First principles always — what does the Constitution actually authorize? What does the evidence actually show — not what does the narrative claim?
- Avoid both failure modes: blind cheerleading ("everything is 5D chess, trust the plan") and catastrophizing ("this is betrayal, it's over"). Both are intellectually lazy and dishonest
- Honesty over tribal loyalty — if something cuts against your own side, say so. Jon's audience trusts him because he calls it straight

Generate a Daily Herold podcast brief for the story below. Use exactly this format — plain text, no markdown symbols:

THE HOOK
[1-2 punchy sentences to open the segment. Frame immediately why this matters to a constitutional, first-principles audience.]

TALKING POINTS
- [Most important fact or development in the story]
- [What makes it significant — not just what happened, but why it matters structurally]
- [Historical or institutional context your audience needs]
- [What to watch next — what does this set up?]

FIRST PRINCIPLES CHECK
[Honest, direct assessment. What does the evidence actually show? Where is this story clear and where is it ambiguous? Do not spin toward a preferred outcome. If Trump did something questionable, say so. If the media framing is deceptive, call that out too.]

THE BIGGER PICTURE
[How does this connect to the dismantling of the administrative state, COG, foreign interference, election integrity, or constitutional restoration? If it doesn't connect clearly, say so — not every story is a Devolution proof and pretending otherwise destroys credibility.]

AUDIENCE QUESTIONS
- [Question that invites first-principles thinking]
- [Question that challenges the audience to look past the surface narrative]
- [Question that connects this story to the longer arc, if applicable]

TRAPS TO AVOID
[Call out the cheerleading or dooming angles this story will tempt people toward — and why staying disciplined matters here.]

Be sharp and honest. Jon's audience is sophisticated. They don't want confirmation of their priors — they want truth, even when it's uncomfortable."""


def generate_brief(title, summary, bullets, topic, source, link):
    if not _brief_client:
        return "OpenAI API key not configured. Set OPENAI_API_KEY in Render environment."

    story_text = f"Headline: {title}\nTopic: {topic}\nSource: {source}\n"
    if summary:
        story_text += f"Summary: {summary}\n"
    if bullets:
        story_text += "Key details:\n" + "\n".join(f"- {b}" for b in bullets)

    try:
        resp = _brief_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": DAILY_HERALD_PROMPT},
                {"role": "user", "content": story_text},
            ],
            max_tokens=700,
            temperature=0.4,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return f"Error generating brief: {e}"


RELEVANCE_PROMPT = """You score news stories for relevance to The Daily Herold, a daily political commentary show on Badlands Media's Rumble channel, hosted by Jon Herold (Patel Patriot).

The audience cares about:
- MAJOR breaking news of broad national/international importance (always high value — the audience lives in the real world)
- Anything related to Trump, his administration, executive actions, and the dismantling of the deep state
- Devolution theory, continuity of government, military operations, COG signals
- Deep state exposure: FBI, CIA, DOJ, intelligence community corruption and accountability
- Election integrity, lawfare, political persecution
- Epstein/trafficking/elite corruption — stories with real substance and sourcing
- Geopolitics through a non-neocon lens: Russia/Ukraine, Israel/Gaza, Iran, China
- Financial system exposure: Federal Reserve, CBDC, crypto, economic warfare
- Censorship, Big Tech, media manipulation
- Stories that interest the Q-adjacent community but have actual evidence (not decoder speculation)
- Constitutional issues, Supreme Court, executive power

Score 1–10 using this scale:
10 — Major breaking news OR direct Devolution/deep state signal with substance
8-9 — High relevance to the audience's core interests, real information
6-7 — Relevant topic, useful context, worth knowing
4-5 — Tangentially related, mainstream framing, little new info
1-3 — Junk, clickbait, off-topic, or pure speculation with no substance

Respond with ONLY a single integer 1–10. Nothing else."""


def score_relevance(title, summary, topic, source):
    """Return an integer relevance score 1-10 for the /brief page sort."""
    if not _brief_client:
        return 5
    story = f"Headline: {title}\nTopic: {topic}\nSource: {source}"
    if summary:
        story += f"\nSummary: {summary[:300]}"
    try:
        resp = _brief_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": RELEVANCE_PROMPT},
                {"role": "user", "content": story},
            ],
            max_tokens=3,
            temperature=0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        score = int(re.search(r"\d+", raw).group())
        return max(1, min(10, score))
    except Exception:
        return 5


BRIEF_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Daily Herold Brief — Badlands Media</title>
  <style>
    :root {
      --bg: #0a0d12; --surface: #111720; --surface2: #192030;
      --border: rgba(255,255,255,.08); --text: #e4ecf5; --muted: #6b85a0;
      --accent: #c8102e; --accent2: #a00d24; --gold: #c9a84c;
      --radius: 12px; --shadow: 0 8px 32px rgba(0,0,0,.6);
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
           background: var(--bg); color: var(--text); line-height: 1.6; }
    a { color: var(--gold); text-decoration: none; }

    /* ── Password gate ── */
    .gate {
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
      padding: 20px;
    }
    .gate-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 40px; max-width: 380px; width: 100%;
      text-align: center; box-shadow: var(--shadow);
    }
    .gate-logo { font-size: 22px; font-weight: 900; letter-spacing: -.5px; margin-bottom: 6px; }
    .gate-logo span { color: var(--accent); }
    .gate-sub { font-size: 13px; color: var(--muted); margin-bottom: 28px; }
    .gate-input {
      width: 100%; padding: 12px 16px; border-radius: 10px;
      border: 1px solid var(--border); background: var(--surface2);
      color: var(--text); font-size: 15px; margin-bottom: 14px;
      outline: none; letter-spacing: 2px; text-align: center;
    }
    .gate-input:focus { border-color: var(--accent); }
    .gate-btn {
      width: 100%; padding: 12px; border-radius: 10px;
      background: var(--accent); border: none; color: #fff;
      font-weight: 800; font-size: 15px; cursor: pointer;
    }
    .gate-err { color: #f87171; font-size: 13px; margin-top: 10px; }

    /* ── Main layout ── */
    .page { max-width: 860px; margin: 0 auto; padding: 24px 16px 60px; }
    .topbar {
      display: flex; align-items: center; justify-content: space-between;
      gap: 12px; margin-bottom: 24px;
      padding-bottom: 18px; border-bottom: 1px solid var(--border);
    }
    .brand { font-size: 20px; font-weight: 900; }
    .brand span { color: var(--accent); }
    .brand-sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
    .logout-btn {
      font-size: 12px; color: var(--muted); background: none;
      border: 1px solid var(--border); border-radius: 8px;
      padding: 6px 12px; cursor: pointer; color: var(--text);
    }

    /* ── Filters ── */
    .filter-row { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 20px; }
    .fpill {
      padding: 5px 13px; border-radius: 999px; border: 1px solid var(--border);
      background: rgba(255,255,255,.04); font-size: 12px; font-weight: 600;
      color: var(--muted); cursor: pointer; transition: all .15s; text-decoration: none;
    }
    .fpill:hover { color: var(--text); }
    .fpill.active { background: var(--accent); border-color: var(--accent); color: #fff; }

    /* ── Story card ── */
    .card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 18px 20px; margin-bottom: 14px;
      box-shadow: var(--shadow);
    }
    .card-topic {
      font-size: 10px; font-weight: 800; text-transform: uppercase;
      letter-spacing: .08em; color: var(--accent); margin-bottom: 7px;
    }
    .card h3 { font-size: 16px; font-weight: 700; line-height: 1.35; margin-bottom: 8px; }
    .card-meta { font-size: 12px; color: var(--muted); margin-bottom: 12px; }
    .card-summary { font-size: 13px; color: var(--muted); margin-bottom: 12px; line-height: 1.5; }

    .brief-btn {
      padding: 8px 18px; border-radius: 8px; border: none;
      background: var(--gold); color: #000; font-weight: 800;
      font-size: 13px; cursor: pointer; transition: opacity .15s;
    }
    .brief-btn:disabled { opacity: .5; cursor: default; }
    .read-link {
      font-size: 13px; color: var(--muted); margin-left: 14px;
    }

    /* ── Brief output ── */
    .brief-out {
      margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border);
      display: none;
    }
    .brief-out.visible { display: block; }
    .brief-section { margin-bottom: 18px; }
    .brief-label {
      font-size: 10px; font-weight: 800; text-transform: uppercase;
      letter-spacing: .1em; color: var(--gold); margin-bottom: 6px;
    }
    .brief-text { font-size: 14px; line-height: 1.65; white-space: pre-wrap; }
    .copy-btn {
      margin-top: 14px; padding: 7px 16px; border-radius: 8px;
      background: var(--surface2); border: 1px solid var(--border);
      color: var(--text); font-size: 12px; font-weight: 700; cursor: pointer;
    }
    .copy-btn:hover { border-color: var(--gold); }
  </style>
</head>
<body>

{% if not authed %}
<div class="gate">
  <div class="gate-card">
    <div class="gate-logo">Daily<span>Herold</span></div>
    <div class="gate-sub">Badlands Media · Show Prep Tool</div>
    <form method="post" action="/brief">
      <input class="gate-input" type="password" name="password" placeholder="••••••••" autofocus/>
      <button class="gate-btn" type="submit">Enter</button>
      {% if error %}<div class="gate-err">Wrong password</div>{% endif %}
    </form>
  </div>
</div>

{% else %}
<div class="page">
  <div class="topbar">
    <div>
      <div class="brand">Daily<span>Herold</span> Brief</div>
      <div class="brand-sub">Badlands Media · Show Prep · Private</div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <a href="/brief/saved" style="font-size:12px;color:var(--gold);border:1px solid var(--gold);border-radius:8px;padding:6px 12px;font-weight:700;text-decoration:none;">📁 Saved Briefs</a>
      <form method="post" action="/brief/logout">
        <button class="logout-btn" type="submit">Log out</button>
      </form>
    </div>
  </div>

  <div class="filter-row">
    <a class="fpill {% if not active_topic %}active{% endif %}" href="/brief">All</a>
    {% for t in all_topics %}
      <a class="fpill {% if active_topic and active_topic|lower == t|lower %}active{% endif %}"
         href="/brief?topic={{ t }}">{{ t }}</a>
    {% endfor %}
  </div>

  <div id="stories">
    {% for s in stories %}
    <div class="card" id="card-{{ loop.index }}">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:7px;">
        <div class="card-topic">{{ s.topic }}</div>
        <div style="font-size:11px;font-weight:800;padding:2px 8px;border-radius:6px;
          background:{% if s.relevance >= 8 %}rgba(201,168,76,.2);color:var(--gold)
                     {% elif s.relevance >= 6 %}rgba(255,255,255,.07);color:var(--muted)
                     {% else %}rgba(255,255,255,.03);color:#3a4a5a{% endif %};">
          {{ s.relevance }}/10
        </div>
      </div>
      <h3>{{ s.title }}</h3>
      <div class="card-meta">
        {{ s.source }}{% if s.source and s.added_at %} · {% endif %}{{ s.added_at }}
      </div>
      {% if s.summary %}
        <div class="card-summary">{{ s.summary }}</div>
      {% endif %}
      <button class="brief-btn"
              data-idx="{{ loop.index }}"
              data-link="{{ s.link|e }}">
        {% if s.saved_brief %}⚡ Regenerate{% else %}⚡ Generate Brief{% endif %}
      </button>
      {% if s.saved_brief %}
        <span style="font-size:11px;color:var(--gold);margin-left:10px;font-weight:700;">✓ Saved</span>
      {% endif %}
      <a class="read-link" href="{{ s.link }}" target="_blank" rel="noopener noreferrer">
        Read source →
      </a>
      <div class="brief-out" id="brief-{{ loop.index }}"
           {% if s.saved_brief %}data-saved="{{ s.saved_brief | e }}"{% endif %}>
        <div id="brief-content-{{ loop.index }}"></div>
        <button class="copy-btn" onclick="copyBrief({{ loop.index }})">Copy to clipboard</button>
      </div>
    </div>
    {% endfor %}

    {% if not stories %}
      <div style="text-align:center;padding:60px 20px;color:var(--muted);">
        <strong style="color:var(--text);font-size:18px;display:block;margin-bottom:8px;">
          No stories yet</strong>
        Check back in a few minutes — the collector updates every 15 minutes.
      </div>
    {% endif %}
  </div>
</div>

<script>
// Pre-render any saved briefs on page load
document.querySelectorAll('.brief-out[data-saved]').forEach(out => {
  const text = out.dataset.saved;
  const content = out.querySelector('[id^="brief-content-"]');
  if (content && text) {
    content.innerHTML = parseBriefSections(text);
    out.classList.add('visible');
  }
});

document.querySelectorAll('.brief-btn').forEach(btn => {
  btn.addEventListener('click', () => genBrief(btn));
});

function parseBriefSections(text) {
  const sections = [
    'THE HOOK', 'TALKING POINTS', 'FIRST PRINCIPLES CHECK',
    'THE BIGGER PICTURE', 'AUDIENCE QUESTIONS', 'TRAPS TO AVOID'
  ];
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  let html = '';
  let remaining = text;
  for (let i = 0; i < sections.length; i++) {
    const label = sections[i];
    const nextLabel = sections[i + 1];
    const start = remaining.indexOf(label);
    if (start === -1) continue;
    const bodyStart = start + label.length;
    const end = nextLabel ? remaining.indexOf(nextLabel, bodyStart) : remaining.length;
    const body = (end === -1 ? remaining.slice(bodyStart) : remaining.slice(bodyStart, end)).trim();
    html += `<div class="brief-section">
      <div class="brief-label">${label}</div>
      <div class="brief-text">${esc(body)}</div>
    </div>`;
    if (end !== -1) remaining = remaining.slice(0, bodyStart) + remaining.slice(end);
  }
  return html || `<div class="brief-text">${esc(text)}</div>`;
}

async function genBrief(btn) {
  const idx  = btn.dataset.idx;
  const link = btn.dataset.link;
  btn.disabled = true;
  btn.textContent = 'Generating…';
  const out = document.getElementById('brief-' + idx);
  const content = document.getElementById('brief-content-' + idx);
  out.classList.remove('visible');

  try {
    const res = await fetch('/api/brief', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ link: link })
    });
    const data = await res.json();
    const text = data.brief || 'Error generating brief.';
    content.innerHTML = parseBriefSections(text);
    out.classList.add('visible');
    btn.textContent = '⚡ Regenerate';
  } catch(e) {
    content.innerHTML = '<div class="brief-text" style="color:#f87171">Error. Try again.</div>';
    out.classList.add('visible');
    btn.textContent = '⚡ Generate Brief';
  }
  btn.disabled = false;
}

function copyBrief(idx) {
  const el = document.getElementById('brief-content-' + idx);
  navigator.clipboard.writeText(el.innerText).then(() => {
    const btn = el.parentElement.querySelector('.copy-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy to clipboard', 2000);
  });
}
</script>
{% endif %}
</body>
</html>
"""


SAVED_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Saved Briefs · Daily Herold</title>
  <style>
    :root {
      --bg: #0a0d12; --surface: #111720; --surface2: #192030;
      --border: rgba(255,255,255,.08); --text: #e4ecf5; --muted: #6b85a0;
      --accent: #c8102e; --gold: #c9a84c; --radius: 12px;
      --shadow: 0 8px 32px rgba(0,0,0,.6);
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
           background: var(--bg); color: var(--text); line-height: 1.6; }
    a { color: var(--gold); text-decoration: none; }
    .page { max-width: 860px; margin: 0 auto; padding: 24px 16px 60px; }
    .topbar {
      display: flex; align-items: center; justify-content: space-between;
      gap: 12px; margin-bottom: 28px;
      padding-bottom: 18px; border-bottom: 1px solid var(--border);
    }
    .brand { font-size: 20px; font-weight: 900; }
    .brand span { color: var(--accent); }
    .brand-sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
    .back-btn {
      font-size: 12px; color: var(--text); background: none;
      border: 1px solid var(--border); border-radius: 8px;
      padding: 6px 12px; cursor: pointer; text-decoration: none;
    }
    .card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 20px 22px; margin-bottom: 16px;
      box-shadow: var(--shadow);
    }
    .card-topic {
      font-size: 10px; font-weight: 800; text-transform: uppercase;
      letter-spacing: .08em; color: var(--accent); margin-bottom: 6px;
    }
    .card h3 { font-size: 16px; font-weight: 700; line-height: 1.35; margin-bottom: 6px; }
    .card-meta { font-size: 12px; color: var(--muted); margin-bottom: 16px; }
    .brief-section { margin-bottom: 16px; }
    .brief-label {
      font-size: 10px; font-weight: 800; text-transform: uppercase;
      letter-spacing: .1em; color: var(--gold); margin-bottom: 5px;
    }
    .brief-text { font-size: 14px; line-height: 1.65; white-space: pre-wrap; }
    .card-footer { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border);
                   display: flex; gap: 14px; align-items: center; }
    .source-link { font-size: 12px; color: var(--muted); }
    .copy-btn {
      padding: 6px 14px; border-radius: 8px;
      background: var(--surface2); border: 1px solid var(--border);
      color: var(--text); font-size: 12px; font-weight: 700; cursor: pointer;
    }
    .copy-btn:hover { border-color: var(--gold); }
    .empty { text-align: center; padding: 80px 20px; color: var(--muted); }
    .empty strong { color: var(--text); font-size: 18px; display: block; margin-bottom: 8px; }
  </style>
</head>
<body>
<div class="page">
  <div class="topbar">
    <div>
      <div class="brand">Daily<span>Herold</span> Brief</div>
      <div class="brand-sub">Saved Briefs · Badlands Media</div>
    </div>
    <a class="back-btn" href="/brief">← Back to Stories</a>
  </div>

  {% if saved %}
    {% for b in saved %}
    <div class="card" id="saved-{{ loop.index }}">
      <div class="card-topic">{{ b.topic }}</div>
      <h3>{{ b.title }}</h3>
      <div class="card-meta">
        {{ b.source }}{% if b.source and b.briefed_at %} · {% endif %}
        Briefed {{ b.briefed_at }}
      </div>
      <div class="brief-body" data-brief="{{ b.saved_brief | e }}"></div>
      <div class="card-footer">
        <a class="source-link" href="{{ b.link }}" target="_blank" rel="noopener noreferrer">Read source →</a>
        <button class="copy-btn" onclick="copyCard({{ loop.index }})">Copy to clipboard</button>
      </div>
    </div>
    {% endfor %}
  {% else %}
    <div class="empty">
      <strong>No saved briefs yet</strong>
      Generate a brief from the <a href="/brief">stories page</a> and it'll appear here automatically.
    </div>
  {% endif %}
</div>

<script>
const sections = [
  'THE HOOK','TALKING POINTS','FIRST PRINCIPLES CHECK',
  'THE BIGGER PICTURE','AUDIENCE QUESTIONS','TRAPS TO AVOID'
];
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

function parseBriefSections(text) {
  let html = '', remaining = text;
  for (let i = 0; i < sections.length; i++) {
    const label = sections[i], nextLabel = sections[i+1];
    const start = remaining.indexOf(label);
    if (start === -1) continue;
    const bodyStart = start + label.length;
    const end = nextLabel ? remaining.indexOf(nextLabel, bodyStart) : remaining.length;
    const body = (end === -1 ? remaining.slice(bodyStart) : remaining.slice(bodyStart, end)).trim();
    html += `<div class="brief-section"><div class="brief-label">${label}</div><div class="brief-text">${esc(body)}</div></div>`;
    if (end !== -1) remaining = remaining.slice(0, bodyStart) + remaining.slice(end);
  }
  return html || `<div class="brief-text">${esc(text)}</div>`;
}

document.querySelectorAll('.brief-body[data-brief]').forEach(el => {
  el.innerHTML = parseBriefSections(el.dataset.brief);
});

function copyCard(idx) {
  const el = document.getElementById('saved-' + idx).querySelector('.brief-body');
  navigator.clipboard.writeText(el.innerText).then(() => {
    const btn = document.getElementById('saved-' + idx).querySelector('.copy-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy to clipboard', 2000);
  });
}
</script>
</body>
</html>
"""


def brief_authed():
    return request.cookies.get("brief_auth") == BRIEF_PASSWORD


@app.route("/brief", methods=["GET", "POST"])
def brief():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == BRIEF_PASSWORD:
            ensure_brief_columns()
            resp = make_response(render_template_string(
                BRIEF_HTML, authed=True, error=False,
                stories=_brief_stories(request.args.get("topic")),
                all_topics=ALL_TOPICS,
                active_topic=request.args.get("topic", ""),
            ))
            resp.set_cookie("brief_auth", BRIEF_PASSWORD, max_age=60*60*24*30, httponly=True)
            return resp
        return render_template_string(BRIEF_HTML, authed=False, error=True)

    if not brief_authed():
        return render_template_string(BRIEF_HTML, authed=False, error=False)

    ensure_brief_columns()
    topic = request.args.get("topic", "").strip() or None
    stories = _brief_stories(topic)
    return render_template_string(
        BRIEF_HTML, authed=True, error=False,
        stories=stories, all_topics=ALL_TOPICS, active_topic=topic or "",
    )


@app.route("/brief/saved")
def brief_saved():
    if not brief_authed():
        return render_template_string(BRIEF_HTML, authed=False, error=False)
    ensure_brief_columns()
    rows = get_saved_briefs()
    saved = []
    for r in rows:
        ts = r.get("briefed_at")
        time_str = ""
        if ts:
            try:
                if isinstance(ts, str):
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                elif isinstance(ts, datetime):
                    dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                else:
                    dt = None
                if dt:
                    cst = pytz.timezone("America/Chicago")
                    time_str = dt.astimezone(cst).strftime("%b %d, %Y %I:%M %p %Z")
            except Exception:
                time_str = str(ts)
        saved.append({
            "title": (r.get("title") or "").strip(),
            "link":  (r.get("link") or "").strip(),
            "source": (r.get("source") or "").strip(),
            "topic": (r.get("topic") or "").strip(),
            "saved_brief": r.get("saved_brief") or "",
            "briefed_at": time_str,
        })
    return render_template_string(SAVED_HTML, saved=saved)


@app.post("/brief/logout")
def brief_logout():
    resp = make_response(render_template_string(BRIEF_HTML, authed=False, error=False))
    resp.delete_cookie("brief_auth")
    return resp


@app.post("/api/brief")
def api_brief():
    if not brief_authed():
        return jsonify({"error": "unauthorized"}), 401
    ensure_brief_columns()
    data = request.get_json(force=True) or {}
    link = data.get("link", "")

    # Look up story from DB by link
    tbl = "public.articles" if using_postgres() else "articles"
    ph  = "%s" if using_postgres() else "?"
    rows = fetch_rows(
        f"SELECT title, link, source, topic, summary, description FROM {tbl} WHERE link = {ph} LIMIT 1",
        (link,)
    )
    if not rows:
        return jsonify({"brief": "Story not found in database."})

    row = rows[0]
    parsed = parse_summary(row.get("summary") or row.get("description") or "")
    brief_text = generate_brief(
        title=row.get("title", ""),
        summary=parsed["summary"],
        bullets=parsed["bullets"],
        topic=row.get("topic", ""),
        source=row.get("source", ""),
        link=link,
    )

    # Persist the brief so it shows up on the Saved Briefs page
    save_brief_to_db(link, brief_text)

    return jsonify({"brief": brief_text})


def _brief_stories(topic=None):
    """Load stories for the brief page, score relevance, sort by score."""
    tbl = "public.articles" if using_postgres() else "articles"
    ph  = "%s" if using_postgres() else "?"
    if topic:
        rows = fetch_rows(
            f"SELECT title,link,source,topic,summary,added_at,saved_brief "
            f"FROM {tbl} WHERE lower(topic)=lower({ph}) ORDER BY added_at DESC LIMIT 40",
            (topic,)
        )
    else:
        rows = fetch_rows(
            f"SELECT title,link,source,topic,summary,added_at,saved_brief "
            f"FROM {tbl} ORDER BY added_at DESC LIMIT 40"
        )
    out = []
    for r in rows:
        s = serialize_story(r)
        s["saved_brief"] = r.get("saved_brief") or ""
        s["relevance"] = score_relevance(s["title"], s["summary"], s["topic"], s["source"])
        out.append(s)
    # Sort: saved briefs stay at original position; unsaved sorted by relevance desc
    out.sort(key=lambda x: x["relevance"], reverse=True)
    return out


if __name__ == "__main__":
    app.run(debug=True, port=5000)
