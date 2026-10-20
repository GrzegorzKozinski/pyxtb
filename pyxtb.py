"""pyxtb - XTB export (zip of xlsx files) -> rows to paste into the "Transakcje" tab of the Google Sheet.

Layout: core/ (shared: export reader, tickers, NBP rates, transactions), sheet/ (TSV, report, checks),
cache/ (NBP and ticker caches), out/ (results), config/ (optional tickers.csv), ut/ (tests), docs/.
Console output is a short summary; all diagnostics go to the report file (docs/SPEC_pyxtb.md §12).
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import date
from pathlib import Path

from core.nbp import NbpRates, NoRates
from core.reader import read_zip
from core.ticker_resolver import TickerResolver, YahooClient, resolve_missing
from core.tickers import TickerDict
from core.transactions import build_rows
from sheet.checks import CATEGORIES, make_audit_report, run_audit, validate_fifo
from sheet.report import make_report, print_summary
from sheet.sheetfile import read_sheet
from sheet.tsv import to_tsv

HERE = Path(__file__).resolve().parent
DEFAULT_ZIP = HERE.parent / "export.zip"                          # one folder above the script


def _iso_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}") from None


def _parse_args(argv):
    ap = argparse.ArgumentParser(description="XTB export -> TSV for the Transakcje tab")
    ap.add_argument("zip", nargs="?", default=str(DEFAULT_ZIP), help="zip with PLN_/EUR_/USD_/IKE_ *.xlsx (default: ../export.zip)")
    ap.add_argument("--from", dest="date_from", type=_iso_date, metavar="YYYY-MM-DD",
                    help="first day to include (inclusive), the same for all accounts. Default: the whole export history")
    ap.add_argument("--portfolio", help="sheet export CSV, optional: earlier positions for the FIFO check when --from is used, "
                                        "and the input of --audit")
    ap.add_argument("--tickers", help="optional override dictionary (default: config/tickers.csv, if present); symbols not in it "
                                      "are resolved automatically via Yahoo and cached in cache/tickers_cache.json")
    ap.add_argument("--offline", action="store_true", help="do not query Yahoo; use only the tickers file and the ticker cache")
    ap.add_argument("--out", help="TSV file (default: out/result.tsv)")
    ap.add_argument("--report", help="report file (default: out/report.md, or out/audit.md with --audit)")
    ap.add_argument("--comment", choices=("tag", "info", "both"), default="both",
                    help="Comment column: FIFO tag, info (tax / currency amount), or both")
    ap.add_argument("--audit", action="store_true",
                    help="compare the WHOLE export history with the sheet file and list discrepancies (changes nothing)")
    return ap.parse_args(argv)


def _fail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


def _write(path: Path, text: str, **kw) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", **kw)


def main(argv=None) -> int:
    args = _parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        accounts, problems = read_zip(args.zip)
    except (OSError, zipfile.BadZipFile) as e:
        return _fail(f"Nie można odczytać {args.zip}: {e}")
    if not accounts:
        return _fail("\n".join(f"! {p}" for p in problems) or "Brak danych w zipie.")

    if args.tickers and not Path(args.tickers).exists():
        return _fail(f"Nie znaleziono pliku słownika: {args.tickers}")
    tickers = TickerDict.load(Path(args.tickers) if args.tickers else HERE / "config" / "tickers.csv")   # optional override
    resolver = TickerResolver(HERE / "cache" / "tickers_cache.json", None if args.offline else YahooClient())
    resolve_missing(accounts, tickers, resolver)

    entries, sheet_warnings = [], []
    if args.portfolio:
        try:
            entries, sheet_warnings = read_sheet(args.portfolio)
        except OSError as e:
            return _fail(f"Nie można odczytać {args.portfolio}: {e}")

    if args.audit:
        if not args.portfolio:
            return _fail("Audyt wymaga pliku arkusza (--portfolio PLIK).")
        return _run_audit(args, accounts, tickers, entries, Path(args.portfolio), problems + sheet_warnings)

    scope = f"od {args.date_from} włącznie" if args.date_from else "cała historia eksportu"
    cutoffs = {acc: args.date_from for acc in accounts} if args.date_from else {}
    notes = list(problems) + sheet_warnings
    if args.portfolio and not args.date_from:
        notes.append("--portfolio pominięto w kontroli FIFO: bez --from wynik obejmuje całą historię (arkusz zdublowałby pozycje)")

    res = build_rows(accounts, tickers, NbpRates(HERE / "cache" / "nbp_cache.json"), cutoffs)
    out = Path(args.out) if args.out else HERE / "out" / "result.tsv"
    _write(out, to_tsv(res.rows, args.comment), newline="")
    # whole history is self-contained; with --from the earlier positions can only come from the sheet file
    fifo = validate_fifo([], res.rows) if not args.date_from else (validate_fifo(entries, res.rows) if entries else None)

    report = make_report(accounts, notes, res, scope, str(out), fifo, resolver.notes)
    report_path = Path(args.report) if args.report else HERE / "out" / "report.md"
    _write(report_path, "\n".join(report) + "\n")
    print_summary(args, accounts, notes, scope, res, fifo, report_path, resolver.notes, out)
    return 0


def _run_audit(args, accounts, tickers, entries, pfile, problems) -> int:
    export = build_rows(accounts, tickers, NoRates())
    ranges = {acc: (min(o.date for o in d.operations), max(o.date for o in d.operations))
              for acc, d in accounts.items() if d.operations}
    result = run_audit(export.rows, entries, ranges)
    path = Path(args.report) if args.report else HERE / "out" / "audit.md"
    _write(path, "\n".join(make_audit_report(result, pfile, problems)) + "\n")
    shown = [f"{len(result.by_category(c)):>4}  {title}" for c, title in CATEGORIES if result.by_category(c)]
    print(f"Audyt: {sum(len(result.by_category(c)) for c, _ in CATEGORIES)} pozycji → {path}")
    print("\n".join(shown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
