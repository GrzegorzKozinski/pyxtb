"""Control report (spec §12) and the short console summary of the sheet export."""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

from core.reader import ACCOUNT_ORDER


def _fmt(d: Decimal | None) -> str:
    return "—" if d is None else f"{d:.2f}".replace(".", ",")


def _to_fix(res) -> list[str]:
    return [f"{r.date} {r.account} {r.ticker} {r.kind}: {'BRAK KURSU (pole puste)' if r.rate is None else r.review}"
            for r in res.rows if r.rate is None or r.review]


def make_report(accounts, notes, res, scope, out_path, fifo, auto_notes=()) -> list[str]:
    L: list[str] = [f"! {n}" for n in notes]

    def section(title: str, lines: list[str]) -> None:
        if lines:
            L.extend(["", f"## {title}", *lines])

    accs = sorted(accounts, key=ACCOUNT_ORDER.index)
    L += ["## Zakres", f"  {scope}"]

    counts = Counter((r.account, r.kind) for r in res.rows)
    L += ["", "## Wygenerowane wiersze"]
    L += [f"  {a:<6}{k:<22}{n:>5}" for (a, k), n in sorted(counts.items(), key=lambda kv: (ACCOUNT_ORDER.index(kv[0][0]), kv[0][1]))]
    L.append(f"  RAZEM: {len(res.rows)}  → {out_path}")

    section("Automatyczne decyzje", [f"  - {d}" for d in res.decisions])
    fix = _to_fix(res)
    by_rule = sum(1 for r in res.rows if r.nbp_used and r.rate is not None and not r.review)
    section("Kursy NBP (ostatni dzień roboczy przed transakcją)",
            ([f"  Do ręcznej poprawy / weryfikacji ({len(fix)}):"] + [f"  - {x}" for x in fix] if fix else [])
            + ([f"  Zakupy, sprzedaże i dywidendy z kursem NBP wg reguły PIT-38 (bez uwag): {by_rule} wierszy"] if by_rule else []))
    section("Tickery rozpoznane automatycznie (Yahoo) – zweryfikuj; poprawki w tickers.csv mają pierwszeństwo",
            [f"  - {n}" for n in auto_notes])
    section("Słownik tickerów – do uzupełnienia", [f"  - {n}" for n in res.dict_notes])
    section("Ostrzeżenia", [f"  - {w}" for w in res.warnings])

    L += ["", "## Walidacja FIFO (pokrycie sprzedaży względem arkusza)"]
    if fifo is None:
        L.append("  pominięto (--from bez --portfolio: nieznane pozycje sprzed tej daty)")
    else:
        L.append(f"  z arkusza wczytano {fifo.sheet_rows} wierszy FIFO (Zakup/DRIP/Sprzedaż), kluczy: {fifo.keys}; nowych sprzedaży: {fifo.new_sales}"
                 if fifo.sheet_rows else f"  sprawdzono wygenerowane wiersze; sprzedaży: {fifo.new_sales}")
        if fifo.skipped_keys:
            L.append(f"  pominięto klucze ze Split (nieobsługiwane): {', '.join(fifo.skipped_keys)}")
        L += [f"  ! {w}" for w in fifo.warnings]
        if not fifo.warnings and fifo.new_sales:
            L.append("  OK – każda nowa sprzedaż ma pokrycie w kluczu FIFO")

    section("Pominięte typy (decyzja użytkownika)",
            [f"  {k.replace('|', ' / '):<34}{len(ops):>4} szt., suma {_fmt(sum((o.amount for o in ops), Decimal(0)))}"
             for k, ops in sorted(res.skipped.items())])

    L += ["", "## Salda gotówki per subkonto (wiersz Total z eksportu) – porównaj z XTB"]
    for a in accs:
        d = accounts[a]
        status = {True: "OK", False: "NIEZGODNE", None: "brak wiersza Total"}[d.balance_ok]
        L.append(f"  {a:<6}{_fmt(d.total):>12} {d.currency} | suma Amount {_fmt(d.amount_sum)} → {status}")
    return L


# ====================================================================== console

def print_summary(args, accounts, notes, scope, res, fifo, report_path, auto_notes=(), out_path=None) -> None:
    accs = sorted(accounts, key=ACCOUNT_ORDER.index)
    for n in notes:
        if "nieczytelny" not in n:               # file-level errors are shown, per-row noise stays in the report
            print(f"! {n}")
    per_acc = Counter(r.account for r in res.rows)
    print(f"Zakres: {scope}" + ("" if args.date_from else " (tylko nowsze wiersze: --from RRRR-MM-DD)"))
    print(f"Wiersze: {len(res.rows)} ({', '.join(f'{a} {per_acc[a]}' for a in accs if per_acc[a]) or 'brak nowych'}) → {out_path}")

    bad = [a for a in accs if accounts[a].balance_ok is not True]
    print("Salda gotówki: " + (f"OK ({len(accs)}/{len(accs)})" if not bad else f"! sprawdź {', '.join(bad)} (niezgodne lub bez wiersza Total)"))

    attention = [(len(_to_fix(res)), "do poprawy/weryfikacji"), (len(res.warnings), "ostrzeżeń"),
                 (len(fifo.warnings) if fifo else 0, "problemów FIFO"), (len(res.dict_notes), "do słownika"), (len(auto_notes), "tickerów rozpoznanych automatycznie")]
    todo = ", ".join(f"{n} {label}" for n, label in attention if n)
    print(f"Uwagi: {todo or 'brak'} → {report_path}")
