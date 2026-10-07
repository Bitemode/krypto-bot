"""Was ein Rundlauf auf Solana wirklich kostet — gemessen, nicht geschaetzt.

Das Gegenstueck zum Orderbuch-Mitschreiber, nur fuer die Kette. Er fragt bei
Jupiter zwei Preise ab und vergleicht sie:

    1. USDC -> Token  fuer einen festen Betrag
    2. Token -> USDC  fuer GENAU die Menge, die aus Schritt 1 herauskaeme

Was am Ende an USDC weniger zurueckkommt, IST der Rundlauf: beide Pool-
Gebuehren und beider Preisabrieb in einer einzigen Zahl, ohne dass irgendetwas
modelliert werden muss. Ein schoener Nebeneffekt: die Nachkommastellen des
Tokens kuerzen sich heraus, weil Schritt 2 mit der Rohmenge aus Schritt 1
rechnet. Nur die 6 Nachkommastellen von USDC muessen stimmen, und die sind
gesichert.

WAS DIESE MESSUNG NICHT ENTHAELT: MEV. Sandwich-Angriffe passieren bei der
Ausfuehrung, nicht bei der Preisabfrage. Die gemessene Zahl ist deshalb die
UNTERGRENZE der echten Kosten — der beste Fall. Fuer 200 USD werden in
oeffentlichen Quellen 0,25 bis 2,5 % MEV-Verlust genannt. Das muss zur
gemessenen Zahl addiert werden, bevor irgendjemand sie mit Bitvavo vergleicht.

Kein privater Schluessel, keine Geldboerse, kein Euro Einsatz. Es wird nur
gefragt, nie unterschrieben.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

# Jupiter hat die Adresse der Preisabfrage schon einmal gewechselt. Beide werden
# der Reihe nach versucht; welche geantwortet hat, steht in der Messzeile.
ENDPUNKTE = (
    ("lite-v1", "https://lite-api.jup.ag/swap/v1/quote"),
    ("quote-v6", "https://quote-api.jup.ag/v6/quote"),
)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DEZIMALEN = 6

SPALTEN = [
    "zeit", "markt", "betrag_usdc", "rundlauf_bps",
    "impact_hin_bps", "impact_rueck_bps", "usdc_zurueck",
    "quelle", "hinweis",
]


class JupiterFehler(RuntimeError):
    """Die Preisabfrage hat nicht geantwortet oder etwas Unerwartetes geliefert."""


def _hole(url: str, timeout: float = 15.0) -> Any:
    anfrage = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "cryptobot-messung"})
    with urllib.request.urlopen(anfrage, timeout=timeout) as antwort:
        return json.loads(antwort.read().decode("utf-8"))


def quote(eingang: str, ausgang: str, menge_roh: int,
          slippage_bps: int = 50) -> tuple[dict[str, Any], str]:
    """Eine Preisabfrage. Gibt (Antwort, Name des Endpunkts) zurueck."""
    letzter = ""
    for name, basis in ENDPUNKTE:
        url = (f"{basis}?inputMint={eingang}&outputMint={ausgang}"
               f"&amount={int(menge_roh)}&slippageBps={int(slippage_bps)}")
        try:
            d = _hole(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            letzter = f"{name}: {e}"
            continue
        except json.JSONDecodeError as e:
            letzter = f"{name}: keine gueltige JSON-Antwort ({e})"
            continue
        if not isinstance(d, dict) or "outAmount" not in d:
            # Lieber abbrechen als eine Zahl erfinden: eine geaenderte
            # Schnittstelle muss auffallen, nicht stillschweigend durchlaufen.
            letzter = f"{name}: Antwort ohne Feld outAmount ({str(d)[:120]})"
            continue
        return d, name
    raise JupiterFehler(letzter or "kein Endpunkt erreichbar")


def _f(wert: Any, standard: float = 0.0) -> float:
    try:
        return float(wert)
    except (TypeError, ValueError):
        return standard


def rundlauf(token_mint: str, betrag_usdc: float,
             slippage_bps: int = 50) -> dict[str, Any]:
    """Hin und zurueck. Das Ergebnis ist der Rundlauf in Basispunkten."""
    hinein = int(round(betrag_usdc * 10 ** USDC_DEZIMALEN))
    if hinein <= 0:
        raise JupiterFehler("Betrag muss groesser als null sein")

    hin, quelle = quote(USDC_MINT, token_mint, hinein, slippage_bps)
    token_roh = int(_f(hin.get("outAmount")))
    if token_roh <= 0:
        raise JupiterFehler("Hinweg liefert keine Tokenmenge")

    # Rueckweg mit GENAU der Rohmenge — damit kuerzen sich die Nachkommastellen.
    rueck, _ = quote(token_mint, USDC_MINT, token_roh, slippage_bps)
    zurueck_roh = int(_f(rueck.get("outAmount")))
    if zurueck_roh <= 0:
        raise JupiterFehler("Rueckweg liefert keinen USDC-Betrag")

    zurueck = zurueck_roh / 10 ** USDC_DEZIMALEN
    kosten_bps = (1.0 - zurueck / betrag_usdc) * 10_000
    return {
        "rundlauf_bps": round(kosten_bps, 2),
        "usdc_zurueck": round(zurueck, 4),
        "impact_hin_bps": round(_f(hin.get("priceImpactPct")) * 10_000, 2),
        "impact_rueck_bps": round(_f(rueck.get("priceImpactPct")) * 10_000, 2),
        "quelle": quelle,
    }


def zeile(symbol: str, token_mint: str, betrag_usdc: float,
          slippage_bps: int = 50, jetzt: datetime | None = None) -> dict[str, Any]:
    """Eine Messzeile. Ein Fehler wird mitgeschrieben, nicht verschluckt."""
    z: dict[str, Any] = {
        "zeit": (jetzt or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "markt": f"{symbol}/USDC",
        "betrag_usdc": round(betrag_usdc, 2),
        "rundlauf_bps": None, "impact_hin_bps": None, "impact_rueck_bps": None,
        "usdc_zurueck": None, "quelle": "", "hinweis": "",
    }
    try:
        z.update(rundlauf(token_mint, betrag_usdc, slippage_bps))
    except JupiterFehler as e:
        z["hinweis"] = str(e)[:160]
    return z


def pruefen(maerkte: dict[str, str], betrag_usdc: float = 100.0) -> list[str]:
    """Einmalige Kontrolle vor dem ersten Vertrauen in die Daten.

    Eine falsche Mint-Adresse misst stillschweigend einen anderen Token. Hier
    wird je Markt ein Rundlauf versucht und das Ergebnis auf Plausibilitaet
    geprueft — ein Rundlauf unter 0 oder ueber 50 % ist kein Messwert, sondern
    ein Hinweis auf die falsche Adresse oder einen leeren Pool.
    """
    raus = []
    for symbol, mint in maerkte.items():
        try:
            e = rundlauf(mint, betrag_usdc)
        except JupiterFehler as fehler:
            raus.append(f"  FEHLER  {symbol:<8} {mint[:12]}…  {fehler}")
            continue
        bps = e["rundlauf_bps"]
        if bps < -1 or bps > 5000:
            raus.append(f"  PRUEFEN {symbol:<8} {mint[:12]}…  Rundlauf {bps/100:.2f} % "
                        "— unplausibel, Adresse oder Pool pruefen")
        else:
            raus.append(f"  ok      {symbol:<8} {mint[:12]}…  Rundlauf {bps/100:.3f} % "
                        f"({e['quelle']})")
    return raus
