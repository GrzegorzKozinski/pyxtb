"""Transactions from the XTB export (spec §6, §7): merged trades, dividends, deposits, conversions, IKE transfers.

Shared by the sheet export and the (future) dashboard; the TSV formatting lives in sheet/tsv.py."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from core.reader import ACCOUNT_ORDER, AccountData, Operation, parse_trade_comment
from core.tickers import TickerDict

FOREIGN_CCY_ACCOUNTS = ("EUR", "USD")
PLN_ACCOUNTS = ("XTB", "Z_IKE")
CASH_KINDS = ("deposit", "transfer", "withdrawal", "ike_deposit")
CONVERSION_TOLERANCE = timedelta(seconds=2)     # PLN and EUR/USD side of a conversion share the timestamp
DEPOSIT_LOOKBACK_DAYS = 10                      # PLN deposit consumed by a conversion
IKE_PAIR_DAYS = 3
ZERO = Decimal(0)


def q(d: Decimal, places: int) -> Decimal:
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _times(amount: Decimal, rate: Decimal | None) -> Decimal | None:
    return q(amount * rate, 2) if rate is not None else None


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f").replace(".", ",")


def _ccy_amount(d: Decimal, round_to_int: bool = False) -> str:
    """'260', '8', '260.5' (dot decimal, as in the sheet's existing comments)."""
    return format((q(d, 0) if round_to_int else d).normalize(), "f")


def fifo_key_tag(account: str, ticker: str, asset_class: str, currency: str) -> str:
    """Konto##TICKER##Klasa##Waluta = FIFO key of przeliczPortfolio (§10)."""
    t = ticker if (ticker == "Gotówka" or ticker.startswith("Waluty_")) else ticker.upper()
    return f"{account}##{t}##{asset_class}##{currency}"


@dataclass
class SheetRow:
    account: str
    date: date
    ticker: str
    currency: str
    name: str
    asset_class: str
    kind: str                       # Zakup / Sprzedaż / Dywidenda / Odsetki / Wpłata środków / Wypłata środków
    qty: Decimal
    price: Decimal
    rate: Decimal | None            # None = unavailable, the field stays empty
    total: Decimal | None
    sort_time: datetime
    info: str | None = None         # comment: tax paid / currency amount
    warnings: list[str] = field(default_factory=list)
    nbp_used: bool = False          # rate comes from NBP
    review: str | None = None       # why the row needs a manual check (reported)
    fee: Decimal = ZERO
    nominal: Decimal = Decimal(1)

    @property
    def tag(self) -> str:
        return fifo_key_tag(self.account, self.ticker, self.asset_class, self.currency)


@dataclass
class BuildResult:
    rows: list[SheetRow] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)      # automatic decisions (reported explicitly)
    warnings: list[str] = field(default_factory=list)
    dict_notes: list[str] = field(default_factory=list)     # to be added to the ticker dictionary
    skipped: dict[str, list] = field(default_factory=lambda: defaultdict(list))


def build_rows(accounts: dict[str, AccountData], tickers: TickerDict, rates,
               cutoffs: dict[str, date] | None = None) -> BuildResult:
    """`rates.get(ccy, day)` -> NBP rate or None; `rates.errors` -> reasons it was unavailable.
    `cutoffs`: per-account first date (inclusive); missing account = whole history."""
    return _Builder(accounts, tickers, rates, cutoffs or {}).run()


class _Builder:
    def __init__(self, accounts, tickers, rates, cutoffs):
        self.accounts, self.tickers, self.rates, self.cutoffs = accounts, tickers, rates, cutoffs
        self.res = BuildResult()
        self._dict_notes: dict[str, None] = {}

    # ---------- helpers ----------

    def warn(self, msg: str) -> None:
        self.res.warnings.append(msg)

    def in_range(self, op: Operation) -> bool:
        cut = self.cutoffs.get(op.account)
        return cut is None or op.date >= cut

    def nbp(self, ccy: str, day: date) -> Decimal | None:
        r = self.rates.get(ccy, day)
        return q(r, 4) if r is not None else None

    def instrument(self, symbol: str, export_name: str):
        inst = self.tickers.resolve(symbol, export_name)
        self._dict_notes.update(dict.fromkeys(inst.report_notes))
        return inst

    def cash_row(self, op: Operation, kind: str, total: Decimal | None, rate: Decimal | None, **kw) -> None:
        self.res.rows.append(SheetRow(
            account=op.account, date=op.date, ticker="Gotówka", currency="PLN", name="Gotówka", asset_class="Gotówka",
            kind=kind, qty=Decimal(1), price=Decimal(1), rate=rate, total=total, sort_time=op.time, **kw))

    # ---------- main ----------

    def run(self) -> BuildResult:
        trades, dividends, cash = defaultdict(list), defaultdict(list), defaultdict(list)
        for data in self.accounts.values():
            for op in data.operations:
                if op.kind in CASH_KINDS:
                    cash[op.kind].append(op)            # matched over the whole history, filtered later
                elif self.in_range(op):
                    self._collect(op, trades, dividends)
        for (acc, symbol, side, day), lines in trades.items():
            self._trade(acc, symbol, side, day, lines)
        for (acc, symbol, day), ops in dividends.items():
            self._dividend(acc, symbol, day, ops)
        self._conversions_and_deposits(cash)
        self._withdrawals(cash["withdrawal"])
        self._ike(cash["ike_deposit"])

        res = self.res
        res.warnings.extend(e for e in getattr(self.rates, "errors", []) if e not in res.warnings)
        res.rows.sort(key=lambda r: (r.date, ACCOUNT_ORDER.index(r.account), r.sort_time))
        res.dict_notes = list(self._dict_notes)
        return res

    def _collect(self, op: Operation, trades, dividends) -> None:
        acc, kind, where = op.account, op.kind, f"{op.account} {op.date} {op.ticker}"
        if kind == "trade":
            tc = parse_trade_comment(op.comment)
            if tc is None:
                self.warn(f"{where}: nieparsowalny komentarz transakcji {op.comment!r} – pominięto")
            elif not tc.supported:
                self.warn(f"{where}: pozycja krótka ({op.comment}) – nieobsługiwane, pominięto")
            else:
                side = "Zakup" if op.type.lower() == "stock purchase" else "Sprzedaż"
                if tc.action != ("OPEN" if side == "Zakup" else "CLOSE"):
                    self.warn(f"{where}: typ „{op.type}” niezgodny z komentarzem „{op.comment}” – pominięto")
                else:
                    trades[(acc, op.ticker, side, op.date)].append((op, tc))
        elif kind == "dividend":
            if op.ticker:
                dividends[(acc, op.ticker, op.date)].append(op)
            else:
                self.warn(f"{acc} {op.date}: {op.type} bez symbolu instrumentu – pominięto")
        elif kind == "unknown":
            self.warn(f"{acc} {op.date}: nieznany typ operacji „{op.type}” ({op.amount}) – brak wiersza")
        elif kind == "skip":
            self.res.skipped[f"{acc}|{op.type}"].append(op)

    # ---------- purchases / sales (§7.1) ----------

    def _trade(self, acc: str, symbol: str, side: str, day: date, lines: list) -> None:
        sub = self.accounts[acc].currency
        inst = self.instrument(symbol, lines[0][0].instrument)
        qty = sum((tc.qty for _, tc in lines), ZERO)
        if qty == 0:
            self.warn(f"{acc} {day} {symbol}: zerowa ilość – pominięto")
            return
        sum_abs = sum((abs(op.amount) for op, _ in lines), ZERO)
        weighted = sum((tc.qty * tc.price for _, tc in lines), ZERO)
        nbp_used = False

        if acc in FOREIGN_CCY_ACCOUNTS:
            if (inst.currency or sub) != sub:
                self.warn(f"{acc} {day} {symbol}: instrument w {inst.currency} na subkoncie {sub} – nieobsługiwane, pominięto")
                return
            ccy, price = sub, sum_abs / qty
            rate = self.nbp(sub, day)
            total, nbp_used = _times(sum_abs, rate), True
        else:                       # XTB / Z_IKE: always the real PLN from the export, no NBP
            ccy, price = inst.currency, weighted / qty
            total = q(sum_abs, 2)
            if ccy == "PLN":
                rate = Decimal(1)
            else:
                rate = q(sum_abs / weighted, 4) if weighted > 0 else None

        if len(lines) > 1:
            self.res.decisions.append(f"Scalono {len(lines)} linii zlecenia: {acc} {day} {side} {inst.ticker} → {_fmt(qty)} szt.")
        self.res.rows.append(SheetRow(
            account=acc, date=day, ticker=inst.ticker, currency=ccy, name=inst.name, asset_class=inst.asset_class,
            kind=side, qty=qty, price=price, rate=rate, total=total, sort_time=min(op.time for op, _ in lines),
            warnings=list(inst.row_warnings), nbp_used=nbp_used))

    # ---------- dividends (§7.2) ----------

    def _dividend(self, acc: str, symbol: str, day: date, ops: list[Operation]) -> None:
        sub = self.accounts[acc].currency
        divs = [o for o in ops if o.type.lower() == "dividend"]
        net = sum((o.amount for o in ops), ZERO)
        if not divs:
            self.warn(f"{acc} {day} {symbol}: Withholding tax bez Dividend – brak wiersza")
            return
        if net <= 0:
            self.warn(f"{acc} {day} {symbol}: dywidenda netto ≤ 0 ({net}) – brak wiersza")
            return
        tax = -sum((o.amount for o in ops if o.type.lower() != "dividend"), ZERO)
        inst = self.instrument(symbol, ops[0].instrument)
        if acc in FOREIGN_CCY_ACCOUNTS:
            rate = self.nbp(sub, day)
            total, nbp_used = _times(net, rate), True
        else:
            rate, total, nbp_used = Decimal(1), q(net, 2), False
        if len(divs) > 1:
            self.res.decisions.append(f"Scalono {len(divs)} linii dywidendy {inst.ticker} z {day} ({acc}) w jeden wiersz netto {net} {sub}")
        self.res.rows.append(SheetRow(
            account=acc, date=day, ticker=inst.ticker, currency=sub, name=inst.name, asset_class=inst.asset_class,
            kind="Dywidenda / Odsetki", qty=Decimal(1), price=Decimal(1), rate=rate, total=total,
            sort_time=min(o.time for o in ops), info=f"Pobrany podatek: {q(tax, 2)} {sub}",
            warnings=list(inst.row_warnings), nbp_used=nbp_used))

    # ---------- cash: conversions, deposits (§7.3 - §7.5) ----------

    def _conversions_and_deposits(self, cash: dict[str, list[Operation]]) -> None:
        """PLN -> EUR/USD conversions are matched over the whole history; the PLN deposit that funded a
        conversion is consumed (no row), the conversion becomes one deposit row on the EUR/USD account."""
        transfers, deposits = cash["transfer"], cash["deposit"]
        foreign_in = sorted((o for o in transfers if o.account in FOREIGN_CCY_ACCOUNTS and o.amount > 0), key=lambda o: o.time)
        pln_out = [o for o in transfers if o.account == "XTB" and o.amount < 0]
        pln_deposits = [o for o in deposits if o.account == "XTB" and o.amount > 0]
        used_out: set[Operation] = set()
        consumed: set[Operation] = set()
        handled: set[Operation] = set()

        for fo in foreign_in:
            ccy = fo.account
            handled.add(fo)
            partner = self._conversion_partner(fo, pln_out, used_out)
            deposit = None
            if partner is not None:
                used_out.add(partner)
                handled.add(partner)
                cands = [d for d in pln_deposits if d not in consumed and d.amount == abs(partner.amount)
                         and fo.date - timedelta(days=DEPOSIT_LOOKBACK_DAYS) <= d.date <= fo.date]
                if cands:
                    deposit = max(cands, key=lambda d: d.time)
                    consumed.add(deposit)
            fo_in = self.in_range(fo)
            if not (fo_in or (deposit is not None and self.in_range(deposit))):
                continue

            label = f"Przewalutowanie {_fmt(abs(partner.amount)) if partner else '?'} PLN→{_fmt(fo.amount)} {ccy} ({fo.date})"
            info = f"{_ccy_amount(fo.amount, True)} {ccy}"
            if partner is None:
                self.warn(f"{label}: brak pary po stronie PLN (Transfer ±2 s) – kurs NBP, do poprawienia")
                if fo_in:
                    rate = self.nbp(ccy, fo.date)
                    self.cash_row(fo, "Wpłata środków", _times(fo.amount, rate), rate, info=info, nbp_used=rate is not None,
                                  review="przewalutowanie bez pary PLN – kurs NBP")
                continue
            pln = abs(partner.amount)
            rate = q(pln / fo.amount, 4)
            if fo_in:
                self.cash_row(fo, "Wpłata środków", q(pln, 2), rate, info=info)
            if deposit is not None:
                self.res.decisions.append(f"{label}: pominięto wpłatę XTB {_fmt(deposit.amount)} PLN z {deposit.date}"
                                          + (f", wiersz {ccy}: kurs {_fmt(rate)}" if fo_in else " (wiersz walutowy poza zakresem)"))
            else:
                self.res.decisions.append(f"{label}: brak wpłaty XTB na {_fmt(pln)} PLN w {DEPOSIT_LOOKBACK_DAYS} dni przed – niczego nie pominięto, wiersz {ccy}: kurs {_fmt(rate)}")
                self.warn(f"{label}: nie znaleziono pasującej wpłaty PLN na XTB")

        for t in transfers:
            if t not in handled and self.in_range(t):
                what = ("przewalutowanie odwrotne/nieobsługiwane" if t.account in FOREIGN_CCY_ACCOUNTS or t.amount > 0
                        else "Transfer PLN bez pary po stronie walutowej")
                self.warn(f"{t.account} {t.date}: {what} ({_fmt(t.amount)}) – brak wiersza")

        for d in sorted(deposits, key=lambda o: o.time):
            if d in consumed or not self.in_range(d):
                continue
            if d.amount <= 0:
                self.warn(f"{d.account} {d.date}: Deposit o kwocie ≤ 0 ({_fmt(d.amount)}) – brak wiersza")
            elif d.account in PLN_ACCOUNTS:
                self.cash_row(d, "Wpłata środków", q(d.amount, 2), Decimal(1))
            else:       # bank deposit in EUR/USD: the real exchange rate is unknown, NBP is an approximation
                rate = self.nbp(d.account, d.date)
                self.cash_row(d, "Wpłata środków", _times(d.amount, rate), rate, info=f"{_ccy_amount(d.amount)} {d.account}",
                              nbp_used=rate is not None, review="wpłata z banku – kurs NBP zamiast rzeczywistego kursu wymiany")

    @staticmethod
    def _conversion_partner(fo: Operation, pln_out: list[Operation], used: set[Operation]) -> Operation | None:
        best = None
        for t in pln_out:
            if t in used:
                continue
            diff = abs(t.time - fo.time)
            m = re.search(r"PLN to ([A-Z]{3})", t.comment or "")
            if diff <= CONVERSION_TOLERANCE and (not m or m.group(1) == fo.account) and (best is None or diff < best[0]):
                best = (diff, t)
        return best[1] if best else None

    # ---------- withdrawals (§7.6) ----------

    def _withdrawals(self, withdrawals: list[Operation]) -> None:
        for w in sorted(withdrawals, key=lambda o: o.time):
            if not self.in_range(w):
                continue
            if w.amount >= 0:
                self.warn(f"{w.account} {w.date}: Withdrawal o kwocie ≥ 0 ({_fmt(w.amount)}) – brak wiersza")
            elif w.account in PLN_ACCOUNTS:
                self.cash_row(w, "Wypłata środków", q(-w.amount, 2), Decimal(1))
            else:
                rate = self.nbp(w.account, w.date)
                self.cash_row(w, "Wypłata środków", _times(-w.amount, rate), rate, info=f"{_ccy_amount(-w.amount)} {w.account}",
                              nbp_used=rate is not None, review="wypłata walutowa – kurs NBP")

    # ---------- XTB <-> IKE transfers (§7.7); paired over the whole history ----------

    def _ike(self, ike_ops: list[Operation]) -> None:
        outs = sorted((o for o in ike_ops if o.account == "XTB"), key=lambda o: o.time)
        ins = [o for o in ike_ops if o.account == "Z_IKE"]
        used_in: set[Operation] = set()
        paired_out: set[Operation] = set()
        for o in outs:
            if o.amount >= 0:
                continue
            cands = [i for i in ins if i not in used_in and i.amount == -o.amount and abs((i.date - o.date).days) <= IKE_PAIR_DAYS]
            i = min(cands, key=lambda x: abs(x.time - o.time), default=None)
            if i is None:
                continue
            used_in.add(i)
            paired_out.add(o)
            if self.in_range(o) or self.in_range(i):
                sides = [s for s, op in (("Wypłata XTB", o), ("Wpłata Z_IKE", i)) if self.in_range(op)]
                self.res.decisions.append(f"Para IKE {_fmt(i.amount)} PLN: XTB {o.date} → Z_IKE {i.date}; wiersze: {', '.join(sides)}")

        for op in ike_ops:
            if not self.in_range(op):
                continue
            sign = -1 if op.account == "XTB" else 1
            if op.account not in PLN_ACCOUNTS or op.amount * sign <= 0:
                self.warn(f"{op.account} {op.date}: IKE deposit o nieoczekiwanym znaku ({_fmt(op.amount)}) – brak wiersza")
                continue
            if op not in (paired_out if op.account == "XTB" else used_in):
                self.warn(f"{op.account} {op.date}: IKE deposit {_fmt(op.amount)} bez pary po drugiej stronie (kwota/data) – wiersz wyemitowany")
            if op.account == "XTB":
                self.cash_row(op, "Wypłata środków", q(-op.amount, 2), Decimal(1))
            else:
                self.cash_row(op, "Wpłata środków", q(op.amount, 2), Decimal(1))
