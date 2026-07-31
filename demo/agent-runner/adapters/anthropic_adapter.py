"""Anthropic (Claude) backend adapter. Pure function: (system, user, model) -> dict.

Model-aware: Opus 4.x / Sonnet 5 / Sonnet 4.6 / Fable accept adaptive thinking
and the effort parameter; Haiku 4.5 and older reject them (400), so for those we
send a plain request. The API key is read from ANTHROPIC_API_KEY in the env.
"""
import anthropic

# model-id substrings whose families accept adaptive thinking + output_config.effort
ADAPTIVE_FAMILIES = ("opus-4", "sonnet-5", "sonnet-4-6", "fable")


def _adaptive(model: str) -> bool:
    return any(f in model for f in ADAPTIVE_FAMILIES)


def complete(system: str, user: str, model: str) -> dict:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    kw = dict(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if _adaptive(model):
        kw["thinking"] = {"type": "adaptive"}
        kw["output_config"] = {"effort": "high"}
    resp = client.messages.create(**kw)
    text = "".join(b.text for b in resp.content if b.type == "text")
    return {
        "text": text,
        "model_id": resp.model,
        "usage": {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
    }
