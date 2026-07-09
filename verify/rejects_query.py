"""Answer Marx's 'what about X?' from the rejects log: verdict + evidence +
source, instantly. Pure.
"""


def why_excluded(company_name: str, rejects: list):
    key = (company_name or "").strip().lower()
    for r in rejects:
        if (r.get("company_name") or "").strip().lower() == key:
            return r
    return None
