#!/bin/sh
# Install the waiting room on a host (KEI-932). Run as root. $1 = a git bundle or repo of the verified commit,
# $2 = that commit. Re-running upgrades the code; state, credentials and the signing key are kept.
# nginx is changed separately and by hand (nginx.conf), after `nginx -t`.
set -eu
SRC=$1; COMMIT=$2
id the-source-intake >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin the-source-intake
mkdir -p /opt/the-source-intake /var/lib/the-source-intake
TMP=$(mktemp -d)
git clone -q "$SRC" "$TMP/src"
git -C "$TMP/src" checkout -q "$COMMIT"
rm -rf /opt/the-source-intake/app.new && mkdir -p /opt/the-source-intake/app.new/api
cp -r "$TMP/src/source_pipeline" /opt/the-source-intake/app.new/
cp -r "$TMP/src/api/intake" /opt/the-source-intake/app.new/api/
echo "$COMMIT" > /opt/the-source-intake/app.new/COMMIT
rm -rf /opt/the-source-intake/app.prev && { [ ! -d /opt/the-source-intake/app ] || mv /opt/the-source-intake/app /opt/the-source-intake/app.prev; }
mv /opt/the-source-intake/app.new /opt/the-source-intake/app
rm -rf "$TMP"
chown -R root:root /opt/the-source-intake/app && chmod -R a+rX,go-w /opt/the-source-intake/app
mkdir -p /var/lib/the-source-intake/state
chown the-source-intake:the-source-intake /var/lib/the-source-intake /var/lib/the-source-intake/state
chmod 0750 /var/lib/the-source-intake /var/lib/the-source-intake/state
# Git in the service-owned clone runs as the service user, never as root (no safe.directory override).
AS="runuser -u the-source-intake --"
if [ ! -d /var/lib/the-source-intake/repo/.git ]; then
  $AS git clone -q --filter=blob:none --sparse https://github.com/keithdealwis-ui/the-source.git /var/lib/the-source-intake/repo
fi
$AS git -C /var/lib/the-source-intake/repo sparse-checkout set data/corpus data/canonical data/dataset data/intake
# The batch signing key: generated here once, root-owned, handed only to the seal unit. Its public half is
# committed to config/waiting_room_allowed_signers, which is what the Waiting room workflow verifies against.
install -d -m 0700 /etc/the-source-intake
[ -f /etc/the-source-intake/signing_key ] || ssh-keygen -q -t ed25519 -N "" -C waiting-room@the-source -f /etc/the-source-intake/signing_key
chmod 0600 /etc/the-source-intake/signing_key
D=$(dirname "$0")
install -m 0644 "$D/the-source-intake.service" "$D/the-source-intake-seal.service" "$D/the-source-intake-seal.timer" \
  "$D/the-source-intake-reconcile.service" "$D/the-source-intake-reconcile.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now the-source-intake-seal.timer the-source-intake-reconcile.timer
systemctl restart the-source-intake
systemctl enable the-source-intake
echo "public key for config/waiting_room_allowed_signers:"
echo "waiting-room@the-source namespaces=\"the-source-waiting-room\" $(cat /etc/the-source-intake/signing_key.pub)"
