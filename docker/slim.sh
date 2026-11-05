#!/bin/sh
# Remove Debian packages the running program never uses (package manager, Perl, user/login tools, PAM, shell
# utilities). Runs once at the end of the image build, after everything is installed.
#
# Why: fewer packages = fewer vulnerability findings that do not concern the program, and nothing left in the
# container that an intruder could use to install software or run Perl.
#
# How: dpkg --purge, so the package database stays correct – Trivy still sees exactly what is in the image
# (nothing is hidden). Not apt-get: apt would also remove packages that are needed (ca-certificates, tzdata,
# util-linux for setpriv, dpkg) because of declared dependencies that only matter for installing.
#
# Kept on purpose (checked below, the build stops if one is missing):
# - Python's libraries (libssl3t64, libsqlite3-0, zlib1g, libffi8, libbz2-1.0, liblzma5, …), libstdc++6 and
#   libgcc-s1 for compiled wheels;
# - openssl: owns /usr/lib/ssl/certs and /usr/lib/ssl/cert.pem – without them Python finds no CA certificates
#   (HTTPS, LDAPS, SMTP with TLS would fail);
# - ca-certificates, tzdata, netbase, base-files, base-passwd: data files;
# - dash, coreutils, findutils, util-linux (setpriv): used by docker-entrypoint.sh;
# - dpkg: keeps the package database readable for scanners and for the package list in the image.
set -eu

# Removal order matters: maintainer scripts of the first groups still need debconf and perl, so those go last.
G1="adduser passwd login login.defs libpam-modules libpam-modules-bin libpam-runtime libpam0g libaudit1
libaudit-common libsemanage2 libsemanage-common libsepol2 liblastlog2-2 libdebconfclient0"
G2="apt libapt-pkg7.0 sqv debian-archive-keyring libxxhash0 libsystemd0 libudev1"
G3="mount bsdutils hostname sysvinit-utils init-system-helpers ncurses-bin gzip"
G4="debconf"
G5="perl-base tar"   # dpkg refuses to start without tar – so tar goes in the very last run
KEEP="dpkg dash coreutils findutils util-linux libc6 libssl3t64 openssl ca-certificates tzdata netbase base-files
base-passwd libsqlite3-0 zlib1g libffi8 libbz2-1.0 liblzma5 libzstd1 libuuid1 libcap-ng0 libselinux1 libpcre2-8-0
libmd0 libgcc-s1 libstdc++6 libgdbm6t64 libdb5.3t64 libncursesw6 libtinfo6 libreadline8t64"

installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'; }

for group in "$G1" "$G2" "$G3" "$G4" "$G5"; do
    present=""
    for p in $group; do installed "$p" && present="$present $p"; done
    [ -n "$present" ] || continue
    echo "slim: removing$present"
    # shellcheck disable=SC2086 # word splitting intended
    dpkg --purge --force-remove-essential --force-remove-protected --force-depends $present
done

# Leftovers of apt and debconf (caches, configuration, state)
rm -rf /etc/apt /var/lib/apt /var/cache/apt /var/log/apt /var/cache/debconf /var/lib/debconf /usr/share/perl5

fail=0
for p in $KEEP; do
    installed "$p" || { echo "slim: required package $p is missing" >&2; fail=1; }
done
for p in $G1 $G2 $G3 $G4 $G5; do
    installed "$p" && { echo "slim: $p is still installed" >&2; fail=1; }
done
for cmd in apt apt-get perl tar gzip passwd login mount; do
    command -v "$cmd" >/dev/null 2>&1 && { echo "slim: $cmd is still available" >&2; fail=1; }
done
[ -e /usr/lib/ssl/cert.pem ] && [ -d /usr/lib/ssl/certs ] || { echo "slim: CA certificate paths missing" >&2; fail=1; }
command -v setpriv >/dev/null 2>&1 || { echo "slim: setpriv missing (needed by docker-entrypoint.sh)" >&2; fail=1; }
exit "$fail"
