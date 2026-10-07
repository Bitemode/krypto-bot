"""Auswertung der mitgeschriebenen Orderbücher.

Beantwortet drei Fragen, und zwar getrennt — weil sie verschiedene Antworten
haben können:

  1. Was kostet ein Rundlauf als NEHMER?
     Slippage Kauf + Slippage Verkauf PLUS die Taker-Gebühr beider Seiten.
     Das ist die Zahl, die in Fassung 7 über den ganzen Plan entscheidet:
     über 0,20 % ist Schluss.

  2. Was brächte ein Rundlauf als STELLER?
     Der Spread, den man dann einnimmt statt zahlt, MINUS die Maker-Gebühr
     beider Seiten. Was davon nach adverser Selektion übrig bleibt, sagt
     diese Messung NICHT. Dafür braucht es ausgeführte Orders, nicht nur
     Bücher.

Die Gebühr stammt aus demselben Kostenmodell, mit dem der Bot bucht
(cryptobot.costs, Bitvavo-Staffel nach 30-Tage-Volumen) — nicht aus einer
zweiten, womöglich abweichenden Zahl in dieser Datei. Nur so ist der
Vergleich mit Abschnitt 4 (Solana, Pool-Gebühren bereits enthalten) ein
Vergleich gleicher Grössen.

  3. Führt der 17:00-Lauf besser aus als der 09:30-Lauf?
     Spread je Stunde deutscher Zeit. Die Stundendaten sagen, dass 16 Uhr die
     lebhafteste Stunde ist — mehr Volumen spricht für bessere Ausführung,
     mehr Schwankung meist für breitere Spreads. Hier steht, was gewinnt.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .costs import CostModel
from .orderbuch import GROESSEN, lesen


def _gebuehren_bps(volumen_30d: float) -> tuple[float, float]:
    """Maker- und Taker-Satz je Seite in Basispunkten, aus dem Kostenmodell
    des Bots. Slippage bleibt hier aussen vor — die wird gemessen, nicht
    geschaetzt, und steckt schon in den Orderbuchzahlen."""
    maker = CostModel(maker_only=True, slippage_bps=0.0).rate(volumen_30d)
    taker = CostModel(maker_only=False, slippage_bps=0.0).rate(volumen_30d)
    return maker * 10_000.0, taker * 10_000.0


def _f(wert: Any) -> float | None:
    try:
        return float(wert)
    except (TypeError, ValueError):
        return None


def _median(werte: list[float]) -> float | None:
    if not werte:
        return None
    s = sorted(werte)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _quantil(werte: list[float], q: float) -> float | None:
    if not werte:
        return None
    s = sorted(werte)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def _berliner_stunde(iso: str) -> int | None:
    """Ortszeit ohne Zeitzonendatenbank: EU-Regel, letzter Sonntag im März
    01:00 UTC bis letzter Sonntag im Oktober."""
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None

    def letzter_sonntag(jahr: int, monat: int) -> datetime:
        d = datetime(jahr, monat, 31 if monat == 3 else 31)
        while d.month != monat:
            d -= timedelta(days=1)
        while d.weekday() != 6:
            d -= timedelta(days=1)
        return d.replace(hour=1, minute=0, second=0, microsecond=0, tzinfo=t.tzinfo)

    sommer = letzter_sonntag(t.year, 3) <= t < letzter_sonntag(t.year, 10)
    return (t + timedelta(hours=2 if sommer else 1)).hour


def bericht(ordner: Path, min_messungen: int = 50, volumen_30d: float = 0.0) -> str:
    maker_bps, taker_bps = _gebuehren_bps(volumen_30d)
    zeilen = lesen(ordner)
    if not zeilen:
        return ("Noch keine Messungen. Der Mitschreiber legt alle 5 Minuten eine\n"
                "Zeile je Markt an, zwischen 08:00 und 21:00 Ortszeit.\n"
                "Pruefen mit:  bash install_macos.sh --status")

    je_markt: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    je_stunde: dict[int, list[float]] = defaultdict(list)
    tage = set()

    for z in zeilen:
        m = z.get("markt")
        if not m:
            continue
        tage.add(str(z.get("zeit", ""))[:10])
        s = _f(z.get("spread_bps"))
        if s is not None and 0 <= s < 2000:
            je_markt[m]["spread"].append(s)
            h = _berliner_stunde(str(z.get("zeit", "")))
            if h is not None:
                je_stunde[h].append(s)
        for g in GROESSEN:
            k, v = _f(z.get(f"slip_kauf_{int(g)}")), _f(z.get(f"slip_verk_{int(g)}"))
            if k is not None and v is not None:
                je_markt[m][f"rund_{int(g)}"].append(k + v)
            je_markt[m][f"erreichbar_{int(g)}"].append(1.0 if k is not None else 0.0)
        t = _f(z.get("tiefe_ask_100bp"))
        if t is not None:
            je_markt[m]["tiefe"].append(t)

    aus: list[str] = []
    aus.append("=" * 96)
    n_fmt = f"{len(zeilen):,}".replace(",", ".")
    aus.append(f"ORDERBUCH — {n_fmt} Messungen, {len(je_markt)} Maerkte, {len(tage)} Tage")
    aus.append("=" * 96)

    # --- 1. Nehmer-Rundlauf ---------------------------------------------
    aus.append("")
    aus.append("1. RUNDLAUF ALS NEHMER — ALLES drin, in Prozent vom Einsatz")
    aus.append(f"   Gemessene Slippage Kauf + Verkauf PLUS Taker-Gebuehr {taker_bps/100:.3f} % je Seite")
    aus.append(f"   (Bitvavo-Staffel bei {volumen_30d:,.0f} EUR 30-Tage-Volumen)."
               .replace(",", "."))
    aus.append(f"   Allein die Gebuehr kostet also schon {2*taker_bps/100:.3f} % je Rundlauf.")
    aus.append("   Entscheidungsgrenze aus Fassung 7: ueber 0,20 % ist der Stundenhandel beendet.")
    aus.append("")
    kopf = (f"   {'Markt':<13}{'Messungen':>10}{'Spread':>9}" +
            "".join(f"{str(int(g))+' EUR':>10}" for g in GROESSEN) +
            f"{'Tiefe ±1%':>12}  Urteil")
    aus.append(kopf)
    aus.append("   " + "-" * 89)

    reihen = []
    for m, d in je_markt.items():
        if len(d["spread"]) < min_messungen:
            continue
        roh = {int(g): _median(d[f"rund_{int(g)}"]) for g in GROESSEN}
        reihen.append((m, _median(d["spread"]) or 0.0,
                       {g: (None if w is None else w + 2.0 * taker_bps)
                        for g, w in roh.items()},
                       _median(d["tiefe"]) or 0.0,
                       {int(g): (sum(d[f"erreichbar_{int(g)}"]) / len(d[f"erreichbar_{int(g)}"])
                                 if d[f"erreichbar_{int(g)}"] else 0.0) for g in GROESSEN},
                       len(d["spread"])))
    reihen.sort(key=lambda r: (r[2].get(500) if r[2].get(500) is not None else 9e9))

    for m, sp, rund, tiefe, erreich, n in reihen:
        zeile = f"   {m:<13}{n:>10}{sp/100:>8.3f}%"
        for g in GROESSEN:
            w = rund.get(int(g))
            if w is None:
                zeile += f"{'zu duenn':>10}"
            else:
                zeile += f"{w/100:>9.3f}%"
        r500 = rund.get(500)
        urteil = ("—" if r500 is None else
                  "traegt" if r500 <= 20 else
                  "grenzwertig" if r500 <= 40 else "zu teuer")
        zeile += f"{tiefe:>11,.0f}€  {urteil}".replace(",", ".")
        aus.append(zeile)

    tragen = [r for r in reihen if (r[2].get(500) or 9e9) <= 20]
    aus.append("")
    aus.append(f"   {len(tragen)} von {len(reihen)} Maerkten unter 0,20 % Rundlauf bei 500 EUR.")
    if tragen:
        aus.append(f"   Guenstigste: {', '.join(r[0] for r in tragen[:8])}")

    # --- 2. Steller-Sicht -------------------------------------------------
    aus.append("")
    aus.append("2. RUNDLAUF ALS STELLER — Spread eingenommen, Maker-Gebuehr abgezogen")
    aus.append(f"   Netto = Spread minus {maker_bps/100:.3f} % Maker-Gebuehr je Seite "
               f"({2*maker_bps/100:.3f} % je Rundlauf).")
    aus.append("   Was nach adverser Selektion bleibt, sagt diese Messung NICHT:")
    aus.append("   dafuer braucht es ausgefuehrte Orders, nicht nur Buecher.")
    aus.append("")
    aus.append(f"   {'Markt':<13}{'Spread Median':>15}{'25 %':>9}{'75 %':>9}"
               f"{'Netto je Rundlauf':>20}")
    aus.append("   " + "-" * 66)
    positiv = 0
    for m, sp, rund, tiefe, erreich, n in sorted(reihen, key=lambda r: -r[1])[:12]:
        d = je_markt[m]["spread"]
        netto = sp - 2.0 * maker_bps
        if netto > 0:
            positiv += 1
        aus.append(f"   {m:<13}{sp/100:>14.3f}%{(_quantil(d, 0.25) or 0)/100:>8.3f}%"
                   f"{(_quantil(d, 0.75) or 0)/100:>8.3f}%{netto/100:>19.3f}%")
    aus.append("")
    aus.append("   Ein negativer Nettowert heisst: der Spread deckt nicht einmal die Gebuehr.")
    aus.append("   Stellen lohnt dort nicht, egal wie gut die Ausfuehrung ist.")
    alle_netto = [r[1] - 2.0 * maker_bps for r in reihen]
    if alle_netto:
        gut = len([x for x in alle_netto if x > 0])
        aus.append(f"   {gut} von {len(alle_netto)} Maerkten tragen die Maker-Gebuehr ueberhaupt.")

    # --- 3. Tageszeit -----------------------------------------------------
    aus.append("")
    aus.append("3. TAGESZEIT — fuehrt der 17:00-Lauf besser aus als der 09:30-Lauf?")
    aus.append("")
    aus.append(f"   {'Stunde':>8}{'Messungen':>12}{'Spread Median':>16}")
    aus.append("   " + "-" * 36)
    for h in sorted(je_stunde):
        if len(je_stunde[h]) < 20:
            continue
        marke = "  <<<" if h in (9, 10, 16, 17) else ""
        aus.append(f"   {h:>6}:00{len(je_stunde[h]):>12}"
                   f"{(_median(je_stunde[h]) or 0)/100:>15.3f}%{marke}")

    frueh = [x for h in (9, 10) for x in je_stunde.get(h, [])]
    spaet = [x for h in (16, 17) for x in je_stunde.get(h, [])]
    aus.append("")
    if len(frueh) >= 20 and len(spaet) >= 20:
        mf, ms = _median(frueh) or 0.0, _median(spaet) or 0.0
        aus.append(f"   Fenster 09-11 Uhr: {mf/100:.3f} %   ({len(frueh)} Messungen)")
        aus.append(f"   Fenster 16-18 Uhr: {ms/100:.3f} %   ({len(spaet)} Messungen)")
        if abs(mf - ms) < 0.1 * max(mf, ms, 1e-9):
            aus.append("   -> Kein nennenswerter Unterschied. Die Wahl des Fensters ist dann egal.")
        elif ms < mf:
            aus.append(f"   -> Nachmittags enger, um {(mf-ms)/100:.3f} Prozentpunkte. "
                       "Der 17:00-Lauf fuehrt besser aus.")
        else:
            aus.append(f"   -> Vormittags enger, um {(ms-mf)/100:.3f} Prozentpunkte. "
                       "Dann sollte der Vormittag umschichten, nicht der Nachmittag.")
    else:
        aus.append("   Noch zu wenige Messungen in beiden Fenstern fuer einen Vergleich.")

    aus.append("")
    aus.append("   Mindestens zwei volle Wochen abwarten, bevor daraus etwas gefolgert wird.")
    aus.append("   Ein Markttag ist eine Stichprobe von eins.")
    beste = [r[2].get(500) for r in reihen if r[2].get(500) is not None]
    # --min soll auch hier greifen, sonst bleibt Abschnitt 4 leer, waehrend
    # Abschnitt 1 schon Zahlen zeigt — und der Vergleich am Ende faellt aus.
    onchain = onchain_abschnitt(ordner, min_messungen=min(20, min_messungen),
                                boerse_bestwert_bps=min(beste) if beste else None)
    if onchain:
        aus.append(onchain)
    return "\n".join(aus)

def onchain_abschnitt(ordner: Path, min_messungen: int = 20,
                      boerse_bestwert_bps: float | None = None) -> str:
    """Solana gegen Boerse — die beiden Zahlen nebeneinander."""
    zeilen = [z for z in lesen(ordner, praefix="jup") if z.get("markt")]
    if not zeilen:
        return ""
    je: dict[tuple[str, str], list[float]] = defaultdict(list)
    fehler: dict[str, int] = defaultdict(int)
    for z in zeilen:
        if z.get("hinweis"):
            fehler[str(z["markt"])] += 1
            continue
        w = _f(z.get("rundlauf_bps"))
        if w is None or not (-100 <= w <= 20_000):
            continue
        je[(str(z["markt"]), str(z.get("betrag_usdc", "")))].append(w)

    aus = ["", "=" * 96,
           "4. SOLANA (Jupiter) — Rundlauf auf der Kette, zum Vergleich",
           "=" * 96,
           "   Gemessen wird hin und zurueck: beide Pool-Gebuehren und beider Preisabrieb.",
           "   NICHT enthalten ist MEV — das passiert bei der Ausfuehrung, nicht bei der",
           "   Abfrage. Oeffentliche Quellen nennen dafuer 0,25 bis 2,5 % je Handel. Die",
           "   Zahlen unten sind also die UNTERGRENZE, nicht die Kosten.", ""]
    aus.append(f"   {'Markt':<14}{'Betrag':>10}{'Messungen':>11}{'Rundlauf':>11}"
               f"{'25 %':>9}{'75 %':>9}")
    aus.append("   " + "-" * 64)
    for (markt, betrag), werte in sorted(je.items(), key=lambda kv: (kv[0][0], float(kv[0][1] or 0))):
        if len(werte) < min_messungen:
            continue
        aus.append(f"   {markt:<14}{betrag+' $':>10}{len(werte):>11}"
                   f"{(_median(werte) or 0)/100:>10.3f}%"
                   f"{(_quantil(werte, 0.25) or 0)/100:>8.3f}%"
                   f"{(_quantil(werte, 0.75) or 0)/100:>8.3f}%")
    if fehler:
        aus.append("")
        for markt, n in sorted(fehler.items()):
            aus.append(f"   nicht abrufbar: {markt} ({n} Versuche) — Mint-Adresse oder Pool pruefen")
    aus.append("")
    # Nur die 500er-Zeilen, denn die Boersenseite ist ebenfalls 500. Ueber alle
    # Betraege zu minimieren waere wieder derselbe Fehler wie vorher: der
    # guenstigste Wert kaeme dann aus der kleinsten Order.
    def _ist_500(betrag: str) -> bool:
        w = _f(betrag)
        return w is not None and abs(w - 500.0) < 1e-6

    bestwerte = [_median(w) for (_, betrag), w in je.items()
                 if _ist_500(betrag) and len(w) >= min_messungen]
    bestwerte = [b for b in bestwerte if b is not None]
    if boerse_bestwert_bps is not None and bestwerte:
        kette_roh = min(bestwerte)
        # Ein negativer Rundlauf heisst nicht Gewinn, sondern zwei Abfragen
        # ueber verschiedene Pools. Fuer den Vergleich zaehlt er als null.
        kette = max(0.0, kette_roh)
        aus.append("   BEIDE SEITEN GLEICH GERECHNET — alles drin, je 500 EUR bzw. 500 $:")
        aus.append(f"     Boerse guenstigster Markt: {boerse_bestwert_bps/100:>7.3f} %  "
                   "(Slippage + Taker-Gebuehr beider Seiten)")
        aus.append(f"     Kette guenstigster Markt:  {kette/100:>7.3f} %  "
                   "(Pool-Gebuehren + Preisabrieb beider Seiten)")
        if kette_roh < 0:
            aus.append(f"     (gemessen {kette_roh/100:.3f} % — negativ ist ein Abfrage-"
                       "Artefakt, hier als 0 gewertet)")
        if kette < boerse_bestwert_bps:
            aus.append(f"     -> Die Kette ist um {(boerse_bestwert_bps-kette)/100:.3f} "
                       "Prozentpunkte guenstiger — VOR MEV.")
            aus.append("     Schon der untere MEV-Richtwert von 0,25 % je Handel dreht das um.")
        else:
            aus.append(f"     -> Die Boerse ist um {(kette-boerse_bestwert_bps)/100:.3f} "
                       "Prozentpunkte guenstiger, MEV noch gar nicht gerechnet.")
    aus.append("")
    aus.append("   Vergleichsmassstab: die Zielmarke aus Fassung 7 liegt bei 0,20 % Rundlauf.")
    aus.append("   Ein Wert darunter wird erst dann interessant, wenn MEV dazugerechnet ist.")
    return "\n".join(aus)
