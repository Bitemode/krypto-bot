"""Ereignisgesteuerter Backtest.

Ablauf je Kerze i:
  1. Signal wird aus Daten bis einschließlich Kerze i berechnet (letzte abgeschlossene Kerze).
  2. Ausführung frühestens zur Kerze i+1 — eine volle Kerze Verzögerung.
  3. Kosten fallen auf das gehandelte Volumen an, Gebührenstufe nach rollierendem
     30-Tage-Volumen.

Damit ist Look-ahead strukturell ausgeschlossen; `lookahead_bug=True` schaltet den
Fehler absichtlich ein, damit ein Test ihn nachweisen kann.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import CostModel, VolumeTracker
from .data import PriceData
from .strategy import StrategyConfig, apply_no_trade_band, regime_factor, target_weights


@dataclass
class Trade:
    time: pd.Timestamp
    market: str
    side: str
    notional: float
    price: float
    cost: float
    reason: str


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[Trade]
    exposure: pd.Series
    regime: pd.Series
    total_costs: float
    turnover: float
    config: StrategyConfig
    cost_model: CostModel
    label: str = ""
    diagnostics: dict = field(default_factory=dict)

    @property
    def n_trades(self) -> int:
        return len(self.trades)


def run_backtest(
    data: PriceData,
    cfg: StrategyConfig,
    cost_model: CostModel,
    initial_equity: float = 10_000.0,
    use_regime: bool = True,
    catastrophe_stop: float | None = -0.25,
    cash_buffer: float = 0.005,
    lookahead_bug: bool = False,
    label: str = "",
) -> BacktestResult:
    closes = data.closes
    index = closes.index
    n = len(index)
    warmup = cfg.warmup()
    if n <= warmup + 2:
        raise ValueError(f"Zu wenig Daten: {n} Kerzen, benötigt werden mehr als {warmup + 2}")

    bars_per_day = data.periods_per_year / 365.0
    units: dict[str, float] = {}
    entry_price: dict[str, float] = {}
    cash = float(initial_equity)
    tracker = VolumeTracker()
    trades: list[Trade] = []
    equity_curve = np.full(n, np.nan)
    exposure_curve = np.full(n, np.nan)
    regime_labels: list[str] = []
    total_costs = 0.0
    turnover = 0.0

    def portfolio_value(i: int) -> float:
        value = cash
        for market, u in units.items():
            price = closes[market].iat[i]
            if np.isfinite(price):
                value += u * price
        return value

    def execute(i: int, market: str, target_value: float, reason: str) -> None:
        nonlocal cash, total_costs, turnover
        price = closes[market].iat[i]
        if not np.isfinite(price) or price <= 0:
            return
        current_value = units.get(market, 0.0) * price
        delta = target_value - current_value
        if abs(delta) < 1e-9:
            return
        day = i / bars_per_day
        cost = cost_model.trade_cost(delta, tracker.volume_30d(day))
        tracker.add(day, delta)
        cash -= delta + cost
        units[market] = units.get(market, 0.0) + delta / price
        if abs(units[market]) < 1e-12:
            units.pop(market, None)
            entry_price.pop(market, None)
        elif market not in entry_price:
            entry_price[market] = price
        total_costs += cost
        turnover += abs(delta)
        trades.append(
            Trade(
                time=index[i],
                market=market,
                side="BUY" if delta > 0 else "SELL",
                notional=float(delta),
                price=float(price),
                cost=float(cost),
                reason=reason,
            )
        )

    for i in range(n):
        equity_curve[i] = portfolio_value(i)
        invested = equity_curve[i] - cash
        exposure_curve[i] = invested / equity_curve[i] if equity_curve[i] > 0 else 0.0

        if i < warmup or i >= n - 1:
            regime_labels.append("")
            continue

        # Nur so viel Historie, wie die Kennzahlen brauchen — hält den Lauf linear.
        needed = warmup + 2
        start_idx = max(0, i + 1 - needed)
        history = (
            closes.iloc[start_idx : i + 2] if lookahead_bug else closes.iloc[start_idx : i + 1]
        )
        _, label_now = regime_factor(history, cfg) if use_regime else (1.0, "OHNE_GATE")
        regime_labels.append(label_now)

        equity_now = equity_curve[i]

        # Notfallprüfung jede Kerze: Katastrophen-Stop
        if catastrophe_stop is not None and units:
            for market in list(units.keys()):
                price = closes[market].iat[i]
                ref = entry_price.get(market)
                if ref and np.isfinite(price) and price / ref - 1.0 <= catastrophe_stop:
                    execute(i + 1, market, 0.0, "CATASTROPHE_STOP")

        # Notfallprüfung: Regime kippt auf RISK_OFF
        if use_regime and label_now == "RISK_OFF" and units:
            for market in list(units.keys()):
                execute(i + 1, market, 0.0, "REGIME_OFF")
            continue

        if (i - warmup) % cfg.rebalance_every != 0:
            continue

        weights, _ = target_weights(history, cfg, use_regime=use_regime)
        current = pd.Series(
            {m: units.get(m, 0.0) * closes[m].iat[i] / equity_now for m in closes.columns},
            dtype=float,
        ).fillna(0.0)
        adjusted = apply_no_trade_band(current, weights, cfg.no_trade_band, cfg.min_weight)

        # Ein Teil des Kapitals bleibt für Gebühren reserviert.
        investierbar = equity_now * (1.0 - cash_buffer)
        ziele = {m: float(adjusted.get(m, 0.0)) * investierbar for m in closes.columns}
        deltas = {
            m: ziele[m] - float(current.get(m, 0.0)) * equity_now for m in closes.columns
        }

        # Erst verkaufen, dann kaufen — sonst wird Geld ausgegeben, das noch gebunden ist.
        # Jede Phase darf ausschließlich in ihre Richtung wirken: Bewegt sich der Kurs
        # zwischen Entscheidung und Ausführung, darf aus einem geplanten Verkauf kein
        # Kauf werden (und umgekehrt) — sonst entsteht ein Kauf auf Kredit.
        for market in sorted(closes.columns, key=lambda m: deltas[m]):
            if deltas[market] >= -1e-9:
                continue
            preis = closes[market].iat[i + 1]
            if not np.isfinite(preis) or preis <= 0:
                continue
            aktuell = units.get(market, 0.0) * preis
            ziel = min(ziele[market], aktuell)  # nur reduzieren
            execute(i + 1, market, ziel, "REBALANCE")

        kaeufe = {m: d for m, d in deltas.items() if d > 1e-9}
        gesamt_kauf = sum(kaeufe.values())
        verfuegbar = max(0.0, cash * (1.0 - cost_model.cost_per_side(0.0) - 1e-6))
        faktor = 0.0 if gesamt_kauf <= 0 else min(1.0, verfuegbar / gesamt_kauf)
        for market, delta in kaeufe.items():
            preis = closes[market].iat[i + 1]
            if not np.isfinite(preis) or preis <= 0:
                continue
            aktuell = units.get(market, 0.0) * preis
            execute(i + 1, market, aktuell + delta * faktor, "REBALANCE")

    while len(regime_labels) < n:
        regime_labels.append("")

    equity = pd.Series(equity_curve, index=index, name="equity").ffill()
    return BacktestResult(
        equity=equity,
        trades=trades,
        exposure=pd.Series(exposure_curve, index=index, name="exposure"),
        regime=pd.Series(regime_labels[:n], index=index, name="regime"),
        total_costs=total_costs,
        turnover=turnover,
        config=cfg,
        cost_model=cost_model,
        label=label,
        diagnostics={"warmup": warmup, "bars": n},
    )


def buy_and_hold(
    data: PriceData, markets: list[str] | None = None, initial_equity: float = 10_000.0
) -> pd.Series:
    """Gleichgewichtetes Halten als Referenz (einmal kaufen, nie umschichten)."""
    closes = data.closes if markets is None else data.closes[markets]
    first_valid = closes.apply(lambda s: s.first_valid_index())
    start = max(v for v in first_valid if v is not None)
    sub = closes.loc[start:].dropna(axis=1, how="any")
    if sub.empty:
        return pd.Series(dtype=float)
    normed = sub / sub.iloc[0]
    equity = normed.mean(axis=1) * initial_equity
    return equity.reindex(closes.index).ffill().fillna(initial_equity)
