from dataclasses import dataclass
from typing import Protocol

import httpx

from pagw.config import Settings
from pagw.fhir import parse_submission
from pagw.http import bounded_request, retry_after
from pagw.legacy import map_legacy
from pagw.security import TokenManager
from pagw.telemetry import LATENCY


@dataclass(frozen=True)
class Result:
    payer_ref: str
    correlation_id: str
    decision: str
    sequence: int
    preauth_ref: str | None = None


class PartnerError(Exception):
    def __init__(self, code: str, retryable: bool, ambiguous=False, retry_after=0.0):
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.retry_after = retry_after


class Connector(Protocol):
    def submit(self, row: dict) -> Result: ...
    def poll(self, row: dict) -> Result: ...


def legacy_payload(row: dict) -> dict:
    return map_legacy(row["bundle"], parse_submission(row["bundle"]), row["id"])


def normalize(payer: str, body: dict) -> Result:
    try:
        if payer == "alpha":
            ext = {x["url"].rsplit("/", 1)[-1]: x for x in body["extension"]}
            result = Result(
                str(body["id"]),
                ext["correlation"]["valueString"],
                ext["decision"]["valueCode"],
                ext["sequence"]["valueInteger"],
                body.get("preAuthRef"),
            )
        else:
            states = {"IN_REVIEW": "PENDED", "AUTHORIZED": "APPROVED", "NOT_AUTHORIZED": "DENIED"}
            result = Result(
                str(body["authorizationId"]),
                body["externalRequestId"],
                states[body["status"]],
                body["revision"],
                body.get("approvalCode"),
            )
        if (
            result.decision not in {"PENDED", "APPROVED", "DENIED"}
            or type(result.sequence) is not int
            or result.sequence < 0
            or not result.payer_ref
            or len(result.payer_ref) > 128
        ):
            raise ValueError("invalid-result")
        if result.decision == "APPROVED" and not result.preauth_ref:
            raise ValueError("missing-approval-reference")
        return result
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PartnerError("INVALID_PAYER_RESPONSE", True, ambiguous=True) from exc


class HTTPConnector:
    def __init__(self, settings: Settings, payer: str, tokens: TokenManager):
        self.settings, self.payer, self.tokens = settings, payer, tokens

    def _call(self, method: str, path: str, row: dict, payload=None) -> Result:
        try:
            for refresh in (False, True):
                token = self.tokens.get(self.payer, force=refresh)
                with (
                    LATENCY.labels(self.payer).time(),
                    httpx.Client(
                        timeout=self.settings.http_timeout_seconds,
                        trust_env=False,
                        follow_redirects=False,
                    ) as client,
                ):
                    response = bounded_request(
                        client,
                        method,
                        self.settings.payer_base_url + path,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Idempotency-Key": str(row["id"]),
                        },
                        seconds=self.settings.http_timeout_seconds,
                    )
                if response.status_code != 401:
                    break
            status = response.status_code
            if status == 429:
                delay = retry_after(response.headers.get("Retry-After", "5"))
                raise PartnerError("PAYER_RATE_LIMIT", True, retry_after=delay)
            if status in {408, 409} or status >= 500:
                raise PartnerError("PAYER_UNCERTAIN", True, ambiguous=method == "POST")
            if status >= 300:
                raise PartnerError("PAYER_REJECTED", False)
            if len(response.content) > 1024 * 1024:
                raise PartnerError("PAYER_RESPONSE_TOO_LARGE", True, ambiguous=True)
            result = normalize(self.payer, response.json())
            if result.correlation_id != str(row["id"]):
                raise PartnerError("PAYER_CORRELATION_MISMATCH", False, ambiguous=True)
            return result
        except httpx.HTTPError as exc:
            raise PartnerError("PAYER_UNAVAILABLE", True, ambiguous=method == "POST") from exc
        except (ValueError, TypeError) as exc:
            raise PartnerError("INVALID_PAYER_RESPONSE", True, ambiguous=True) from exc

    def submit(self, row: dict) -> Result:
        # A retry must keep the admitted wire identity across mapper upgrades.
        # Pre-snapshot rows use the original v1 translator and admission metadata;
        # they must not be reparsed under today's stricter admission policy.
        payload = row.get("submission_payload")
        if self.payer == "alpha":
            if payload is None:
                payload = {"bundle": row["bundle"], "correlation_id": str(row["id"])}
            return self._call("POST", "/alpha/fhir/Claim/$submit", row, payload)
        if payload is None:
            parsed = {
                "claim": next(
                    e["resource"]
                    for e in row["bundle"]["entry"]
                    if e["resource"]["resourceType"] == "Claim"
                ),
                "business_system": row["business_system"],
                "business_value": row["business_value"],
            }
            payload = map_legacy(row["bundle"], parsed, row["id"], version=1)
        return self._call("POST", "/beta/api/v2/authorizations", row, payload)

    def poll(self, row: dict) -> Result:
        # Look up by the SAME submission identity even when the submit response was lost.
        return self._call("GET", f"/{self.payer}/by-key/{row['id']}", row)
