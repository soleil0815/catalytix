# Catalytix changes

The current version is shown at the bottom left of the sidebar after signing in. German version: [CHANGELOG.md](CHANGELOG.md).

## 3.1.0 – 2026-11-06
**Changed**
- The German interface now addresses users formally ("Sie") instead of informally ("du") – on all pages and messages,
  in the e-mails, the help, the news, the manual (PDF) and the README. The English interface is unchanged. A test
  keeps informal forms from coming back.
- If the login page's welcome text was saved unchanged, it also appears in the new form.
- The German manual now has a source in the repository (`docs/manual/`, built with `python3 tools/build_manual.py`)
  and describes the automatic sign-out.

## 3.0.9 – 2026-11-06
**Internal (fewer GitHub Actions minutes, same checks)**
- No CI run after merging into `main` any more – every pull request is already checked, and `main` is built, tested
  and scanned again in full when publishing. Nothing untested is ever published.
- When publishing, the check job no longer builds, tests and scans the image in addition – the publish job does that
  anyway, with exactly the images that are published.
- Weekly security update: build only amd64 first and compare; the slow (emulated) arm64 build only runs when
  something changed.
- Setting up arm64 emulation without rebuilding the man page index (took up to 7 minutes).
- Self-tests: passwords are hashed with a fast method there (still scrypt in operation) – the tests run in a little
  over half the time.

## 3.0.8 – 2026-11-06
**New**
- Automatic sign-out after a time without activity (*Administration → Settings → Automatic sign-out*): one limit
  for everyone (default 12 hours, start value from `CATALYTIX_SESSION_HOURS` in `.env`), an own, shorter one for
  admins (default 60 minutes) and optionally one for sign-ins from outside. The shortest applicable limit applies.
  Before, a sign-in lasted as long as the browser stayed open.
- Optional: two minutes before, "You will be signed out in 2 minutes – keep working?" appears (default: on). The
  server measures the time; activity in another tab counts too.
- Optional: "Sign out when the browser is closed" – the sign-in does not survive a browser restart, "Stay signed in"
  is not offered.

**Changed**
- "Stay signed in" keeps the sign-in across a browser restart, but only as long as the limit without activity has not
  passed (before: a flat 14 days). Without it, the sign-in ends with the browser as before. The option is only
  available where no shorter limit applies – so no longer for admins, and not from outside when an own limit is set
  there. Existing "stay signed in" sign-ins of admins end once with this update.

## 3.0.7 – 2026-11-05
**Security**
- Slimmer image: what the running program never uses is removed at the end of the build – the package manager
  (`apt`), Perl, user and login tools (`passwd`, `login`, PAM), `tar`, `gzip`, `mount` and more, 32 of the previous
  87 Debian packages. Fewer packages mean fewer vulnerability warnings that do not concern the program, and an
  intruder cannot install software in the container. The package database is kept, so security scanners still see
  exactly what is inside (`docker/slim.sh`).
- The CI run now also starts the finished image like a server does (entrypoint, switch to user 1000, login page
  answers) and checks that the package manager and Perl are gone and Python finds the CA certificates.

## 3.0.6 – 2026-11-05
**New**
- "Buy me a coffee" button at the bottom of the sidebar – visible to admins only, opens the developer's support
  page in a new tab. Readers and librarians don't see it.

**Security**
- Debian packages at the state of 2026-11-05: includes `apt` 3.0.4 (CVE-2026-109518, HIGH). The temporary exception
  in `.trivyignore` from 3.0.3 is removed again.

## 3.0.5 – 2026-11-05
**Accessibility**
- Every form field now has a name for screen readers: about 100 labels are linked to their field (clicking the label
  also puts the cursor into the field), search and filter fields without a visible label have an invisible one. A
  test renders the pages with forms and checks every field.

## 3.0.4 – 2026-11-05
**Security**
- Active Directory: a bare group name for the administrator and librarian roles (e.g. `Bibliothek-Admins`) matched
  every group of that name in the whole directory. Whoever may create groups somewhere (e.g. teachers in their OU)
  could give themselves rights that way. When saving, such a name is now turned into the DN of exactly that group
  (with a service account). If the name exists more than once or not at all, nothing is saved. Without a service
  account, with AD switched off or not reachable, the name stays. Existing settings keep working; the *Active
  Directory* page points out bare names until they have been converted.

## 3.0.3 – 2026-11-05
**Security**
- CVE-2026-109518 in apt (HIGH): temporarily in `.trivyignore` (until 2026-11-19). apt is only used while building
  the image, the running program never calls it. The fix (apt 3.0.4) is not yet in the Debian archive state the build
  uses; the weekly security update takes it.

**Improved (update path and build chain)**
- Every image carries its exact tag (`catalytix.tag`, e.g. `3.0.3-s1`) and the commit it was built from
  (`org.opencontainers.image.revision`; for a security update the commit of the underlying release). The weekly
  security update chooses the tag before the build for that.
- After a failed start, update.sh goes back to exactly the tag of the image that ran before – also when it was a
  security update fetched only as `latest` (before, it became the version without the fixes). For older images
  without this information, update.sh says so.
- The GitHub release always points to the commit the published image was built from and contains `update.sh`,
  `docker-compose.yml` and `env.example` from the image itself – also when the release is only created in a later run.
- Build and security update now limit concurrent runs per workflow instead of per job (a shared job group let GitHub
  cancel waiting jobs, e.g. the release after the image was published). `latest` is never moved to a lower version –
  checked against the releases and against the image `latest` currently points to.
- The 7-day cool-down now applies to every new or changed package version – also to packages that come in as a new
  dependency, and to freshly uploaded older versions. If a chosen version is too young, the security update takes the
  next older one instead of giving up.

## 3.0.2 – 2026-11-05
**Security**
- Confirmation links for a new e-mail address (self-registered accounts) are only valid while the account still has
  the address the change was requested from: after one change is confirmed, all older links are invalid, and no
  link works twice (links sent before the update must be requested again). Before, e.g. a link sent to a mistyped address could still switch the account to that address
  for 48 hours. The allowed domains are checked again on confirmation.
- Imprint and privacy link: values like `//other-site.example` (or with spaces/control characters) are no longer stored. In e-mails, relative paths
  (`/datenschutz`) now become a full address based on the configured program address.
- Group-bound librarians get "not found" (404) instead of "forbidden" (403) for books of other groups they may not
  see – so they can no longer tell which book numbers exist there.
- Covers are only cached as "private", no longer in shared proxies.

**Fixed**
- Update: if a version change failed and the old version ran again afterwards, the next update attempt made no new
  backup before the change (the old one from the first attempt still counted). From 3.0.2 on, every successful start
  resets the "backup already made" marker (so it applies when the version you go back to is 3.0.2 or newer).
- Quick capture: if the same ISBN is scanned twice (e.g. quickly in auto mode), the server adds the copies to the
  existing book of the same group instead of creating a second book.
- Deleting a user: if the account does have an ongoing loan at that moment, it is not deleted (before, the loan
  could have been deleted with it).
- update.sh:
  - checks that `.env` is writable before changing anything – before, a downgrade with a read-only `.env` could
    stop with the container stopped and the database already put back;
  - reads and writes `.env` like Docker Compose (`export CATALYTIX_TAG=…`, comments after the value);
  - stops without changes when `CATALYTIX_TAG` or `CATALYTIX_IMAGE` is set in the shell (Compose would otherwise
    start another image), and no longer reports success when another image than the one fetched runs anyway;
  - removes "orphan" containers (`--remove-orphans`) only in the Compose project of this folder – no longer in
    another stack whose name the container carries;
  - removes the temporary helper container together with its anonymous volume;
  - `--cleanup` stops when a file name with a line break is in the data folder (a link with such a name would not
    have been recognised as protected).
- Build: the package list in the image stops the build on an error instead of staying incomplete;
  `highest_release` no longer treats an unreadable release list as "no version yet";
  `tools/security_update.py changelog` only writes when both CHANGELOGs exist.
- Two-factor sign-in: with the unchanged library name, authenticator apps and passkeys show the name in the
  interface language ("Library" instead of "Bibliothek" in English).

**Interface and accessibility**
- Phone: names in the loans list no longer wrap letter by letter.
- The closed sidebar on phones can no longer be reached with the Tab key; the menu button reports whether it is open.
- "+ Book" (top bar on phones) and the camera buttons at the circulation desk have a name for screen readers.
- More contrast for grey hint texts, table headers and brass text (WCAG AA).

**Quality**
- Type checking: `dbversion.py`, `composefile.py` and `lockout.py` are now checked strictly. Remaining findings: 58.
- Docs: `.env.example` lists `CATALYTIX_COVER_VIA_PROXY`; the English README correctly says the interface is
  bilingual.

## 3.0.1-s1 – 2026-11-02 (security update)
Same program as 3.0.1, only updated packages (weekly security update, built, tested and scanned with Trivy automatically):
- Base image: `python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 → python:3.13.18-slim-trixie@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1`
- Python: `3.13.17 → 3.13.18`
- Debian packages: `tzdata 2026e-0+deb13u1 → 2026f-0+deb13u1`
- Python packages: `certifi 2026.10.19 → 2026.10.23`, `pycparser 3.11 → 3.12`, `sqlalchemy 2.1.5 → 2.1.6`, `wtforms 3.2.2 → 3.3.1`

## 3.0.1 – 2026-11-01
**Fixed**
- Active Directory: If the domain controller rejected the service account's bind (e.g. expired password), sign-in
  aborted with an internal error instead of showing “Bind refused (…)” with the reason. Cause: placeholders such as
  `{msg}` in translated texts collided with the parameter names of the translation functions – the parameters are now
  positional only, so such placeholders work everywhere.

**Security**
- pyasn1 0.6.4 → 0.6.5 (four HIGH vulnerabilities reported by Trivy; needed for AD sign-in). Deliberately adopted
  before the 7-day waiting period ended.

**Quality**
- Found by type checking, no effect in operation: the search for a matching backup (`find_for_version`) now checks an
  invalid version number itself (previously only the command before it did); `tools/lock.py` and
  `tools/security_update.py` report unreadable entries with a clear message instead of a program error.
- Type checking with mypy, step by step: `backup.py`, `netzone.py`, `proxy.py`, `models.py` (permission checks) and the
  `tools/` build chain are checked strictly – this must pass on every pull request. All other modules are checked and
  only listed (61 findings, previously 119). mypy runs in the pinned base image (Python 3.13), installed with checksums
  from `tools/mypy.lock`.
- Database relationships in `models.py` are typed (`Mapped[...]`).

## 3.0.0 – 2026-10-29
Switch to the Docker standard: the program lives entirely in the image. All that remains on the server is
`docker-compose.yml`, `.env` and the data – updates work as with other containers.

**Installation and update**
- Installation: download `docker-compose.yml` and `env.example` (as `.env`) from the release, `docker compose up -d`.
  No zip, no program folder, nothing is built locally.
- Updates with a container manager (Dockhand, Watchtower, Tugtainer – recommendation: at night), with
  `docker compose pull && docker compose up -d` or with `update.sh`.
- **`docker-compose.yml` stays stable:** image `${CATALYTIX_IMAGE:-ghcr.io/soleil0815/catalytix}:${CATALYTIX_TAG:-latest}`,
  no more `build:`, no version number that changes with every release; defaults (e.g. time zone) are in the image.
  The file has its own version number (`CATALYTIX_COMPOSE: "2"`). If it is too old, a notice in the log and, for
  admins, on the overview says what to do. The image ships the matching file.
- **New `update.sh`** (POSIX `sh`, runs with dash/BusyBox and on a NAS, with `docker compose` or `docker-compose` 1.27
  or later):
  - remembers the running image as `previous`, fetches the requested version (default `latest`; `--version=X.Y.Z`,
    also `X.Y.Z-sN`, is written to `.env`), starts it and waits for the health check;
  - if the new version does not start: asks (automatically with `-y`), restores the backup from before the update
    (only one from this run), starts the previous version and stays on it for now;
  - older version requested: first restores the newest backup that this version can read;
  - newer `docker-compose.yml` in the image: puts it in place, keeps the old one as `docker-compose.yml.alt`;
  - without internet: `--load catalytix-X.Y.Z-amd64.tar.gz` (verified against `SHA256SUMS` or `.sha256`);
  - `--cleanup` for installations before 3.0.0: shows the program files that are no longer needed (fixed list, old
    zips, working folders, locally built `catalytix:X.Y.Z` images), asks and deletes only these – never data,
    backups, `.env`, Compose files or mounted folders, not even via links (paths are fully resolved, mounts of all
    containers and the host's mount points are protected, Git folders are not touched).
  - Removed: the zip route, local building, replacing program files, `--pull`, `--no-cache`, `--prune`.

**Database**
- The database remembers the program version (`settings.db_version`). When a new version starts, it is backed up
  before any change (`catalytix-…-vor-X.Y.Z.db`, “before update to X.Y.Z”; the last three are kept in addition to the
  daily ones). If this backup fails, the version does not start. With MySQL/PostgreSQL, a notice in the log (back up
  yourself).
- If an older version starts on a newer database, it aborts with a clear message (which version, which backup) and
  changes nothing. Effective from 3.0.0 – older versions do not know this check yet.
- New commands for going back: `flask restore-before X.Y.Z [--since ZEIT]` and `flask restore-for-version X.Y.Z`
  (with `CATALYTIX_SKIP_DB_CHECK=1`, which skips the check and migrations for this one call).

**Publishing and release**
- A merged pull request no longer publishes anything. Publishing is manual only: *Actions → Build → Run workflow* on
  `main` with **publish** – then, as before, build both platforms, test, scan, `:<version>` and `latest`. The weekly
  security updates (`-sN`) continue to run automatically.
- The release no longer contains a zip but the images as files for the offline route
  (`catalytix-X.Y.Z-amd64.tar.gz`, `-arm64.tar.gz`, exactly the published ones), `SHA256SUMS`, `update.sh`,
  `docker-compose.yml` and `env.example`.

**Migrating existing installations** (step by step in the README and help): run the previous `./update.sh --pull`
once, then `./update.sh --cleanup`.

**Independent review before the merge – fixed, each with a test:** `--cleanup` only works in a folder with an old
Catalytix installation, deletes only entries that clearly belong to the program (your own files with the same name
stay), re-checks every entry right before deleting it and rejects names containing a line break (such a name could have
hit `data`); links inside a linked data folder are protected too. A failed rollback to an older version restores the
newer version's data. In a restart loop after an aborted migration, the backup is made only once (previously on every
start), and the first backup of the run is restored. Going back to 2.x restores the database with the new image. The
way back stays on a security update (`X.Y.Z-sN`) instead of switching to the older release image. “Database too new”
and a failed backup make gunicorn stop instead of restarting workers every second. The old script's `--pull[=X]` is
still understood; images without a health check count as started after 30 s without a restart; `--load` with a custom
registry name; release image files are test-loaded and checked after they are created; README: `chmod 600 .env`.

**Manuals** (`docs/Handbuch-Catalytix.pdf`, `docs/Manual-Catalytix.pdf`) brought up to date for 3.0 (previously 2.10),
with new screenshots: installation and update the new way, pinning a version and going back, migration, reservations,
lending from the catalogue, reference-only collection, person and collection search at the desk, my details, what's new,
AD sync. Noticed and fixed along the way: the name field in the group form was tiny (missing grid classes
`span-1`/`span-5`).

Documentation (README, help *Updates and maintenance*, `.env.example`, comments in `docker-compose.yml`) and tests
(simulated `docker` under dash: update, failed start with rollback, going back, offline, cleanup with links and mounts;
database older/newer than the program; version check of the Compose file; manual-only publishing) updated.

## 2.20.1 – 2026-10-28
Code review of the changes since 2.17.1 (three independent reviewers: loan and reservation logic, security and
permissions, operations/CI/interface) – no critical findings, all confirmed ones fixed, each with a test.

**Important**
- **GitHub: publishing only after passing tests.** The tests in the image ran through `| tee`; GitHub runs steps
  without an explicit shell without `pipefail`, so a test failure was lost and a release or security update would have
  been published anyway (this did not happen – all runs had passing tests). All workflows now run with `shell: bash`
  (`bash -eo pipefail`), `tools/ci.sh` sets `pipefail` itself and evaluates the tests' exit code directly; a self-test
  checks this under `bash -e`.
- **Reservations that no longer apply no longer block a copy.** They are now ended when the account is locked or
  deleted, the person is no longer in the book's group (including through the AD sync) or the book – also individually
  when editing – becomes reference only. A copy set aside for it goes straight to the next person in the queue:
  immediately after admin changes, otherwise during the regular background run. Previously it stayed set aside for up
  to 7 days; when an account was deleted, nobody moved up.
- **No queue jumping:** nobody can borrow a copy that has become free but has not yet been set aside past the queue –
  it is set aside for the first person immediately.

**Other fixes**
- Borrowing via the address `/buecher/<id>/ausleihen` first checks whether you may see the book (otherwise “not
  found”). Previously the message named the title and group of reference-only books that are invisible in the
  catalogue.
- Mobile: the reservation lists on the book page and under *My reservations* were unreadable at 360 px (name/title
  squeezed to one character); the status is now below the name. Librarians additionally see the username there and on
  return (“Please put it aside – reserved for …”).
- Duplicate ISBN when adding: the message names the copies actually added (fewer at the maximum) and says that the
  form's other details were not applied; a notice at 0 copies.
- Profile: the display name must not be empty.
- `update.sh --pull=X.Y.Z-sN` pins a security update (previously rejected); `.env` remembers exactly this image.
- Help: current label of the lend-immediately option at the desk; reservations, adding books and maintenance added.

## 2.20.0 – 2026-10-27
**New**
- **Reference only:** groups have the option *Reference only – books in this group are not lent out*
  (*Administration → Groups*). The books stay in the catalogue (notice “Reference only”) but are not offered at the
  circulation desk (neither in the collection search nor when scanning – there with a clear message) and can be
  neither lent, borrowed by readers themselves nor reserved; checked centrally on every loan. When switched on, open
  reservations for these books are ended. Current loans are returned normally. No account is created for AD people for
  this. New column `groups.lendable` (added automatically on startup; existing groups stay lendable).
- **Lending from the catalogue:** below every available book you manage there is *Lend* (tile and list view). The
  button opens the book page at the *Lend to a user* form, with the cursor in the *User* field.
- **My details in the profile:** local and self-registered accounts change their display name and email address
  themselves – regardless of whether emails are switched on, with the same rules as before (valid, unused address; for
  self-registration an allowed domain and, if required, confirmation via the new address). AD accounts remain
  read-only as before; the profile points out that the data comes from AD.

**Improved**
- **Added twice:** if a book is saved on creation with an ISBN that already exists in the same group, the entered
  copies are added to the existing book (in SQL, simultaneous additions both count) instead of creating a second
  entry; new copies go to the reservation queue first. In a different group a separate entry is still created. The
  notice on ISBN lookup now says so.
- Help (German/English), README and what's new updated; 20 new tests.

## 2.19.0 – 2026-10-25
Build and delivery reorganised: everything pinned, automatically secure and up to date – without version jumps.

**Reproducible build**
- Base image with a fixed version **and** checksum: `python:3.13.17-slim-trixie@sha256:…` (previously 3.13.16; in the
  GitHub builds of 2.18.3 the moving tag `3.13-slim-trixie`).
- **Debian packages** pinned to a fixed archive state (`DEBIAN_SNAPSHOT`, snapshot.debian.org) – all packages in the
  image, including those of the base image. The build aborts if the base image is not Debian “trixie”.
- **Python packages** in `requirements.txt` with exact version and SHA-256 of every file, including indirect ones
  (`--require-hashes`); `pip` in `docker/pip.lock`, removed from the image after installation (checked). The content
  list is in the image at `/app/image-packages.txt`.
- `tools/lock.py update` re-pins all versions – only within the foundation: same Debian version, same Python series,
  every package within its major version (before 1.0: minor version), calendar versions (e.g. `certifi`) without a
  limit; new versions only after 7 days, an already pinned version is never excluded. `requirements.in` follows this
  rule (e.g. `Flask>=3.1,<4`). First pinning: Python 3.13.17, cryptography 50.0.2, SQLAlchemy 2.1.5, Werkzeug 3.1.9,
  MarkupSafe 3.0.4, charset-normalizer 3.5.2, pycparser 3.11.
- The build options of 2.18.3 (`OS_UPDATES`, `DEPS=current`, `tools/lock_floor.py`) are removed – the GitHub builds
  now use exactly the pinned versions.

**Weekly security update** (`.github/workflows/security-update.yml`, Mondays 04:17 UTC, also manually)
- Code of the highest release, re-pin versions, build once without cache for amd64 and arm64, compare with the
  published `latest` – nothing changed: do nothing. Otherwise: tests in the image, Trivy for both platforms, and only if
  everything passes: publish exactly these images as `<version>-s<counter>`, locks and CHANGELOG entry (list of changed
  packages) on `main`, then switch `latest`. Replaces the weekly rebuild `rebuild.yml`.
- On release, too, both platforms are built once, tested and scanned, and exactly these images are published. The
  Debian state is always a fully imported point in time (previous day 00:00 UTC); the build checks that no package was
  held back. Checks of the registry and the GitHub API never treat errors as “not present”; the highest version is
  determined from all releases. Independent review of the new workflows before publishing.

**Trivy, scans, foundation**
- Trivy no longer as a GitHub Action but a fixed version 0.76.0, verified by SHA-256 (`tools/trivy.lock`,
  `tools/trivy_scan.py`); only HIGH/CRITICAL with a fix stops. `.trivyignore`: every entry with a reason and expiry
  date (at most 6 months), otherwise the run stops.
- **Daily scan** of the published `latest` (`scan.yml`).
- **Foundation check** weekly (`foundation.yml`, endoflife.date): from 12 months before the end of security updates
  for Debian 13 or Python 3.13 a notice, from 6 months a red run.

**Workflow security**
- All actions pinned to commit checksums, `permissions: {}` as default and minimal permissions per job, no stored Git
  credentials (`persist-credentials: false`), publishing only from `main` and never concurrently (shared lock),
  published tags are never overwritten, `latest` only for the highest version. Tests run in the finished image instead
  of on the runner. Causes of failures appear readably as an annotation on the run.
- Self-tests (`tests/test_supply_chain.py`): schedules, order of steps, version limits, permissions, pinning, flow with
  a simulated Trivy, counter and changelog, foundation thresholds.
- README and help (*Maintenance*): what happens automatically and what to do on a red run.

## 2.18.3 – 2026-10-24
- **Everything in the GitHub image stays up to date** – on every release and on the weekly rebuild, within the same
  foundation:
  - Python: latest 3.13 patch version (`python:3.13-slim-trixie` instead of fixed 3.13.16), Debian 13 “trixie” plus
    current security updates.
  - Python packages: freshly resolved from `requirements.in` (`DEPS=current`) – newest versions within the allowed
    ranges, but only releases at least 7 days old (`pip --uploaded-prior-to`, protection against freshly uploaded
    malicious versions). Versions from `requirements.txt` count as a lower bound: a reviewed security fix entered there
    may skip the waiting period (`tools/lock_floor.py`). The first run showed exactly that: the waiting period would
    have held back the certifi fix (5 days old), and Trivy stopped the publication.
  - **The tests now run in the finished image** (exactly its Python and packages) – only then Trivy and publishing.
    The installed versions are listed in `/app/installed-packages.txt` and in the run summary.
- Local builds (zip package, `update.sh`) remain exactly reproducible: pinned base image and `requirements.txt`.
- The foundation (Debian 14, Python 3.14, new major versions) is raised deliberately once the current foundation no
  longer receives updates.

## 2.18.2 – 2026-10-24
- **Weekly automatic rebuild** (`.github/workflows/rebuild.yml`, replaces the check-only run `scan.yml`): every Monday
  the state of the **latest release** (never unpublished commits from `main`) is rebuilt without build cache – with the
  current Debian security updates –, tested and scanned with Trivy. Only if everything passes is the image published as
  `latest`, plus `<version>-r<Datum>` and `sha-<commit>`; the `<version>` tag remains the release image. Never runs at
  the same time as a release build.
- If Trivy finds a vulnerability in a (pinned) Python package, the run fails and nothing is published – GitHub reports
  this by email; a new version with the updated package is then needed.
- `Dockerfile`: `OS_UPDATES=1` (set in all GitHub builds) applies the current Debian security updates to the pinned
  base image. Local builds (zip package) stay exactly at the pinned state without the option.

## 2.18.1 – 2026-10-24
- **Security scanning with Trivy** in the GitHub workflow: the image is built and scanned before publishing. Known
  HIGH/CRITICAL vulnerabilities with an available fix stop the run – then nothing is published (report as a table in
  the run summary and as an annotation). Vulnerabilities without an available fix do not block. Exceptions with a reason
  in `.trivyignore`. In addition, `.github/workflows/scan.yml` checks the published `latest` every Monday.
- The Trivy action is pinned to the full commit of the immutable release v0.36.0, Trivy itself to v0.76.0 (after the
  supply chain attack on Trivy in March 2026, CVE-2026-33634); a test checks this.
- **Fixed what the first scan found** (33 findings, 6 critical):
  - **Pillow 13.0.0** (previously 12.3.0) – 29 vulnerabilities in image processing, which is used for cover and logo
    uploads. The code now uses `Image.Resampling.LANCZOS`.
  - **certifi 2026.10.19** (previously 2026.7.22).
  - **pip is removed from the image after installation** – it is not needed at runtime and brought its own outdated
    copies of urllib3, msgpack and setuptools.

## 2.18.0 – 2026-10-24
- **Reservations**, optional under *Settings → Reserve* (off by default): if all copies are on loan, you can reserve a
  book on its page and join a queue (position visible, also under *My loans*, where it can also be deleted).
- When a copy becomes free – return, additional copy, reservation deleted or expired –, it is **set aside** for the
  first person: email “is ready for you”, notice on the overview, and at the desk the return reports “Please put it
  aside – reserved for …”. For everyone else the copy counts as unavailable (catalogue, filters, suggestions, desk, CSV
  export).
- **Pickup period** (default 7 days): if the book is not collected, the reservation expires and the next person is up.
  Lending to the person completes the reservation; at the desk, once the person is selected, the set-aside book appears
  at the top under “Set aside for …”.
- Readers cannot renew reserved books themselves (librarians can). Librarians see the queue on the book page and can
  delete reservations. Maximum number of open reservations per person adjustable (default 5).
- Fewer copies than set aside: the most recently set-aside ones go back into the queue (position kept). Locked
  accounts drop out when moving up. Switching off deletes all open reservations.
- Completed reservations are deleted with the loans' retention period; a note for the privacy policy in the help.
- Database: new table `reservations` – created automatically on startup.
- Help for the circulation desk: texts on the “… on an exact match (ISBN, shelf mark)” automation adapted to
  2.17.1.
- README: the network `172.16.0.0/12` (section “Internal network”) had been corrupted since 2.16.1 by a slip when the
  version number was bumped.

## 2.17.1 – 2026-10-24
Code audit (four independent reviewers: sign-in/permissions, business logic/data, operations/update/CI, interface) –
no critical findings, all confirmed ones fixed; in addition, `./update.sh --pull` now follows `latest`.

**Security and permissions**
- **Group librarians without a group:** if a librarian lost their last group (group deleted, checkbox cleared, removed
  from the group in AD), they then managed the **entire** collection. Now the account remembers that it was bound to
  groups (`users.group_bound`) and then manages only general books. The entire collection is only available if an
  admin ticks “Manage the entire collection without a group” in the user account. New librarians without a group
  manage everything as before; existing accounts with groups are marked as group-bound on startup.
- **AD rules for OUs:** an escaped comma in the name (`CN=Mallory\, OU=Bib-Admins,…`) could fake membership of an OU
  and thereby grant a role. Comparison is now component by component.
- **Accounts expired in AD** (`accountExpires`) are treated like disabled ones: sessions end and the sync locks them.
- **Registration:** “Name/address already taken?” checks are limited per address (10 in 15 minutes, across all server
  processes) – previously accounts and AD names could be queried any number of times.
- An unconfirmed account locked by an admin is no longer activated by the confirmation link.
- The desk no longer turns an account that has since become local (password via `flask reset-password`) into an AD
  account; `reset-password` also lifts a lock set by the AD sync. Sign-in locks count spellings such as “ＡＮＮＡ”
  (full-width) as “anna”.

**Loans and email**
- **Reminders after a renewal:** previously a reminder sent once (or the maximum number of overdue notices) applied to
  the whole loan – after a renewal nothing more came. Now they count per due date (`loans.due_changed_at`).
- **Mail server disconnects midway** (message limit, timeout): it reconnects; if that fails, the day is released and
  the rest is caught up later (previously the remaining reminders were lost).
- The list of due reminders is built in a few queries instead of several per loan (2,000 loans: previously around
  7,000 queries when opening the email settings). “Send now” and the daily run can no longer overlap (no duplicate
  reminders).
- **Circulation desk:** “Lend/return immediately on an exact match” now acts only on shelf mark, ISBN or “#number” –
  no longer on a single hit that happens to be in title, author or tag. The free-text person search finds only active
  accounts and takes `%`/`_` literally.
- A duplicate return (double click, two desks) does not overwrite the time and sends only one confirmation. The
  maximum number of loans per person also holds for simultaneous loans under MySQL/PostgreSQL.
- User list: groups are loaded in one query.

**Update and GitHub**
- **`./update.sh --pull` now follows `latest`:** `.env` then contains `CATALYTIX_TAG=latest` instead of a fixed
  version number. This lets a container manager that updates images automatically pull new versions itself.
  `./update.sh --pull=X.Y.Z` pins exactly one version (`CATALYTIX_PINNED=1`) until the next `--pull`. Installations
  that had a fixed number entered by older versions follow `latest` from now on.
- **GitHub publishes `latest` and `<version>` only for a new version number** (higher than the last release). Pushes
  without a new number only produce `sha-<commit>` – servers following `latest` never get unreleased intermediate
  states. No more tag runs (no race on release), `workflow_dispatch` publishes only on `main`.
- `update.sh`: the installed version comes from the running container (not from possibly outdated files in the
  folder). If an update is aborted after `docker pull`, the tag points to the old image again – previously a later
  `docker compose up` could start the new version without a backup and the cleanup could delete the old image. Rolling
  back after an error during the swap never again deletes an original that had not yet been moved. In pull mode,
  outdated program files from earlier zip installations are cleared away. Hint for going back in pull mode:
  `./update.sh --pull=<alte Version>`. Errors writing `.env` are reported.
- Image: health check with a 120 s start period (large `./data`), `update.sh` waits up to 300 s; the version number is
  the last label (dependencies stay in the build cache).
- README: `docker-compose` 1.x only from 1.27 (older ones cannot read `docker-compose.yml` without `version:`); `7z`
  is not supported.

**Interface**
- A late AD response in the person search no longer overwrites an already selected person or reopens the list. The
  camera turns off if the scanner is closed while starting. In quick capture and ISBN lookup, only the result of the
  most recently scanned ISBN counts.
- Expired session: requests from the page get a clear message (HTTP 401) instead of an empty error box.
- Drop-down lists (sorting, filters) can be operated with the keyboard without every arrow key reloading. Error
  messages stay until you close them.
- Mobile: long titles, names and commands no longer widen the page (including help, overview, book form).
- What's new: a security update always comes first in the notice on the overview; the dot on the user menu is also
  read out by screen readers. Program name removed from the help and CSV file names (`bestand.csv` / `catalogue.csv`).
- Database: new columns `users.group_bound` and `loans.due_changed_at` – created automatically on startup.

## 2.17.0 – 2026-10-24
- **What's new in the app:** at the bottom of the sidebar next to the version number (and in the user menu) there is
  *What's new* – short, easy-to-understand notes on new features, improvements and **security updates**, in German
  and English. Everyone sees only what concerns their role (readers get no admin-only topics).
- Unobtrusive: if there is something new after an update, a small dot appears next to *What's new* (on mobile on the
  avatar) and a narrow line “New in version …” on the overview, which can be dismissed. Anyone who opens the page or
  closes the line no longer sees it until the next update. New accounts start without old messages; after this
  update, existing accounts see only this version's news, the older ones are on the page for reference.
- The texts are in `catalytix/news.json` (per version; kind `security`/`new`/`improved`/`fixed`, audience
  `all`/`librarian`/`admin`). A test checks both languages for completeness and that the current version has an entry.
- Database: new column `users.news_seen` – created automatically on startup.
- GitHub: the **release with zip package is now created automatically** as soon as a new version number arrives on
  `main` – no tag needed any more (if the release already exists, nothing happens).

## 2.16.1 – 2026-10-23
- **GitHub workflow repaired:** the test run on GitHub aborted because a test used PyYAML, which is not one of
  Catalytix's packages. The test now checks the workflow file without PyYAML.
- The workflow uses the current versions of the GitHub Actions (`checkout@v7`, `setup-python@v7`, Docker actions
  v4/v7), which run on Node.js 24 – the warning “Node.js 20 actions are deprecated” is gone.

## 2.16.0 – 2026-10-23
- **GitHub builds the container:** workflow `.github/workflows/build.yml` for the repository `soleil0815/catalytix`.
  On every push to `main` the tests run with the pinned package versions, then the image
  `ghcr.io/soleil0815/catalytix` is created (tags `<version>`, `latest`, `sha-<commit>`; for Intel/AMD and ARM). A tag
  `v<version>` additionally creates a release with zip package and checksum. The workflow checks that the version
  numbers in the code, `Dockerfile` and `docker-compose.yml` match.
- **`./update.sh --pull`** fetches the finished image from GitHub instead of building locally (`--pull=X.Y.Z` for a
  specific version). The matching installation files (`docker-compose.yml`, `update.sh`, `.env.example`, READMEs) come
  from the image; `.env` remembers the image and version (`CATALYTIX_IMAGE`, `CATALYTIX_TAG`) so that
  `docker compose up -d` always starts exactly this version. Backup beforehand, checks and cleanup as with the classic
  update. New installation: just put `update.sh` in an empty folder and run `./update.sh --pull`.
- **The classic route remains:** `./update.sh catalytix-X.Y.Z.zip` builds locally as before and switches back from
  `--pull` automatically.
- `docker-compose.yml`: image selectable via `CATALYTIX_IMAGE`/`CATALYTIX_TAG`; **Dockhand label removed**.
- `.gitignore` keeps data, keys, `.env`, databases and packages out of the repository; image with OCI labels (source,
  description) so that GitHub associates it with the repository.

## 2.15.2 – 2026-10-23
- **Server messages in English:** log lines (e.g. startup errors, key check, time zone, AD sync, sign-ins, mail
  delivery, backup) and the command-line commands (`flask --app wsgi …`: help, prompts, output) are now in English.
  Results that also appear in the interface (backup, retention, reminder run) are still shown in the interface in the
  selected language, and in English in the log and on the command line. The interface itself remains German and
  English.

## 2.15.1 – 2026-10-23
- **Installation files in English:** output, prompts and help of `update.sh` as well as the comments in
  `docker-compose.yml`, `.env.example`, `Dockerfile`, `requirements.txt`/`requirements.in`, `wsgi.py` and the messages
  of `docker-entrypoint.sh` are now in English. The `update.sh` prompt reads `[Y/n]` (“j” is still accepted).
  Interface, help and READMEs remain bilingual.
- Existing `.env` files are not changed – only the template `.env.example` has been reworded.

## 2.15.0 – 2026-10-23
Security review (application, and operations and Docker, one independent reviewer each) – no critical findings, all
others fixed; plus cleanup on update.

**Update cleanup**
- `update.sh` cleans up after a successful update: older Catalytix images and zip packages `catalytix-X.Y.Z.zip` (in
  the program folder and next to the installed package – the current and the previous one stay for going back, as do
  newer ones), working folders of aborted updates after 7 days, and Python caches. Other files, links, shared folders
  such as `/tmp` and `./data` are never touched. `--prune` keeps only the current state, `--no-cleanup` turns the
  cleanup off.

**Security – application**
- **Initial setup only with a setup code.** Previously anyone who reached port 9300 before you could create the
  administrator account. The code is in the log, and `update.sh` shows it after installation
  (`flask --app wsgi setup-code`); guessing attempts are slowed down.
- **Links in registration emails** are now built only from the base address (*Administration → Email*), never from
  the request – a forged Host header could make confirmation and admin emails point to a foreign site. Without a base
  address, registration with email confirmation is blocked (with a notice in the settings).
- **AD without a service account:** the password is now always checked against exactly the entry found. Previously
  someone whose UPN matched another person's sign-in name could sign in as that person.
- **Failed attempts:** locks triggered from outside now apply only to access from outside (on the home network you can
  still sign in); an address from the internet can no longer try one password on all accounts; for the second factor
  at most 30 wrong codes per account and day.
- **Email address in the profile:** self-registered accounts must confirm a new address; an address used by another
  account is rejected.
- Admins signed in on the home network without a second factor now also have to confirm it for retention “Clean up
  now”, backup settings, rejecting registrations and deleting groups.
- AD role rules: hint to use the full group DN for admins and librarians (a bare name matches every group of the same
  name); “Load groups” takes over the DN for this. Warning for an unencrypted connection.
- Group librarians now see only their own collection for locations and the overdue counter.
- Notice in the 2FA settings if the home network does without a second factor but every private address still counts
  as a reverse proxy; recommendations for running on the internet in the README.

**Security – operations**
- `update.sh` checks the package against `catalytix-X.Y.Z.zip.sha256` (included with every package), otherwise it
  shows the checksum. File permissions from the package are not adopted blindly (nothing writable by others), new
  files belong to the owner of the program folder, and `.env` is readable only by them. No more `7z`.
- Database, covers, keys and backups are now readable only by the app user (`umask 077`, existing files are adjusted
  on startup).
- Container with `no-new-privileges`; backups are never served via links; `X-Forwarded-Port` is no longer adopted.

## 2.14.0 – 2026-10-23
- **AD sync and person cache** (*Active Directory*, on by default with a service account): in the background –
  hourly, adjustable up to daily, “Sync now” starts it immediately – Catalytix fetches all AD people who may use the
  library. The person search at the desk then finds them in milliseconds without asking AD (load test: 3,000 people,
  search under 30 ms). Only if the cache knows nobody does the search additionally ask AD directly. Lending still
  happens only after a fresh check in AD.
- People who are **disabled, deleted or no longer authorised** in AD drop out of the cache during the sync. Switching
  off the cache deletes it immediately.
- **Existing AD accounts are brought up to date**, including for people who never sign in: name, email, role, library
  groups. **Renames in AD** are detected (fixed AD identifier objectGUID) – the account and its loans move along, also
  when signing in with the new name. If a name in AD is given to a different person, the old account is locked under
  a substitute name instead of the new person inheriting its loans.
- Accounts of people missing from AD or no longer authorised are **locked, not deleted** (loans stay visible, user
  list: “no longer in AD”) and automatically unlocked when the person comes back – also immediately at the desk or on
  sign-in. Accounts locked by hand stay locked.
- **Safety brake:** if a sync would lock more than 30 % of the AD accounts at once, it locks none and reports this. If
  the AD search is incomplete (e.g. size limit) or AD is unreachable, everything stays unchanged. Only one sync runs
  at a time, even with several server processes.
- Nested groups are only re-resolved when the direct groups have changed, otherwise once a day. Status display on the
  *Active Directory* page. Notes for the privacy policy in the README and help.
- Database: new table `directory_people`, new columns `users.ldap_guid` and `users.ldap_gone_at` – created
  automatically on startup (including index).

**Code review of this version – fixed**
- Rename plus reassignment of a name in AD (e.g. “berta” → “berta.m”, a new person is called “berta”): the account is
  now matched correctly regardless of the order in AD – first via the fixed identifier, then via the name. Previously
  the renamed person's account could be locked briefly.
- The regular session check never again overwrites an account with another person's data when that name has been
  reassigned in AD – the session is ended. If a name belongs to another person, Catalytix checks in AD whether the
  previous one was only renamed (then their account moves) or has left (then it is deactivated). If this cannot be
  resolved, nothing is created.
- Accounts locked by hand are never unlocked automatically in these cases either. New: an account locked by the sync
  that an admin saves with “Account active” unticked stays locked permanently.
- If the group query for a person fails temporarily (timeout, server busy), they count as “uncertain” and stay
  unchanged – previously they would have dropped out of the cache and their account would have been locked.
- The database is no longer locked during the AD query; only what has changed is written (5,000 people: 0.6 s instead
  of 5.7 s). The sync lock regularly sends a sign of life and is released only by its own run.
- After an error, it retries after 15 minutes (not only in the next time window); a cache older than two days (AD
  unreachable for a long time) is no longer used; switching off during a run saves nothing; the brake also covers
  mass renames (e.g. after changing the sign-in attribute); substitute names are always unique; the desk re-checks
  locked accounts in AD at most once per minute.
- Follow-up review: the safety brake also counts accounts that would be deactivated because of a reassigned name
  (e.g. domain migration with all-new identifiers – then none is deactivated). OpenLDAP identifiers (entryUUID) are
  found during resolution. Partial responses to group queries count as “uncertain”. A move blocked by the name of a
  deactivated account is resolved; a sign-in rejected this way does not count as a failed attempt.
- Existing AD accounts are synced **even without the person cache** (then once a day) as soon as a service account is
  entered.

## 2.13.0 – 2026-10-22
- **Search field at the top with suggestions:** while typing (from two characters), matching books appear with
  author, shelf mark and available copies. Click or arrow keys + Enter open the book directly; Enter without a
  selection shows all hits in the catalogue as before. Only what you may also see in the catalogue is shown. Exact ISBN
  and shelf mark hits come first, then titles that begin with the search text.
- **Person search at the desk much faster:** known people appear immediately, hits from Active Directory are added a
  moment later (previously the whole list waited for AD – anyone pressing Enter quickly got no suggestion). A person
  already selected with the arrow keys stays selected when more results load.
- AD search: the (nested) groups of people found are cached for five minutes – AD is not asked again on every
  keystroke. If the direct groups change in AD, they are re-resolved immediately; sign-in and re-checks always query
  fresh.
- Suggestions respond faster (150 instead of 250 ms after the last keystroke); the browser's own input list no longer
  covers the search field at the top.
- Minor: in the person list, people without a group had a superfluous dot (“· 1 on loan”).

## 2.12.2 – 2026-10-22
Code review of 2.12.1:
- `update.sh` without `unzip`: a damaged package was not detected when extracting with `python3` (the check reported
  the error only as text). Now the script aborts before anything is replaced.
- `update.sh` creates `.env` only once `docker-compose.yml` is present – so on a new installation from the package's
  template instead of as an empty file.
- New tests: update with old `docker-compose` without `.env` and without `unzip`, damaged package, new installation
  (407 in total).

## 2.12.1 – 2026-10-22
- **No minimum version for Docker Compose any more** (previously 2.24). Catalytix thus also runs with older Compose
  versions, e.g. on a Synology. `docker-compose.yml` now reads `.env` the classic way – so the file must exist
  (`cp .env.example .env`, everything in it remains optional).
- `update.sh` creates a missing `.env` itself from the template, falls back to the older `docker-compose`, manages
  without `docker compose ps --status` and, if `unzip` is missing, extracts with `python3` or `7z` instead.
- **When updating from 2.12.0 or older:** if there is no `.env` yet, run `cp .env.example .env` once beforehand – the
  update still runs with the old `update.sh`, which does not create the file itself. Otherwise the start aborts with
  “env file … .env not found”; then create `.env` and run `./update.sh` again.

## 2.12.0 – 2026-10-22
- **Collection search at the circulation desk:** matching books appear in the book field while typing – search by
  title, author, subtitle, shelf mark, ISBN and tag, with location, available and lent copies, and group. Select with
  arrow keys/Enter or a click. Books the selected person may not borrow come last and are marked; in the *Return* tab,
  borrowed books come first. Group librarians see only their own collection.
- The book field also works without a selected person (collection display only, no lending).
- Search by input + Enter now also searches author, subtitle and tags; `%` and `_` are taken literally.
- Code review: exact hits (shelf mark, ISBN) always come first, even with many partial hits. Only an exact hit is
  preselected – otherwise Enter searches for the typed text instead of lending the first suggestion. Books the person
  already has come last and are marked; for AD people not yet created, their groups apply.

## 2.11.0 – 2026-10-22
- **Person search when lending:** at the circulation desk and on the book page (*Lend to a user*) there is a live
  search by name, username or email – operable with arrow keys and Enter. The long drop-down of all users on the book
  page is gone (faster with many users).
- **Also AD people who have never signed in:** with a service account, the search also searches Active Directory.
  Only active accounts that may use the library according to the role rules are shown, marked “new from AD”. The
  account is created only on the first loan – with name, email, role and groups from AD, without a password; if the
  loan fails (e.g. wrong group), nothing is created. Nobody is signed in by this, local accounts of the same name stay
  unchanged. If AD is unreachable, the search still finds all known users. The directory is only queried from three
  characters, results are cached for one minute, and after an AD outage only local search is used for one minute –
  this spares AD and keeps the page fast.
- Group librarians see only their groups and loans from their own collection in the search (as at the desk).

**Code review of this version – fixed**
- Two-factor confirmation on the LAN now also applies to approving new accounts, email settings and backups (create,
  download, delete) – a backup contains all data.
- Stored SMTP and AD passwords are only reused for the same server and the same account; a changed server no longer
  gets the old password (“test email to a foreign server”).
- 2FA codes with digits from other scripts (e.g. Arabic keyboard) are accepted instead of an error page.
- Person search: no error page for hit lists at the desk, no error when two people create the same AD person at the
  same time, Enter during a running search never selects an outdated hit, works with OpenLDAP too, finds known people
  also via new details in AD, screen reader support.
- Docs: port binding via `CATALYTIX_BIND` (`docker-compose.yml` is replaced on updates), scope of the 2FA confirmation
  in README and help, help on lending updated.

## 2.10.1 – 2026-10-22
- **Branding was not saved:** anyone signed in on the internal network without a second factor but who has 2FA
  themselves was redirected to the security page on *every* change in the administration since 2.10.0 – including
  colours, logo, groups or email. The confirmation now applies only to security-relevant changes (users, roles,
  resetting 2FA, lifting locks, settings, Active Directory). There it leads directly to the code prompt and then back
  to the page.

## 2.10.0 – 2026-10-22
Two complete code reviews (security, logic, operations) – all findings fixed, 112 new tests (367 in total).

**Security**
- **Two-factor:** a session started on the internal network without a second factor can no longer add, replace or
  remove factors, generate recovery codes or change anything in the administration – only after “Confirm second
  factor”. Previously this could be used to slip in your own factor for access from outside.
- **AD sign-in** can no longer take over a local account with the same name (e.g. with “DOMÄNE\name”).
- **Initial setup** is only possible with an empty database. If later no admin is left, a new one can only be created
  on the server (`create-admin`).
- **“Reject registration”** now only affects pending registrations – never admins or your own account.
- **Sign-in locks:** usernames are normalised (“DOMÄNE\anna” = “anna”), the per-account lock also applies on the LAN
  (100 instead of 40 attempts), and after 10 wrong codes for the second factor the password is required again.
  Strangers cannot end a sign-in in progress through failed attempts.
- **TOTP codes and recovery codes** are guaranteed to be valid only once, even with simultaneous sign-ins.
- **Internal network:** without an entry only 10.x and 192.168.x count – Docker networks (172.16–31.x) only if
  entered. New: `CATALYTIX_BIND` (e.g. `127.0.0.1`) so nobody can bypass the reverse proxy; notes in the README.
- **Cover download** connects only to the checked address (protection against DNS tricks targeting internal servers),
  with an overall timeout; broken images or servers that drop the connection no longer cause error pages.
- A forged `X-Forwarded-Host` can no longer redirect links in registration emails. Recommendation: enter the base
  address under *Email* – then all links come from this setting only.
- Group librarians now see only their groups at the desk and in the ISBN check.
- Recovery codes are no longer readable in the session cookie; the profile email follows the domain rule.

**Lending and catalogue**
- **No more double lending** on simultaneous clicks (lock + database rule); the maximum number of loans and renewals
  now always holds too.
- **Desk:** plain digits count as a shelf mark; the internal book number is entered with `#` (e.g. `#12`). Shelf marks
  with umlauts are found.
- **Faster:** catalogue filters, CSV export and the loan list need only a few database queries instead of 10,000 with
  10,000 books. The loan list has pages instead of stopping at 500 entries.
- No more error pages for unusual input (e.g. “X” in the middle of an ISBN, “²”, huge numbers).
- Paging through the catalogue keeps the location filter; no reminder for returned books.

**Operations**
- **Startup:** the two server processes no longer run the database setup at the same time; the secret key is
  guaranteed to be generated only once.
- **Key check:** if `secret_key` does not match the database, Catalytix does not start (instead of silently breaking
  all 2FA sign-ins) and reports this clearly in the log. Documented in the README and `.env.example`.
- **update.sh:** backup before the swap; the new package is first checked and extracted, then swapped in – on errors or
  abort, the old state is restored. Adopts the Compose project name, aborts at prompts when there is no terminal
  (otherwise `-y`), the zip path works from any folder. The way back to the old version is described in the README.
- Commands in the container run via `docker-entrypoint.sh` (no longer as root – backups stay readable).
- Warning if `CATALYTIX_BACKUP_DIR` is not a mounted drive; log limited to 3 × 10 MB.
- The email run is retried every 15 minutes after an SMTP failure; the daily summary is never sent twice.
- Backup: no more leftover files; backups from the same second are listed and rotated correctly.
- MySQL/MariaDB: safe isolation level for lending; column names are escaped correctly.
- The time zone is checked – if it is missing from the image, a clear warning appears in the log.

**Language**
- The default name “Bibliothek” appears in English as “Library”; registration emails in the language of the
  registration; first message after sign-in in the profile language; plural forms in the help corrected; the “?” on
  “All loans” leads to the right topic.

## 2.9.1 – 2026-10-21
- Link to the developer website [helfi.space](https://helfi.space) on the *About this program* page, in the READMEs and
  in both manuals.

## 2.9.0 – 2026-10-20
- **German and English:** the entire interface, all messages, emails and browser messages are now also available in
  English. Switch with **DE / EN** at the top right and on the sign-in page; when signed in, the choice is saved in
  the profile and also applies to emails to this person. Without a choice the browser language applies, otherwise the
  new **default language** (*Settings → General*). Dates follow the language.
- **Help in the program**, depending on role: readers see 7 topics, librarians 14, administrators 25. The **?** at the
  top right jumps to the help for the current page. The texts adapt to the library's settings (loan period,
  self-checkout, renewals, reminders, mandatory two-factor, AD account …). With a search field.
- **Manuals in the package:** `docs/Handbuch-Catalytix.pdf` and `docs/Manual-Catalytix.pdf` (new, English with English
  screenshots), linked for download in the help. The German manual has a new chapter “Sprache und Hilfe” (language
  and help) and describes updating with `update.sh`.
- Minor fixes: the “Renew” button on the book page only appears when renewing is actually allowed. The help states the
  correct lead time for reminders. The result of the last reminder run is shown in the viewer's language. “1 Monat”
  instead of “1 Monate” (singular instead of plural for one month).
- New tests: completeness of the translation, English pages without German leftovers, language selection, email
  language, role-dependent help (254 in total).

## 2.8.1 – 2026-10-20
- **Reproducible build:** base image pinned exactly (`python:3.13.16-slim-trixie`, additionally frozen by digest). All
  Python packages including sub-dependencies and `pip` itself set to a fixed version (`requirements.txt`); installation
  without pulling in further packages, only from pre-built packages, with a final check. Direct dependencies with
  allowed ranges in `requirements.in`.
- Updated to: SQLAlchemy 2.1.4, gunicorn 26.2.0, WTForms 3.2.2, Flask-WTF 1.3.0, PyMySQL 1.2.3, pyasn1 0.6.4. All 237
  tests run with exactly these versions.
- **`update.sh`:** rebuild and update with one command – install the package (personal files stay), backup, build the
  image, restart, wait for “healthy”, optionally clean up old images. Works with `./update.sh`, `bash update.sh` and
  `sh update.sh`.

## 2.8.0 – 2026-10-20
Security and bug review of the entire program.

**Security**
- **Sign out on all devices** (*Profile → Security*). Changing the password and resetting two-factor authentication
  automatically end all other sessions – including “Stay signed in” cookies. Previously a stolen device stayed signed
  in after a password change.
- **AD accounts are re-checked:** every 4 hours per session (via the service account). Accounts disabled, deleted or
  no longer authorised in AD are signed out, role changes are applied. If AD is down, you stay signed in.
- **Minimum password length** raised to 10 characters (applies to new/changed passwords).
- **Credentials encrypted:** the AD service account and SMTP passwords are no longer stored in plain text in the
  database; existing entries are converted automatically on startup.
- **Content Security Policy:** all inline scripts removed; the browser only runs scripts from the app's own server.
  This blocks injected code even if there were a vulnerability somewhere.
- **Redirects** after admin actions and loans only to the app's own pages (not via tab/line-break tricks either).
- **From outside**, an additional brake per username against distributed password guessing. The response time no
  longer reveals whether a username exists.
- **Cover uploads** are checked and re-encoded (no disguised files); covers of other groups are only visible if the
  book is visible.
- **CSV export:** protection against formula injection in Excel.
- **Email:** recipient addresses are checked strictly (no comma-separated lists), subject lines without line breaks –
  no injection of additional recipients.

**Bugs and logic**
- Settings with upper limits (loan period, renewals, ports, times …) – nonsensical values led to error pages.
- The due date when lending must be between today and ten years from now.
- Desk: invalid form values give “not found” instead of a server error.
- Group colours are checked; the base address for email links must start with http(s).
- Incorrect comments in the default settings corrected.
- 27 new automated tests (237 in total).

## 2.7.0 – 2026-10-20
- **Set up locations** (*Management → Locations*, for librarians and admins): list of shelves/rooms, several can be
  added at once, sorted naturally (“Regal 2” before “Regal 10”), optional note.
- When **adding, in quick capture and when editing**, the location is chosen from the list instead of typed; case and
  spaces are normalised, unknown locations are rejected. An already saved location that is not listed is kept when
  editing.
- **Renaming** updates all affected books too; **deleting** only if the location is empty.
- **Tidy up existing data:** adopt freely typed locations or replace them with a defined one; for spelling variants
  and typos, the matching location is suggested.
- Group librarians may not change books of other groups along with it.
- **Catalogue:** filter by location.

## 2.6.0 – 2026-10-20
- **Own internet connection as internal** (*Settings → Two-factor authentication*): enter a fixed public IP or DynDNS
  name. Internal computers that call the public domain and are passed on by the router with the library's own public
  IP thus count as internal. DynDNS names are re-resolved every 5 minutes (if DNS fails, the last address remains valid
  for up to an hour); the currently resolved address is shown.
- Notice in the interface: the guest Wi-Fi also comes through this address and then counts as internal.

## 2.5.0 – 2026-10-20
- **Two-factor depending on the network** (*Settings → Two-factor authentication*): mandatory for access from outside,
  adjustable as before; additionally **“On the internal network”**: same as from outside (default) / not mandatory /
  no two-factor prompt.
  - **Internal network** freely definable (one network address per line; empty = private networks).
  - Behind the reverse proxy, the real visitor address (`X-Forwarded-For`) counts; if it is missing, the access counts
    as external. Forged details are ignored.
  - A session started internally without a second factor is not valid from outside (new sign-in needed); “Stay signed
    in” then has no effect.
  - The settings page shows whether your own connection counts as internal or external.
- **Unlock accounts without the command line:** new section *Sign-in and security* in the user account with “Reset
  two-factor authentication” and “Lift sign-in lock”; marker and filter “Sign-in lock” in the user list. Resetting also
  lifts an existing lock.
- The sign-in lock after failed attempts is now stored in the database – so it applies to all server processes
  together and can be lifted reliably.

## 2.4.0 – 2026-10-19
- **Automatic backup** (*Administration → Backup*): daily from an adjustable time (default 2 a.m.), during operation,
  with an integrity check of every backup. The **newest 10** are kept (adjustable 1–100). “Back up now”, download,
  delete; warning sign in the menu and email to admins if a backup fails. Stored in `./data/backups` or
  `CATALYTIX_BACKUP_DIR` (e.g. NAS).
- Commands `flask backup`, `flask list-backups`, `flask restore-backup <datei>` (backs up the current state first).
- **Old project name removed completely** – from code, interface, documentation and compatibility layers:
  - The database file is now only called `catalytix.db`. If a different `.db` file is in the data folder instead, the
    program does not start (protection against an empty restart) and names the rename command.
  - Old variable names in `.env` are no longer read – only `CATALYTIX_…`.
  - New internal key for authenticator apps: apps already set up must be set up again once. Affected users get a
    clear notice on sign-in (sign in with a recovery code or security key, then *Security → Set up app again*; or an
    admin resets 2FA). Security keys (YubiKey) work unchanged.

## 2.3.1 – 2026-10-18
- `docker-compose.yml`: label `dockhand.update=false` so that Dockhand does not update the container automatically
  (the image is built locally; updates come as a new package).

## 2.3.0 – 2026-10-17
- **No program name in the interface any more.** The sign-in page, sidebar, emails, authenticator app and browser tab
  only show the **library name** (*Settings*). “Catalytix” remains the project name (GitHub, README, Docker image) and
  only appears on the *About this program* page (licence/source code).
- Long library names wrap in the sidebar instead of being cut off; on mobile the name is now shown above the sign-in.
- The old default name “CATALYTIX” becomes “Bibliothek”; custom names stay.
- Branding: the option is now called “Show the library name next to the logo”.

## 2.2.0 – 2026-10-17
- **Automatic retention period** (GDPR, storage limitation) under *Settings → Retention*: completed loans are deleted
  3/6/**12**/24/36 months after return (default 12, “never” possible). The count per book is kept (“x entries · y older
  ones without details”).
- Entries in the email log are deleted after the same period – except entries for current loans (they record how many
  overdue notices have already been sent).
- Current and overdue loans are never deleted.
- Runs automatically once a day (even without email delivery set up), plus “Clean up now” and the command
  `flask apply-retention`. Result of the last run in the settings.
- Profile: notice of when your own loan history is deleted.
- When a user is deleted, the books' loan counts are kept.
- The database gets the column `books.archived_loans` automatically on startup.

## 2.1.0 – 2026-10-17
Privacy for running on the internet – users' browsers now connect only to the library's own server:
- **Fonts from the own server** instead of Google Fonts (Inter and Fraunces included, SIL Open Font License).
- **Built-in barcode recognition** for Firefox and Safari instead of the ZXing library loaded from unpkg.com (Chrome,
  Edge and Android continue to use the browser's built-in recognition). A code is only accepted once it has been read
  the same way twice in a row.
- **Cover images always via the own server:** books without a locally stored cover fetch it server-side on first
  access; the preview when adding books also goes through the server. Only public addresses are fetched (protecting
  internal servers), only JPG/PNG/WebP/GIF. The setting “Cover lokal speichern” (store covers locally) is removed.
- **Legal notice and privacy policy** under *Settings → Legal*; links on the sign-in page, in the sidebar and in all
  emails. The previous privacy address of self-registration is carried over.
- README: new section “Privacy and running on the internet”.

## 2.0.0 – 2026-10-17
- **Project name Catalytix** – in the interface, emails, Docker image (`catalytix:2.0.0`) and Python package
  (`catalytix`).
- **Free software under the GNU AGPL v3 or later** (file `LICENSE`).
- New page **“About Catalytix”** (click the version number in the sidebar): version, licence, licence text and link to
  the source code (`CATALYTIX_SOURCE_URL`). Only visible after signing in.
- The Docker service is now called `catalytix` – run `docker compose down` before the update (see README).
- English README (`README.en.md`).

## 1.6.0 – 2026-10-17
- **Self-registration** (off by default) under *Settings*: off / with approval by an admin / active immediately.
  - optional email confirmation (link, 48 h), notification of admins, allowed email domains, default group for new
    readers, mandatory acceptance of the privacy notice.
  - New accounts are always readers. Approve/reject directly in the user list; notice on the overview and counter in
    the navigation.
  - Protection: bot trap, minimum time to fill in, limit per IP, reserved names, AD usernames blocked, password at least
    10 characters; unconfirmed accounts are deleted after 7 days.
- New emails: confirm email, account activated, new registration (to admins).
- User list: filter by status (awaiting approval, self-registered, locked).

## 1.5.0 – 2026-10-17
- **Branding / corporate design** (*Administration → Branding*) with live preview:
  - custom **logo** (PNG/JPG/WebP/GIF/SVG) in the sidebar, sign-in page (also mobile) and emails; favicon generated
    automatically; optionally without lettering and on a light background.
  - custom **background image** for the sign-in page with adjustable darkening; images are scaled down, metadata (GPS)
    removed.
  - custom **colours** (main, accent, sidebar colour) with automatically readable text colours, also in dark mode.
  - custom **welcome texts**, bookshelf graphic can be switched off, “Reset everything”.
- Emails adopt the logo and colours.
- Security: SVG logos with scripts/external content are rejected and served only with a strict CSP.
- New dependency: `Pillow` (image processing).

## 1.4.1 – 2026-10-17
- Sidebar: new entries **“My profile”** and **“Security”** for all users (previously only reachable via the small
  name initials at the bottom left or the mobile menu). Notice “2FA off” as long as no second factor is active.
- The name at the bottom left is now also a link to the profile.

## 1.4.0 – 2026-10-17
- **Two-factor authentication**
  - Authenticator app (TOTP, RFC 6238) with QR code; secrets stored encrypted, codes not reusable.
  - **Security keys** (YubiKey & other FIDO2 keys, passkeys) via WebAuthn; several keys per account.
  - 10 recovery codes (one-time codes), can be regenerated.
  - Requirement adjustable: nobody / administrators / librarians + admins / everyone – with guided setup.
  - An admin can reset a user's second factor; emergency command `flask reset-2fa`.
  - Failed-attempt lock for the second step too; the sign-in step expires after 5 minutes.
- New page *Profile → Security*; 2FA marker in the user list.
- New dependencies: `fido2` (Yubico), `segno` (QR codes).

## 1.3.3 – 2026-10-17
- **Signing out on a smartphone:** new user menu at the top right (profile, light/dark, sign out).
- Bug fixed: on phones the expandable menu was taller than the visible area (address bar), so the sign-out area was
  off-screen at the bottom.
- The “+ Buch” (+ book) button in the header shows only an icon on mobile to save space.

## 1.3.2 – 2026-10-16
- **Reverse proxy is detected automatically** – no setting needed any more. Proxy headers are only accepted from
  private networks (cannot be forged from outside); adjustable via `CATALYTIX_TRUSTED_PROXIES`.
- **Secure cookies automatically** with HTTPS; signing in via `http://IP:9300` on the LAN still works.
- **`docker-compose.yml` no longer needs to be edited.** Your own values (port, time zone, PUID …) go in `.env`
  (template `.env.example`), which is never overwritten by updates. Extensions in `docker-compose.override.yml`.
- Note: requires Docker Compose 2.24 or newer (because of the optional `.env`).

## 1.3.1 – 2026-10-16
Sign-in page made more neutral for running on the internet (behind a reverse proxy):
- Notice “Anmeldung über Active Directory ist aktiv” (sign-in via Active Directory is active) and domain hints removed.
- Neutral welcome text instead of a description of features.
- No version number on the sign-in page and in publicly visible file links any more (after sign-in still at the
  bottom left of the sidebar).
- Uniform error message on sign-in: from outside it is no longer possible to tell whether an account exists, is locked
  or has not been approved (details are still in the server log).

## 1.3.0 – 2026-10-16
- **Email notifications via SMTP** (STARTTLS, SSL/TLS or unencrypted, with/without authentication): confirmation on
  loan and return, reminder before the due date, overdue notices with adjustable repetition and maximum number, daily
  summary of overdue loans to librarians.
- Daily delivery at the set time; several books per reader in one email; no duplicate sending (even with several
  server processes). “Send now” button and command `flask send-reminders`.
- New page *Administration → Email* with test email and delivery log.
- Manual reminder with one click on every loan (*All loans*).
- Users can turn off notifications in their profile; local users maintain their address there.
- The database is extended with new columns automatically on startup – update without data loss.
- Bug fixed: templates could not be rendered outside a web request (background delivery).

## 1.2.1 – 2026-10-16
Reworked group resolution in Active Directory:
- **Primary group is detected** (e.g. *Domain Users*). AD does not list it in `memberOf`, so it was previously
  overlooked – groups containing it were missing too.
- **Groups outside the user OU are found.** The search now starts at the domain root (`DC=…`) instead of only below the
  base DN.
- **Nested groups via two routes:** `LDAP_MATCHING_RULE_IN_CHAIN` and additionally `tokenGroups` (the groups from the
  Windows sign-in token). If one route fails (older Samba version, restricted permissions), the other takes over –
  previously nested groups were then silently lost.
- Groups are read with the signed-in user's connection (which may always read its own `tokenGroups`); service account
  as a fallback.
- OpenLDAP / classic Samba domains: nested `groupOfNames` groups are now resolved as well.
- **“Test sign-in” shows the origin of each group:** direct, primary or nested.
- “Load groups” also searches from the domain root.

## 1.2.0 – 2026-10-16
- **Roles via AD groups:** administrators, librarians and readers are now simply defined by the **name of an AD
  group** (e.g. `Bibliothek-Admins`) – no separate OU needed any more. Group DNs and OUs still work; existing settings
  remain valid.
- **Select groups from AD:** on the *Active Directory* page, AD groups can be loaded, filtered and adopted as admin,
  librarian or reader with a click.
- Library groups can also be linked to an AD group by group name (instead of DN).
- Version number in the interface, in the Docker image (`catalytix:1.2.0`) and in the package file name.

## 1.1.0
- **Fetch the TLS certificate automatically from the domain controller** (LDAPS/StartTLS), including adding the root
  CA via AIA or from AD; display of the fingerprint and confirmation by the admin.
- Option “Verify hostname”; understandable error message for an untrusted certificate.
- Docker image on Python 3.13; new dependency `cryptography`.

## 1.0.2
- Docker: permissions on the data folder are set automatically on startup (`PUID`/`PGID`), the app does not run as
  root.
- Clear error message if the data directory is not writable.

## 1.0.1
- Default port changed to **9300**.

## 1.0.0
- First version: catalogue, ISBN capture (scanner, camera, DNB/Open Library/Google Books), quick capture, lending and
  circulation desk, groups and roles, local users, Active Directory integration, SQLite (optional MySQL/PostgreSQL),
  Docker.
