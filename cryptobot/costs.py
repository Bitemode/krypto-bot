"""Handelskosten: Gebührenstufen, Slippage, Kostenmodell.

Die Gebührentabelle entspricht der öffentlichen Bitvavo-Staffel für EUR-Märkte
(Stand September 2026). Die Stufe richtet sich nach dem Handelsvolumen der
letzten 30 Tage.
"""

from __future__ import annotations

from dataclasses import dataclass

# (30-Tage-Volumen in EUR, Maker, Taker) — aufsteigend sortiert
BITVAVO_EUR_TIERS: tuple[tuple[float, float, float], ...] = (
    (0.0, 0.0015, 0.0025),
    (100_000.0, 0.0010, 0.0020),
    (250_000.0, 0.0009, 0.0018),
    (500_000.0, 0.0008, 0.0016),
    (1_000_000.0, 0.0005, 0.0014),
    (2_500_000.0, 0.0003, 0.0012),
    (5_000_000.0, 0.0001, 0.0010),
    (10_000_000.0, 0.0000, 0.0008),
    (25_000_000.0, 0.0000, 0.0004),
    (100_000_000.0, 0.0000, 0.0002),
)

# Zum Vergleich: Kraken-Spot, unterste Stufen
KRAKEN_EUR_TIERS: tuple[tuple[float, float, float], ...] = (
    (0.0, 0.0040, 0.0080),
    (2_500.0, 0.0030, 0.0060),
    (10_000.0, 0.0022, 0.0038),
    (25_000.0, 0.0020, 0.0035),
)


@dataclass(frozen=True)
class CostModel:
    """Kostenmodell für eine Börse.

    maker_only:  True  -> es wird immer die Maker-Gebühr angesetzt (Limit-Order,
                          POST_ONLY, nimmt in Kauf, dass nicht jede Order gefüllt wird)
                 False -> Taker-Gebühr (Market- oder aggressive Limit-Order)
    slippage_bps: zusätzlicher Abschlag je Seite in Basispunkten, unabhängig von
                  der Gebühr (halber Spread plus Marktwirkung)
    """

    tiers: tuple[tuple[float, float, float], ...] = BITVAVO_EUR_TIERS
    maker_only: bool = True
    slippage_bps: float = 5.0
    name: str = "Bitvavo EUR"

    def rate(self, volume_30d: float) -> float:
        """Gebührensatz je Seite bei gegebenem 30-Tage-Volumen."""
        maker, taker = self.tiers[0][1], self.tiers[0][2]
        for threshold, mk, tk in self.tiers:
            if volume_30d >= threshold:
                maker, taker = mk, tk
        return maker if self.maker_only else taker

    def cost_per_side(self, volume_30d: float) -> float:
        """Gesamtkosten je Seite (Gebühr + Slippage) als Anteil des Ordervolumens."""
        return self.rate(volume_30d) + self.slippage_bps / 10_000.0

    def trade_cost(self, notional: float, volume_30d: float) -> float:
        """Kosten einer einzelnen Order in Währungseinheiten."""
        return abs(notional) * self.cost_per_side(volume_30d)

    def breakeven_roundtrip(self, volume_30d: float) -> float:
        """Bruttogewinn, den ein vollständiger Trade braucht, um bei null zu landen."""
        return 2.0 * self.cost_per_side(volume_30d)


class VolumeTracker:
    """Führt das rollierende 30-Tage-Handelsvolumen für die Gebührenstufe."""

    def __init__(self) -> None:
        self._events: list[tuple[float, float]] = []  # (zeit_in_tagen, notional)
        self._total = 0.0

    def add(self, day: float, notional: float) -> None:
        self._events.append((day, abs(notional)))
        self._total += abs(notional)

    def volume_30d(self, day: float) -> float:
        cutoff = day - 30.0
        while self._events and self._events[0][0] < cutoff:
            self._total -= self._events.pop(0)[1]
        return self._total
