"""Resolver tests on a fake Yahoo client (no network). Run: python -m pytest -q"""
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal as D

import openpyxl

import xtb2sheet
from core.reader import AccountData, Operation
from core.ticker_resolver import ChartData, TickerResolver, resolve_missing, verify_prices
from core.tickers import TickerDict
from core.transactions import build_rows

DAY = date(2026, 8, 28)


def chart(symbol, ccy="USD", exch="NMS", typ="EQUITY", lo="230", hi="240", day=DAY):
    return ChartData(symbol, ccy, exch, typ, symbol, [(day, D(lo), D(hi))])


class FakeClient:
    def __init__(self, charts=None, search=None, fail=False):
        self.charts, self.searches, self.fail, self.calls = charts or {}, search or {}, fail, []

    def chart(self, symbol, start, end):
        self.calls.append(symbol)
        if self.fail:
            raise ConnectionError("brak sieci")
        return self.charts.get(symbol)

    def search(self, query):
        return self.searches.get(query, [])


EV = [(DAY, D("236.94"))]


def resolver(client, tmp_path):
    return TickerResolver(tmp_path / "cache.json", client)


def test_us_stock_verified_by_price(tmp_path):
    r = resolver(FakeClient({"TTWO": chart("TTWO")}), tmp_path)
    e = r.resolve("TTWO.US", "Take-Two", EV)
    assert (e["ticker"], e["instrument_currency"], e["name"], e["asset_class"]) == ("NASDAQ:TTWO", "USD", "Take-Two", "Akcje zagraniczne")
    assert "cena potwierdzona 1/1" in r.notes[0]


def test_nyse_exchange(tmp_path):
    e = resolver(FakeClient({"V": chart("V", exch="NYQ")}), tmp_path).resolve("V.US", "Visa", EV)
    assert e["ticker"] == "NYSE:V"


def test_price_mismatch_is_rejected(tmp_path):
    r = resolver(FakeClient({"TTWO": chart("TTWO", lo="10", hi="12")}), tmp_path)
    assert r.resolve("TTWO.US", "", EV) is None
    assert "nie pasują" in r.notes[0]
    assert not (tmp_path / "cache.json").exists()


def test_de_etf_and_stock_conventions(tmp_path):
    c = FakeClient({"IS3N.DE": chart("IS3N.DE", "EUR", "GER", "ETF"), "SAP.DE": chart("SAP.DE", "EUR", "GER", "EQUITY")})
    r = resolver(c, tmp_path)
    assert r.resolve("IS3N.DE", "x", EV)["ticker"] == "GER:IS3N:EUR"
    assert r.resolve("SAP.DE", "x", EV)["ticker"] == "ETR:SAP"


def test_uk_pence_quote_with_price_in_pounds(tmp_path):
    e = resolver(FakeClient({"EQQQ.L": chart("EQQQ.L", "GBp", "LSE", "ETF", lo="23000", hi="24000")}), tmp_path).resolve(
        "EQQQ.UK", "x", [(DAY, D("236.5"))])
    assert (e["ticker"], e["instrument_currency"]) == ("LON:EQQQ", "GBP")


def test_uk_usd_line(tmp_path):
    e = resolver(FakeClient({"IUIT.L": chart("IUIT.L", "USD", "LSE", "ETF")}), tmp_path).resolve("IUIT.UK", "x", EV)
    assert (e["ticker"], e["instrument_currency"]) == ("LON:IUIT", "USD")


def test_pl_stock(tmp_path):
    e = resolver(FakeClient({"KRU.WA": chart("KRU.WA", "PLN", "WSE")}), tmp_path).resolve("KRU.PL", "Kruk", EV)
    assert (e["ticker"], e["asset_class"], e["instrument_currency"]) == ("WSE:KRU", "Akcje polskie", "PLN")


def test_search_fallback_when_rule_candidate_unknown(tmp_path):
    c = FakeClient({"ABC.DE": chart("ABC.DE", "EUR", "GER", "ETF")}, search={"Some ETF": ["ABC.DE", "ABC.L"]})
    e = resolver(c, tmp_path).resolve("XYZ.DE", "Some ETF", EV)
    assert e["ticker"] == "GER:ABC:EUR" and c.calls == ["XYZ.DE", "ABC.DE"]


def test_no_evidence_is_accepted_but_flagged(tmp_path):
    r = resolver(FakeClient({"V": chart("V", exch="NYQ")}), tmp_path)
    assert r.resolve("V.US", "Visa", [])["ticker"] == "NYSE:V"
    assert "bez weryfikacji" in r.notes[0]


def test_cache_hit_needs_no_network(tmp_path):
    resolver(FakeClient({"V": chart("V", exch="NYQ")}), tmp_path).resolve("V.US", "Visa", EV)
    c = FakeClient(fail=True)
    assert resolver(c, tmp_path).resolve("V.US", "Visa", EV)["ticker"] == "NYSE:V" and c.calls == []


def test_offline_uses_cache_only(tmp_path):
    resolver(FakeClient({"V": chart("V", exch="NYQ")}), tmp_path).resolve("V.US", "Visa", EV)
    off = TickerResolver(tmp_path / "cache.json", None)
    assert off.resolve("V.US", "Visa", EV) is not None
    assert off.resolve("MA.US", "Mastercard", EV) is None and "offline" in off.notes[0]


def test_network_failure_is_not_cached_and_trips_breaker(tmp_path):
    c = FakeClient(fail=True)
    r = resolver(c, tmp_path)
    for s in ("A.US", "B.US", "C.US", "D.US"):
        assert r.resolve(s, "", EV) is None
    assert len(c.calls) == 3 and "niedostępne" in r.notes[-1] and not (tmp_path / "cache.json").exists()


def test_unsupported_suffix_and_unknown_us_exchange(tmp_path):
    r = resolver(FakeClient({"ZZ": chart("ZZ", exch="XXX")}), tmp_path)
    assert r.resolve("ABC.XX", "", EV) is None
    assert r.resolve("ZZ.US", "", EV) is None
    assert "nieobsługiwany sufiks" in r.notes[0] and "nieznana giełda" in r.notes[1]


def test_verify_prices_window_and_ratio():
    ch = chart("X", lo="100", hi="110")
    assert verify_prices(ch, [(DAY, D("105")), (date(2026, 8, 29), D("109")), (date(2026, 1, 1), D("1"))]) == (2, 2)
    assert verify_prices(ch, [(DAY, D("50"))]) == (0, 1)


def _accounts():
    t = datetime(2026, 8, 28, 14, tzinfo=timezone.utc)
    d = AccountData("USD", "USD", "f.xlsx")
    d.operations = [Operation("USD", "Stock purchase", "Take-Two", "TTWO.US", t, D("-473.88"), "OPEN BUY 2/2.2 @ 236.94"),
                    Operation("USD", "Dividend", "Visa", "V.US", t, D("0.70"), "")]
    return {"USD": d}


def test_resolve_missing_skips_dictionary_entries_and_feeds_builder(tmp_path):
    tickers = TickerDict({"V.US": dict(xtb_symbol="V.US", ticker="NYSE:V", name="Visa", asset_class="Akcje zagraniczne", instrument_currency="USD")})
    c = FakeClient({"TTWO": chart("TTWO")})
    resolve_missing(_accounts(), tickers, resolver(c, tmp_path))
    assert c.calls == ["TTWO"] and tickers.has("TTWO.US")

    class Rates:
        errors: list = []

        def get(self, ccy, on):
            return D("4")
    res = build_rows(_accounts(), tickers, Rates())
    assert {r.ticker for r in res.rows} == {"NASDAQ:TTWO", "NYSE:V"} and not res.dict_notes


def test_end_to_end_without_any_dictionary_file(tmp_path, monkeypatch, capsys):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cash Operations"
    for _ in range(4):
        ws.append([None])
    ws.append(["Type", "Instrument", "Ticker", "Category", "Time", "Amount", "ID", "Comment", "Product", "Position ID"])
    ws.append(["Stock purchase", "Take-Two", "TTWO.US", "", datetime(2026, 8, 28, 14), -473.88, "1", "OPEN BUY 2 @ 236.94", "My Trades", ""])
    ws.append(["Total", None, None, None, None, -473.88])
    wb.save(tmp_path / "USD_1_2026.xlsx")
    with zipfile.ZipFile(tmp_path / "export.zip", "w") as z:
        z.write(tmp_path / "USD_1_2026.xlsx", "USD_1_2026.xlsx")

    class Rates:
        errors: list = []

        def get(self, ccy, on):
            return D("4.0000")
    monkeypatch.setattr(xtb2sheet, "HERE", tmp_path)                     # no tickers.csv, own cache
    monkeypatch.setattr(xtb2sheet, "NbpRates", lambda *_a, **_k: Rates())
    monkeypatch.setattr(xtb2sheet, "YahooClient", lambda: FakeClient({"TTWO": chart("TTWO")}))

    code = xtb2sheet.main([str(tmp_path / "export.zip"), "--from", "2026-01-01",
                           "--out", str(tmp_path / "o.tsv"), "--report", str(tmp_path / "r.md")])
    out = capsys.readouterr().out
    assert code == 0 and len(out.strip().splitlines()) <= 5
    assert (tmp_path / "o.tsv").read_text(encoding="utf-8").startswith("USD\t2026-08-28\tNASDAQ:TTWO\tUSD\tTake-Two")
    assert "NASDAQ:TTWO" in (tmp_path / "r.md").read_text(encoding="utf-8") and (tmp_path / "cache" / "tickers_cache.json").exists()
    assert "tickerów rozpoznanych automatycznie" in out


def test_yahoo_client_parses_chart_response(monkeypatch):
    from core.ticker_resolver import YahooClient
    payload = {"chart": {"result": [{"meta": {"currency": "GBp", "exchangeName": "LSE", "instrumentType": "ETF",
                                              "longName": "Invesco EQQQ", "gmtoffset": 3600},
                                     "timestamp": [1787900400, 1787986800],
                                     "indicators": {"quote": [{"low": [23000.5, None], "high": [24000, 24100]}]}}], "error": None}}
    c = YahooClient(pause=0)
    monkeypatch.setattr(c, "_get", lambda url: payload)
    ch = c.chart("EQQQ.L", date(2026, 8, 25), date(2026, 8, 30))
    assert (ch.currency, ch.exchange, ch.instrument_type, ch.name) == ("GBp", "LSE", "ETF", "Invesco EQQQ")
    assert len(ch.days) == 1 and ch.days[0][1:] == (D("23000.5"), D("24000"))
    monkeypatch.setattr(c, "_get", lambda url: None)
    assert c.chart("NOPE", date(2026, 8, 25), date(2026, 8, 30)) is None
