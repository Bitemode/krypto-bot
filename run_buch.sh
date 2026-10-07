#!/bin/bash
# Orderbuch-Schnappschuss. Wird von launchd alle 5 Minuten gestartet.
#
# Handelt nicht. Braucht keinen API-Schlüssel. Ändert am Bot nichts.
# Ausserhalb des Messfensters beendet er sich sofort und still — ein Laptop
# soll nachts nicht alle fünf Minuten aufwachen, um nichts zu tun.

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 0

VON=$(python3 -c "import yaml;print(yaml.safe_load(open('config.yaml')).get('orderbuch',{}).get('von_stunde',8))" 2>/dev/null || echo 8)
BIS=$(python3 -c "import yaml;print(yaml.safe_load(open('config.yaml')).get('orderbuch',{}).get('bis_stunde',21))" 2>/dev/null || echo 21)
H=$(date +%-H)

if [ "$H" -lt "$VON" ] || [ "$H" -ge "$BIS" ]; then
  exit 0
fi

mkdir -p state
python3 -m cryptobot.cli buch >> state/buch.log 2>&1

# Solana-Messung im selben Takt. Getrennter Aufruf: faellt Jupiter aus, laeuft
# die Boersenmessung trotzdem weiter.
JUP=$(python3 -c "import yaml;print(yaml.safe_load(open('config.yaml')).get('jupiter',{}).get('aktiv',False))" 2>/dev/null || echo False)
if [ "$JUP" = "True" ]; then
  python3 -m cryptobot.cli jupiter >> state/buch.log 2>&1
fi

# Protokoll kurz halten: die letzten 2000 Zeilen reichen für eine Fehlersuche.
if [ -f state/buch.log ] && [ "$(wc -l < state/buch.log)" -gt 3000 ]; then
  tail -n 2000 state/buch.log > state/buch.log.tmp && mv state/buch.log.tmp state/buch.log
fi
