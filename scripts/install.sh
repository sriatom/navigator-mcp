#!/usr/bin/env bash
# NAVIGATOR in one step, on the Linux host that reaches your devices.
#
#   curl -fsSL https://navigator.atomidea.com/install.sh | bash                       NAVIGATOR only
#   curl -fsSL https://navigator.atomidea.com/install.sh | bash -s -- --hermes        + a Hermes chat bot
#   curl -fsSL https://navigator.atomidea.com/install.sh | bash -s -- --cursor        + an "Add to Cursor" link
#
# Options: --hermes [PROFILE]   --approvals PROFILE   --cursor [user@host]   --claude
#          --version X.Y.Z   --batfish   --no-setup   --no-services   --dir DIR (default ~/navigator)
# Runs as your user (no sudo, except the optional linger line it prints). Running it again upgrades in place.
# Every step it takes is on the Install page, step by step: https://navigator.atomidea.com/INTEGRATION/
set -euo pipefail

DIR="$HOME/navigator"; VERSION=""; EXTRA=""; SETUP=1; SERVICES=1
HERMES=""; APPROVALS=""; CURSOR=""; CURSOR_HOST=""; CLAUDE=""
REPO="https://github.com/sriatom/navigator-mcp"

while [ $# -gt 0 ]; do
  case "$1" in
    --hermes) HERMES=navigator; if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then HERMES="$2"; shift; fi ;;
    --approvals) APPROVALS="${2:?--approvals needs a profile name}"; shift ;;
    --cursor) CURSOR=1; if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then CURSOR_HOST="$2"; shift; fi ;;
    --claude) CLAUDE=1 ;;
    --version) VERSION="${2:?--version needs X.Y.Z}"; shift ;;
    --batfish) EXTRA="[batfish]" ;;
    --no-setup) SETUP=0 ;;
    --no-services) SERVICES=0 ;;
    --dir) DIR="${2:?--dir needs a folder}"; shift ;;
    -h|--help) sed -n '2,12p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\n== %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

say "1/5 Python"
PY=$(command -v python3 || true)
[ -n "$PY" ] || die "python3 is missing. Ubuntu/Debian: sudo apt-get install -y python3 python3-venv curl"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python 3.11 or newer is needed ($("$PY" -V))"
"$PY" -c 'import venv, ensurepip' 2>/dev/null || die "the venv module is missing. Ubuntu/Debian: sudo apt-get install -y python3-venv"
command -v curl >/dev/null || die "curl is missing. Ubuntu/Debian: sudo apt-get install -y curl"
echo "$("$PY" -V) at $PY"

say "2/5 navigator-mcp into $DIR/.venv"
mkdir -p "$DIR"
[ -x "$DIR/.venv/bin/python" ] || "$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
SPEC="navigator-mcp$EXTRA"; [ -n "$VERSION" ] && SPEC="$SPEC==$VERSION"
SPEC="${NAVIGATOR_PIP_SPEC:-$SPEC}"                 # testing only: a wheel file instead of PyPI
"$DIR/.venv/bin/pip" install -q --upgrade "$SPEC"
V=$("$DIR/.venv/bin/pip" show navigator-mcp | sed -n 's/^Version: //p')
echo "navigator-mcp $V"
mkdir -p "$HOME/.local/bin"
for c in navigator-setup; do ln -sf "$DIR/.venv/bin/$c" "$HOME/.local/bin/$c"; done

say "3/5 service files and Hermes plugins for v$V"
ARCHIVE="${NAVIGATOR_ARCHIVE:-$REPO/archive/refs/tags/v$V.tar.gz}"   # testing only: a local tarball
{ case "$ARCHIVE" in http*) curl -fsSL "$ARCHIVE" ;; *) cat "$ARCHIVE" ;; esac; } \
  | tar -xz -C "$DIR" --strip-components=1 --wildcards '*/scripts/*.service' '*/integrations/*'
ls "$DIR/scripts"/*.service >/dev/null && echo "in $DIR/scripts and $DIR/integrations"

say "4/5 settings"
if [ "$SETUP" = 1 ] && ( : < /dev/tty ) 2>/dev/null; then
  "$DIR/.venv/bin/navigator-setup" < /dev/tty || echo "navigator-setup stopped; run it again any time: navigator-setup"
else
  echo "skipped (no terminal, or --no-setup). Run it later: navigator-setup"
fi
if [ "$SERVICES" = 1 ] && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cp "$DIR/scripts/atlas-link.service" "$DIR/scripts/rudder-scheduler.service" "$HOME/.config/systemd/user/"
  systemctl --user daemon-reload
  systemctl --user enable --now atlas-link rudder-scheduler >/dev/null 2>&1 \
    && echo "atlas-link and rudder-scheduler: running" \
    || echo "services installed; start them after navigator-setup: systemctl --user enable --now atlas-link rudder-scheduler"
  loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes' \
    || echo "to keep them running after you log out: sudo loginctl enable-linger $USER"
else
  echo "services skipped (no systemd user session, or --no-services)"
fi

say "5/5 agent host"
SETUP_BIN="$DIR/.venv/bin/navigator-setup"
if [ -n "$HERMES" ]; then
  if [ -n "$APPROVALS" ]; then "$SETUP_BIN" connect hermes --profile "$HERMES" --approvals "$APPROVALS"
  else "$SETUP_BIN" connect hermes --profile "$HERMES"; fi
fi
if [ -n "$CURSOR" ]; then
  if [ -n "$CURSOR_HOST" ]; then "$SETUP_BIN" connect cursor --ssh-host "$CURSOR_HOST"
  else "$SETUP_BIN" connect cursor; fi
fi
[ -n "$CLAUDE" ] && "$SETUP_BIN" connect claude
[ -z "$HERMES$CURSOR$CLAUDE" ] && echo "none chosen. Later: navigator-setup connect hermes | cursor | claude"

printf '\nDone: navigator-mcp %s in %s. Commands: %s/.venv/bin (navigator-setup is also in ~/.local/bin).\n' "$V" "$DIR" "$DIR"
