"""TSV formatting of transaction rows for pasting into the Transakcje tab (spec §4.2, §10)."""
from __future__ import annotations

from decimal import Decimal

from core.transactions import SheetRow, q


def _num(d: Decimal | None, places: int) -> str:
    return "" if d is None else f"{q(d, places):f}".replace(".", ",")


def _qty(d: Decimal) -> str:
    """Up to 8 decimal places, no trailing zeros."""
    s = f"{q(d, 8):f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def comment(row: SheetRow, mode: str = "both") -> str:
    """Segments joined with ' | ': FIFO tag, info, warnings. mode: tag | info | both."""
    parts = [row.tag] if mode in ("tag", "both") else []
    if mode in ("info", "both") and row.info:
        parts.append(row.info)
    parts += [f"⚠️ {w}" for w in row.warnings]
    return " | ".join(parts)


def to_tsv(rows: list[SheetRow], mode: str = "both") -> str:
    """Tab-separated, decimal comma, no header row; ready to paste under the existing data."""
    def clean(s: str) -> str:
        return s.replace("\t", " ").replace("\n", " ")

    lines = []
    for r in rows:
        fields = [r.account, r.date.isoformat(), r.ticker, r.currency, r.name, r.asset_class, r.kind,
                  _qty(r.qty), "1" if r.price == 1 else _num(r.price, 3), _num(r.fee, 0), _num(r.rate, 4),
                  _num(r.nominal, 0), _num(r.total, 2), comment(r, mode)]
        lines.append("\t".join(clean(f) for f in fields) + "\n")
    return "".join(lines)
