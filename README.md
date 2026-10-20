# pyxtb

XTB export (zip of `PLN_/EUR_/USD_/IKE_*.xlsx`) -> TSV rows to paste into the `Transakcje` tab of the Google Sheet, plus a report.
A local dashboard is planned and will reuse `core/`. Spec: `docs/SPEC_pyxtb.md`.
Install: `pip install -r requirements.txt`. Tests: `python -m pytest -q` (from this folder).

## Layout
```
pyxtb.py        CLI of the sheet export (entry point)
README.md, requirements.txt
core/               shared by the sheet export and the future dashboard
  reader.py           zip/xlsx -> operations per sub-account, trade-comment parser
  transactions.py     operations -> merged trades, dividends, deposits, conversions, IKE transfers (PLN totals, FIFO key)
  tickers.py          ticker dictionary (optional override)
  ticker_resolver.py  automatic XTB symbol -> sheet ticker (Yahoo, price verification)
  nbp.py              NBP rates (last business day before the date)
sheet/              Google Sheet specific
  tsv.py              TSV formatting (decimal comma, comment tag)
  report.py           control report and console summary
  checks.py           FIFO coverage check, --audit
  sheetfile.py        reader of the sheet exported as CSV (optional)
cache/              nbp_cache.json, tickers_cache.json (created automatically, safe to delete)
out/                result.tsv, report.md, audit.md (created automatically)
config/             optional tickers.csv (manual ticker overrides; create the folder when needed)
ut/                 tests
docs/               specification
dashboard/          (to be added) local dashboard on top of core/
```

## Usage
```
python pyxtb.py                          # ../export.zip, whole history -> out/result.tsv, out/report.md
python pyxtb.py other.zip --report r.md
python pyxtb.py --from 2026-08-01        # only rows from this day (inclusive), the same for ALL accounts
python pyxtb.py --from 2026-08-01 --portfolio ../portfolio-transakcje.csv   # + FIFO check against earlier positions
python pyxtb.py --audit --portfolio ../portfolio-transakcje.csv             # whole history vs sheet -> out/audit.md
```
Options: `zip` · `--from YYYY-MM-DD` · `--portfolio FILE` · `--tickers FILE` · `--offline` · `--out FILE` · `--report FILE` · `--comment tag|info|both` · `--audit`

- **Range.** Default = the whole export history (the console says so). `--from` limits it to one start date for every account; paste only rows newer than your last sheet entry. The sheet file is never required: `--portfolio` is optional and used only for `--audit` and for the FIFO check together with `--from` (earlier positions).
- **Exchange rates.** Always NBP (table A, last business day before the transaction, cached in `cache/nbp_cache.json`). There are no prompts. Bank deposits/withdrawals in EUR/USD get the NBP rate and are listed in the report as "do poprawy", because the real bank rate is unknown.
- **Console** shows a 4-5 line summary. Everything else (automatic decisions, rows to fix, warnings, ticker resolution, FIFO check, skipped types, balances) is in the report.
- **Tickers.** No dictionary file is needed. Symbols are resolved automatically by `core/ticker_resolver.py` (Yahoo chart endpoint via `urllib`, no extra dependency): suffix rules give the candidate (`TTWO.US` → `TTWO`, `EQQQ.UK` → `EQQQ.L`, `IS3N.DE` → `IS3N.DE`, `KRU.PL` → `KRU.WA`), a Yahoo search by the export's instrument name is the fallback, and a candidate is accepted only if trade prices from the export fall inside that day's low–high range (`verified` in the cache). Results are cached in `cache/tickers_cache.json`. `--offline` uses only the cache.
- **Sheet convention** produced by the resolver: `NASDAQ:`/`NYSE:`/`NYSEARCA:` (from Yahoo's exchange), `WSE:`, `LON:`, `GER:SYM:EUR` for ETFs on Xetra, `ETR:SYM` for stocks on Xetra, other suffixes per `SUFFIX_RULES`. `GER`/`FRA`/`ETR` are equivalent for prices in the sheet scripts but are part of the FIFO key, so keep existing rows consistent.
- **Override.** Optional `config/tickers.csv` (headers `xtb_symbol, ticker, name, asset_class, instrument_currency`, legacy Polish headers accepted) takes precedence over the resolver and cache: corrections, instruments Yahoo does not know (closed ones), prefixes of tickers already in the sheet.

## Moving from the flat layout
Move `nbp_cache.json` and `tickers_cache.json` to `cache/`, `tickers.csv` (if you have one) to `config/`, and delete the old root files (`reader.py`, `builder.py`, `refdata.py`, `checks.py`, `ticker_resolver.py`, `test_*.py`, `tickers_seed.csv`, `SPEC_xtb2sheet.md`). Old `result.tsv`/`report.md` in the root are not used any more.
