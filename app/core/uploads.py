"""
Storing uploaded images.

Every upload route used to do this for itself, and four of the five took the
saved file's extension from the filename the browser sent. UPLOAD_DIR is served
by StaticFiles, which types a file by its extension, so a "photo" named
something.html or something.svg came back from /media as a live document on the
API's own origin. One of those routes is open to any signed-in customer
(review photos), so any account could publish a page there.

Now there is one path. The Content-Type header must name an image type, the
bytes must actually start like that kind of image, and the extension comes from
what the bytes are - never from the filename.
"""

import os
import re
import secrets
import uuid

from fastapi import HTTPException, UploadFile

from app.config import get_settings

settings = get_settings()

ALLOWED_TYPES = ("image/jpeg", "image/png", "image/webp")
_EXTENSION = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def sniff_image(head: bytes) -> str | None:
    """The image type these leading bytes belong to, or None if not one we take."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def save_image(file: UploadFile, prefix: str) -> str:
    """
    Validate an uploaded image, write it to UPLOAD_DIR and return its /media URL.

    `prefix` names the file (e.g. "review_12"); a random suffix keeps names
    unguessable and stops one upload overwriting another.
    """
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Use JPEG, PNG, or WebP")

    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    # One byte past the limit is enough to know it is over, without reading a
    # body of any size into memory first.
    contents = file.file.read(limit + 1)
    if len(contents) > limit:
        raise HTTPException(status_code=400, detail=f"Max {settings.MAX_UPLOAD_MB}MB")

    kind = sniff_image(contents[:16])
    if kind is None:
        raise HTTPException(status_code=400, detail="That file is not a JPEG, PNG, or WebP image")

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    filename = f"{prefix}_{uuid.uuid4().hex[:8]}.{_EXTENSION[kind]}"
    with open(os.path.join(settings.UPLOAD_DIR, filename), "wb") as f:
        f.write(contents)
    return f"/media/{filename}"


# ─── DESIGN REFERENCES ────────────────────────────────
# A picture of the cake a customer wants, chosen in Build a Cake — which is
# BEFORE the order it belongs to exists. There is nothing to hang it on yet, so
# it is written to UPLOAD_DIR on its own and the URL travels in the cart; order
# creation claims it onto the order as an attachment (see
# order_service._attach_design_reference).
#
# Staging it on the server at the moment it is picked, rather than carrying the
# file through the cart, is not a preference: a File cannot be put in
# localStorage, and the cart survives page loads and a round trip to the
# payment gateway. The alternative was what used to happen — the picture was
# never sent at all and only its FILENAME travelled, written into the cake's
# message field, where it was piped onto the cake in icing and pushed the field
# past its 60-character limit. An order was lost that way.
#
# The name carries 32 hex characters of randomness, not the 8 a review photo
# gets. These files are served by StaticFiles with no authentication — the
# brief's pictures always have been — and a reference is a photograph from
# somebody's phone, so the URL is the only thing protecting it and guessing has
# to be hopeless rather than merely unlikely.

_REFERENCE_URL = re.compile(r"^/media/ref_[0-9a-f]{24}_[0-9a-f]{8}\.(?:jpg|png|webp)$")


def new_reference_prefix() -> str:
    """A one-off name for a staged reference, before any order owns it."""
    return f"ref_{secrets.token_hex(12)}"


def staged_reference_file(url: str) -> str | None:
    """
    The file on disk a staged-reference URL names, or None.

    None covers both "that is not a staged reference" and "it is not there any
    more", because the caller does the same thing either way and neither is
    worth telling apart. The regex pins the WHOLE string, so the basename taken
    from it cannot climb out of UPLOAD_DIR.
    """
    if not url or not _REFERENCE_URL.match(url):
        return None
    path = os.path.join(settings.UPLOAD_DIR, url.rsplit("/", 1)[-1])
    return path if os.path.isfile(path) else None


# The same shape, for the schema that has to refuse a URL the customer's
# browser did not get from us. Kept here so the rule that writes these names
# and the rule that accepts them cannot drift apart.
REFERENCE_URL_PATTERN = _REFERENCE_URL.pattern


# ─── AUDIO ────────────────────────────────────────────
# Voice notes left on an order. Held to exactly the same rule as images: the
# extension comes from what the bytes are, never from the name the browser
# sent, because UPLOAD_DIR is served by StaticFiles and StaticFiles types a
# file by its extension. A ".webm" that is really HTML would otherwise come
# back from /media as a live document on the API's own origin.

_AUDIO_EXTENSION = {
    "audio/webm": "webm",     # what Chrome's MediaRecorder produces
    "audio/mp4": "m4a",       # what Safari's does
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
}


def sniff_audio(head: bytes) -> str | None:
    """The audio type these leading bytes belong to, or None if not one we take."""
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        # EBML. Matroska and WebM share it; a browser recording is WebM.
        return "audio/webm"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "audio/mp4"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # MPEG frame sync: an MP3 with no ID3 tag in front of it.
        return "audio/mpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    return None


def save_audio(file: UploadFile, prefix: str) -> tuple[str, int]:
    """
    Validate an uploaded recording, write it to UPLOAD_DIR, return its /media
    URL and its size in bytes.

    The size comes back because the caller cannot get it afterwards without
    reading the file again, and the UI wants to show how long a note is before
    anyone commits to playing it.
    """
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    contents = file.file.read(limit + 1)
    if len(contents) > limit:
        raise HTTPException(status_code=400, detail=f"Max {settings.MAX_UPLOAD_MB}MB")
    if not contents:
        raise HTTPException(status_code=400, detail="That recording is empty")

    kind = sniff_audio(contents[:16])
    if kind is None:
        raise HTTPException(
            status_code=400,
            detail="That file is not a recording we can play back",
        )

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    filename = f"{prefix}_{uuid.uuid4().hex[:8]}.{_AUDIO_EXTENSION[kind]}"
    with open(os.path.join(settings.UPLOAD_DIR, filename), "wb") as f:
        f.write(contents)
    return f"/media/{filename}", len(contents)
