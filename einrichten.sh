#!/bin/bash
# Doppelklick genügt. Richtet den Bot ein und lässt ihn ab sofort täglich um 10:00 laufen.
cd "$(dirname "$0")" || exit 1
clear
echo "================================================"
echo "   Krypto-Bot einrichten"
echo "================================================"
echo

# --- Python vorhanden? -----------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "Auf diesem Mac fehlt noch Python."
  echo
  echo "Gleich öffnet sich ein Fenster von Apple mit der Frage, ob die"
  echo "Entwicklerwerkzeuge installiert werden sollen. Klick dort auf"
  echo "\"Installieren\" und warte, bis es fertig ist (dauert ein paar Minuten)."
  echo "Danach diese Datei einfach nochmal doppelklicken."
  echo
  read -r -p "Weiter mit der Eingabetaste …" _
  xcode-select --install 2>/dev/null
  exit 0
fi
echo "Python gefunden:  $(python3 --version)"

# --- Bibliotheken ----------------------------------------------------------
echo
if python3 -c "import pandas, numpy, yaml" 2>/dev/null; then
  echo "Bibliotheken:     schon vorhanden"
else
  echo "Bibliotheken:     werden installiert, das dauert 1-2 Minuten …"
  echo
  python3 -m pip install --user --quiet --upgrade pip 2>/dev/null
  if ! python3 -m pip install --user --quiet pandas numpy pyyaml; then
    echo
    echo "Die Installation ist fehlgeschlagen. Bist du mit dem Internet verbunden?"
    echo
    echo "(Fenster kann jetzt geschlossen werden)"
    exit 1
  fi
  echo "Bibliotheken:     fertig"
fi

# --- Einrichten ------------------------------------------------------------
echo
echo "------------------------------------------------"
bash install_macos.sh
ERGEBNIS=$?
echo "------------------------------------------------"
echo

if [ $ERGEBNIS -eq 0 ]; then
  echo "Fertig. Der Bot läuft ab jetzt zweimal täglich: 09:30 nur Notfallprüfung, 17:00 mit Umschichtung."
  echo
  echo "Was du noch tun kannst:"
  echo "  \"2 Bot jetzt laufen lassen\"  - einmal sofort ausführen"
  echo "  \"3 Stand ansehen\"            - Positionen und letzte Läufe"
else
  echo "Da ist etwas schiefgegangen. Schick mir am besten den Text von oben."
fi
echo
echo "(Fenster kann jetzt geschlossen werden)"
