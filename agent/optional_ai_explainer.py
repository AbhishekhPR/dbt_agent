"""
Optional AI explainer helper (formerly groq_client).

This file is intentionally isolated from the core product flow.
By default it is disabled — the core deterministic engine must NOT
call any external LLMs. To enable, set `ENABLE_AI_EXPLAINER=true`
and provide a GROQ_API_KEY in a secure environment. Even when
enabled, callers MUST sanitize inputs: do not send raw customer
SQL, logs, or sensitive data without explicit approval.

This module is provided for teams that opt-in to external AI
explanations. It is NOT used by Relium's core detection paths.
"""
import os
import json
from pathlib import Path

ENABLED = os.getenv("ENABLE_AI_EXPLAINER", "false").lower() in ("1", "true", "yes")

if ENABLED:
    try:
        from groq import Groq
    except Exception:  # pragma: no cover - optional dependency
        Groq = None

    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path)
    except Exception:
        pass

    if Groq is not None:
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

        def call_llm(prompt: str, system: str = None) -> str:
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
    else:
        def call_llm(*args, **kwargs):
            raise RuntimeError("AI explainer not available: optional dependency missing or Groq not installed")

        def call_llm_json(*args, **kwargs):
            raise RuntimeError("AI explainer not available: optional dependency missing or Groq not installed")
else:
    def call_llm(*args, **kwargs):
        raise RuntimeError("AI explainer disabled by default. Set ENABLE_AI_EXPLAINER=true to enable.")

    def call_llm_json(*args, **kwargs):
        raise RuntimeError("AI explainer disabled by default. Set ENABLE_AI_EXPLAINER=true to enable.")
