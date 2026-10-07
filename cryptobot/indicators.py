"""Kennzahlen auf Kursreihen. Alles nur auf abgeschlossenen Kerzen — kein Repainting."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """Einfacher gleitender Durchschnitt."""
    return series.rolling(window, min_periods=window).mean()


def pct_return(series: pd.Series, window: int) -> pd.Series:
    """Rendite über `window` Perioden."""
    return series / series.shift(window) - 1.0


def log_returns(series: pd.Series) -> pd.Series:
    return np.log(series / series.shift(1))


def ewma_volatility(series: pd.Series, halflife: int, periods_per_year: float) -> pd.Series:
    """Annualisierte Volatilität als EWMA der Log-Renditen.

    Halbwertszeit in Perioden. Annualisiert mit sqrt(periods_per_year).
    """
    r = log_returns(series)
    var = r.pow(2).ewm(halflife=halflife, min_periods=halflife).mean()
    return np.sqrt(var * periods_per_year)


def rolling_covariance(returns: pd.DataFrame, window: int) -> pd.DataFrame:
    """Kovarianzmatrix der letzten `window` Perioden (letzter Zeitpunkt)."""
    tail = returns.tail(window).dropna(axis=1, how="all")
    return tail.cov()


def portfolio_volatility(
    weights: pd.Series, cov: pd.DataFrame, periods_per_year: float
) -> float:
    """Erwartete annualisierte Portfoliovolatilität aus Gewichten und Kovarianzmatrix."""
    cols = [c for c in weights.index if c in cov.columns]
    if not cols:
        return 0.0
    w = weights[cols].to_numpy(dtype=float)
    c = cov.loc[cols, cols].to_numpy(dtype=float)
    c = np.nan_to_num(c, nan=0.0)
    var = float(w @ c @ w)
    return float(np.sqrt(max(var, 0.0) * periods_per_year))


def max_drawdown(equity: pd.Series) -> float:
    """Größter prozentualer Rückgang vom laufenden Höchststand (negativ)."""
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())
