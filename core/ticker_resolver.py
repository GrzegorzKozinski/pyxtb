"""Automatic XTB symbol -> sheet ticker (replaces the ticker seed file).

For every symbol missing from the optional `config/tickers.csv` override:
  1. candidates come from suffix rules (TTWO.US -> TTWO, IS3N.DE -> IS3N.DE, EQQQ.UK -> EQQQ.L, ...),
     plus a Yahoo search by the export's instrument name when the rule candidate does not verify;
  2. a candidate is accepted when the trade prices from the export (comment `OPEN BUY 2 @ 236.94`) fall
     inside the day's low-high range on Yahoo; the same response gives exchange, currency and type;
  3. the result goes to `cache/tickers_cache.json`, so each symbol is asked once.
Only urllib is used (no extra dependency). The sheet's ticker convention (Google Finance style) is kept:
NASDAQ:TTWO, NYSE:V, WSE:KRU, LON:EQQQ, GER:IS3N:EUR (ETF on Xetra), ETR:SAP (stock on Xetra).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from core.reader import parse_trade_comment
from core.tickers import TickerDict

# XTB suffix -> (Yahoo suffix, sheet exchange prefix, default currency)
SUFFIX_RULES = {
    "PL": (".WA", "WSE", "PLN"),
    "UK": (".L", "LON", "GBP"),
    "DE": (".DE", "GER", "EUR"),
    "FR": (".PA", "EPA", "EUR"),
    "NL": (".AS", "AMS", "EUR"),
    "FI": (".HE", "HEL", "EUR"),
    "IT": (".MI", "BIT", "EUR"),
    "ES": (".MC", "BME", "EUR"),
    "CH": (".SW", "SWX", "CHF"),
    "DK": (".CO", "CPH", "DKK"),
    "SE": (".ST", "STO", "SEK"),
}
# Yahoo exchangeName -> sheet prefix for US listings (.US has no suffix on Yahoo)
US_EXCHANGES = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NYQ": "NYSE",
                "PCX": "NYSEARCA", "ASE": "NYSEAMERICAN", "BTS": "BATS"}
CURRENCY_ALIASES = {"GBp": "GBP", "GBX": "GBP"}

MAX_EVIDENCE = 5            # most recent trade prices used for verification
PRICE_TOLERANCE = Decimal("0.03")
MIN_MATCH_RATIO = 0.6
MAX_SEARCH_CANDIDATES = 3
MAX_FAILURES = 3            # consecutive network failures before Yahoo is skipped for the rest of the run


# ====================================================================== Yahoo client

@dataclass
class ChartData:
    symbol: str
    currency: str                       # raw Yahoo currency, e.g. USD, EUR, GBp
    exchange: str                       # exchangeName, e.g. NMS, NYQ, GER, LSE
    instrument_type: str                # EQUITY / ETF / ...
    name: str
    days: list[tuple[date, Decimal, Decimal]] = field(default_factory=list)    # (day, low, high)


class YahooClient:
    """Minimal Yahoo Finance access. `chart()` returns None for an unknown symbol, raises ConnectionError on network problems."""
    CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=1d"
    SEARCH = "https://query2.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=8&newsCount=0"

    def __init__(self, timeout: float = 10, retries: int = 2, pause: float = 0.3):
        self.timeout, self.retries, self.pause = timeout, retries, pause

    def _get(self, url: str):
        time.sleep(self.pause)
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                last = e
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = e
            time.sleep(0.5 * (attempt + 1))
        raise ConnectionError(f"{type(last).__name__}: {last}")

    def chart(self, symbol: str, start: date, end: date) -> ChartData | None:
        p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
        p2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
        data = self._get(self.CHART.format(sym=urllib.parse.quote(symbol), p1=p1, p2=p2))
        results = ((data or {}).get("chart") or {}).get("result")
        if not results:
            return None
        r = results[0]
        meta = r.get("meta") or {}
        quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
        offset = int(meta.get("gmtoffset") or 0)
        days = []
        for t, lo, hi in zip(r.get("timestamp") or [], quote.get("low") or [], quote.get("high") or []):
            if lo is not None and hi is not None:
                day = datetime.fromtimestamp(t + offset, timezone.utc).date()
                days.append((day, Decimal(str(lo)), Decimal(str(hi))))
        return ChartData(symbol, meta.get("currency") or "", meta.get("exchangeName") or "",
                         meta.get("instrumentType") or "", meta.get("longName") or meta.get("shortName") or "", days)

    def search(self, query: str) -> list[str]:
        data = self._get(self.SEARCH.format(q=urllib.parse.quote(query)))
        return [q["symbol"] for q in (data or {}).get("quotes", []) if q.get("symbol")]


# ====================================================================== resolver

def split_symbol(symbol: str) -> tuple[str, str]:
    base, _, suffix = symbol.strip().upper().rpartition(".")
    return (base, suffix) if base else (symbol.strip().upper(), "")


def verify_prices(chart: ChartData, evidence: list[tuple[date, Decimal]]) -> tuple[int, int]:
    """(matched, checked): trade prices that fall inside Yahoo's low-high range of the day (+-1 day)."""
    factors = (Decimal(1), Decimal(100)) if chart.currency == "GBp" else (Decimal(1),)   # pence vs pounds
    matched = checked = 0
    for day, price in evidence:
        near = [(lo, hi) for d, lo, hi in chart.days if abs((d - day).days) <= 1]
        if not near:
            continue
        low, high = min(lo for lo, _ in near), max(hi for _, hi in near)
        checked += 1
        if any(low * (1 - PRICE_TOLERANCE) <= price * f <= high * (1 + PRICE_TOLERANCE) for f in factors):
            matched += 1
    return matched, checked


class TickerResolver:
    """`client=None` = offline: only the cache is used. `notes` feed the report."""

    def __init__(self, cache_path: str | Path | None = None, client=None):
        self.cache_path = Path(cache_path) if cache_path else None
        self.client = client
        self.notes: list[str] = []
        self._cache: dict[str, dict] = {}
        self._failures = 0
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

    # ---------- public ----------

    def resolve(self, symbol: str, export_name: str, evidence: list[tuple[date, Decimal]]) -> dict | None:
        """Dictionary entry (keys as in tickers.csv) or None when the symbol cannot be resolved."""
        sym = symbol.strip().upper()
        if sym in self._cache:
            return self._entry_from_cache(sym)
        if self.client is None:
            self.notes.append(f"{sym}: brak w cache, tryb offline – nie rozpoznano")
            return None
        if self._failures >= MAX_FAILURES:
            self.notes.append(f"{sym}: Yahoo niedostępne (kolejne błędy sieci) – nie rozpoznano")
            return None

        ev = sorted({d: p for d, p in evidence}.items(), reverse=True)[:MAX_EVIDENCE]
        try:
            return self._resolve_online(sym, export_name, ev)
        except ConnectionError as e:
            self._failures += 1
            self.notes.append(f"{sym}: błąd sieci Yahoo ({e}) – nie rozpoznano")
            return None

    # ---------- internals ----------

    def _resolve_online(self, sym: str, export_name: str, ev: list[tuple[date, Decimal]]) -> dict | None:
        base, suffix = split_symbol(sym)
        if suffix != "US" and suffix not in SUFFIX_RULES:
            self.notes.append(f"{sym}: nieobsługiwany sufiks .{suffix} – dopisz do tickers.csv")
            return None
        start = (min(d for d, _ in ev) if ev else date.today() - timedelta(days=30)) - timedelta(days=5)
        end = (max(d for d, _ in ev) if ev else date.today()) + timedelta(days=5)

        tried: list[str] = []
        best: tuple[str, ChartData, int, int] | None = None
        for ysym in self._candidates(base, suffix, export_name):
            chart = self.client.chart(ysym, start, end)
            self._failures = 0
            if chart is None:
                continue
            matched, checked = verify_prices(chart, ev)
            if checked and matched / checked < MIN_MATCH_RATIO:
                tried.append(f"{ysym} ({matched}/{checked})")
                continue
            best = (ysym, chart, matched, checked)
            break

        if best is None:
            why = f"ceny z transakcji nie pasują: {', '.join(tried)}" if tried else "Yahoo nie zna symbolu"
            self.notes.append(f"{sym}: nie rozpoznano ({why}) – dopisz do tickers.csv")
            return None

        ysym, chart, matched, checked = best
        entry = self._build_entry(sym, ysym, suffix, chart, export_name)
        if entry is None:
            self.notes.append(f"{sym}: Yahoo {ysym} – nieznana giełda „{chart.exchange}” – dopisz do tickers.csv")
            return None
        proof = f"cena potwierdzona {matched}/{checked}" if checked else "bez weryfikacji ceną (brak notowań z dni transakcji)"
        self.notes.append(f"{sym} → {entry['ticker']} ({entry['instrument_currency']}), Yahoo {ysym}, {proof}")
        self._cache[sym] = {**entry, "yahoo": ysym, "verified": bool(checked), "checked": date.today().isoformat()}
        self._save()
        return entry

    def _candidates(self, base, suffix, export_name):
        """Rule candidate first; Yahoo search by name only after the rule candidate failed."""
        seen: set[str] = set()
        first = base if suffix == "US" else base + SUFFIX_RULES[suffix][0]
        seen.add(first)
        yield first
        if not export_name:
            return
        ysuffix = "" if suffix == "US" else SUFFIX_RULES[suffix][0]
        extra = 0
        for cand in self.client.search(export_name):
            ok = ("." not in cand) if suffix == "US" else cand.upper().endswith(ysuffix)
            if ok and cand not in seen and extra < MAX_SEARCH_CANDIDATES:
                seen.add(cand)
                extra += 1
                yield cand

    @staticmethod
    def _build_entry(sym: str, ysym: str, suffix: str, chart: ChartData, export_name: str) -> dict | None:
        ccy = CURRENCY_ALIASES.get(chart.currency, chart.currency).upper()
        if suffix == "US":
            prefix = US_EXCHANGES.get(chart.exchange)
            if prefix is None:
                return None
            core, ccy = ysym, ccy or "USD"
            ticker = f"{prefix}:{core}"
        else:
            ysuffix, prefix, default_ccy = SUFFIX_RULES[suffix]
            core = ysym[:-len(ysuffix)] if ysym.upper().endswith(ysuffix) else ysym
            ccy = ccy or default_ccy
            if suffix == "DE":      # sheet convention: ETF -> GER:SYM:EUR, stock -> ETR:SYM
                ticker = f"GER:{core}:{ccy}" if chart.instrument_type == "ETF" else f"ETR:{core}"
            else:
                ticker = f"{prefix}:{core}"
        return {"xtb_symbol": sym, "ticker": ticker, "name": export_name or chart.name or sym,
                "asset_class": "Akcje polskie" if suffix == "PL" else "Akcje zagraniczne", "instrument_currency": ccy}

    def _entry_from_cache(self, sym: str) -> dict:
        c = self._cache[sym]
        return {"xtb_symbol": sym, "ticker": c["ticker"], "name": c.get("name", ""), "asset_class": c["asset_class"],
                "instrument_currency": c["instrument_currency"]}

    def _save(self) -> None:
        if self.cache_path:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(self._cache, indent=1, sort_keys=True, ensure_ascii=False),
                                           encoding="utf-8")
            except OSError:
                pass


# ====================================================================== integration

def collect_symbols(accounts) -> dict[str, tuple[str, list[tuple[date, Decimal]]]]:
    """symbol -> (instrument name from the export, [(date, price from the trade comment)])."""
    out: dict[str, tuple[str, list]] = defaultdict(lambda: ("", []))
    for data in accounts.values():
        for op in data.operations:
            if op.kind not in ("trade", "dividend") or not op.ticker:
                continue
            sym = op.ticker.strip().upper()
            name, prices = out[sym]
            out[sym] = (name or op.instrument, prices)
            if op.kind == "trade":
                tc = parse_trade_comment(op.comment)
                if tc is not None and tc.supported:
                    prices.append((op.date, tc.price))
    return dict(out)


def resolve_missing(accounts, tickers: TickerDict, resolver: TickerResolver) -> None:
    """Adds resolved entries to `tickers`; symbols already in the dictionary (tickers.csv override) are left alone."""
    for sym, (name, evidence) in sorted(collect_symbols(accounts).items()):
        if not tickers.has(sym):
            entry = resolver.resolve(sym, name, evidence)
            if entry:
                tickers.add(entry)
