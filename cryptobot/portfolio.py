"""Papier-Portfolio: Zustand, Grenzen, Buchungen — alles auf Platte, damit ein
Neustart nichts vergisst.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path


@dataclass
class Position:
    market: str
    units: float
    entry_price: float
    entry_date: str
    high_water: float = 0.0     # höchster Kurs seit Einstieg, für spätere Trailing-Regeln
    last_price: float = 0.0     # zuletzt gesehener Kurs — Ersatz, wenn ein Lauf keinen hat

    def value(self, price: float) -> float:
        return self.units * price

    def pnl_pct(self, price: float) -> float:
        return price / self.entry_price - 1.0 if self.entry_price > 0 else 0.0


@dataclass
class PortfolioState:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    equity_history: list[tuple[str, float]] = field(default_factory=list)
    day_start_equity: float = 0.0
    day: str = ""
    peak_equity: float = 0.0
    cooldown: dict[str, str] = field(default_factory=dict)   # Markt -> Datum des Verkaufs
    deposits: list[tuple[str, float]] = field(default_factory=list)
    realized_pnl: float = 0.0
    created: str = ""
    last_rebalance: str = ""      # Datum des letzten Rebalance, Grundlage für den Takt


class Portfolio:
    """Führt den Zustand und setzt die Grenzen durch."""

    def __init__(self, state: PortfolioState, pfad: Path, cash_buffer: float = 0.005) -> None:
        self.s = state
        self.pfad = pfad
        self.cash_buffer = cash_buffer

    # -- Laden und Speichern ------------------------------------------------
    @classmethod
    def load_or_create(cls, pfad: Path, initial_equity: float, cash_buffer: float = 0.005) -> "Portfolio":
        if pfad.exists():
            roh = json.loads(pfad.read_text(encoding="utf-8"))
            pos = {m: Position(**p) for m, p in roh.get("positions", {}).items()}
            st = PortfolioState(
                cash=float(roh["cash"]),
                positions=pos,
                equity_history=[tuple(x) for x in roh.get("equity_history", [])],
                day_start_equity=float(roh.get("day_start_equity", 0.0)),
                day=roh.get("day", ""),
                peak_equity=float(roh.get("peak_equity", 0.0)),
                cooldown=roh.get("cooldown", {}),
                deposits=[tuple(x) for x in roh.get("deposits", [])],
                realized_pnl=float(roh.get("realized_pnl", 0.0)),
                created=roh.get("created", ""),
                last_rebalance=roh.get("last_rebalance", ""),
            )
            return cls(st, pfad, cash_buffer)
        jetzt = datetime.now(timezone.utc)
        st = PortfolioState(
            cash=float(initial_equity),
            day_start_equity=float(initial_equity),
            day=jetzt.date().isoformat(),
            peak_equity=float(initial_equity),
            deposits=[(jetzt.isoformat(), float(initial_equity))],
            created=jetzt.isoformat(),
        )
        p = cls(st, pfad, cash_buffer)
        p.save()
        return p

    def save(self) -> None:
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        roh = {
            "cash": self.s.cash,
            "positions": {m: asdict(p) for m, p in self.s.positions.items()},
            "equity_history": self.s.equity_history[-2000:],
            "day_start_equity": self.s.day_start_equity,
            "day": self.s.day,
            "peak_equity": self.s.peak_equity,
            "cooldown": self.s.cooldown,
            "deposits": self.s.deposits,
            "realized_pnl": self.s.realized_pnl,
            "created": self.s.created,
            "last_rebalance": self.s.last_rebalance,
        }
        tmp = self.pfad.with_suffix(".tmp")
        tmp.write_text(json.dumps(roh, indent=2), encoding="utf-8")
        tmp.replace(self.pfad)   # atomar: ein Absturz hinterlässt keine halbe Datei

    # -- Bewertung ----------------------------------------------------------
    def kurse(self, preise: dict[str, float]) -> dict[str, float]:
        """Kurse fuer alle gehaltenen Muenzen.

        Fehlt in einem Lauf der Kurs einer gehaltenen Muenze (kein Ticker, nicht
        mehr im Universum), gilt der zuletzt gesehene Kurs, notfalls der
        Einstiegskurs — nie 0. Bis 07.10.2026 zaehlte die Position dann als
        wertlos, und das Konto schien um ihren ganzen Wert gefallen.
        Gesehene Kurse werden gemerkt und mit dem Portfolio gespeichert.
        Fuer Orders zaehlt das nicht: die brauchen weiterhin ein frisches Orderbuch.
        """
        voll = dict(preise)
        for m, p in self.s.positions.items():
            preis = voll.get(m)
            if preis:
                p.last_price = float(preis)
            else:
                ersatz = p.last_price or p.entry_price
                if ersatz:
                    voll[m] = ersatz
        return voll

    def equity(self, preise: dict[str, float]) -> float:
        preise = self.kurse(preise)
        wert = self.s.cash
        for m, p in self.s.positions.items():
            preis = preise.get(m)
            if preis:
                wert += p.value(preis)
        return wert

    def exposure(self, preise: dict[str, float]) -> float:
        eq = self.equity(preise)
        return (eq - self.s.cash) / eq if eq > 0 else 0.0

    def mark_day(self, preise: dict[str, float], heute: str | None = None) -> bool:
        """Tageswechsel feststellen und Tagesstartwert setzen. True, wenn neuer Tag."""
        heute = heute or date.today().isoformat()
        eq = self.equity(preise)
        self.s.peak_equity = max(self.s.peak_equity, eq)
        if heute != self.s.day:
            self.s.day = heute
            self.s.day_start_equity = eq
            self.s.equity_history.append((heute, round(eq, 2)))
            return True
        if self.s.equity_history and self.s.equity_history[-1][0] == heute:
            self.s.equity_history[-1] = (heute, round(eq, 2))
        else:
            self.s.equity_history.append((heute, round(eq, 2)))
        return False

    # -- Grenzen ------------------------------------------------------------
    def day_loss(self, preise: dict[str, float]) -> float:
        if self.s.day_start_equity <= 0:
            return 0.0
        return self.equity(preise) / self.s.day_start_equity - 1.0

    def drawdown(self, preise: dict[str, float]) -> float:
        if self.s.peak_equity <= 0:
            return 0.0
        return self.equity(preise) / self.s.peak_equity - 1.0

    def in_cooldown(self, market: str, heute: str, tage: int) -> bool:
        seit = self.s.cooldown.get(market)
        if not seit:
            return False
        try:
            vergangen = (date.fromisoformat(heute) - date.fromisoformat(seit)).days
        except ValueError:
            return False
        return vergangen < tage

    # -- Buchungen ----------------------------------------------------------
    def buy(self, market: str, notional: float, price: float, cost: float, heute: str) -> None:
        if notional <= 0 or price <= 0:
            return
        units = notional / price
        self.s.cash -= notional + cost
        vorhanden = self.s.positions.get(market)
        if vorhanden:
            gesamt = vorhanden.units + units
            vorhanden.entry_price = (
                vorhanden.entry_price * vorhanden.units + price * units
            ) / gesamt
            vorhanden.units = gesamt
            vorhanden.high_water = max(vorhanden.high_water, price)
        else:
            self.s.positions[market] = Position(
                market=market, units=units, entry_price=price,
                entry_date=heute, high_water=price,
            )

    def sell(self, market: str, notional: float, price: float, cost: float,
             heute: str, grund: str) -> float:
        """Verkauft für `notional` Euro. Gibt den realisierten Gewinn zurück."""
        pos = self.s.positions.get(market)
        if not pos or price <= 0:
            return 0.0
        units = min(pos.units, notional / price)
        erloes = units * price
        einstand = units * pos.entry_price
        self.s.cash += erloes - cost
        pos.units -= units
        gewinn = erloes - einstand - cost
        self.s.realized_pnl += gewinn
        if pos.units <= 1e-12:
            self.s.positions.pop(market, None)
            if grund in {"TREND_BREAK", "CATASTROPHE_STOP"}:
                self.s.cooldown[market] = heute
        return gewinn

    def deposit(self, betrag: float, wann: str) -> None:
        self.s.cash += betrag
        self.s.deposits.append((wann, betrag))

    def invested_capital(self) -> float:
        return sum(b for _, b in self.s.deposits)
