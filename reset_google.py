import sqlite3

DB = "news.db"

conn = sqlite3.connect(DB)
cur = conn.cursor()

cur.execute("DELETE FROM seen_keys")
cur.execute("UPDATE feed_state SET warmed_up=0, warmed_at=NULL WHERE feed_url LIKE '%news.google.com/rss/%'")

conn.commit()
conn.close()

print("Reset Google News keys + rewarm enabled ✅")
