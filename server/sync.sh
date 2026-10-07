#!/bin/bash
# ---------------------------------------------------------------------------
# Legt den aktuellen Stand des Bots auf GitHub ab. Laeuft nach jedem
# Handelslauf und stuendlich. Schreibt vorher eine Statusdatei, damit man auf
# GitHub auch sieht, OB der Server laeuft — nicht nur, was er zuletzt tat.
# ---------------------------------------------------------------------------

set -uo pipefail
BOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BOT" || exit 1
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# Nie zwei Abgleiche gleichzeitig
exec 9>/tmp/krypto-sync.lock
flock -n 9 || exit 0

[ -d .git ] || exit 0

# Ein frueher abgebrochener Abgleich kann eine Sperrdatei hinterlassen, die
# sonst jeden weiteren blockiert. Nach 15 Minuten gilt sie als verwaist.
find .git -maxdepth 1 -name index.lock -mmin +15 -delete 2>/dev/null

mkdir -p state
{
  echo "Server-Stand: $(date '+%Y-%m-%d %H:%M %Z')"
  echo "Laeuft seit:  $(uptime -s 2>/dev/null)"
  echo
  echo "--- Zeitplaene ---"
  systemctl --user list-timers --all --no-pager 'krypto-*' 2>&1
  echo
  for u in krypto-lauf krypto-buch; do
    echo "--- letzte Meldungen: $u ---"
    journalctl --user -u "$u" -n 12 --no-pager -o short-iso 2>&1
    echo
  done
  echo "--- Speicher ---"
  df -h / | tail -n 1
} > state/server-status.txt 2>&1

git add -A

# Zweiter Zaun hinter .gitignore: nichts, was nach Schluessel aussieht, geht raus
if git diff --cached --name-only | grep -qE '(^|/)(\.env|[^/]*\.env|[^/]*\.pem|[^/]*\.key|id_rsa[^/]*|id_ed25519[^/]*)$' \
   || git diff --cached -U0 | grep -qE '^\+.*BEGIN [A-Z ]*PRIVATE KEY'; then
  git reset -q
  echo "$(date -Is) ABGEBROCHEN: moeglicher Schluessel im Stand, nichts hochgeladen" >> state/sync.err.log
  # Nicht stumm bleiben: nur die Statusdatei mit einer Warnung hochladen, damit
  # auf GitHub sichtbar ist, dass der Abgleich gesperrt ist.
  {
    echo
    echo "ACHTUNG: Abgleich gesperrt seit $(date '+%Y-%m-%d %H:%M') —"
    echo "im Stand liegt etwas, das wie ein Schluessel aussieht. Es wird nichts"
    echo "hochgeladen, bis das geklaert ist."
  } >> state/server-status.txt
  git add state/server-status.txt
  git commit -q -m "Warnung: Abgleich gesperrt" && git push -q origin main 2>>state/sync.err.log
  exit 1
fi

git diff --cached --quiet && exit 0
git commit -q -m "Stand $(date '+%Y-%m-%d %H:%M')"
if ! git push -q origin main 2>>state/sync.err.log; then
  echo "$(date -Is) Hochladen gescheitert, naechster Versuch beim naechsten Abgleich" >> state/sync.err.log
  exit 1
fi
