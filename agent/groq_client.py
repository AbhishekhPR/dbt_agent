import os
import json
from pathlib import Path
from dotenv import load_dotenv
from agent.logging_config import get_logger

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)
load_dotenv()

logger = get_logger(__name__)

_client = None
_client_error = None

def _get_client():
    """Lazy-initialize the Groq client on first use."""
    global _client, _client_error

    if _client is not None:
        return _client

    if _client_error is not None:
        raise RuntimeError(_client_error)

    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        _client_error = "GROQ_API_KEY is not set. Set it in .env or environment variables to use AI features."
        raise RuntimeError(_client_error)

    try:
        from groq import Groq
        _client = Groq(api_key=api_key)
        return _client
    except Exception as e:
        _client_error = f"Failed to initialize Groq client: {e}"
        raise RuntimeError(_client_error) from e


MODEL = "llama-3.3-70b-versatile"


def call_llm(prompt: str, system: str = None) -> str:
    """
    Base wrapper. Returns plain string response.
    Lazy-initializes Groq client on first call.
    Raises RuntimeError if GROQ_API_KEY is missing or invalid.
    """
    client = _get_client()
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
    Lazy-initializes Groq client on first call.
    Raises RuntimeError if GROQ_API_KEY is missing or invalid.
    """
    client = _get_client()
    messages = []

    if system:
        messages.append({"role": "system", "content": system})

    messages.append({"role": "user", "content": prompt})

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=1024,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    return json.loads(raw)