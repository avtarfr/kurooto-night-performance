#!/usr/bin/env bash
# Installs kurooto-legion-night (beside this file) to /usr/local/bin as root, and lets the account that ran sudo run
# exactly its on, off and status lines without a password, so Kurooto's Night performance add-on can put a Lenovo
# Legion or LOQ (Linux, the legion_laptop driver from LenovoLegionLinux) into its night mode and back.
#
#   Install:  sudo bash install.sh
#   Undo:     sudo rm /usr/local/bin/kurooto-legion-night /etc/sudoers.d/kurooto-legion-night
#
# The sudo rule is checked with visudo before it's put in place; nothing is installed when it doesn't check out.
set -uo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
HELPER=/usr/local/bin/kurooto-legion-night
RULE=/etc/sudoers.d/kurooto-legion-night

[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo: sudo bash $0"; exit 1; }
WHO=${SUDO_USER:-}
if [ -z "$WHO" ] || [ "$WHO" = root ]; then
  echo "Run it with sudo from your own account (the one Kurooto runs as), not as root."
  exit 1
fi
[ -d /sys/bus/platform/drivers/legion ] || {
  echo "This computer has no legion_laptop driver (LenovoLegionLinux); nothing installed."
  exit 1
}
HERE=$(cd "$(dirname "$0")" && pwd)
[ -f "$HERE/kurooto-legion-night" ] || { echo "kurooto-legion-night isn't beside this script; nothing installed."; exit 1; }

TMP=$(mktemp) || exit 1
trap 'rm -f "$TMP"' EXIT
{
  echo "# Kurooto's Night performance add-on: only these lines, without a password. Undo: sudo rm $RULE $HELPER"
  printf '%s ALL=(root) NOPASSWD: ' "$WHO"
  printf '%s on custom fan, %s on custom nofan, %s on performance fan, %s on performance nofan, %s off, %s status\n' \
    "$HELPER" "$HELPER" "$HELPER" "$HELPER" "$HELPER" "$HELPER"
} > "$TMP"
visudo -cf "$TMP" > /dev/null || { echo "The sudo rule didn't check out; nothing installed."; exit 1; }
install -o root -g root -m 0755 "$HERE/kurooto-legion-night" "$HELPER" || exit 1
install -o root -g root -m 0440 "$TMP" "$RULE" || { rm -f "$HELPER"; exit 1; }
echo "Installed. Kurooto runs (and so can you, without a password):"
echo "  sudo -n $HELPER status"
sudo -u "$WHO" sudo -n "$HELPER" status
