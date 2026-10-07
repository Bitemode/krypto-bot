#!/bin/bash
# Zeigt Positionen, Kennzahlen und die letzten automatischen Läufe.
cd "$(dirname "$0")" || exit 1
clear
echo "================================================"
echo "   Aktueller Stand"
echo "================================================"
python3 -m cryptobot.cli positions
echo
python3 -m cryptobot.cli performance
echo
echo "------------------------------------------------"
echo "Läuft der tägliche Dienst?"
bash install_macos.sh --status 2>/dev/null | sed -n '1,6p'
echo
echo "(Fenster kann jetzt geschlossen werden)"
