"""llmclient.chat_json: the request budget and the lenient JSON extraction.

The deployed model is a local reasoning model that spends tokens on hidden
chain-of-thought before writing its JSON answer (in a separate reasoning_content
field, never in content). A max_tokens budget too small for that truncates the
response before any JSON is ever written, and every call silently abstains -
this is exactly what shipped and was caught live, 2026-10-11, against the kids
classifier (see llmclient.chat_json's docstring). These tests pin the real
default and the extraction behaviour against a fake transport so the request
shape never regresses without a loud failure.
"""
import dataclasses
import json

import httpx

from app.services import llmclient

_RealClient = httpx.Client   # captured before any test patches httpx.Client, to avoid infinite recursion


def _client(handler):
    return _RealClient(transport=httpx.MockTransport(handler), base_url="http://fake-llm")


def _enable(monkeypatch):
    monkeypatch.setattr(llmclient, "settings", dataclasses.replace(
        llmclient.settings, llm_enabled=True, llm_base_url="http://fake-llm",
        llm_model="primary", llm_api_key="k", llm_timeout=5,
    ))


def test_default_max_tokens_is_generous_not_400(monkeypatch):
    _enable(monkeypatch)

    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"verdict":"kids","confidence":1.0,"reason":"x"}'}}]})

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _client(handler))
    result = llmclient.chat_json("system prompt", "user prompt")
    assert captured["body"]["max_tokens"] >= 2000, (
        "max_tokens is too small for a reasoning model's hidden chain-of-thought; "
        "see llmclient.chat_json's docstring for the live incident this guards against"
    )
    assert result == {"verdict": "kids", "confidence": 1.0, "reason": "x"}


def test_a_response_with_reasoning_before_the_json_answer_still_parses(monkeypatch):
    """The reasoning model's `content` field is sometimes prefixed with leftover prose
    even when response_format=json_object is honoured by the gateway - the lenient
    brace-finding in _extract_json must still pull out the real answer."""
    _enable(monkeypatch)

    def handler(request):
        content = 'Here is my answer:\n{"verdict":"not_kids","confidence":0.88,"reason":"mature themes"}\n'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _client(handler))
    result = llmclient.chat_json("system prompt", "user prompt")
    assert result == {"verdict": "not_kids", "confidence": 0.88, "reason": "mature themes"}


def test_a_truncated_response_with_no_json_abstains(monkeypatch):
    """What shipped: a tight max_tokens cuts the response off mid chain-of-thought,
    before any { is ever written. chat_json must abstain (None), never raise and
    never fabricate a verdict."""
    _enable(monkeypatch)

    def handler(request):
        content = "Let me think about this book carefully. First, I'll consider the themes involved and whether"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _client(handler))
    assert llmclient.chat_json("system prompt", "user prompt") is None
