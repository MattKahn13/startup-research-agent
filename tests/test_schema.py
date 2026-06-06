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


from schema import StartupRecord


def _good_aff():
    return CornellianAffiliation(
        name="Sandy Weill", school="CU", role="alumnus",
        grad_year=1955, role_at_company="founder",
        evidence_span="Weill", source_url="https://en.wikipedia.org/wiki/Sandy_Weill",
    )


def test_minimum_valid_record():
    r = StartupRecord(
        company_name="Citigroup",
        cornellians=[_good_aff()],
        proof_url="https://en.wikipedia.org/wiki/Citigroup",
    )
    assert r.status == "unknown"
    assert r.cornellians[0].name == "Sandy Weill"


def test_empty_cornellians_rejected():
    with pytest.raises(ValidationError):
        StartupRecord(
            company_name="x", cornellians=[], proof_url="https://x",
        )


def test_funding_coercion_from_string():
    r = StartupRecord(
        company_name="A", cornellians=[_good_aff()],
        proof_url="https://x", funding_total_usd="$12M",
    )
    assert r.funding_total_usd == 12_000_000


def test_status_enum_enforced():
    with pytest.raises(ValidationError):
        StartupRecord(
            company_name="A", cornellians=[_good_aff()],
            proof_url="https://x", status="zombie",
        )


def test_acquisition_amount_coerced():
    r = StartupRecord(
        company_name="A", cornellians=[_good_aff()],
        proof_url="https://x", status="acquired",
        acquisition_amount_usd="$1.2B",
    )
    assert r.acquisition_amount_usd == 1_200_000_000


from schema import ExtractionResult, SearchStrategy, GapItem


def test_extraction_result_round_trips():
    r = ExtractionResult(records=[
        StartupRecord(company_name="A", cornellians=[_good_aff()], proof_url="https://x"),
    ], notes="ok")
    s = r.model_dump_json()
    r2 = ExtractionResult.model_validate_json(s)
    assert r2.records[0].company_name == "A"


def test_search_strategy_requires_queries():
    with pytest.raises(ValidationError):
        SearchStrategy(name="x", rationale="y", queries=[])


def test_gap_item_tier_enforced():
    with pytest.raises(ValidationError):
        GapItem(record_id="a", missing_fields=["founders"],
                validation_tier="bogus", suggested_action="search")
