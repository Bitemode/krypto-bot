"""Kennzahlen zur Bewertung eines Backtest-Ergebnisses."""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .engine import BacktestResult
from .indicators import max_drawdown


@dataclass
class Metrics:
    label: str
    jahre: float
    endkapital: float
    rendite_pa: float
    volatilitaet_pa: float
    sharpe: float
    max_drawdown: float
    calmar: float
    trades: int
    trades_pro_jahr: float
    kosten_gesamt: float
    kosten_pa_pct: float
    umsatz_pa: float
    kostenquote: float
    investitionsquote: float

    def as_dict(self) -> dict:
        return asdict(self)


def compute_metrics(
    result: BacktestResult, periods_per_year: float, initial_equity: float = 10_000.0
) -> Metrics:
    # Die Aufwärmphase zählt nicht mit: dort ist per Konstruktion nichts investiert.
    warmup = int(result.diagnostics.get("warmup", 0))
    equity = result.equity.dropna().iloc[warmup:]
    exposure = result.exposure.dropna().iloc[warmup:]
    if len(equity) < 2:
        raise ValueError("Zu kurze Equity-Kurve")

    rets = equity.pct_change().dropna()
    years = len(equity) / periods_per_year
    total_return = float(equity.iloc[-1] / equity.iloc[0])
    cagr = total_return ** (1.0 / years) - 1.0 if years > 0 and total_return > 0 else -1.0
    vol = float(rets.std() * np.sqrt(periods_per_year))
    sharpe = float(rets.mean() / rets.std() * np.sqrt(periods_per_year)) if rets.std() > 0 else 0.0
    mdd = max_drawdown(equity)
    calmar = float(cagr / abs(mdd)) if mdd < 0 else float("nan")

    gross_profit = float(equity.iloc[-1] - equity.iloc[0] + result.total_costs)
    cost_ratio = float(result.total_costs / gross_profit) if gross_profit > 0 else float("nan")

    return Metrics(
        label=result.label,
        jahre=round(years, 2),
        endkapital=round(float(equity.iloc[-1]), 2),
        rendite_pa=round(cagr, 4),
        volatilitaet_pa=round(vol, 4),
        sharpe=round(sharpe, 3),
        max_drawdown=round(mdd, 4),
        calmar=round(calmar, 3) if np.isfinite(calmar) else float("nan"),
        trades=result.n_trades,
        trades_pro_jahr=round(result.n_trades / years, 1) if years > 0 else 0.0,
        kosten_gesamt=round(result.total_costs, 2),
        kosten_pa_pct=round(result.total_costs / initial_equity / years, 4) if years > 0 else 0.0,
        umsatz_pa=round(result.turnover / years, 0) if years > 0 else 0.0,
        kostenquote=round(cost_ratio, 3) if np.isfinite(cost_ratio) else float("nan"),
        investitionsquote=round(float(exposure.mean()), 3) if len(exposure) else 0.0,
    )


def metrics_for_equity(equity: pd.Series, periods_per_year: float, label: str) -> Metrics:
    """Kennzahlen für eine reine Equity-Kurve (z. B. Buy-and-hold)."""
    rets = equity.pct_change().dropna()
    years = len(equity) / periods_per_year
    total = float(equity.iloc[-1] / equity.iloc[0])
    cagr = total ** (1.0 / years) - 1.0 if years > 0 and total > 0 else -1.0
    vol = float(rets.std() * np.sqrt(periods_per_year))
    sharpe = float(rets.mean() / rets.std() * np.sqrt(periods_per_year)) if rets.std() > 0 else 0.0
    mdd = max_drawdown(equity)
    return Metrics(
        label=label,
        jahre=round(years, 2),
        endkapital=round(float(equity.iloc[-1]), 2),
        rendite_pa=round(cagr, 4),
        volatilitaet_pa=round(vol, 4),
        sharpe=round(sharpe, 3),
        max_drawdown=round(mdd, 4),
        calmar=round(float(cagr / abs(mdd)), 3) if mdd < 0 else float("nan"),
        trades=0,
        trades_pro_jahr=0.0,
        kosten_gesamt=0.0,
        kosten_pa_pct=0.0,
        umsatz_pa=0.0,
        kostenquote=float("nan"),
        investitionsquote=1.0,
    )


def table(rows: list[Metrics]) -> str:
    """Formatiert Kennzahlen als Textblock."""
    header = (
        f"{'Variante':<34}{'Rendite p.a.':>13}{'Vola':>8}{'Sharpe':>8}"
        f"{'MaxDD':>9}{'Trades/J':>10}{'Kosten p.a.':>13}{'Invest.':>9}"
    )
    lines = [header, "-" * len(header)]
    for m in rows:
        lines.append(
            f"{m.label:<34}{m.rendite_pa:>12.1%}{m.volatilitaet_pa:>8.0%}{m.sharpe:>8.2f}"
            f"{m.max_drawdown:>9.1%}{m.trades_pro_jahr:>10.0f}{m.kosten_pa_pct:>12.1%}"
            f"{m.investitionsquote:>9.0%}"
        )
    return "\n".join(lines)
