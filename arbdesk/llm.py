"""AI analyst over an OpenAI-compatible API (DeepSeek or Qwen).

Only public market data and the risk limits are sent to the model — never API
keys, balances, order history or anything from `.env`.
"""
from __future__ import annotations

import json
import logging

from openai import OpenAI

from .config import LLM_PRESETS, Settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a cautious crypto trading analyst for a very small account.
You receive automatically detected opportunities (already net of taker fees as noted)
and a market snapshot. Your job:
1. Judge which opportunities are realistically executable for a small retail account
   (consider minimum order sizes, withdrawal/transfer delays, slippage, funding flips,
   exchange counterparty risk, and that quotes may be stale).
2. Optionally propose at most 2 concrete actions. Each action must stay within the limits
   given and only use the allowed symbols and exchanges. Prefer doing nothing over a weak trade.
3. Never suggest leverage above 1x, borrowing, or transferring funds.

Answer ONLY with JSON of this shape:
{
  "market_view": "short paragraph",
  "ranked": [{"ref": <index in opportunities>, "verdict": "actionable|watch|ignore", "reason": "..."}],
  "proposals": [{
      "title": "...",
      "rationale": "...",
      "confidence": 0.0-1.0,
      "legs": [{"exchange": "...", "symbol": "BASE/QUOTE", "market": "spot|swap",
                "side": "buy|sell", "usd": number}],
      "exit_plan": "..."
  }],
  "risks": ["..."]
}
Write the text fields in Chinese."""


class Analyst:
    def __init__(self, settings: Settings):
        s = settings.secrets
        preset = LLM_PRESETS.get(s.llm_provider, LLM_PRESETS["deepseek"])
        self.provider = s.llm_provider
        self.model = s.llm_model or preset["model"]
        self.settings = settings
        self.client = None
        if s.llm_api_key:
            self.client = OpenAI(api_key=s.llm_api_key, base_url=s.llm_base_url or preset["base_url"],
                                 timeout=90, max_retries=2)

    @property
    def available(self) -> bool:
        return self.client is not None and self.settings.llm.enabled

    def build_payload(self, opportunities: list[dict], overview: list[dict]) -> dict:
        r = self.settings.risk
        return {
            "limits": {
                "max_capital_usd": r.max_capital_usd,
                "max_per_trade_usd": r.max_per_trade_usd,
                "allowed_symbols": self.settings.allowed_symbols,
                "allowed_exchanges": self.settings.scan.exchanges,
            },
            "opportunities": [{"ref": i, **o} for i, o in enumerate(opportunities[:30])],
            "market": overview,
        }

    def analyze(self, opportunities: list[dict], overview: list[dict]) -> dict:
        if not self.available:
            raise RuntimeError("LLM not configured (set LLM_API_KEY in .env)")
        payload = self.build_payload(opportunities, overview)
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=self.settings.llm.temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        return parse_json(resp.choices[0].message.content or "")


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            return {"market_view": text[:2000], "ranked": [], "proposals": [], "risks": ["unparseable"]}
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        return {"market_view": str(data)[:2000], "ranked": [], "proposals": [], "risks": []}
    data.setdefault("proposals", [])
    data.setdefault("ranked", [])
    data.setdefault("risks", [])
    return data
