"""OpenAI-compatible backend adapter. Pure function:
    (system, user, model, base_url, api_key) -> dict.

Covers Google Gemini (its OpenAI-compatible endpoint) and any locally served
model (Ollama, vLLM). The endpoint and key are passed in explicitly so one
adapter serves every OpenAI-compatible backend.
"""
import requests


def complete(system: str, user: str, model: str, base_url: str, api_key: str) -> dict:
    r = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
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
        "model_id": data.get("model", model),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }
