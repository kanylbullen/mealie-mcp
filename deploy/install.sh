#!/usr/bin/env bash
# Install or upgrade mealie-mcp on the host that runs it. Idempotent.
#   REF=<git sha or tag> ./install.sh
# Creates the service user, a venv under /opt/mealie-mcp, installs the pinned ref and the unit.
# Secrets (/etc/mealie-mcp/secrets.env) and the site drop-in are NOT written here.
set -euo pipefail
REPO="${REPO:-https://github.com/kanylbullen/mealie-mcp}"
REF="${REF:?set REF to a commit sha or tag}"
BASE=/opt/mealie-mcp

id -u mealiemcp >/dev/null 2>&1 || useradd --system --home-dir /var/lib/mealie-mcp --shell /usr/sbin/nologin mealiemcp
install -d -m 755 "$BASE"
install -d -m 750 -o root -g root /etc/mealie-mcp

if [ ! -d "$BASE/src/.git" ]; then
  git clone --quiet "$REPO" "$BASE/src"
fi
git -C "$BASE/src" fetch --quiet origin
git -C "$BASE/src" checkout --quiet --detach "$REF"
echo "$REF" > "$BASE/src/GIT_REF"

[ -x "$BASE/venv/bin/python" ] || python3 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install --quiet --upgrade pip
"$BASE/venv/bin/pip" install --quiet "$BASE/src"

install -m 644 "$BASE/src/deploy/mealie-mcp.service" /etc/systemd/system/mealie-mcp.service
systemctl daemon-reload
echo "installed $("$BASE/venv/bin/pip" show mealie-mcp | awk '/^Version/{print $2}') at $REF"
echo "next: write /etc/mealie-mcp/secrets.env (0600), add a drop-in with MCP_PUBLIC_URL etc., then: systemctl enable --now mealie-mcp"
