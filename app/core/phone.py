"""
Phone number normalisation and validation.

The phone number IS the login identifier and the WhatsApp address, so a bad one
is not a cosmetic problem: the account cannot be signed into and cannot be
messaged. Staff were being created with an email address in the phone column
(a browser autofilled it into an untyped text input), producing accounts that
looked fine in the admin list and could never log in.

Indian mobile numbers only, which is what the business delivers to.
"""

import re

from fastapi import HTTPException

# 10 digits starting 6-9, optionally prefixed with 91 / +91 / 0.
_DIGITS = re.compile(r"\D")
_VALID = re.compile(r"^[6-9]\d{9}$")


def normalize_phone(raw: str | None) -> str:
    """
    Return a phone as +91XXXXXXXXXX.

    Raises 422 for anything that is not a plausible Indian mobile number, so a
    typo or an autofilled email is rejected at the edge rather than stored.
    """
    if raw is None or not str(raw).strip():
        raise HTTPException(status_code=422, detail="Phone number is required")

    value = str(raw).strip()

    # Catch the specific failure that caused this: an email in the phone field.
    if "@" in value:
        raise HTTPException(
            status_code=422,
            detail=("That looks like an email address, not a phone number. "
                    "Enter a 10-digit mobile number, e.g. 9876543210."),
        )

    digits = _DIGITS.sub("", value)

    # Strip the country code / trunk prefix down to the bare 10 digits.
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if not _VALID.match(digits):
        raise HTTPException(
            status_code=422,
            detail=(f"'{value}' is not a valid Indian mobile number. "
                    "Enter 10 digits starting with 6, 7, 8 or 9."),
        )

    return f"+91{digits}"


def same_number(a: str | None, b: str | None) -> bool:
    """Whether two numbers refer to the same handset, ignoring formatting."""
    def bare(v):
        d = _DIGITS.sub("", v or "")
        return d[-10:] if len(d) >= 10 else d
    return bool(bare(a)) and bare(a) == bare(b)
