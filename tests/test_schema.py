import pytest
from pydantic import ValidationError
from schema import CornellianAffiliation


def test_minimum_valid_affiliation():
    a = CornellianAffiliation(
        name="Sandy Weill",
        school="CU",
        role="alumnus",
        grad_year=1955,
        role_at_company="founder",
        evidence_span="Sandy Weill (Cornell '55) founded Citigroup",
        source_url="https://en.wikipedia.org/wiki/Sandy_Weill",
    )
    assert a.school == "CU"


def test_invalid_school_rejected():
    with pytest.raises(ValidationError):
        CornellianAffiliation(
            name="X", school="Harvard", role="alumnus",
            grad_year=None, role_at_company="founder",
            evidence_span="x", source_url="https://x",
        )


def test_invalid_role_rejected():
    with pytest.raises(ValidationError):
        CornellianAffiliation(
            name="X", school="CU", role="janitor",
            grad_year=None, role_at_company="founder",
            evidence_span="x", source_url="https://x",
        )


def test_grad_year_out_of_range_rejected():
    with pytest.raises(ValidationError):
        CornellianAffiliation(
            name="X", school="CU", role="alumnus",
            grad_year=1750, role_at_company="founder",
            evidence_span="x", source_url="https://x",
        )
