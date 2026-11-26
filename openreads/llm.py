"""Optional OpenAI-compatible reconciliation helper.

The LLM is deliberately constrained to ranking/explanation. It is not permitted
to invent identifiers or authoritative bibliographic facts.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

ENABLED = os.getenv("OPENREADS_LLM_ENABLED", "0").lower() in {"1", "true", "yes"}
BASE_URL = os.getenv("OPENREADS_LLM_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/")
API_KEY = os.getenv("OPENREADS_LLM_API_KEY", "")
MODEL = os.getenv("OPENREADS_LLM_MODEL", "")
TIMEOUT = float(os.getenv("OPENREADS_LLM_TIMEOUT", "45"))

SYSTEM = """You are a bibliographic reconciliation engine. Use only the supplied evidence.
Never invent ISBNs, ASINs, OCLC/LCCN values, provider IDs, publication dates,
publishers, narrators, or other bibliographic facts. Your task is to compare
candidate records and return a structured recommendation. If evidence is
ambiguous, say review_required=true. Output JSON only."""


def reconcile(subject: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not ENABLED or not MODEL:
        return {"enabled": False, "review_required": True, "reason": "LLM disabled"}
    payload = {
        "model": MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "subject": subject,
                        "candidates": candidates,
                        "required_schema": {
                            "selected_index": "integer|null",
                            "confidence": "0..1",
                            "review_required": "boolean",
                            "reason": "string",
                            "evidence_used": ["string"],
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
    # Defensive output validation: the model may select a candidate, but it
    # may not add facts that were not supplied.
    idx = result.get("selected_index")
    if idx is not None and (not isinstance(idx, int) or idx < 0 or idx >= len(candidates)):
        result["selected_index"] = None
        result["review_required"] = True
        result["reason"] = "Model returned an invalid candidate index"
    return result


def suggest_observations(entity: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose only among observed values; never allow the model to author metadata."""
    if not ENABLED or not MODEL:
        return {"enabled": False, "review_required": True, "suggestions": [], "reason": "LLM disabled"}
    candidates = [
        {
            "id": x["id"],
            "field": x["field"],
            "source": x["source"],
            "value": x.get("value"),
            "confidence": x.get("confidence", 1.0),
        }
        for x in observations
    ]
    payload = {
        "model": MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": SYSTEM + " You may select ONLY observation IDs that appear in the supplied candidates. Never return a new metadata value.",
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "entity": entity,
                        "observations": candidates,
                        "required_schema": {
                            "suggestions": [{"field": "string", "observation_id": "existing id", "confidence": "0..1", "reason": "string"}],
                            "review_required": "boolean",
                            "summary": "string",
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
    valid = {x["id"]: x for x in candidates}
    safe = []
    for suggestion in result.get("suggestions") or []:
        oid = suggestion.get("observation_id")
        hit = valid.get(oid)
        if not hit or suggestion.get("field") != hit["field"]:
            continue
        safe.append({
            "field": hit["field"],
            "observation_id": oid,
            "source": hit["source"],
            "value": hit["value"],
            "confidence": max(0.0, min(1.0, float(suggestion.get("confidence", 0)))),
            "reason": str(suggestion.get("reason") or "")[:1000],
        })
    return {
        "enabled": True,
        "suggestions": safe,
        "review_required": bool(result.get("review_required", True)),
        "summary": str(result.get("summary") or "")[:2000],
    }
