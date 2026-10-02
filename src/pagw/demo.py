"""Exercise real HTTP, durable payer records and signed provider webhooks."""

import json
import time
from uuid import uuid4

import httpx

from pagw.config import Settings
from pagw.db import Database
from pagw.fhir import fixture


def main():
    settings = Settings()
    if settings.mode != "local":
        raise SystemExit("The synthetic demo runner is local-only")
    db = Database(settings)
    base = settings.callback_base_url
    evidence = []
    with httpx.Client(timeout=10, trust_env=False) as client:
        for _ in range(100):
            try:
                if client.get(base + "/readyz").is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("gateway did not become ready")
        actual_broker = client.get(base + "/readyz").json()["broker"]
        session = client.post(base + "/demo/token")
        session.raise_for_status()
        client.headers["Authorization"] = "Bearer " + session.json()["access_token"]
        subscription = client.post(
            base + "/webhooks/subscriptions", json={"url": settings.receiver_url}
        )
        assert subscription.status_code in {201, 409}, subscription.status_code
        for payer, code, chaos in [
            ("alpha", "70551", "normal"),
            ("beta", "27447", "normal"),
            ("alpha", "J0585", "early_callback"),
            ("beta", "70551", "timeout_after_accept"),
            ("alpha", "99999", "normal"),
        ]:
            with httpx.Client(timeout=3, trust_env=False) as control:
                control.post(
                    settings.payer_base_url + f"/mock/{payer}/chaos",
                    headers={"X-Mock-Key": settings.payer_client_secret.get_secret_value()},
                    json={"mode": chaos, "count": 1 if chaos != "normal" else 0},
                ).raise_for_status()
            key = str(uuid4())
            bundle = fixture(payer, code, "demo-" + key)
            response = client.post(
                base + "/fhir/Claim/$submit", headers={"Idempotency-Key": key}, json=bundle
            )
            response.raise_for_status()
            rid = response.json()["id"]
            replay = client.post(
                base + "/fhir/Claim/$submit", headers={"Idempotency-Key": key}, json=bundle
            )
            assert replay.headers["Idempotent-Replayed"] == "true"
            assert replay.json() == response.json()
            started = time.monotonic()
            while time.monotonic() - started < 45:
                result = client.get(base + "/api/requests/" + rid)
                result.raise_for_status()
                detail = result.json()
                row = detail["request"]
                if row["decision"] in {"APPROVED", "DENIED"} or row["delivery_state"] == "FAILED":
                    if detail["deliveries"] and all(
                        d["state"] == "DELIVERED" for d in detail["deliveries"]
                    ):
                        break
                time.sleep(0.2)
            else:
                raise RuntimeError("demo scenario did not complete: " + rid)
            payer_row = db.one(
                "SELECT count(*) AS n FROM mock_authorizations WHERE correlation_id=%s", (rid,)
            )
            assert payer_row is not None
            payer_count = payer_row["n"]
            expected = "FAILED" if code == "99999" else "DENIED" if code == "27447" else "APPROVED"
            assert (row["delivery_state"] if code == "99999" else row["decision"]) == expected
            assert payer_count == (0 if code == "99999" else 1)
            assert all(d["state"] == "DELIVERED" for d in detail["deliveries"])
            item = {
                "payer": payer,
                "scenario": chaos,
                "request_id": rid,
                "result": expected,
                "payer_authorizations": payer_count,
                "webhooks": len(detail["deliveries"]),
            }
            evidence.append(item)
            print(json.dumps(item))
    return {"broker": actual_broker, "scenarios": evidence}


if __name__ == "__main__":
    main()
