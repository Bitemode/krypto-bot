"""Erzeugt ein Pine-Skript, das die TATSÄCHLICHEN Trades des Bots im Chart zeigt.

Warum nicht der Strategy-Tester von TradingView: der rechnet standardmässig mit
Gebühren und Slippage von null, kann auf Heikin-Ashi und Renko repainten, holt
über request.security(..., lookahead_on) Daten aus der Zukunft, und optimiert
über die ganze Historie ohne geteilte Hälften. Jede dieser vier Fallen erzeugt
eine schöne Kurve, die nichts bedeutet.

Hier passiert das Gegenteil: gezeichnet wird, was wirklich gebucht wurde —
echter Kurs, echte Gebühr, echter Zeitpunkt. Nichts wird simuliert, nichts
optimiert. Das Skript ist ein Fenster auf das Journal, kein zweiter Backtest.

Benutzung: Skript erzeugen, in TradingView unter Pine Editor einfügen, auf den
Chart legen. Es zeigt automatisch die Trades des Marktes, den der Chart gerade
anzeigt (BTC/EUR -> BTC-EUR).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

MAX_MARKEN = 480          # Pine erlaubt höchstens 500 Labels je Skript


def _ms(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _f(wert: Any, standard: float = 0.0) -> float:
    try:
        return float(wert)
    except (TypeError, ValueError):
        return standard


def sammeln(profile: list[tuple[str, Any, bool]], journal_fuer) -> list[dict[str, Any]]:
    """Trades aller Profile einsammeln, neueste zuerst begrenzt auf MAX_MARKEN."""
    raus: list[dict[str, Any]] = []
    for name, cfg_p, champion in profile:
        for t in journal_fuer(cfg_p).read_trades():
            ms = _ms(str(t.get("zeit", "")))
            if ms is None:
                continue
            raus.append({
                "ms": ms,
                "profil": name,
                "champion": champion,
                "markt": str(t.get("markt", "")),
                "seite": str(t.get("seite", "")).upper(),
                "preis": _f(t.get("preis")),
                "eur": _f(t.get("notional_eur")),
                "gebuehr": _f(t.get("gebuehr_eur")),
                "grund": str(t.get("grund", "")),
                "realisiert": _f(t.get("realisiert_eur")),
            })
    raus.sort(key=lambda z: z["ms"])
    if len(raus) > MAX_MARKEN:
        raus = raus[-MAX_MARKEN:]          # die jüngsten behalten
    return raus


def _pine_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _pine_float(x: float) -> str:
    """Immer mit Dezimalpunkt. Mischt man in array.from() Ganz- und Kommazahlen,
    kann Pine den Typ der Liste nicht bestimmen und uebersetzt das Skript nicht."""
    s = f"{x:.10g}"
    if not any(z in s for z in ".eE"):
        s += ".0"
    return s


def erzeugen(trades: list[dict[str, Any]], profilnamen: list[str]) -> str:
    """Das fertige Pine-v6-Skript als Text."""
    if not trades:
        return ("// Noch keine Trades im Journal.\n"
                "//@version=6\nindicator(\"Bot-Trades\", overlay = true)\n")

    def spalte(schluessel, wandeln=str):
        return ", ".join(wandeln(t[schluessel]) for t in trades)

    wahl = ["Alle"] + profilnamen
    kopf = f"""//@version=6
// ---------------------------------------------------------------------------
// Tatsaechlich gebuchte Trades des Krypto-Bots.
// Erzeugt am {datetime.now().strftime('%d.%m.%Y %H:%M')} aus dem Journal.
// {len(trades)} Buchungen aus {len({t['markt'] for t in trades})} Maerkten.
//
// Das hier ist KEIN Backtest. Es wird nichts simuliert und nichts optimiert —
// gezeichnet wird, was gebucht wurde: echter Kurs, echte Gebuehr, echte Zeit.
// Der Chart zeigt automatisch den Markt, den du gerade offen hast.
// ---------------------------------------------------------------------------
indicator("Bot-Trades", overlay = true, max_labels_count = 500)

profil_wahl = input.string("Alle", "Profil", options = [{", ".join(_pine_str(w) for w in wahl)}])
nur_kaeufe  = input.bool(false, "Nur Kaeufe zeigen")
zeige_text  = input.bool(true,  "Beschriftung einblenden")

markt_hier = syminfo.basecurrency + "-" + syminfo.currency

var int[]    t_zeit   = array.from({spalte('ms')})
var string[] t_markt  = array.from({spalte('markt', _pine_str)})
var string[] t_profil = array.from({spalte('profil', _pine_str)})
var string[] t_seite  = array.from({spalte('seite', _pine_str)})
var float[]  t_preis  = array.from({spalte('preis', _pine_float)})
var float[]  t_eur    = array.from({spalte('eur', _pine_float)})
var float[]  t_geb    = array.from({spalte('gebuehr', _pine_float)})
var string[] t_grund  = array.from({spalte('grund', _pine_str)})
var float[]  t_real   = array.from({spalte('realisiert', _pine_float)})

anzahl = array.size(t_zeit)
dauer  = timeframe.in_seconds(timeframe.period) * 1000

// Die Buchungen sind nach Zeit sortiert, und Kerzen laufen vorwaerts. Deshalb
// merkt sich das Skript, wie weit es gekommen ist, statt auf jeder Kerze alle
// Buchungen durchzugehen. Sonst waere die Arbeit Kerzen mal Buchungen — auf
// einem Minutenchart ueber Monate bricht TradingView das ab.
var int zeiger = 0

zeichne(int i) =>
    kauf   = array.get(t_seite, i) == "BUY"
    preis  = array.get(t_preis, i)
    profil = array.get(t_profil, i)
    real   = array.get(t_real, i)
    info   = profil + "  " + array.get(t_seite, i)
    info  := info + "\\n" + str.tostring(array.get(t_eur, i), "#.##") + " EUR"
    info  := info + "\\n" + str.tostring(preis, "#.########") + " je Stueck"
    info  := info + "\\n" + array.get(t_grund, i)
    info  := info + "\\nGebuehr " + str.tostring(array.get(t_geb, i), "#.####") + " EUR"
    if not kauf
        info := info + "\\nRealisiert " + str.tostring(real, "#.##") + " EUR"
    stil  = kauf ? label.style_label_up : label.style_label_down
    farbe = kauf ? color.teal : (real >= 0 ? color.blue : color.maroon)
    marke = zeige_text ? (kauf ? "K " : "V ") + profil : ""
    label.new(bar_index, preis, marke, style = stil, color = color.new(farbe, 20), textcolor = color.white, size = size.small, tooltip = info)

passt(int i) =>
    kauf = array.get(t_seite, i) == "BUY"
    ok = array.get(t_markt, i) == markt_hier
    ok := ok and (profil_wahl == "Alle" or profil_wahl == array.get(t_profil, i))
    ok and not (nur_kaeufe and not kauf)

if barstate.isconfirmed
    while zeiger < anzahl
        zt = array.get(t_zeit, zeiger)
        if zt >= time + dauer
            break
        if zt >= time and passt(zeiger)
            zeichne(zeiger)
        zeiger := zeiger + 1
else if barstate.islast
    // Die laufende Kerze: zeichnen, ohne den Zeiger zu bewegen. TradingView
    // verwirft und wiederholt die letzte Kerze bei jedem Tick, doppelte
    // Beschriftungen entstehen dadurch nicht.
    j = zeiger
    while j < anzahl
        zt2 = array.get(t_zeit, j)
        if zt2 >= time + dauer
            break
        if zt2 >= time and passt(j)
            zeichne(j)
        j := j + 1
"""
    return kopf


def schreiben(text: str, pfad: Path) -> Path:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(text, encoding="utf-8")
    return pfad
