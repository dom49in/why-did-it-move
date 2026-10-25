"""Meterless Nightshift client (OpenAI-compatible /chat/completions). All model calls go through here.

The model that actually handled each request is returned so it can be logged: meterless/auto may
route to non-NVIDIA models, and the log is how we show where NVIDIA contributed.

Two modes (see mode()):
  nightshift     the real thing. Used whenever METERLESS_BASE_URL and METERLESS_RUNTIME_TOKEN are set.
  offline_rules  ONLY when SAMPLE_MODE=true and no Nightshift credentials are set. Decisions come from
                 app/offline.py, a deterministic rule set, and every log line says model="offline-rules".
                 It exists so a judge can run the project from a clean clone with no credentials. It is never
                 used against Zetaris, and it never presents itself as a model.
"""
from __future__ import annotations

import json
import re
import time

import requests

from . import config
from .errors import LLMError

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
OFFLINE_MODEL = "offline-rules"


def mode() -> str:
    forced = (config.get("LLM_MODE", "auto") or "auto").lower()
    configured = bool(config.get("METERLESS_BASE_URL") and config.get("METERLESS_RUNTIME_TOKEN"))
    if forced == "offline_rules":
        if not config.sample_mode():
            raise LLMError("LLM_MODE=offline_rules is only allowed with SAMPLE_MODE=true.", stage="llm")
        return "offline_rules"
    if forced == "nightshift" or configured:
        return "nightshift"
    if config.sample_mode():
        return "offline_rules"
    return "nightshift"   # will fail loudly in chat() with a clear message


def chat(messages: list[dict], temperature: float = 0.2, max_tokens: int | None = None,
         timeout: float | None = None, max_attempts: int = 2) -> dict:
    base = config.get("METERLESS_BASE_URL")
    token = config.get("METERLESS_RUNTIME_TOKEN")
    if not base or not token:
        raise LLMError("METERLESS_BASE_URL and METERLESS_RUNTIME_TOKEN must be set (Nightshift Runtime URL and token).",
                       stage="llm")
    requested = config.get("METERLESS_MODEL", "meterless/auto")
    payload = {"model": requested, "messages": messages, "temperature": temperature}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "meterless-app": config.get("METERLESS_APP", "why-did-it-move"),
        "meterless-scope": "user",
    }
    url = base.rstrip("/") + "/chat/completions"
    timeout = timeout or config.get_float("LLM_TIMEOUT", 60.0)
    backoff = config.get_float("LLM_BACKOFF_SECONDS", 1.0)
    last_error = ""
    for attempt in range(max_attempts):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            elif resp.status_code >= 400:
                raise LLMError(f"Nightshift returned HTTP {resp.status_code}: {resp.text[:300]}", stage="llm",
                               details={"http_status": resp.status_code})
            else:
                try:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise LLMError(f"Unexpected Nightshift response shape: {exc}", stage="llm") from exc
                if not isinstance(content, str) or not content.strip():
                    raise LLMError("Nightshift returned an empty completion.", stage="llm")
                return {"content": content, "model": data.get("model") or requested, "retries": attempt,
                        "usage": data.get("usage") if isinstance(data.get("usage"), dict) else None}
        if attempt + 1 < max_attempts:
            time.sleep(backoff * (attempt + 1))
    raise LLMError(f"Nightshift unreachable after {max_attempts} attempts: {last_error}", stage="llm",
                   details={"attempts": max_attempts})


def extract_json(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except ValueError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("no JSON object found")


def _valid(obj, required_keys) -> bool:
    return isinstance(obj, dict) and all(k in obj for k in required_keys)


def chat_json(messages: list[dict], required_keys: tuple = (), **kwargs) -> tuple[dict, dict]:
    """Call the model and parse a JSON object. One corrective retry, then a structured LLMError.

    Returns (object, meta) with meta = {"model", "retries", "usage"}.
    """
    if mode() == "offline_rules":
        from . import offline
        obj = offline.respond(messages)
        if not _valid(obj, required_keys):
            raise LLMError(f"Offline rules produced no {list(required_keys)}.", stage="llm")
        return obj, {"model": OFFLINE_MODEL, "retries": 0, "usage": None}
    first = chat(messages, **kwargs)
    retries = first["retries"]
    try:
        obj = extract_json(first["content"])
        if _valid(obj, required_keys):
            return obj, {"model": first["model"], "retries": retries, "usage": first.get("usage")}
    except ValueError:
        pass
    repair = messages + [
        {"role": "assistant", "content": first["content"][:2000]},
        {"role": "user", "content": "That was not valid JSON with the required keys "
                                     f"{list(required_keys)}. Reply with ONLY the JSON object."},
    ]
    second = chat(repair, **kwargs)
    retries += 1 + second["retries"]
    try:
        obj = extract_json(second["content"])
    except ValueError as exc:
        raise LLMError("Model did not return valid JSON after a corrective retry.", stage="llm") from exc
    if not _valid(obj, required_keys):
        raise LLMError(f"Model JSON is missing required keys {list(required_keys)}.", stage="llm")
    return obj, {"model": second["model"], "retries": retries, "usage": second.get("usage")}
