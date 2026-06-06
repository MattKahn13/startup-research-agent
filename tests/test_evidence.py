from evidence import normalize, span_present


def test_normalize_collapses_whitespace_and_lowercases():
    assert normalize("Sandy   Weill\n\n(Cornell '55)") == "sandy weill (cornell '55)"


def test_span_present_exact():
    assert span_present(span="Sandy Weill", source="...by Sandy Weill ...")


def test_span_present_case_insensitive():
    assert span_present(span="sandy WEILL", source="founded by Sandy Weill in 1962")


def test_span_present_whitespace_tolerant():
    assert span_present(span="Sandy Weill", source="Sandy\nWeill founded Citigroup")


def test_span_absent():
    assert not span_present(span="Sandy Weill", source="Bob Smith founded the firm")


def test_empty_span_is_not_present():
    assert not span_present(span="", source="anything")
