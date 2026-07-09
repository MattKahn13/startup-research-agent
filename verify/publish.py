"""The single gate decision: (verdict + signals) -> verified | rejected(reason)
| needs_human. Pure. The published dataset is exactly the 'verified' set.
"""

_NONCOMPANY_TYPES = {"university_unit", "investment_fund", "foundation", "nonprofit",
                     "journal", "government_program"}
_NONFOUNDER = {"EMPLOYEE", "EXECUTIVE", "INVESTOR", "DONOR", "ATTENDEE", "NONCOMPANY"}


def decide(llm_verdict, company_real, entity_type, cornell_tie, confidence, contradiction):
    if contradiction:
        return {"state": "needs_human", "reason": "founder/evidence contradiction"}
    if entity_type in _NONCOMPANY_TYPES or company_real is False:
        return {"state": "rejected", "reason": f"not-a-company ({entity_type})"}
    if cornell_tie == "none":
        return {"state": "rejected", "reason": "no Cornell tie"}
    if llm_verdict in _NONFOUNDER:
        return {"state": "rejected", "reason": f"role={llm_verdict.lower()}"}
    if confidence >= 0.70 and llm_verdict in (None, "FOUNDER"):
        return {"state": "verified", "reason": "founding confirmed"}
    return {"state": "needs_human", "reason": f"insufficient confidence ({confidence})"}
