"""FHIR R4 JSON schema plus deliberately bounded gateway rules.

Schema validation is not PAS profile or terminology conformance validation.
"""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from functools import lru_cache
from importlib.resources import files
from typing import Any
from uuid import UUID

from jsonschema import Draft6Validator
from pydantic import ValidationError

from pagw.legacy import LegacyRequest, map_legacy


class InvalidFHIR(ValueError):
    def __init__(self, location: str, code: str = "invalid"):
        self.location = location
        self.code = code
        super().__init__(code)


def canonical(value: Any) -> bytes:
    # Object-key order is ignored; array order remains significant by contract.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@lru_cache
def schema() -> dict:
    return json.loads(files("pagw").joinpath("fhir_schema/fhir.schema.json").read_text())


def validate_resource(resource: dict) -> None:
    if not isinstance(resource, dict) or not isinstance(resource.get("resourceType"), str):
        raise InvalidFHIR("resourceType", "invalid-resource")
    try:
        canonical(resource)
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidFHIR("resourceType", "invalid-json-value") from exc
    kind = resource.get("resourceType")
    if kind not in {
        "Bundle",
        "Claim",
        "ClaimResponse",
        "Patient",
        "Coverage",
        "Organization",
        "Practitioner",
    }:
        raise InvalidFHIR("resourceType", "unsupported-resource")
    validator = Draft6Validator(
        {"$ref": f"#/definitions/{kind}", "definitions": schema()["definitions"]}
    )
    error = next(validator.iter_errors(resource), None)
    if error:
        path = ".".join(str(x) for x in error.absolute_path)
        raise InvalidFHIR(path or "resourceType", "schema-invalid")
    # Official JSON schemas permit some absent primitives because FHIR supports
    # data-absent extensions. Our supported subset requires these actual values.
    required = {
        "Claim": ["status", "use", "created"],
        "ClaimResponse": ["status", "use", "created", "outcome"],
        "Coverage": ["status"],
    }
    for field in required.get(kind, []):
        if not resource.get(field):
            raise InvalidFHIR(field, "required")


def parse_submission(bundle: dict) -> dict:
    validate_resource(bundle)

    def check_modifiers(value, path="Bundle"):
        if isinstance(value, dict):
            if value.get("modifierExtension") or value.get("implicitRules"):
                raise InvalidFHIR(path, "unsupported-modifier")
            for key, child in value.items():
                check_modifiers(child, f"{path}.{key}")
        elif isinstance(value, list):
            for i, child in enumerate(value):
                check_modifiers(child, f"{path}[{i}]")

    check_modifiers(bundle)
    if bundle.get("type") != "collection":
        raise InvalidFHIR("Bundle.type")
    entries = bundle.get("entry", [])
    if not 1 <= len(entries) <= 30:
        raise InvalidFHIR("Bundle.entry", "bundle-size")
    references: dict[str, dict] = {}
    contained_ids = set()
    claims = []
    for i, entry in enumerate(entries):
        resource = entry.get("resource", {})
        validate_resource(resource)
        # These resources will be embedded in the response. Reject rather than
        # strip version/security metadata or emit invalid contained resources.
        if resource.get("contained") or set(resource.get("meta", {})) & {
            "versionId",
            "lastUpdated",
            "security",
        }:
            raise InvalidFHIR(f"Bundle.entry[{i}].resource", "unsupported-contained-metadata")
        supported = {
            "Patient": {"identifier", "name", "birthDate", "gender"},
            "Coverage": {"status", "beneficiary", "payor", "subscriberId"},
            "Organization": {"identifier", "name"},
            "Practitioner": {"identifier", "name"},
        }
        kind = resource["resourceType"]
        if kind != "Claim" and (
            kind not in supported
            or not resource.keys() <= supported[kind] | {"resourceType", "id", "meta"}
        ):
            raise InvalidFHIR(f"Bundle.entry[{i}].resource", "unsupported-resource-field")
        if not resource.get("id"):
            raise InvalidFHIR(f"Bundle.entry[{i}].resource.id", "required")
        if resource["id"] in contained_ids:
            raise InvalidFHIR(f"Bundle.entry[{i}].resource.id", "duplicate-contained-id")
        contained_ids.add(resource["id"])
        ref = f"{resource['resourceType']}/{resource['id']}"
        for key in {ref, entry.get("fullUrl", ref)}:
            if key in references:
                raise InvalidFHIR(f"Bundle.entry[{i}]", "duplicate-reference")
            references[key] = resource
        if resource["resourceType"] == "Claim":
            claims.append(resource)
    if len(claims) != 1:
        raise InvalidFHIR("Bundle.entry", "one-claim-required")
    claim = claims[0]
    supported_claim_fields = {
        "resourceType",
        "id",
        "identifier",
        "status",
        "type",
        "use",
        "patient",
        "created",
        "insurer",
        "provider",
        "priority",
        "insurance",
        "diagnosis",
        "item",
        "meta",
    }
    if not claim.keys() <= supported_claim_fields:
        raise InvalidFHIR("Claim", "unsupported-claim-field")
    if len(claim["insurance"]) != 1:
        raise InvalidFHIR("Claim.insurance", "single-coverage-supported")
    if not claim["insurance"][0].keys() <= {"sequence", "focal", "coverage", "id"}:
        raise InvalidFHIR("Claim.insurance", "unsupported-insurance-field")

    def resolve(ref: dict, expected: str, location: str):
        result = references.get(ref.get("reference", ""))
        if result is None or result["resourceType"] != expected:
            raise InvalidFHIR(location, "unresolved-reference")
        return result

    def check_refs(value, path="Bundle"):
        if isinstance(value, dict):
            if "reference" in value and value["reference"] not in references:
                raise InvalidFHIR(path, "unresolved-reference")
            for key, child in value.items():
                check_refs(child, f"{path}.{key}")
        elif isinstance(value, list):
            for i, child in enumerate(value):
                check_refs(child, f"{path}[{i}]")

    check_refs(bundle)
    if claim["use"] != "preauthorization" or claim["status"] != "active":
        raise InvalidFHIR("Claim.use")
    patient = resolve(claim["patient"], "Patient", "Claim.patient")
    insurer = resolve(claim.get("insurer", {}), "Organization", "Claim.insurer")
    provider = references.get(claim["provider"].get("reference"))
    if not provider or provider["resourceType"] not in {"Practitioner", "Organization"}:
        raise InvalidFHIR("Claim.provider")
    focal = [x for x in claim["insurance"] if x.get("focal") is True]
    if len(focal) != 1:
        raise InvalidFHIR("Claim.insurance", "one-focal-coverage-required")
    coverage = resolve(focal[0]["coverage"], "Coverage", "Claim.insurance.coverage")
    if (
        not patient.get("identifier")
        or not all(x.get("system") and x.get("value") for x in patient["identifier"])
        or not coverage.get("subscriberId")
    ):
        raise InvalidFHIR("Coverage.subscriberId", "member-identifiers-required")
    if any(not x.keys() <= {"system", "value"} for x in patient["identifier"]):
        raise InvalidFHIR("Patient.identifier", "unsupported-member-identifier-field")
    if (
        coverage["status"] != "active"
        or resolve(coverage["beneficiary"], "Patient", "Coverage.beneficiary") != patient
    ):
        raise InvalidFHIR("Coverage.beneficiary")
    if not any(resolve(x, "Organization", "Coverage.payor") == insurer for x in coverage["payor"]):
        raise InvalidFHIR("Coverage.payor")
    routing = [
        x.get("value")
        for x in insurer.get("identifier", [])
        if x.get("system") == "urn:example:payer-id"
    ]
    identifiers = [x for x in claim.get("identifier", []) if x.get("system") and x.get("value")]
    if len(routing) != 1 or routing[0] not in {"alpha", "beta"}:
        raise InvalidFHIR("Claim.insurer", "unknown-payer")
    if len(identifiers) != 1:
        raise InvalidFHIR("Claim.identifier", "one-business-identifier-required")
    if len(identifiers[0]["system"]) > 512 or len(identifiers[0]["value"]) > 128:
        raise InvalidFHIR("Claim.identifier", "identifier-too-long")
    diagnoses = claim.get("diagnosis", [])
    items = claim.get("item", [])
    if not diagnoses or not items or len(items) > 20:
        raise InvalidFHIR("Claim.item", "items-and-diagnoses-required")
    for group, name in (
        (items, "item"),
        (diagnoses, "diagnosis"),
        (claim["insurance"], "insurance"),
    ):
        sequences = [x.get("sequence") for x in group]
        if any(type(x) is not int or x <= 0 for x in sequences):
            raise InvalidFHIR(f"Claim.{name}.sequence", "positive-sequence-required")
        if len(sequences) != len(set(sequences)):
            raise InvalidFHIR(f"Claim.{name}.sequence", "duplicate-sequence")
    diagnosis_ids = {x["sequence"] for x in diagnoses}
    for diagnosis in diagnoses:
        if not diagnosis.keys() <= {"sequence", "diagnosisCodeableConcept", "id"}:
            raise InvalidFHIR("Claim.diagnosis", "unsupported-diagnosis-field")
        if not diagnosis.get("diagnosisCodeableConcept", {}).get("coding"):
            raise InvalidFHIR("Claim.diagnosis", "coded-diagnosis-required")
        if not all(
            x.get("system") and x.get("code")
            for x in diagnosis["diagnosisCodeableConcept"]["coding"]
        ):
            raise InvalidFHIR("Claim.diagnosis", "coded-diagnosis-required")
    for item in items:
        if not item.keys() <= {
            "sequence",
            "productOrService",
            "quantity",
            "servicedDate",
            "servicedPeriod",
            "diagnosisSequence",
            "id",
        }:
            raise InvalidFHIR("Claim.item", "unsupported-service-field")
        if item.get("quantity", {}).get("value", 1) <= 0:
            raise InvalidFHIR("Claim.item.quantity", "positive-quantity-required")
        if "quantity" in item and "value" not in item["quantity"]:
            raise InvalidFHIR("Claim.item.quantity.value", "required")
        if not item.get("diagnosisSequence") or not set(item["diagnosisSequence"]) <= diagnosis_ids:
            raise InvalidFHIR("Claim.item.diagnosisSequence")
        coding = item["productOrService"].get("coding", [])
        if (
            len(coding) != 1
            or not coding[0].get("code")
            or coding[0].get("system")
            not in {
                "http://www.ama-assn.org/go/cpt",
                "https://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets",
            }
        ):
            raise InvalidFHIR("Claim.item.productOrService")
        if bool(item.get("servicedDate")) == bool(item.get("servicedPeriod")):
            raise InvalidFHIR("Claim.item.servicedDate")
    parsed = {
        "claim": claim,
        "payer": routing[0],
        "business_system": identifiers[0]["system"],
        "business_value": identifiers[0]["value"],
        "code": items[0]["productOrService"]["coding"][0]["code"],
    }
    if parsed["payer"] == "beta":
        try:
            # The UUID is allocated only after admission; it cannot affect the
            # static wire constraints checked here.
            LegacyRequest.model_validate_json(canonical(map_legacy(bundle, parsed, UUID(int=0))))
        except ValidationError as exc:
            raise InvalidFHIR("Bundle", "unsupported-legacy-contract") from exc
    return parsed


def claim_response(row: dict) -> dict:
    claim = next(
        e["resource"] for e in row["bundle"]["entry"] if e["resource"]["resourceType"] == "Claim"
    )
    decision = row["decision"]
    outcome = "complete" if decision in {"APPROVED", "DENIED"} else "queued"
    if row["delivery_state"] in {"FAILED", "EXPIRED"} and decision == "UNKNOWN":
        outcome = "error"
    # Return a self-contained resource. Include only resources reachable from the
    # Claim so FHIR's contained-resource reference invariant is respected.
    resources = [deepcopy(entry["resource"]) for entry in row["bundle"]["entry"]]
    # Preserve bundle reference semantics without advertising general resource storage.
    by_ref = {}
    for entry in row["bundle"]["entry"]:
        res = entry["resource"]
        by_ref[f"{res['resourceType']}/{res['id']}"] = res["id"]
        by_ref[entry.get("fullUrl", "")] = res["id"]
    for resource in resources:

        def rewrite(value):
            if isinstance(value, dict):
                if "reference" in value:
                    value["reference"] = "#" + by_ref[value["reference"]]
                for child in value.values():
                    rewrite(child)
            elif isinstance(value, list):
                for child in value:
                    rewrite(child)

        rewrite(resource)
    reachable = {claim["id"]}

    def collect(value):
        if isinstance(value, dict):
            if "reference" in value:
                reachable.add(value["reference"].removeprefix("#"))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for _ in resources:
        for resource in resources:
            if resource["id"] in reachable:
                collect(resource)
    resources = [r for r in resources if r["id"] in reachable]
    response = {
        "resourceType": "ClaimResponse",
        "id": str(row["id"]),
        "status": "active",
        "type": claim["type"],
        "use": "preauthorization",
        "patient": {"reference": "#" + by_ref[claim["patient"]["reference"]]},
        "created": row["created_at"].isoformat()
        if isinstance(row["created_at"], datetime)
        else row["created_at"],
        "insurer": {"reference": "#" + by_ref[claim["insurer"]["reference"]]},
        "request": {"reference": "#" + claim["id"]},
        "outcome": outcome,
        "disposition": decision,
        "contained": resources,
        "extension": [
            {
                "url": "https://example.org/fhir/StructureDefinition/gateway-delivery-state",
                "valueCode": row["delivery_state"],
            }
        ],
        "meta": {"versionId": str(row["version"])},
    }
    if row.get("preauth_ref"):
        response["preAuthRef"] = row["preauth_ref"]
    validate_resource(response)
    return response


def claim_resource(row: dict) -> dict:
    response = claim_response(row)
    claim = next(x for x in response["contained"] if x["resourceType"] == "Claim")
    old_id = claim["id"]
    claim["contained"] = [x for x in response["contained"] if x is not claim]
    claim["id"] = str(row["id"])

    def rewrite(value):
        if isinstance(value, dict):
            if value.get("reference") == "#" + old_id:
                value["reference"] = "#"
            for child in value.values():
                rewrite(child)
        elif isinstance(value, list):
            for child in value:
                rewrite(child)

    rewrite(claim)
    validate_resource(claim)
    return claim


def fixture(payer="alpha", code="70551", business_key="demo-001") -> dict:
    def entry(resource):
        return {
            "fullUrl": f"https://synthetic.example/fhir/{resource['resourceType']}/{resource['id']}",
            "resource": resource,
        }

    patient = {
        "resourceType": "Patient",
        "id": "patient-synthetic",
        "identifier": [{"system": "urn:example:synthetic", "value": "FAKE-001"}],
        "name": [{"family": "Synthetic", "given": ["Demo"]}],
        "birthDate": "1980-01-01",
        "gender": "unknown",
    }
    insurer = {
        "resourceType": "Organization",
        "id": "payer",
        "identifier": [{"system": "urn:example:payer-id", "value": payer}],
        "name": f"Synthetic {payer}",
    }
    provider = {"resourceType": "Organization", "id": "provider", "name": "Synthetic Clinic"}
    coverage = {
        "resourceType": "Coverage",
        "id": "coverage",
        "status": "active",
        "beneficiary": {"reference": "Patient/patient-synthetic"},
        "payor": [{"reference": "Organization/payer"}],
        "subscriberId": "FAKE-MEMBER",
    }
    claim = {
        "resourceType": "Claim",
        "id": "claim",
        "identifier": [{"system": "urn:example:claim", "value": business_key}],
        "status": "active",
        "type": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/claim-type",
                    "code": "professional",
                }
            ]
        },
        "use": "preauthorization",
        "patient": {"reference": "Patient/patient-synthetic"},
        "created": datetime.now(UTC).date().isoformat(),
        "insurer": {"reference": "Organization/payer"},
        "provider": {"reference": "Organization/provider"},
        "priority": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/processpriority",
                    "code": "normal",
                }
            ]
        },
        "insurance": [
            {"sequence": 1, "focal": True, "coverage": {"reference": "Coverage/coverage"}}
        ],
        "diagnosis": [
            {
                "sequence": 1,
                "diagnosisCodeableConcept": {
                    "coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "M25.561"}]
                },
            }
        ],
        "item": [
            {
                "sequence": 1,
                "productOrService": {
                    "coding": [
                        {
                            "system": "https://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets"
                            if code[0].isalpha()
                            else "http://www.ama-assn.org/go/cpt",
                            "code": code,
                        }
                    ]
                },
                "quantity": {"value": 1},
                "servicedDate": "2027-01-15",
                "diagnosisSequence": [1],
            }
        ],
    }
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [entry(x) for x in (claim, patient, coverage, insurer, provider)],
    }
