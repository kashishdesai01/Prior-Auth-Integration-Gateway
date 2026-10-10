from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pagw.fhir import (
    InvalidFHIR,
    claim_response,
    digest,
    fixture,
    parse_submission,
    validate_resource,
)


@pytest.mark.parametrize("payer", ["alpha", "beta"])
@pytest.mark.parametrize("code", ["70551", "27447", "J0585", "99999"])
def test_fixtures(payer, code):
    assert parse_submission(fixture(payer, code))["code"] == code


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["entry"][0]["resource"].update(use="claim"),
        lambda b: b["entry"][0]["resource"]["patient"].update(reference="Patient/missing"),
        lambda b: b["entry"][2]["resource"].pop("status"),
        lambda b: b["entry"][0]["resource"]["item"][0].update(diagnosisSequence=[99]),
        lambda b: b["entry"].append(deepcopy(b["entry"][0])),
        lambda b: b["entry"][0]["resource"].update(item=[]),
        lambda b: b["entry"][3]["resource"]["identifier"][0].update(value="other"),
    ],
)
def test_invalid_references_and_rules(mutate):
    bundle = fixture()
    mutate(bundle)
    with pytest.raises(InvalidFHIR):
        parse_submission(bundle)


@pytest.mark.parametrize(
    "decision,state",
    [
        ("UNKNOWN", "RECEIVED"),
        ("PENDED", "SUBMITTED"),
        ("APPROVED", "SUBMITTED"),
        ("DENIED", "SUBMITTED"),
        ("UNKNOWN", "NEEDS_REVIEW"),
    ],
)
def test_output_valid_and_input_unchanged(decision, state):
    bundle = fixture()
    before = deepcopy(bundle)
    response = claim_response(
        dict(
            id=uuid4(),
            bundle=bundle,
            created_at=datetime.now(UTC),
            version=1,
            decision=decision,
            delivery_state=state,
            preauth_ref="SYNTHETIC" if decision == "APPROVED" else None,
        )
    )
    validate_resource(response)
    assert bundle == before
    contained = {r["id"] for r in response["contained"]}
    assert response["request"]["reference"].removeprefix("#") in contained


@given(st.dictionaries(st.text(min_size=1, max_size=5), st.integers(), max_size=10))
def test_canonical_object_key_order(value):
    assert digest(value) == digest(dict(reversed(list(value.items()))))


def test_hcpcs_fixture_uses_its_own_namespace():
    parsed = parse_submission(fixture(code="J0585"))
    assert (
        parsed["claim"]["item"][0]["productOrService"]["coding"][0]["system"]
        == "https://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets"
    )
