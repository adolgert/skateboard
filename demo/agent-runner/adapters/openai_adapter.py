"""OpenAI-compatible backend adapter. Pure function: (system, user) -> text.

Covers Google Gemini (OpenAI-compatible endpoint) and any locally served model
(Ollama, vLLM, llama.cpp) that speaks /v1/chat/completions. Swapping backend is
a matter of setting OPENAI_BASE_URL + AGENT_MODEL -- no other component changes.

  Ollama example:  OPENAI_BASE_URL=http://host.docker.internal:11434/v1
                   AGENT_MODEL=qwen2.5:14b
  Gemini example:  OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
                   AGENT_MODEL=gemini-2.0-flash
"""
import os

import requests

BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1")
MODEL = os.environ.get("AGENT_MODEL", "qwen2.5:14b")
API_KEY = os.environ.get("OPENAI_API_KEY", "not-needed")


def complete(system: str, user: str) -> dict:
    r = requests.post(
        f"{BASE_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "stream": False,
        },
        timeout=600,
    )
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return {
        "text": text,
        "model_id": data.get("model", MODEL),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }
