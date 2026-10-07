"""Kommandozeile.

  analyze      bewerten und ausgeben, nichts buchen
  rebalance    Plan erstellen und im Papier-Portfolio buchen
  positions    offene Positionen
  performance  Kennzahlen
  doctor       Datenquellen und Konfiguration prüfen
  capital      Ein- und Auszahlungen verbuchen
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import report
from .config import Config, ConfigError, load
from .journal import Journal
from .jupiter import SPALTEN as JUP_SPALTEN
from .orderbuch import anhaengen, rotieren, zeile
from .planner import build_plan, execute_paper
from .portfolio import Portfolio
from .providers.bitvavo import BitvavoData, OrderBook, ProviderError, Ticker


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _universum(cfg: Config, quelle: BitvavoData | None, offline: Path | None) -> list[str]:
    if offline:
        return sorted(p.stem for p in offline.glob("*.csv"))
    stables = set(cfg.get("universe.stablecoins", []))
    ausschluss = set(cfg.get("universe.exclude", []))
    top = quelle.markets_by_volume(cfg.get("trading.quote", "EUR"), stables)
    raus = []
    for markt, vol in top:
        if markt.split("-")[0] in ausschluss:
            continue
        if vol < cfg.get("universe.min_volume_eur", 250_000):
            continue
        raus.append(markt)
        if len(raus) >= int(cfg.get("universe.top_n", 25)):
            break
    return raus


def _lade_daten(cfg: Config, maerkte: list[str], quelle: BitvavoData | None,
                offline: Path | None) -> pd.DataFrame:
    reihen: dict[str, pd.Series] = {}
    mindest = int(cfg.get("universe.min_history_days", 365))
    for m in maerkte:
        if offline:
            df = pd.read_csv(offline / f"{m}.csv")
            idx = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            s = pd.Series(df["close"].astype(float).to_numpy(), index=idx)
        else:
            roh = quelle.daily_candles(m, days=mindest + 200)
            if not roh:
                continue
            idx = pd.to_datetime([t for t, _ in roh], unit="ms", utc=True)
            s = pd.Series([c for _, c in roh], index=idx)
        s = s[~s.index.duplicated(keep="last")].sort_index()
        if len(s) >= mindest:
            reihen[m] = s
    if not reihen:
        raise ProviderError("Keine Marktdaten geladen")
    return pd.DataFrame(reihen).sort_index()


def _momentaufnahmen(maerkte: list[str], quelle: BitvavoData | None,
                     closes: pd.DataFrame) -> tuple[dict[str, Ticker], dict[str, OrderBook]]:
    tickers: dict[str, Ticker] = {}
    books: dict[str, OrderBook] = {}
    if quelle is None:
        # Offline: Bid/Ask und ein einfaches Orderbuch aus dem letzten Schlusskurs,
        # damit auch Spread-, Tiefen- und Slippage-Prüfung durchlaufen werden.
        jetzt = _jetzt()
        for m in maerkte:
            reihe = closes[m].dropna()
            if reihe.empty:
                continue
            p = float(reihe.iloc[-1])
            tickers[m] = Ticker(m, p * 0.9995, p * 1.0005, 10_000_000.0, jetzt)
            stufen = [(p * (1 + 0.0005 + i * 0.0004), 20_000.0 / p) for i in range(25)]
            gegen = [(p * (1 - 0.0005 - i * 0.0004), 20_000.0 / p) for i in range(25)]
            books[m] = OrderBook(m, gegen, stufen, jetzt)
        return tickers, books
    for m in maerkte:
        try:
            tickers[m] = quelle.ticker(m)
            books[m] = quelle.orderbook(m)
        except ProviderError:
            continue
    return tickers, books


def _lade_markt(args) -> tuple[Config, pd.DataFrame, dict[str, Ticker], dict[str, OrderBook]]:
    """Marktdaten einmal laden — alle Profile teilen sie sich."""
    cfg = load(args.config)
    offline = Path(args.offline) if getattr(args, "offline", None) else None
    quelle = None if offline else BitvavoData(cache_dir=cfg.get("paths.data_cache"))
    maerkte = _universum(cfg, quelle, offline)
    closes = _lade_daten(cfg, maerkte, quelle, offline)
    tickers, books = _momentaufnahmen(list(closes.columns), quelle, closes)
    return cfg, closes, tickers, books


def _portfolio_und_journal(cfg: Config) -> tuple[Portfolio, Journal]:
    portfolio = Portfolio.load_or_create(
        cfg.path("state"),
        float(cfg.get("portfolio.initial_equity", 10_000.0)),
        float(cfg.get("portfolio.cash_buffer", 0.005)),
    )
    return portfolio, Journal(cfg.path("journal"), cfg.path("trades"))


def _ist_rebalance_tag(portfolio: Portfolio, cfg: Config, jetzt: datetime) -> bool:
    """Ist heute Rebalance-Tag?

    Gerechnet wird vom letzten TATSÄCHLICH durchgeführten Rebalance, nicht vom
    Anlegedatum: Ein ausgefallener Lauf darf den Takt nicht dauerhaft verschieben.
    Beim Kaltstart — leeres Portfolio, noch nie umgeschichtet — wird sofort
    umgeschichtet, statt auf einen Kalendertag zu warten.
    """
    from datetime import date as _date

    takt = max(1, int(cfg.get("schedule.rebalance_every_days", 10)))
    letzter = portfolio.s.last_rebalance
    if not letzter:
        return True
    try:
        vergangen = (jetzt.date() - _date.fromisoformat(letzter[:10])).days
    except ValueError:
        return True
    return vergangen >= takt


def _lauf(cfg_p: Config, name: str, closes, tickers, books, args, buchen: bool):
    """Ein Profil durchrechnen; bei `buchen` auch ausführen."""
    jetzt = _jetzt()
    portfolio, journal = _portfolio_und_journal(cfg_p)
    preise = {m: t.mid for m, t in tickers.items()}
    # Wie im Planer: fehlt ein Ticker, gilt der letzte Tagesschluss — und fuer
    # gehaltene Muenzen ohne beides der zuletzt gesehene Kurs (portfolio.kurse).
    for m in closes.columns:
        if not preise.get(m) and closes[m].notna().any():
            preise[m] = float(closes[m].dropna().iloc[-1])
    preise = portfolio.kurse(preise)
    portfolio.mark_day(preise, jetzt.date().isoformat())

    ist_rebalance = bool(args.force or _ist_rebalance_tag(portfolio, cfg_p, jetzt))
    grenze = cfg_p.get("schedule.execute_after_hour")
    verschoben = False
    if ist_rebalance and grenze is not None and not args.force and jetzt.astimezone().hour < int(grenze):
        ist_rebalance, verschoben = False, True

    plan = build_plan(closes, tickers, books, portfolio, cfg_p, jetzt,
                      ist_rebalance_tag=ist_rebalance)
    if verschoben:
        plan.hinweise.append(
            f"Umschichtung faellig — wird beim Lauf ab {int(grenze)}:00 Uhr ausgefuehrt."
        )

    fills = None
    if buchen:
        fills = execute_paper(plan, portfolio, cfg_p, journal, jetzt)
        if ist_rebalance:
            portfolio.s.last_rebalance = jetzt.date().isoformat()
        portfolio.mark_day(preise, jetzt.date().isoformat())
        portfolio.save()
        plan.equity = portfolio.equity(preise)
        plan.exposure = portfolio.exposure(preise)
        journal.log_run(plan.entscheidungen,
                        {"equity": plan.equity, "cash": portfolio.s.cash,
                         "exposure": plan.exposure, "fills": len(fills), "profil": name},
                        {"target_vol_portfolio": cfg_p.get("risk.target_vol_portfolio"),
                         "rebalance_every_days": cfg_p.get("schedule.rebalance_every_days")},
                        cfg_p.get("trading.mode"))
    else:
        journal.log_run(plan.entscheidungen,
                        {"equity": plan.equity, "cash": portfolio.s.cash,
                         "exposure": plan.exposure, "profil": name},
                        {"target_vol_portfolio": cfg_p.get("risk.target_vol_portfolio")},
                        "analysis")
        portfolio.save()
    return plan, portfolio, fills, preise, jetzt


def _profil_zeile(name: str, champ: bool, plan, portfolio, fills) -> str:
    marke = "*" if champ else " "
    n = len(fills) if fills is not None else 0
    return (f" {marke} {name:<10}{plan.equity:>11,.2f} EUR"
            f"{plan.exposure:>9.0%} investiert"
            f"{len(portfolio.s.positions):>4} Pos."
            f"{n:>4} Orders   {plan.regime}")


def cmd_analyze(args) -> int:
    cfg, closes, tickers, books = _lade_markt(args)
    profile = cfg.profiles()
    erste = True
    zeilen = []
    for name, cfg_p, champ in profile:
        plan, portfolio, fills, preise, jetzt = _lauf(cfg_p, name, closes, tickers, books,
                                                      args, buchen=False)
        if args.format == "json" and champ:
            print(report.als_json(plan, portfolio, cfg_p.get("trading.mode"), jetzt))
        elif args.format != "json" and (champ or (erste and len(profile) == 1)):
            print(report.voll(plan, portfolio, preise, cfg_p.get("trading.mode"), jetzt))
        zeilen.append(_profil_zeile(name, champ, plan, portfolio, fills))
        erste = False
    if len(profile) > 1 and args.format != "json":
        print("Profile (* = Champion)")
        print("-" * 78)
        print("\n".join(zeilen))
        print()
    return 0


def cmd_rebalance(args) -> int:
    cfg, closes, tickers, books = _lade_markt(args)
    modus = str(cfg.get("trading.mode", "analysis")).lower()
    if modus == "live":
        print("Live-Handel ist in dieser Ausbaustufe nicht implementiert.", file=sys.stderr)
        return 1
    if modus == "analysis":
        print("trading.mode ist analysis — es wird nichts gebucht.", file=sys.stderr)
        return 1
    if getattr(args, "offline", None) and not args.dry_run:
        print("Offline-Daten: es wird nichts gebucht (Testmodus).", file=sys.stderr)
        args.dry_run = True

    profile = cfg.profiles()
    zeilen = []
    for name, cfg_p, champ in profile:
        plan, portfolio, fills, preise, jetzt = _lauf(cfg_p, name, closes, tickers, books,
                                                      args, buchen=not args.dry_run)
        if champ or len(profile) == 1:
            titel = modus + (" (Trockenlauf)" if args.dry_run else "")
            print(report.voll(plan, portfolio, preise, titel, jetzt, fills))
        zeilen.append(_profil_zeile(name, champ, plan, portfolio, fills))
    if len(profile) > 1:
        print("Profile (* = Champion)")
        print("-" * 78)
        print("\n".join(zeilen))
        print()
    return 0


def cmd_profiles(args) -> int:
    """Vergleich der Profile — nur gepaart aussagekräftig, deshalb mit Warnhinweis."""
    cfg = load(args.config)
    profile = cfg.profiles()
    print(f"{'Profil':<12}{'Kapital':>12}{'gesamt':>10}"
          f"{'Trades':>8}{'Gebuehren':>11}{'Pos.':>6}")
    print("-" * 60)
    reihen = []
    for name, cfg_p, champ in profile:
        portfolio, journal = _portfolio_und_journal(cfg_p)
        eingezahlt = portfolio.invested_capital()
        hist = portfolio.s.equity_history
        wert = hist[-1][1] if hist else eingezahlt
        trades = journal.read_trades()
        geb = sum(float(t["gebuehr_eur"]) for t in trades)
        gesamt = wert / eingezahlt - 1 if eingezahlt > 0 else 0.0
        reihen.append((name, champ, wert, eingezahlt, len(trades), geb,
                       len(portfolio.s.positions), len(hist)))
        print(f"{('* ' if champ else '  ') + name:<12}{wert:>12,.2f}"
              f"{gesamt:>10.1%}"
              f"{len(trades):>8}{geb:>11,.2f}{len(portfolio.s.positions):>6}")
    tage = max((r[7] for r in reihen), default=0)
    min_t = cfg.get("vergleich.min_trades", 60)
    min_m = cfg.get("vergleich.min_monate", 6)
    print()
    if tage < min_m * 30 or max((r[4] for r in reihen), default=0) < min_t:
        print(f"  Noch kein Vergleich moeglich: {tage} Tage Historie, "
              f"hoechstens {max((r[4] for r in reihen), default=0)} Trades.")
        print(f"  Erforderlich: {min_m} Monate und {min_t} Trades je Profil.")
    else:
        print(f"  Hinweis: {len(profile)} Profile heisst {len(profile)-1} Vergleiche — "
              f"der beste sieht immer gut aus.")
        print(f"  Ein Champion-Wechsel verlangt {cfg.get('vergleich.vorsprung_sharpe', 0.3)} "
              f"Sharpe Vorsprung UND Bestaetigung im Backtest.")
    return 0


def cmd_positions(args) -> int:
    cfg, closes, tickers, books = _lade_markt(args)
    name, cfg_c, _ = next((p for p in cfg.profiles() if p[2]), cfg.profiles()[0])
    portfolio, _ = _portfolio_und_journal(cfg_c)
    preise = portfolio.kurse({m: t.mid for m, t in tickers.items()})
    print(report.positionen(portfolio, preise))
    print(f"\n  Cash {portfolio.s.cash:,.2f} EUR · Kapital {portfolio.equity(preise):,.2f} EUR")
    return 0


def cmd_performance(args) -> int:
    cfg = load(args.config)
    name, cfg_c, _ = next((p for p in cfg.profiles() if p[2]), cfg.profiles()[0])
    portfolio, journal = _portfolio_und_journal(cfg_c)
    hist = portfolio.s.equity_history
    if len(hist) < 3:
        print("Noch zu wenig Historie für Kennzahlen "
              f"({len(hist)} Tage). Kennzahlen brauchen mindestens einige Wochen.")
        return 0
    eq = pd.Series([w for _, w in hist],
                   index=pd.to_datetime([d for d, _ in hist], utc=True)).sort_index()
    r = eq.pct_change().dropna()
    jahre = max(len(eq) / 365.0, 1e-9)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / jahre) - 1 if eq.iloc[0] > 0 else 0.0
    vol = float(r.std() * np.sqrt(365)) if len(r) > 2 else 0.0
    sharpe = float(r.mean() / r.std() * np.sqrt(365)) if len(r) > 2 and r.std() > 0 else 0.0
    dd = float((eq / eq.cummax() - 1).min())
    trades = journal.read_trades()
    gebuehren = sum(float(t["gebuehr_eur"]) for t in trades)
    realisiert = sum(float(t["realisiert_eur"]) for t in trades)

    print(f"\nKennzahlen über {len(eq)} Tage")
    print("-" * 56)
    for k, v in [("Kapital heute", f"{eq.iloc[-1]:,.2f} EUR"),
                 ("Eingezahlt", f"{portfolio.invested_capital():,.2f} EUR"),
                 ("Rendite p.a.", f"{cagr:.1%}"), ("Volatilität p.a.", f"{vol:.0%}"),
                 ("Sharpe", f"{sharpe:.2f}"), ("Max. Drawdown", f"{dd:.1%}"),
                 ("Trades", f"{len(trades)}"), ("Gebühren gesamt", f"{gebuehren:,.2f} EUR"),
                 ("Realisiert", f"{realisiert:,.2f} EUR")]:
        print(f"  {k:<24}{v:>28}")
    kill_s = cfg.get("gates.kill_rolling_sharpe", 0.2)
    kill_d = cfg.get("gates.kill_drawdown", -0.25)
    if len(eq) > 200 and sharpe < kill_s:
        print(f"\n  ! Sharpe {sharpe:.2f} unter der Not-Aus-Schwelle {kill_s}")
    if dd <= kill_d:
        print(f"  ! Drawdown {dd:.1%} unter der Not-Aus-Schwelle {kill_d:.0%}")
    return 0


def cmd_capital(args) -> int:
    cfg = load(args.config)
    name, cfg_c, _ = next((p for p in cfg.profiles() if p[2]), cfg.profiles()[0])
    portfolio, _ = _portfolio_und_journal(cfg_c)
    if not args.confirm:
        print("Ein- und Auszahlungen müssen mit --confirm bestätigt werden.", file=sys.stderr)
        return 1
    betrag = args.amount if args.action == "deposit" else -abs(args.amount)
    if args.action == "withdraw" and portfolio.s.cash + betrag < 0:
        print(f"Nicht genug Cash: {portfolio.s.cash:,.2f} EUR verfügbar.", file=sys.stderr)
        return 1
    portfolio.deposit(betrag, _jetzt().isoformat())
    portfolio.save()
    print(f"{'Einzahlung' if betrag > 0 else 'Auszahlung'} über {abs(betrag):,.2f} EUR verbucht. "
          f"Cash jetzt {portfolio.s.cash:,.2f} EUR.")
    print("Hinweis: Der Bot bewegt kein Geld — die Überweisung machst du selbst an der Börse.")
    return 0


def cmd_buch(args) -> int:
    """Einen Orderbuch-Schnappschuss schreiben. Handelt nicht, braucht keinen Schluessel."""
    cfg = load(args.config)
    quelle = BitvavoData(cache_dir=cfg.get("paths.data_cache"))
    ordner = Path(cfg.get("paths.orderbuch", "state/orderbuch"))

    stables = set(cfg.get("universe.stablecoins", []))
    ausschluss = set(cfg.get("universe.exclude", []))
    top = quelle.markets_by_volume(cfg.get("trading.quote", "EUR"), stables)
    maerkte = [m for m, v in top
               if m.split("-")[0] not in ausschluss
               and v >= cfg.get("universe.min_volume_eur", 250_000)][:int(cfg.get("universe.top_n", 25))]

    jetzt = _jetzt()
    reihen, fehler = [], 0
    for m in maerkte:
        try:
            z = zeile(quelle.orderbook(m), jetzt)
        except ProviderError:
            fehler += 1
            continue
        if z:
            reihen.append(z)
    if reihen:
        anhaengen(reihen, ordner, jetzt)
    gepackt = rotieren(ordner, int(cfg.get("orderbuch.packen_nach_tagen", 3)))
    print(f"{jetzt.isoformat(timespec='seconds')}  {len(reihen)} Maerkte geschrieben"
          + (f", {fehler} nicht erreichbar" if fehler else "")
          + (f", {gepackt} Datei(en) gepackt" if gepackt else ""))
    return 0


def cmd_spreads(args) -> int:
    """Auswertung der mitgeschriebenen Orderbuecher."""
    from .spreadbericht import bericht

    cfg = load(args.config)
    # Die Gebuehrenstufe haengt am 30-Tage-Volumen. Dasselbe Mass wie beim
    # Buchen (planner.execute_paper), damit Bericht und Journal nicht
    # auseinanderlaufen: die Buchungen des Champion-Profils.
    volumen_30d = 0.0
    try:
        _, journal = _portfolio_und_journal(cfg)
        volumen_30d = sum(abs(float(t["notional_eur"]))
                          for t in journal.read_trades()[-500:])
    except Exception:
        pass
    print(bericht(Path(cfg.get("paths.orderbuch", "state/orderbuch")),
                  min_messungen=int(args.min), volumen_30d=volumen_30d))
    return 0


def cmd_tradingview(args) -> int:
    """Pine-Skript mit den tatsaechlich gebuchten Trades erzeugen."""
    from .tradingview import erzeugen, sammeln, schreiben

    cfg = load(args.config)
    profile = cfg.profiles()
    trades = sammeln(profile, lambda c: _portfolio_und_journal(c)[1])
    if not trades:
        print("Noch keine Trades im Journal — es gibt nichts zu zeichnen.", file=sys.stderr)
        return 1
    text = erzeugen(trades, [n for n, _, _ in profile])
    ziel = schreiben(text, Path(args.out))
    maerkte = sorted({t["markt"] for t in trades})
    print(f"{len(trades)} Buchungen aus {len(maerkte)} Maerkten geschrieben:")
    print(f"  {ziel}")
    print()
    print("So kommt es in den Chart:")
    print("  1. Datei oeffnen und den ganzen Inhalt kopieren")
    print("  2. In TradingView unten auf 'Pine Editor', alles markieren, einfuegen")
    print("  3. 'Zum Chart hinzufuegen'")
    print()
    print(f"  Maerkte mit Buchungen: {', '.join(maerkte)}")
    print("  Der Chart zeigt automatisch den Markt, den du gerade offen hast.")
    return 0


def cmd_jupiter(args) -> int:
    """Solana-Rundlauf messen. Kein Schluessel, keine Geldboerse, kein Einsatz."""
    from .jupiter import pruefen, zeile

    cfg = load(args.config)
    maerkte = cfg.get("jupiter.maerkte", {}) or {}
    if not maerkte:
        print("Keine Maerkte unter jupiter.maerkte konfiguriert.", file=sys.stderr)
        return 1

    if args.pruefen:
        print("Kontrolle der Mint-Adressen — eine falsche Adresse misst still den "
              "falschen Token:\n")
        for z in pruefen(maerkte):
            print(z)
        return 0

    ordner = Path(cfg.get("paths.orderbuch", "state/orderbuch"))
    slip = int(cfg.get("jupiter.slippage_bps", 50))
    jetzt = _jetzt()
    reihen = []
    for symbol, mint in maerkte.items():
        for betrag in cfg.get("jupiter.betraege_usdc", [100, 500, 2000]):
            reihen.append(zeile(symbol, mint, float(betrag), slip, jetzt))
    anhaengen(reihen, ordner, jetzt, spalten=JUP_SPALTEN, praefix="jup")
    rotieren(ordner, int(cfg.get("orderbuch.packen_nach_tagen", 3)), praefix="jup")
    ok = sum(1 for z in reihen if z.get("rundlauf_bps") is not None)
    print(f"{jetzt.isoformat(timespec='seconds')}  {ok}/{len(reihen)} Messungen "
          f"({len(maerkte)} Maerkte)")
    for z in reihen:
        if z.get("hinweis"):
            print(f"  ! {z['markt']} {z['betrag_usdc']}$: {z['hinweis']}", file=sys.stderr)
    return 0


def cmd_doctor(args) -> int:
    print("Konfiguration")
    try:
        cfg = load(args.config)
    except ConfigError as e:
        print(f"  FEHLER  {e}")
        return 1
    print(f"  ok      {cfg.quelle}")
    if cfg.env_overrides:
        print(f"  Hinweis Umgebungsvariablen überschreiben: {', '.join(cfg.env_overrides)}")
    print(f"  Modus   {cfg.get('trading.mode')}  ·  Zielvolatilität "
          f"{cfg.get('risk.target_vol_portfolio'):.0%}  ·  Rebalance alle "
          f"{cfg.get('schedule.rebalance_every_days')} Tage")

    schluessel = [k for k in os.environ if "KEY" in k.upper() or "SECRET" in k.upper()]
    print(f"\nSchlüssel im Prozess: {len(schluessel)} "
          f"({'keine — für Marktdaten auch nicht nötig' if not schluessel else 'vorhanden'})")

    print("\nDatenquelle")
    if args.offline:
        n = len(list(Path(args.offline).glob("*.csv")))
        print(f"  ok      Offline-Ordner mit {n} Märkten")
        return 0
    quelle = BitvavoData(cache_dir=cfg.get("paths.data_cache"))
    try:
        maerkte = _universum(cfg, quelle, None)
        print(f"  ok      {len(maerkte)} Märkte: {', '.join(maerkte[:8])}"
              f"{' …' if len(maerkte) > 8 else ''}")
        t = quelle.ticker(maerkte[0])
        print(f"  ok      {t.market}: Bid {t.bid} / Ask {t.ask}, Spread {t.spread:.3%}")
        b = quelle.orderbook(maerkte[0])
        print(f"  ok      Orderbuch: Tiefe ±1 % = {b.depth_within(0.01):,.0f} EUR")
    except ProviderError as e:
        print(f"  FEHLER  {e}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cryptobot", description="Krypto-Handelsbot")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--offline", default=None,
                   help="Ordner mit Tages-CSVs statt Live-Daten (für Tests)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="bewerten, nichts buchen")
    a.add_argument("--format", choices=["text", "json"], default="text")
    a.add_argument("--force", action="store_true", help="Rebalance-Tag erzwingen")
    a.set_defaults(func=cmd_analyze)

    r = sub.add_parser("rebalance", help="Plan im Papier-Portfolio buchen")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--force", action="store_true")
    r.set_defaults(func=cmd_rebalance)

    sub.add_parser("positions", help="offene Positionen").set_defaults(func=cmd_positions)
    sub.add_parser("profiles", help="Profile vergleichen").set_defaults(func=cmd_profiles)
    sub.add_parser("performance", help="Kennzahlen").set_defaults(func=cmd_performance)

    c = sub.add_parser("capital", help="Ein- oder Auszahlung verbuchen")
    c.add_argument("action", choices=["deposit", "withdraw"])
    c.add_argument("--amount", type=float, required=True)
    c.add_argument("--confirm", action="store_true")
    c.set_defaults(func=cmd_capital)

    sub.add_parser("buch", help="Orderbuch-Schnappschuss schreiben").set_defaults(func=cmd_buch)

    sp = sub.add_parser("spreads", help="Orderbücher auswerten")
    sp.add_argument("--min", type=int, default=50, help="Mindestmessungen je Markt")
    sp.set_defaults(func=cmd_spreads)

    tv = sub.add_parser("tradingview", help="Trades als Pine-Skript für TradingView")
    tv.add_argument("--out", default="state/bot-trades.pine")
    tv.set_defaults(func=cmd_tradingview)

    j = sub.add_parser("jupiter", help="Solana-Rundlauf messen (Jupiter)")
    j.add_argument("--pruefen", action="store_true",
                   help="Mint-Adressen einmalig kontrollieren statt messen")
    j.set_defaults(func=cmd_jupiter)

    d = sub.add_parser("doctor", help="Konfiguration und Datenquellen prüfen")
    d.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as e:
        print(f"Konfigurationsfehler: {e}", file=sys.stderr)
        return 1
    except ProviderError as e:
        print(f"Datenquelle nicht erreichbar: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
