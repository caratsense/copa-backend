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
