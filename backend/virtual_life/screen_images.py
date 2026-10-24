"""Decode, validate and normalize uploaded screenshots without trusting metadata.

All limits are enforced here in addition to the streaming byte cap, so a decoded
image can never exceed the configured pixels, side or format policy. Transparent
pixels are composited onto white and EXIF orientation is applied once, producing
one canonical RGB image that every downstream coordinate shares.
"""

import hashlib
import io
import math
from dataclasses import dataclass
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError

NORMALIZATION_VERSION = "screen-norm-v1"
PREPROCESSING_VERSION = "preprocess-v1"
ALGORITHM_VERSION = "screen-analysis-v1"
SUPPORTED_MEDIA_TYPES = ("image/png", "image/jpeg")
SUPPORTED_FORMATS = ("PNG", "JPEG")


class ImageValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    """A canonical PNG plus its normalized geometry and pixel digest."""

    data: bytes
    width: int
    height: int
    media_type: str
    pixel_sha256: str
    normalization_version: str = NORMALIZATION_VERSION

    def open(self) -> Image.Image:
        return Image.open(io.BytesIO(self.data))


def _bounded_dimensions(width: int, height: int, max_pixels: int, max_side: int) -> None:
    if width <= 0 or height <= 0:
        raise ImageValidationError("screen_image_invalid", "Image dimensions must be positive")
    if width > max_side or height > max_side:
        raise ImageValidationError("screen_image_too_large", "Image side exceeds the server limit")
    if width * height > max_pixels:
        raise ImageValidationError("screen_image_too_large", "Image pixel count exceeds the limit")


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )


def _flatten_on_white(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.split()[-1])
    return background


def validate_and_normalize(
    raw: bytes,
    *,
    max_bytes: int,
    max_pixels: int,
    max_side: int,
) -> NormalizedImage:
    """Fully decode and rebuild one RGB screenshot or raise a coded error."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ImageValidationError("screen_image_invalid", "Image content is empty")
    if len(raw) > max_bytes:
        raise ImageValidationError("screen_image_too_large", "Image bytes exceed the server limit")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            image_format = probe.format
            animated = bool(getattr(probe, "is_animated", False))
            width, height = probe.size
    except (UnidentifiedImageError, OSError, ValueError):
        raise ImageValidationError("screen_image_invalid", "Image could not be decoded") from None
    if image_format not in SUPPORTED_FORMATS:
        raise ImageValidationError(
            "screen_image_type_unsupported", "Only static PNG and JPEG are accepted"
        )
    if animated:
        raise ImageValidationError(
            "screen_image_type_unsupported", "Animated images are not accepted"
        )
    _bounded_dimensions(width, height, max_pixels, max_side)
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            oriented = ImageOps.exif_transpose(source)
            if oriented is None:
                oriented = source
            flattened = (
                _flatten_on_white(oriented) if _has_alpha(oriented) else oriented.convert("RGB")
            )
            normalized = flattened.copy()
    except ImageValidationError:
        raise
    except (Image.DecompressionBombError, OSError, ValueError):
        raise ImageValidationError("screen_image_invalid", "Image could not be decoded") from None
    _bounded_dimensions(normalized.width, normalized.height, max_pixels, max_side)
    pixels = normalized.tobytes()
    digest = hashlib.sha256()
    digest.update(NORMALIZATION_VERSION.encode("ascii"))
    digest.update(b"\x00")
    digest.update(str(normalized.width).encode("ascii"))
    digest.update(b"x")
    digest.update(str(normalized.height).encode("ascii"))
    digest.update(b"\x00")
    digest.update(pixels)
    buffer = io.BytesIO()
    normalized.save(buffer, format="PNG", optimize=False)
    canonical = buffer.getvalue()
    if len(canonical) > max_bytes:
        raise ImageValidationError(
            "screen_image_too_large", "Normalized image exceeds the server byte limit"
        )
    return NormalizedImage(
        data=canonical,
        width=normalized.width,
        height=normalized.height,
        media_type="image/png",
        pixel_sha256=digest.hexdigest(),
    )


def open_canonical(image: NormalizedImage) -> Image.Image:
    try:
        loaded = Image.open(io.BytesIO(image.data))
        loaded.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ImageValidationError("screen_image_invalid", "Stored image is unreadable") from None
    return loaded.convert("RGB")


def encode_model_image(
    image: Image.Image,
    *,
    long_side: int,
    media_type: Literal["image/png", "image/jpeg"] = "image/jpeg",
) -> bytes:
    """Downscale a working image so its longest side fits the model budget."""
    if long_side <= 0:
        raise ValueError("long_side must be positive")
    width, height = image.size
    scale = min(1.0, long_side / max(width, height))
    if scale < 1.0:
        resized = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS
        )
    else:
        resized = image
    buffer = io.BytesIO()
    if media_type == "image/png":
        resized.save(buffer, format="PNG")
    else:
        resized.convert("RGB").save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def crop_roi(image: Image.Image, bbox: list[float]) -> tuple[Image.Image, list[float]]:
    """Crop by a normalized bbox and return the same bbox for reverse mapping."""
    x1, y1, x2, y2 = bbox
    left = max(0, min(image.width, int(math.floor(x1 * image.width))))
    top = max(0, min(image.height, int(math.floor(y1 * image.height))))
    right = max(left + 1, min(image.width, int(math.ceil(x2 * image.width))))
    bottom = max(top + 1, min(image.height, int(math.ceil(y2 * image.height))))
    return image.crop((left, top, right, bottom)), bbox


def map_local_bbox(roi_bbox: list[float], local: list[float]) -> list[float]:
    """Map a bbox expressed relative to a ROI back into whole-image coordinates."""
    x1, y1, x2, y2 = roi_bbox
    width, height = x2 - x1, y2 - y1
    return [
        round(x1 + local[0] * width, 6),
        round(y1 + local[1] * height, 6),
        round(x1 + local[2] * width, 6),
        round(y1 + local[3] * height, 6),
    ]


def padded_bbox(bbox: list[float], padding_px: int, image: Image.Image) -> list[float]:
    pad_x = padding_px / image.width
    pad_y = padding_px / image.height
    return [
        max(0.0, bbox[0] - pad_x),
        max(0.0, bbox[1] - pad_y),
        min(1.0, bbox[2] + pad_x),
        min(1.0, bbox[3] + pad_y),
    ]
