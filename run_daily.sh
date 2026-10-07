#!/bin/bash
# Der tägliche Lauf. Wird von launchd gestartet, funktioniert aber auch von Hand.
#
# Ablauf: analysieren, dann buchen. An Nicht-Rebalance-Tagen prüft der Bot nur
# auf Notfälle (Regime-Wechsel, Katastrophen-Stop, Datenqualität) und bucht sonst
# nichts — der Takt steckt in der Konfiguration, nicht hier.

set -uo pipefail
PROJEKT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJEKT"
mkdir -p state
LOG="state/cron.log"
PY="$(command -v python3 || echo /usr/bin/python3)"

zeit() { date "+%Y-%m-%d %H:%M:%S"; }

{
  echo "===== $(zeit) Lauf beginnt ====="
  "$PY" -m cryptobot.cli rebalance
  code=$?
  if [ "$code" -ne 0 ]; then
    echo "!!!!! $(zeit) Lauf fehlgeschlagen (Code $code)"
    # Kurze Notiz obenauf, damit ein Fehler beim Draufschauen sofort auffällt
    echo "$(zeit) FEHLER beim Lauf, Code $code" >> state/fehler.log
    exit $code
  fi
  echo "===== $(zeit) Lauf beendet ====="
  echo
} >> "$LOG" 2>&1

# Protokoll nicht ins Unendliche wachsen lassen
if [ "$(wc -c < "$LOG")" -gt 5000000 ]; then
  tail -c 2000000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
