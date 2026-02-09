import sqlite3

DB_FILE = "news.db"

conn = sqlite3.connect(DB_FILE)

conn.execute("UPDATE stories SET source='BBC' WHERE (source IS NULL OR source='') AND url LIKE '%bbc.com%'")
conn.execute("UPDATE stories SET source='CoinDesk' WHERE (source IS NULL OR source='') AND url LIKE '%coindesk.com%'")

conn.commit()
conn.close()

print("Backfilled sources ✅")
