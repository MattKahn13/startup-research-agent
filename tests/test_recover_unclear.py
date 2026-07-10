from recover_unclear import build_recovery_prompt


def test_prompt_includes_page_text_and_asks_founder_yes_no():
    p = build_recovery_prompt(company="Ava Labs", person="Emin Gun Sirer",
                              page_text="Ava Labs was co-founded by Emin Gun Sirer and Ted Yin.")
    assert "Ava Labs" in p and "Emin Gun Sirer" in p
    assert "co-founded by Emin" in p
    assert "FOUNDER" in p and "NOT" in p  # asks for a founder/not decision
