"""The separate legacy partner wire contract; it contains no FHIR Bundle."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Identifier(StrictModel):
    system: str = Field(min_length=1, max_length=512)
    value: str = Field(min_length=1, max_length=128)


class ClaimNumber(StrictModel):
    namespace: str = Field(min_length=1, max_length=512)
    value: str = Field(min_length=1, max_length=128)


class Member(StrictModel):
    identifiers: list[Identifier] = Field(min_length=1, max_length=10)
    names: list[dict] = Field(max_length=10)
    birthDate: str | None
    gender: str | None = None
    subscriberId: str = Field(min_length=1, max_length=128)


class Provider(StrictModel):
    identifiers: list[dict] = Field(max_length=10)
    name: str | list[dict] | None


class Diagnosis(StrictModel):
    lineNumber: int = Field(gt=0)
    codes: list[dict] = Field(min_length=1, max_length=10)


class Service(StrictModel):
    lineNumber: int = Field(gt=0)
    procedure: dict
    quantity: dict
    serviceDate: str | None
    servicePeriod: dict | None
    diagnosisLines: list[int] = Field(min_length=1, max_length=20)


class LegacyRequest(StrictModel):
    externalRequestId: UUID
    claimNumber: ClaimNumber
    claimType: dict
    priority: dict
    member: Member
    requestingProvider: Provider
    diagnoses: list[Diagnosis] = Field(min_length=1, max_length=20)
    services: list[Service] = Field(min_length=1, max_length=20)


def map_legacy(bundle: dict, parsed: dict, request_id: UUID, *, version=2) -> dict:
    claim = parsed["claim"]
    resources = {}
    for entry in bundle["entry"]:
        resource = entry["resource"]
        resources[f"{resource['resourceType']}/{resource['id']}"] = resource
        resources[entry.get("fullUrl", "")] = resource
    patient = resources[claim["patient"]["reference"]]
    coverage = resources[next(x for x in claim["insurance"] if x["focal"])["coverage"]["reference"]]
    provider = resources[claim["provider"]["reference"]]
    payload = {
        "externalRequestId": str(request_id),
        "claimNumber": {"namespace": parsed["business_system"], "value": parsed["business_value"]},
        "claimType": claim["type"],
        "priority": claim["priority"],
        "member": {
            "identifiers": [
                {"system": x["system"], "value": x["value"]} for x in patient["identifier"]
            ],
            "names": patient.get("name", []),
            "birthDate": patient.get("birthDate"),
            "subscriberId": coverage["subscriberId"],
        },
        "requestingProvider": {
            "identifiers": provider.get("identifier", []),
            "name": provider.get("name"),
        },
        "diagnoses": [
            {"lineNumber": d["sequence"], "codes": d["diagnosisCodeableConcept"]["coding"]}
            for d in claim["diagnosis"]
        ],
        "services": [
            {
                "lineNumber": x["sequence"],
                "procedure": x["productOrService"]["coding"][0],
                "quantity": x.get("quantity", {"value": 1}),
                "serviceDate": x.get("servicedDate"),
                "servicePeriod": x.get("servicedPeriod"),
                "diagnosisLines": x["diagnosisSequence"],
            }
            for x in claim["item"]
        ],
    }
    if version == 2:
        payload["member"]["gender"] = patient.get("gender")
    return payload
