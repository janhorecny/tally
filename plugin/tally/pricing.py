"""Cost estimation from token counts.

List prices in $ per million tokens (Anthropic first-party API, cached
2026-10-06). Cache writes are 1.25x input for the 5-minute TTL and 2x for the
1-hour TTL; cache reads vary by model. Override or extend in
~/.claude/tally/config.json under "pricing".

These are estimates at list price. Subscription plans (Pro/Max) are not billed
per token; the figure is still useful to compare tasks.
"""

import math
import re
from typing import Dict, Optional


def _p(inp, out, read, w5=None, w1=None):
    return {
        "input": inp,
        "output": out,
        "cache_read": read,
        "cache_write_5m": w5 if w5 is not None else inp * 1.25,
        "cache_write_1h": w1 if w1 is not None else inp * 2.0,
    }


DEFAULT_PRICES: Dict[str, dict] = {
    "claude-fable-5-1": _p(10, 50, 0.25),
    "claude-mythos-5-1": _p(10, 50, 0.25),
    "claude-fable-5": _p(10, 50, 1.0),
    "claude-mythos-5": _p(10, 50, 1.0),
    "claude-opus-5-5": _p(4, 20, 0.20),
    "claude-opus-5": _p(5, 25, 0.50),
    "claude-opus-4-8": _p(5, 25, 0.50),
    "claude-opus-4-7": _p(5, 25, 0.50),
    "claude-opus-4-6": _p(5, 25, 0.50),
    "claude-opus-4-5": _p(5, 25, 0.50),
    "claude-opus-4-1": _p(15, 75, 1.50),
    "claude-opus-4": _p(15, 75, 1.50),
    # Cache reads at $0.10: what Claude Code 2.1.29x itself bills (checked
    # against its cost-state records from 2026-10-10 on; earlier ones used $0.20).
    "claude-sonnet-5-5": _p(2, 10, 0.10),
    "claude-sonnet-5": _p(2, 10, 0.20),
    "claude-sonnet-4-6": _p(3, 15, 0.30),
    "claude-sonnet-4-5": _p(3, 15, 0.30),
    "claude-sonnet-4": _p(3, 15, 0.30),
    "claude-3-7-sonnet": _p(3, 15, 0.30),
    "claude-haiku-5-5": _p(0.10, 0.50, 0.01),
    "claude-haiku-4-5": _p(1, 5, 0.10),
    "claude-3-5-haiku": _p(0.80, 4, 0.08),
}

# OpenAI models used by Codex (developers.openai.com/api/docs/pricing,
# 2026-10-12, short context). Codex with a ChatGPT login is not billed per
# token: these are notional list-price estimates.
DEFAULT_PRICES.update({
    "gpt-6-astra": _p(10, 50, 1.0),
    "gpt-6.1-sol": _p(2, 10, 0.10),
    "gpt-6-sol": _p(2, 10, 0.20),
    "gpt-6-luna": _p(0.10, 0.50, 0.01),
    "gpt-5.6-sol": _p(4, 20, 0.40),
    "gpt-5.6-terra": _p(2, 12, 0.20),
    "gpt-5.6-luna": _p(0.20, 1.20, 0.02),
    "gpt-5.5": _p(5, 30, 0.50),
    "gpt-5.4": _p(2.5, 15, 0.25),
    "gpt-5.4-mini": _p(0.75, 4.5, 0.075),
    "gpt-5.4-nano": _p(0.20, 1.25, 0.02),
    "gpt-5.2": _p(1.75, 14, 0.175),
    "gpt-5.1": _p(1.25, 10, 0.125),
    "gpt-5": _p(1.25, 10, 0.125),
    "gpt-5-mini": _p(0.25, 2, 0.025),
})

# Prompt-size tiers: above `threshold` prompt tokens every rate is multiplied.
# Claude Haiku 5.5 is $0.10/$0.50 up to 100K prompt tokens, $0.50/$2.50 beyond.
TIERS = {
    "claude-haiku-5-5": (100_000, 5.0),
}

# Fast mode (usage.speed == "fast") is billed at 2x on the Opus models.
FAST_MULTIPLIER = 2.0

# Web search (server tool): $10 per 1,000 searches, on top of the tokens.
WEB_SEARCH_PER_1K = 10.0

_DATE_SUFFIX = re.compile(r"-\d{8}$")
_VERSION_SUFFIX = re.compile(r"-v\d+(:\d+)?$")


def normalize_model(model: Optional[str]) -> str:
    if not model:
        return ""
    m = model.strip().lower()
    m = m.split("[", 1)[0]                     # claude-opus-5-5[1m]
    m = m.split("/")[-1]                       # provider/model forms
    for prefix in ("us.anthropic.", "eu.anthropic.", "apac.anthropic.", "global.anthropic.", "anthropic."):
        if m.startswith(prefix):
            m = m[len(prefix):]
    m = m.split("@", 1)[0]                     # vertex: claude-x@20250101
    m = _VERSION_SUFFIX.sub("", m)
    m = _DATE_SUFFIX.sub("", m)
    return m


def _price(v) -> Optional[float]:
    """A price from config.json, or None if it is not a usable number ("4$", true, -1)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


class Pricer:
    def __init__(self, overrides: Optional[dict] = None):
        self.prices = dict(DEFAULT_PRICES)
        # a hand-edited "pricing" section: entries that are not usable are skipped
        for name, spec in (overrides.items() if isinstance(overrides, dict) else ()):
            if not isinstance(name, str):
                continue
            base = dict(self.prices.get(normalize_model(name), {}))
            if isinstance(spec, dict):
                for k, v in spec.items():
                    f = _price(v)
                    if f is not None:
                        base[k] = f
            if "input" in base and "output" in base:
                base.setdefault("cache_read", base["input"] * 0.1)
                base.setdefault("cache_write_5m", base["input"] * 1.25)
                base.setdefault("cache_write_1h", base["input"] * 2.0)
                self.prices[normalize_model(name)] = base
        # longest key first so "claude-opus-5-5" wins over "claude-opus-5"
        self._keys = sorted(self.prices, key=len, reverse=True)
        self._cache: Dict[str, Optional[str]] = {}

    def resolve(self, model: Optional[str]) -> Optional[str]:
        norm = normalize_model(model)
        if norm in self._cache:
            return self._cache[norm]
        found = None
        if norm in self.prices:
            found = norm
        else:
            for key in self._keys:
                if norm.startswith(key + "-") or norm == key:
                    found = key
                    break
        self._cache[norm] = found
        return found

    def row_cost(self, r) -> Optional[float]:
        """Cost of one usage row: what the agent reported (pi, Codex), else list price."""
        if r["cost"] is not None:
            return r["cost"]
        try:
            searches = r["web_search"]
        except (KeyError, IndexError):
            searches = 0
        return self.cost(r["model"], r["input"], r["output"], r["cache_read"], r["cache_write_5m"],
                         r["cache_write_1h"], r["speed"], web_search=searches)

    def cost(self, model, input_tokens=0, output_tokens=0, cache_read=0,
             cache_write_5m=0, cache_write_1h=0, speed=None, tiers=True, web_search=0) -> Optional[float]:
        """Cost of ONE API call. Pass tiers=False for summed usage of many
        calls: prompt-size tiers apply per call and cannot be judged on a sum.
        web_search: number of web searches, priced per search (no tier or
        fast-mode multiplier)."""
        key = self.resolve(model)
        if key is None:
            return None
        p = self.prices[key]
        mult = 1.0
        tier = TIERS.get(key) if tiers else None
        if tier:
            prompt_tokens = input_tokens + cache_read + cache_write_5m + cache_write_1h
            if prompt_tokens > tier[0]:
                mult *= tier[1]
        if speed == "fast":
            mult *= FAST_MULTIPLIER
        total = (
            input_tokens * p["input"]
            + output_tokens * p["output"]
            + cache_read * p["cache_read"]
            + cache_write_5m * p["cache_write_5m"]
            + cache_write_1h * p["cache_write_1h"]
        )
        return total * mult / 1_000_000 + (web_search or 0) * WEB_SEARCH_PER_1K / 1000
