"""Bounded local admission benchmark; not a production throughput claim."""

import argparse
import json
import platform
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx

from pagw.config import Settings
from pagw.fhir import fixture

parser = argparse.ArgumentParser()
parser.add_argument("--count", type=int, default=30)
parser.add_argument("--concurrency", type=int, default=5)
args = parser.parse_args()
if not 1 <= args.count <= 200 or not 1 <= args.concurrency <= 20:
    raise SystemExit("Use count <= 200 and concurrency <= 20")
settings = Settings()
base = settings.callback_base_url
with httpx.Client(timeout=10, trust_env=False) as client:
    token_response = client.post(base + "/demo/token")
    token_response.raise_for_status()
    token = token_response.json()["access_token"]


def one(n):
    started = time.perf_counter()
    key = "load-" + str(uuid4())
    with httpx.Client(timeout=20, trust_env=False) as client:
        result = client.post(
            base + "/fhir/Claim/$submit",
            json=fixture("alpha" if n % 2 == 0 else "beta", "J0585", key),
            headers={"Authorization": "Bearer " + token, "Idempotency-Key": key},
        )
    return result.status_code, time.perf_counter() - started


started = time.perf_counter()
with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
    results = list(pool.map(one, range(args.count)))
elapsed = time.perf_counter() - started
latencies = sorted(x[1] for x in results)
output = {
    "scope": "local HTTP durable-admission latency, includes client creation; not payer completion",
    "platform": platform.platform(),
    "python": platform.python_version(),
    "broker": settings.broker,
    "requests": args.count,
    "concurrency": args.concurrency,
    "accepted": sum(x[0] == 202 for x in results),
    "errors": [x[0] for x in results if x[0] != 202],
    "seconds": elapsed,
    "accepted_per_second": sum(x[0] == 202 for x in results) / elapsed,
    "p50_seconds": statistics.median(latencies),
    "p95_seconds": latencies[int((len(latencies) - 1) * 0.95)],
}
Path("artifacts").mkdir(exist_ok=True)
Path("artifacts/local-load.json").write_text(json.dumps(output, indent=2) + "\n")
print(json.dumps(output, indent=2))
assert not output["errors"]
