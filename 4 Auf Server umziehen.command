#!/bin/bash
# ---------------------------------------------------------------------------
# Doppelklick genügt. Zieht den Krypto-Bot vom Mac auf deinen Server um.
#
# Du wirst Schritt für Schritt geführt. Tippen musst du nur zwei Dinge:
# die Adresse des Servers und deinen GitHub-Benutzernamen.
#
# Sicherheitsnetz: Geht unterwegs etwas schief, läuft der Bot automatisch
# wieder auf dem Mac weiter. Es geht kein Stand verloren.
# ---------------------------------------------------------------------------

cd "$(dirname "$0")" || exit 1
PROJEKT="$(pwd)"
SCHLUESSEL="$HOME/.ssh/krypto_server"
BEKANNTE="$HOME/.ssh/krypto_known_hosts"

warte()   { read -r -p "$1" _; }
abbruch() {
  echo
  echo "------------------------------------------------"
  echo "ABBRUCH: $*"
  echo "------------------------------------------------"
  echo
  warte "Mit der Eingabetaste schließen …"
  exit 1
}
trenner() { echo; echo "================================================"; echo "   $*"; echo "================================================"; echo; }

clear
trenner "Krypto-Bot: Umzug auf den Server"

# --- Schon umgezogen? ---------------------------------------------------------
if [ -f server.txt ] && grep -q '^umgezogen=' server.txt; then
  abbruch "Der Bot ist schon umgezogen ($(grep '^umgezogen=' server.txt | cut -d= -f2)).
Ein zweiter Umzug würde den neueren Stand auf dem Server mit dem alten
Stand vom Mac überschreiben. Deshalb passiert hier nichts."
fi

for f in server-einrichtung.yaml server/einrichten.sh server/sync.sh server/gitignore install_macos.sh; do
  [ -f "$f" ] || abbruch "Die Datei '$f' fehlt im Ordner krypto-bot."
done

# --- Schritt 1: Schlüssel und Server ----------------------------------------
trenner "Schritt 1 von 4: Server bei Hetzner anlegen"

if [ ! -f "$SCHLUESSEL" ]; then
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  ssh-keygen -q -t ed25519 -N "" -C "krypto-bot-mac" -f "$SCHLUESSEL" \
    || abbruch "Der Anmeldeschlüssel ließ sich nicht erzeugen."
fi
pbcopy < "$SCHLUESSEL.pub"

echo "Dein Anmeldeschlüssel für den Server liegt jetzt in der Zwischenablage."
echo "Er ist öffentlich — das ist der Teil, den der Server kennen darf."
echo
echo "Gleich öffnet sich Hetzner im Browser. Dort:"
echo
echo "  1. Neues Projekt anlegen (Name egal), dann \"Server hinzufügen\""
echo "  2. Standort:  Nürnberg oder Falkenstein"
echo "  3. Image:     Ubuntu 24.04"
echo "  4. Typ:       der kleinste (\"Cost-Optimized\", 2 vCPU)"
echo "  5. Netzwerk:  öffentliche IPv4 eingeschaltet lassen"
echo "  6. SSH-Keys:  \"SSH-Key hinzufügen\", mit ⌘V einfügen, Name: Mac"
echo "  7. Cloud config: Es öffnet sich auch ein TextEdit-Fenster mit der"
echo "     Einrichtung. Dort ⌘A und ⌘C, dann im Feld bei Hetzner ⌘V."
echo "     (Erst Schritt 6 erledigen — ⌘C ersetzt die Zwischenablage.)"
echo "  8. Name:      krypto-bot  →  \"Kaufen & Erstellen\""
echo
echo "Danach steht bei dem Server eine IPv4-Adresse (vier Zahlen mit Punkten)."
echo
warte "Eingabetaste, um Hetzner zu öffnen …"
open "https://console.hetzner.cloud/" 2>/dev/null
open -e "$PROJEKT/server-einrichtung.yaml" 2>/dev/null
echo

IP=""
for versuch in 1 2 3; do
  read -r -p "IPv4-Adresse des Servers hier einfügen, dann Eingabetaste: " IP
  IP="$(echo "$IP" | tr -d '[:space:]')"
  if [[ "$IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then break; fi
  echo "  Das sieht nicht wie eine IPv4-Adresse aus (Beispiel: 116.203.45.12)."
  IP=""
done
[ -n "$IP" ] || abbruch "Keine gültige Adresse eingegeben."
# Wurde der Server neu angelegt und hat dieselbe Adresse, waere der alte
# Erkennungsschluessel im Weg. Also vergessen; der neue wird beim ersten
# Kontakt uebernommen.
ssh-keygen -R "$IP" -f "$BEKANNTE" >/dev/null 2>&1 || true

ZIEL="bot@$IP"
SSH_OPT=(-i "$SCHLUESSEL" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new
         -o UserKnownHostsFile="$BEKANNTE" -o ConnectTimeout=10 -o BatchMode=yes
         -o ServerAliveInterval=30)
server() { ssh "${SSH_OPT[@]}" "$ZIEL" "$@"; }

# --- Schritt 2: warten, bis der Server sich eingerichtet hat ------------------
trenner "Schritt 2 von 4: Server richtet sich ein"
echo "Das dauert nach dem Anlegen etwa 5 bis 10 Minuten. Einfach warten."
echo
bereit=""
for i in $(seq 1 90); do
  if server 'test -f /var/lib/krypto-einrichtung.ok' 2>/dev/null; then bereit=1; break; fi
  printf "."
  sleep 10
done
echo
[ -n "$bereit" ] || abbruch "Der Server ist nach 15 Minuten nicht bereit.
Häufigste Ursachen: Bei Hetzner wurde der SSH-Key nicht ausgewählt, oder
das Feld Cloud config ist leer. Am Mac ist nichts verändert worden."
echo "  ok      Server ist eingerichtet"

# --- Schritt 3: GitHub ------------------------------------------------------
trenner "Schritt 3 von 4: GitHub verbinden"
echo "Du brauchst ein GitHub-Konto und darin ein NEUES, LEERES, ÖFFENTLICHES"
echo "Projekt (\"Repository\") mit dem Namen:  krypto-bot"
echo "Falls noch nicht geschehen: jetzt auf github.com anlegen. Kein Haken bei"
echo "\"Add a README\" nötig."
echo

NUTZER=""
for versuch in 1 2 3; do
  read -r -p "Dein GitHub-Benutzername: " NUTZER
  NUTZER="$(echo "$NUTZER" | tr -d '[:space:]@')"
  if [[ "$NUTZER" =~ ^[A-Za-z0-9-]{1,39}$ ]]; then break; fi
  echo "  Ein GitHub-Benutzername besteht nur aus Buchstaben, Ziffern und Bindestrichen."
  NUTZER=""
done
[ -n "$NUTZER" ] || abbruch "Kein gültiger Benutzername eingegeben."

SERVERSCHLUESSEL="$(server 'cat ~/.ssh/github.pub' 2>/dev/null)"
case "$SERVERSCHLUESSEL" in
  ssh-ed25519\ *) printf '%s\n' "$SERVERSCHLUESSEL" | pbcopy ;;
  *) abbruch "Der Schlüssel des Servers ließ sich nicht abholen." ;;
esac
echo
echo "Der Schlüssel des SERVERS liegt jetzt in der Zwischenablage. Er gilt"
echo "später nur für dieses eine Projekt, nicht für dein ganzes Konto."
echo
echo "Gleich öffnet sich GitHub. Dort:"
echo "  1. Title:  Server"
echo "  2. Key:    mit ⌘V einfügen"
echo "  3. Haken bei  \"Allow write access\"   ← wichtig"
echo "  4. \"Add key\""
echo
warte "Eingabetaste, um GitHub zu öffnen …"
open "https://github.com/$NUTZER/krypto-bot/settings/keys/new" 2>/dev/null
echo

verbunden=""
for versuch in 1 2 3 4 5; do
  warte "Wenn der Schlüssel eingetragen ist: Eingabetaste …"
  antwort="$(server 'ssh -o BatchMode=yes -T git@github.com 2>&1' 2>/dev/null)"
  if echo "$antwort" | grep -qi "hi $NUTZER/krypto-bot.*successfully authenticated"; then
    verbunden=1; break
  fi
  if echo "$antwort" | grep -qi "successfully authenticated"; then
    echo "  GitHub kennt den Schlüssel, aber für ein anderes Projekt:"
    echo "  $(echo "$antwort" | head -n 1)"
    echo "  Er muss im Projekt  $NUTZER/krypto-bot  eingetragen sein."
  else
    echo "  GitHub kennt den Schlüssel noch nicht. Eingetragen und gespeichert?"
  fi
done
[ -n "$verbunden" ] || abbruch "GitHub hat den Server nicht erkannt. Am Mac ist nichts verändert worden.
Du kannst diesen Doppelklick einfach noch einmal starten."
echo "  ok      GitHub erkennt den Server"

# --- Schritt 4: Umzug ---------------------------------------------------------
trenner "Schritt 4 von 4: Umzug"

if server 'test -f /opt/krypto-bot/.umgezogen' 2>/dev/null; then
  abbruch "Auf dem Server läuft der Bot schon. Ein zweiter Umzug würde den
neueren Stand dort mit dem alten vom Mac überschreiben."
fi

# Den Mac waehrend des Umzugs wach halten — die Pruefung kann 20 Minuten dauern
caffeinate -i -w $$ >/dev/null 2>&1 &

MAC_AUS=""         # Zeitplaene auf dem Mac entfernt?
SERVER_EVTL_AN=""  # koennte der Server schon handeln?
FERTIG=""
HINWEISDATEI="$PROJEKT/UMZUG UNTERBROCHEN - bitte Claude fragen.txt"

# Erfolg (0) NUR, wenn bestaetigt ist, dass auf dem Server kein Handelslauf
# mehr eingeplant ist. Ist der Server nicht erreichbar, ist das NICHT bestaetigt.
server_sicher_aus() {
  server 'export XDG_RUNTIME_DIR=/run/user/$(id -u)
          systemctl --user disable --now krypto-lauf.timer krypto-buch.timer krypto-sync.timer >/dev/null 2>&1
          rm -f /opt/krypto-bot/.umgezogen
          ! systemctl --user is-enabled --quiet krypto-lauf.timer 2>/dev/null' 2>/dev/null
}

zurueck_auf_den_mac() {
  [ -n "$MAC_AUS" ] || return 0
  echo
  if [ -n "$SERVER_EVTL_AN" ]; then
    if ! server_sicher_aus; then
      # Lieber kurz gar kein Bot als zwei, die sich den Stand aufspalten.
      echo "  ACHTUNG  Der Server antwortet gerade nicht. Ob der Bot dort schon"
      echo "           läuft, lässt sich nicht prüfen. Damit nicht zwei Bots"
      echo "           gleichzeitig handeln, bleibt der Mac AUS."
      echo "           Bitte Claude Bescheid geben."
      {
        echo "Der Umzug auf den Server wurde am $(date '+%d.%m.%Y um %H:%M') unterbrochen."
        echo
        echo "Der Bot auf dem Mac ist AUS. Ob er auf dem Server ($IP) schon läuft,"
        echo "war nicht prüfbar. Bitte NICHT selbst \"1 Bot einrichten\" klicken,"
        echo "sondern Claude fragen — sonst handeln womöglich zwei Bots."
      } > "$HINWEISDATEI"
      return 1
    fi
  else
    server_sicher_aus >/dev/null 2>&1 || true
  fi
  echo "Der Bot wird wieder auf dem Mac eingeschaltet …"
  if bash install_macos.sh >/tmp/krypto-zurueck.log 2>&1; then
    MAC_AUS=""
    echo "  ok      Der Bot läuft weiter auf dem Mac, wie vorher."
  else
    echo "  ACHTUNG  Bitte \"1 Bot einrichten\" doppelklicken, damit er wieder läuft."
  fi
}

# Fenster geschlossen oder abgebrochen: zuruecksetzen, statt den Bot
# nirgends laufen zu lassen.
notfall() {
  trap - HUP INT TERM
  [ -n "$FERTIG" ] || zurueck_auf_den_mac >/dev/null 2>&1
  exit 1
}
trap notfall HUP INT TERM

echo "Der Bot auf dem Mac wird angehalten, damit nicht zwei Bots gleichzeitig"
echo "handeln …"
MAC_AUS=1
bash install_macos.sh --remove >/dev/null 2>&1
if launchctl list 2>/dev/null | grep -q 'de\.local\.cryptobot'; then
  zurueck_auf_den_mac
  abbruch "Der Bot auf dem Mac ließ sich nicht anhalten. Es wurde nichts umgezogen."
fi
# Ein gerade laufender Lauf darf zu Ende schreiben, bevor kopiert wird
for i in $(seq 1 120); do
  pgrep -f 'run_daily\.sh|run_buch\.sh|cryptobot\.cli' >/dev/null 2>&1 || break
  [ "$i" -eq 1 ] && echo "  Auf dem Mac läuft gerade ein Lauf, ich warte, bis er fertig ist …"
  sleep 5
done
if pgrep -f 'run_daily\.sh|run_buch\.sh|cryptobot\.cli' >/dev/null 2>&1; then
  zurueck_auf_den_mac
  abbruch "Ein Lauf auf dem Mac wird nach 10 Minuten nicht fertig. Es wurde nichts umgezogen."
fi
echo "  ok      Mac angehalten"

echo "Code und bisheriger Stand werden hochgeladen …"
if ! rsync -a --delete \
      --exclude '.venv' --exclude '__pycache__' --exclude '.git' --exclude '.DS_Store' \
      --exclude '*.bak' --exclude 'data_offline' --exclude 'data_voll' \
      --exclude 'kursdaten-voll' --exclude 'kursdaten-voll.tar.gz' --exclude 'bitvavo_voll.txt' \
      --exclude 'server.txt' \
      --exclude 'launchd.out.log' --exclude 'launchd.err.log' \
      --exclude 'buch.out.log' --exclude 'buch.err.log' \
      -e "ssh ${SSH_OPT[*]}" \
      ./ "$ZIEL:/opt/krypto-bot/"; then
  zurueck_auf_den_mac
  abbruch "Hochladen gescheitert."
fi
echo "  ok      hochgeladen"
echo

if ! server 'bash /opt/krypto-bot/server/einrichten.sh pruefen'; then
  zurueck_auf_den_mac
  abbruch "Die Prüfung auf dem Server ist gescheitert (Meldungen oben).
Der Stand ist nicht verloren. Schick Claude den Text von oben."
fi
echo

if ! server "bash /opt/krypto-bot/server/einrichten.sh github $NUTZER"; then
  zurueck_auf_den_mac
  abbruch "Der Stand ließ sich nicht auf GitHub ablegen (Meldungen oben).
Nach dem Beheben diesen Doppelklick einfach noch einmal starten."
fi
echo

# Ab hier koennte der Server handeln — ein Ruecksprung braucht dann eine
# Bestaetigung vom Server, sonst bleibt der Mac aus.
SERVER_EVTL_AN=1
if ! server 'bash /opt/krypto-bot/server/einrichten.sh einschalten'; then
  zurueck_auf_den_mac
  abbruch "Die Zeitpläne auf dem Server ließen sich nicht einschalten.
Schick Claude den Text von oben."
fi
FERTIG=1
trap - HUP INT TERM
rm -f "$HINWEISDATEI"

{
  echo "ip=$IP"
  echo "github=$NUTZER"
  echo "umgezogen=$(date '+%Y-%m-%d %H:%M')"
} > server.txt

trenner "Fertig. Der Bot läuft jetzt auf dem Server."
echo "Dein Mac darf ab jetzt zu bleiben — der Bot braucht ihn nicht mehr."
echo
echo "Stand und Ergebnisse, jederzeit, auch vom Handy:"
echo "    https://github.com/$NUTZER/krypto-bot"
echo
echo "Sag Claude Bescheid, dass der Umzug fertig ist. Dann wird der"
echo "Montagsbericht auf GitHub umgestellt."
echo
echo "\"2 Bot jetzt laufen lassen\" und \"3 Stand ansehen\" auf dem Mac gelten ab jetzt"
echo "nicht mehr — sie zeigen den alten Stand vom Mac."
echo
open "https://github.com/$NUTZER/krypto-bot" 2>/dev/null
warte "Mit der Eingabetaste schließen …"
