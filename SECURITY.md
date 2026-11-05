# Security / Sicherheit

[English](#english) · [Deutsch](#deutsch)

## English

### Reporting a vulnerability
Please **do not open a public issue** for security problems. Report them confidentially through GitHub instead:

**[Report a vulnerability](https://github.com/soleil0815/catalytix/security/advisories/new)**
(tab *Security → Report a vulnerability*). Only the maintainer sees the report until a fix is published.

Helpful in a report:
- affected version (shown at the bottom left of the sidebar, or `docker inspect` label `org.opencontainers.image.version`),
- what an attacker can do and which role they need (not signed in, reader, librarian, admin),
- steps to reproduce, ideally with a minimal example – please use test data only, never real accounts or passwords,
- relevant settings (Active Directory, reverse proxy, two-factor rules) if they matter.

What happens next: you get an answer as soon as possible. Confirmed issues are fixed in a new version; the fix is
described in the change log and, if you like, credited to you. Please give us reasonable time to publish the fix
before you disclose details.

### Supported versions
Only the **newest release** (image tag `latest`) gets fixes, including its weekly security updates (`X.Y.Z-sN`,
same program with updated packages). Please update before reporting – the issue may already be fixed
([change log](CHANGELOG.en.md)).

### Scope
In scope: the program in this repository and the published image `ghcr.io/soleil0815/catalytix`, including
`update.sh` and `docker-compose.yml`.

Not in scope: findings that need admin access to the server or the `data/` folder, missing hardening without a
concrete attack, problems of your own reverse proxy, mail server or directory, and denial of service through
large amounts of requests. Known package findings in the image are handled automatically (daily Trivy scan, weekly
security update) – report them only if a fix is available and not yet included.

## Deutsch

### Sicherheitslücke melden
Bitte für Sicherheitsprobleme **kein öffentliches Issue anlegen**, sondern vertraulich über GitHub melden:

**[Sicherheitslücke melden](https://github.com/soleil0815/catalytix/security/advisories/new)**
(Reiter *Security → Report a vulnerability*). Die Meldung sieht nur der Betreuer, bis eine Korrektur veröffentlicht ist.

Hilfreich in der Meldung:
- betroffene Version (steht unten links in der Seitenleiste, oder Label `org.opencontainers.image.version` bei
  `docker inspect`),
- was ein Angreifer tun kann und welche Rolle er dafür braucht (nicht angemeldet, Leser, Verwalter, Admin),
- Schritte zum Nachvollziehen, am besten mit einem kleinen Beispiel – bitte nur Testdaten, nie echte Konten oder
  Passwörter,
- wichtige Einstellungen (Active Directory, Reverse-Proxy, Zwei-Faktor-Regeln), falls sie eine Rolle spielen.

Wie es weitergeht: Du bekommst so bald wie möglich eine Antwort. Bestätigte Lücken werden in einer neuen Version
behoben; die Korrektur steht im CHANGELOG, auf Wunsch mit deinem Namen. Bitte gib uns angemessen Zeit, die Korrektur
zu veröffentlichen, bevor du Einzelheiten bekannt machst.

### Unterstützte Versionen
Korrekturen gibt es nur für die **neueste Version** (Image-Tag `latest`) samt ihren wöchentlichen Sicherheitsupdates
(`X.Y.Z-sN`, gleiches Programm mit aktualisierten Paketen). Bitte vor einer Meldung aktualisieren – vielleicht ist
das Problem schon behoben ([CHANGELOG](CHANGELOG.md)).

### Umfang
Dazu gehören: das Programm in diesem Repository und das veröffentlichte Image `ghcr.io/soleil0815/catalytix`,
einschließlich `update.sh` und `docker-compose.yml`.

Nicht dazu gehören: Funde, die Admin-Zugriff auf den Server oder den Ordner `data/` voraussetzen, fehlende Härtung
ohne konkreten Angriff, Probleme des eigenen Reverse-Proxys, Mailservers oder Verzeichnisdienstes sowie
Überlastung durch sehr viele Anfragen. Bekannte Paket-Funde im Image werden automatisch behandelt (täglicher
Trivy-Scan, wöchentliches Sicherheitsupdate) – bitte nur melden, wenn es eine Korrektur gibt, die noch fehlt.
