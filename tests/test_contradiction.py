from verify.contradiction import founder_matches_evidence


def test_matching_founder_and_evidence_is_consistent():
    assert founder_matches_evidence("Raj Mehra", "Raj Mehra, MBA '09, founded Sage") is True


def test_evidence_names_a_different_founder_flags_contradiction():
    # the Ava Labs case: record says Emin Gun Sirer, evidence names Ted Yin as founder
    assert founder_matches_evidence("Emin Gun Sirer",
                                    "founder Maofan 'Ted' Yin, M.S. '19") is False


def test_no_name_in_evidence_is_not_a_contradiction():
    # thin evidence (directory) -- absence is not a mismatch
    assert founder_matches_evidence("Will Bruey", "Will Bruey '11, MEng '12") is True
