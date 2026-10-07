#!/usr/bin/env python3
"""Taeglicher Gesundheitscheck des Krypto-Bots.

Prueft anhand der Dateien im Bot-Ordner, ob alle Systeme laufen. Braucht nur
die Dateien — keinen Server-Zugang, keinen Mac. Laeuft daher genauso auf einer
frischen Kopie des GitHub-Projekts wie im Ordner auf dem Server oder dem Mac.

    python3 server/tagescheck.py [ORDNER]

Rueckgabe: 0 = alles in Ordnung, 1 = Warnung, 2 = Fehler.
Nur Standardbibliothek; PyYAML wird benutzt, wenn vorhanden.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
OK, WARN, FEHLER, INFO = "ok", "WARNUNG", "FEHLER", "info"

# Wie alt darf der Herzschlag des Servers sein? Abgleich ist stuendlich.
HERZSCHLAG_MAX_STUNDEN = 2.5
# Wie spaet darf ein Lauf nach seiner Planzeit beginnen (Nachholen nach Neustart)?
LAUF_SPIELRAUM_STUNDEN = 3
# Ein Lauf, der laenger als das hier "beginnt" ohne zu enden, haengt.
LAUF_HAENGT_STUNDEN = 0.75   # auf dem Server dauert ein Lauf 1-2 Minuten
# Tagesveraenderung, ab der ein Profilwert verdaechtig ist (Bewertungsfehler?)
SPRUNG_GRENZE = 0.08


def _cfg(ordner: Path) -> dict:
    pfad = ordner / "config.yaml"
    if not pfad.exists():
        return {}
    try:
        import yaml  # noqa: WPS433
        return yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _laufzeiten(cfg: dict) -> list[tuple[int, int]]:
    roh = (cfg.get("schedule") or {}).get("run_times_local") or ["09:30", "17:00"]
    zeiten = []
    for x in roh:
        if isinstance(x, int):              # YAML liest 17:00 ohne Anfuehrungszeichen als 1020
            zeiten.append(divmod(x, 60))
        else:
            h, m = str(x).strip().split(":")
            zeiten.append((int(h), int(m)))
    return zeiten


class Bericht:
    def __init__(self) -> None:
        self.zeilen: list[tuple[str, str, str]] = []

    def add(self, stufe: str, bereich: str, text: str) -> None:
        self.zeilen.append((stufe, bereich, text))

    @property
    def gesamt(self) -> str:
        stufen = {s for s, _, _ in self.zeilen}
        if FEHLER in stufen:
            return FEHLER
        if WARN in stufen:
            return WARN
        return OK

    def text(self, jetzt: datetime) -> str:
        kopf = {OK: "ALLES IN ORDNUNG", WARN: "WARNUNG", FEHLER: "FEHLER"}[self.gesamt]
        aus = [f"Krypto-Bot Tagescheck — {jetzt.astimezone(BERLIN):%d.%m.%Y %H:%M} — {kopf}", ""]
        reihenfolge = {FEHLER: 0, WARN: 1, OK: 2, INFO: 3}
        for stufe, bereich, text in sorted(self.zeilen, key=lambda z: reihenfolge[z[0]]):
            aus.append(f"  {stufe:<8} {bereich:<12} {text}")
        return "\n".join(aus)


# --- 1. Herzschlag ---------------------------------------------------------
def pruefe_herzschlag(ordner: Path, jetzt: datetime, b: Bericht) -> None:
    pfad = ordner / "state" / "server-status.txt"
    if not pfad.exists():
        b.add(INFO, "Server", "kein Serverstatus — der Bot laeuft offenbar noch auf dem Mac")
        return
    text = pfad.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"Server-Stand:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", text)
    if not m:
        b.add(FEHLER, "Server", "Statusdatei unlesbar")
        return
    stand = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=BERLIN)
    alter = (jetzt - stand).total_seconds() / 3600
    if alter > HERZSCHLAG_MAX_STUNDEN:
        b.add(FEHLER, "Server", f"meldet sich seit {alter:.0f} Stunden nicht "
                                f"(letzter Stand {stand:%d.%m. %H:%M})")
    else:
        b.add(OK, "Server", f"lebt (letzter Stand {stand:%H:%M} Uhr)")

    for timer in ("krypto-lauf.timer", "krypto-buch.timer", "krypto-sync.timer"):
        if timer not in text:
            b.add(FEHLER, "Zeitplaene", f"{timer} fehlt in der Liste des Servers")
    if "ACHTUNG: Abgleich gesperrt" in text:
        b.add(FEHLER, "Abgleich", "gesperrt — im Stand liegt etwas, das wie ein Schluessel aussieht")

    m = re.search(r"\s(\d{1,3})%\s+/\s*$", text, flags=re.M)
    if m and int(m.group(1)) >= 85:
        b.add(WARN, "Speicher", f"Festplatte zu {m.group(1)} % voll")


# --- 2. Handelslaeufe --------------------------------------------------------
_ZEILE = re.compile(r"^(=====|!!!!!) (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) Lauf (beginnt|beendet|fehlgeschlagen)")


def _laeufe(ordner: Path) -> list[tuple[datetime, str]]:
    pfad = ordner / "state" / "cron.log"
    if not pfad.exists():
        return []
    ereignisse = []
    for zeile in pfad.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _ZEILE.match(zeile)
        if m:
            t = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BERLIN)
            ereignisse.append((t, m.group(3)))
    return ereignisse


def pruefe_laeufe(ordner: Path, cfg: dict, jetzt: datetime, b: Bericht) -> None:
    ereignisse = _laeufe(ordner)
    if not ereignisse:
        b.add(FEHLER, "Handel", "kein einziger Lauf im Protokoll")
        return

    # Paare bilden: jedes "beginnt" mit dem naechsten Ende
    laeufe = []
    offen = None
    for t, art in ereignisse:
        if art == "beginnt":
            if offen is not None:
                laeufe.append((offen, None, "abgebrochen"))
            offen = t
        elif offen is not None:
            laeufe.append((offen, t, art))
            offen = None
    if offen is not None:
        laeufe.append((offen, None, "laeuft"))

    fenster_start = jetzt - timedelta(hours=26)
    juengste = [l for l in laeufe if l[0] >= fenster_start]

    # Erwartete Laeufe: jede Planzeit der letzten 26 h, die mindestens
    # LAUF_SPIELRAUM_STUNDEN zurueckliegt, braucht einen Beginn in ihrem Fenster.
    fehlend = []
    for tag in (jetzt.date() - timedelta(days=1), jetzt.date()):
        for h, mi in _laufzeiten(cfg):
            soll = datetime(tag.year, tag.month, tag.day, h, mi, tzinfo=BERLIN)
            if soll < fenster_start or soll > jetzt - timedelta(hours=LAUF_SPIELRAUM_STUNDEN):
                continue
            treffer = [l for l in laeufe
                       if soll - timedelta(minutes=10) <= l[0] <= soll + timedelta(hours=LAUF_SPIELRAUM_STUNDEN)]
            if not treffer:
                fehlend.append(soll)
    for soll in fehlend:
        b.add(FEHLER, "Handel", f"Lauf von {soll:%d.%m. %H:%M} fehlt")

    for beginn, ende, art in juengste:
        if art == "fehlgeschlagen":
            b.add(FEHLER, "Handel", f"Lauf von {beginn:%d.%m. %H:%M} ist fehlgeschlagen")
        elif art == "abgebrochen":
            b.add(FEHLER, "Handel", f"Lauf von {beginn:%d.%m. %H:%M} hat nie geendet")
        elif art == "laeuft" and (jetzt - beginn).total_seconds() / 3600 > LAUF_HAENGT_STUNDEN:
            b.add(FEHLER, "Handel", f"Lauf von {beginn:%d.%m. %H:%M} haengt seit "
                                    f"{(jetzt - beginn).total_seconds() / 3600:.1f} Stunden")
        elif art == "beendet" and ende and (ende - beginn).total_seconds() > 1800:
            b.add(WARN, "Handel", f"Lauf von {beginn:%d.%m. %H:%M} brauchte "
                                  f"{(ende - beginn).total_seconds() / 3600:.1f} Stunden")

    gut = [l for l in juengste if l[2] == "beendet"]
    if gut and not fehlend and all(l[2] in ("beendet", "laeuft") for l in juengste):
        letzter = max(l[1] for l in gut)
        b.add(OK, "Handel", f"{len(gut)} Laeufe in 24 h sauber beendet, zuletzt {letzter:%d.%m. %H:%M}")

    pfad = ordner / "state" / "fehler.log"
    if pfad.exists():
        neu = []
        for zeile in pfad.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", zeile)
            if m and datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BERLIN) >= fenster_start:
                neu.append(zeile.strip())
        if neu:
            b.add(FEHLER, "Handel", f"{len(neu)} {'neuer Eintrag' if len(neu) == 1 else 'neue Eintraege'} in fehler.log, zuletzt: {neu[-1]}")


# --- 3. Orderbuch und Solana -------------------------------------------------
def _csv_zeilen(pfad: Path) -> list[dict]:
    if not pfad.exists():
        gz = pfad.with_suffix(pfad.suffix + ".gz")
        if not gz.exists():
            return []
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    with pfad.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def pruefe_orderbuch(ordner: Path, cfg: dict, jetzt: datetime, b: Bericht) -> None:
    o = cfg.get("orderbuch") or {}
    von, bis = int(o.get("von_stunde", 8)), int(o.get("bis_stunde", 21))
    erwartet_tag = (bis - von) * 12
    gestern = (jetzt.astimezone(BERLIN) - timedelta(days=1)).date()
    d = ordner / "state" / "orderbuch"

    zeilen = _csv_zeilen(d / f"buch-{gestern.isoformat()}.csv")
    schnappschuesse = len({z.get("zeit", "")[:16] for z in zeilen if z.get("zeit")})
    anteil = schnappschuesse / erwartet_tag if erwartet_tag else 0
    if schnappschuesse == 0:
        b.add(FEHLER, "Orderbuch", f"gestern ({gestern:%d.%m.}) keine einzige Messung")
    elif anteil < 0.8:
        b.add(WARN, "Orderbuch", f"gestern nur {schnappschuesse} von {erwartet_tag} Messungen ({anteil:.0%})")
    else:
        b.add(OK, "Orderbuch", f"gestern {schnappschuesse} von {erwartet_tag} Messungen ({anteil:.0%})")

    jup = _csv_zeilen(d / f"jup-{gestern.isoformat()}.csv")
    if not (cfg.get("jupiter") or {}).get("aktiv", False):
        return
    if not jup:
        b.add(WARN, "Solana", f"gestern ({gestern:%d.%m.}) keine Messung")
        return
    fehlerhaft = sum(1 for z in jup if (z.get("hinweis") or "").strip())
    if fehlerhaft > len(jup) * 0.2:
        b.add(WARN, "Solana", f"{fehlerhaft} von {len(jup)} Messungen gestern ohne Ergebnis")
    else:
        b.add(OK, "Solana", f"gestern {len(jup)} Messungen")


# --- 4. Profile ----------------------------------------------------------------
def pruefe_profile(ordner: Path, jetzt: datetime, b: Bericht) -> None:
    gefunden = 0
    heute = jetzt.astimezone(BERLIN).date()
    for pfad in sorted((ordner / "state").glob("*/portfolio.json")):
        name = pfad.parent.name
        try:
            s = json.loads(pfad.read_text(encoding="utf-8"))
        except Exception:
            b.add(FEHLER, "Profile", f"{name}: Portfolio-Datei unlesbar")
            continue
        gefunden += 1
        hist = s.get("equity_history") or []
        if not hist:
            b.add(WARN, "Profile", f"{name}: noch keine Bewertung")
            continue
        letzter_tag = datetime.strptime(hist[-1][0], "%Y-%m-%d").date()
        if (heute - letzter_tag).days > 1:
            b.add(FEHLER, "Profile", f"{name}: zuletzt am {letzter_tag:%d.%m.} bewertet")
        if len(hist) >= 2 and hist[-2][1] > 0:
            sprung = hist[-1][1] / hist[-2][1] - 1
            if abs(sprung) >= SPRUNG_GRENZE:
                b.add(WARN, "Profile", f"{name}: Wert springt {sprung:+.1%} an einem Tag "
                                       f"({hist[-2][1]:,.0f} → {hist[-1][1]:,.0f} EUR) — echt oder Bewertungsfehler?")
    if gefunden == 0:
        b.add(FEHLER, "Profile", "keine Portfolio-Dateien gefunden")
    else:
        b.add(OK, "Profile", f"{gefunden} Profile bewertet")


# --- 5. Abgleich mit GitHub ------------------------------------------------------
def pruefe_abgleich(ordner: Path, jetzt: datetime, b: Bericht) -> None:
    pfad = ordner / "state" / "sync.err.log"
    if not pfad.exists():
        return
    neu = []
    for zeile in pfad.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            t = datetime.fromisoformat(zeile.split(" ", 1)[0])
        except ValueError:
            continue
        if t >= jetzt - timedelta(hours=26):
            neu.append(zeile.strip())
    if neu:
        b.add(WARN, "Abgleich", f"{len(neu)} {'Problem' if len(neu) == 1 else 'Probleme'} beim Hochladen in 24 h, zuletzt: {neu[-1][:90]}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ordner = Path(argv[0]) if argv and not argv[0].startswith("--") else Path.cwd()
    jetzt = datetime.now(timezone.utc)
    for a in argv:
        if a.startswith("--jetzt="):   # fuer Tests
            jetzt = datetime.fromisoformat(a.split("=", 1)[1]).astimezone(timezone.utc)
    cfg = _cfg(ordner)
    b = Bericht()
    pruefe_herzschlag(ordner, jetzt, b)
    pruefe_laeufe(ordner, cfg, jetzt, b)
    pruefe_orderbuch(ordner, cfg, jetzt, b)
    pruefe_profile(ordner, jetzt, b)
    pruefe_abgleich(ordner, jetzt, b)
    print(b.text(jetzt))
    return {OK: 0, WARN: 1, FEHLER: 2}[b.gesamt]


if __name__ == "__main__":
    raise SystemExit(main())
