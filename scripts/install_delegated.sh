#!/bin/bash
# Install the open-source MCP servers that RUDDER delegates to, standalone (no agent host needed).
# Each server gets its own virtualenv (their dependency pins differ).
#
#   scripts/install_delegated.sh [multivendor] [fortinet] [pyats]      (default: all three)
#
# Env: DELEGATED_HOME (default ~/.rudder/delegated), PYTHON (default python3),
#      SERVERS_REPO / SERVERS_REF: the repository that holds multivendor-cli-mcp and fortinet-mcp under
#        mcp-servers/, and the commit tested (a4905469); checked out to $DELEGATED_HOME/src
#      PYATS_MCP_REPO / PYATS_MCP_REF: the pyATS MCP server (default: its HEAD)
set -euo pipefail
HOME_DIR="${DELEGATED_HOME:-$HOME/.rudder/delegated}"
PY="${PYTHON:-python3}"
SERVERS_REPO="${SERVERS_REPO:?set SERVERS_REPO to the git URL of the open-source MCP servers repository}"
SERVERS_REF="${SERVERS_REF:-a4905469}"
PYATS_MCP_REPO="${PYATS_MCP_REPO:-https://github.com/automateyournetwork/pyATS_MCP.git}"
SRC="$HOME_DIR/src"
WHAT="${*:-multivendor fortinet pyats}"
mkdir -p "$HOME_DIR/venvs"; chmod 700 "$HOME_DIR"

need_src() {
  if [ ! -d "$SRC/.git" ]; then
    git clone -q --filter=blob:none "$SERVERS_REPO" "$SRC"
  fi
  git -C "$SRC" fetch -q origin
  git -C "$SRC" checkout -q "$SERVERS_REF"
  echo "server sources at $(git -C "$SRC" log --oneline -1)"
}

venv() {   # venv NAME REQUIREMENTS...
  local v="$HOME_DIR/venvs/$1"; shift
  [ -x "$v/bin/python" ] || "$PY" -m venv "$v"
  "$v/bin/python" -m pip install -q --upgrade pip
  "$v/bin/python" -m pip install -q "$@"
  echo "$v ready"
}

for w in $WHAT; do
  case "$w" in
    multivendor) need_src; venv multivendor -r "$SRC/mcp-servers/multivendor-cli-mcp/requirements.txt" ;;
    fortinet)    need_src; venv fortinet -r "$SRC/mcp-servers/fortinet-mcp/requirements.txt" ;;
    pyats)
      if [ ! -d "$HOME_DIR/pyATS_MCP/.git" ]; then
        git clone -q "$PYATS_MCP_REPO" "$HOME_DIR/pyATS_MCP"
      fi
      [ -n "${PYATS_MCP_REF:-}" ] && git -C "$HOME_DIR/pyATS_MCP" checkout -q "$PYATS_MCP_REF"
      echo "pyATS_MCP at $(git -C "$HOME_DIR/pyATS_MCP" log --oneline -1)"
      req="$HOME_DIR/pyATS_MCP/requirements.txt"
      if [ -f "$req" ]; then venv pyats -r "$req"; else venv pyats "pyats[full]" mcp python-dotenv; fi ;;
    *) echo "unknown: $w"; exit 2 ;;
  esac
done
