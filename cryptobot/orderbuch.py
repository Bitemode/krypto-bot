"""Orderbuch-Mitschreiber: misst, was ein Handel WIRKLICH kostet.

Warum das gebraucht wird: Die beworbene Gebühr steht auf einer Webseite, der
Spread steht im Orderbuch und ist jede Minute anders. Gemessen an 366.410
Stundenkerzen liegt der gesamte Unterschied zwischen "funktioniert" und
"funktioniert nicht" zwischen 0,40 % und 0,12 % Rundlaufkosten — ein Bereich,
der kleiner ist als der Spread mancher Altcoins. Eine so entscheidende Zahl
wird gemessen und nicht geglaubt.

Zwei Fragen werden mit denselben Daten beantwortet:

  1. Was kostet ein Rundlauf als NEHMER?   Spread + Slippage beider Seiten.
  2. Was brächte ein Rundlauf als STELLER? Der Spread ist dann die Einnahme,
     nicht die Ausgabe — abzüglich dessen, was man verliert, weil man genau
     dann bedient wird, wenn der andere mehr weiss (adverse Selektion).

Der Mitschreiber ändert am Bot nichts. Er handelt nicht, er braucht keinen
API-Schlüssel, er liest nur öffentliche Orderbücher und schreibt CSV-Zeilen.
"""

from __future__ import annotations

import csv
import gzip
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

GROESSEN = (100.0, 500.0, 2000.0)      # Euro, in denen gemessen wird
TIEFEN = (0.005, 0.01)                  # ±0,5 % und ±1 % vom Mittelkurs

SPALTEN = [
    "zeit", "markt", "bid", "ask", "mid", "spread_bps",
    "bid_eur", "ask_eur",
    "tiefe_bid_50bp", "tiefe_ask_50bp", "tiefe_bid_100bp", "tiefe_ask_100bp",
] + [f"slip_kauf_{int(g)}" for g in GROESSEN] + [f"slip_verk_{int(g)}" for g in GROESSEN]


def effektivpreis(stufen: Iterable[tuple[float, float]], notional_eur: float) -> float | None:
    """Mengengewichteter Preis beim Durchlaufen einer Orderbuchseite.

    Gibt None zurück, wenn die sichtbare Tiefe nicht reicht — dann ist die
    Order in dieser Grösse schlicht nicht ausführbar, und ein geschätzter Wert
    wäre eine Erfindung.
    """
    rest, kosten, menge = notional_eur, 0.0, 0.0
    for preis, verfuegbar in stufen:
        if preis <= 0 or verfuegbar <= 0:
            continue
        wert = preis * verfuegbar
        nehme = min(rest, wert)
        kosten += nehme
        menge += nehme / preis
        rest -= nehme
        if rest <= 1e-9:
            break
    if rest > 1e-9 or menge <= 0:
        return None
    return kosten / menge


def slippage_bps(book, notional_eur: float, seite: str) -> float | None:
    """Abweichung vom Mittelkurs in Basispunkten. "kauf" läuft die Asks hoch,
    "verkauf" die Bids hinunter."""
    if not book.bids or not book.asks:
        return None
    mid = (book.asks[0][0] + book.bids[0][0]) / 2.0
    if mid <= 0:
        return None
    stufen = book.asks if seite == "kauf" else book.bids
    preis = effektivpreis(stufen, notional_eur)
    if preis is None:
        return None
    ab = (preis / mid - 1.0) if seite == "kauf" else (1.0 - preis / mid)
    return round(ab * 10_000, 2)


def _eur_am_besten(stufen) -> float:
    return round(stufen[0][0] * stufen[0][1], 2) if stufen else 0.0


def zeile(book, jetzt: datetime | None = None) -> dict[str, Any] | None:
    """Eine Messzeile aus einem Orderbuch."""
    if not book.bids or not book.asks:
        return None
    bid, ask = book.bids[0][0], book.asks[0][0]
    mid = (bid + ask) / 2.0
    if mid <= 0 or ask < bid:          # gekreuztes Buch: Datenfehler, nicht verwerten
        return None
    z: dict[str, Any] = {
        "zeit": (jetzt or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "markt": book.market,
        "bid": bid, "ask": ask, "mid": round(mid, 10),
        "spread_bps": round((ask - bid) / mid * 10_000, 2),
        "bid_eur": _eur_am_besten(book.bids),
        "ask_eur": _eur_am_besten(book.asks),
    }
    for pct, name in zip(TIEFEN, ("50bp", "100bp")):
        z[f"tiefe_bid_{name}"] = round(book.depth_within(pct, "bids"), 2)
        z[f"tiefe_ask_{name}"] = round(book.depth_within(pct, "asks"), 2)
    for g in GROESSEN:
        z[f"slip_kauf_{int(g)}"] = slippage_bps(book, g, "kauf")
        z[f"slip_verk_{int(g)}"] = slippage_bps(book, g, "verkauf")
    return z


# -- Schreiben --------------------------------------------------------------
def tagesdatei(ordner: Path, jetzt: datetime | None = None,
               praefix: str = "buch") -> Path:
    tag = (jetzt or datetime.now(timezone.utc)).date().isoformat()
    return ordner / f"{praefix}-{tag}.csv"


def anhaengen(zeilen: list[dict[str, Any]], ordner: Path,
              jetzt: datetime | None = None, spalten: list[str] | None = None,
              praefix: str = "buch") -> Path:
    """Zeilen an die Tagesdatei anhängen; Kopfzeile nur beim Anlegen."""
    ordner.mkdir(parents=True, exist_ok=True)
    SPALTEN_ = spalten or SPALTEN
    pfad = tagesdatei(ordner, jetzt, praefix)
    neu = not pfad.exists()
    with pfad.open("a", encoding="utf-8", newline="") as f:
        schreiber = csv.DictWriter(f, fieldnames=SPALTEN_, extrasaction="ignore")
        if neu:
            schreiber.writeheader()
        for z in zeilen:
            schreiber.writerow({k: ("" if z.get(k) is None else z.get(k)) for k in SPALTEN_})
    return pfad


def rotieren(ordner: Path, tage: int = 3, praefix: str = "buch") -> int:
    """Dateien, die älter als `tage` sind, packen. Spart rund 90 % Platz."""
    if not ordner.exists():
        return 0
    heute = datetime.now(timezone.utc).date()
    gepackt = 0
    for p in sorted(ordner.glob(f"{praefix}-*.csv")):
        try:
            tag = datetime.fromisoformat(p.stem.removeprefix(f"{praefix}-")).date()
        except ValueError:
            continue
        if (heute - tag).days < tage:
            continue
        with p.open("rb") as roh, gzip.open(p.with_suffix(".csv.gz"), "wb") as ziel:
            shutil.copyfileobj(roh, ziel)
        p.unlink()
        gepackt += 1
    return gepackt


def lesen(ordner: Path, praefix: str = "buch") -> list[dict[str, Any]]:
    """Alle Messzeilen, gepackt und ungepackt."""
    raus: list[dict[str, Any]] = []
    if not ordner.exists():
        return raus
    for p in sorted(list(ordner.glob(f"{praefix}-*.csv"))
                    + list(ordner.glob(f"{praefix}-*.csv.gz"))):
        oeffnen = gzip.open if p.suffix == ".gz" else open
        try:
            with oeffnen(p, "rt", encoding="utf-8", newline="") as f:
                for z in csv.DictReader(f):
                    raus.append(z)
        except (OSError, csv.Error):
            continue                    # eine kaputte Datei darf die Auswertung nicht stoppen
    return raus
