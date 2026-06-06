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
