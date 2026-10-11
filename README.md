# iPhone SMS Backup

A guided, private iPhone SMS backup for Linux. The first run walks through storage selection, USB trust and pairing, the initial backup, and Wi-Fi verification. Afterward, systemd checks every two minutes and performs an incremental backup whenever the paired iPhone is reachable on the same Wi-Fi.

The raw Apple backup and exported CSV remain on your computer. Nothing is uploaded.

## Supported platforms

- Ubuntu and Debian on x86-64 or ARM64
- Windows is planned, but is not included in this release

## Install

```bash
git clone https://github.com/jdholasca/iphone-sms-backup.git
cd iphone-sms-backup
./install.sh
```

The installer asks for `sudo` only to install OS packages and enable user services at boot. Run it as your normal user, not as root.

During guided setup:

1. Choose a local or externally mounted backup directory.
2. Connect and unlock the iPhone by USB.
3. Tap **Trust** on the iPhone and enter its passcode.
4. Disconnect USB and keep both devices on the same Wi-Fi.
5. Leave the first backup running until it reaches 100%.

The first backup examines the complete Apple backup inventory but retains only the SMS database and the manifest needed for future incremental backups. Its duration depends on total iPhone storage use, Wi-Fi, and storage speed.

## Commands

```bash
iphone-sms-backup status   # completion, percentage, last export and free space
iphone-sms-backup backup   # start an incremental backup now
iphone-sms-backup logs     # latest backup log
iphone-sms-backup setup    # repeat guided setup
iphone-sms-backup android-pair # create a one-time LocalLedger pairing code
```

If `~/.local/bin` is not in the shell path, sign out and back in or run the command using `~/.local/bin/iphone-sms-backup`.

## Automatic incremental backups

`iphone-sms-backup.timer` checks every two minutes and runs once when the phone appears on Wi-Fi. If the phone is absent, it exits without an error. A file lock prevents overlapping backups. iOS may still require the phone to be unlocked or ask for its passcode before allowing a backup; the running job continues after approval.

```bash
systemctl --user status iphone-sms-backup.timer
systemctl --user list-timers iphone-sms-backup.timer
```

The CSV is saved as `exports/messages.csv` under the chosen storage directory. Backup directories use private permissions and the CSV is mode `0600`.

## LocalLedger Android sync

The installer also runs a private HTTPS bridge advertised only on the local network. In LocalLedger, open **Settings → Data → iPhone SMS**, discover this server, and enter the one-time code printed by:

```bash
iphone-sms-backup android-pair
```

The code expires after 10 minutes. After pairing, LocalLedger imports the existing messages once and then requests only messages newer than its saved cursor. The Android app pins this server's certificate, encrypts the pairing token and imported messages with Android Keystore, and checks for new messages in the background about every 15 minutes.

If a firewall blocks discovery or sync, allow mDNS and TCP port `18765` from the server's local subnet only. For this server's current LAN:

```bash
sudo ufw allow from 192.168.2.0/24 to any port 5353 proto udp comment 'Local mDNS'
sudo ufw allow from 192.168.2.0/24 to any port 18765 proto tcp comment 'LocalLedger iPhone SMS'
```

## Uninstall

```bash
systemctl --user disable --now iphone-sms-backup.timer iphone-netmuxd.service iphone-android-bridge.service
rm -f ~/.config/systemd/user/iphone-sms-backup.{service,timer} ~/.config/systemd/user/iphone-netmuxd.service ~/.config/systemd/user/iphone-android-bridge.service
rm -f ~/.local/bin/iphone-sms-backup ~/.local/bin/iphone-backup
rm -rf ~/.local/share/iphone-sms-backup ~/.config/iphone-sms-backup
systemctl --user daemon-reload
```

This intentionally does not delete the selected backup directory.

## Privacy and limitations

- Pairing records and messages are sensitive. Do not share the backup directory.
- The app exports normal SMS/iMessage text and decodes Apple's attributed-body format when the plain text field is empty.
- Automatic backup requires the Linux machine to be running and the iPhone to be reachable on the same local network.
- This is an independent utility and is not affiliated with Apple.

## Components

- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) performs Apple backup operations.
- [netmuxd](https://github.com/jkcoxson/netmuxd) exposes paired Wi-Fi devices to the backup client.
- `iphone_sms_exporter.py` reads the retained SMS database and writes a formula-safe CSV.

## Windows roadmap

The Windows version will keep the same guided flow and backup format. It still needs a Windows service/tray host, Apple Mobile Device drivers, Windows pairing-record handling, and installer signing. No Windows binary is claimed by this Linux release.
