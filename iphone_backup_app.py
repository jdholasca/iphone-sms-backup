#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


APP_NAME = "iPhone SMS Backup"
CONFIG_DIR = Path.home() / ".config/iphone-sms-backup"
CONFIG_PATH = CONFIG_DIR / "config.json"
INSTALL_DIR = Path(__file__).resolve().parent
PMD = INSTALL_DIR / "venv/bin/pymobiledevice3"
NETMUXD = INSTALL_DIR / "bin/netmuxd"
USBSOCKET = "127.0.0.1:27015"
UDID_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def command(args, *, capture=False, check=True, env=None):
    return subprocess.run(
        [str(value) for value in args], text=True, capture_output=capture, check=check, env=env
    )


def load_config():
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"setup is incomplete ({error}); run iphone-sms-backup setup") from error
    storage = Path(config.get("storage", ""))
    udid = config.get("udid", "")
    if not storage.is_absolute() or not UDID_PATTERN.fullmatch(udid):
        raise RuntimeError("the saved configuration is invalid; run iphone-sms-backup setup")
    return {"storage": storage, "udid": udid}


def save_config(storage, udid):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"storage": str(storage), "udid": udid}, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(CONFIG_PATH)


def pmd(*args, capture=False, check=True, network=False):
    if not PMD.is_file():
        raise RuntimeError("pymobiledevice3 is not installed; run ./install.sh")
    command_args = [PMD, *args]
    if network:
        command_args.extend(("--usbmux", USBSOCKET))
    return command(command_args, capture=capture, check=check)


def usb_udids():
    result = pmd("usbmux", "list", "--usb", "--simple", capture=True, check=False)
    return parse_udids(result.stdout)


def network_udids():
    result = pmd("usbmux", "list", "--network", "--simple", capture=True, check=False, network=True)
    return parse_udids(result.stdout)


def parse_udids(output):
    try:
        values = json.loads(output)
        if isinstance(values, list):
            return [value for value in values if isinstance(value, str) and UDID_PATTERN.fullmatch(value)]
    except json.JSONDecodeError:
        pass
    return [line.strip() for line in output.splitlines() if UDID_PATTERN.fullmatch(line.strip())]


def copy_pair_records(storage, udid):
    source_dir = Path("/var/lib/lockdown")
    destination = storage / "state/lockdown"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    pair_record = source_dir / f"{udid}.plist"
    if not pair_record.is_file():
        raise RuntimeError(f"pairing record {pair_record} was not created")
    shutil.copy2(pair_record, destination / pair_record.name)
    system_config = source_dir / "SystemConfiguration.plist"
    if system_config.is_file():
        shutil.copy2(system_config, destination / system_config.name)
    for path in destination.iterdir():
        path.chmod(0o600)


def setup():
    print(f"\n{APP_NAME} — guided Linux setup\n")
    if sys.platform != "linux":
        raise RuntimeError("this release supports Linux only; Windows is planned")
    for required in (PMD, NETMUXD):
        if not required.is_file():
            raise RuntimeError(f"missing {required}; run ./install.sh first")

    default_storage = "/mnt/iphone-backup"
    chosen = input(f"1/5 Backup storage directory [{default_storage}]: ").strip() or default_storage
    storage = Path(chosen).expanduser().resolve()
    storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.access(storage, os.W_OK):
        raise RuntimeError(f"{storage} is not writable")
    free_gib = shutil.disk_usage(storage).free / 1024**3
    print(f"    Available space: {free_gib:.1f} GiB")

    print("\n2/5 Connect the iPhone by USB, unlock it, and tap Trust on the iPhone.")
    input("    Press Enter when ready...")
    devices = usb_udids()
    if not devices:
        raise RuntimeError("no unlocked iPhone was found over USB")
    if len(devices) == 1:
        udid = devices[0]
    else:
        for number, device in enumerate(devices, 1):
            print(f"    {number}. {device}")
        selected = input("    Select device number: ").strip()
        if not selected.isdigit() or not 1 <= int(selected) <= len(devices):
            raise RuntimeError("invalid device selection")
        udid = devices[int(selected) - 1]

    print("\n3/5 Pairing with the trusted iPhone...")
    pmd("lockdown", "pair", "--udid", udid)
    copy_pair_records(storage, udid)
    save_config(storage, udid)
    command(("systemctl", "--user", "daemon-reload"))
    command(("systemctl", "--user", "enable", "--now", "iphone-netmuxd.service"))

    print("\n4/5 Disconnect the USB cable. Keep the iPhone unlocked and on the same Wi-Fi.")
    input("    Press Enter to test Wi-Fi...")
    for _ in range(30):
        if udid in network_udids():
            break
        print("    Waiting for iPhone on Wi-Fi...", flush=True)
        time.sleep(4)
    else:
        raise RuntimeError("the paired iPhone was not found on Wi-Fi; setup was saved, so you can retry")

    print("\n5/5 Starting the first SMS backup. This is the longest run.\n")
    backup(manual=True)
    command(("systemctl", "--user", "enable", "--now", "iphone-sms-backup.timer"))
    print("\nSetup complete. Incremental backups will run automatically when the iPhone is on this Wi-Fi.")


def backup_paths(config):
    storage = config["storage"]
    return {
        "backup_root": storage / "backups",
        "state": storage / "state",
        "export": storage / "exports/messages.csv",
        "log": storage / "state/backup.log",
        "lock": storage / "state/backup.lock",
        "pid": storage / "state/backup.pid",
        "online": storage / "state/phone-online",
    }


def backup(manual=False):
    config = load_config()
    paths = backup_paths(config)
    if not config["storage"].is_dir() or not os.access(config["storage"], os.W_OK):
        raise RuntimeError(f"backup storage is unavailable: {config['storage']}")
    paths["backup_root"].mkdir(parents=True, exist_ok=True, mode=0o700)
    paths["state"].mkdir(parents=True, exist_ok=True, mode=0o700)

    with paths["lock"].open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if manual:
                print("A backup is already running.")
            return 0
        if config["udid"] not in network_udids():
            paths["online"].unlink(missing_ok=True)
            if manual:
                raise RuntimeError("the paired iPhone is not reachable over Wi-Fi")
            return 0
        if not manual and paths["online"].exists():
            return 0

        paths["pid"].write_text(str(os.getpid()), encoding="ascii")
        backup_command = [
            PMD, "backup2", "backup", paths["backup_root"], "--only", "sms",
            "--usbmux", USBSOCKET, "--udid", config["udid"],
        ]
        print("Starting incremental SMS backup...")
        try:
            result = command(("script", "-qefc", shlex.join(str(v) for v in backup_command), paths["log"]), check=False)
            if result.returncode:
                raise RuntimeError(f"iPhone backup failed (exit code {result.returncode}); see {paths['log']}")
            result = command((
                sys.executable, INSTALL_DIR / "iphone_sms_exporter.py",
                "--backup-root", paths["backup_root"], "--udid", config["udid"], "--output", paths["export"],
            ), check=False)
            if result.returncode:
                raise RuntimeError("backup completed but SMS export failed")
            (paths["state"] / "last-success").write_text(datetime.now(timezone.utc).isoformat(), encoding="ascii")
            paths["online"].touch()
        finally:
            paths["pid"].unlink(missing_ok=True)
    return 0


def latest_progress(log_path):
    try:
        matches = re.findall(rb"([0-9]+(?:\.[0-9]+)?)/100", log_path.read_bytes())
        return float(matches[-1]) if matches else None
    except OSError:
        return None


def status():
    config = load_config()
    paths = backup_paths(config)
    running = False
    try:
        pid = int(paths["pid"].read_text(encoding="ascii"))
        os.kill(pid, 0)
        running = True
    except (OSError, ValueError):
        pass
    progress = latest_progress(paths["log"])
    if running:
        print("Status: RUNNING")
        print(f"Whole backup progress: {progress:.1f}%" if progress is not None else "Whole backup progress: starting")
    else:
        status_path = paths["backup_root"] / config["udid"] / "Status.plist"
        complete = False
        try:
            with status_path.open("rb") as source:
                complete = str(plistlib.load(source).get("SnapshotState", "")).lower() == "finished"
        except (OSError, plistlib.InvalidFileException):
            pass
        print("Status: COMPLETED" if complete else "Status: NOT COMPLETED")
    success = paths["state"] / "last-success"
    if success.is_file():
        print(f"Last successful export: {success.read_text(encoding='ascii').strip()}")
    if paths["export"].is_file():
        print(f"Messages CSV: {paths['export']}")
    print(f"Storage free: {shutil.disk_usage(config['storage']).free / 1024**3:.1f} GiB")


def logs():
    paths = backup_paths(load_config())
    if not paths["log"].is_file():
        print("No backup log exists yet.")
        return
    data = paths["log"].read_text(encoding="utf-8", errors="replace").replace("\r", "\n")
    print("\n".join(data.splitlines()[-40:]))


def run_netmuxd():
    config = load_config()
    state = config["storage"] / "state/lockdown"
    if not state.is_dir():
        raise RuntimeError(f"pairing state is unavailable: {state}")
    os.execv(NETMUXD, (str(NETMUXD), "--disable-unix", "--disable-usb", "--host", "127.0.0.1", "--port", "27015", "--plist-storage", str(state)))


def main():
    parser = argparse.ArgumentParser(prog="iphone-sms-backup", description="Private iPhone SMS backup over local Wi-Fi")
    parser.add_argument("command", nargs="?", default="setup", choices=("setup", "backup", "auto-backup", "status", "logs", "run-netmuxd"))
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup()
        elif args.command in ("backup", "auto-backup"):
            return backup(manual=args.command == "backup")
        elif args.command == "status":
            status()
        elif args.command == "logs":
            logs()
        else:
            run_netmuxd()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
