import os
import json
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

MODEL = "llama-3.3-70b-versatile"

def call_llm(prompt: str, system: str = None) -> str:
    """
    Base wrapper. Returns plain string response.
    """
    messages = []
    
    if system:
        messages.append({"role": "system", "content": system})
    
    messages.append({"role": "user", "content": prompt})

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=1024,
    )
    return response.choices[0].message.content


def call_llm_json(prompt: str, system: str = None) -> dict:
    """
    Forces JSON output using Groq's native JSON mode.
    Returns a parsed dict directly — no manual parsing needed.
    """
    messages = []

    if system:
        messages.append({"role": "system", "content": system})

    messages.append({"role": "user", "content": prompt})

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=1024,
        response_format={"type": "json_object"},  # native JSON mode
    )
    
    raw = response.choices[0].message.content
    return json.loads(raw)