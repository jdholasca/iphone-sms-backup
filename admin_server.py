#!/usr/bin/env python3
import csv
import json
import os
import plistlib
import shutil
import socketserver
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from android_bridge import PORT as BRIDGE_PORT, create_pair_code
from iphone_backup_app import backup_paths, latest_progress, load_config


SOCKET_PATH = Path(os.environ.get("IPHONE_BACKUP_ADMIN_SOCKET", f"/run/user/{os.getuid()}/iphone-backup-admin/admin.sock"))
SERVICES = {
    "wifi": "iphone-netmuxd.service",
    "automatic": "iphone-sms-backup.timer",
    "backup": "iphone-sms-backup.service",
    "android": "iphone-android-bridge.service",
}
PERSISTENT = {"wifi", "automatic", "android"}


def command(*args):
    return subprocess.run(args, text=True, capture_output=True, check=False)


def unit_state(unit):
    active = command("systemctl", "--user", "is-active", unit).stdout.strip()
    enabled = command("systemctl", "--user", "is-enabled", unit).stdout.strip()
    return {"active": active, "enabled": enabled}


def backup_state():
    config = load_config()
    paths = backup_paths(config)
    running = False
    try:
        pid = int(paths["pid"].read_text(encoding="ascii"))
        os.kill(pid, 0)
        running = True
    except (OSError, ValueError):
        pass
    complete = False
    status_path = paths["backup_root"] / config["udid"] / "Status.plist"
    try:
        with status_path.open("rb") as source:
            complete = str(plistlib.load(source).get("SnapshotState", "")).lower() == "finished"
    except (OSError, plistlib.InvalidFileException):
        pass
    message_count = 0
    try:
        with paths["export"].open(newline="", encoding="utf-8") as source:
            message_count = max(0, sum(1 for _ in csv.reader(source)) - 1)
    except OSError:
        pass
    last_success = ""
    try:
        last_success = (paths["state"] / "last-success").read_text(encoding="ascii").strip()
    except OSError:
        pass
    return {
        "state": "running" if running else "completed" if complete else "not-completed",
        "progress": latest_progress(paths["log"]),
        "lastSuccess": last_success,
        "messageCount": message_count,
        "phoneOnline": paths["online"].exists(),
        "storage": str(config["storage"]),
        "storageFreeGiB": round(shutil.disk_usage(config["storage"]).free / 1024**3, 1),
        "device": f"{config['udid'][:8]}…{config['udid'][-4:]}",
        "bridgePort": BRIDGE_PORT,
    }


def status_payload():
    return {"backup": backup_state(), "services": {name: unit_state(unit) for name, unit in SERVICES.items()}}


def run_action(action):
    if action == "backup-now":
        result = command("systemctl", "--user", "start", "--no-block", SERVICES["backup"])
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Could not start the backup")
        return {"ok": True}
    operation, separator, name = action.partition(":")
    if separator and operation in {"start", "restart"} and name in SERVICES and name != "backup":
        args = ["systemctl", "--user", operation]
        if operation == "start" and name in PERSISTENT:
            args = ["systemctl", "--user", "enable", "--now"]
        result = command(*args, SERVICES[name])
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"Could not {operation} {name}")
        return {"ok": True}
    if action == "pair":
        config = load_config()
        code, server_id, _ = create_pair_code(config["storage"])
        return {"ok": True, "code": code, "serverId": server_id, "expiresIn": 600}
    raise ValueError("Unsupported action")


def service_logs(name):
    if name not in SERVICES:
        raise ValueError("Unknown service")
    if name == "backup":
        path = backup_paths(load_config())["log"]
        try:
            text = path.read_text(encoding="utf-8", errors="replace").replace("\r", "\n")
            return "\n".join(text.splitlines()[-120:])
        except OSError:
            return "No backup log exists yet."
    result = command("journalctl", "--user", "-u", SERVICES[name], "-n", "120", "--no-pager", "-o", "short-iso")
    return result.stdout.strip() or "No logs are available for this service."


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "IphoneBackupAdmin/1"
    sys_version = ""

    def local_request(self):
        return True

    def send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.local_request():
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Local access only"})
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/v1/status":
                self.send_json(HTTPStatus.OK, status_payload())
                return
            if parsed.path == "/v1/logs":
                name = parse_qs(parsed.query).get("service", ["backup"])[0]
                self.send_json(HTTPStatus.OK, {"service": name, "logs": service_logs(name)})
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except (OSError, RuntimeError, ValueError) as error:
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})

    def do_POST(self):
        if not self.local_request():
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Local access only"})
            return
        if self.path != "/v1/actions":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1024 or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("Invalid request")
            body = json.loads(self.rfile.read(length))
            self.send_json(HTTPStatus.OK, run_action(str(body.get("action", ""))))
        except (json.JSONDecodeError, OSError, RuntimeError, ValueError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def log_message(self, fmt, *args):
        return


def serve():
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    SOCKET_PATH.unlink(missing_ok=True)
    server = socketserver.ThreadingUnixStreamServer(str(SOCKET_PATH), AdminHandler)
    SOCKET_PATH.chmod(0o600)
    print(f"iPhone backup administration API listening on private Unix socket {SOCKET_PATH}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    serve()
