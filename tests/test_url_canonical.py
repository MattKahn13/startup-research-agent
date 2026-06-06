from url_canonical import canonicalize_url


def test_strips_utm_params():
    assert canonicalize_url("https://x.com/a?utm_source=foo&id=1") == "https://x.com/a?id=1"


def test_strips_all_tracking_params():
    url = "https://x.com/?utm_medium=a&utm_campaign=b&fbclid=c&gclid=d&id=1"
    assert canonicalize_url(url) == "https://x.com/?id=1"


def test_lowercases_host():
    assert canonicalize_url("HTTPS://EXAMPLE.COM/Path") == "https://example.com/Path"


def test_drops_trailing_slash_on_root():
    assert canonicalize_url("https://example.com/") == "https://example.com"


def test_wikipedia_unchanged_otherwise():
    url = "https://en.wikipedia.org/wiki/Sandy_Weill"
    assert canonicalize_url(url) == url


def test_none_passes_through():
    assert canonicalize_url(None) is None
