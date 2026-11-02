#!/bin/sh
# Install the GitWatch MCP adapter (KEI-932). Run as root. $1 = a git bundle or repo of the commit, $2 = that commit.
# Re-running upgrades the code; state, clients, tokens, the passphrase hash and the credential are kept.
# The credential (/etc/the-source-gitwatch-mcp/gitwatch.token, root 0600) and the passphrase are provisioned
# separately, never through this script; nginx is changed by hand (nginx.conf), after `nginx -t`.
set -eu
SRC=$1; COMMIT=$2
id the-source-gitwatch >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin the-source-gitwatch
TMP=$(mktemp -d)
git clone -q "$SRC" "$TMP/src"
git -C "$TMP/src" checkout -q "$COMMIT"
rm -rf /opt/the-source-gitwatch-mcp/app.new && mkdir -p /opt/the-source-gitwatch-mcp/app.new/source_pipeline
cp "$TMP/src/source_pipeline/__init__.py" "$TMP/src/source_pipeline/gitwatch_mcp.py" /opt/the-source-gitwatch-mcp/app.new/source_pipeline/
echo "$COMMIT" > /opt/the-source-gitwatch-mcp/app.new/COMMIT
rm -rf /opt/the-source-gitwatch-mcp/app.prev && { [ ! -d /opt/the-source-gitwatch-mcp/app ] || mv /opt/the-source-gitwatch-mcp/app /opt/the-source-gitwatch-mcp/app.prev; }
mv /opt/the-source-gitwatch-mcp/app.new /opt/the-source-gitwatch-mcp/app
rm -rf "$TMP"
chown -R root:root /opt/the-source-gitwatch-mcp && chmod -R a+rX,go-w /opt/the-source-gitwatch-mcp
install -d -m 0700 /etc/the-source-gitwatch-mcp
install -d -o the-source-gitwatch -g the-source-gitwatch -m 0700 /var/lib/the-source-gitwatch-mcp
install -m 0644 "$(dirname "$0")/the-source-gitwatch-mcp.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable the-source-gitwatch-mcp
if [ -s /etc/the-source-gitwatch-mcp/gitwatch.token ]; then systemctl restart the-source-gitwatch-mcp; else echo "credential not provisioned yet: service enabled, not started"; fi
