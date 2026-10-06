"""Start/stop only this project's registered local processes."""

import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from pagw.config import Settings

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
MANIFEST = RUNTIME / "processes.json"
SERVICES = {
    "idp": 8081,
    "payers": 8082,
    "receiver": 8083,
    "api": 8080,
    "relay": None,
    "worker": None,
    "webhooks": None,
    "scheduler": None,
    "callbacks": None,
}


def registered(pid: int, name: str) -> bool:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
    )
    parts = result.stdout.strip().split(" -m pagw.cli ", 1)
    if len(parts) != 2:
        return False
    command = shlex.split(parts[1])
    if command[: len(service_args(name)) - 2] != service_args(name)[2:]:
        return False
    if Path(parts[0]).resolve() != Path(sys.executable).resolve():
        return False
    proc = Path(f"/proc/{pid}/cwd")
    if proc.exists():
        return proc.resolve() == ROOT
    cwd = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True
    )
    return "n" + str(ROOT) in cwd.stdout.splitlines()


def service_args(name: str) -> list[str]:
    if name in {"worker", "webhooks"}:
        return ["-m", "pagw.cli", "worker", "--queue", "submit" if name == "worker" else "webhook"]
    return ["-m", "pagw.cli", name]


def up(sqs=False):
    os.chdir(ROOT)
    subprocess.run([sys.executable, "scripts/setup.py"], check=True)
    connection = conninfo_to_dict(Settings().database_url.get_secret_value())
    if (
        connection.get("dbname") == "pagw_native"
        and connection.get("host") == "127.0.0.1"
        and connection.get("port") == "55480"
    ):
        with psycopg.connect(
            make_conninfo(**dict(connection, dbname="postgres")), autocommit=True
        ) as c:
            if not c.execute("SELECT 1 FROM pg_database WHERE datname='pagw_native'").fetchone():
                try:
                    c.execute("CREATE DATABASE pagw_native")
                except psycopg.errors.DuplicateDatabase:
                    pass
    subprocess.run([sys.executable, "-m", "pagw.cli", "migrate"], check=True)
    if sqs:
        urls = json.loads((RUNTIME / "sqs-urls.json").read_text())
        os.environ.update(
            PAGW_BROKER="sqs",
            PAGW_SQS_ENDPOINT="http://127.0.0.1:45679",
            PAGW_SUBMIT_QUEUE_URL=urls["submit"],
            PAGW_WEBHOOK_QUEUE_URL=urls["webhook"],
            AWS_ACCESS_KEY_ID="test",
            AWS_SECRET_ACCESS_KEY="test",  # noqa: S106 -- local emulator only
        )
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    mode_path = RUNTIME / "broker-mode"
    mode = "sqs" if sqs else "postgres"
    if manifest and mode_path.exists() and mode_path.read_text() != mode:
        raise SystemExit("Stop the registered services before changing broker mode")
    for name, port in SERVICES.items():
        if port:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0 and not registered(
                    manifest.get(name, 0), name
                ):
                    raise SystemExit(f"Port {port} is occupied by another service")
    for name, port in SERVICES.items():
        prior = manifest.get(name)
        if prior:
            try:
                os.kill(prior, 0)
                if not registered(prior, name):
                    raise SystemExit(f"Manifest PID for {name} belongs to another process")
                continue
            except ProcessLookupError:
                pass
        command = [sys.executable, *service_args(name)]
        if port:
            command += ["--port", str(port)]
        with (RUNTIME / f"{name}.log").open("ab") as log:
            process = subprocess.Popen(
                command, cwd=ROOT, stdout=log, stderr=log, start_new_session=True
            )
        manifest[name] = process.pid
        MANIFEST.write_text(json.dumps(manifest))
    mode_path.write_text(mode)
    for _ in range(80):
        try:
            if (
                all(registered(pid, name) for name, pid in manifest.items())
                and httpx.get("http://127.0.0.1:8080/readyz", timeout=1, trust_env=False).is_success
            ):
                print("Gateway console: http://127.0.0.1:8080")
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise SystemExit("Startup failed; inspect .runtime/*.log")


def down():
    if not MANIFEST.exists():
        return
    for name, pid in json.loads(MANIFEST.read_text()).items():
        if registered(pid, name):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for _ in range(50):
        if not any(registered(pid, name) for name, pid in json.loads(MANIFEST.read_text()).items()):
            break
        time.sleep(0.2)
    else:
        raise SystemExit("Registered services are still shutting down; manifest retained")
    MANIFEST.unlink()
    print("Stopped registered gateway services. Database and durable data retained.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["up", "down"])
    parser.add_argument("--sqs", action="store_true")
    args = parser.parse_args()
    up(args.sqs) if args.command == "up" else down()
