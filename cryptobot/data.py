"""Kursdaten laden, prüfen und — für Methodentests — synthetisch erzeugen."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PriceData:
    """Schlusskurse als DataFrame: Index = Zeitpunkt, Spalten = Märkte."""

    closes: pd.DataFrame
    periods_per_year: float
    source: str

    def __post_init__(self) -> None:
        if not self.closes.index.is_monotonic_increasing:
            raise ValueError("Zeitindex ist nicht aufsteigend sortiert")
        if self.closes.index.has_duplicates:
            raise ValueError("Zeitindex enthält Duplikate")

    @property
    def markets(self) -> list[str]:
        return list(self.closes.columns)

    def returns(self) -> pd.DataFrame:
        return np.log(self.closes / self.closes.shift(1))


def load_csv_directory(path: str | Path, periods_per_year: float) -> PriceData:
    """Lädt je Markt eine CSV mit den Spalten `timestamp,close`.

    Dateiname ohne Endung ist der Marktname, z. B. `BTC-EUR.csv`.
    """
    directory = Path(path)
    files = sorted(directory.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"Keine CSV-Dateien in {directory}")
    frames: dict[str, pd.Series] = {}
    for f in files:
        df = pd.read_csv(f)
        missing = {"timestamp", "close"} - set(df.columns)
        if missing:
            raise ValueError(f"{f.name}: Spalten fehlen: {sorted(missing)}")
        ts = pd.to_datetime(df["timestamp"], utc=True, unit="ms", errors="coerce")
        if ts.isna().all():
            ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        s = pd.Series(df["close"].astype(float).to_numpy(), index=ts, name=f.stem)
        s = s[~s.index.duplicated(keep="last")].sort_index()
        frames[f.stem] = s
    closes = pd.DataFrame(frames).sort_index()
    return PriceData(closes=closes, periods_per_year=periods_per_year, source=str(directory))


def validate(data: PriceData, max_gap_periods: int = 3) -> list[str]:
    """Einfache Qualitätsprüfungen. Gibt eine Liste von Befunden zurück (leer = sauber)."""
    findings: list[str] = []
    for market in data.markets:
        s = data.closes[market].dropna()
        if s.empty:
            findings.append(f"{market}: keine Daten")
            continue
        if (s <= 0).any():
            findings.append(f"{market}: nicht-positive Kurse")
        rel = s.pct_change().abs()
        extreme = int((rel > 0.5).sum())
        if extreme:
            findings.append(f"{market}: {extreme} Sprünge über 50 % je Periode")
        full = data.closes[market]
        inner = full.loc[s.index[0] : s.index[-1]]
        gaps = int(inner.isna().sum())
        if gaps > max_gap_periods:
            findings.append(f"{market}: {gaps} fehlende Perioden innerhalb der Historie")
    return findings


def make_synthetic(
    n_markets: int = 20,
    n_periods: int = 1800,
    periods_per_year: float = 365.0,
    seed: int = 7,
    trend_strength: float = 0.0,
    start: str = "2021-01-01",
    cycles: bool = True,
) -> PriceData:
    """Erzeugt Kursreihen mit kryptotypischen Eigenschaften.

    Enthalten sind: ein gemeinsamer Marktfaktor (erzeugt hohe Korrelationen),
    Volatilitätsclusterung, fette Ränder und mehrjährige Auf- und Abwärtsphasen.

    `trend_strength` steuert die einzige Annahme, die über reines Rauschen
    hinausgeht: eine positive Autokorrelation der Renditen über mehrere Tage.
      0.0  -> keine Vorhersagbarkeit (Nullhypothese; misst reine Kostenlast)
      >0   -> es existiert ein Trendeffekt (Gegenhypothese; prüft, ob die Engine ihn findet)
    """
    rng = np.random.default_rng(seed)

    # Alle Größen sind auf Tageskerzen kalibriert und werden auf die tatsächliche
    # Auflösung umgerechnet: Drift linear, Volatilität mit der Wurzel der Zeit.
    bars_per_day = periods_per_year / 365.0
    drift_scale = 1.0 / bars_per_day
    vol_scale = 1.0 / np.sqrt(bars_per_day)

    # Marktregime: mehrmonatige Phasen mit unterschiedlicher Drift.
    # cycles=False -> konstante, leicht positive Drift (reiner Zufallspfad ohne Zyklen)
    regime = np.zeros(n_periods)
    if cycles:
        t = 0
        bullish = True
        while t < n_periods:
            length = int(rng.integers(180 * bars_per_day, 420 * bars_per_day))
            drift = (0.0022 if bullish else -0.0016) * rng.uniform(0.6, 1.4) * drift_scale
            regime[t : t + length] = drift
            t += length
            bullish = not bullish
    else:
        regime[:] = 0.0004 * drift_scale

    # Gemeinsamer Marktfaktor mit Volatilitätsclusterung (GARCH-artig).
    # Kalibrierung: Marktfaktor rund 42 % Jahresvolatilität, einzelne Coins 55-85 %.
    market_ret = np.zeros(n_periods)
    base_vol = 0.022 * vol_scale
    v = base_vol**2
    for i in range(n_periods):
        shock = rng.standard_t(df=4) / np.sqrt(4 / 2)  # fette Ränder, Varianz 1
        market_ret[i] = regime[i] + np.sqrt(v) * shock
        v = 0.02 * base_vol**2 + 0.88 * v + 0.10 * (market_ret[i] - regime[i]) ** 2

    columns: dict[str, pd.Series] = {}
    freq = pd.Timedelta(days=1) / bars_per_day
    idx = pd.date_range(start=start, periods=n_periods, freq=freq, tz="UTC")
    for m in range(n_markets):
        beta = float(rng.uniform(0.7, 1.4))
        idio_vol = float(rng.uniform(0.010, 0.025)) * vol_scale
        r = beta * market_ret + idio_vol * rng.standard_t(df=5, size=n_periods) / np.sqrt(5 / 3)
        if trend_strength > 0.0:
            smoothed = pd.Series(r).ewm(halflife=10 * bars_per_day).mean().to_numpy()
            r = r + trend_strength * np.roll(smoothed, 1)
            r[0] = 0.0
        price = 100.0 * np.exp(np.cumsum(r))
        name = "BTC-EUR" if m == 0 else f"SYN{m:02d}-EUR"
        columns[name] = pd.Series(price, index=idx, name=name)

    return PriceData(
        closes=pd.DataFrame(columns),
        periods_per_year=periods_per_year,
        source=f"synthetisch(seed={seed}, trend={trend_strength})",
    )
