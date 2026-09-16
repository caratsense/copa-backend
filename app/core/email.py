"""
Email normalisation and validation.

Optional on a customer account, so "not given" and "given but malformed" are
different answers: a blank field normalises to None, while a value that cannot
be an address is rejected. Storing a malformed one is not harmless - it is what
receipts and password resets are sent to.

Deliberately a regex rather than pydantic's EmailStr: that needs the
email-validator package, which this project does not ship, and adding a
dependency to check a shape we can state in one line is not worth it. The rule
is the practical one - a local part, an @, a domain with a dot and a 2+ letter
tail - not RFC 5322, which allows addresses no mail provider accepts.
"""

import re

from fastapi import HTTPException

_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)*\.[A-Za-z]{2,}$")


def normalize_email(raw: str | None) -> str | None:
    """
    Return a trimmed email, or None when nothing was given.

    Raises 422 for a value that is present but not a plausible address, so the
    account is not created with somewhere we can never write to.
    """
    if raw is None:
        return None

    value = str(raw).strip()
    if not value:
        # An empty string is "no email", not a malformed one. The web form
        # posts "" for an untouched optional field.
        return None

    if len(value) > 254 or not _EMAIL.match(value):
        raise HTTPException(
            status_code=422,
            detail=f"'{value}' is not a valid email address.",
        )

    return value
