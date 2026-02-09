import os
import requests
from readability import Document
from bs4 import BeautifulSoup
from openai import OpenAI

USER_AGENT = "MyNewsSummarizer/1.0"

def fetch_html(url: str) -> str:
    r = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.text

def extract_text(html: str) -> str:
    doc = Document(html)
    content_html = doc.summary(html_partial=True)
    soup = BeautifulSoup(content_html, "html.parser")
    return soup.get_text(" ", strip=True)

def summarize(title: str, url: str, text: str) -> str:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    prompt = f"""
Summarize this article in 5 bullet points.
Keep it short. Paraphrase. End with: Why it matters: ...

Title: {title}
URL: {url}

Text:
{text}
""".strip()

    resp = client.responses.create(
        model="gpt-5",
        input=prompt
    )
    return resp.output_text.strip()

if __name__ == "__main__":
    url = input("Paste an article URL, then press Enter: ").strip()
    title = input("Paste the article title (or just press Enter): ").strip()

    html = fetch_html(url)
    text = extract_text(html)

    # limit text so it doesn't get huge
    text = text[:12000]

    print("\n--- SUMMARY ---\n")
    print(summarize(title or "Article", url, text))
