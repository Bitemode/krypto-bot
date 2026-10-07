"""Strategie nach Fassung 6: Ensemble-Trendfolge mit zweistufiger Volatilitätssteuerung.

Alle Funktionen bekommen ausschließlich Daten bis zur letzten abgeschlossenen Kerze.
Die Engine stellt sicher, dass die Ausführung erst danach stattfindet.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .indicators import ewma_volatility, pct_return, portfolio_volatility, sma


@dataclass(frozen=True)
class StrategyConfig:
    """Alle Parameter der Strategie. Einheit aller Fenster: Kerzen (bars)."""

    # Signal
    fast_window: int = 30
    slow_window: int = 60
    momentum_window: int = 30
    trend_threshold: float = 2.0 / 3.0

    # Regime
    regime_market: str = "BTC-EUR"
    regime_slow: int = 60
    regime_momentum: int = 30

    # Positionsgröße
    target_vol_position: float = 0.10
    target_vol_portfolio: float = 0.35
    max_weight: float = 0.25
    min_weight: float = 0.02
    max_positions: int = 8
    vol_halflife: int = 20
    cov_window: int = 90

    # Ablauf
    rebalance_every: int = 10
    no_trade_band: float = 0.20
    min_history: int = 120

    # Zeitachse
    periods_per_year: float = 365.0

    def warmup(self) -> int:
        """Anzahl Kerzen, die vor dem ersten Signal vorliegen müssen."""
        return max(
            self.slow_window,
            self.fast_window,
            self.momentum_window,
            self.cov_window,
            self.vol_halflife * 3,
            self.min_history,
        )


def trend_strength(closes: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    """Trendstärke T je Markt am letzten Zeitpunkt der übergebenen Daten.

    T = (Kurs > SMA_fast) + (Kurs > SMA_slow) + (Rendite über momentum_window > 0), geteilt durch 3.
    Märkte ohne ausreichende Historie erhalten NaN.
    """
    last = closes.iloc[-1]

    def window_mean(window: int) -> pd.Series:
        tail = closes.tail(window)
        if len(tail) < window:
            return pd.Series(np.nan, index=closes.columns)
        # Nur Märkte mit lückenloser Historie im Fenster gelten als gültig
        return tail.mean().where(tail.notna().all(), np.nan)

    fast = window_mean(cfg.fast_window)
    slow = window_mean(cfg.slow_window)

    if len(closes) > cfg.momentum_window:
        past = closes.iloc[-1 - cfg.momentum_window]
        mom = last / past - 1.0
    else:
        mom = pd.Series(np.nan, index=closes.columns)

    parts = pd.DataFrame({"f": last > fast, "s": last > slow, "m": mom > 0.0})
    valid = fast.notna() & slow.notna() & mom.notna() & last.notna()
    t = parts.sum(axis=1) / 3.0
    return t.where(valid, np.nan)


def regime_factor(closes: pd.DataFrame, cfg: StrategyConfig) -> tuple[float, str]:
    """Regime-Faktor und Label aus dem Leitmarkt (Standard Bitcoin)."""
    if cfg.regime_market not in closes.columns:
        return 1.0, "OHNE_GATE"
    s = closes[cfg.regime_market].dropna()
    if len(s) < max(cfg.regime_slow, cfg.regime_momentum) + 1:
        return 0.0, "UNBEKANNT"
    above = bool(s.iloc[-1] > s.tail(cfg.regime_slow).mean())
    positive = bool(s.iloc[-1] / s.iloc[-1 - cfg.regime_momentum] - 1.0 > 0.0)
    score = int(above) + int(positive)
    return {2: (1.0, "RISK_ON"), 1: (0.5, "NEUTRAL"), 0: (0.0, "RISK_OFF")}[score]


def target_weights(
    closes: pd.DataFrame, cfg: StrategyConfig, use_regime: bool = True
) -> tuple[pd.Series, dict]:
    """Zielgewichte je Markt (Summe ≤ 1). Rest ist Cash.

    Stufe 1: wᵢ = Ziel-Einzelvolatilität / realisierte Volatilität, gedeckelt.
    Stufe 2: Skalierung auf die Ziel-Portfoliovolatilität über die Kovarianzmatrix.
    """
    info: dict = {}
    t = trend_strength(closes, cfg)
    factor, label = regime_factor(closes, cfg) if use_regime else (1.0, "OHNE_GATE")
    info["regime"] = label
    info["regime_factor"] = factor

    candidates = t[(t >= cfg.trend_threshold) & t.notna()]
    info["kandidaten"] = int(len(candidates))
    empty = pd.Series(0.0, index=closes.columns, dtype=float)
    if factor <= 0.0 or candidates.empty:
        info["portfolio_vol"] = 0.0
        info["skalierung"] = 0.0
        return empty, info

    vol_tail = min(len(closes), cfg.vol_halflife * 6)
    vol = pd.Series(
        {
            m: ewma_volatility(
                closes[m].tail(vol_tail).dropna(), cfg.vol_halflife, cfg.periods_per_year
            ).iloc[-1]
            for m in candidates.index
        }
    ).dropna()
    vol = vol[vol > 0]
    if vol.empty:
        info["portfolio_vol"] = 0.0
        info["skalierung"] = 0.0
        return empty, info

    # Auswahl: stärkster Trend zuerst, bei Gleichstand die ruhigere Position
    ranking = pd.DataFrame({"t": candidates.reindex(vol.index), "vol": vol})
    ranking = ranking.sort_values(["t", "vol"], ascending=[False, True])
    chosen = ranking.head(cfg.max_positions)

    raw = (cfg.target_vol_position / chosen["vol"]).clip(upper=cfg.max_weight)
    raw = raw[raw >= cfg.min_weight]
    if raw.empty:
        info["portfolio_vol"] = 0.0
        info["skalierung"] = 0.0
        return empty, info

    returns = np.log(closes[list(raw.index)] / closes[list(raw.index)].shift(1))
    cov = returns.tail(cfg.cov_window).cov()
    pvol = portfolio_volatility(raw, cov, cfg.periods_per_year)
    scale = 1.0 if pvol <= 0 else min(1.0, cfg.target_vol_portfolio / pvol)

    weights = (raw * scale * factor).clip(lower=0.0)
    total = float(weights.sum())
    if total > 1.0:  # nie mehr als das Kapital einsetzen (kein Hebel)
        weights = weights / total

    info["portfolio_vol"] = float(pvol)
    info["skalierung"] = float(scale)
    info["investitionsquote"] = float(weights.sum())
    return empty.add(weights, fill_value=0.0).fillna(0.0), info


def apply_no_trade_band(
    current: pd.Series,
    target: pd.Series,
    band: float,
    min_weight: float,
    max_total: float = 1.0,
) -> pd.Series:
    """Unterdrückt Anpassungen, deren relative Abweichung unter dem Band liegt.

    Ein Auf- oder Abbau auf null bzw. von null weg wird immer ausgeführt.

    Wichtig: Das Band lässt gewachsene Positionen stehen und kann dadurch neue
    Positionen obendrauf legen, sodass die Summe über `max_total` steigt. Das wäre
    ein Kauf mit nicht vorhandenem Geld. Übersteigt die Summe die Grenze, werden
    deshalb alle Gewichte proportional zurückskaliert.
    """
    result = current.copy()
    for market in target.index:
        cur = float(current.get(market, 0.0))
        tgt = float(target.get(market, 0.0))
        if cur == 0.0 and tgt == 0.0:
            continue
        if cur == 0.0 or tgt == 0.0:
            result[market] = tgt
            continue
        if abs(tgt - cur) / max(cur, min_weight) >= band:
            result[market] = tgt
    result = result.fillna(0.0).clip(lower=0.0)
    total = float(result.sum())
    if total > max_total > 0.0:
        result = result * (max_total / total)
    return result
