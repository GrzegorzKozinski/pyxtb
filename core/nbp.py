"""NBP exchange rates (spec §8): last business day BEFORE the given date (PIT-38 rule), cached in a JSON file."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path


NBP_URL = "https://api.nbp.pl/api/exchangerates/rates/a/{ccy}/{start}/{end}/?format=json"
NBP_LOOKBACK_DAYS = 10


def fetch_series(currency: str, start: date, end: date, timeout: float = 10, retries: int = 3) -> list[tuple[date, Decimal]]:
    """[(effectiveDate, mid)] from NBP table A. Empty list on HTTP 404 (no data in the range)."""
    url = NBP_URL.format(ccy=currency.lower(), start=start.isoformat(), end=end.isoformat())
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"), parse_float=Decimal)
            return [(date.fromisoformat(r["effectiveDate"]), Decimal(str(r["mid"]))) for r in data["rates"]]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            last = e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
            last = e
        time.sleep(0.5 * (attempt + 1))
    raise ConnectionError(f"{type(last).__name__}: {last}")


class NbpRates:
    """Rate of the last business day BEFORE the given date (PIT-38 rule), cached in a JSON file.

    `get()` returns None when the rate is unavailable; the reason is appended to `errors`."""

    def __init__(self, cache_path: str | Path | None = None, fetch=fetch_series):
        self.cache_path = Path(cache_path) if cache_path else None
        self.fetch = fetch
        self.errors: list[str] = []
        self._cache: dict[str, str] = {}
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

    def get(self, currency: str, on: date) -> Decimal | None:
        key = f"{currency.upper()}|{on.isoformat()}"
        if key in self._cache:
            return Decimal(self._cache[key])
        try:
            series = self.fetch(currency, on - timedelta(days=NBP_LOOKBACK_DAYS), on - timedelta(days=1))
        except Exception as e:      # network problem: do not abort, leave the rate empty
            return self._fail(f"NBP: nie pobrano kursu {currency} dla {on} ({e})")
        earlier = [(d, mid) for d, mid in series if d < on]
        if not earlier:
            return self._fail(f"NBP: brak kursu {currency} w oknie {NBP_LOOKBACK_DAYS} dni przed {on}")
        rate = max(earlier)[1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        self._cache[key] = str(rate)
        self._save()
        return rate

    def _fail(self, msg: str) -> None:
        if msg not in self.errors:
            self.errors.append(msg)

    def _save(self) -> None:
        if self.cache_path:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(self._cache, indent=1, sort_keys=True), encoding="utf-8")
            except OSError:
                pass


class NoRates:
    """Offline stand-in (audit mode): every rate is unavailable."""
    errors: list[str] = []

    def get(self, currency: str, on: date) -> None:
        return None
