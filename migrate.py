import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path("news.db")

def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()

def table_exists(cur, name):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None

def columns(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]

def ensure_tables(cur):
    # stories
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        story_key TEXT UNIQUE,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        source TEXT,
        matched_topic TEXT,
        saved_at TEXT,
        summary TEXT,
        raw_text TEXT,
        has_ai INTEGER DEFAULT 0
    )
    """)

    # seen
    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen (
        url TEXT PRIMARY KEY,
        seen_at TEXT
    )
    """)

    # feed_state
    cur.execute("""
    CREATE TABLE IF NOT EXISTS feed_state (
        feed_url TEXT PRIMARY KEY,
        warmed_up INTEGER DEFAULT 0,
        warmed_at TEXT
    )
    """)

def fix_seen_keys(cur):
    """
    If seen_keys exists but has wrong schema, rebuild it safely.
    Required schema: seen_keys(key TEXT PRIMARY KEY, seen_at TEXT)
    """
    if not table_exists(cur, "seen_keys"):
        cur.execute("""
        CREATE TABLE seen_keys (
            key TEXT PRIMARY KEY,
            seen_at TEXT
        )
        """)
        return

    cols = columns(cur, "seen_keys")
    if "key" in cols and "seen_at" in cols:
        return  # already correct

    # Backup old table
    backup = f"seen_keys_old_{int(datetime.now().timestamp())}"
    cur.execute(f"ALTER TABLE seen_keys RENAME TO {backup}")

    # Create correct table
    cur.execute("""
    CREATE TABLE seen_keys (
        key TEXT PRIMARY KEY,
        seen_at TEXT
    )
    """)

    old_cols = columns(cur, backup)

    # Try to copy any existing keys from the old table
    possible_key_cols = ["key", "seen_key", "url", "id"]
    key_col = next((c for c in possible_key_cols if c in old_cols), None)

    if key_col:
        # Use existing timestamp column if present, else fill now
        time_col = "seen_at" if "seen_at" in old_cols else ("first_seen" if "first_seen" in old_cols else None)

        if time_col:
            cur.execute(f"""
                INSERT OR IGNORE INTO seen_keys(key, seen_at)
                SELECT {key_col}, {time_col} FROM {backup}
            """)
        else:
            # No timestamp column; fill with now
            cur.execute(f"""
                INSERT OR IGNORE INTO seen_keys(key, seen_at)
                SELECT {key_col}, ? FROM {backup}
            """, (now_utc_iso(),))

def add_missing_columns(cur):
    # make sure these exist (won't error if already present because we check)
    def add_col(table, col, ddl):
        if table_exists(cur, table) and col not in columns(cur, table):
            cur.execute(ddl)

    add_col("stories", "story_key", "ALTER TABLE stories ADD COLUMN story_key TEXT")
    add_col("stories", "source", "ALTER TABLE stories ADD COLUMN source TEXT")
    add_col("stories", "matched_topic", "ALTER TABLE stories ADD COLUMN matched_topic TEXT")
    add_col("stories", "saved_at", "ALTER TABLE stories ADD COLUMN saved_at TEXT")
    add_col("stories", "summary", "ALTER TABLE stories ADD COLUMN summary TEXT")
    add_col("stories", "raw_text", "ALTER TABLE stories ADD COLUMN raw_text TEXT")
    add_col("stories", "has_ai", "ALTER TABLE stories ADD COLUMN has_ai INTEGER DEFAULT 0")

    add_col("seen", "seen_at", "ALTER TABLE seen ADD COLUMN seen_at TEXT")

def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    ensure_tables(cur)
    add_missing_columns(cur)
    fix_seen_keys(cur)

    conn.commit()
    conn.close()
    print("Migrations applied ✅ (DB upgraded)")

if __name__ == "__main__":
    main()
