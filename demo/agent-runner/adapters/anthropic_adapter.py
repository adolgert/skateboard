"""Anthropic (Claude) backend adapter. Pure function: (system, user) -> text.

Uses the Messages API with adaptive thinking and high effort, which is the
recommended configuration for code work on Opus 4.8. The API key is read from
ANTHROPIC_API_KEY in the environment (the only secret this container holds).
"""
import os

import anthropic

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-4-8")


def complete(system: str, user: str) -> dict:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    resp = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return {
        "text": text,
        "model_id": resp.model,
        "usage": {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
    }
