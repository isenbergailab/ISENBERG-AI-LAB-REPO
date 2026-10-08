"""One constrained OpenRouter model; a single retry, without fallback."""

import json
import os
import time
import requests

MODEL = "qwen/qwen3.5-flash-02-23"
URL = "https://openrouter.ai/api/v1/chat/completions"
HOOKS = (
    "See what the Lab is exploring.",
    "Build, test, and discuss together.",
    "Follow the latest Lab work.",
)


def draft_hook(safe_brief: dict) -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is missing")
    schema = {"type": "object", "properties": {"hook": {"type": "string", "enum": list(HOOKS)}},
              "required": ["hook"], "additionalProperties": False}
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Write one short, natural promotional hook. Use only provided public facts. No names, quotations, private details, or new claims. Return JSON."},
            {"role": "user", "content": json.dumps(safe_brief, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"name": "campaign_hook", "strict": True, "schema": schema}},
        "provider": {"zdr": True, "data_collection": "deny", "require_parameters": True, "allow_fallbacks": False},
        "temperature": 0.5,
        "max_tokens": 120,
    }
    last_error = None
    for attempt in range(2):
        try:
            response = requests.post(URL, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                     json=payload, timeout=25)
            response.raise_for_status()
            answer = json.loads(response.json()["choices"][0]["message"]["content"])
            hook = answer["hook"]
            if hook not in HOOKS:
                raise ValueError("Invalid model hook")
            return hook
        except (requests.RequestException, KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
            last_error = error
            if attempt == 0:
                time.sleep(1)
    raise RuntimeError("Qwen draft failed twice; campaign skipped") from last_error
