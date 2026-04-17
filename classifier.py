import json
from openai import OpenAI
from config import OPENAI_API_KEY, OPENAI_MODEL, CLASSIFIER_SYSTEM_PROMPT

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def classify(text: str) -> dict:
    client = _get_client()
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        temperature=0,
        messages=[
            {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
    return {
        "match": bool(data.get("match", False)),
        "confidence": float(data.get("confidence", 0.0) or 0.0),
        "reason": str(data.get("reason", "")),
    }
