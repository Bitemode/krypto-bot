# Krypto-Handelsbot

Ensemble-Trendfolge mit Volatilitätssteuerung, Spot, long-only, EUR-Paare auf Bitvavo.
Läuft im Papier-Betrieb; echter Handel ist nicht implementiert und dreifach gesperrt.

**Festgelegt am 03.09.2026:** Zielvolatilität 20 %, Rebalance alle 10 Tage.

## Loslegen

```bash
pip install pandas numpy pyyaml

python3 -m cryptobot.cli doctor                    # Konfiguration und Datenquelle prüfen
python3 -m cryptobot.cli analyze                   # bewerten, nichts buchen
python3 -m cryptobot.cli rebalance --dry-run       # zeigen, was gebucht würde
python3 -m cryptobot.cli rebalance                 # im Papier-Portfolio buchen
python3 -m cryptobot.cli positions
python3 -m cryptobot.cli performance
```

Ohne Netz oder zum Ausprobieren mit den mitgelieferten Kursdaten:

```bash
python3 -m cryptobot.cli --offline data_offline analyze --force
```

Für Marktdaten braucht der Bot **keinen API-Schlüssel** — es werden nur öffentliche
Endpunkte gelesen. Ein Handelsschlüssel kommt erst mit dem Live-Adapter ins Spiel,
und der existiert noch nicht.

## Täglich laufen lassen (macOS)

```bash
bash install_macos.sh            # richtet den täglichen Lauf um 10:00 Ortszeit ein
bash install_macos.sh --status   # läuft er? letzte Läufe ansehen
bash install_macos.sh --remove   # wieder abschalten
./run_daily.sh                   # einmal sofort ausführen
```

Der Installer prüft vorher Python, die Bibliotheken und die Datenquelle und richtet
nichts ein, wenn der Testlauf scheitert. Eine andere Uhrzeit:
`CRYPTOBOT_HOUR=8 CRYPTOBOT_MINUTE=30 bash install_macos.sh`.

Schläft der Rechner um 10:00, holt launchd den Lauf beim nächsten Aufwachen nach.
Ist der Mac den ganzen Tag aus, fällt der Lauf aus — bei einem 10-Tage-Takt ist das
verschmerzbar, für den Dauerbetrieb wäre ein kleiner Server die bessere Heimat.

Was wo landet: `state/cron.log` das Protokoll aller Läufe, `state/fehler.log` nur die
Fehler, `state/portfolio.json` der Kontostand, `state/trades.csv` jeder Trade.

## Was der Bot tut

Läuft täglich um 10:00 Ortszeit. An jedem zehnten Tag ist Rebalance-Tag; an den
übrigen Tagen wird nur auf Notfälle geprüft — Regime-Wechsel, Katastrophen-Stop,
Datenqualität.

**Signal.** Für jeden Coin drei Bedingungen auf Tageskerzen: Kurs über SMA(30),
Kurs über SMA(60), 30-Tage-Rendite positiv. Zwei von drei genügen. Ein Ensemble statt
eines optimierten Fensters, weil die Literatur sich nur über das Band 20–65 Tage einig
ist, nicht über den Einzelwert.

**Regime.** Bitcoin über SMA(60) und 30-Tage-Rendite positiv → voll investierbar; eine
der beiden → halbe Quote; keine → alles in Cash.

**Positionsgröße.** Zweistufig: erst je Coin invers zur Volatilität, dann das ganze
Portfolio auf 20 % Zielvolatilität skaliert. Die zweite Stufe erledigt zugleich die
Korrelationsfrage — laufen alle Coins gleichzeitig, schrumpft die Gesamtposition
automatisch. In der Praxis ergibt das rund 20 % investiert und 80 % Cash.

**Ausstieg.** Trendbruch beim Rebalance, Regime auf RISK_OFF, oder Katastrophen-Stop
bei −25 % je Position. Kein Kursziel, keine Gewinnmitnahme: Trendfolge lebt von den
wenigen großen Bewegungen.

## Was gemessen wurde

Backtest über 4,3 Jahre echter Bitvavo-Kurse (18 Märkte, Mai 2022 bis September 2026),
mit Gebührenstaffel, Slippage und einer Kerze Ausführungsverzögerung:

| | Rendite p. a. | Sharpe | Max. Drawdown | Kosten p. a. |
|---|---|---|---|---|
| Diese Konfiguration | 10,5 % | 0,70 | −24,4 % | 2,2 % |
| ohne Regime-Gate | 10,8 % | 0,48 | −42,3 % | 4,6 % |
| Gleichgewichtet halten | 2,4 % | 0,35 | −68,6 % | 0 % |

Walk-Forward über sechs Halbjahre: Sharpe im Median 0,65 bis 0,86, mit einzelnen
Halbjahren bei −1,89. Die längste Zeit unter dem alten Höchststand betrug 634 Tage.
37 % der Zeit war der Bot vollständig in Cash.

## Freigabe und Not-Aus

In `config.yaml` unter `gates`. Kein „Mindest-Sharpe" als Vorgabe — Sharpe ist ein
Messergebnis, kein Regler. Was eingestellt ist:

- **Live-Freigabe** frühestens bei Walk-Forward-Sharpe ≥ 0,8 über mindestens zwei Jahre
  und 100 Trades.
- **Not-Aus** bei rollierendem 12-Monats-Sharpe unter 0,2 oder Drawdown über 25 %.
- Der eigentliche Risikoregler ist `risk.target_vol_portfolio`. 0,20 ergab −24,4 %
  Drawdown, 0,35 ergab −39,6 % bei praktisch gleichem Sharpe.

## Aufbau

```
config.yaml            alle Parameter, kommentiert
cryptobot/
  config.py            YAML + Umgebungsvariablen, Validierung beim Start
  strategy.py          Trendstärke, Regime, zweistufige Positionsgröße   (geprüft)
  indicators.py        SMA, Renditen, EWMA-Volatilität, Kovarianz        (geprüft)
  costs.py             Bitvavo-Gebührenstaffel, Slippage                 (geprüft)
  engine.py            Backtest-Engine, gleiche Regeln wie live          (geprüft)
  planner.py           Signal + Filter -> Orders
  portfolio.py         Papier-Portfolio, Grenzen, atomare Persistenz
  journal.py           Entscheidungen und Buchungen, auch Ablehnungen
  providers/bitvavo.py öffentliche Marktdaten, nur lesend
  report.py            Konsolenausgabe und JSON
  cli.py
tests/test_bot.py      21 Tests
```

Die mit „geprüft" markierten Module stammen unverändert aus dem Backtest-Projekt und
sind dort mit 28 weiteren Tests abgedeckt, darunter ein Look-ahead- und ein
Repainting-Test. Live-Pfad und Backtest teilen sich diesen Code — sonst testet man
etwas anderes, als man betreibt.

## Grenzen

- Der Live-Handel fehlt bewusst. `trading.mode: live` bricht ab.
- Der Backtest umfasst einen einzigen Zeitraum, der nahe dem Tief von 2022 beginnt;
  zwischenzeitlich eingestellte Coins fehlen. Beides schönt das Ergebnis.
- Gerechnet wird auf Tagesschlusskursen.
- Steuern sind nicht enthalten. In Deutschland fallen Gewinne aus Haltedauern unter
  einem Jahr unter § 23 EStG.

Keine Anlageberatung.
