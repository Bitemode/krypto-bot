#!/bin/bash
# Richtet den täglichen Lauf auf macOS ein (launchd).
#
#   bash install_macos.sh            installieren
#   bash install_macos.sh --status   Status ansehen
#   bash install_macos.sh --remove   wieder entfernen
#
# Der Bot läuft danach jeden Tag um 10:00 Uhr Ortszeit. Schläft der Rechner zu
# dem Zeitpunkt, holt launchd den Lauf beim nächsten Aufwachen nach.

set -euo pipefail

LABEL="de.local.cryptobot"
LABEL_BUCH="de.local.cryptobot.buch"
PROJEKT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PLIST_BUCH="$HOME/Library/LaunchAgents/$LABEL_BUCH.plist"
# Laufzeiten aus der Konfiguration lesen (Ortszeit), Rückfall auf 09:30 und 17:00
ZEITEN="$(python3 - <<'PYEOF' 2>/dev/null
import yaml
try:
    z = yaml.safe_load(open("config.yaml"))["schedule"]["run_times_local"]
    print(" ".join(str(x) for x in z))
except Exception:
    print("")
PYEOF
)"
[ -z "$ZEITEN" ] && ZEITEN="09:30 17:00"
[ -n "${CRYPTOBOT_HOUR:-}" ] && ZEITEN="${CRYPTOBOT_HOUR}:${CRYPTOBOT_MINUTE:-00}"

status() {
  echo "Projekt:  $PROJEKT"
  echo "Plist:    $PLIST"
  if launchctl list | grep -q "$LABEL"; then
    echo "Status:   aktiv"
    launchctl list "$LABEL" | sed -n '1,12p' || true
  else
    echo "Status:   nicht installiert"
  fi
  echo
  if launchctl list | grep -q "$LABEL_BUCH"; then
    echo "Orderbuch: aktiv (alle 5 Minuten)"
  else
    echo "Orderbuch: nicht installiert"
  fi
  echo "  Messungen: $(cat "$PROJEKT"/state/orderbuch/buch-*.csv 2>/dev/null | grep -c . || echo 0) Zeilen"
  tail -n 3 "$PROJEKT/state/buch.log" 2>/dev/null || true
  echo
  echo "Letzte Läufe:"
  tail -n 20 "$PROJEKT/state/cron.log" 2>/dev/null || echo "  (noch keine)"
}

entfernen() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$LABEL_BUCH" 2>/dev/null || launchctl unload "$PLIST_BUCH" 2>/dev/null || true
  rm -f "$PLIST" "$PLIST_BUCH"
  echo "Entfernt. Der Bot läuft nicht mehr automatisch."
}

case "${1:-}" in
  --status) status; exit 0 ;;
  --remove) entfernen; exit 0 ;;
esac

# --- Ort prüfen -------------------------------------------------------------
# macOS schützt Downloads, Desktop, Dokumente und iCloud (TCC). Ein Hintergrund-
# dienst darf dort nichts ausführen — der Lauf scheitert dann täglich still mit
# "Operation not permitted". Deshalb hier abfangen, statt es später zu suchen.
case "$PROJEKT" in
  "$HOME"/Downloads/*|"$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Library/Mobile*)
    ZIEL="$HOME/$(basename "$PROJEKT")"
    echo "Der Ordner liegt an einem von macOS geschützten Ort:"
    echo "    $PROJEKT"
    echo
    echo "Ein täglicher Hintergrunddienst kann dort nicht starten. Der Ordner muss"
    echo "nach $ZIEL umziehen."
    echo
    if [ -e "$ZIEL" ]; then
      echo "Dort liegt schon etwas. Bitte benenne $ZIEL um und starte neu." >&2
      exit 1
    fi
    read -r -p "Jetzt umziehen und dort einrichten? [j/N] " umzug
    if [[ "$umzug" =~ ^[jJyY]$ ]]; then
      mv "$PROJEKT" "$ZIEL" || { echo "Umzug fehlgeschlagen." >&2; exit 1; }
      echo "Umgezogen. Weiter geht es dort …"
      echo
      exec bash "$ZIEL/install_macos.sh" "$@"
    else
      echo "Abgebrochen. Verschiebe den Ordner nach $ZIEL und starte neu." >&2
      exit 1
    fi
    ;;
esac

# --- Voraussetzungen prüfen -------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 fehlt. Installiere es z. B. mit:  brew install python" >&2
  exit 1
fi
PY="$(command -v python3)"
echo "Python:   $PY ($($PY --version))"

if ! "$PY" -c "import pandas, numpy, yaml" 2>/dev/null; then
  echo
  echo "Es fehlen Bibliotheken. Installiere sie mit:"
  echo "    $PY -m pip install --user pandas numpy pyyaml"
  echo
  read -r -p "Jetzt versuchen? [j/N] " antwort
  if [[ "$antwort" =~ ^[jJyY]$ ]]; then
    "$PY" -m pip install --user pandas numpy pyyaml
  else
    echo "Abgebrochen — bitte zuerst die Bibliotheken installieren." >&2
    exit 1
  fi
fi

# --- Testlauf ---------------------------------------------------------------
echo
echo "Testlauf …"
mkdir -p "$PROJEKT/state"
if ! (cd "$PROJEKT" && "$PY" -m cryptobot.cli doctor); then
  echo "Der Testlauf ist fehlgeschlagen — es wird nichts eingerichtet." >&2
  exit 1
fi

# --- launchd-Auftrag schreiben ---------------------------------------------
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTENDE
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJEKT/run_daily.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJEKT</string>
  <key>StartCalendarInterval</key>
  <array>
$(for z in $ZEITEN; do
    printf '    <dict><key>Hour</key><integer>%d</integer><key>Minute</key><integer>%d</integer></dict>\n' \
      "$(echo "${z%%:*}" | sed 's/^0//')" "$(echo "${z##*:}" | sed 's/^0//;s/^$/0/')"
  done)
  </array>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$PROJEKT/state/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$PROJEKT/state/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
PLISTENDE

# --- Orderbuch-Mitschreiber: eigener Auftrag, alle 5 Minuten ----------------
# Bewusst getrennt vom Handelslauf: ein Fehler beim Messen darf den Bot nicht
# anhalten, und ein Fehler beim Handeln darf die Messreihe nicht unterbrechen.
cat > "$PLIST_BUCH" <<BUCHENDE
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_BUCH</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJEKT/run_buch.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJEKT</string>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$PROJEKT/state/buch.out.log</string>
  <key>StandardErrorPath</key><string>$PROJEKT/state/buch.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
BUCHENDE

chmod +x "$PROJEKT/run_daily.sh" "$PROJEKT/run_buch.sh"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL_BUCH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_BUCH" 2>/dev/null || launchctl load "$PLIST_BUCH"

echo
echo "Eingerichtet: täglich um $ZEITEN Uhr Ortszeit."
echo "  Orderbuch:   alle 5 Minuten zwischen 08:00 und 21:00 (misst nur, handelt nicht)"
echo "  Auswertung:  python3 -m cryptobot.cli spreads"
echo "  Der frühe Lauf prüft nur Notfälle, der späte schichtet um."
echo "  Protokoll:   $PROJEKT/state/cron.log"
echo "  Status:      bash install_macos.sh --status"
echo "  Sofort testen: $PROJEKT/run_daily.sh"
echo "  Entfernen:   bash install_macos.sh --remove"
