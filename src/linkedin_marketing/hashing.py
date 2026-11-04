"""SHA-256 hashing of personal identifiers before they leave the machine.

LinkedIn's Conversions and Matched Audiences APIs take emails as
``SHA256_EMAIL``: lower-cased, all whitespace removed, SHA-256, hex. Plain
emails are never sent; callers may pass either a plain email (hashed here)
or an already-hashed 64-character hex value (passed through, lower-cased).

First and last names for the Conversions API's ``hashedFirstName`` /
``hashedLastName`` (LinkedIn-Version 202609+) are normalised as the schema
asks: lower-cased, with whitespace and punctuation removed, UTF-8, SHA-256.

Docs: https://learn.microsoft.com/linkedin/marketing/integrations/ads-reporting/conversions-api
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
_WHITESPACE = re.compile(r"\s+")


def normalise_email(email: str) -> str:
    return _WHITESPACE.sub("", email).lower()


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_sha256_hex(value: str) -> bool:
    return bool(_SHA256_HEX.match(value))


def hash_email(email: str) -> str:
    """The ``SHA256_EMAIL`` id value for ``email`` (pre-hashed values pass through)."""
    value = email.strip()
    if is_sha256_hex(value):
        return value.lower()
    if "@" not in value:
        raise ValueError("Not an email address or a SHA-256 hex digest.")
    return sha256_hex(normalise_email(value))


def normalise_name(name: str) -> str:
    """Lower-cased, without whitespace or Unicode punctuation (``O'Brien`` → ``obrien``)."""
    kept = (
        char
        for char in name.lower()
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )
    return "".join(kept)


def hash_name(name: str) -> str:
    """The ``hashedFirstName``/``hashedLastName`` value (pre-hashed values pass through)."""
    value = name.strip()
    if is_sha256_hex(value):
        return value.lower()
    normalised = normalise_name(value)
    if not normalised:
        raise ValueError("A name to hash must contain letters.")
    return sha256_hex(normalised)
