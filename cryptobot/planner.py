"""Vom Signal zur Order.

Reihenfolge, bewusst so und nicht anders:
  1. Datenqualität  — schlechte Daten heißen kein Handel, nicht vorsichtiger Handel
  2. Regime         — kippt der Gesamtmarkt, spielt das Einzelsignal keine Rolle
  3. Zielgewichte   — aus der geprüften Strategie
  4. Filter         — jeder einzeln benannt, alle ausgewertet
  5. No-Trade-Band  — kleine Abweichungen werden nicht gehandelt
  6. Erst verkaufen, dann kaufen
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from .config import Config
from .costs import BITVAVO_EUR_TIERS, CostModel
from .journal import Decision, Fill
from .portfolio import Portfolio
from .providers.bitvavo import OrderBook, Ticker
from .strategy import apply_no_trade_band, regime_factor, target_weights, trend_strength


@dataclass
class OrderIntent:
    market: str
    seite: str          # BUY | SELL
    notional: float     # in Euro, immer positiv
    limit: float
    grund: str


@dataclass
class Plan:
    entscheidungen: list[Decision]
    orders: list[OrderIntent]
    regime: str
    regime_factor: float
    equity: float
    exposure: float
    hinweise: list[str]


def datenvertrauen(
    market: str,
    closes: pd.Series,
    ticker: Ticker | None,
    book: OrderBook | None,
    cfg: Config,
    jetzt,
) -> tuple[int, list[str]]:
    """Datenvertrauens-Score von 0 bis 100 mit benannten Abzügen."""
    score, gruende = 100, []

    def abzug(punkte: int, text: str) -> None:
        nonlocal score
        score -= punkte
        gruende.append(f"−{punkte} {text}")

    if ticker is None:
        abzug(40, "kein Ticker")
    else:
        alter = (jetzt - ticker.fetched_at).total_seconds()
        if alter > cfg.get("quality.max_ticker_age_s", 300):
            abzug(15, f"Ticker {alter:.0f}s alt")
    if book is None:
        abzug(30, "kein Orderbuch")
    else:
        alter = (jetzt - book.fetched_at).total_seconds()
        if alter > cfg.get("quality.max_orderbook_age_s", 30):
            abzug(20, f"Orderbuch {alter:.0f}s alt")

    reihe = closes.dropna()
    if reihe.empty:
        return 0, ["keine Kursdaten"]
    letzte = reihe.index[-1]
    alter_h = (jetzt - letzte.to_pydatetime()).total_seconds() / 3600
    if alter_h > cfg.get("quality.max_candle_age_h", 26):
        abzug(25, f"letzte Kerze {alter_h:.0f}h alt")

    voll = closes.loc[reihe.index[0] :]
    luecken = int(voll.isna().sum())
    if luecken:
        abzug(min(30, 5 * luecken), f"{luecken} fehlende Tage")

    if (reihe <= 0).any():
        abzug(20, "nicht-positive Kurse")
    spruenge = int((reihe.pct_change().abs() > 0.5).sum())
    if spruenge:
        abzug(min(20, 10 * spruenge), f"{spruenge} Tagessprünge über 50 %")

    mindest = cfg.get("universe.min_history_days", 365)
    if len(reihe) < mindest * 1.2:
        abzug(10, f"Historie knapp ({len(reihe)} Tage)")

    return max(0, min(100, score)), gruende


def build_plan(
    closes: pd.DataFrame,
    tickers: dict[str, Ticker],
    books: dict[str, OrderBook],
    portfolio: Portfolio,
    cfg: Config,
    jetzt,
    ist_rebalance_tag: bool,
) -> Plan:
    scfg = cfg.strategy()
    heute = jetzt.date().isoformat()
    preise = {m: t.mid for m, t in tickers.items()}
    for m in closes.columns:
        preise.setdefault(m, float(closes[m].dropna().iloc[-1]) if closes[m].notna().any() else 0.0)
    preise = portfolio.kurse(preise)

    equity = portfolio.equity(preise)
    faktor, regime_label = regime_factor(closes, scfg)
    hinweise: list[str] = []

    # Portfolioweite Bremsen
    tagesverlust = portfolio.day_loss(preise)
    dd = portfolio.drawdown(preise)
    sperre_neu: list[str] = []
    if tagesverlust <= cfg.get("risk.daily_loss_limit", -0.08):
        sperre_neu.append("DAILY_LOSS_LIMIT")
        hinweise.append(f"Tagesverlust {tagesverlust:.1%} — keine Neueinstiege")
    if dd <= cfg.get("risk.drawdown_brake", -0.25):
        faktor *= 0.5
        hinweise.append(f"Drawdown {dd:.1%} — Quote halbiert")

    gewichte, info = target_weights(closes, scfg, use_regime=True)
    if faktor < info.get("regime_factor", 1.0):
        gewichte = gewichte * (faktor / max(info.get("regime_factor", 1.0), 1e-9))
    staerke = trend_strength(closes, scfg)

    ist_gewicht = pd.Series(
        {m: portfolio.s.positions[m].units * preise.get(m, 0.0) / equity if equity > 0 else 0.0
         for m in portfolio.s.positions},
        dtype=float,
    ).reindex(closes.columns).fillna(0.0)

    # --- Filter je Markt ---------------------------------------------------
    entscheidungen: list[Decision] = []
    blockiert: set[str] = set()
    vertrauen_map: dict[str, int] = {}

    for market in closes.columns:
        t = tickers.get(market)
        b = books.get(market)
        vertrauen, qgruende = datenvertrauen(market, closes[market], t, b, cfg, jetzt)
        vertrauen_map[market] = vertrauen
        filter_aktiv: list[str] = []
        begruendung: list[str] = list(qgruende)

        if vertrauen < cfg.get("quality.min_trust_score", 85):
            filter_aktiv.append("LOW_DATA_TRUST")
        if t is not None and t.spread > cfg.get("universe.max_spread", 0.005):
            filter_aktiv.append("WIDE_SPREAD")
            begruendung.append(f"Spread {t.spread:.2%}")
        if t is not None and t.volume_quote < cfg.get("universe.min_volume_eur", 250_000):
            filter_aktiv.append("LOW_VOLUME")
            begruendung.append(f"24-h-Volumen {t.volume_quote:,.0f} EUR")
        if portfolio.in_cooldown(market, heute, int(cfg.get("risk.cooldown_days", 10))):
            filter_aktiv.append("COOLDOWN")
        if regime_label == "RISK_OFF":
            filter_aktiv.append("REGIME_OFF")
        filter_aktiv.extend(sperre_neu)

        ziel = float(gewichte.get(market, 0.0))
        ist = float(ist_gewicht.get(market, 0.0))

        # Slippage prüft nur, wer tatsächlich kaufen will
        slip = None
        if b is not None and ziel > ist and equity > 0:
            slip = b.slippage_for((ziel - ist) * equity)
            if slip > cfg.get("costs.slippage_bps", 5) / 10_000 * 6:
                filter_aktiv.append("HIGH_SLIPPAGE")
                begruendung.append(
                    f"Slippage {slip:.2%}" if slip != float("inf") else "Orderbuch zu dünn"
                )

        if filter_aktiv and ziel > ist:
            blockiert.add(market)
            ziel = min(ziel, ist)

        t_wert = staerke.get(market)
        t_wert = None if t_wert is None or pd.isna(t_wert) else float(t_wert)

        if ziel > ist + 1e-9:
            entscheidung = "KAUFEN"
            begruendung.append(f"Trendstärke {t_wert:.2f}" if t_wert is not None else "Trend unbekannt")
        elif ist > 0 and ziel < ist - 1e-9:
            entscheidung = "VERKAUFEN" if ziel <= 1e-9 else "REDUZIEREN"
            if t_wert is not None and t_wert < scfg.trend_threshold:
                begruendung.append(f"Trendstärke {t_wert:.2f} unter Schwelle")
        elif ist > 0:
            entscheidung = "HALTEN"
        elif t_wert is not None and t_wert >= scfg.trend_threshold and filter_aktiv:
            entscheidung = "BEOBACHTEN"
            begruendung.append("Signal da, aber " + ", ".join(filter_aktiv))
        elif t_wert is not None and t_wert >= scfg.trend_threshold and ziel <= 1e-9:
            # Signal vorhanden, aber kein Platz oder zu kleines Zielgewicht
            entscheidung = "BEOBACHTEN"
            if len(portfolio.s.positions) >= scfg.max_positions:
                begruendung.append(
                    f"kein Platz frei ({scfg.max_positions} Positionen belegt)")
            else:
                begruendung.append("Zielgewicht unter der Mindestgröße")
        elif t_wert is not None and 0 < t_wert < scfg.trend_threshold:
            entscheidung = "BEOBACHTEN"
            begruendung.append(f"Trend im Aufbau ({t_wert:.2f})")
        else:
            entscheidung = "NICHT KAUFEN"

        entscheidungen.append(Decision(
            market=market, entscheidung=entscheidung, trend_strength=t_wert,
            regime=regime_label, regime_factor=faktor, ziel_gewicht=round(ziel, 4),
            ist_gewicht=round(ist, 4), datenvertrauen=vertrauen,
            filter_aktiv=filter_aktiv, begruendung=begruendung,
            kurs=preise.get(market), spread=t.spread if t else None,
            slippage=slip if slip not in (None, float("inf")) else None,
            vola=None,
        ))

    # --- Orders ------------------------------------------------------------
    orders: list[OrderIntent] = []
    if not ist_rebalance_tag and regime_label != "RISK_OFF":
        hinweise.append("Kein Rebalance-Tag — nur Notfallprüfung")
    else:
        ziel_serie = pd.Series({d.market: d.ziel_gewicht for d in entscheidungen}, dtype=float)
        angepasst = apply_no_trade_band(
            ist_gewicht, ziel_serie, scfg.no_trade_band, scfg.min_weight,
            max_total=1.0 - cfg.get("portfolio.cash_buffer", 0.005),
        )
        for market in closes.columns:
            ziel_wert = float(angepasst.get(market, 0.0)) * equity
            ist_wert = float(ist_gewicht.get(market, 0.0)) * equity
            delta = ziel_wert - ist_wert
            if abs(delta) < max(1.0, equity * 0.001):
                continue
            preis = preise.get(market, 0.0)
            if preis <= 0:
                continue
            if delta < 0:
                grund = "TREND_BREAK" if ziel_wert <= 1e-9 else "REBALANCE"
                if regime_label == "RISK_OFF":
                    grund = "REGIME_OFF"
                orders.append(OrderIntent(market, "SELL", -delta, preis, grund))
            elif market not in blockiert:
                orders.append(OrderIntent(market, "BUY", delta, preis, "REBALANCE"))

    # Notfall: Katastrophen-Stop, jeden Tag geprüft
    stop = cfg.get("risk.catastrophe_stop", -0.25)
    for market, pos in list(portfolio.s.positions.items()):
        preis = preise.get(market)
        if preis and pos.pnl_pct(preis) <= stop:
            if not any(o.market == market and o.seite == "SELL" for o in orders):
                orders.append(OrderIntent(market, "SELL", pos.value(preis), preis, "CATASTROPHE_STOP"))
                hinweise.append(f"{market}: Katastrophen-Stop bei {pos.pnl_pct(preis):.1%}")

    orders.sort(key=lambda o: 0 if o.seite == "SELL" else 1)
    return Plan(
        entscheidungen=entscheidungen, orders=orders, regime=regime_label,
        regime_factor=faktor, equity=equity, exposure=portfolio.exposure(preise),
        hinweise=hinweise,
    )


def execute_paper(plan: Plan, portfolio: Portfolio, cfg: Config, journal, jetzt) -> list[Fill]:
    """Führt den Plan simuliert aus. Erst verkaufen, dann kaufen — nie auf Kredit."""
    modell = CostModel(
        tiers=BITVAVO_EUR_TIERS,
        maker_only=bool(cfg.get("costs.maker_only", True)),
        slippage_bps=float(cfg.get("costs.slippage_bps", 5.0)),
    )
    heute = jetzt.date().isoformat()
    volumen_30d = sum(abs(float(t["notional_eur"])) for t in journal.read_trades()[-500:])
    fills: list[Fill] = []

    for o in plan.orders:
        if o.seite == "SELL":
            kosten = modell.trade_cost(o.notional, volumen_30d)
            gewinn = portfolio.sell(o.market, o.notional, o.limit, kosten, heute, o.grund)
            f = Fill(jetzt.isoformat(), o.market, "SELL", o.notional, o.limit,
                     kosten, o.grund, gewinn)
        else:
            verfuegbar = max(0.0, portfolio.s.cash * (1 - modell.cost_per_side(volumen_30d) - 1e-6))
            betrag = min(o.notional, verfuegbar)
            if betrag < max(10.0, plan.equity * cfg.get("risk.min_weight", 0.02) * 0.5):
                continue
            kosten = modell.trade_cost(betrag, volumen_30d)
            portfolio.buy(o.market, betrag, o.limit, kosten, heute)
            f = Fill(jetzt.isoformat(), o.market, "BUY", betrag, o.limit, kosten, o.grund)
        volumen_30d += abs(f.notional)
        journal.log_fill(f)
        fills.append(f)
    return fills
