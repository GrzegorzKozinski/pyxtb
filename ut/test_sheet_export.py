"""Tests on in-memory data (no network, no real exports). Run: python -m pytest -q"""
from datetime import date, datetime, timezone
from decimal import Decimal as D

import openpyxl
import pytest

import pyxtb
from core.reader import AccountData, Operation
from core.tickers import TickerDict
from core.transactions import build_rows
from sheet.checks import validate_fifo
from sheet.sheetfile import SheetEntry
from sheet.tsv import to_tsv

DICT = TickerDict({
    "TTWO.US": dict(xtb_symbol="TTWO.US", ticker="NASDAQ:TTWO", name="Take-Two", asset_class="Akcje zagraniczne", instrument_currency="USD"),
    "V.US": dict(xtb_symbol="V.US", ticker="NYSE:V", name="Visa", asset_class="Akcje zagraniczne", instrument_currency="USD"),
    "KTY.PL": dict(xtb_symbol="KTY.PL", ticker="WSE:KTY", name="Kety", asset_class="Akcje polskie", instrument_currency="PLN"),
})


class FixedRates:
    errors: list[str] = []

    def get(self, ccy, on):
        return D("4.0000")


def op(acc, typ, day, amount, ticker="", comment="", hms=(12, 0, 0)):
    t = datetime(day.year, day.month, day.day, *hms, tzinfo=timezone.utc)
    return Operation(acc, typ, "", ticker, t, D(str(amount)), comment)


def accounts(*ops):
    out = {}
    for o in ops:
        out.setdefault(o.account, AccountData(o.account, "PLN" if o.account in ("XTB", "Z_IKE") else o.account, "f.xlsx")).operations.append(o)
    return out


def build(*ops, cutoffs=None):
    return build_rows(accounts(*ops), DICT, FixedRates(), cutoffs)


def test_trade_lines_are_merged_and_use_nbp():
    res = build(op("USD", "Stock purchase", date(2026, 8, 28), -473.88, "TTWO.US", "OPEN BUY 2/2.2 @ 236.94"),
                op("USD", "Stock purchase", date(2026, 8, 28), -47.48, "TTWO.US", "OPEN BUY 0.2/2.2 @ 237.41"))
    [r] = res.rows
    assert (r.ticker, r.kind, r.qty, r.rate, r.total) == ("NASDAQ:TTWO", "Zakup", D("2.2"), D("4.0000"), D("2085.44"))
    assert to_tsv(res.rows).split("\t")[8] == "236,982"


def test_dividend_is_netted():
    res = build(op("USD", "Dividend", date(2026, 9, 1), 0.70, "V.US"), op("USD", "Withholding tax", date(2026, 9, 1), -0.10, "V.US"))
    [r] = res.rows
    assert r.total == D("2.40") and r.info == "Pobrany podatek: 0.10 USD"


def test_conversion_consumes_pln_deposit():
    res = build(op("XTB", "Deposit", date(2025, 1, 17), 1200),
                op("XTB", "Transfer", date(2025, 1, 20), -1200, comment="Currency conversion, PLN to USD"),
                op("USD", "Transfer", date(2025, 1, 20), 292.02))
    [r] = res.rows
    assert (r.account, r.kind, r.total, r.rate, r.info) == ("USD", "Wpłata środków", D("1200.00"), D("4.1093"), "292 USD")
    assert any("pominięto wpłatę XTB" in d for d in res.decisions)


def test_bank_fx_deposit_uses_nbp_and_is_flagged():
    [r] = build(op("USD", "Deposit", date(2026, 8, 5), 290)).rows
    assert (r.rate, r.total, r.info) == (D("4.0000"), D("1160.00"), "290 USD") and r.review


def test_ike_pair():
    res = build(op("XTB", "IKE deposit", date(2026, 5, 22), -5074.21), op("Z_IKE", "IKE deposit", date(2026, 5, 22), 5074.21))
    assert [(r.account, r.kind, r.total) for r in res.rows] == [("XTB", "Wypłata środków", D("5074.21")), ("Z_IKE", "Wpłata środków", D("5074.21"))]


def test_global_cutoff_filters_all_accounts():
    ops = [op("XTB", "Deposit", date(2026, 1, 1), 100), op("XTB", "Deposit", date(2026, 3, 1), 100),
           op("Z_IKE", "Deposit", date(2026, 1, 1), 100)]
    assert len(build(*ops, cutoffs={"XTB": date(2026, 2, 1), "Z_IKE": date(2026, 2, 1)}).rows) == 1


def test_fifo_flags_sale_without_purchase():
    sale = build(op("XTB", "Stock sell", date(2026, 3, 1), 300, "KTY.PL", "CLOSE BUY 1 @ 300")).rows
    buy = [SheetEntry("XTB", date(2026, 1, 1), "WSE:KTY", "PLN", "Akcje polskie", "Zakup", D(1))]
    assert not validate_fifo(buy, sale).warnings
    assert validate_fifo([], sale).warnings


def test_from_rejects_per_account_syntax():
    with pytest.raises(SystemExit):
        pyxtb.main(["x.zip", "--from", "USD=2026-01-01"])


def test_end_to_end_concise_console(tmp_path, monkeypatch, capsys):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cash Operations"
    for _ in range(4):
        ws.append([None])
    ws.append(["Type", "Instrument", "Ticker", "Category", "Time", "Amount", "ID", "Comment", "Product", "Position ID"])
    ws.append(["Dividend", "Visa", "V.US", "", datetime(2026, 9, 1, 14), 0.70, "1", "", "My Trades", ""])
    ws.append(["Withholding tax", "Visa", "V.US", "", datetime(2026, 9, 1, 14), -0.10, "2", "", "My Trades", ""])
    ws.append(["Total", None, None, None, None, 0.6])
    wb.save(tmp_path / "USD_1_2026.xlsx")
    import zipfile
    with zipfile.ZipFile(tmp_path / "export.zip", "w") as z:
        z.write(tmp_path / "USD_1_2026.xlsx", "USD_1_2026.xlsx")
    (tmp_path / "t.csv").write_text("xtb_symbol,ticker,name,asset_class,instrument_currency\nV.US,NYSE:V,Visa,Akcje zagraniczne,USD\n", encoding="utf-8")
    monkeypatch.setattr(pyxtb, "NbpRates", lambda *_a, **_k: FixedRates())

    code = pyxtb.main([str(tmp_path / "export.zip"), "--from", "2026-01-01", "--tickers", str(tmp_path / "t.csv"),
                           "--out", str(tmp_path / "o.tsv"), "--report", str(tmp_path / "r.md")])
    out = capsys.readouterr().out
    assert code == 0
    assert len(out.strip().splitlines()) <= 5
    assert "Salda gotówki: OK" in out
    assert (tmp_path / "o.tsv").read_text(encoding="utf-8").startswith("USD\t2026-09-01\tNYSE:V")
    assert "## Salda gotówki" in (tmp_path / "r.md").read_text(encoding="utf-8")


def _zip_with_deposits(tmp_path):
    import zipfile
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cash Operations"
    for _ in range(4):
        ws.append([None])
    ws.append(["Type", "Instrument", "Ticker", "Category", "Time", "Amount", "ID", "Comment", "Product", "Position ID"])
    ws.append(["Deposit", None, None, None, datetime(2025, 1, 10, 12), 100.0, "1", "", "My Trades", ""])
    ws.append(["Deposit", None, None, None, datetime(2026, 3, 1, 12), 200.0, "2", "", "My Trades", ""])
    ws.append(["Total", None, None, None, None, 300.0])
    wb.save(tmp_path / "PLN_1_2026.xlsx")
    with zipfile.ZipFile(tmp_path / "export.zip", "w") as z:
        z.write(tmp_path / "PLN_1_2026.xlsx", "PLN_1_2026.xlsx")
    return str(tmp_path / "export.zip")


def test_default_is_whole_history_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pyxtb, "HERE", tmp_path)
    args = [_zip_with_deposits(tmp_path), "--out", str(tmp_path / "o.tsv"), "--report", str(tmp_path / "r.md")]
    assert pyxtb.main(args) == 0
    out = capsys.readouterr().out
    assert "Zakres: cała historia eksportu (tylko nowsze wiersze: --from RRRR-MM-DD)" in out
    assert len((tmp_path / "o.tsv").read_text(encoding="utf-8").splitlines()) == 2
    assert "sprawdzono wygenerowane wiersze" in (tmp_path / "r.md").read_text(encoding="utf-8")

    assert pyxtb.main(args + ["--from", "2026-01-01"]) == 0
    assert "Zakres: od 2026-01-01 włącznie" in capsys.readouterr().out
    assert len((tmp_path / "o.tsv").read_text(encoding="utf-8").splitlines()) == 1
    assert "--from bez --portfolio" in (tmp_path / "r.md").read_text(encoding="utf-8")
