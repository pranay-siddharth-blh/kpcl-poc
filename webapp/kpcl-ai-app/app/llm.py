"""
The one LLM call in the request path, against the internal OpenAI-compatible vLLM gateway (the same gateway
and settings contract as the WB POC). Plain prompting, so the reply is parsed defensively: a reasoning model's
</think> preamble and markdown fences are stripped before JSON parsing, missing keys are filled, and a chart
spec with a wrong key or type is repaired or dropped rather than failing the whole answer.
"""
from __future__ import annotations

import json
import re

import httpx

from app.config import settings
from app.models import LLMQueryPlan

_client: httpx.AsyncClient | None = None
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_THINK_RE = re.compile(r"^.*</think>", re.IGNORECASE | re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_STRING_FIELDS = ("sql", "answer_template", "clarification_message")
JSON_REMINDER = ("\n\nRespond with a single JSON object containing ALL of these keys, even when a value is empty, false or "
                 "null: answerable, sql, answer_template, clarification_message, chart_data.")


class LLMUnavailable(RuntimeError):
    """The gateway is not configured in .env (the app still runs every non-LLM route)."""


def init_llm_client() -> httpx.AsyncClient | None:
    global _client
    if not settings.llm_configured:
        return None
    _client = httpx.AsyncClient(
        base_url=settings.sql_verify_base_url,
        headers={"Authorization": f"Bearer {settings.sql_verify_api_key}", "Content-Type": "application/json"},
        verify=settings.ai_model_ca_bundle_path or False,
        timeout=settings.llm_timeout_seconds,
    )
    return _client


async def shutdown_llm_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _normalize_chart(data: dict) -> None:
    chart = data.get("chart_data")
    if not isinstance(chart, dict):
        data["chart_data"] = None
        return
    if "type" not in chart and "chart_type" in chart:
        chart["type"] = chart.pop("chart_type")
    if chart.get("type") not in ("bar", "line") or not chart.get("label_column") or not chart.get("value_column"):
        data["chart_data"] = None


def parse_query_plan(content: str) -> LLMQueryPlan:
    text = _THINK_RE.sub("", (content or "").strip(), count=1).strip()
    text = _FENCE_RE.sub("", text).strip()
    if not text.startswith("{"):
        m = _JSON_OBJECT_RE.search(text)
        text = m.group(0) if m else text
    data = json.loads(text, strict=False)  # raw newlines inside the SQL string are allowed
    for f in _STRING_FIELDS:
        if data.get(f) is None:
            data[f] = ""
    data["answerable"] = bool(data.get("answerable", False))
    _normalize_chart(data)
    return LLMQueryPlan.model_validate(data)


RETRY_NOTE = ("\n\nIMPORTANT: your previous reply was cut off or was not valid JSON (it listed far too many columns). "
              "Reply with ONE short JSON object. The SQL must select only the few columns the question needs (usually 2 "
              "to 6), each aliased once; never list every column of a table.")


class LLMTruncated(ValueError):
    """The model hit the token limit before finishing its JSON (it usually loops listing columns)."""


async def generate_query_plan(prompt: str, retry: bool = False) -> LLMQueryPlan:
    if _client is None:
        raise LLMUnavailable("LLM gateway not configured")
    response = await _client.post("/chat/completions", json={
        "model": settings.sql_verify_model,
        "messages": [{"role": "user", "content": prompt + JSON_REMINDER + (RETRY_NOTE if retry else "")}],
        # a retry at the same temperature would reproduce the same broken reply
        "temperature": 0.2 if retry else 0.0,
        "max_tokens": settings.llm_max_tokens,
        "chat_template_kwargs": {"enable_thinking": settings.llm_enable_thinking},
    })
    response.raise_for_status()
    choice = response.json()["choices"][0]
    content = choice["message"]["content"]
    try:
        return parse_query_plan(content)
    except (ValueError, KeyError):
        if choice.get("finish_reason") == "length":
            raise LLMTruncated("the reply hit the token limit before the JSON was complete") from None
        raise


# ---------------------------------------------------------------------------------------------- answer verification
class Verdict:
    """The reviewer's judgement of one model-written answer."""

    def __init__(self, ok: bool, problem: str = "", category: str = ""):
        self.ok, self.problem, self.category = ok, problem, category

    def __repr__(self) -> str:
        return f"Verdict(ok={self.ok}, category={self.category!r}, problem={self.problem!r})"


def parse_verdict(content: str) -> Verdict:
    text = _THINK_RE.sub("", (content or "").strip(), count=1).strip()
    text = _FENCE_RE.sub("", text).strip()
    if not text.startswith("{"):
        m = _JSON_OBJECT_RE.search(text)
        text = m.group(0) if m else text
    data = json.loads(text, strict=False)
    ok = data.get("ok")
    if isinstance(ok, str):
        ok = ok.strip().lower() in ("true", "yes", "ok", "correct")
    return Verdict(bool(ok), str(data.get("problem") or "").strip()[:500], str(data.get("category") or "").strip()[:40])


async def verify_answer(verify_prompt: str) -> Verdict:
    """Second, independent model pass: does this SQL + result + answer really answer the question?"""
    if _client is None:
        raise LLMUnavailable("LLM gateway not configured")
    response = await _client.post("/chat/completions", json={
        "model": settings.sql_verify_model,
        "messages": [{"role": "user", "content": verify_prompt}],
        "temperature": 0.0,
        "max_tokens": 300,
        "chat_template_kwargs": {"enable_thinking": settings.llm_enable_thinking},
    }, timeout=settings.kpcl_verify_timeout_seconds)
    response.raise_for_status()
    return parse_verdict(response.json()["choices"][0]["message"]["content"])
