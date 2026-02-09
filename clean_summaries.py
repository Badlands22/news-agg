import sqlite3

DB_PATH = "news.db"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Find articles with bad/old summaries (those that start with "- " or repeat title)
c.execute("""
    SELECT id, title, summary
    FROM articles
    WHERE summary LIKE '- %' OR summary LIKE '%Matched topic:%'
""")

bad_articles = c.fetchall()

if not bad_articles:
    print("No bad summaries found!")
else:
    print(f"Found {len(bad_articles)} bad/old summaries. Fixing them...")
    for article in bad_articles:
        id, title, old_summary = article
        print(f" - {title[:60]}...")
        # Optional: delete the bad summary (sets to None)
        c.execute("UPDATE articles SET summary = NULL WHERE id = ?", (id,))
        # Or keep a placeholder
        # c.execute("UPDATE articles SET summary = ? WHERE id = ?", ("Summary being updated...", id))

conn.commit()
conn.close()
print("Cleanup done! Restart collector to generate new Grok summaries.")