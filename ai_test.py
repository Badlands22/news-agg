import os
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

resp = client.responses.create(
    model="gpt-5",
    input="Say 'OK' and then give me a 1-sentence summary of what a news aggregator is.",
)

print(resp.output_text)
