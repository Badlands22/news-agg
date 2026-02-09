import sqlite3, hashlib

DB = "news.db"

def main():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")

    # 1) Delete duplicates, keep newest rowid per (source,title)
    c.execute("""
        DELETE FROM stories
        WHERE rowid NOT IN (
            SELECT MAX(rowid) FROM stories
            GROUP BY source, title
        )
    """)

    # 2) Backfill story_key for NULLs using sha1(source|title)
    rows = c.execute("""
        SELECT rowid, source, title
        FROM stories
        WHERE story_key IS NULL
          AND source IS NOT NULL
          AND title IS NOT NULL
    """).fetchall()

    for rowid, source, title in rows:
        k = hashlib.sha1(f"{source}|{title}".encode("utf-8")).hexdigest()
        c.execute("UPDATE stories SET story_key=? WHERE rowid=?", (k, rowid))

    c.commit()

    dupe_check = c.execute("""
        SELECT COUNT(*) FROM (
            SELECT source, title, COUNT(*) n
            FROM stories
            GROUP BY source, title
            HAVING n > 1
        )
    """).fetchone()[0]

    null_keys = c.execute("SELECT COUNT(*) FROM stories WHERE story_key IS NULL").fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM stories").fetchone()[0]

    print("dupe-check:", dupe_check)
    print("null story_key:", null_keys)
    print("total stories:", total)

    c.close()

if __name__ == "__main__":
    main()
