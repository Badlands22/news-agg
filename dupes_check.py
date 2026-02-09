import sqlite3

c = sqlite3.connect("news.db")
q = """
SELECT COUNT(*) FROM (
  SELECT title, source, COUNT(*) AS n
  FROM stories
  GROUP BY title, source
  HAVING n > 1
)
"""
print(c.execute(q).fetchone()[0])
c.close()
