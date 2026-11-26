from __future__ import annotations

import json
import logging
from typing import Any, NamedTuple

import httpx

from ..config import settings

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(settings.llm_enabled and settings.llm_base_url and settings.llm_model)


def model_name() -> str | None:
    """For audit payloads: which model produced a verdict (None when disabled)."""
    return settings.llm_model or None if enabled() else None


def chat_json(system: str, user: str, *, max_tokens: int = 4000, timeout: float | None = None) -> dict[str, Any] | None:
    """One OpenAI-compatible chat completion, parsed as strict JSON.

    Returns None on ANY failure - disabled, unreachable, non-2xx, unparsable response -
    and never raises. Callers that use this for a safety decision (kids-tag
    classification) must treat None as "abstain", not as "assume yes" or "assume no".

    max_tokens defaults to 4000, not a small number like 400: the deployed model
    (BOOKDOE_LLM_MODEL=primary, the same local reasoning model universe's
    research-worker uses) spends its budget on hidden chain-of-thought before
    writing the JSON answer, in a separate `reasoning_content` field the content
    field never includes. A tight budget truncates the response before any JSON
    is ever written, so _extract_json finds no `{`/`}` and every call silently
    abstains - confirmed live, 2026-10-11, against the kids classifier: 400 tokens
    abstained on every book; a direct 4000-token call for the same prompt returned
    a clean, complete {"verdict":...} on the first try.
    """
    return chat_messages([{"role": "system", "content": system}, {"role": "user", "content": user}],
                         max_tokens=max_tokens, timeout=timeout).data


class LLMResult(NamedTuple):
    """`data` is the parsed JSON object or None; `status` says why not: 'ok', 'disabled',
    'unreachable' (transport error, timeout or non-2xx) or 'unparsable' (a reply with no JSON object)."""
    data: dict[str, Any] | None
    status: str


def chat_messages(messages: list[dict[str, str]], *, max_tokens: int = 4000, timeout: float | None = None,
                  thinking: bool | None = None) -> LLMResult:
    """The multi-turn form of chat_json (Discover chat). Same endpoint, same lenient JSON
    extraction, never raises; the status separates "the model is down" from "it answered
    badly", which callers treat differently (retry once vs. fail at once).

    thinking=False asks a reasoning model (the deployed `primary`) to answer without its long hidden
    reasoning pass (vLLM `chat_template_kwargs.enable_thinking`). Measured 2026-10-15: a book-recommendation
    reply took 10 s that way and more than 170 s with thinking on, which is why Discover chat timed out.
    None leaves the server default, so other callers are unchanged."""
    if not enabled():
        return LLMResult(None, "disabled")
    try:
        # `timeout` lets one caller (the kids classifier) cap its own calls without
        # changing the default every other caller gets.
        with httpx.Client(base_url=settings.llm_base_url, timeout=settings.llm_timeout if timeout is None else timeout) as client:
            r = client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {},
                json={
                    "model": settings.llm_model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.1,
                    # Not every OpenAI-compatible server honours this; the system
                    # prompt also demands JSON-only, and the parser below is lenient
                    # about a fenced code block around it.
                    "response_format": {"type": "json_object"},
                    **({"chat_template_kwargs": {"enable_thinking": bool(thinking)}} if thinking is not None else {}),
                },
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return LLMResult(None, "unreachable")
    data = _extract_json(content)
    return LLMResult(data, "ok" if data is not None else "unparsable")


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
