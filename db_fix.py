import sqlite3

DB = "news.db"

def has_col(cur, table, col):
    cur.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())

conn = sqlite3.connect(DB)
cur = conn.cursor()

# Add missing columns safely
if not has_col(cur, "stories", "saved_at"):
    cur.execute("ALTER TABLE stories ADD COLUMN saved_at TEXT")
if not has_col(cur, "stories", "matched_topic"):
    cur.execute("ALTER TABLE stories ADD COLUMN matched_topic TEXT")
if not has_col(cur, "seen", "seen_at"):
    cur.execute("ALTER TABLE seen ADD COLUMN seen_at TEXT")

# Backfill from older column names if they exist
if has_col(cur, "stories", "created_at") and has_col(cur, "stories", "saved_at"):
    cur.execute("""
        UPDATE stories
        SET saved_at = COALESCE(saved_at, created_at)
        WHERE saved_at IS NULL OR saved_at = ''
    """)

if has_col(cur, "seen", "first_seen") and has_col(cur, "seen", "seen_at"):
    cur.execute("""
        UPDATE seen
        SET seen_at = COALESCE(seen_at, first_seen)
        WHERE seen_at IS NULL OR seen_at = ''
    """)

conn.commit()
conn.close()

print("DB fixed ✅ (added missing columns + backfilled)")
