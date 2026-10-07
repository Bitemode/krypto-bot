"""Journal: jede Entscheidung und jede Buchung wird protokolliert — auch die Ablehnungen.

Ein stiller Nichtkauf ist ein Ereignis. Ohne die abgelehnten Signale lässt sich später
nicht auswerten, ob die Filter richtig lagen.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class Decision:
    """Was der Bot zu einem Coin entschieden hat und warum."""

    market: str
    entscheidung: str                 # KAUFEN | BEOBACHTEN | NICHT KAUFEN | HALTEN | VERKAUFEN
    trend_strength: float | None
    regime: str
    regime_factor: float
    ziel_gewicht: float
    ist_gewicht: float
    datenvertrauen: int
    filter_aktiv: list[str] = field(default_factory=list)
    begruendung: list[str] = field(default_factory=list)
    kurs: float | None = None
    spread: float | None = None
    slippage: float | None = None
    vola: float | None = None


@dataclass
class Fill:
    zeit: str
    market: str
    seite: str
    notional: float
    preis: float
    gebuehr: float
    grund: str
    realisiert: float = 0.0


class Journal:
    def __init__(self, jsonl: Path, trades_csv: Path) -> None:
        self.jsonl = jsonl
        self.trades_csv = trades_csv
        self.jsonl.parent.mkdir(parents=True, exist_ok=True)
        if not self.trades_csv.exists():
            with self.trades_csv.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(
                    ["zeit", "markt", "seite", "notional_eur", "preis",
                     "gebuehr_eur", "grund", "realisiert_eur"]
                )

    def log_run(
        self,
        entscheidungen: list[Decision],
        portfolio: dict[str, Any],
        konfiguration: dict[str, Any],
        modus: str,
    ) -> None:
        eintrag = {
            "zeit": datetime.now(timezone.utc).isoformat(),
            "modus": modus,
            "portfolio": portfolio,
            "konfiguration": konfiguration,
            "entscheidungen": [asdict(d) for d in entscheidungen],
        }
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(eintrag, ensure_ascii=False) + "\n")

    def log_fill(self, fill: Fill) -> None:
        with self.trades_csv.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                fill.zeit, fill.market, fill.seite, round(fill.notional, 2),
                fill.preis, round(fill.gebuehr, 4), fill.grund, round(fill.realisiert, 2),
            ])

    def read_trades(self) -> list[dict[str, Any]]:
        if not self.trades_csv.exists():
            return []
        with self.trades_csv.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))
