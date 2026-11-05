# Änderungen an Catalytix

Die aktuelle Version steht nach der Anmeldung unten links in der Seitenleiste. English version:
[CHANGELOG.en.md](CHANGELOG.en.md).

## 3.0.3 – 05.11.2026
**Verbessert (Update-Weg und Build-Kette)**
- Jedes Image trägt seinen genauen Tag (`catalytix.tag`, z. B. `3.0.3-s1`) und den Commit, aus dem es gebaut wurde
  (`org.opencontainers.image.revision`; bei einem Sicherheitsupdate der Commit des zugrunde liegenden Releases). Das
  wöchentliche Sicherheitsupdate legt den Tag dafür schon vor dem Bau fest.
- update.sh geht nach einem Fehlstart auf genau den Tag des vorher laufenden Images zurück – auch wenn es ein
  Sicherheitsupdate war, das nur als `latest` geholt wurde (bisher wurde daraus die Version ohne die Fixes). Bei
  älteren Images ohne diese Angabe weist update.sh darauf hin.
- Das GitHub-Release zeigt immer auf den Commit, aus dem das veröffentlichte Image gebaut wurde, und enthält
  `update.sh`, `docker-compose.yml` und `env.example` aus dem Image selbst – auch wenn das Release erst in einem
  späteren Lauf entsteht.
- Build und Sicherheitsupdate begrenzen gleichzeitige Läufe jetzt je Workflow statt je Job (eine gemeinsame
  Job-Gruppe ließ GitHub wartende Jobs abbrechen, z. B. das Release nach dem Veröffentlichen des Images). `latest`
  wird nie auf eine niedrigere Version verschoben – geprüft gegen die Releases und gegen das Image, auf das `latest`
  gerade zeigt.
- Die 7-Tage-Wartezeit gilt jetzt für jede neue oder geänderte Paketversion – auch für Pakete, die als neue
  Abhängigkeit hinzukommen, und für frisch hochgeladene ältere Versionen. Ist eine gewählte Version zu jung, nimmt
  das Sicherheitsupdate die nächstältere statt ganz abzubrechen.

## 3.0.2 – 05.11.2026
**Sicherheit**
- Bestätigungslinks für eine neue E-Mail-Adresse (selbst registrierte Konten) gelten nur noch, solange das Konto
  die Adresse hat, von der aus die Änderung angefordert wurde: Nach einer bestätigten Änderung sind alle älteren
  Links ungültig, und kein Link funktioniert zweimal (vor dem Update verschickte Links müssen neu angefordert
  werden). Bisher konnte z. B. ein Link an eine vertippte Adresse noch
  48 Stunden lang das Konto auf diese Adresse umstellen. Die erlaubten Domains werden beim Bestätigen erneut geprüft.
- Impressum- und Datenschutz-Link: Angaben wie `//andere-seite.example` (oder mit Leer-/Steuerzeichen) werden nicht mehr gespeichert. In E-Mails
  stehen relative Pfade (`/datenschutz`) jetzt als vollständige Adresse auf Basis der eingestellten Programmadresse.
- Gruppengebundene Verwalter bekommen für Bücher anderer Gruppen, die sie nicht sehen dürfen, „nicht gefunden“
  (404) statt „verboten“ (403) – so lässt sich nicht mehr erkennen, welche Buchnummern es dort gibt.
- Coverbilder werden nur noch als „private“ zwischengespeichert, nicht mehr in gemeinsam genutzten Proxys.

**Behoben**
- Update: Schlug ein Versionswechsel fehl und lief danach wieder die alte Version, machte der nächste Update-Versuch
  keine neue Sicherung vor der Umstellung (es galt noch die alte vom ersten Versuch). Ab 3.0.2 setzt jeder
  erfolgreiche Start zurück, dass die Sicherung schon gemacht ist (wirkt also, wenn 3.0.2 oder neuer die Version ist,
  zu der man zurückkehrt).
- Schnellerfassung: Wird dieselbe ISBN zweimal gescannt (z. B. im Automatik-Modus kurz hintereinander), zählt der
  Server die Exemplare beim vorhandenen Buch derselben Gruppe dazu, statt ein zweites Buch anzulegen.
- Benutzer löschen: Hat das Konto in diesem Moment doch noch eine laufende Ausleihe, wird es nicht gelöscht
  (bisher hätte die Ausleihe mit gelöscht werden können).
- update.sh:
  - prüft vor jeder Änderung, ob `.env` beschreibbar ist – bisher konnte ein Downgrade bei schreibgeschützter
    `.env` mit gestopptem Container und schon zurückgesetzter Datenbank abbrechen;
  - liest und schreibt `.env` wie Docker Compose (`export CATALYTIX_TAG=…`, Kommentare hinter dem Wert);
  - bricht ohne Änderung ab, wenn `CATALYTIX_TAG` oder `CATALYTIX_IMAGE` in der Shell gesetzt ist (Compose würde
    sonst ein anderes Image starten), und meldet keinen Erfolg, wenn trotzdem ein anderes Image läuft als das geholte;
  - entfernt „verwaiste“ Container (`--remove-orphans`) nur noch im Compose-Projekt dieses Ordners – nicht mehr in
    einem fremden Stack, dessen Namen der Container trägt;
  - entfernt den kurzzeitig angelegten Hilfscontainer samt seinem anonymen Volume;
  - `--cleanup` bricht ab, wenn im Datenordner ein Dateiname mit Zeilenumbruch liegt (sonst wäre ein Link mit so
    einem Namen nicht als geschützt erkannt worden).
- Build: Die Paketliste im Image bricht den Bau bei einem Fehler ab, statt unvollständig zu bleiben;
  `highest_release` wertet eine unlesbare Release-Liste nicht mehr als „noch keine Version“;
  `tools/security_update.py changelog` schreibt nur, wenn beide CHANGELOGs vorhanden sind.
- Zwei-Faktor-Anmeldung: Authenticator-App und Passkey zeigen bei unverändertem Bibliotheksnamen den Namen in der
  Sprache der Oberfläche („Library“ statt „Bibliothek“ auf Englisch).

**Oberfläche und Barrierefreiheit**
- Handy: In der Ausleihliste brechen Namen nicht mehr Buchstabe für Buchstabe um.
- Die zugeklappte Seitenleiste auf dem Handy ist mit der Tab-Taste nicht mehr erreichbar; der Menüknopf meldet,
  ob sie offen ist.
- „+ Buch“ (oben auf dem Handy) und die Kamera-Knöpfe an der Ausleihtheke haben einen Namen für Screenreader.
- Mehr Kontrast für graue Hinweistexte, Tabellenköpfe und Messing-Schrift (WCAG AA).

**Qualität**
- Typprüfung: `dbversion.py`, `composefile.py` und `lockout.py` werden jetzt streng geprüft. Übrige Funde: 58.
- Doku: `.env.example` nennt `CATALYTIX_COVER_VIA_PROXY`; README (englisch) sagt richtig, dass die Oberfläche
  zweisprachig ist.

## 3.0.1-s1 – 02.11.2026 (Sicherheitsupdate)
Gleiches Programm wie 3.0.1, nur aktualisierte Pakete (wöchentliches Sicherheitsupdate, automatisch gebaut, getestet und mit Trivy geprüft):
- Basis-Image: `python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 → python:3.13.18-slim-trixie@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1`
- Python: `3.13.17 → 3.13.18`
- Debian-Pakete: `tzdata 2026e-0+deb13u1 → 2026f-0+deb13u1`
- Python-Pakete: `certifi 2026.10.19 → 2026.10.23`, `pycparser 3.11 → 3.12`, `sqlalchemy 2.1.5 → 2.1.6`, `wtforms 3.2.2 → 3.3.1`

## 3.0.1 – 01.11.2026
**Behoben**
- Active Directory: Lehnte der Domänencontroller die Anmeldung des Dienstkontos ab (z. B. abgelaufenes Kennwort),
  brach die Anmeldung mit einem internen Fehler ab, statt „Bind abgelehnt (…)“ mit dem Grund zu zeigen. Ursache:
  Platzhalter wie `{msg}` in übersetzten Texten kollidierten mit den Parameternamen der Übersetzungsfunktionen –
  die Parameter sind jetzt nur noch positionsgebunden, solche Platzhalter funktionieren überall.

**Sicherheit**
- pyasn1 0.6.4 → 0.6.5 (vier Sicherheitslücken der Stufe HIGH, von Trivy gemeldet; wird für die
  AD-Anmeldung gebraucht). Bewusst vor Ablauf der 7-Tage-Wartezeit übernommen.

**Qualität**
- Gefunden durch die Typprüfung, ohne Auswirkung im Betrieb: Die Suche nach einer passenden Sicherung
  (`find_for_version`) prüft eine ungültige Versionsnummer jetzt selbst (bisher nur der Befehl davor),
  `tools/lock.py` und `tools/security_update.py` melden unlesbare Einträge mit klarer Meldung statt Programmfehler.
- Typprüfung mit mypy, Schritt für Schritt: `backup.py`, `netzone.py`, `proxy.py`, `models.py` (Rechteprüfung) und
  die Build-Kette `tools/` werden streng geprüft – das muss bei jedem Pull Request bestehen. Alle übrigen Module
  werden geprüft und nur aufgelistet (61 Funde, vorher 119). mypy läuft im festgelegten Basis-Image (Python 3.13),
  installiert mit Prüfsummen aus `tools/mypy.lock`.
- Datenbank-Beziehungen in `models.py` sind typisiert (`Mapped[...]`).

## 3.0.0 – 29.10.2026
Umstellung auf den Docker-Standard: Das Programm steckt komplett im Image. Auf dem Server liegen nur noch
`docker-compose.yml`, `.env` und die Daten – Updates wie bei anderen Containern.

**Installation und Update**
- Installation: `docker-compose.yml` und `env.example` (als `.env`) aus dem Release laden, `docker compose up -d`.
  Kein Zip, kein Programmordner, nichts wird lokal gebaut.
- Updates mit einer Container-Verwaltung (Dockhand, Watchtower, Tugtainer – Empfehlung: nachts), mit
  `docker compose pull && docker compose up -d` oder mit `update.sh`.
- **`docker-compose.yml` bleibt stabil:** Image `${CATALYTIX_IMAGE:-ghcr.io/soleil0815/catalytix}:${CATALYTIX_TAG:-latest}`,
  kein `build:` mehr, keine Versionsnummer, die sich mit jedem Release ändert; Vorgaben (z. B. Zeitzone) stehen im
  Image. Die Datei hat eine eigene Versionsnummer (`CATALYTIX_COMPOSE: "2"`). Ist sie zu alt, stehen ein Hinweis im
  Protokoll und für Admins auf der Übersicht, was zu tun ist. Das Image bringt die passende Datei mit.
- **`update.sh` neu** (POSIX-`sh`, läuft mit dash/BusyBox und auf einem NAS, mit `docker compose` oder
  `docker-compose` ab 1.27):
  - merkt sich das laufende Image als `previous`, holt die gewünschte Version (Standard `latest`;
    `--version=X.Y.Z`, auch `X.Y.Z-sN`, wird in die `.env` geschrieben), startet und wartet auf den Gesundheitscheck;
  - startet die neue Version nicht: Nachfrage (mit `-y` automatisch), Sicherung von vor dem Update zurückspielen (nur
    eine aus diesem Lauf), vorige Version starten und vorerst bei ihr bleiben;
  - ältere Version gewünscht: spielt vorher die neueste Sicherung zurück, die diese Version lesen kann;
  - neuere `docker-compose.yml` im Image: ablegen, alte als `docker-compose.yml.alt` behalten;
  - ohne Internet: `--load catalytix-X.Y.Z-amd64.tar.gz` (geprüft gegen `SHA256SUMS` bzw. `.sha256`);
  - `--cleanup` für Installationen vor 3.0.0: zeigt die nicht mehr gebrauchten Programmdateien (feste Liste, alte
    Zips, Arbeitsordner, lokal gebaute `catalytix:X.Y.Z`-Images), fragt und löscht nur diese – nie Daten,
    Sicherungen, `.env`, Compose-Dateien oder eingebundene Ordner, auch nicht über Verknüpfungen (Pfade werden
    vollständig aufgelöst, Mounts aller Container und Einhängepunkte des Hosts geschützt, Git-Ordner werden nicht
    angefasst).
  - Entfallen: Zip-Weg, lokales Bauen, Austausch von Programmdateien, `--pull`, `--no-cache`, `--prune`.

**Datenbank**
- Die Datenbank merkt sich die Programmversion (`settings.db_version`). Beim Start einer neuen Version wird sie vor
  jeder Anpassung gesichert (`catalytix-…-vor-X.Y.Z.db`, „vor Update auf X.Y.Z“; die letzten drei bleiben zusätzlich
  zu den täglichen erhalten). Schlägt diese Sicherung fehl, startet die Version nicht. Bei MySQL/PostgreSQL ein Hinweis
  im Protokoll (selbst sichern).
- Startet eine ältere Version auf einer neueren Datenbank, bricht sie mit klarer Meldung ab (welche Version, welche
  Sicherung) und ändert nichts. Wirkt ab 3.0.0 – ältere Versionen kennen die Prüfung noch nicht.
- Neue Befehle für den Weg zurück: `flask restore-before X.Y.Z [--since ZEIT]` und `flask restore-for-version X.Y.Z`
  (mit `CATALYTIX_SKIP_DB_CHECK=1`, das Prüfung und Migrationen für diesen einen Aufruf auslässt).

**Veröffentlichen und Release**
- Ein gemergter Pull Request veröffentlicht nichts mehr. Veröffentlicht wird nur von Hand: *Actions → Build → Run
  workflow* auf `main` mit **publish** – dann wie bisher beide Plattformen bauen, testen, prüfen, `:<version>` und
  `latest`. Die wöchentlichen Sicherheitsupdates (`-sN`) laufen weiter automatisch.
- Das Release enthält kein Zip mehr, sondern die Images als Dateien für den Weg ohne Internet
  (`catalytix-X.Y.Z-amd64.tar.gz`, `-arm64.tar.gz`, genau die veröffentlichten), `SHA256SUMS`, `update.sh`,
  `docker-compose.yml` und `env.example`.

**Umstieg bestehender Installationen** (Schritt für Schritt in README und Hilfe): einmal das bisherige
`./update.sh --pull`, danach `./update.sh --cleanup`.

**Unabhängiger Review vor dem Merge – behoben, jeweils mit Test:** `--cleanup` arbeitet nur in einem Ordner mit
alter Catalytix-Installation, löscht nur Einträge, die erkennbar zum Programm gehören (gleichnamige eigene Dateien
bleiben), prüft jeden Eintrag direkt vor dem Löschen erneut und lehnt Namen mit Zeilenumbruch ab (ein solcher Name
hätte `data` treffen können); Verknüpfungen in einem verlinkten Datenordner werden mit geschützt. Ein misslungenes
Zurückgehen auf eine ältere Version spielt die Daten der neueren wieder ein. Bei einer Neustart-Schleife nach
abgebrochener Migration wird nur einmal gesichert (vorher bei jedem Start), zurückgespielt wird die erste Sicherung des
Laufs. Rückweg zu 2.x stellt die Datenbank mit dem neuen Image wieder her. Der Rückweg bleibt bei einem
Sicherheitsupdate (`X.Y.Z-sN`) statt auf das ältere Release-Image zu wechseln. „Datenbank zu neu“ und eine
fehlgeschlagene Sicherung lassen gunicorn anhalten statt Worker im Sekundentakt neu zu starten. `--pull[=X]` des alten
Skripts wird weiter verstanden; Images ohne Gesundheitscheck gelten nach 30 s ohne Neustart als gestartet; `--load`
mit eigenem Registry-Namen; Release-Image-Dateien werden nach dem Erzeugen probehalber geladen und geprüft;
README: `chmod 600 .env`.

**Handbücher** (`docs/Handbuch-Catalytix.pdf`, `docs/Manual-Catalytix.pdf`) auf Stand 3.0 gebracht (vorher 2.10), mit
neuen Screenshots: Installation und Update nach dem neuen Weg, Version festlegen und zurückgehen, Umstieg, Vormerken,
Verleihen aus dem Katalog, Präsenzbestand, Personen- und Bestandssuche an der Theke, Meine Daten, Neuigkeiten,
AD-Abgleich. Dabei aufgefallen und behoben: Im Gruppenformular war das Namensfeld winzig (fehlende Rasterklassen
`span-1`/`span-5`).

Doku (README, Hilfe *Update und Wartung*, `.env.example`, Kommentare der `docker-compose.yml`) und Tests (nachgebautes
`docker` unter dash: Update, Fehlstart mit Rückweg, Zurückgehen, ohne Internet, Aufräumen mit Verknüpfungen und Mounts;
Datenbank älter/neuer als das Programm; Versionsprüfung der Compose-Datei; nur-von-Hand-Veröffentlichung) angepasst.

## 2.20.1 – 28.10.2026
Code-Review der Änderungen seit 2.17.1 (drei unabhängige Prüfer: Ausleih- und Vormerklogik, Sicherheit und Rechte,
Betrieb/CI/Oberfläche) – keine kritischen Befunde, alle bestätigten behoben, jeder mit Test.

**Wichtig**
- **GitHub: Veröffentlichung nur nach bestandenen Tests.** Die Tests im Image liefen durch `| tee`; GitHub führt
  Schritte ohne ausdrückliche Shell ohne `pipefail` aus, dadurch ging ein Fehler der Tests verloren und ein Release
  bzw. Sicherheitsupdate wäre trotzdem veröffentlicht worden (passiert ist das nicht – alle Läufe hatten bestandene
  Tests). Alle Workflows laufen jetzt mit `shell: bash` (`bash -eo pipefail`), `tools/ci.sh` setzt `pipefail` selbst
  und wertet den Rückgabewert der Tests direkt aus; ein Selbsttest prüft das unter `bash -e`.
- **Vormerkungen, die nicht mehr gelten, blockieren kein Exemplar mehr.** Beendet werden sie jetzt, wenn das Konto
  gesperrt oder gelöscht wird, die Person nicht mehr in der Gruppe des Buchs ist (auch durch den AD-Abgleich) oder
  das Buch – auch einzeln beim Bearbeiten – in den Präsenzbestand kommt. Ein dafür zurückgelegtes Exemplar geht
  sofort an die Nächste in der Warteschlange: nach Admin-Änderungen sofort, sonst beim regelmäßigen Hintergrundlauf.
  Vorher blieb es bis zu 7 Tage zurückgelegt; beim Löschen eines Kontos rückte niemand nach.
- **Kein Vordrängeln:** Ein frei gewordenes, noch nicht zurückgelegtes Exemplar kann niemand an der Warteschlange
  vorbei ausleihen – es wird sofort für die Erste zurückgelegt.

**Weitere Korrekturen**
- Ausleihen über die Adresse `/buecher/<id>/ausleihen` prüft zuerst, ob man das Buch sehen darf (sonst „nicht
  gefunden“). Vorher nannte die Meldung Titel und Gruppe von Präsenzbestand-Büchern, die im Katalog unsichtbar sind.
- Handy: Die Vormerk-Listen auf der Buchseite und unter *Meine Vormerkungen* waren bei 360 px unlesbar (Name/Titel
  auf ein Zeichen gequetscht); der Status steht jetzt unter dem Namen. Verwalter sehen dort und bei der Rückgabe
  („Bitte zurücklegen – vorgemerkt für …“) zusätzlich den Benutzernamen.
- Doppelte ISBN beim Anlegen: Die Meldung nennt die tatsächlich hinzugefügten Exemplare (an der Höchstgrenze
  weniger) und sagt, dass die übrigen Angaben des Formulars nicht übernommen wurden; bei 0 Exemplaren ein Hinweis.
- Profil: Der Anzeigename darf nicht leer sein.
- `update.sh --pull=X.Y.Z-sN` hält ein Sicherheitsupdate fest (vorher abgelehnt); die `.env` merkt sich genau dieses
  Image.
- Hilfe: aktuelle Beschriftung der Sofort-Verleihen-Option an der Theke; Vormerkungen, Erfassen und Wartung ergänzt.

## 2.20.0 – 27.10.2026
**Neu**
- **Präsenzbestand:** Gruppen haben die Option *Präsenzbestand – Bücher dieser Gruppe werden nicht verliehen*
  (*Administration → Gruppen*). Die Bücher bleiben im Katalog (Hinweis „Präsenzbestand“), werden an der Ausleihtheke
  aber nicht angeboten (weder in der Bestandssuche noch beim Scannen – dort mit klarer Meldung) und können weder
  verliehen, selbst ausgeliehen noch vorgemerkt werden; geprüft zentral bei jeder Ausleihe. Beim Einschalten werden
  offene Vormerkungen dieser Bücher beendet. Laufende Ausleihen werden normal zurückgegeben. Für AD-Personen wird
  dafür kein Konto angelegt. Neue Spalte `groups.lendable` (wird beim Start automatisch ergänzt, bestehende Gruppen
  bleiben ausleihbar).
- **Verleihen aus dem Katalog:** Unter jedem freien Buch, das man verwaltet, steht *Verleihen* (Kachel- und
  Listenansicht). Der Knopf öffnet die Buchseite beim Formular *An Benutzer verleihen*, der Cursor steht im Feld
  *Benutzer*.
- **Meine Daten im Profil:** Lokale und selbst registrierte Konten ändern Anzeigename und E-Mail-Adresse selbst –
  unabhängig davon, ob E-Mails eingeschaltet sind, mit denselben Regeln wie bisher (gültige, freie Adresse; bei
  Selbstregistrierung erlaubte Domain und ggf. Bestätigung über die neue Adresse). AD-Konten bleiben unverändert
  schreibgeschützt; das Profil weist darauf hin, dass die Daten aus dem AD kommen.

**Verbessert**
- **Doppelt erfasst:** Wird beim Anlegen ein Buch mit einer ISBN gespeichert, die es in derselben Gruppe schon gibt,
  werden die eingetragenen Exemplare beim vorhandenen Buch hinzugezählt (in SQL, gleichzeitige Erfassungen zählen
  beide) statt einen zweiten Eintrag anzulegen; neue Exemplare gehen zuerst an die Vormerk-Warteschlange. In einer
  anderen Gruppe entsteht weiterhin ein eigener Eintrag. Der Hinweis beim ISBN-Abruf sagt das jetzt.
- Hilfe (Deutsch/Englisch), README und Neuigkeiten ergänzt; 20 neue Tests.

## 2.19.0 – 25.10.2026
Bau und Auslieferung neu aufgestellt: alles fest, automatisch sicher und aktuell – ohne Versionssprünge.

**Reproduzierbarer Bau**
- Basis-Image mit fester Version **und** Prüfsumme: `python:3.13.17-slim-trixie@sha256:…` (vorher 3.13.16; in den
  GitHub-Builds von 2.18.3 der wandernde Tag `3.13-slim-trixie`).
- **Debian-Pakete** auf einen festen Archiv-Stand (`DEBIAN_SNAPSHOT`, snapshot.debian.org) – alle Pakete im Image,
  auch die des Basis-Images. Der Bau bricht ab, wenn das Basis-Image nicht Debian „trixie“ ist.
- **Python-Pakete** in `requirements.txt` mit exakter Version und SHA-256 jeder Datei, auch indirekte
  (`--require-hashes`); `pip` in `docker/pip.lock`, nach der Installation aus dem Image entfernt (geprüft).
  Die Inhaltsliste steht im Image unter `/app/image-packages.txt`.
- `tools/lock.py update` legt alle Versionen neu fest – nur innerhalb des Unterbaus: gleiche Debian-Version, gleiche
  Python-Reihe, jedes Paket innerhalb seiner Hauptversion (vor 1.0: Unterversion), Kalenderversionen (z. B.
  `certifi`) ohne Grenze; neue Versionen erst nach 7 Tagen, eine schon festgelegte Version wird nie ausgeschlossen.
  `requirements.in` folgt dieser Regel (z. B. `Flask>=3.1,<4`). Erste Festlegung: Python 3.13.17, cryptography 50.0.2,
  SQLAlchemy 2.1.5, Werkzeug 3.1.9, MarkupSafe 3.0.4, charset-normalizer 3.5.2, pycparser 3.11.
- Die Build-Optionen von 2.18.3 (`OS_UPDATES`, `DEPS=current`, `tools/lock_floor.py`) entfallen – die GitHub-Builds
  nehmen jetzt genau die festgelegten Versionen.

**Wöchentliches Sicherheitsupdate** (`.github/workflows/security-update.yml`, montags 04:17 UTC, auch von Hand)
- Code des höchsten Releases, Versionen neu festlegen, einmal ohne Zwischenspeicher für amd64 und arm64 bauen, mit
  dem veröffentlichten `latest` vergleichen – nichts geändert: nichts tun. Sonst: Tests im Image, Trivy für beide
  Plattformen, und nur wenn alles besteht: genau diese Images als `<version>-s<Zähler>` veröffentlichen, Locks und
  CHANGELOG-Eintrag (Liste der geänderten Pakete) auf `main`, danach `latest` umstellen. Ersetzt den Wochen-Neubau
  `rebuild.yml`.
- Auch beim Release werden beide Plattformen einmal gebaut, getestet bzw. geprüft und genau diese Images
  veröffentlicht. Der Debian-Stand ist immer ein vollständig importierter Zeitpunkt (Vortag 00:00 UTC), der Bau prüft,
  dass kein Paket zurückgehalten wurde. Prüfungen der Registry und der GitHub-API werten Fehler nie als „nicht
  vorhanden“; die höchste Version wird aus allen Releases bestimmt. Unabhängiger Review der neuen Abläufe vor der
  Veröffentlichung.

**Trivy, Scans, Unterbau**
- Trivy nicht mehr als GitHub Action, sondern feste Version 0.76.0, per SHA-256 geprüft (`tools/trivy.lock`,
  `tools/trivy_scan.py`); nur HOCH/KRITISCH mit Korrektur stoppt. `.trivyignore`: jeder Eintrag mit Begründung und
  Ablaufdatum (höchstens 6 Monate), sonst stoppt der Lauf.
- **Täglicher Scan** des veröffentlichten `latest` (`scan.yml`).
- **Unterbau-Prüfung** wöchentlich (`foundation.yml`, endoflife.date): ab 12 Monaten vor Ende der Sicherheitsupdates
  von Debian 13 bzw. Python 3.13 ein Hinweis, ab 6 Monaten ein roter Lauf.

**Sicherheit der Workflows**
- Alle Actions auf Commit-Prüfsummen, `permissions: {}` als Vorgabe und minimale Rechte je Job, keine gespeicherten
  Git-Anmeldedaten (`persist-credentials: false`), veröffentlicht wird nur von `main` und nie gleichzeitig (gemeinsame
  Sperre), veröffentlichte Tags werden nie überschrieben, `latest` nur für die höchste Version. Tests laufen im
  fertigen Image statt auf dem Runner. Ursachen von Fehlern stehen lesbar als Anmerkung am Lauf.
- Selbsttests (`tests/test_supply_chain.py`): Zeitpläne, Reihenfolge der Schritte, Versionsgrenzen, Rechte, Pinning,
  Ablauf mit nachgestelltem Trivy, Zähler und Änderungsprotokoll, Unterbau-Schwellen.
- README und Hilfe (*Wartung*): was automatisch passiert und was bei einem roten Lauf zu tun ist.

## 2.18.3 – 24.10.2026
- **Alles im Image von GitHub bleibt aktuell** – bei jedem Release und beim wöchentlichen Neubau, innerhalb derselben
  Grundlage:
  - Python: neueste 3.13-Patch-Version (`python:3.13-slim-trixie` statt fest 3.13.16), Debian 13 „trixie“ plus
    aktuelle Sicherheitsupdates.
  - Python-Pakete: neu aufgelöst aus `requirements.in` (`DEPS=current`) – neueste Versionen innerhalb der erlaubten
    Bereiche, aber nur Veröffentlichungen, die mindestens 7 Tage alt sind (`pip --uploaded-prior-to`, Schutz vor
    frisch hochgeladenen Schadversionen). Versionen aus `requirements.txt` gelten dabei als Untergrenze: Eine geprüfte
    Sicherheitskorrektur, die dort eingetragen ist, darf die Wartezeit überspringen (`tools/lock_floor.py`). Der erste
    Lauf hatte genau das gezeigt: Die Wartezeit hätte die Korrektur von certifi (5 Tage alt) zurückgehalten, Trivy hat
    die Veröffentlichung gestoppt.
  - **Die Tests laufen jetzt im fertigen Image** (genau dessen Python und Pakete) – erst danach Trivy und
    Veröffentlichung. Die installierten Versionen stehen in `/app/installed-packages.txt` und in der Zusammenfassung des
    Laufs.
- Lokale Builds (Zip-Paket, `update.sh`) bleiben exakt reproduzierbar: gepinntes Basis-Image und `requirements.txt`.
- Der Unterbau (Debian 14, Python 3.14, neue Hauptversionen) wird bewusst angehoben, wenn die jetzige Grundlage keine
  Updates mehr bekommt.

## 2.18.2 – 24.10.2026
- **Wöchentlicher automatischer Neubau** (`.github/workflows/rebuild.yml`, ersetzt den reinen Prüflauf `scan.yml`):
  Jeden Montag wird der Stand des **letzten Releases** (nie unveröffentlichte Commits von `main`) ohne Build-Cache
  neu gebaut – mit den aktuellen Debian-Sicherheitsupdates –, getestet und mit Trivy geprüft. Nur wenn alles
  besteht, wird das Image als `latest` veröffentlicht, dazu `<version>-r<Datum>` und `sha-<commit>`; der Tag
  `<version>` bleibt das Image des Releases. Läuft nie gleichzeitig mit einem Release-Build.
- Findet Trivy eine Lücke in einem (fest gepinnten) Python-Paket, schlägt der Lauf fehl und nichts wird veröffentlicht
  – GitHub meldet das per E-Mail; dann braucht es eine neue Version mit dem aktualisierten Paket.
- `Dockerfile`: `OS_UPDATES=1` (in allen GitHub-Builds gesetzt) spielt die aktuellen Debian-Sicherheitsupdates auf das
  gepinnte Basis-Image. Lokale Builds (Zip-Paket) bleiben ohne die Option exakt beim gepinnten Stand.

## 2.18.1 – 24.10.2026
- **Sicherheitsprüfung mit Trivy** im GitHub-Workflow: Das Image wird vor dem Veröffentlichen gebaut und geprüft.
  Bekannte Schwachstellen der Stufe HIGH/CRITICAL, für die es eine Korrektur gibt, stoppen den Lauf – dann wird
  nichts veröffentlicht (Bericht als Tabelle in der Zusammenfassung des Laufs und als Anmerkung). Schwachstellen ohne
  verfügbare Korrektur blockieren nicht. Ausnahmen mit Begründung in `.trivyignore`. Zusätzlich prüft
  `.github/workflows/scan.yml` jeden Montag das veröffentlichte `latest`.
- Die Trivy-Action ist auf den vollen Commit des unveränderlichen Releases v0.36.0 gepinnt, Trivy selbst auf v0.76.0
  (nach dem Supply-Chain-Angriff auf Trivy im März 2026, CVE-2026-33634); ein Test prüft das.
- **Behoben, was die erste Prüfung gefunden hat** (33 Befunde, 6 kritisch):
  - **Pillow 13.0.0** (vorher 12.3.0) – 29 Lücken in der Bildverarbeitung, die bei Cover- und Logo-Uploads genutzt
    wird. Der Code nutzt jetzt `Image.Resampling.LANCZOS`.
  - **certifi 2026.10.19** (vorher 2026.7.22).
  - **pip wird nach der Installation aus dem Image entfernt** – es wird zur Laufzeit nicht gebraucht und brachte eigene,
    veraltete Kopien von urllib3, msgpack und setuptools mit.

## 2.18.0 – 24.10.2026
- **Vormerken (Reservieren)**, optional unter *Einstellungen → Vormerken* (standardmäßig aus): Sind alle Exemplare
  verliehen, kann man ein Buch auf seiner Seite vormerken und steht in einer Warteschlange (Platz sichtbar, auch unter
  *Meine Ausleihen*, dort auch löschbar).
- Wird ein Exemplar frei – Rückgabe, zusätzliches Exemplar, Vormerkung gelöscht oder abgelaufen –, wird es für die
  erste Person **zurückgelegt**: E-Mail „liegt für dich bereit“, Hinweis auf der Übersicht, an der Theke meldet die
  Rückgabe „Bitte zurücklegen – vorgemerkt für …“. Für alle anderen zählt das Exemplar als nicht verfügbar (Katalog,
  Filter, Vorschläge, Theke, CSV-Export).
- **Abholfrist** (Standard 7 Tage): Wird das Buch nicht abgeholt, läuft die Vormerkung ab und die nächste Person ist
  dran. Mit der Ausleihe an die Person ist die Vormerkung erledigt; an der Theke steht das zurückgelegte Buch nach Wahl
  der Person oben unter „Für … zurückgelegt“.
- Vorgemerkte Bücher können Leser nicht selbst verlängern (Verwalter schon). Verwalter sehen die Warteschlange auf der
  Buchseite und können Vormerkungen löschen. Höchstzahl offener Vormerkungen je Person einstellbar (Standard 5).
- Weniger Exemplare als zurückgelegte: die zuletzt zurückgelegten gehen wieder in die Warteschlange (Platz bleibt).
  Gesperrte Konten fallen beim Nachrücken heraus. Ausschalten löscht alle offenen Vormerkungen.
- Abgeschlossene Vormerkungen werden mit der Aufbewahrungsfrist der Ausleihen gelöscht; Hinweis für die
  Datenschutzerklärung in der Hilfe.
- Datenbank: neue Tabelle `reservations` – wird beim Start automatisch angelegt.
- Hilfe zur Ausleihtheke: Texte zur Automatik „Bei genauem Treffer (ISBN, Signatur)“ an 2.17.1 angepasst.
- README: Netzangabe `172.16.0.0/12` (Abschnitt „Internes Netz“) war seit 2.16.1 durch ein Versehen beim
  Hochzählen der Versionsnummer verfälscht.

## 2.17.1 – 24.10.2026
Code-Audit (vier unabhängige Prüfer: Anmeldung/Rechte, Fachlogik/Daten, Betrieb/Update/CI, Oberfläche) – keine
kritischen Befunde, alle bestätigten behoben; dazu folgt `./update.sh --pull` jetzt `latest`.

**Sicherheit und Rechte**
- **Gruppen-Verwalter ohne Gruppe:** Verlor ein Verwalter seine letzte Gruppe (Gruppe gelöscht, Haken entfernt, im
  AD aus der Gruppe genommen), verwaltete er danach den **gesamten** Bestand. Jetzt merkt sich das Konto, dass es
  gruppengebunden war (`users.group_bound`), und verwaltet dann nur allgemeine Bücher. Den gesamten Bestand gibt es nur
  noch, wenn ein Admin im Benutzerkonto „Ohne Gruppe den gesamten Bestand verwalten“ ankreuzt. Neue Verwalter ohne
  Gruppe verwalten wie bisher alles; bestehende Konten mit Gruppen werden beim Start als gruppengebunden markiert.
- **AD-Regeln für OUs:** Ein maskiertes Komma im Namen (`CN=Mallory\, OU=Bib-Admins,…`) konnte die Zugehörigkeit zu
  einer OU vortäuschen und damit eine Rolle verschaffen. Verglichen wird jetzt Bestandteil für Bestandteil.
- **Im AD abgelaufene Konten** (`accountExpires`) gelten wie deaktivierte: Sitzungen enden, der Abgleich sperrt sie.
- **Registrierung:** Prüfungen „Name/Adresse schon vergeben?“ sind je Adresse begrenzt (10 in 15 Minuten, für alle
  Server-Prozesse) – vorher ließen sich Konten und AD-Namen beliebig oft abfragen.
- Ein von einem Admin gesperrtes, noch unbestätigtes Konto wird durch den Bestätigungslink nicht mehr aktiviert.
- Die Theke macht ein inzwischen lokales Konto (Passwort per `flask reset-password`) nicht mehr zum AD-Konto;
  `reset-password` hebt auch die Sperre durch den AD-Abgleich auf. Anmeldesperren zählen Schreibweisen wie
  „ＡＮＮＡ“ (Vollbreite) wie „anna“.

**Ausleihe und E-Mail**
- **Erinnerungen nach einer Verlängerung:** Bisher galt eine einmal verschickte Erinnerung (bzw. die Höchstzahl an
  Mahnungen) für die ganze Ausleihe – nach einer Verlängerung kam nichts mehr. Jetzt zählt je Frist
  (`loans.due_changed_at`).
- **Mailserver trennt unterwegs** (Nachrichtenlimit, Zeitlimit): Es wird neu verbunden; gelingt das nicht, wird der
  Tag freigegeben und der Rest später nachgeholt (vorher gingen die übrigen Erinnerungen verloren).
- Die Liste der fälligen Erinnerungen entsteht in wenigen Abfragen statt mehreren je Ausleihe (2.000 Ausleihen: vorher
  rund 7.000 Abfragen beim Öffnen der E-Mail-Einstellungen). „Jetzt senden“ und der tägliche Lauf können sich nicht
  mehr überschneiden (keine doppelten Erinnerungen).
- **Ausleihtheke:** „Bei genauem Treffer sofort verleihen/zurücknehmen“ handelt nur noch bei Signatur, ISBN oder
  „#Nummer“ – nicht mehr bei einem zufällig einzigen Treffer in Titel, Autor oder Schlagwort. Die Personensuche
  per Freitext findet nur aktive Konten und nimmt `%`/`_` wörtlich.
- Eine doppelte Rückgabe (Doppelklick, zwei Theken) überschreibt den Zeitpunkt nicht und verschickt nur eine
  Bestätigung. Die Höchstzahl an Ausleihen je Person hält auch bei gleichzeitigen Ausleihen unter MySQL/PostgreSQL.
- Benutzerliste: Gruppen werden in einer Abfrage geladen.

**Update und GitHub**
- **`./update.sh --pull` folgt jetzt `latest`:** In der `.env` steht danach `CATALYTIX_TAG=latest` statt einer
  festen Versionsnummer. Damit kann eine Container-Verwaltung, die Images automatisch aktualisiert, neue Versionen
  selbst ziehen. `./update.sh --pull=X.Y.Z` hält genau eine Version fest (`CATALYTIX_PINNED=1`), bis zum nächsten
  `--pull`. Installationen, die von älteren Versionen eine feste Nummer eingetragen bekamen, folgen ab jetzt `latest`.
- **GitHub veröffentlicht `latest` und `<version>` nur noch bei einer neuen Versionsnummer** (höher als das letzte
  Release). Pushes ohne neue Nummer erzeugen nur `sha-<commit>` – Server, die `latest` folgen, bekommen nie
  unveröffentlichte Zwischenstände. Keine Tag-Läufe mehr (kein Wettlauf beim Release), `workflow_dispatch`
  veröffentlicht nur auf `main`.
- `update.sh`: Die installierte Version kommt aus dem laufenden Container (nicht aus evtl. veralteten Dateien im
  Ordner). Wird ein Update nach dem `docker pull` abgebrochen, zeigt der Tag wieder auf das alte Image – vorher konnte
  ein späteres `docker compose up` die neue Version ohne Sicherung starten und das Aufräumen das alte Image löschen.
  Die Rücknahme bei einem Fehler während des Austauschs löscht nie mehr ein Original, das noch nicht verschoben war.
  Im Pull-Modus werden veraltete Programmdateien früherer Zip-Installationen weggeräumt. Hinweis für den Weg zurück
  im Pull-Modus: `./update.sh --pull=<alte Version>`. Fehler beim Schreiben der `.env` werden gemeldet.
- Image: Gesundheitsprüfung mit 120 s Anlaufzeit (große `./data`), `update.sh` wartet bis zu 300 s; die
  Versionsnummer steht als letztes Label (Abhängigkeiten bleiben im Build-Cache).
- README: `docker-compose` 1.x erst ab 1.27 (ältere lesen die `docker-compose.yml` ohne `version:` nicht); `7z`
  wird nicht unterstützt.

**Oberfläche**
- Eine spät eintreffende AD-Antwort in der Personensuche überschreibt keine schon gewählte Person mehr und öffnet die
  Liste nicht wieder. Die Kamera geht aus, wenn der Scanner während des Starts geschlossen wird. In der
  Schnellerfassung und beim ISBN-Abruf zählt nur noch das Ergebnis der zuletzt gescannten ISBN.
- Abgelaufene Sitzung: Abfragen aus der Seite bekommen eine klare Meldung (HTTP 401) statt eines leeren Fehlerkastens.
- Auswahllisten (Sortierung, Filter) lassen sich mit der Tastatur bedienen, ohne dass jede Pfeiltaste neu lädt.
  Fehlermeldungen bleiben stehen, bis man sie schließt.
- Handy: Lange Titel, Namen und Befehle verbreitern die Seite nicht mehr (u. a. Hilfe, Übersicht, Buchformular).
- Neuigkeiten: Ein Sicherheitsupdate steht im Hinweis auf der Übersicht immer vorne; der Punkt am Benutzermenü wird
  auch vorgelesen. Programmname aus Hilfe und CSV-Dateinamen entfernt (`bestand.csv` / `catalogue.csv`).
- Datenbank: neue Spalten `users.group_bound` und `loans.due_changed_at` – werden beim Start automatisch angelegt.

## 2.17.0 – 24.10.2026
- **Neuigkeiten in der App:** Unten in der Seitenleiste neben der Versionsnummer (und im Benutzermenü) steht
  *Neuigkeiten* – kurze, verständliche Hinweise zu neuen Funktionen, Verbesserungen und **Sicherheitsupdates**,
  deutsch und englisch. Jeder sieht nur, was seine Rolle betrifft (Leser keine reinen Admin-Themen).
- Dezent: Gibt es nach einem Update etwas Neues, erscheint ein kleiner Punkt neben *Neuigkeiten* (auf dem Handy am
  Avatar) und auf der Übersicht eine schmale Zeile „Neu in Version …“, die sich wegklicken lässt. Wer die Seite
  öffnet oder die Zeile schließt, sieht sie bis zum nächsten Update nicht mehr. Neue Konten starten ohne alte
  Meldungen; bestehende Konten sehen nach diesem Update nur die Neuigkeiten dieser Version, die älteren stehen auf
  der Seite zum Nachlesen.
- Die Texte stehen in `catalytix/news.json` (je Version; Art `security`/`new`/`improved`/`fixed`, Zielgruppe
  `all`/`librarian`/`admin`). Ein Test prüft Vollständigkeit beider Sprachen und dass die aktuelle Version einen
  Eintrag hat.
- Datenbank: neue Spalte `users.news_seen` – wird beim Start automatisch angelegt.
- GitHub: Das **Release mit Zip-Paket entsteht jetzt automatisch**, sobald eine neue Versionsnummer auf `main`
  ankommt – kein Tag mehr nötig (gibt es das Release schon, passiert nichts).

## 2.16.1 – 23.10.2026
- **GitHub-Workflow repariert:** Der Testlauf auf GitHub brach ab, weil ein Test PyYAML benutzte, das nicht zu den
  Paketen von Catalytix gehört. Der Test prüft die Workflow-Datei jetzt ohne PyYAML.
- Workflow nutzt die aktuellen Versionen der GitHub-Actions (`checkout@v7`, `setup-python@v7`, Docker-Actions v4/v7),
  die mit Node.js 24 laufen – die Warnung „Node.js 20 actions are deprecated“ entfällt.

## 2.16.0 – 23.10.2026
- **GitHub baut den Container:** Workflow `.github/workflows/build.yml` für das Repository `soleil0815/catalytix`.
  Bei jedem Push auf `main` laufen die Tests mit den festgelegten Paketversionen, danach entsteht das Image
  `ghcr.io/soleil0815/catalytix` (Tags `<version>`, `latest`, `sha-<commit>`; für Intel/AMD und ARM). Ein Tag
  `v<version>` erzeugt zusätzlich ein Release mit Zip-Paket und Prüfsumme. Der Workflow prüft, dass die
  Versionsnummern in Code, `Dockerfile` und `docker-compose.yml` übereinstimmen.
- **`./update.sh --pull`** holt das fertige Image von GitHub statt lokal zu bauen (`--pull=X.Y.Z` für eine bestimmte
  Version). Die passenden Installationsdateien (`docker-compose.yml`, `update.sh`, `.env.example`, READMEs) kommen
  aus dem Image; Image und Version merkt sich die `.env` (`CATALYTIX_IMAGE`, `CATALYTIX_TAG`), damit
  `docker compose up -d` immer genau diese Version startet. Sicherung vorher, Prüfungen und Aufräumen wie beim
  klassischen Update. Neuinstallation: nur `update.sh` in einen leeren Ordner legen und `./update.sh --pull`.
- **Klassisch bleibt:** `./update.sh catalytix-X.Y.Z.zip` baut wie bisher lokal und schaltet von `--pull` automatisch
  zurück.
- `docker-compose.yml`: Image wählbar über `CATALYTIX_IMAGE`/`CATALYTIX_TAG`; **Dockhand-Label entfernt**.
- `.gitignore` hält Daten, Schlüssel, `.env`, Datenbanken und Pakete aus dem Repository; Image mit OCI-Labels
  (Quelle, Beschreibung), damit GitHub es dem Repository zuordnet.

## 2.15.2 – 23.10.2026
- **Server-Meldungen auf Englisch:** Protokollzeilen (z. B. Startfehler, Schlüsselprüfung, Zeitzone, AD-Abgleich,
  Anmeldungen, Mailversand, Datensicherung) und die Kommandozeilen-Befehle (`flask --app wsgi …`: Hilfe, Rückfragen,
  Ausgaben) sind jetzt englisch. Ergebnisse, die auch in der Oberfläche erscheinen (Sicherung, Aufbewahrung,
  Erinnerungslauf), werden in der Oberfläche weiter in der gewählten Sprache angezeigt, im Protokoll und auf der
  Kommandozeile englisch. Die Oberfläche selbst bleibt deutsch und englisch.

## 2.15.1 – 23.10.2026
- **Installationsdateien auf Englisch:** Ausgaben, Rückfragen und Hilfe von `update.sh` sowie die Kommentare in
  `docker-compose.yml`, `.env.example`, `Dockerfile`, `requirements.txt`/`requirements.in`, `wsgi.py` und die
  Meldungen von `docker-entrypoint.sh` sind jetzt englisch. Die Rückfrage von `update.sh` lautet `[Y/n]`
  („j“ wird weiter angenommen). Oberfläche, Hilfe und READMEs bleiben zweisprachig.
- Bestehende `.env`-Dateien werden nicht verändert – nur die Vorlage `.env.example` ist neu formuliert.

## 2.15.0 – 23.10.2026
Sicherheitsprüfung (Anwendung sowie Betrieb und Docker, je ein unabhängiger Prüfer) – keine kritischen Befunde,
alle übrigen behoben; dazu Aufräumen beim Update.

**Update aufräumen**
- `update.sh` räumt nach einem erfolgreichen Update auf: ältere Catalytix-Images und Zip-Pakete
  `catalytix-X.Y.Z.zip` (im Programmordner und neben dem eingespielten Paket – das aktuelle und das vorige bleiben
  für den Weg zurück, ebenso neuere), Arbeitsordner abgebrochener Updates nach 7 Tagen und Python-Caches. Andere
  Dateien, Verknüpfungen, gemeinsame Ordner wie `/tmp` und `./data` werden nie angefasst. `--prune` behält nur den
  aktuellen Stand, `--no-cleanup` schaltet das Aufräumen ab.

**Sicherheit – Anwendung**
- **Ersteinrichtung nur mit Einrichtungscode.** Bisher konnte jeder, der Port 9300 vor euch erreichte, das
  Administratorkonto anlegen. Der Code steht im Protokoll, `update.sh` zeigt ihn nach der Installation an
  (`flask --app wsgi setup-code`); Rateversuche werden gebremst.
- **Links in Registrierungs-Mails** entstehen nur noch aus der Basis-Adresse (*Administration → E-Mail*), nie aus
  der Anfrage – ein gefälschter Host-Kopf konnte Bestätigungs- und Admin-Mails auf eine fremde Seite zeigen lassen.
  Ohne Basis-Adresse ist die Registrierung mit E-Mail-Bestätigung gesperrt (mit Hinweis in den Einstellungen).
- **AD ohne Dienstkonto:** Das Passwort wird jetzt immer gegen genau den gefundenen Eintrag geprüft. Vorher
  konnte jemand, dessen UPN zum Anmeldenamen einer anderen Person passt, sich als diese anmelden.
- **Fehlversuche:** Von außen ausgelöste Sperren gelten nur noch für Zugriffe von außen (im Hausnetz kann man sich
  weiter anmelden); eine Adresse aus dem Internet darf nicht mehr ein Passwort bei allen Konten durchprobieren;
  beim zweiten Faktor höchstens 30 falsche Codes je Konto und Tag.
- **E-Mail-Adresse im Profil:** Selbst registrierte Konten müssen eine neue Adresse bestätigen; eine Adresse, die
  ein anderes Konto nutzt, wird abgelehnt.
- Im Hausnetz ohne zweiten Faktor angemeldete Admins müssen ihn jetzt auch für Aufbewahrung „Jetzt bereinigen“,
  Datensicherungs-Einstellungen, Registrierungen ablehnen und Gruppen löschen bestätigen.
- AD-Rollenregeln: Hinweis, für Admins und Verwalter den vollständigen Gruppen-DN zu nehmen (ein bloßer Name passt
  auf jede gleichnamige Gruppe); „Gruppen laden“ übernimmt dafür den DN. Warnung bei unverschlüsselter Verbindung.
- Gruppen-Verwalter sehen bei Standorten und beim Überfällig-Zähler nur noch ihren Bestand.
- Hinweis in den 2FA-Einstellungen, wenn das Hausnetz ohne zweiten Faktor auskommt, aber noch jede private
  Adresse als Reverse-Proxy gilt; Empfehlungen für den Betrieb im Internet in der README.

**Sicherheit – Betrieb**
- `update.sh` prüft das Paket gegen `catalytix-X.Y.Z.zip.sha256` (liegt jedem Paket bei), sonst zeigt es die
  Prüfsumme an. Dateirechte aus dem Paket werden nicht blind übernommen (nichts für andere beschreibbar),
  neue Dateien gehören dem Besitzer des Programmordners, die `.env` ist nur für ihn lesbar. Kein `7z` mehr.
- Datenbank, Cover, Schlüssel und Sicherungen sind nur noch für den App-Benutzer lesbar (`umask 077`, bestehende
  Dateien werden beim Start angepasst).
- Container mit `no-new-privileges`; Sicherungen werden nie über Verknüpfungen ausgeliefert; `X-Forwarded-Port`
  wird nicht mehr übernommen.

## 2.14.0 – 23.10.2026
- **AD-Abgleich und Personen-Cache** (*Active Directory*, mit Dienstkonto standardmäßig an): Im Hintergrund –
  stündlich, einstellbar bis täglich, „Jetzt abgleichen“ startet ihn sofort – holt Catalytix alle AD-Personen, die
  die Bibliothek nutzen dürfen. Die Personensuche an der Theke findet sie dann in Millisekunden, ohne das AD zu
  fragen (Lasttest: 3.000 Personen, Suche unter 30 ms). Nur wenn der Cache niemanden kennt, fragt die Suche
  zusätzlich direkt das AD. Verliehen wird weiterhin nur nach einer frischen Prüfung im AD.
- Im AD **deaktivierte, gelöschte oder nicht mehr berechtigte** Personen fliegen beim Abgleich aus dem Cache.
  Ausschalten des Caches löscht ihn sofort.
- **Bestehende AD-Konten werden nachgezogen**, auch bei Personen, die sich nie anmelden: Name, E-Mail, Rolle,
  Bibliotheksgruppen. **Umbenennungen im AD** werden erkannt (feste AD-Kennung objectGUID) – das Konto samt
  Ausleihen zieht mit, auch bei der Anmeldung mit dem neuen Namen. Wird ein Name im AD an eine andere Person
  vergeben, wird das alte Konto unter einem Ersatznamen gesperrt, statt dass die neue Person dessen Ausleihen erbt.
- Konten von Personen, die im AD fehlen oder nicht mehr berechtigt sind, werden **gesperrt, nicht gelöscht**
  (Ausleihen bleiben sichtbar, Benutzerliste: „nicht mehr im AD“) und automatisch wieder freigegeben, wenn die
  Person zurückkommt – auch sofort an der Theke oder bei der Anmeldung. Von Hand gesperrte Konten bleiben gesperrt.
- **Sicherheitsbremse:** Würde ein Abgleich mehr als 30 % der AD-Konten auf einmal sperren, sperrt er keines und
  meldet das. Ist die AD-Suche unvollständig (z. B. Größenlimit) oder das AD nicht erreichbar, bleibt alles
  unverändert. Es läuft immer nur ein Abgleich gleichzeitig, auch bei mehreren Server-Prozessen.
- Verschachtelte Gruppen werden nur neu aufgelöst, wenn sich die direkten Gruppen geändert haben, sonst einmal
  täglich. Statusanzeige auf der Seite *Active Directory*. Hinweise für die Datenschutzerklärung in README und Hilfe.
- Datenbank: neue Tabelle `directory_people`, neue Spalten `users.ldap_guid` und `users.ldap_gone_at` – werden beim
  Start automatisch angelegt (samt Index).

**Code Review zu dieser Version – behoben**
- Umbenennung plus Neuvergabe eines Namens im AD (z. B. „berta“ → „berta.m“, neue Person heißt „berta“): Das
  Konto wird jetzt unabhängig von der Reihenfolge im AD richtig zugeordnet – erst über die feste Kennung, dann über
  den Namen. Vorher konnte das Konto der umbenannten Person kurzzeitig gesperrt werden.
- Die regelmäßige Sitzungsprüfung überschreibt ein Konto nie mehr mit den Daten einer anderen Person, wenn deren
  Name im AD neu vergeben wurde – die Sitzung wird beendet. Gehört ein Name einer anderen Person, prüft Catalytix
  im AD, ob die bisherige nur umbenannt wurde (dann zieht ihr Konto um) oder ausgeschieden ist (dann wird es
  stillgelegt). Ist das nicht klärbar, wird nichts angelegt.
- Von Hand gesperrte Konten werden auch in diesen Fällen nie automatisch freigegeben. Neu: Ein vom Abgleich
  gesperrtes Konto, das ein Admin mit „Konto aktiv“ aus speichert, bleibt dauerhaft gesperrt.
- Schlägt bei einer Person die Gruppenabfrage vorübergehend fehl (Zeitlimit, Server beschäftigt), gilt sie als
  „ungewiss“ und bleibt unverändert – vorher wäre sie aus dem Cache gefallen und ihr Konto gesperrt worden.
- Während der AD-Abfrage ist die Datenbank nicht mehr gesperrt; geschrieben wird nur, was sich geändert hat
  (5.000 Personen: 0,6 s statt 5,7 s). Die Abgleich-Sperre gibt regelmäßig ein Lebenszeichen und wird nur vom
  eigenen Lauf freigegeben.
- Nach einem Fehler wird nach 15 Minuten erneut versucht (nicht erst im nächsten Zeitfenster); ein Cache, der älter
  als zwei Tage ist (AD lange nicht erreichbar), wird nicht mehr benutzt; Abschalten während eines Laufs speichert
  nichts; Bremse auch für Massen-Umbenennungen (z. B. nach Wechsel des Anmeldeattributs); Ersatznamen sind immer
  eindeutig; die Theke prüft gesperrte Konten höchstens einmal pro Minute im AD nach.
- Nachprüfung: Die Sicherheitsbremse zählt auch Konten mit, die wegen eines neu vergebenen Namens stillgelegt
  würden (z. B. Domänen-Migration mit lauter neuen Kennungen – dann wird keines stillgelegt). OpenLDAP-Kennungen
  (entryUUID) werden bei der Klärung gefunden. Teilantworten bei Gruppenabfragen gelten als „ungewiss“. Ein vom
  Namen eines stillgelegten Kontos blockierter Umzug wird geklärt; eine so abgelehnte Anmeldung zählt nicht als
  Fehlversuch.
- Bestehende AD-Konten werden auch **ohne Personen-Cache** abgeglichen (dann einmal täglich), sobald ein
  Dienstkonto eingetragen ist.

## 2.13.0 – 22.10.2026
- **Suchfeld oben mit Vorschlägen:** Schon beim Tippen (ab zwei Zeichen) erscheinen passende Bücher mit Autor,
  Signatur und freien Exemplaren. Klick oder Pfeiltasten + Enter öffnen das Buch direkt; Enter ohne Auswahl zeigt wie
  bisher alle Treffer im Katalog. Gezeigt wird nur, was man im Katalog auch sehen darf. Genaue ISBN- und
  Signatur-Treffer stehen oben, danach Titel, die mit dem Suchtext beginnen.
- **Personensuche an der Theke deutlich schneller:** Bekannte Personen erscheinen sofort, die Treffer aus dem Active
  Directory kommen einen Moment später dazu (vorher wartete die ganze Liste auf das AD – wer schneller Enter drückte,
  bekam keinen Vorschlag). Eine schon mit den Pfeiltasten gewählte Person bleibt beim Nachladen gewählt.
- AD-Suche: Die (verschachtelten) Gruppen gefundener Personen werden fünf Minuten zwischengespeichert – beim
  Weitertippen fragt das AD nicht jedes Mal erneut. Ändern sich die direkten Gruppen im AD, wird sofort neu
  aufgelöst; Anmeldung und Nachprüfung fragen immer frisch.
- Vorschläge reagieren schneller (150 statt 250 ms nach dem letzten Tastendruck); die Browser-eigene
  Eingabeliste überdeckt das Suchfeld oben nicht mehr.
- Kleinigkeit: In der Personenliste stand bei Personen ohne Gruppe ein überzähliger Punkt („· 1 ausgeliehen“).

## 2.12.2 – 22.10.2026
Code Review zu 2.12.1:
- `update.sh` ohne `unzip`: Ein beschädigtes Paket wurde beim Entpacken mit `python3` nicht erkannt (die
  Prüfung meldete den Fehler nur als Text). Jetzt bricht das Skript ab, bevor etwas ersetzt wird.
- `update.sh` legt die `.env` erst an, wenn die `docker-compose.yml` da ist – bei einer Neuinstallation also aus der
  Vorlage des Pakets statt als leere Datei.
- Neue Tests: Update mit altem `docker-compose` ohne `.env` und ohne `unzip`, beschädigtes Paket, Neuinstallation
  (insgesamt 407).

## 2.12.1 – 22.10.2026
- **Keine Mindestversion für Docker Compose mehr** (bisher 2.24). Catalytix läuft damit auch mit älteren
  Compose-Versionen, z. B. auf einer Synology. Die `docker-compose.yml` liest die `.env` jetzt auf dem klassischen
  Weg ein – die Datei muss deshalb existieren (`cp .env.example .env`, alles darin bleibt optional).
- `update.sh` legt eine fehlende `.env` selbst aus der Vorlage an, nimmt ersatzweise das ältere `docker-compose`,
  kommt ohne `docker compose ps --status` aus und entpackt notfalls mit `python3` oder `7z`, wenn `unzip` fehlt.
- **Beim Update von 2.12.0 oder älter:** Gibt es noch keine `.env`, vorher einmal `cp .env.example .env`
  ausführen – das Update läuft noch mit dem alten `update.sh`, das die Datei nicht selbst anlegt. Sonst bricht der
  Start mit „env file … .env not found“ ab; dann `.env` anlegen und `./update.sh` erneut ausführen.

## 2.12.0 – 22.10.2026
- **Bestandssuche an der Ausleihtheke:** Im Buchfeld erscheinen beim Tippen passende Bücher – Suche nach Titel,
  Autor, Untertitel, Signatur, ISBN und Schlagwort, mit Standort, freien und verliehenen Exemplaren und Gruppe.
  Auswahl mit Pfeiltasten/Enter oder Klick. Bücher, die die gewählte Person nicht ausleihen darf, stehen hinten und
  sind markiert; im Reiter *Rückgabe* stehen ausgeliehene Bücher vorne. Gruppen-Verwalter sehen nur ihren Bestand.
- Das Buchfeld funktioniert auch ohne gewählte Person (nur Anzeige des Bestands, kein Verleihen).
- Die Suche per Eingabe + Enter findet jetzt auch über Autor, Untertitel und Schlagwörter; `%` und `_` werden
  wörtlich genommen.
- Code Review dazu: Genaue Treffer (Signatur, ISBN) stehen immer oben, auch bei vielen Teiltreffern. Vorausgewählt
  ist nur ein genauer Treffer – sonst sucht Enter nach dem getippten Text, statt den ersten Vorschlag zu verleihen.
  Bücher, die die Person schon hat, stehen hinten und sind markiert; für noch nicht angelegte AD-Personen gelten
  deren Gruppen.

## 2.11.0 – 22.10.2026
- **Personensuche bei der Ausleihe:** An der Ausleihtheke und auf der Buchseite (*An Benutzer verleihen*) gibt es
  eine Live-Suche nach Name, Benutzername oder E-Mail – mit Pfeiltasten und Enter bedienbar. Die lange Auswahlliste
  aller Benutzer auf der Buchseite entfällt (schneller bei vielen Benutzern).
- **Auch noch nie angemeldete AD-Personen:** Mit Dienstkonto durchsucht die Suche zusätzlich das Active Directory.
  Angezeigt werden nur aktive Konten, die laut Rollenregeln die Bibliothek nutzen dürfen, markiert mit „neu aus dem
  AD“. Angelegt wird das Konto erst beim ersten Verleihen – mit Name, E-Mail, Rolle und Gruppen aus dem AD, ohne
  Passwort; scheitert die Ausleihe (z. B. falsche Gruppe), wird nichts angelegt. Niemand wird dadurch angemeldet,
  lokale Konten gleichen Namens bleiben unverändert. Ist das AD nicht erreichbar, findet die Suche weiter alle
  bekannten Benutzer. Das Verzeichnis wird erst ab drei Zeichen gefragt, Ergebnisse eine Minute zwischengespeichert,
  nach einem AD-Ausfall eine Minute nur lokal gesucht – das schont das AD und hält die Seite flott.
- Gruppen-Verwalter sehen in der Suche nur ihre Gruppen und Ausleihen aus ihrem Bestand (wie an der Theke).

**Code Review zu dieser Version – behoben**
- Zwei-Faktor-Bestätigung im LAN gilt jetzt auch für Freigaben neuer Konten, E-Mail-Einstellungen und
  Datensicherungen (anlegen, herunterladen, löschen) – eine Sicherung enthält alle Daten.
- Gespeicherte SMTP- und AD-Passwörter werden nur noch für denselben Server und dasselbe Konto weiterverwendet; ein
  geänderter Server bekommt das alte Passwort nicht mehr („Testmail an fremden Server“).
- 2FA-Codes mit Ziffern aus anderen Schriften (z. B. arabische Tastatur) werden angenommen statt Fehlerseite.
- Personensuche: keine Fehlerseite bei Trefferlisten an der Theke, kein Fehler wenn zwei gleichzeitig dieselbe
  AD-Person anlegen, Enter während einer laufenden Suche wählt nie einen veralteten Treffer, funktioniert auch mit
  OpenLDAP, findet bekannte Personen auch über neue Angaben im AD, Bildschirmleser-Unterstützung.
- Doku: Port-Bindung über `CATALYTIX_BIND` (die `docker-compose.yml` wird bei Updates ersetzt), Umfang der
  2FA-Bestätigung in README und Hilfe, Hilfe zur Ausleihe aktualisiert.

## 2.10.1 – 22.10.2026
- **Erscheinungsbild wurde nicht gespeichert:** Wer im internen Netz ohne zweiten Faktor angemeldet war, aber
  selbst 2FA hat, wurde seit 2.10.0 bei *jeder* Änderung in der Administration zur Sicherheitsseite umgeleitet –
  auch bei Farben, Logo, Gruppen oder E-Mail. Die Bestätigung gilt jetzt nur noch für sicherheitsrelevante
  Änderungen (Benutzer, Rollen, 2FA zurücksetzen, Sperren aufheben, Einstellungen, Active Directory). Dort führt
  sie direkt zur Code-Abfrage und danach zurück auf die Seite.

## 2.10.0 – 22.10.2026
Zwei vollständige Code Reviews (Sicherheit, Logik, Betrieb) – alle Befunde behoben, 112 neue Tests (insgesamt 367).

**Sicherheit**
- **Zwei-Faktor:** Eine Sitzung, die im internen Netz ohne zweiten Faktor begonnen wurde, kann keine Faktoren
  mehr anlegen, ersetzen oder entfernen, keine Wiederherstellungscodes erzeugen und in der Administration nichts
  ändern – erst nach „Zweiten Faktor bestätigen“. Vorher ließ sich so ein eigener Faktor für den Zugriff von
  außen unterschieben.
- **AD-Anmeldung** kann kein lokales Konto mit gleichem Namen mehr übernehmen (z. B. mit „DOMÄNE\name“).
- **Ersteinrichtung** ist nur noch bei einer leeren Datenbank möglich. Fehlt später jeder Admin, wird ein neuer
  nur auf dem Server angelegt (`create-admin`).
- **„Registrierung ablehnen“** wirkt nur noch auf wartende Registrierungen – nie auf Admins oder das eigene Konto.
- **Anmeldesperren:** Benutzernamen werden vereinheitlicht („DOMÄNE\anna“ = „anna“), die Sperre je Konto gilt
  auch im LAN (100 statt 40 Versuche), nach 10 falschen Codes beim zweiten Faktor ist das Passwort erneut nötig.
  Fremde können eine laufende Anmeldung nicht durch Fehlversuche beenden.
- **TOTP-Codes und Wiederherstellungscodes** gelten auch bei gleichzeitigen Anmeldungen garantiert nur einmal.
- **Internes Netz:** Ohne Eintrag zählen nur 10.x und 192.168.x – Docker-Netze (172.16–31.x) nur, wenn eingetragen.
  Neu: `CATALYTIX_BIND` (z. B. `127.0.0.1`), damit niemand den Reverse-Proxy umgehen kann; Hinweise im README.
- **Cover-Download** verbindet sich nur noch mit der geprüften Adresse (Schutz gegen DNS-Tricks auf interne
  Server), mit Gesamt-Zeitlimit; kaputte Bilder oder abbrechende Server führen nicht mehr zu Fehlerseiten.
- Ein gefälschter `X-Forwarded-Host` kann Links in Registrierungs-Mails nicht mehr umbiegen. Empfehlung: unter
  *E-Mail* die Basis-Adresse eintragen – dann stammen alle Links nur aus dieser Einstellung.
- Gruppen-Verwalter sehen an der Theke und in der ISBN-Prüfung nur noch ihre Gruppen.
- Wiederherstellungscodes liegen nicht mehr lesbar im Sitzungs-Cookie; Profil-E-Mail folgt der Domain-Regel.

**Ausleihe und Katalog**
- **Kein doppeltes Verleihen** mehr bei gleichzeitigen Klicks (Sperre + Datenbankregel), auch die Höchstzahl an
  Ausleihen und Verlängerungen hält jetzt immer.
- **Theke:** Reine Ziffern gelten als Signatur, die interne Buch-Nummer gibt man mit `#` ein (z. B. `#12`).
  Signaturen mit Umlauten werden gefunden.
- **Schneller:** Katalogfilter, CSV-Export und Ausleihliste brauchen bei 10.000 Büchern nur noch wenige
  Datenbankabfragen statt 10.000. Die Ausleihliste hat Seiten statt bei 500 Einträgen abzubrechen.
- Keine Fehlerseiten mehr bei ungewöhnlichen Eingaben (z. B. „X“ mitten in einer ISBN, „²“, riesige Zahlen).
- Blättern im Katalog behält den Standort-Filter; keine Erinnerung für zurückgegebene Bücher.

**Betrieb**
- **Startvorgang:** Beide Server-Prozesse starten nicht mehr gleichzeitig in die Datenbank-Einrichtung; der
  geheime Schlüssel wird garantiert nur einmal erzeugt.
- **Schlüssel-Prüfung:** Passt `secret_key` nicht zur Datenbank, startet Catalytix nicht (statt still alle
  2FA-Anmeldungen zu brechen) und meldet das klar im Protokoll. Dokumentiert in README und `.env.example`.
- **update.sh:** Sicherung vor dem Austausch; neues Paket wird erst geprüft und entpackt, dann ausgetauscht – bei
  Fehlern oder Abbruch wird der alte Stand wiederhergestellt. Übernimmt den Compose-Projektnamen, bricht ohne
  Terminal bei Rückfragen ab (sonst `-y`), Zip-Pfad funktioniert aus jedem Ordner. Weg zurück zur alten Version
  im README beschrieben.
- Befehle im Container laufen über `docker-entrypoint.sh` (nicht mehr als root – Sicherungen bleiben lesbar).
- Warnung, wenn `CATALYTIX_BACKUP_DIR` kein eingebundenes Laufwerk ist; Protokoll auf 3 × 10 MB begrenzt.
- E-Mail-Lauf wird nach einer SMTP-Störung alle 15 Minuten wiederholt; tägliche Übersicht nie doppelt.
- Datensicherung: keine Restdateien mehr, Sicherungen aus derselben Sekunde werden korrekt gelistet und rotiert.
- MySQL/MariaDB: sichere Isolationsstufe für das Ausleihen; Spaltennamen werden korrekt maskiert.
- Zeitzone wird geprüft – fehlt sie im Image, steht eine klare Warnung im Protokoll.

**Sprache**
- Standardname „Bibliothek“ erscheint auf Englisch als „Library“; Registrierungs-Mails in der Sprache der
  Registrierung; erste Meldung nach der Anmeldung in der Profilsprache; Pluralformen in der Hilfe korrigiert;
  das „?“ auf „Alle Ausleihen“ führt zum richtigen Thema.

## 2.9.1 – 21.10.2026
- Link zur Entwickler-Webseite [helfi.space](https://helfi.space) auf der Seite *Über dieses Programm*, in den
  READMEs und in beiden Handbüchern.

## 2.9.0 – 20.10.2026
- **Deutsch und Englisch:** Die gesamte Oberfläche, alle Meldungen, E-Mails und die Browser-Meldungen gibt es
  jetzt auch auf Englisch. Umschalten mit **DE / EN** oben rechts und auf der Anmeldeseite; angemeldet wird die
  Wahl im Profil gespeichert und gilt auch für E-Mails an diese Person. Ohne Wahl gilt die Browser-Sprache, sonst
  die neue **Standardsprache** (*Einstellungen → Allgemein*). Datumsangaben folgen der Sprache.
- **Hilfe im Programm**, rollenabhängig: Leser sehen 7 Themen, Verwalter 14, Administratoren 25. Das **?** oben
  rechts springt zur Hilfe der aktuellen Seite. Die Texte richten sich nach den Einstellungen der Bibliothek
  (Leihfrist, Selbstausleihe, Verlängerungen, Erinnerungen, Zwei-Faktor-Pflicht, AD-Konto …). Mit Suchfeld.
- **Handbücher im Paket:** `docs/Handbuch-Catalytix.pdf` und `docs/Manual-Catalytix.pdf` (neu, Englisch mit
  englischen Screenshots), in der Hilfe als Download verlinkt. Das deutsche Handbuch hat ein neues Kapitel
  „Sprache und Hilfe“ und beschreibt das Update mit `update.sh`.
- Kleinere Korrekturen: Der Knopf „Verlängern“ auf der Buchseite erscheint nur noch, wenn Verlängern auch erlaubt
  ist. Die Hilfe nennt die richtige Vorlaufzeit der Erinnerungen. Das Ergebnis des letzten Erinnerungslaufs wird in
  der Sprache des Betrachters angezeigt. „1 Monat“ statt „1 Monate“.
- Neue Tests: Vollständigkeit der Übersetzung, englische Seiten ohne deutsche Reste, Sprachwahl, E-Mail-Sprache,
  rollenabhängige Hilfe (insgesamt 254).

## 2.8.1 – 20.10.2026
- **Reproduzierbarer Build:** Basis-Image exakt festgelegt (`python:3.13.16-slim-trixie`, zusätzlich per Digest
  eingefroren). Alle Python-Pakete inklusive Unterabhängigkeiten und `pip` selbst auf eine feste Version gesetzt
  (`requirements.txt`); Installation ohne Nachziehen weiterer Pakete, nur aus fertigen Paketen, mit
  abschließender Prüfung. Direkte Abhängigkeiten mit erlaubten Bereichen in `requirements.in`.
- Aktualisiert auf: SQLAlchemy 2.1.4, gunicorn 26.2.0, WTForms 3.2.2, Flask-WTF 1.3.0, PyMySQL 1.2.3, pyasn1 0.6.4.
  Alle 237 Tests laufen mit genau diesen Versionen.
- **`update.sh`:** Neubau und Update mit einem Befehl – Paket einspielen (persönliche Dateien bleiben),
  Datensicherung, Image bauen, Neustart, Warten auf „gesund“, auf Wunsch alte Images aufräumen.
  Funktioniert mit `./update.sh`, `bash update.sh` und `sh update.sh`.

## 2.8.0 – 20.10.2026
Sicherheits- und Fehlerprüfung des gesamten Programms.

**Sicherheit**
- **Abmelden auf allen Geräten** (*Profil → Sicherheit*). Passwortänderung und das Zurücksetzen der
  Zwei-Faktor-Anmeldung beenden automatisch alle anderen Sitzungen – auch „Angemeldet bleiben“-Cookies.
  Bisher blieb ein gestohlenes Gerät nach einer Passwortänderung angemeldet.
- **AD-Konten werden nachgeprüft:** alle 4 Stunden je Sitzung (über das Dienstkonto). Im AD deaktivierte,
  gelöschte oder nicht mehr berechtigte Konten werden abgemeldet, Rollenänderungen übernommen. Bei AD-Ausfall
  bleibt man angemeldet.
- **Mindestlänge für Passwörter** auf 10 Zeichen erhöht (gilt für neue/geänderte Passwörter).
- **Zugangsdaten verschlüsselt:** AD-Dienstkonto- und SMTP-Passwort liegen nicht mehr im Klartext in der
  Datenbank; vorhandene Einträge werden beim Start automatisch umgestellt.
- **Content-Security-Policy:** Alle eingebetteten Skripte entfernt, der Browser führt nur noch Skripte vom eigenen
  Server aus. Das blockiert eingeschleusten Code auch dann, wenn irgendwo eine Lücke wäre.
- **Weiterleitungen** nach Admin-Aktionen und Ausleihen nur noch auf eigene Seiten (auch nicht über
  Tab/Zeilenumbruch-Tricks).
- **Von außen** zusätzliche Bremse je Benutzername gegen verteiltes Passwort-Raten. Die Antwortzeit verrät nicht
  mehr, ob ein Benutzername existiert.
- **Cover-Uploads** werden geprüft und neu kodiert (keine getarnten Dateien), Cover anderer Gruppen sind nur
  sichtbar, wenn das Buch sichtbar ist.
- **CSV-Export:** Schutz vor Formel-Einschleusung in Excel.
- **E-Mail:** Empfängeradressen werden streng geprüft (keine Listen mit Komma), Betreffzeilen ohne
  Zeilenumbrüche – kein Einschleusen weiterer Empfänger.

**Fehler und Logik**
- Einstellungen mit Obergrenzen (Leihfrist, Verlängerungen, Ports, Uhrzeiten …) – unsinnige Werte führten
  zu Fehlerseiten.
- Fälligkeitsdatum beim Ausleihen muss zwischen heute und in zehn Jahren liegen.
- Theke: ungültige Formularwerte geben „nicht gefunden“ statt eines Serverfehlers.
- Gruppenfarben werden geprüft; Basis-Adresse für E-Mail-Links muss mit http(s) beginnen.
- Fehlerhafte Kommentare in den Standardeinstellungen korrigiert.
- 27 neue automatische Tests (insgesamt 237).

## 2.7.0 – 20.10.2026
- **Standorte festlegen** (*Verwaltung → Standorte*, für Verwalter und Admins): Liste der Regale/Räume, mehrere
  auf einmal anlegbar, natürlich sortiert („Regal 2“ vor „Regal 10“), optionale Notiz.
- Beim **Erfassen, in der Schnellerfassung und beim Bearbeiten** wird der Standort aus der Liste gewählt statt
  getippt; Groß-/Kleinschreibung und Leerzeichen werden angeglichen, unbekannte Standorte abgelehnt. Ein schon
  gespeicherter, nicht gelisteter Standort bleibt beim Bearbeiten erhalten.
- **Umbenennen** stellt alle betroffenen Bücher mit um; **Löschen** nur, wenn der Standort leer ist.
- **Altbestand aufräumen:** frei getippte Standorte übernehmen oder durch einen festgelegten ersetzen; bei
  Schreibvarianten und Tippfehlern wird der passende Standort vorgeschlagen.
- Gruppen-Verwalter dürfen keine Bücher anderer Gruppen mit umstellen.
- **Katalog:** Filter nach Standort.

## 2.6.0 – 20.10.2026
- **Eigener Internetanschluss als intern** (*Einstellungen → Zwei-Faktor-Anmeldung*): feste öffentliche IP oder
  DynDNS-Name eintragen. Interne Rechner, die die öffentliche Domain aufrufen und vom Router mit der eigenen
  öffentlichen IP ausgegeben werden, gelten damit als intern. DynDNS-Namen werden alle 5 Minuten neu aufgelöst
  (bei DNS-Ausfall gilt die letzte Adresse noch bis zu einer Stunde); die aktuell aufgelöste Adresse wird angezeigt.
- Hinweis in der Oberfläche: Auch das Gäste-WLAN kommt über diese Adresse und gilt dann als intern.

## 2.5.0 – 20.10.2026
- **Zwei-Faktor je nach Netz** (*Einstellungen → Zwei-Faktor-Anmeldung*): Pflicht für Zugriffe von außen wie bisher
  einstellbar; zusätzlich **„Im internen Netz“**: wie von außen (Standard) / keine Pflicht / keine Zwei-Faktor-Abfrage.
  - **Internes Netz** frei festlegbar (eine Netzadresse pro Zeile; leer = private Netze).
  - Hinter dem Reverse-Proxy zählt die echte Besucher-Adresse (`X-Forwarded-For`); fehlt sie, gilt der Zugriff als extern.
    Gefälschte Angaben werden ignoriert.
  - Eine intern ohne zweiten Faktor begonnene Sitzung gilt nicht von außen (neue Anmeldung nötig); „Angemeldet
    bleiben“ ist dann wirkungslos.
  - Die Einstellungsseite zeigt, ob die eigene Verbindung als intern oder extern gilt.
- **Konten ohne Kommandozeile entsperren:** neuer Bereich *Anmeldung und Sicherheit* im Benutzerkonto mit
  „Zwei-Faktor-Anmeldung zurücksetzen“ und „Anmeldesperre aufheben“; Markierung und Filter „Anmeldesperre“ in der
  Benutzerliste. Das Zurücksetzen hebt auch eine bestehende Sperre auf.
- Anmeldesperre nach Fehlversuchen liegt jetzt in der Datenbank – gilt damit für alle Server-Prozesse gemeinsam
  und lässt sich zuverlässig aufheben.

## 2.4.0 – 19.10.2026
- **Automatische Datensicherung** (*Administration → Datensicherung*): täglich ab einstellbarer Uhrzeit (Standard 2 Uhr),
  im laufenden Betrieb, mit Integritätsprüfung jeder Sicherung. Aufbewahrt werden die **neuesten 10** (einstellbar 1–100).
  „Jetzt sichern“, Herunterladen, Löschen; Warnzeichen im Menü und E-Mail an Admins, wenn eine Sicherung fehlschlägt.
  Ablage `./data/backups` oder `CATALYTIX_BACKUP_DIR` (z. B. NAS).
- Befehle `flask backup`, `flask list-backups`, `flask restore-backup <datei>` (sichert vorher den aktuellen Stand).
- **Alter Projektname vollständig entfernt** – aus Code, Oberfläche, Dokumentation und Kompatibilitätsschichten:
  - Datenbankdatei heißt nur noch `catalytix.db`. Liegt stattdessen eine andere `.db`-Datei im Datenordner,
    startet das Programm nicht (Schutz vor einem leeren Neustart) und nennt den Umbenennungsbefehl.
  - Alte Variablennamen in der `.env` werden nicht mehr gelesen – nur noch `CATALYTIX_…`.
  - Neuer interner Schlüssel für Authenticator-Apps: bereits eingerichtete Apps müssen einmal neu eingerichtet werden.
    Betroffene bekommen bei der Anmeldung einen klaren Hinweis (Anmeldung per Wiederherstellungscode oder
    Sicherheitsschlüssel, dann *Sicherheit → App neu einrichten*; oder Admin setzt 2FA zurück).
    Sicherheitsschlüssel (YubiKey) funktionieren unverändert.

## 2.3.1 – 18.10.2026
- `docker-compose.yml`: Label `dockhand.update=false`, damit Dockhand den Container nicht automatisch
  aktualisiert (das Image wird lokal gebaut; Updates kommen per neuem Paket).

## 2.3.0 – 17.10.2026
- **Kein Programmname mehr in der Oberfläche.** Anmeldeseite, Seitenleiste, E-Mails, Authenticator-App und
  Browser-Tab zeigen nur noch den **Namen der Bibliothek** (*Einstellungen*). „Catalytix“ bleibt Projektname
  (GitHub, README, Docker-Image) und steht nur noch auf der Seite *Über dieses Programm* (Lizenz/Quellcode).
- Lange Bibliotheksnamen werden in der Seitenleiste umbrochen statt abgeschnitten; auf dem Handy steht der Name
  jetzt über der Anmeldung.
- Alter Standardname „CATALYTIX“ wird zu „Bibliothek“; eigene Namen bleiben.
- Erscheinungsbild: Option heißt jetzt „Namen der Bibliothek neben dem Logo anzeigen“.

## 2.2.0 – 17.10.2026
- **Automatische Aufbewahrungsfrist** (DSGVO, Speicherbegrenzung) unter *Einstellungen → Aufbewahrung*:
  abgeschlossene Ausleihen werden nach 3/6/**12**/24/36 Monaten ab Rückgabe gelöscht (Standard 12, „nie“ möglich).
  Pro Buch bleibt die Anzahl erhalten („x Vorgänge · y ältere ohne Einzeldaten“).
- Einträge im E-Mail-Protokoll werden nach derselben Frist gelöscht – außer Einträge zu laufenden Ausleihen
  (daran hängt, wie oft schon gemahnt wurde).
- Laufende und überfällige Ausleihen werden nie gelöscht.
- Läuft einmal täglich automatisch (auch ohne eingerichteten E-Mail-Versand), zusätzlich „Jetzt bereinigen“ und
  Befehl `flask apply-retention`. Ergebnis des letzten Laufs in den Einstellungen.
- Profil: Hinweis, nach welcher Zeit der eigene Ausleihverlauf gelöscht wird.
- Beim Löschen eines Benutzers bleibt die Ausleihzahl der Bücher erhalten.
- Datenbank erhält die Spalte `books.archived_loans` automatisch beim Start.

## 2.1.0 – 17.10.2026
Datenschutz für den Betrieb im Internet – die Browser der Nutzer verbinden sich nur noch mit dem eigenen Server:
- **Schriften vom eigenen Server** statt von Google Fonts (Inter und Fraunces mitgeliefert, SIL Open Font License).
- **Eigene Barcode-Erkennung** für Firefox und Safari statt der von unpkg.com nachgeladenen ZXing-Bibliothek
  (Chrome, Edge und Android nutzen weiter die eingebaute Erkennung des Browsers). Ein Code wird erst übernommen,
  wenn er zweimal hintereinander gleich gelesen wurde.
- **Coverbilder immer über den eigenen Server:** Bücher ohne lokal gespeichertes Cover laden es beim ersten Aufruf
  serverseitig nach; die Vorschau beim Erfassen läuft ebenfalls über den Server. Nur öffentliche Adressen werden
  abgerufen (Schutz interner Server), nur JPG/PNG/WebP/GIF. Die Einstellung „Cover lokal speichern“ entfällt.
- **Impressum und Datenschutzerklärung** unter *Einstellungen → Rechtliches*; Links auf der Anmeldeseite,
  in der Seitenleiste und in allen E-Mails. Die bisherige Datenschutz-Adresse der Selbstregistrierung wird übernommen.
- README: neuer Abschnitt „Datenschutz und Betrieb im Internet“.

## 2.0.0 – 17.10.2026
- **Projektname Catalytix** – in Oberfläche, E-Mails, Docker-Image (`catalytix:2.0.0`) und Python-Paket (`catalytix`).
- **Freie Software unter der GNU AGPL v3 oder später** (Datei `LICENSE`).
- Neue Seite **„Über Catalytix“** (Klick auf die Versionsnummer in der Seitenleiste): Version, Lizenz, Lizenztext
  und Link zum Quellcode (`CATALYTIX_SOURCE_URL`). Nur nach der Anmeldung sichtbar.
- Docker-Dienst heißt jetzt `catalytix` – vor dem Update `docker compose down` ausführen (siehe README).
- Englische README (`README.en.md`).

## 1.6.0 – 17.10.2026
- **Selbstregistrierung** (standardmäßig aus) unter *Einstellungen*: aus / mit Freigabe durch Admin / sofort aktiv.
  - optional E-Mail-Bestätigung (Link, 48 h), Benachrichtigung der Admins, erlaubte E-Mail-Domains,
    Standardgruppe für neue Leser, Pflicht-Bestätigung der Datenschutzhinweise.
  - Neue Konten sind immer Leser. Freigeben/Ablehnen direkt in der Benutzerliste; Hinweis auf der Übersicht
    und Zähler in der Navigation.
  - Schutz: Bot-Falle, Mindest-Ausfüllzeit, Begrenzung pro IP, reservierte Namen, AD-Benutzernamen gesperrt,
    Passwort mind. 10 Zeichen; unbestätigte Konten werden nach 7 Tagen gelöscht.
- Neue Mails: E-Mail bestätigen, Konto freigeschaltet, neue Registrierung (an Admins).
- Benutzerliste: Filter nach Status (wartet auf Freigabe, selbst registriert, gesperrt).

## 1.5.0 – 17.10.2026
- **Erscheinungsbild / Corporate Design** (*Administration → Erscheinungsbild*) mit Live-Vorschau:
  - eigenes **Logo** (PNG/JPG/WebP/GIF/SVG) in Seitenleiste, Anmeldeseite (auch mobil) und E-Mails; Favicon wird
    automatisch erzeugt; optional ohne Schriftzug und auf hellem Feld.
  - eigenes **Hintergrundbild** für die Anmeldeseite mit einstellbarer Abdunkelung; Bilder werden verkleinert,
    Metadaten (GPS) entfernt.
  - eigene **Farben** (Haupt-, Akzent-, Seitenleistenfarbe) mit automatisch lesbaren Schriftfarben, auch im Dunkelmodus.
  - eigene **Begrüßungstexte**, Bücherregal-Grafik abschaltbar, „Alles zurücksetzen“.
- E-Mails übernehmen Logo und Farben.
- Sicherheit: SVG-Logos mit Skripten/externen Inhalten werden abgelehnt und nur mit strikter CSP ausgeliefert.
- Neue Abhängigkeit: `Pillow` (Bildbearbeitung).

## 1.4.1 – 17.10.2026
- Seitenleiste: neue Einträge **„Mein Profil“** und **„Sicherheit“** für alle Benutzer (vorher nur über das
  kleine Namenskürzel unten links bzw. das Handy-Menü erreichbar). Hinweis „2FA aus“, solange kein zweiter Faktor aktiv ist.
- Name unten links ist jetzt ebenfalls ein Link zum Profil.

## 1.4.0 – 17.10.2026
- **Zwei-Faktor-Anmeldung**
  - Authenticator-App (TOTP, RFC 6238) mit QR-Code; Geheimnisse verschlüsselt gespeichert, Codes nicht wiederverwendbar.
  - **Sicherheitsschlüssel** (YubiKey & andere FIDO2-Schlüssel, Passkeys) per WebAuthn; mehrere Schlüssel je Konto.
  - 10 Wiederherstellungscodes (Einmal-Codes), neu erzeugbar.
  - Pflicht einstellbar: niemand / Administratoren / Verwalter + Admins / alle – mit geführter Einrichtung.
  - Admin kann den zweiten Faktor eines Benutzers zurücksetzen; Notfall-Befehl `flask reset-2fa`.
  - Fehlversuch-Sperre auch für den zweiten Schritt; Anmeldeschritt läuft nach 5 Minuten ab.
- Neue Seite *Profil → Sicherheit*; 2FA-Kennzeichen in der Benutzerliste.
- Neue Abhängigkeiten: `fido2` (Yubico), `segno` (QR-Codes).

## 1.3.3 – 17.10.2026
- **Abmelden auf dem Smartphone:** Neues Benutzermenü oben rechts (Profil, Hell/Dunkel, Abmelden).
- Fehler behoben: Das ausklappbare Menü war auf Handys höher als der sichtbare Bereich (Adressleiste),
  dadurch lag der Abmelde-Bereich unten außerhalb des Bildschirms.
- „+ Buch“-Knopf in der Kopfzeile auf dem Handy platzsparend nur als Symbol.

## 1.3.2 – 16.10.2026
- **Reverse-Proxy wird automatisch erkannt** – keine Einstellung mehr nötig. Proxy-Header werden nur aus
  privaten Netzen akzeptiert (von außen nicht fälschbar); anpassbar über `CATALYTIX_TRUSTED_PROXIES`.
- **Secure-Cookies automatisch** bei HTTPS; Anmeldung per `http://IP:9300` im LAN funktioniert weiterhin.
- **`docker-compose.yml` muss nicht mehr bearbeitet werden.** Eigene Werte (Port, Zeitzone, PUID …) stehen
  in `.env` (Vorlage `.env.example`), die bei Updates nie überschrieben wird. Erweiterungen in
  `docker-compose.override.yml`.
- Hinweis: benötigt Docker Compose 2.24 oder neuer (wegen optionaler `.env`).

## 1.3.1 – 16.10.2026
Anmeldeseite für den Betrieb im Internet (hinter Reverse-Proxy) neutraler gestaltet:
- Hinweis „Anmeldung über Active Directory ist aktiv“ und Domänen-Hinweise entfernt.
- Neutraler Begrüßungstext statt Funktionsbeschreibung.
- Keine Versionsnummer mehr auf der Anmeldeseite und in öffentlich sichtbaren Datei-Links
  (nach dem Login weiterhin unten links in der Seitenleiste).
- Einheitliche Fehlermeldung bei der Anmeldung: Von außen ist nicht mehr erkennbar, ob ein Konto
  existiert, gesperrt oder nicht freigegeben ist (Details stehen weiterhin im Server-Log).

## 1.3.0 – 16.10.2026
- **E-Mail-Benachrichtigungen per SMTP** (STARTTLS, SSL/TLS oder unverschlüsselt, mit/ohne Anmeldung):
  Bestätigung bei Ausleihe und Rückgabe, Erinnerung vor Fälligkeit, Mahnungen mit einstellbarer
  Wiederholung und Höchstzahl, tägliche Übersicht überfälliger Ausleihen an Verwalter.
- Täglicher Versand zur eingestellten Uhrzeit; mehrere Bücher pro Leser in einer Mail; kein Doppelversand
  (auch bei mehreren Server-Prozessen). „Jetzt versenden“-Knopf und Befehl `flask send-reminders`.
- Neue Seite *Administration → E-Mail* mit Testmail und Versandprotokoll.
- Manuelle Erinnerung per Klick bei jeder Ausleihe (*Alle Ausleihen*).
- Benutzer können Benachrichtigungen im Profil abschalten; lokale Benutzer pflegen dort ihre Adresse.
- Datenbank wird beim Start automatisch um neue Spalten ergänzt – Update ohne Datenverlust.
- Fehler behoben: Vorlagen ließen sich außerhalb einer Web-Anfrage (Hintergrundversand) nicht rendern.

## 1.2.1 – 16.10.2026
Überarbeitung der Gruppenauflösung im Active Directory:
- **Primäre Gruppe wird erkannt** (z. B. *Domänen-Benutzer*). AD führt sie nicht in `memberOf`,
  deshalb wurde sie bisher übersehen – auch Gruppen, die sie enthalten, fehlten.
- **Gruppen außerhalb der Benutzer-OU werden gefunden.** Die Suche läuft jetzt ab der Domänenwurzel
  (`DC=…`) statt nur unterhalb der Basis-DN.
- **Verschachtelte Gruppen über zwei Wege:** `LDAP_MATCHING_RULE_IN_CHAIN` und zusätzlich `tokenGroups`
  (die Gruppen aus dem Windows-Anmeldetoken). Fällt ein Weg aus (ältere Samba-Version, eingeschränkte
  Rechte), greift der andere – vorher gingen verschachtelte Gruppen dann stillschweigend verloren.
- Gruppen werden mit der Verbindung des angemeldeten Benutzers gelesen (darf sein eigenes `tokenGroups`
  immer lesen); Dienstkonto als Rückfall.
- OpenLDAP / klassische Samba-Domänen: verschachtelte `groupOfNames`-Gruppen werden jetzt ebenfalls aufgelöst.
- **„Login testen“ zeigt pro Gruppe die Herkunft:** direkt, primär oder verschachtelt.
- „Gruppen laden“ sucht ebenfalls ab der Domänenwurzel.

## 1.2.0 – 16.10.2026
- **Rollen über AD-Gruppen:** Administratoren, Verwalter und Leser werden jetzt einfach über den
  **Namen einer AD-Gruppe** festgelegt (z. B. `Bibliothek-Admins`) – keine eigene OU mehr nötig.
  Gruppen-DNs und OUs funktionieren weiterhin, bestehende Einstellungen bleiben gültig.
- **Gruppen aus dem AD auswählen:** Auf der Seite *Active Directory* lassen sich die Gruppen des AD
  laden, filtern und per Klick als Admin, Verwalter oder Leser übernehmen.
- Bibliotheksgruppen lassen sich ebenfalls per Gruppenname (statt DN) mit einer AD-Gruppe verknüpfen.
- Versionsnummer in der Oberfläche, im Docker-Image (`catalytix:1.2.0`) und im Dateinamen des Pakets.

## 1.1.0
- **TLS-Zertifikat automatisch vom Domänencontroller holen** (LDAPS/StartTLS), inkl. Ergänzung der
  Stamm-CA über AIA oder aus dem AD; Anzeige des Fingerabdrucks und Bestätigung durch den Admin.
- Option „Hostname prüfen“; verständliche Fehlermeldung bei nicht vertrauenswürdigem Zertifikat.
- Docker-Image auf Python 3.13; neue Abhängigkeit `cryptography`.

## 1.0.2
- Docker: Rechte am Datenordner werden beim Start automatisch gesetzt (`PUID`/`PGID`), App läuft nicht als root.
- Klare Fehlermeldung, wenn das Datenverzeichnis nicht beschreibbar ist.

## 1.0.1
- Standard-Port auf **9300** geändert.

## 1.0.0
- Erste Version: Katalog, ISBN-Erfassung (Scanner, Kamera, DNB/Open Library/Google Books),
  Schnellerfassung, Ausleihe und Ausleihtheke, Gruppen und Rollen, lokale Benutzer,
  Active-Directory-Anbindung, SQLite (optional MySQL/PostgreSQL), Docker.
