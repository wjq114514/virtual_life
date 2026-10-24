"""Cheap perceptual hashing and block change maps used only for routing.

P0 never drops a frame from these values: near-duplicate scores only pick
candidates and ROI suggestions. Exact reuse depends on the server-side
``pixel_sha256`` computed by the image pipeline.
"""

from collections import deque
from typing import Iterable

from PIL import Image, ImageChops

DEFAULT_MAD_THRESHOLD = 0.008
DEFAULT_DHASH_HAMMING = 2
DEFAULT_BLOCK_DIFF = 0.02
CHANGE_LONG_SIDE = 1536
CHANGE_BLOCK = 32
CHANGE_PADDING_PX = 12
MAX_CHANGE_REGIONS = 8


def _grayscale(image: Image.Image, size: tuple[int, int]) -> list[int]:
    resized = image.convert("L").resize(size, Image.Resampling.BILINEAR)
    return list(resized.tobytes())


def dhash64(image: Image.Image) -> int:
    """64-bit difference hash over a 9x8 grayscale image."""
    values = _grayscale(image, (9, 8))
    bits = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            bits = (bits << 1) | (1 if values[offset + column] < values[offset + column + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def mad_32(image: Image.Image) -> list[int]:
    return _grayscale(image, (32, 32))


def mean_absolute_difference(a: Iterable[int], b: Iterable[int]) -> float:
    total = 0
    count = 0
    for left, right in zip(a, b, strict=True):
        total += abs(left - right)
        count += 1
    return total / (count * 255.0) if count else 1.0


def near_duplicate(
    left: Image.Image,
    right: Image.Image,
    *,
    mad_threshold: float = DEFAULT_MAD_THRESHOLD,
    hamming_threshold: int = DEFAULT_DHASH_HAMMING,
) -> bool:
    if left.size != right.size:
        return False
    mad = mean_absolute_difference(mad_32(left), mad_32(right))
    if mad > mad_threshold:
        return False
    return hamming(dhash64(left), dhash64(right)) <= hamming_threshold


def change_regions(
    base: Image.Image,
    current: Image.Image,
    *,
    long_side: int = CHANGE_LONG_SIDE,
    block: int = CHANGE_BLOCK,
    diff_threshold: float = DEFAULT_BLOCK_DIFF,
    padding_px: int = CHANGE_PADDING_PX,
    max_regions: int = MAX_CHANGE_REGIONS,
) -> list[list[float]]:
    """Return normalized bounding boxes of blocks that changed noticeably."""
    if base.size != current.size:
        current = current.resize(base.size, Image.Resampling.BILINEAR)
    width, height = base.size
    scale = min(1.0, long_side / max(width, height))
    if scale < 1.0:
        size = (max(1, int(width * scale)), max(1, int(height * scale)))
        base_small = base.convert("L").resize(size, Image.Resampling.BILINEAR)
        current_small = current.convert("L").resize(size, Image.Resampling.BILINEAR)
    else:
        base_small = base.convert("L")
        current_small = current.convert("L")
    small_w, small_h = base_small.size
    diff = ImageChops.difference(base_small, current_small)
    blocks_x = max(1, (small_w + block - 1) // block)
    blocks_y = max(1, (small_h + block - 1) // block)
    means = diff.resize((blocks_x, blocks_y), Image.Resampling.BOX).tobytes()
    threshold = int(diff_threshold * 255)
    active = {
        (index % blocks_x, index // blocks_x)
        for index, value in enumerate(means)
        if value > threshold
    }
    if not active:
        return []
    regions: list[list[float]] = []
    remaining = set(active)
    while remaining and len(regions) < max_regions:
        start = next(iter(remaining))
        queue = deque([start])
        remaining.discard(start)
        min_x = max_x = start[0]
        min_y = max_y = start[1]
        while queue:
            x, y = queue.popleft()
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    candidate = (x + dx, y + dy)
                    if candidate in remaining:
                        remaining.discard(candidate)
                        queue.append(candidate)
        pad_x = padding_px / small_w
        pad_y = padding_px / small_h
        regions.append(
            [
                max(0.0, min_x * block / small_w - pad_x),
                max(0.0, min_y * block / small_h - pad_y),
                min(1.0, (max_x + 1) * block / small_w + pad_x),
                min(1.0, (max_y + 1) * block / small_h + pad_y),
            ]
        )
    regions.sort(key=lambda box: (box[2] - box[0]) * (box[3] - box[1]), reverse=True)
    return regions
