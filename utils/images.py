"""Image validation and base64 data-URI encoding (limits from Meshy Image-to-3D docs)."""
from __future__ import annotations

import base64
import hashlib
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

SUPPORTED_EXTENSIONS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
MAX_BYTES = 20 * 1024 * 1024
MIN_SIDE = 32
MAX_PIXELS = 178_956_970


class ImageValidationError(ValueError):
    pass


@dataclass
class ImageInfo:
    path: Path
    format: str
    width: int
    height: int
    size: int
    sha256: str

    @property
    def mime(self) -> str:
        return MIME_TYPES[self.format]


def is_supported_path(path: str | Path) -> bool:
    p = Path(path)
    return p.suffix.lower() in SUPPORTED_EXTENSIONS and not p.name.startswith(("~", "."))


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_image(path: str | Path) -> ImageInfo:
    p = Path(path)
    if not p.is_file():
        raise ImageValidationError(f"File not found: {p}")
    if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ImageValidationError(f"Unsupported file type '{p.suffix}'. Use PNG, JPG, JPEG or WebP.")
    size = p.stat().st_size
    if size == 0:
        raise ImageValidationError("File is empty.")
    if size > MAX_BYTES:
        raise ImageValidationError(f"File is {size / 1048576:.1f} MB; Meshy accepts at most 20 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            old_limit = Image.MAX_IMAGE_PIXELS
            Image.MAX_IMAGE_PIXELS = MAX_PIXELS
            try:
                with Image.open(p) as img:
                    fmt = img.format
                    width, height = img.size
                    img.verify()
            finally:
                Image.MAX_IMAGE_PIXELS = old_limit
    except Image.DecompressionBombError:
        raise ImageValidationError("Image has too many pixels for Meshy.") from None
    except Exception as exc:  # noqa: BLE001 - any decode failure means invalid image
        raise ImageValidationError(f"Not a readable image: {exc}") from None
    if fmt not in MIME_TYPES:
        raise ImageValidationError(f"Image content is {fmt}, not PNG/JPEG/WebP.")
    if width < MIN_SIDE or height < MIN_SIDE:
        raise ImageValidationError(f"Image is {width}x{height}; each side must be at least {MIN_SIDE}px.")
    if width * height > MAX_PIXELS:
        raise ImageValidationError(f"Image is {width}x{height}; maximum is {MAX_PIXELS:,} pixels.")
    return ImageInfo(p, fmt, width, height, size, file_sha256(p))


def to_data_uri(path: str | Path) -> str:
    """Validate and encode an image as a base64 data URI (mime from actual content)."""
    info = validate_image(path)
    data = Path(path).read_bytes()
    return f"data:{info.mime};base64,{base64.b64encode(data).decode('ascii')}"
