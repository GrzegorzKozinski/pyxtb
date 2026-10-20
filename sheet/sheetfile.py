"""The existing Transakcje tab exported as CSV (portfolio-transakcje.csv).

Optional input: earlier positions for the FIFO check when --from is used, and the data of --audit."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from core.reader import ACCOUNT_ORDER, parse_amount


@dataclass(frozen=True)
class SheetEntry:
    """One existing row of the Transakcje tab (only what FIFO validation and the audit need)."""
    account: str
    date: date
    ticker: str
    currency: str
    asset_class: str
    kind: str                       # Rodzaj transakcji
    qty: Decimal
    total: Decimal | None = None    # Total PLN (column M)


# key -> (header name, positional fallback = column index in spec §4.1)
_SHEET_COLUMNS = {"account": ("konto", 0), "date": ("data", 1), "ticker": ("ticker", 2), "currency": ("waluta", 3),
                  "asset_class": ("klasa aktywów", 5), "kind": ("rodzaj transakcji", 6), "qty": ("liczba", 7),
                  "total": ("total pln", 12)}
_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%m/%d/%Y")


def parse_sheet_date(value: str) -> date | None:
    s = (value or "").strip().strip('"')
    s = s.split(" ")[0] if s else s
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_sheet_number(value: str) -> Decimal | None:
    """'1 234,56 zł' / '0,5' / '-3.25' -> Decimal."""
    s = re.sub(r"[^\d,.\-]", "", (value or "").replace("\u00a0", " "))
    return parse_amount(s) if s not in ("", "-") else None


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1250"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_sheet(path: str | Path) -> tuple[list[SheetEntry], list[str]]:
    """Reads the sheet export (delimiter auto-detected). Returns (entries, warnings)."""
    text = _decode(Path(path).read_bytes())
    if not text.strip():
        return [], [f"{path}: pusty plik"]
    delim = max((",", ";", "\t"), key=text.splitlines()[0].count)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    header = [h.strip().lower() for h in rows[0]]

    warns: list[str] = []
    idx: dict[str, int] = {}
    for key, (name, default) in _SHEET_COLUMNS.items():
        if name in header:
            idx[key] = header.index(name)
        else:
            idx[key] = default
            if key != "total":
                warns.append(f"{path}: brak nagłówka „{name}” – przyjęto kolumnę {chr(65 + default)}")

    entries: list[SheetEntry] = []
    bad_date = bad_qty = 0
    for r in rows[1:]:
        def get(key: str) -> str:
            return r[idx[key]].strip() if idx[key] < len(r) else ""

        if get("account") not in ACCOUNT_ORDER:
            continue
        d = parse_sheet_date(get("date"))
        if d is None:
            bad_date += 1       # also unusable for the start date
            continue
        qty = parse_sheet_number(get("qty"))
        if qty is None:
            bad_qty += 1
            qty = Decimal(0)
        entries.append(SheetEntry(get("account"), d, get("ticker"), get("currency"), get("asset_class"),
                                  get("kind"), qty, parse_sheet_number(get("total"))))
    if bad_date:
        warns.append(f"{path}: {bad_date} wierszy z nieczytelną datą pominięto (także przy wyznaczaniu daty granicznej)")
    if bad_qty:
        warns.append(f"{path}: {bad_qty} wierszy z nieczytelną liczbą (przyjęto 0 w walidacji FIFO)")
    return entries, warns
