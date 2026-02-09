import sqlite3

conn = sqlite3.connect("news.db")
c = conn.cursor()
c.execute("SELECT COUNT(*) FROM articles")
count = c.fetchone()[0]
print(f"You have {count} articles in the database.")
conn.close()