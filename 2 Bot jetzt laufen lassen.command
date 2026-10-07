#!/bin/bash
# Führt einen Lauf sofort aus, ohne auf 10:00 Uhr zu warten.
cd "$(dirname "$0")" || exit 1
clear
echo "================================================"
echo "   Bot läuft …"
echo "================================================"
echo
python3 -m cryptobot.cli rebalance
echo
echo "================================================"
echo "Der Lauf wurde in state/cron.log protokolliert."
echo
read -r -p "Mit der Eingabetaste schließen …" _
