# url_canonical.py
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse


_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
             "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid", "ref"}


def canonicalize_url(url: str | None) -> str | None:
    if url is None:
        return None
    p = urlparse(url.strip())
    if not p.scheme:
        return url
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
          if k.lower() not in _TRACKING]
    query = urlencode(qs)
    path = p.path
    if path == "/" and not query and not p.fragment:
        return f"{scheme}://{netloc}"
    return urlunparse((scheme, netloc, path, p.params, query, p.fragment))
