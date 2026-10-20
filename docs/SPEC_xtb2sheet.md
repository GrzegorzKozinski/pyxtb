# Specyfikacja: `xtb2sheet` – eksport XTB → wiersze do zakładki „Transakcje”

Status: **gotowa do implementacji** (stan na 2026-10-04). Nic nie zostało jeszcze zaimplementowane.
Dane referencyjne: eksporty XTB z zakresu 2006-01-01 … 2026-09-19 oraz arkusz `portfolio-transakcje.csv` (372 wiersze).

---

## 1. Cel i zakres

Skrypt CLI (Python, Windows) przyjmuje **zip z eksportami XTB** i zwraca **TSV gotowy do wklejenia** (kopiuj-wklej) w zakładce `Transakcje` arkusza Google, plus **raport kontrolny**.

- Czytana jest wyłącznie zakładka **Cash Operations** (pozycje otwarte/zamknięte wynikają z transakcji).
- W zakresie: konta XTB (PLN), EUR, USD, IKE. **ZEDO (obligacje) poza zakresem.**
- Poza zakresem: obligacje, splity, DRIP, krypto, instrumenty w walucie innej niż waluta subkonta (z wyjątkiem kont PLN/IKE – patrz §7.1).
- Wklejone wiersze muszą być przetwarzalne przez istniejące skrypty arkusza (§11) – to twarde ograniczenie.

## 2. Środowisko i CLI

- Python 3.11+ na Windows. Zależności: `openpyxl`, `tzdata` (Windows nie ma bazy stref czasowych dla `zoneinfo`), opcjonalnie `pyperclip`. HTTP do NBP przez `urllib` (bez dodatkowych zależności).
- Testy: `pytest`.

```
python xtb2sheet.py eksport.zip --portfolio portfolio-transakcje.csv
        [--od KONTO=RRRR-MM-DD ...]   # nadpisanie daty granicznej (włącznie)
        [--slownik tickers.csv]       # domyślnie obok skryptu
        [--out wynik.tsv] [--raport raport.md]
        [--bez-schowka]               # domyślnie wynik trafia też do schowka
        [--audyt]                     # tryb opcjonalny, §13
```

Interakcja: skrypt pyta w terminalu o wpłaty walutowe z banku (§7.4). Wynik: `wynik.tsv` + kopia w schowku + `raport.md`; podsumowanie ostrzeżeń także w terminalu.

## 3. Wejście

### 3.1 Zip i rozpoznawanie subkonta
Zip zawiera 4 pliki `.xlsx`. **Konto rozpoznaje się po prefiksie nazwy pliku**, nie po numerze w komórce B1 (w pliku IKE zakładka *Open Positions* ma w B1 numer konta PLN – B1 jest niewiarygodne).

| Prefiks pliku | Wartość w kolumnie `Konto` | Waluta subkonta |
|---|---|---|
| `PLN_` | `XTB` | PLN |
| `EUR_` | `EUR` | EUR |
| `USD_` | `USD` | USD |
| `IKE_` | `Z_IKE` | PLN |

Przykładowe nazwy: `USD_50539843_2006-01-01_2026-09-19.xlsx`. Nieznany prefiks → ostrzeżenie i pominięcie pliku.

### 3.2 Zakładka Cash Operations
Wiersze 1–4: metadane (konto, zakres dat). **Wiersz 5 = nagłówki**, dane od wiersza 6. Kolumny:
`Type, Instrument, Ticker, Category, Time, Amount, ID, Comment, Product, Position ID`.

W prawdziwym xlsx: `Time` = `datetime` (UTC, z ms), `Amount` = `float`, `ID` = tekst. (W CSV z Google Sheets kwoty mają przecinek dziesiętny i cudzysłowy – skrypt ma czytać xlsx, ale parser kwot powinien też znieść CSV, bo testy mogą go używać.)

Ostatni wiersz ma `Type = Total` i `Amount` = saldo gotówki. **Zweryfikowano**, że w czterech plikach saldo = suma wszystkich Amount (PLN 34,09; EUR 18,17; USD 31,71; IKE 47,36). Wiersz `Total` pomijamy w przetwarzaniu, ale używamy do kontroli (§12).

`Product` przyjmuje wartości `My Trades`, `Investment Plans`, `IKE`. Operacje z `Investment Plans` należą do tego samego subkonta i są normalnymi transakcjami (np. zakupy ETF i dywidendy z planów inwestycyjnych są w arkuszu).

### 3.3 Typy operacji (`Type`) – obsługa

| Type | Postępowanie |
|---|---|
| `Stock purchase`, `Stock sell` | Zakup / Sprzedaż (§7.1) |
| `Dividend`, `Withholding tax` | Dywidenda netto (§7.2) |
| `Deposit` (PLN) | Wpłata środków XTB (§7.3) |
| `Deposit` (EUR/USD, bank) | Wpłata z pytaniem o kurs (§7.4) |
| `Transfer` (przewalutowanie PLN→EUR/USD) | Wpłata na subkoncie walutowym, bez wierszy po stronie PLN (§7.5) |
| `Withdrawal` | Wypłata środków (§7.6) |
| `IKE deposit` | Para Wypłata (XTB) + Wpłata (Z_IKE) (§7.7) |
| `Subaccount transfer` | Pomijany (przesunięcie My Trades ↔ Investment Plans w obrębie jednego konta, saldo netto 0); tylko w raporcie |
| `Free funds interest`, `Free funds interest tax`, `SEC fee` | Pomijane (kwoty pomijalne – decyzja użytkownika); tylko w raporcie z liczbą i sumą |
| `Total` | Kontrola salda |
| inne / nieznane | Brak wiersza, ostrzeżenie w raporcie (nie przerywać pracy) |

## 4. Wyjście

### 4.1 Kolumny (zakładka Transakcje, A–N, wszystkie to **wartości**, nie formuły)

| Kol. | Nagłówek | Zawartość |
|---|---|---|
| A | Konto | `XTB`, `EUR`, `USD`, `Z_IKE` |
| B | Data | `RRRR-MM-DD` |
| C | Ticker | format arkusza (np. `NASDAQ:TTWO`, `GER:VUAA:EUR`, `WSE:KRU`) albo `Gotówka` |
| D | Waluta | waluta instrumentu; dla wierszy gotówkowych **PLN**; dla dywidend – waluta subkonta |
| E | Nazwa | ze słownika (§9) |
| F | Klasa aktywów | `Akcje polskie`, `Akcje zagraniczne`, `Gotówka` |
| G | Rodzaj transakcji | `Zakup`, `Sprzedaż`, `Wpłata środków`, `Wypłata środków`, `Dywidenda / Odsetki` |
| H | Liczba | ilość (ułamkowa dozwolona, do 8 miejsc, bez końcowych zer) |
| I | Cena | cena jednostkowa (3 miejsca – interpretacja odpowiedzi użytkownika, §16) |
| J | Prowizje | `0` |
| K | Kurs PLN transakcji | 4 miejsca |
| L | Cena nominalna | `1` |
| M | Total PLN | 2 miejsca |
| N | Komentarz | §10 |

### 4.2 Format TSV
- Separator: **tabulator** (TSV = jak CSV, ale kolumny oddziela tabulator; po wklejeniu Google Sheets sam rozdziela kolumny, a przecinek dziesiętny nie koliduje z separatorem).
- Liczby z **przecinkiem dziesiętnym**, bez separatora tysięcy i bez „zł” (arkusz ma polskie ustawienia regionalne). Daty `RRRR-MM-DD`.
- Kodowanie UTF-8 (komentarze zawierają emoji ⚠️). Bez wiersza nagłówka (wklejanie pod istniejące dane).
- Kolejność: **data rosnąco**, potem konto, potem czas z eksportu. W tym samym dniu Zakup przed Sprzedażą (arkusz i tak sortuje to w `transactionsSort`).

### 4.3 Data
Czas w eksporcie jest w UTC; datę liczyć w strefie `Europe/Warsaw` (użytkownik: różnica bez znaczenia, ale koszt zerowy). Operacje handlowe nie wypadają w okolicy północy.

## 5. Zakres dat (dedupe)

Eksport zawsze obejmuje całą historię, więc skrypt filtruje po dacie – **osobno dla każdego konta**.

- Domyślna data graniczna dla konta = **dzień po ostatniej dacie danego konta w `portfolio-transakcje.csv`** (parametr `--portfolio`).
- Aktualne ostatnie daty: `XTB 2026-07-24`, `USD 2026-08-28`, `EUR 2026-09-11`, `Z_IKE 2026-08-11`.
- `--od KONTO=DATA` nadpisuje (interpretacja: **od daty włącznie**).
- Operacje z samego ostatniego dnia wpisu wylistować w raporcie jako „do ręcznego sprawdzenia” (możliwe częściowo wpisane zlecenia).
- Każda strona pary (np. IKE: Wypłata po stronie XTB i Wpłata po stronie Z_IKE) jest filtrowana własną datą graniczną konta.

Oczekiwany wynik pierwszego uruchomienia na danych referencyjnych (domyślne daty): 2 dywidendy USD (Visa 2026-09-01, FactSet 2026-09-17), 1 dywidenda XTB (Grupa Kęty 2026-09-03), 1 Wypłata środków XTB 400 zł (2026-08-10; strona IKE już jest w arkuszu). EUR i Z_IKE: brak nowych wierszy.

## 6. Parsowanie transakcji (z `Comment`)

Format: `(OPEN|CLOSE) (BUY|SELL) <ilość>[/<ilość_zlecenia>] @ <cena>`.
- `OPEN BUY` = zakup; `CLOSE BUY` = sprzedaż pozycji kupionej (typ eksportu `Stock sell`).
- Przykłady: `OPEN BUY 2/2.2 @ 236.94`, `CLOSE BUY 0.9803/1.9803 @ 45.13`, `OPEN BUY 25/25.6802 @ 46.345`. Liczba przed `/` to ilość tej linii (częściowa realizacja), po `/` – suma zlecenia.
- `OPEN SELL` / pozycje krótkie: nieobsługiwane → ostrzeżenie.

**Łączenie zleceń w jeden wiersz** (wybór użytkownika): klucz = *subkonto + instrument + kierunek + dzień*. Liczba = suma ilości; Cena = średnia ważona = Σ|Amount| / Σilość (dla instrumentu w walucie subkonta). Przykład: TTWO 2026-08-28: 2 + 0,2 szt. → 2,2 szt., Cena ≈ 236,98.

## 7. Budowa wierszy

### 7.1 Zakup / Sprzedaż

| Przypadek | Waluta (D) | Cena (I) | Kurs (K) | Total (M) |
|---|---|---|---|---|
| Instrument w walucie subkonta EUR/USD | waluta instrumentu | średnia ważona | **kurs NBP** (§8) | Σ\|Amount\| × Kurs |
| Subkonto PLN/IKE, instrument w PLN | PLN | z komentarza | 1,00 | Σ\|Amount\| |
| Subkonto PLN/IKE, **instrument zagraniczny** (np. `IS3N.DE` z IKE, `EQQQ.UK` z XTB) | ze słownika (EUR/GBP) | z komentarza, w walucie instrumentu | **Total / Σ(ilość × cena)** | **realne PLN z eksportu**, Σ\|Amount\| |
| Subkonto walutowe, instrument w innej walucie niż subkonto | – | – | – | nieobsługiwane → ostrzeżenie, bez wiersza |

Liczba = suma ilości; Prowizje = 0; Cena nominalna = 1; Ticker/Nazwa/Klasa ze słownika.

Dla zakupów z konta PLN/IKE użytkownik wybrał „zawsze realne PLN z eksportu, bez NBP” (np. IKE 2026-06-11: 138,65 + 197,42 = 336,07 zł, a nie 333,99 z NBP).

### 7.2 Dywidenda (`Dywidenda / Odsetki`)
- Łączymy `Dividend` + `Withholding tax` tego samego instrumentu w tym samym dniu na tym subkoncie → **jeden wiersz netto**.
- Konto/Waluta: waluta **subkonta** (np. `USD` / `USD`; dla XTB zawsze `PLN`, także dla dywidend ETF-ów zagranicznych, np. `LON:EQQQ` / `PLN`).
- Liczba = 1, Cena = 1, Prowizje = 0, Cena nominalna = 1.
- Kurs: konto PLN/IKE → 1,00; konto EUR/USD → **NBP** (§8).
- Total = netto (Dividend + Withholding tax) × Kurs. Przykład: Visa 2026-09-01: 0,70 − 0,10 = 0,60 USD.
- Komentarz: `Pobrany podatek: X.XX WAL` (kropka dziesiętna; WAL = waluta subkonta; §10).
- `Withholding tax` bez `Dividend` lub netto ≤ 0 → ostrzeżenie, bez wiersza.
- Uwaga: w arkuszu bywają trzy osobne dywidendy tego samego papieru w jednym dniu (ELT, 2024-09-04); skrypt łączy je w jeden wiersz (raport podaje liczbę scalonych linii).

### 7.3 Wpłata PLN na subkonto XTB / Z_IKE
`Wpłata środków`, Ticker=`Gotówka`, Nazwa=`Gotówka`, Klasa=`Gotówka`, **Waluta=PLN** (celowo – XIRR i wartości kont liczone w jednej jednostce), Liczba=1, Cena=1, Kurs=1,00, Total = kwota. Wyjątek: wpłata pokryta przewalutowaniem (§7.5).

### 7.4 Wpłata walutowa z banku (EUR/USD, np. Pekao)
Skrypt **pyta w terminalu** (dla każdej takiej wpłaty): *„Wpłata 260 EUR z 2026-08-05 – ile zł zapłaciłeś i ile EUR otrzymałeś w wymianie?”*.
- Kurs = zapłacone PLN / otrzymana waluta (użytkownik zaokrągla i nie wpłaca całości wymienionej kwoty).
- Wiersz: `Wpłata środków`, Ticker/Nazwa/Klasa=`Gotówka`, Waluta=PLN, Liczba=1, Cena=1, Kurs=ten kurs (od teraz **zawsze rzeczywisty**; starsze wiersze mają czasem 1,00), **Total = kwota z eksportu × Kurs**.
- Komentarz zawiera kwotę w walucie, np. `260 EUR` (waluta, nie „zł” – stare wpisy typu `240 zł` to błąd).
- Brak odpowiedzi (Enter) → kurs NBP, wiersz oznaczony w raporcie jako „do poprawienia”.

### 7.5 Przewalutowanie PLN → EUR/USD (`Transfer`)
Eksport: po stronie EUR/USD `Transfer` z dodatnią kwotą, po stronie PLN `Transfer` z ujemną; komentarz: `Currency conversion, PLN to EUR from TA: … to: …, Exchange rate:0.232885`. Strony mają ten sam czas (±2 s).
- Wiersz po stronie EUR/USD: `Wpłata środków`, Waluta=PLN, Total = **zapisana kwota PLN z eksportu** (np. 1000,00), Kurs = Total / kwota waluty (np. 1000 / 232,88 ≈ 4,2942), Komentarz `233 EUR`.
- Po stronie PLN: **nie generować wierszy** – ani dla `Transfer`, ani dla powiązanej wpłaty BLIK/PayU na XTB (użytkownik tak dziś robi; wpłata PLN jest „zużyta” przez przewalutowanie).
- Powiązanie wpłaty PLN z przewalutowaniem: ta sama kwota, wpłata do 10 dni wcześniej (przykłady: 2025-04-25 – tego samego dnia; 2025-01-17 → 2025-01-20), każda wpłata zużywana raz.
- **Jawność (wymóg użytkownika):** każdą automatyczną decyzję zalogować w terminalu i w raporcie, np. *„Przewalutowanie 1200 PLN→292,02 USD (2025-01-20): pominięto wpłatę XTB 1200 PLN z 2025-01-17, wiersz USD: kurs 4,1092”*.
- Brak pary lub różne kwoty → ostrzeżenie; emitować wiersz walutowy, wpłaty PLN nie pomijać.
- Przewalutowanie odwrotne (EUR/USD → PLN): w v1 tylko ostrzeżenie.

### 7.6 Wypłata środków
`Withdrawal` → `Wypłata środków` (Gotówka/PLN, Liczba=1, Cena=1). Konto PLN: Kurs=1, Total=kwota. Konto walutowe (np. USD 8,00): Kurs=NBP, oznaczyć „do poprawienia”.
Uwaga: arkusz zawiera tylko 3 z 15 wypłat z eksportu. Decyzja użytkownika: skoro eksport jest źródłem prawdy – generować każdą (nieścisłości historyczne ujawni tryb audytu).

### 7.7 Przelewy XTB ↔ IKE (`IKE deposit`)
W pliku `PLN_` kwota ujemna (`Transfer out operation on account…`), w `IKE_` dodatnia (`Transfer in operation…`). Dla każdej pary **w dokładnej kwocie** (np. 5074,21):
- `Wypłata środków` na `XTB` (Gotówka/PLN, Kurs 1,00, Total = kwota),
- `Wpłata środków` na `Z_IKE` (jw.).
Wpłata z banku na XTB, z której pochodzi przelew, jest osobnym wierszem (nie kompensować, jak w arkuszu 2026-06-11). Każda strona filtrowana własną datą graniczną konta.

## 8. Kursy NBP

Źródło: `https://api.nbp.pl/api/exchangerates/rates/a/<WAL>/<od>/<do>/?format=json` (tabela A, `mid`).
- Reguła (wybór użytkownika, zgodna z PIT-38): **kurs z ostatniego dnia roboczego poprzedzającego dzień transakcji** (a nie z dnia transakcji).
- Implementacja wzorowana na funkcji `currency_rates` z `daneGieldowe.txt`: zapytanie o zakres `[data−6, data−1]`, wybrać ostatni element; w Pythonie bezpieczniej zapytać o ~10 dni i wziąć najpóźniejszy wpis o `effectiveDate < data transakcji`. Zaokrąglenie: 4 miejsca.
- Cache kursów w pliku (`nbp_cache.json`); retry z timeoutem; błąd sieci → wiersz z pustym Kursem i ostrzeżeniem (nie przerywać).
- Używany dla: dywidend na kontach EUR/USD, zakupów/sprzedaży w EUR/USD, domyślnie dla wpłat bez odpowiedzi.
- **Przypomnienie w raporcie:** lista wszystkich wierszy z kursem NBP wstawionym domyślnie („zweryfikuj ręcznie”) – użytkownik poprawia kursy ręcznie w arkuszu.
- Uwaga testowa: środowisko Claude nie ma dostępu do `api.nbp.pl` (nie ma domeny na liście); logikę testować na mocku, a test z prawdziwym NBP wykona użytkownik.

## 9. Słownik tickerów

Plik `tickers.csv` (start: `tickers_seed.csv` zbudowany z arkusza): `xtb_symbol, ticker, nazwa, klasa, waluta_instrumentu`.

- Giełda (NYSE/NASDAQ, GER/FRA/ETR, LON/LSE) **nie wynika z eksportu** – musi być w słowniku.
- Nazwy **ze słownika** (nie z kolumny `Instrument` eksportu). Pusta nazwa → fallback na `Instrument` z eksportu + ostrzeżenie. W arkuszu 5 instrumentów ma jako Nazwa sam ticker (CCC, CTRA, EQEU, SPL, SXR8) – w seedzie pola puste do uzupełnienia.
- `waluta_instrumentu` jest stała dla instrumentu (np. `EQQQ.UK` = GBP, `IUIT.UK` = USD, `QDV5.DE` = EUR). Wiersz `LON:EQQQ / PLN` w arkuszu to dywidenda na koncie XTB, nie waluta instrumentu.
- Klasa: `.PL` → `Akcje polskie`, reszta (także ETF-y) → `Akcje zagraniczne`.
- **Nowy symbol `.PL` nieobecny w słowniku**: automatycznie `WSE:<symbol>`, `Akcje polskie`, `PLN`, nazwa z eksportu; odnotować w raporcie („dopisz do słownika”).
- Nowy symbol zagraniczny: wiersz z surowym symbolem XTB i ostrzeżeniem w komentarzu (§10); do słownika uzupełnia użytkownik.
- Spacje w tickerach niedozwolone (`transactionsSort` usuwa pierwszą spację).
- Seed: 48 symboli dopasowanych, 3 bez wpisu w arkuszu (`DNP.PL`, `IB01.UK`, `VWCE.DE` – instrumenty zamknięte, pola puste).

## 10. Komentarz (kolumna N) – konwencja

### 10.1 Ustalenia z analizy
- W arkuszu 32 z 372 wierszy ma w Komentarzu tag **`Konto##Ticker##Klasa##Waluta`** (np. `USD##NYSE:FDS##Akcje zagraniczne##USD`, `XTB##Gotówka##Gotówka##PLN`).
- Tag jest **identyczny z kluczem FIFO** budowanym w `przeliczPortfolio` (`row[0] + '##' + TICKER + '##' + row[5] + '##' + row[3]`; ticker wielkimi literami, wyjątek: `Gotówka` i prefiks `Waluty_`). Zweryfikowane: 32/32 tagów = klucz własnego wiersza.
- Występuje jako **jeden ciągły blok** wierszy arkusza 317–348 (daty 2026-05-20 … 2026-07-24), dla *wszystkich* rodzajów (dywidendy 19, wpłaty 5, zakupy 6, sprzedaż 1, wypłata 1). W tym bloku zastąpił notatkę o podatku i kwotę wpłaty walutowej. Wcześniej i później tagu nie ma.
- Dostarczone skrypty (`fifo.txt`, `makra.txt`, `daneGieldowe.txt`) **nie zapisują** tagu do arkusza – klucz powstaje tylko w pamięci i jest używany w komunikatach błędów FIFO (`Konto/Ticker/Klasa/Waluta`). Kod formularza `loadForm` nie został dostarczony.
- Skrypt dodawania transakcji **używa Komentarza jako kanału diagnostycznego**: nowsze wiersze mają `⚠️ Nieznana giełda FT, ticker zapisano bez konwersji: GER:IS3N:EUR`.
- Żaden z dostarczonych skryptów nie czyta kolumny N (FIFO czyta `A2:M`).

Wniosek (hipoteza): tag służy do wskazania, do którego koszyka FIFO trafia wiersz, co ułatwia diagnozę błędu „Próbujesz sprzedać aktywo, którego nie posiadasz”. Źródło zapisu nie zostało zidentyfikowane.

### 10.2 Reguła generowania (domyślna – do potwierdzenia, §16)
Komentarz składa się z segmentów łączonych `" | "`, w kolejności:
1. **Tag** `Konto##Ticker##Klasa##Waluta` – dokładnie jak klucz FIFO (to samo wielkie/małe litery, `Gotówka` bez zmiany) – dla każdego wygenerowanego wiersza.
2. **Informacja**: dywidenda → `Pobrany podatek: 0.37 USD`; wpłata walutowa/przewalutowanie → `260 EUR`.
3. **Ostrzeżenie** (jeśli jest) w stylu istniejącym w arkuszu, np. `⚠️ Nieznany symbol XTB, brak wpisu w słowniku: XYZ.US`.

Przykład: `USD##NASDAQ:SBUX##Akcje zagraniczne##USD | Pobrany podatek: 0.37 USD`.
Tryb przełączalny flagą (`--komentarz tag|info|oba`, domyślnie `oba`), bo ostatnie wiersze (od 2026-08) tagu nie mają. Tag jako prefiks jest łatwy do odcięcia (`split(' | ')[0]`).

## 11. Zgodność ze skryptami arkusza (twarde ograniczenia)

Z `fifo.txt` i `makra.txt`:
- `przeliczPortfolio` czyta `Transakcje!A2:M` (nie N). FIFO uwzględnia tylko `Zakup`, `DRIP`, `Split`, `Sprzedaż`, `Wpłata środków`. `Dywidenda / Odsetki` i `Wypłata środków` nie wchodzą do FIFO, ale `Wpłata`/`Wypłata` zasilają XIRR (kolumny B, G, M). Dlatego **Total (M) dla wpłat/wypłat musi być w PLN**.
- **Klucz FIFO = Konto ## TICKER ## Klasa ## Waluta.** Sprzedaż musi trafić w ten sam klucz, co wcześniejszy zakup – inaczej wyjątek („Próbujesz sprzedać…”). Zmiana waluty instrumentu między zakupem a sprzedażą (np. `LON:EQQQ` GBP vs PLN) psuje FIFO.
- **Koszt FIFO liczony z Total**: `cena = Total / Liczba / Kurs` (nie z kolumny Cena, z wyjątkiem obligacji korporacyjnych). Total, Liczba i Kurs muszą być spójne; Cena jest informacyjna, ale powinna zgadzać się z Total/(Liczba×Kurs).
- Precyzja ilości w FIFO: 8 miejsc – nie zaokrąglać Liczby poniżej tej precyzji.
- `transactionsSort`: sortuje datą rosnąco i Rodzajem malejąco (Zakup przed Sprzedażą w tym samym dniu) oraz usuwa spacje w tickerach.
- Dozwolone wartości `Rodzaj transakcji` używane w skryptach: `Zakup`, `Sprzedaż`, `Wpłata środków`, `Wypłata środków`, `Dywidenda / Odsetki` (+ `DRIP`, `Split`).
- Walidacja opcjonalna w narzędziu (gdy podano `--portfolio`): zasymulować otwarte pozycje z arkusza + nowe wiersze i **ostrzec**, jeśli sprzedaż nie ma pokrycia w kluczu FIFO.

## 12. Raport kontrolny (`raport.md` + terminal)

1. Użyte daty graniczne per konto i liczba wygenerowanych wierszy per konto/rodzaj.
2. **Automatyczne decyzje** (jawnie): sparowane przewalutowania i pominięte wpłaty PLN, pary IKE, scalone zlecenia/dywidendy.
3. **Do ręcznej poprawy:** wiersze z kursem NBP wstawionym domyślnie; wpłaty bez odpowiedzi; nowe symbole do dopisania w słowniku; ostrzeżenia z komentarzy.
4. Operacje z ostatniego dnia wpisu (możliwe duplikaty).
5. Pominięte typy z liczbą i sumą (odsetki, podatek od odsetek, SEC fee, Subaccount transfer) oraz nieznane typy.
6. **Salda gotówki per subkonto** (wiersz `Total` z eksportu) do porównania z XTB, wraz z kontrolą: suma Amount == Total.

## 13. Tryb audytu (`--audyt`, opcjonalny, faza 2)

Porównuje **całą historię** eksportu z `portfolio-transakcje.csv` i wypisuje rozbieżności (niczego nie poprawia). Użytkownik zaznaczył, że arkusz zawiera błędy przepisywania; wiadome kandydaty:
- 3 z 15 wypłat z eksportu są w arkuszu; wypłata 128,00 zł z 2026-01-25 („prezent”) nie ma odpowiednika w `Withdrawal`.
- Brak dywidendy XTB.PL z 2026-06-24 (108,79 zł netto).
- Zakup SNT na IKE: w arkuszu 2026-08-11, w eksporcie 2026-08-10.
- Kwoty: przelew XTB→IKE 5074,00 (arkusz) vs 5074,21 (eksport); IKE 2026-06-11 zakup IS3N 333,99 vs 336,07 zł.
- Zlecenia, które w arkuszu są rozbite na osobne wiersze (DVN, IUIT), a eksport scala.

## 14. Przypadki brzegowe

- Wiele linii eksportu dla jednego zlecenia (częściowe realizacje) – scalać (§6).
- Dywidenda i podatek mają ten sam znacznik czasu; tego samego dnia mogą wystąpić dwie dywidendy tego samego papieru – scalać.
- Ułamkowe ilości (0,9803; 0,2657) – Liczba bez zaokrągleń do mniej niż 8 miejsc.
- Zlecenia w tym samym dniu: zakup i sprzedaż tego samego instrumentu – osobne wiersze.
- Ticker w eksporcie z cudzą giełdą niż w słowniku – słownik wygrywa.
- Pusty plik / brak zakładki Cash Operations → czytelny błąd dla danego pliku, reszta kontynuuje.
- Operacja w dniu = data graniczna → patrz §5.

## 15. Testy akceptacyjne (dane referencyjne)

Jednostkowe/integracyjne na fixture’ach (zanonimizowane eksporty + `portfolio-transakcje.csv`):

| # | Wejście | Oczekiwany wiersz |
|---|---|---|
| 1 | USD 2026-08-28: `OPEN BUY 2/2.2 @ 236.94` + `0.2/2.2 @ 237.41` (−473,88, −47,48) | 1 wiersz `NASDAQ:TTWO`, Zakup, Liczba 2,2, Cena ≈ 236,98, Kurs = NBP, Total = 521,36 × Kurs |
| 2 | USD 2026-08-11: `CLOSE BUY 0.9803/1.9803 @ 45.13` + `1/1.9803 @ 45.21` | 1 wiersz `NYSE:DVN`, Sprzedaż, Liczba 1,9803 |
| 3 | IKE 2026-06-11: `0.7023/1.7023 @ 46.185` + `1/1.7023 @ 46.185` (−138,65, −197,42) | `GER:IS3N:EUR`, Waluta EUR, Liczba 1,7023, Cena 46,185, Total 336,07, Kurs = 336,07 / (1,7023 × 46,185) ≈ 4,27 |
| 4 | USD 2026-08-28 SBUX: dywidenda 2,48 i podatek −0,37 | `NASDAQ:SBUX`, Dywidenda, Liczba 1, Cena 1, Total = 2,11 × NBP, komentarz z `Pobrany podatek: 0.37 USD` |
| 5 | USD 2025-01-20 przewalutowanie 1200 PLN → 292,02 USD; wpłata XTB 1200 PLN z 2025-01-17 | wiersz `USD` Wpłata środków, Total 1200,00, Kurs ≈ 4,1092, komentarz `292 USD`; brak wiersza XTB; wpisy w logu |
| 6 | IKE 2026-05-22: `IKE deposit` 5074,21 w obu plikach | `XTB` Wypłata 5074,21 + `Z_IKE` Wpłata 5074,21 |
| 7 | Domyślne daty graniczne z `portfolio-transakcje.csv` | wynik jak w §5 (4 wiersze) |
| 8 | Salda | PLN 34,09 / EUR 18,17 / USD 31,71 / IKE 47,36 = suma Amount |
| 9 | Nieznany symbol zagraniczny | wiersz z surowym symbolem + `⚠️` w komentarzu + wpis w raporcie |
| 10 | Wklejenie wyniku do arkusza | liczby jako liczby (przecinek), daty jako daty, `##` i emoji w komentarzu bez zniekształceń |

## 16. Pytania otwarte / założenia do potwierdzenia

1. **Komentarz:** czy dopisywać po tagu informację (podatek / kwota waluty), czy zostawić sam tag w stylu bloku 317–348 (`--komentarz tag`)? Domyślnie: tag + informacja.
2. **„3 miejsca po przecinku”** (odpowiedź użytkownika przy łączeniu zleceń) zinterpretowano jako precyzję **Ceny**; Kurs 4, Total 2 miejsca.
3. Przewalutowanie odwrotne EUR/USD → PLN: poza v1 (ostrzeżenie).
4. Czy tryb audytu (§13) ma wejść do v1, czy do fazy 2 – domyślnie faza 2.
5. Dla wypłat walutowych (USD 8,00) kurs NBP z oznaczeniem „do poprawienia”.

## 17. Proponowane etapy implementacji

1. Parser zipa/xlsx + rozpoznawanie kont + parser komentarzy + testy na fixture’ach.
2. Słownik tickerów + budowa wierszy Zakup/Sprzedaż/Dywidenda (bez NBP, mock).
3. Kursy NBP (cache, reguła „dzień roboczy przed”), wpłaty (pytania), przewalutowania i IKE.
4. Daty graniczne z `portfolio-transakcje.csv`, TSV, schowek.
5. Raport kontrolny + walidacja pokrycia sprzedaży względem klucza FIFO.
6. (Faza 2) Tryb audytu.
