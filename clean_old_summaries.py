import sqlite3

DB_PATH = "news.db"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Clear ALL summaries that are not NULL and shorter than 100 chars (old/short ones)
c.execute("""
    UPDATE articles
    SET summary = NULL
    WHERE summary IS NOT NULL AND length(summary) < 100
""")

count = c.rowcount
conn.commit()
conn.close()

print(f"Cleared {count} old/short summaries. Site should now be fully clean!")