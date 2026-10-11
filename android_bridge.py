#!/usr/bin/env python3
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sqlite3
import ssl
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID
from zeroconf import IPVersion, ServiceInfo, Zeroconf

from iphone_sms_exporter import apple_datetime, find_sms_database


SERVICE_TYPE = "_iphone-sms._tcp.local."
PORT = 18765


def private_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def bridge_paths(storage):
    state = storage / "state/android-bridge"
    return {
        "state": state,
        "identity": state / "identity.json",
        "cert": state / "server.crt",
        "key": state / "server.key",
        "pair": state / "pair-code.json",
        "clients": state / "clients.json",
    }


def ensure_identity(storage):
    paths = bridge_paths(storage)
    identity = private_json(paths["identity"], {})
    if not identity.get("serverId"):
        identity = {"serverId": str(uuid.uuid4())}
        write_private_json(paths["identity"], identity)
    if not paths["cert"].is_file() or not paths["key"].is_file():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"LocalLedger {identity['serverId']}")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        paths["key"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths["key"].write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ))
        paths["cert"].write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        paths["key"].chmod(0o600)
        paths["cert"].chmod(0o600)
    cert = x509.load_pem_x509_certificate(paths["cert"].read_bytes())
    fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    return paths, identity["serverId"], fingerprint


def create_pair_code(storage):
    paths, server_id, fingerprint = ensure_identity(storage)
    code = f"{secrets.randbelow(100_000_000):08d}"
    write_private_json(paths["pair"], {
        "hash": hashlib.sha256(code.encode()).hexdigest(),
        "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "attempts": 5,
    })
    return code, server_id, fingerprint


def encrypted_pair_response(code, value):
    key = hashlib.sha256(f"local-ledger-pair-v1:{code}".encode()).digest()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(value).encode(), None)
    return {"nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ciphertext).decode()}


def validate_pair_code(paths, code):
    pair = private_json(paths["pair"], {})
    try:
        valid_time = datetime.fromisoformat(pair["expires"]) > datetime.now(timezone.utc)
        attempts = int(pair.get("attempts", 0))
    except (KeyError, TypeError, ValueError):
        return False
    valid = valid_time and attempts > 0 and hmac.compare_digest(
        pair.get("hash", ""), hashlib.sha256(code.encode()).hexdigest()
    )
    if not valid:
        pair["attempts"] = max(0, attempts - 1)
        write_private_json(paths["pair"], pair)
    return valid


def authorize(paths, header):
    if not header.startswith("Bearer "):
        return False
    digest = hashlib.sha256(header[7:].encode()).hexdigest()
    clients = private_json(paths["clients"], {}).get("clients", [])
    return any(hmac.compare_digest(item.get("tokenHash", ""), digest) for item in clients)


def message_page(storage, udid, after, limit=500):
    source = find_sms_database(storage / "backups", udid)
    with tempfile.TemporaryDirectory(dir=storage / "state") as temporary:
        copy = Path(temporary) / "sms.db"
        copy.write_bytes(source.read_bytes())
        with sqlite3.connect(f"file:{copy}?mode=ro", uri=True) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(message)")}
            service = "m.service" if "service" in columns else "''"
            subject = "m.subject" if "subject" in columns else "''"
            rows = db.execute(
                f"""
                SELECT m.ROWID, m.guid, m.date, m.is_from_me, COALESCE(h.id, ''),
                       {service}, COALESCE({subject}, ''), COALESCE(m.text, '')
                  FROM message AS m LEFT JOIN handle AS h ON h.ROWID = m.handle_id
                 WHERE m.ROWID > ? ORDER BY m.ROWID LIMIT ?
                """,
                (after, limit + 1),
            ).fetchall()
    has_more = len(rows) > limit
    messages = [
        {
            "id": row[0], "guid": row[1] or "", "date": apple_datetime(row[2]),
            "direction": "sent" if row[3] else "received", "sender": row[4] or "Unknown",
            "service": row[5] or "", "subject": row[6] or "", "message": row[7] or "",
        }
        for row in rows[:limit]
    ]
    return {"messages": messages, "next": messages[-1]["id"] if messages else after, "hasMore": has_more}


def local_ipv4():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    finally:
        sock.close()


def make_handler(storage, udid, paths, server_id, fingerprint):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalLedgerBridge/1"

        def send_json(self, status, value):
            payload = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if self.path != "/v1/pair":
                self.send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 8192:
                    raise ValueError
                request = json.loads(self.rfile.read(length))
                code = str(request.get("code", ""))
                name = str(request.get("deviceName", "Android"))[:100]
            except (ValueError, json.JSONDecodeError):
                self.send_json(400, {"error": "invalid request"})
                return
            if not re_full_code(code) or not validate_pair_code(paths, code):
                self.send_json(403, {"error": "invalid or expired pairing code"})
                return
            token = secrets.token_urlsafe(32)
            clients = private_json(paths["clients"], {"clients": []})
            clients["clients"] = [item for item in clients.get("clients", []) if item.get("name") != name]
            clients["clients"].append({
                "name": name, "tokenHash": hashlib.sha256(token.encode()).hexdigest(),
                "pairedAt": datetime.now(timezone.utc).isoformat(),
            })
            write_private_json(paths["clients"], clients)
            paths["pair"].unlink(missing_ok=True)
            self.send_json(200, encrypted_pair_response(code, {
                "serverId": server_id, "fingerprint": fingerprint, "token": token,
            }))

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/v1/messages":
                self.send_json(404, {"error": "not found"})
                return
            if not authorize(paths, self.headers.get("Authorization", "")):
                self.send_json(401, {"error": "unauthorized"})
                return
            try:
                after = int(parse_qs(parsed.query).get("after", ["0"])[0])
                if after < 0:
                    raise ValueError
                self.send_json(200, message_page(storage, udid, after))
            except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
                self.send_json(503, {"error": f"messages unavailable: {error}"})

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} - {fmt % args}")

    return Handler


def re_full_code(value):
    return len(value) == 8 and value.isdigit()


def serve(storage, udid):
    paths, server_id, fingerprint = ensure_identity(storage)
    address = local_ipv4()
    server = ThreadingHTTPServer((address, PORT), make_handler(storage, udid, paths, server_id, fingerprint))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(paths["cert"], paths["key"])
    server.socket = context.wrap_socket(server.socket, server_side=True)
    info = ServiceInfo(
        SERVICE_TYPE, f"LocalLedger-{server_id}.{SERVICE_TYPE}",
        addresses=[ipaddress.ip_address(address).packed], port=PORT,
        properties={"id": server_id, "fingerprint": fingerprint, "protocol": "1"},
        server=f"localledger-{server_id}.local.",
    )
    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    zeroconf.register_service(info)
    print(f"LocalLedger bridge {server_id} listening securely on {address}:{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        zeroconf.unregister_service(info)
        zeroconf.close()
        server.server_close()
