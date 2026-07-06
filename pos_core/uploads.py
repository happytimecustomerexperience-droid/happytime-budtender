"""Upload validation: bound size, allowlist + magic-byte sniff, image verify.

Vendored from the monorepo `core/utils/uploads.py` (C-3 hardening). Use at every
multipart upload site BEFORE buffering the whole file into memory. Size cap reads
the new project's `ID_SCAN_MAX_IMAGE_BYTES` setting (default 6 MB).
"""

from __future__ import annotations

import io

from django.conf import settings
from django.core.exceptions import ValidationError

DEFAULT_MAX_IMAGE_BYTES = 6 * 1024 * 1024
_MAX_IMAGE_PIXELS = 40_000_000  # decompression-bomb guard (~40 MP)
_MAX_ID_IMAGES = 2

ALLOWED_IMAGE_MIME = ("image/jpeg", "image/png", "image/webp", "image/gif")


def _max_image_bytes() -> int:
    return int(getattr(settings, "ID_SCAN_MAX_IMAGE_BYTES", DEFAULT_MAX_IMAGE_BYTES))


def _sniff_image_mime(head: bytes) -> str | None:
    """Return the image MIME from magic bytes, or None if unrecognized."""
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_bounded(f, max_bytes: int) -> bytes:
    """Read at most max_bytes (reads +1 to detect overflow). Checks declared size first."""
    if f is None:
        raise ValidationError("No file provided")
    size = getattr(f, "size", None)
    if size is not None and size > max_bytes:
        raise ValidationError(f"File too large ({size} bytes; max {max_bytes}).")
    try:
        f.seek(0)
    except (OSError, AttributeError, ValueError):
        pass
    data = f.read(max_bytes + 1)
    if not data:
        raise ValidationError("Empty upload")
    if len(data) > max_bytes:
        raise ValidationError(f"File too large (> {max_bytes} bytes).")
    return data


def _verify_image_bytes(data: bytes) -> None:
    """Decode-verify via Pillow (with a decompression-bomb pixel cap) if Pillow is installed."""
    try:
        from PIL import Image
    except ImportError:
        return  # magic-byte sniff already applied; Pillow is an optional hardening layer
    prev = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
    try:
        Image.open(io.BytesIO(data)).verify()
    except ValidationError:
        raise
    except Exception as exc:  # PIL raises a wide variety; treat all as invalid
        raise ValidationError("Invalid or corrupt image.") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = prev


def validate_image_upload(f, *, max_bytes: int | None = None, allowed_mime=ALLOWED_IMAGE_MIME) -> bytes:
    """Validate an uploaded image and return its bytes (read once).

    Size-capped BEFORE read, magic-byte sniffed, MIME-allowlisted, then Pillow
    `verify()` (decompression-bomb capped) when Pillow is available. Raises
    ValidationError on any violation.
    """
    if max_bytes is None:
        max_bytes = _max_image_bytes()
    data = _read_bounded(f, max_bytes)
    mime = _sniff_image_mime(data[:16])
    if mime is None:
        raise ValidationError("File is not a recognized image (magic-byte mismatch).")
    if allowed_mime and mime not in allowed_mime:
        raise ValidationError(f"Disallowed image type: {mime}.")
    _verify_image_bytes(data)
    return data


def collect_id_images(files) -> list[bytes]:
    """Validate + collect up to 2 ID images (mirrors the monorepo `_collect_id_images`).

    `files` is an iterable of uploaded file objects. Each is size-capped +
    magic-byte/Pillow-verified before reading; count-capped to 2. A single image
    is duplicated for both sides (n8n parity). Raises ValidationError on any bad
    upload or when no images are provided.
    """
    images = [validate_image_upload(f) for f in list(files)[:_MAX_ID_IMAGES]]
    if not images:
        raise ValidationError("No images provided")
    if len(images) == 1:
        images.append(images[0])
    return images
