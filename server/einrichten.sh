#!/bin/bash
# ---------------------------------------------------------------------------
# Richtet den Krypto-Bot auf dem Server ein. Laeuft als Benutzer "bot".
# Wird vom Doppelklick "4 Auf Server umziehen" auf dem Mac aufgerufen,
# Schritt fuer Schritt:
#
#   bash server/einrichten.sh pruefen          Pakete, Tests, Probelauf, Geheimnis-Pruefung.
#                                              Schaltet NICHTS ein, veraendert keinen Stand.
#   bash server/einrichten.sh github NUTZER    GitHub-Projekt NUTZER/krypto-bot verbinden,
#                                              ersten Stand hochladen.
#   bash server/einrichten.sh einschalten      Zeitplaene einschalten. Ab jetzt laeuft der
#                                              Bot auf dem Server.
#
# Rueckgabewert 0 = alles gut, alles andere = Fehler mit Erklaerung.
# ---------------------------------------------------------------------------

set -uo pipefail

BOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$BOT/.venv"
cd "$BOT" || exit 1
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

ok()     { echo "  ok      $*"; }
fehler() { echo "  FEHLER  $*" >&2; }
hinweis(){ echo "          $*"; }

# --- Python-Umgebung --------------------------------------------------------
umgebung() {
  if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV" || { fehler "Python-Umgebung liess sich nicht anlegen"; return 1; }
  fi
  "$VENV/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1
  if [ -f requirements.txt ]; then
    "$VENV/bin/python" -m pip install -q -r requirements.txt \
      || { fehler "Bibliotheken aus requirements.txt liessen sich nicht installieren"; return 1; }
  else
    "$VENV/bin/python" -m pip install -q pandas numpy pyyaml requests \
      || { fehler "Bibliotheken liessen sich nicht installieren"; return 1; }
  fi
  ok "Python-Umgebung: $("$VENV/bin/python" --version 2>&1)"
}

# --- Laeuft das, was der Mac gestartet hat, auch hier? ----------------------
uebertragbar() {
  local f gefunden=0 treffer
  for f in run_daily.sh run_buch.sh; do
    if [ ! -f "$f" ]; then fehler "$f fehlt"; gefunden=1; continue; fi
    bash -n "$f" || { fehler "$f hat einen Syntaxfehler"; gefunden=1; continue; }
    # Zeilen, die es nur auf einem Mac gibt (Kommentare ausgenommen)
    treffer="$(grep -nE '/Users/|/opt/homebrew|osascript|launchctl|caffeinate|pmset' "$f" \
               | grep -vE '^[0-9]+:[[:space:]]*#' || true)"
    if [ -n "$treffer" ]; then
      fehler "$f enthaelt Mac-spezifische Zeilen:"
      echo "$treffer" | sed 's/^/            /' >&2
      gefunden=1
    fi
  done
  [ "$gefunden" -eq 0 ] && ok "Startskripte ohne Mac-Besonderheiten"
  return "$gefunden"
}

# --- Nichts Geheimes darf in ein oeffentliches Projekt ----------------------
geheimnisse() {
  local liste dateien inhalt
  # Genau die Ordner auslassen, die auch .gitignore auslaesst — nicht jeden
  # Ordner, der zufaellig "cache" heisst.
  liste="$(mktemp)"
  find . \( -path ./.venv -o -path ./.git -o -path ./state/cache -o -path ./data_offline \
            -o -path ./data_voll -o -path ./kursdaten-voll \) -prune -o -type f -print0 > "$liste"
  dateien="$(tr '\0' '\n' < "$liste" \
              | grep -iE '(^|/)(\.env|[^/]*\.env|[^/]*\.pem|[^/]*\.key|id_rsa[^/]*|id_ed25519[^/]*)$' || true)"
  # -i: API_KEY = "..." genauso wie api_key = "..."
  inhalt="$(xargs -0 -r grep -nIiE \
              -e 'BEGIN [A-Z ]*PRIVATE KEY' \
              -e '(secret|api[_-]?key|token|password|passwort)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9/+_-]{24,}' \
              -- < "$liste" 2>/dev/null | cut -d: -f1,2 | sort -u || true)"
  rm -f "$liste"
  # Nur Datei und Zeilennummer ausgeben, NIE den Inhalt — sonst stuende ein
  # gefundener Schluessel im Klartext im Terminal.
  if [ -n "$dateien" ] || [ -n "$inhalt" ]; then
    fehler "Moegliche Geheimnisse gefunden — das Projekt wird oeffentlich, deshalb Abbruch:"
    [ -n "$dateien" ] && echo "$dateien" | sed 's/^/            Datei: /' >&2
    [ -n "$inhalt" ]  && echo "$inhalt"  | sed 's/^/            Zeile: /' >&2
    hinweis "Der Inhalt wird absichtlich nicht angezeigt."
    return 1
  fi
  ok "keine Schluessel oder Passwoerter im Projekt"
}

# --- Probelauf in einer Kopie: echter Lauf, echter Markt, kein echter Stand --
probelauf() {
  local probe rc vorher nachher stunde
  probe="$(mktemp -d /tmp/krypto-probe.XXXXXX)"
  if ! tar -C "$BOT" --exclude=./.venv --exclude=./.git -cf - . | tar -C "$probe" -xf -; then
    fehler "Kopie fuer den Probelauf liess sich nicht anlegen"
    rm -rf "$probe"
    return 1
  fi
  ln -s "$VENV" "$probe/.venv"

  # Tests und Selbsttest ebenfalls in der Kopie — was sie schreiben, soll
  # nicht im echten Stand und damit nicht auf GitHub landen.
  echo "          Tests …"
  if ! (cd "$probe" && PATH="$VENV/bin:/usr/local/bin:/usr/bin:/bin" timeout 900 python tests/test_bot.py) \
       > "$probe/tests.log" 2>&1; then
    fehler "Tests gescheitert:"
    tail -n 20 "$probe/tests.log" | sed 's/^/            /' >&2
    rm -rf "$probe"
    return 1
  fi
  ok "$(tail -n 1 "$probe/tests.log")"
  if ! (cd "$probe" && PATH="$VENV/bin:/usr/local/bin:/usr/bin:/bin" timeout 300 python -m cryptobot.cli doctor) \
       > "$probe/doctor.log" 2>&1; then
    fehler "Selbsttest (doctor) gescheitert — erreicht der Server Bitvavo?"
    tail -n 20 "$probe/doctor.log" | sed 's/^/            /' >&2
    rm -rf "$probe"
    return 1
  fi
  ok "Selbsttest: Datenquellen erreichbar"

  echo "          Probelauf des Handelslaufs (in einer Kopie, dauert ein paar Minuten) …"
  # Erfolg = Rueckgabe 0 UND das Protokoll ist gewachsen. Schreibt das Skript
  # an einen Mac-Pfad statt in die Kopie, waechst hier nichts — genau das soll
  # auffallen.
  vorher="$(wc -c < "$probe/state/cron.log" 2>/dev/null || echo 0)"
  (cd "$probe" && PATH="$VENV/bin:/usr/local/bin:/usr/bin:/bin" timeout 900 bash ./run_daily.sh) \
    > "$probe/probe-lauf.log" 2>&1
  rc=$?
  nachher="$(wc -c < "$probe/state/cron.log" 2>/dev/null || echo 0)"
  # run_daily.sh meldet einen Absturz mit Code 0 (bekannter Fehler im Skript).
  # Deshalb zaehlt nicht der Rueckgabewert allein, sondern was der Lauf ins
  # Protokoll geschrieben hat: "Lauf beendet" muss drinstehen, "fehlgeschlagen"
  # darf es nicht.
  local neu=""
  if [ "${nachher:-0}" -gt "${vorher:-0}" ]; then
    neu="$(tail -c +"$((vorher + 1))" "$probe/state/cron.log")"
  fi
  if [ "$rc" -ne 0 ] || [ -z "$neu" ] \
     || ! printf '%s' "$neu" | grep -q 'Lauf beendet' \
     || printf '%s' "$neu" | grep -q 'fehlgeschlagen'; then
    if [ -z "$neu" ]; then
      fehler "Probelauf beendet, aber nichts ins Protokoll state/cron.log geschrieben."
      hinweis "Vermutlich schreibt run_daily.sh an einen festen Ort auf dem Mac."
    else
      fehler "Probelauf des Handelslaufs gescheitert (Rueckgabe $rc). Letzte Zeilen:"
    fi
    tail -n 15 "$probe/probe-lauf.log" | sed 's/^/            /' >&2
    tail -n 15 "$probe/state/cron.log" 2>/dev/null | sed 's/^/            /' >&2
    rm -rf "$probe"
    return 1
  fi
  ok "Handelslauf laeuft hier (Probe, Stand nicht veraendert)"

  vorher="$(cat "$probe"/state/orderbuch/buch-*.csv 2>/dev/null | wc -l)"
  (cd "$probe" && PATH="$VENV/bin:/usr/local/bin:/usr/bin:/bin" timeout 240 bash ./run_buch.sh) \
    > "$probe/probe-buch.log" 2>&1
  rc=$?
  nachher="$(cat "$probe"/state/orderbuch/buch-*.csv 2>/dev/null | wc -l)"
  stunde="$(date +%-H)"
  if [ "$rc" -ne 0 ]; then
    fehler "Probelauf des Orderbuch-Messers gescheitert (Rueckgabe $rc):"
    tail -n 15 "$probe/probe-buch.log" | sed 's/^/            /' >&2
    rm -rf "$probe"
    return 1
  fi
  if [ "$stunde" -ge 8 ] && [ "$stunde" -lt 21 ] && [ "$nachher" -le "$vorher" ]; then
    fehler "Orderbuch-Messer lief, hat aber nichts geschrieben:"
    tail -n 15 "$probe/probe-buch.log" | sed 's/^/            /' >&2
    rm -rf "$probe"
    return 1
  fi
  ok "Orderbuch-Messer laeuft hier"
  rm -rf "$probe"
}

pruefen() {
  echo "Pruefung auf dem Server"
  umgebung      || return 1
  uebertragbar  || return 1
  geheimnisse   || return 1
  probelauf     || return 1
  echo "Pruefung bestanden."
}

# --- GitHub ------------------------------------------------------------------
github() {
  local nutzer="${1:-}" antwort n
  if ! [[ "$nutzer" =~ ^[A-Za-z0-9-]{1,39}$ ]]; then
    fehler "Ungueltiger GitHub-Benutzername: '$nutzer'"
    return 1
  fi
  echo "GitHub-Projekt $nutzer/krypto-bot verbinden"

  antwort="$(ssh -o BatchMode=yes -T git@github.com 2>&1 || true)"
  if ! echo "$antwort" | grep -q "successfully authenticated"; then
    fehler "GitHub kennt den Schluessel dieses Servers noch nicht."
    hinweis "Antwort von GitHub: $(echo "$antwort" | head -n 1)"
    return 1
  fi
  ok "GitHub erkennt den Server"

  # Immer die Regeln vom Server — eine alte .gitignore vom Mac koennte
  # Schluesseldateien oder die Python-Umgebung durchlassen.
  cp server/gitignore .gitignore
  [ -d .git ] || git init -q -b main
  git config user.name  "Krypto-Bot (Server)"
  git config user.email "krypto-bot@server.invalid"
  git remote remove origin 2>/dev/null || true
  git remote add origin "git@github.com:$nutzer/krypto-bot.git"
  git add -A
  git diff --cached --quiet || git commit -q -m "Umzug vom Mac auf den Server"
  echo "          Oeffentlich sichtbar werden diese Ordner und Dateien:"
  git ls-files | cut -d/ -f1 | sort -u | tr '\n' ' ' | fold -s -w 66 | sed 's/^/            /'
  echo

  if git push -q -u origin main 2>/tmp/krypto-push.log; then
    ok "Stand hochgeladen: https://github.com/$nutzer/krypto-bot"
    return 0
  fi
  if grep -qiE 'not found|does not exist' /tmp/krypto-push.log; then
    fehler "Das Projekt $nutzer/krypto-bot gibt es auf GitHub nicht — Name richtig geschrieben?"
    return 1
  fi
  if grep -qiE 'denied|read.only|write access|forbidden' /tmp/krypto-push.log; then
    fehler "GitHub erlaubt dem Server nur Lesen."
    hinweis "Den Schluessel bei GitHub loeschen und neu eintragen, diesmal mit Haken bei"
    hinweis "\"Allow write access\"."
    return 1
  fi
  if grep -qiE 'rejected|fetch first|non-fast-forward' /tmp/krypto-push.log; then
    # Ein frisch angelegtes Projekt hat manchmal schon eine README. Die darf
    # weg. Hat es mehr Geschichte, ist es nicht das neue Projekt — dann Abbruch.
    git fetch -q origin main 2>/dev/null
    n="$(git rev-list --count origin/main 2>/dev/null || echo 999)"
    if [ "$n" -le 3 ]; then
      if git push -q -u --force origin main 2>>/tmp/krypto-push.log; then
        ok "Stand hochgeladen (die Start-Datei des neuen Projekts wurde ersetzt)"
        return 0
      fi
    else
      fehler "Das Projekt $nutzer/krypto-bot hat schon $n Eintraege — das ist nicht das neue,"
      hinweis "leere Projekt. Zur Sicherheit wird nichts ueberschrieben."
      return 1
    fi
  fi
  fehler "Hochladen gescheitert:"
  sed 's/^/            /' /tmp/krypto-push.log >&2
  return 1
}

# --- Zeitplaene ---------------------------------------------------------------
einschalten() {
  local d="$HOME/.config/systemd/user" zeiten von bis z kalender=""
  echo "Zeitplaene einschalten"
  mkdir -p "$d"

  # Laufzeiten und Messfenster aus config.yaml, wie auf dem Mac
  read -r zeiten von bis < <("$VENV/bin/python" - <<'PY'
import yaml
c = yaml.safe_load(open("config.yaml")) or {}
z = (c.get("schedule") or {}).get("run_times_local") or ["09:30", "17:00"]
o = c.get("orderbuch") or {}

def hhmm(x):
    # YAML liest 17:00 ohne Anfuehrungszeichen als Zahl 1020 (Minuten)
    if isinstance(x, int):
        h, m = divmod(x, 60)
        return f"{h:02d}:{m:02d}"
    return str(x).strip()

print(",".join(hhmm(x) for x in z), int(o.get("von_stunde", 8)), int(o.get("bis_stunde", 21)))
PY
)
  if [ -z "${zeiten:-}" ]; then zeiten="09:30,17:00"; von=8; bis=21; fi
  for z in ${zeiten//,/ }; do
    if ! [[ "$z" =~ ^[0-9]{1,2}:[0-9]{2}$ ]]; then fehler "Laufzeit '$z' in config.yaml unlesbar"; return 1; fi
    kalender+="OnCalendar=*-*-* ${z}:00 Europe/Berlin"$'\n'
  done

  cat > "$d/krypto-lauf.service" <<EOF
[Unit]
Description=Krypto-Bot Handelslauf (Papier)

[Service]
Type=oneshot
WorkingDirectory=$BOT
Environment=PATH=$VENV/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/bash $BOT/run_daily.sh
ExecStopPost=-/bin/bash $BOT/server/sync.sh
TimeoutStartSec=30min
# Der Abgleich danach zaehlt zur Stopp-Phase — ohne das haette er nur 90 Sekunden
TimeoutStopSec=10min
EOF

  cat > "$d/krypto-lauf.timer" <<EOF
[Unit]
Description=Krypto-Bot Handelslauf zu festen Zeiten

[Timer]
${kalender}Persistent=true

[Install]
WantedBy=timers.target
EOF

  cat > "$d/krypto-buch.service" <<EOF
[Unit]
Description=Krypto-Bot Orderbuch-Messer (misst nur, handelt nicht)

[Service]
Type=oneshot
WorkingDirectory=$BOT
Environment=PATH=$VENV/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/bash $BOT/run_buch.sh
TimeoutStartSec=4min
EOF

  cat > "$d/krypto-buch.timer" <<EOF
[Unit]
Description=Krypto-Bot Orderbuch alle 5 Minuten

[Timer]
OnCalendar=*-*-* $(printf '%02d' "$von")..$(printf '%02d' $((bis - 1))):00/5:00 Europe/Berlin
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

  cat > "$d/krypto-sync.service" <<EOF
[Unit]
Description=Krypto-Bot Stand nach GitHub

[Service]
Type=oneshot
WorkingDirectory=$BOT
ExecStart=/bin/bash $BOT/server/sync.sh
TimeoutStartSec=10min
EOF

  cat > "$d/krypto-sync.timer" <<EOF
[Unit]
Description=Krypto-Bot Stand stuendlich nach GitHub

[Timer]
OnCalendar=hourly
RandomizedDelaySec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

  systemctl --user daemon-reload || { fehler "Zeitplaene liessen sich nicht laden"; return 1; }
  systemctl --user enable --now krypto-lauf.timer krypto-buch.timer krypto-sync.timer \
    >/dev/null 2>&1 || { fehler "Zeitplaene liessen sich nicht einschalten"; return 1; }
  date -Is > "$BOT/.umgezogen"
  ok "Handelslauf: ${zeiten//,/ und } Uhr (deutsche Zeit)"
  ok "Orderbuch:   alle 5 Minuten, $(printf '%02d' "$von"):00 bis $(printf '%02d' "$bis"):00"
  ok "GitHub:      nach jedem Handelslauf und stuendlich"
  echo
  systemctl --user list-timers --no-pager 'krypto-*' 2>/dev/null | sed 's/^/          /'
}

case "${1:-}" in
  pruefen)     pruefen ;;
  github)      github "${2:-}" ;;
  einschalten) einschalten ;;
  *) echo "Aufruf: bash server/einrichten.sh pruefen | github NUTZER | einschalten" >&2; exit 2 ;;
esac
