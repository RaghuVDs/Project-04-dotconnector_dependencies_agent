import redactor


def test_redact_email():
    assert redactor.redact("ping me at jane.doe@example.com") == "ping me at [EMAIL]"


def test_redact_ssn():
    assert redactor.redact("ssn 123-45-6789 on file") == "ssn [SSN] on file"


def test_redact_card():
    assert "[CARD]" in redactor.redact("card 4111 1111 1111 1111")


def test_redaction_count():
    sample = "a@b.com and 123-45-6789"
    assert redactor.redaction_count(sample) == 2


def test_no_pii_unchanged():
    assert redactor.redact("nothing to see here") == "nothing to see here"
