import argparse
import json
import signal
import time

import httpx
import uvicorn

from pagw.api import create_app
from pagw.config import get_settings
from pagw.db import Database
from pagw.mocks import idp_app, payers_app, receiver_app
from pagw.queueing import broker_for, relay
from pagw.store import Store
from pagw.telemetry import configure, event
from pagw.worker import Worker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "migrate",
            "api",
            "idp",
            "payers",
            "receiver",
            "worker",
            "relay",
            "scheduler",
            "callbacks",
            "openapi",
        ],
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--queue", choices=["submit", "webhook", "all"], default="all")
    args = parser.parse_args()
    settings = get_settings()
    expected_role = (
        "webhooks" if args.command == "worker" and args.queue == "webhook" else args.command
    )
    if settings.mode == "production" and (
        settings.service_role != expected_role or (args.command == "worker" and args.queue == "all")
    ):
        raise ValueError("production command must match its configured service_role")
    configure(settings)
    db = Database(settings)
    if args.command == "migrate":
        db.migrate()
        return
    if args.command == "openapi":
        print(json.dumps(create_app(settings).openapi(), indent=2, sort_keys=True))
        return
    factories = {"api": create_app, "idp": idp_app, "payers": payers_app, "receiver": receiver_app}
    if args.command in factories:
        uvicorn.run(
            factories[args.command](settings), host=args.host, port=args.port, access_log=False
        )
        return
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    store, broker = Store(db, settings), broker_for(settings, db)
    worker = Worker(store, settings, broker) if args.command == "worker" else None
    while not stopped:
        try:
            if args.command == "worker":
                assert worker is not None
                worker.once(("submit", "webhook") if args.queue == "all" else (args.queue,))
            elif args.command == "relay":
                relay(db, broker)
            elif args.command == "scheduler":
                store.sweep()
                store.expire()
            else:
                if settings.mode != "local":
                    raise ValueError("mock callback ticker is local-only")
                with httpx.Client(timeout=5, trust_env=False) as client:
                    client.post(
                        settings.payer_base_url + "/mock/callback-tick",
                        headers={"X-Mock-Key": settings.payer_client_secret.get_secret_value()},
                    ).raise_for_status()
        except Exception:
            # Suppress external exception strings/payloads. A killed/failed worker
            # leaves a durable lease that is reclaimed by the scheduler.
            event("process.iteration_failed", error_code="ITERATION_FAILED", kind=args.command)
            time.sleep(1)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
