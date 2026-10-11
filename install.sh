#!/usr/bin/env bash
set -euo pipefail

[[ $EUID -ne 0 ]] || { echo "Run this installer as your normal user, not root." >&2; exit 1; }
[[ $(uname -s) == Linux ]] || { echo "This release supports Linux only. Windows support is planned." >&2; exit 1; }
command -v apt-get >/dev/null || { echo "This installer currently supports Ubuntu and Debian." >&2; exit 1; }

source_dir=$(cd "$(dirname "$0")" && pwd)
install_dir="$HOME/.local/share/iphone-sms-backup"
bin_dir="$HOME/.local/bin"
unit_dir="$HOME/.config/systemd/user"

echo "Installing Linux requirements (sudo will ask for your password)..."
sudo apt-get update
sudo apt-get install -y ca-certificates curl libimobiledevice-utils python3-venv tar usbmuxd util-linux
sudo loginctl enable-linger "$USER"

mkdir -p "$install_dir/bin" "$bin_dir" "$unit_dir"
python3 -m venv "$install_dir/venv"
"$install_dir/venv/bin/pip" install --upgrade pip
"$install_dir/venv/bin/pip" install -r "$source_dir/requirements.txt"

machine=$(uname -m)
case $machine in
  x86_64)
    asset=netmuxd-x86_64-unknown-linux-gnu.tar.gz
    checksum=85b6598284fc639f2a282584461d05e2090b79bdf3ec949d2a5e5d3dc655dde4
    ;;
  aarch64|arm64)
    asset=netmuxd-aarch64-unknown-linux-gnu.tar.gz
    checksum=a89d587b094184b41431bc343da6dfa890391ccc7d4b3d6bce02b524f3191ae6
    ;;
  *) echo "Unsupported Linux architecture: $machine" >&2; exit 1 ;;
esac
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
curl -fL "https://github.com/jkcoxson/netmuxd/releases/download/v0.4.3/$asset" -o "$archive"
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status || { echo "netmuxd checksum verification failed" >&2; exit 1; }
tar -xzf "$archive" -C "$install_dir/bin" netmuxd
chmod 0755 "$install_dir/bin/netmuxd"

install -m 0755 "$source_dir/iphone_backup_app.py" "$install_dir/"
install -m 0755 "$source_dir/iphone_sms_exporter.py" "$install_dir/"
install -m 0755 "$source_dir/android_bridge.py" "$install_dir/"
install -m 0755 "$source_dir/iphone-backup" "$install_dir/"
install -m 0755 "$source_dir/sync.sh" "$install_dir/"
install -m 0644 "$source_dir/systemd/iphone-netmuxd.service" "$unit_dir/"
install -m 0644 "$source_dir/systemd/iphone-sms-backup.service" "$unit_dir/"
install -m 0644 "$source_dir/systemd/iphone-sms-backup.timer" "$unit_dir/"
install -m 0644 "$source_dir/systemd/iphone-android-bridge.service" "$unit_dir/"
ln -sfn "$install_dir/iphone-backup" "$bin_dir/iphone-sms-backup"
ln -sfn "$install_dir/iphone-backup" "$bin_dir/iphone-backup"
systemctl --user daemon-reload

echo
echo "Installation complete. Starting guided setup..."
if [[ ${1:-} != --no-setup ]]; then
  "$bin_dir/iphone-sms-backup" setup
fi
