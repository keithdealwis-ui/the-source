#!/bin/sh
# Install The Source MCP on a host (KEI-851). Run as root. $1 = a git bundle or repo of the verified commit,
# $2 = that commit. Re-running upgrades the code to a new verified commit; data keeps refreshing from main.
set -eu
SRC=$1; COMMIT=$2
id the-source-mcp >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin the-source-mcp
mkdir -p /opt/the-source-mcp
TMP=$(mktemp -d)
git clone -q "$SRC" "$TMP/src"
git -C "$TMP/src" checkout -q "$COMMIT"
rm -rf /opt/the-source-mcp/app.new && mkdir -p /opt/the-source-mcp/app.new
cp -r "$TMP/src/source_pipeline" /opt/the-source-mcp/app.new/
echo "$COMMIT" > /opt/the-source-mcp/app.new/COMMIT
rm -rf /opt/the-source-mcp/app && mv /opt/the-source-mcp/app.new /opt/the-source-mcp/app
rm -rf "$TMP"
chown -R root:root /opt/the-source-mcp/app && chmod -R a+rX,go-w /opt/the-source-mcp/app
if [ ! -d /opt/the-source-mcp/data/.git ]; then
  git clone -q --filter=blob:none --sparse https://github.com/keithdealwis-ui/the-source.git /opt/the-source-mcp/data
fi
# Re-applied on every install so an upgrade picks up new published layers (KEI-912: data/corpus).
git -C /opt/the-source-mcp/data sparse-checkout set data/canonical api/v1 data/momentum data/radar data/corpus
chown -R the-source-mcp:the-source-mcp /opt/the-source-mcp/data
D=$(dirname "$0")
install -m 0644 "$D/the-source-mcp.service" "$D/the-source-mcp-refresh.service" "$D/the-source-mcp-refresh.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now the-source-mcp-refresh.timer
systemctl restart the-source-mcp
systemctl enable the-source-mcp
