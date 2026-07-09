from verify.confidence import score_edge, SIGNAL_WEIGHTS


def test_directory_plus_api_agreement_is_high_confidence():
    s = score_edge(source_tier="directory", corroborations=2,
                   api_confirmed=True, cornell_tie="strong", llm_verdict="FOUNDER")
    assert s["confidence"] >= 0.85
    assert s["publishable"] is True
    assert "directory-source" in s["provenance"]


def test_single_low_source_no_api_is_low_confidence():
    s = score_edge(source_tier="mention", corroborations=1,
                   api_confirmed=False, cornell_tie="weak", llm_verdict="UNCLEAR")
    assert s["confidence"] < 0.5
    assert s["publishable"] is False


def test_api_confirmation_alone_can_publish_without_llm():
    # structured-agreement shortcut: two authoritative sources agree
    s = score_edge(source_tier="mention", corroborations=2,
                   api_confirmed=True, cornell_tie="strong", llm_verdict=None)
    assert s["publishable"] is True
