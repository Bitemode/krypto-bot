"""Die drei zu vergleichenden Varianten.

Die Strategielogik ist in allen drei identisch — nur Kerzenauflösung, Fensterlängen
und Rebalance-Takt unterscheiden sich. Alle Fenster sind in Zeit gedacht und in
Kerzen umgerechnet, damit die Varianten wirklich vergleichbar sind.
"""

from __future__ import annotations

from .strategy import StrategyConfig

BARS_PER_DAY = {"1d": 1, "4h": 6, "15m": 96}


def config_for(
    interval: str,
    fast_days: float,
    slow_days: float,
    momentum_days: float,
    rebalance_days: float,
    **overrides,
) -> StrategyConfig:
    """Baut eine Konfiguration aus Zeitangaben in Tagen."""
    bpd = BARS_PER_DAY[interval]
    base = dict(
        fast_window=max(2, round(fast_days * bpd)),
        slow_window=max(3, round(slow_days * bpd)),
        momentum_window=max(2, round(momentum_days * bpd)),
        regime_slow=max(3, round(slow_days * bpd)),
        regime_momentum=max(2, round(momentum_days * bpd)),
        rebalance_every=max(1, round(rebalance_days * bpd)),
        vol_halflife=max(5, round(20 * bpd if interval == "1d" else 5 * bpd)),
        cov_window=max(30, round(90 * bpd if interval == "1d" else 20 * bpd)),
        min_history=max(60, round(slow_days * bpd * 2)),
        periods_per_year=365.0 * bpd,
    )
    base.update(overrides)
    return StrategyConfig(**base)


def variant_1_daily() -> StrategyConfig:
    """Fassung 6: Tageskerzen, Trendfenster 30/60 Tage, Rebalance alle 10 Tage."""
    return config_for("1d", fast_days=30, slow_days=60, momentum_days=30, rebalance_days=10)


def variant_1_daily_fast() -> StrategyConfig:
    """Wie Variante 1, aber Rebalance alle 5 Tage (die ursprüngliche Vorgabe)."""
    return config_for("1d", fast_days=30, slow_days=60, momentum_days=30, rebalance_days=5)


def variant_2_swing_4h() -> StrategyConfig:
    """4-Stunden-Kerzen, Trendfenster 7/14 Tage, Rebalance alle 2 Tage."""
    return config_for("4h", fast_days=7, slow_days=14, momentum_days=7, rebalance_days=2)


def variant_3_daytrading() -> StrategyConfig:
    """15-Minuten-Kerzen, Trendfenster 4/12 Stunden, Rebalance alle 2 Stunden.

    Entspricht ungefähr 12 Prüfzeitpunkten pro Tag und damit dem, was gemeinhin
    als Daytrading bezeichnet wird.
    """
    return config_for(
        "15m",
        fast_days=4 / 24,
        slow_days=12 / 24,
        momentum_days=4 / 24,
        rebalance_days=2 / 24,
        max_positions=5,
    )


ALL_VARIANTS = {
    "V1 Tageskerzen / 10 Tage": ("1d", variant_1_daily),
    "V1b Tageskerzen / 5 Tage": ("1d", variant_1_daily_fast),
    "V2 4h-Kerzen / 2 Tage": ("4h", variant_2_swing_4h),
    "V3 15m-Kerzen / 2 Stunden": ("15m", variant_3_daytrading),
}
