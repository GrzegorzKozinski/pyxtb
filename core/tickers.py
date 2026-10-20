"""Ticker dictionary (spec §9): optional config/tickers.csv override plus entries resolved automatically."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Instrument:
    symbol: str            # XTB symbol, e.g. TTWO.US
    ticker: str            # sheet ticker, e.g. NASDAQ:TTWO
    name: str
    asset_class: str       # 'Akcje polskie' / 'Akcje zagraniczne'
    currency: str          # instrument currency ('' = unknown)
    report_notes: list[str] = field(default_factory=list)  # for the report
    row_warnings: list[str] = field(default_factory=list)  # for the row comment


# canonical CSV headers: xtb_symbol, ticker, name, asset_class, instrument_currency (legacy Polish ones are aliases)
_HEADER_ALIASES = {"nazwa": "name", "klasa": "asset_class", "waluta_instrumentu": "instrument_currency"}


class TickerDict:
    def __init__(self, entries: dict[str, dict[str, str]]):
        self.entries = entries

    @classmethod
    def load(cls, path: str | Path | None) -> "TickerDict":
        """Optional override file; a missing file gives an empty dictionary (symbols are then resolved automatically)."""
        entries: dict[str, dict[str, str]] = {}
        if path is None or not Path(path).exists():
            return cls(entries)
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                r = {_HEADER_ALIASES.get(k.strip(), k.strip()): (v or "").strip() for k, v in r.items() if k}
                if r.get("xtb_symbol"):
                    entries[r["xtb_symbol"].upper()] = r
        return cls(entries)

    def has(self, symbol: str) -> bool:
        e = self.entries.get(symbol.strip().upper())
        return bool(e and e.get("ticker"))

    def add(self, entry: dict[str, str]) -> None:
        self.entries[entry["xtb_symbol"].strip().upper()] = entry

    def resolve(self, symbol: str, export_name: str = "") -> Instrument:
        sym = symbol.strip().upper()
        e = self.entries.get(sym)
        is_pl = sym.endswith(".PL")

        if e and e.get("ticker"):                                  # complete entry
            notes = []
            name = e.get("name", "")
            if not name:
                name = export_name
                notes.append(f"{sym}: pusta nazwa w słowniku – użyto nazwy z eksportu „{export_name}” (uzupełnij tickers.csv)")
            return Instrument(sym, e["ticker"], name,
                              e.get("asset_class") or ("Akcje polskie" if is_pl else "Akcje zagraniczne"),
                              e.get("instrument_currency") or ("PLN" if is_pl else ""), report_notes=notes)

        why = "niekompletny wpis" if e else "brak wpisu"
        if is_pl:                                                  # new Polish symbol: automatic rule
            base = sym[:-3]
            return Instrument(sym, f"WSE:{base}", export_name, "Akcje polskie", "PLN",
                              report_notes=[f"{sym}: {why} w słowniku – użyto WSE:{base} (dopisz do tickers.csv)"])
        return Instrument(sym, sym, export_name, "Akcje zagraniczne", "",
                          report_notes=[f"{sym}: {why} w słowniku – zapisano surowy symbol (uzupełnij tickers.csv)"],
                          row_warnings=[f"Nieznany symbol XTB, brak wpisu w słowniku: {sym}"])
