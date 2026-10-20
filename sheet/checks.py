"""Checks of the generated rows against the existing sheet: FIFO coverage (spec §11) and the audit mode (§13)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from core.transactions import PLN_ACCOUNTS, SheetRow, fifo_key_tag
from sheet.sheetfile import SheetEntry

TRADE_KINDS = ("zakup", "sprzedaż")


def _n(d: Decimal | None, places: int | None = None) -> str:
    if d is None:
        return "?"
    s = f"{d:.{places}f}" if places is not None else format(d.normalize(), "f")
    return s.replace(".", ",")


# ====================================================================== FIFO coverage

BUY_KINDS = ("zakup", "drip")
SELL_KIND = "sprzedaż"
SPLIT_KIND = "split"
QTY_TOLERANCE = Decimal("0.00000001")


def _q8(d: Decimal) -> Decimal:
    return d.quantize(QTY_TOLERANCE, rounding=ROUND_HALF_UP)


@dataclass
class FifoCheck:
    warnings: list[str] = field(default_factory=list)
    sheet_rows: int = 0
    keys: int = 0
    new_sales: int = 0
    skipped_keys: list[str] = field(default_factory=list)


def validate_fifo(entries: list[SheetEntry], new_rows: list[SheetRow]) -> FifoCheck:
    """Does every new sale have a purchase under the same key Konto##TICKER##Klasa##Waluta?

    Only quantities are simulated (Zakup/DRIP add, Sprzedaż subtracts, 8 decimals). Keys with a Split
    are skipped - split semantics live in the sheet script."""
    res = FifoCheck()
    events = []                                   # (date, sells-after-buys, seq, key, kind, qty, new row | None)
    sheet_keys: dict[tuple[str, str], set[str]] = defaultdict(set)     # (account, TICKER) -> keys in the sheet
    all_keys: set[str] = set()
    split_keys: set[str] = set()

    for i, e in enumerate(entries):
        kind = e.kind.strip().casefold()
        if kind not in BUY_KINDS + (SELL_KIND, SPLIT_KIND):
            continue
        key = fifo_key_tag(e.account, e.ticker, e.asset_class, e.currency)
        all_keys.add(key)
        sheet_keys[(e.account, e.ticker.upper())].add(key)
        if kind == SPLIT_KIND:
            split_keys.add(key)
        else:
            events.append((e.date, kind == SELL_KIND, i, key, kind, e.qty, None))
    res.sheet_rows, res.keys = len(events), len(all_keys)

    for j, r in enumerate(new_rows):
        kind = r.kind.strip().casefold()
        if kind not in BUY_KINDS + (SELL_KIND,):
            continue
        key = r.tag
        events.append((r.date, kind == SELL_KIND, 10**9 + j, key, kind, r.qty, r))
        res.new_sales += kind == SELL_KIND
        known = sheet_keys.get((r.account, r.ticker.upper()), set())
        if known and key not in known:        # same ticker, other key: Klasa / Waluta drift
            res.warnings.append(f"{r.account} {r.date} {r.kind} {r.ticker}: klucz FIFO {key} nie występuje w arkuszu, "
                                f"a ten ticker ma tam klucz: {', '.join(sorted(known))} – sprawdź Klasę/Walutę")

    res.skipped_keys = sorted(split_keys)
    holdings: dict[str, Decimal] = defaultdict(Decimal)
    for day, _, _, key, kind, qty, new in sorted(events, key=lambda ev: ev[:3]):
        if key in split_keys:
            continue
        have = holdings[key]
        if kind in BUY_KINDS:
            holdings[key] = _q8(have + qty)
        elif have + QTY_TOLERANCE < qty:
            if new is not None:
                msg = f"{new.account} {day} Sprzedaż {new.ticker} {_n(qty)}: brak pokrycia w kluczu {key} (dostępne {_n(have)})"
                similar = sorted(sheet_keys.get((new.account, new.ticker.upper()), set()) - {key})
                if similar:
                    msg += f"; ten ticker ma w arkuszu klucz: {', '.join(similar)}"
                res.warnings.append(msg)
            holdings[key] = Decimal(0)
        else:
            holdings[key] = _q8(have - qty)
    return res


# ====================================================================== audit (whole history vs sheet)
# Nothing is corrected. Export rows are built without NBP, so for EUR/USD accounts only the quantity
# (trades) or presence/count (cash, dividends) is compared; PLN accounts are also compared by Total PLN.

QTY_TOL = Decimal("0.000001")
TOTAL_TOL = Decimal("0.02")
DATE_TOL_DAYS = 3
KNOWN_KINDS = {"zakup", "sprzedaż", "wpłata środków", "wypłata środków", "dywidenda / odsetki"}
CATEGORIES = (
    ("missing_in_sheet", "Są w eksporcie, brak w arkuszu"),
    ("missing_in_export", "Są w arkuszu, brak w eksporcie"),
    ("value", "Różne wartości (ten sam dzień)"),
    ("date", "Różne daty (ta sama wartość)"),
    ("count", "Różna liczba wierszy (dzień/instrument bez wartości do porównania)"),
    ("key", "Różny klucz FIFO (Waluta / Klasa)"),
    ("info", "Informacyjnie: zlecenia rozbite w arkuszu na kilka wierszy (eksport scala)"),
)


@dataclass
class Finding:
    category: str
    account: str
    day: date
    text: str


@dataclass
class AuditResult:
    findings: list[Finding] = field(default_factory=list)
    matched_days: int = 0
    out_of_range: int = 0
    ignored_kinds: dict[str, int] = field(default_factory=dict)
    export_rows: int = 0
    sheet_rows: int = 0

    def by_category(self, cat: str) -> list[Finding]:
        return sorted((f for f in self.findings if f.category == cat), key=lambda f: (f.day, f.account, f.text))


@dataclass
class _Day:
    """Rows of one (account, ticker, kind) on one day - export rows and sheet rows alike."""
    qty: Decimal
    total: Decimal | None
    count: int
    currencies: set[str]
    classes: set[str]


def _agg(rows) -> _Day:
    totals = [r.total for r in rows]
    return _Day(sum((r.qty for r in rows), Decimal(0)), None if None in totals else sum(totals, Decimal(0)),
                len(rows), {r.currency for r in rows}, {r.asset_class for r in rows})


def _diffs(account: str, kind_cf: str, e: _Day, s: _Day) -> list[str]:
    out = []
    if kind_cf in TRADE_KINDS and abs(e.qty - s.qty) > QTY_TOL:
        out.append(f"Liczba arkusz {_n(s.qty)} vs eksport {_n(e.qty)}")
    if account in PLN_ACCOUNTS and e.total is not None and s.total is not None and abs(e.total - s.total) > TOTAL_TOL:
        out.append(f"Total arkusz {_n(s.total, 2)} vs eksport {_n(e.total, 2)}")
    return out


def _compatible(account: str, kind_cf: str, e: _Day, s: _Day) -> bool:
    """Can these two days be the same operation entered with a shifted date?"""
    if kind_cf in TRADE_KINDS:                      # trades: quantity decides, Total differences are reported afterwards
        return abs(e.qty - s.qty) <= QTY_TOL
    if account in PLN_ACCOUNTS and e.total is not None and s.total is not None:
        return abs(e.total - s.total) <= TOTAL_TOL
    return True                                     # nothing comparable: the nearest date is the best guess


def _desc(account: str, kind_cf: str, d: _Day) -> str:
    parts = []
    if kind_cf in TRADE_KINDS:
        parts.append(f"Liczba {_n(d.qty)}")
    if account in PLN_ACCOUNTS and d.total is not None:
        parts.append(f"Total {_n(d.total, 2)}")
    if d.count > 1:
        parts.append(f"{d.count} wiersze")
    return ", ".join(parts) or "—"


def _group(items, key) -> dict[tuple, dict[date, list]]:
    out: dict[tuple, dict[date, list]] = defaultdict(lambda: defaultdict(list))
    for it in items:
        out[key(it)][it.date].append(it)
    return out


def _key(x) -> tuple[str, str, str]:
    return x.account, x.ticker.strip().casefold(), x.kind.strip().casefold()


def run_audit(export_rows: list[SheetRow], entries: list[SheetEntry],
              export_ranges: dict[str, tuple[date, date]]) -> AuditResult:
    res = AuditResult(export_rows=len(export_rows))
    in_scope = []
    for e in entries:
        kind_cf = e.kind.strip().casefold()
        rng = export_ranges.get(e.account)
        if kind_cf not in KNOWN_KINDS:
            res.ignored_kinds[e.kind] = res.ignored_kinds.get(e.kind, 0) + 1
        elif rng is None or not (rng[0] <= e.date <= rng[1]):
            res.out_of_range += 1
        else:
            in_scope.append(e)
    res.sheet_rows = len(in_scope)

    exp, sh = _group(export_rows, _key), _group(in_scope, _key)
    for key in sorted(set(exp) | set(sh)):
        _compare(res, key, exp.get(key, {}), sh.get(key, {}))
    return res


def _compare(res: AuditResult, key: tuple, e_rows: dict[date, list], s_rows: dict[date, list]) -> None:
    account, _, kind_cf = key
    e_days = {d: _agg(rows) for d, rows in e_rows.items()}
    s_days = {d: _agg(rows) for d, rows in s_rows.items()}
    first = next(r for rows in (*e_rows.values(), *s_rows.values()) for r in rows)
    head = f"{first.kind} {first.ticker}"
    has_value = kind_cf in TRADE_KINDS or account in PLN_ACCOUNTS

    def add(cat: str, day: date, text: str) -> None:
        res.findings.append(Finding(cat, account, day, text))

    def check_pair(de: date, ds: date) -> None:
        e, s = e_days[de], s_days[ds]
        diffs = _diffs(account, kind_cf, e, s)
        if de == ds:
            res.matched_days += 1
            if diffs:
                add("value", de, f"{account} {de} {head}: " + "; ".join(diffs))
            elif not has_value and e.count != s.count:
                add("count", de, f"{account} {de} {head}: arkusz {s.count} vs eksport {e.count} wierszy")
            if kind_cf in TRADE_KINDS and e.count == 1 and s.count > 1:
                add("info", de, f"{account} {de} {head}: w arkuszu {s.count} wiersze, eksport scala w 1 (Liczba {_n(e.qty)})")
        else:
            extra = ("; " + "; ".join(diffs)) if diffs else ""
            add("date", de, f"{account} {head}: arkusz {ds} ↔ eksport {de} ({_desc(account, kind_cf, e)}){extra}")
        if (kind_cf in TRADE_KINDS or kind_cf == "dywidenda / odsetki") and (e.currencies != s.currencies or e.classes != s.classes):
            add("key", de, f"{account} {de} {head}: Waluta/Klasa arkusz {sorted(s.currencies)}/{sorted(s.classes)} "
                           f"vs eksport {sorted(e.currencies)}/{sorted(e.classes)}")

    for d in sorted(e_days.keys() & s_days.keys()):
        check_pair(d, d)
    left_s = set(s_days) - set(e_days)
    for de in sorted(set(e_days) - set(s_days)):
        cands = [ds for ds in left_s if abs((ds - de).days) <= DATE_TOL_DAYS and _compatible(account, kind_cf, e_days[de], s_days[ds])]
        if cands:
            ds = min(cands, key=lambda x: (abs((x - de).days), x))
            left_s.discard(ds)
            check_pair(de, ds)
        else:
            add("missing_in_sheet", de, f"{account} {de} {head}: {_desc(account, kind_cf, e_days[de])}")
    for ds in sorted(left_s):
        add("missing_in_export", ds, f"{account} {ds} {head}: {_desc(account, kind_cf, s_days[ds])}")


def make_audit_report(res: AuditResult, sheet_path, problems: list[str] | None = None) -> list[str]:
    L = [f"## Audyt: cała historia eksportu vs arkusz ({sheet_path})",
         f"  wierszy z eksportu (po scaleniu): {res.export_rows}; wierszy arkusza w zakresie eksportu: {res.sheet_rows}; "
         f"dni zgodnych co do daty: {res.matched_days}",
         "  UWAGA: konta EUR/USD porównywane tylko po ilości / obecności (kurs NBP nie jest tu pobierany); XTB i Z_IKE także po Total PLN."]
    if res.out_of_range:
        L.append(f"  pominięto {res.out_of_range} wierszy arkusza spoza zakresu dat eksportu danego konta")
    if res.ignored_kinds:
        L.append("  pominięto rodzaje spoza zakresu narzędzia: " + ", ".join(f"{k} ({n})" for k, n in sorted(res.ignored_kinds.items())))
    L += [f"  ! {p}" for p in problems or []]
    L += ["", "## Podsumowanie"]
    L += [f"  {len(res.by_category(cat)):>4}  {title}" for cat, title in CATEGORIES]
    for cat, title in CATEGORIES:
        items = res.by_category(cat)
        if items:
            L += ["", f"## {title} ({len(items)})"] + [f"  - {f.text}" for f in items]
    return L
