# schema.py
from __future__ import annotations
from typing import Literal, Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


SchoolType = Literal["CU", "Cornell Tech", "Weill", "Vet", "unknown"]
CornellRole = Literal["alumnus", "faculty", "student", "postdoc", "researcher"]
CompanyRole = Literal["founder", "cofounder", "ceo", "cto",
                      "early_employee", "board", "investor", "advisor"]


class CornellianAffiliation(BaseModel):
    name: str
    school: SchoolType
    role: CornellRole
    grad_year: Optional[int] = None
    role_at_company: CompanyRole
    evidence_span: str
    source_url: str

    @field_validator("grad_year")
    @classmethod
    def grad_year_plausible(cls, v):
        if v is None:
            return v
        if not (1860 <= v <= 2030):
            raise ValueError(f"grad_year {v} out of plausible range")
        return v

    @field_validator("evidence_span")
    @classmethod
    def evidence_span_nonempty(cls, v):
        if not v or not v.strip():
            raise ValueError("evidence_span must be non-empty")
        return v


import re

FundingStage = Literal["pre-seed", "seed", "series-a", "series-b", "series-c",
                       "series-d", "series-e", "growth", "public", "unknown"]
StatusType = Literal["active", "acquired", "shutdown", "ipo", "unknown"]
TierType = Literal["high", "provisional", "weak"]


_MONEY_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([MmBbKk]?)")
_MULT = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "": 1}


def _coerce_money(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = _MONEY_RE.search(str(v).strip())
    if not m:
        raise ValueError(f"cannot parse money: {v!r}")
    raw, suffix = m.group(1).replace(",", ""), m.group(2).upper()
    return int(float(raw) * _MULT[suffix])


class StartupRecord(BaseModel):
    company_name: str
    cornellians: list[CornellianAffiliation] = Field(min_length=1)
    proof_url: str

    description: Optional[str] = None
    industry: Optional[str] = None
    funding_total_usd: Optional[int] = None
    funding_stage: Optional[FundingStage] = None
    funding_last_round_year: Optional[int] = None
    founded_year: Optional[int] = None
    employee_count: Optional[int] = None
    is_public: Optional[bool] = None
    headquarters: Optional[str] = None

    status: StatusType = "unknown"
    exit_year: Optional[int] = None
    acquirer: Optional[str] = None
    acquisition_amount_usd: Optional[int] = None

    website_url: Optional[str] = None
    linkedin_company_url: Optional[str] = None
    crunchbase_url: Optional[str] = None

    tags: list[str] = Field(default_factory=list)
    non_cornell_cofounder_schools: list[str] = Field(default_factory=list)

    first_seen_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    validation_tier: TierType = "weak"
    validation_issues: list[str] = Field(default_factory=list)

    @field_validator("funding_total_usd", "acquisition_amount_usd", mode="before")
    @classmethod
    def coerce_money(cls, v):
        return _coerce_money(v)

    @field_validator("founded_year", "exit_year", "funding_last_round_year", mode="before")
    @classmethod
    def coerce_year(cls, v):
        if v is None or v == "":
            return None
        try:
            n = int(str(v).strip())
        except ValueError:
            raise ValueError(f"cannot parse year: {v!r}")
        if not (1700 <= n <= 2030):
            raise ValueError(f"year {n} out of plausible range")
        return n

    @field_validator("employee_count", mode="before")
    @classmethod
    def coerce_int(cls, v):
        if v is None or v == "":
            return None
        try:
            return int(str(v).replace(",", "").strip())
        except ValueError:
            raise ValueError(f"cannot parse int: {v!r}")
