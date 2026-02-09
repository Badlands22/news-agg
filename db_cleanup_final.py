# db_cleanup_final.py
import sqlite3
import hashlib

DB = "news.db"

def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    print("Before cleanup:")
    print("  Total rows:   ", conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0])
    print("  NULL story_key:", conn.execute("SELECT COUNT(*) FROM stories WHERE story_key IS NULL").fetchone()[0])

    # Step 1: Keep only the newest row per (source, title)
    conn.execute("""
        DELETE FROM stories
        WHERE rowid NOT IN (
            SELECT MAX(rowid)
            FROM stories
            GROUP BY source, title
        )
    """)

    # Step 2: Backfill story_key for any remaining NULLs (using source + title)
    rows = conn.execute("""
        SELECT rowid, source, title
        FROM stories
        WHERE story_key IS NULL
          AND source IS NOT NULL
          AND title IS NOT NULL
    """).fetchall()

    updated = 0
    for row in rows:
        key = hashlib.sha1(f"{row['source']}|{row['title']}".encode("utf-8")).hexdigest()
        conn.execute("UPDATE stories SET story_key = ? WHERE rowid = ?", (key, row['rowid']))
        updated += 1

    conn.commit()

    print("\nAfter cleanup:")
    print("  Updated story_keys:", updated)
    print("  Remaining rows:   ", conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0])
    print("  Duplicates left:  ", conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT source, title, COUNT(*) n
            FROM stories
            GROUP BY source, title
            HAVING n > 1
        )
    """).fetchone()[0] or 0)

    conn.close()

if __name__ == "__main__":
    main()
