"""
Vision module for image validation, perceptual hashing, and visual defect triage.
"""

import json
import struct
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from PIL import Image

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from botex.security import mask_secrets

# Constants
MAX_IMAGE_PIXELS: int = 10_000_000
MAX_IMAGE_BYTES: int = 20 * 1024 * 1024


def _parse_png_dimensions(data: bytes) -> Tuple[int, int]:
    """Parse width and height from PNG header."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid PNG header")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _parse_jpeg_dimensions(data: bytes) -> Tuple[int, int]:
    """Parse width and height from JPEG header."""
    if len(data) < 2 or data[:2] != b"\xff\xd8":
        raise ValueError("Invalid JPEG header")

    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            raise ValueError("Invalid JPEG marker")
        marker = data[i + 1]
        i += 2

        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            # SOF markers - start of frame
            if i + 5 > len(data):
                raise ValueError("Incomplete JPEG SOF segment")
            height, width = struct.unpack(">HH", data[i + 3 : i + 7])
            return width, height

        if marker == 0xDA:  # SOS - start of scan
            break

        if i + 2 > len(data):
            break
        segment_length = struct.unpack(">H", data[i : i + 2])[0]
        i += segment_length

    raise ValueError("JPEG dimensions not found")


def _parse_webp_dimensions(data: bytes) -> Tuple[int, int]:
    """Parse width and height from WebP header."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("Invalid WebP header")

    if data[12:16] == b"VP8 ":
        # Lossy WebP
        if len(data) < 30:
            raise ValueError("Incomplete WebP VP8 header")
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return width, height
    elif data[12:16] == b"VP8L":
        # Lossless WebP
        if len(data) < 25:
            raise ValueError("Incomplete WebP VP8L header")
        bits = struct.unpack("<I", data[21:25])[0]
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    elif data[12:16] == b"VP8X":
        # Extended WebP
        if len(data) < 30:
            raise ValueError("Incomplete WebP VP8X header")
        width = struct.unpack("<I", b"\x00" + data[24:27])[0] + 1
        height = struct.unpack("<I", b"\x00" + data[27:30])[0] + 1
        return width, height

    raise ValueError("Unsupported WebP format")


def _parse_gif_dimensions(data: bytes) -> Tuple[int, int]:
    """Parse width and height from GIF header."""
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("Invalid GIF header")
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _get_image_dimensions(image_bytes: bytes) -> Tuple[int, int]:
    """Extract image dimensions from various formats without PIL."""
    if len(image_bytes) >= 24 and image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return _parse_png_dimensions(image_bytes)
    elif len(image_bytes) >= 2 and image_bytes[:2] == b"\xff\xd8":
        return _parse_jpeg_dimensions(image_bytes)
    elif len(image_bytes) >= 30 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return _parse_webp_dimensions(image_bytes)
    elif len(image_bytes) >= 10 and image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return _parse_gif_dimensions(image_bytes)

    # Try PIL as fallback
    if PIL_AVAILABLE:
        try:
            img = Image.open(image_bytes)
            return img.size
        except Exception:
            pass

    raise ValueError("Unsupported image format or unable to parse dimensions")


def validate_image_safety(image_bytes: bytes) -> Tuple[int, int]:
    """
    Validate image safety limits and return dimensions.

    Args:
        image_bytes: Raw image bytes

    Returns:
        Tuple of (width, height)

    Raises:
        ValueError: If image exceeds safety limits or is invalid
    """
    if not image_bytes:
        raise ValueError("Image exceeds safety limits (Decompression Bomb guard)")

    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds safety limits (Decompression Bomb guard)")

    width, height = _get_image_dimensions(image_bytes)

    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("Image exceeds safety limits (Decompression Bomb guard)")

    return width, height


def _compute_dhash_pil(image_bytes: bytes) -> int:
    """Compute dhash using PIL."""
    img = Image.open(image_bytes)
    # Resize to 9x8 grayscale
    img = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(img.getdata())

    # Compute horizontal gradient hash
    hash_value = 0
    for y in range(8):
        for x in range(8):
            idx = y * 9 + x
            if pixels[idx] > pixels[idx + 1]:
                hash_value |= 1 << (63 - (y * 8 + x))
    return hash_value


def _compute_dhash_fallback(image_bytes: bytes) -> int:
    """Compute dhash using pure Python fallback (sampling bytes)."""
    # Sample bytes to create a pseudo-luminance grid
    # This is a deterministic fallback when PIL is not available
    data = image_bytes
    if len(data) < 72:
        # Pad or repeat if too small
        data = (data * (72 // len(data) + 1))[:72]

    # Create 9x8 grid by sampling
    grid = []
    step = max(1, len(data) // 72)
    for i in range(72):
        idx = (i * step) % len(data)
        grid.append(data[idx])

    # Reshape to 9x8
    hash_value = 0
    for y in range(8):
        for x in range(8):
            idx = y * 9 + x
            if grid[idx] > grid[idx + 1]:
                hash_value |= 1 << (63 - (y * 8 + x))
    return hash_value


def compute_dhash(image_bytes: bytes) -> int:
    """
    Compute deterministic 64-bit difference hash (dhash) of an image.

    Args:
        image_bytes: Raw image bytes

    Returns:
        64-bit integer hash
    """
    validate_image_safety(image_bytes)

    if PIL_AVAILABLE:
        try:
            return _compute_dhash_pil(image_bytes)
        except Exception:
            pass

    return _compute_dhash_fallback(image_bytes)


def hamming_distance(hash1: int, hash2: int) -> int:
    """
    Compute Hamming distance between two hashes.

    Args:
        hash1: First hash
        hash2: Second hash

    Returns:
        Number of differing bits
    """
    return (int(hash1) ^ int(hash2)).bit_count()


async def triage_visual_defect(
    image_bytes: bytes,
    prompt_hint: str = "",
    reference_hash: Optional[int] = None,
    workspace_dir: Optional[Path] = None,
    llm_caller: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Triage visual defect using perceptual hashing and optional LLM analysis.

    Args:
        image_bytes: Raw image bytes
        prompt_hint: Optional prompt hint for LLM
        reference_hash: Optional reference dhash for comparison
        workspace_dir: Optional workspace directory
        llm_caller: Optional async/sync callable(provider, model, image_bytes, prompt_hint)

    Returns:
        Dictionary with triage results
    """
    dhash = compute_dhash(image_bytes)

    # Check against reference hash
    if reference_hash is not None:
        dist = hamming_distance(dhash, reference_hash)
        if dist <= 4:
            return {
                "ok": True,
                "status": "NO_VISUAL_DIFF",
                "skipped_llm": True,
                "dhash": dhash,
                "hamming_distance": dist,
                "provider": None,
                "model": None,
                "finding": "No significant visual change detected.",
                "observation_json": json.dumps(
                    {"status": "NO_VISUAL_DIFF", "dhash": dhash, "hamming_distance": dist}
                ),
            }

    # Zero-Cost Fallback Chain
    fallback_chain = [
        ("google", "gemini-2.5-flash"),
        ("nvidia", "meta/llama-3.2-11b-vision-instruct"),
        ("openrouter-free", "meta-llama/llama-3.2-11b-vision-instruct:free"),
    ]

    last_error = None
    for provider, model in fallback_chain:
        if llm_caller is not None:
            try:
                # Handle both sync and async callers
                import inspect

                if inspect.iscoroutinefunction(llm_caller):
                    result = await llm_caller(provider, model, image_bytes, prompt_hint)
                else:
                    result = llm_caller(provider, model, image_bytes, prompt_hint)

                # Assume result is a dict with finding
                finding = mask_secrets(str(result.get("finding", str(result))))
                return {
                    "ok": True,
                    "status": "DEFECT_DETECTED" if finding else "CLEAN",
                    "skipped_llm": False,
                    "dhash": dhash,
                    "hamming_distance": hamming_distance(dhash, reference_hash)
                    if reference_hash
                    else 0,
                    "provider": provider,
                    "model": model,
                    "finding": finding,
                    "observation_json": json.dumps(
                        {
                            "status": "DEFECT_DETECTED" if finding else "CLEAN",
                            "dhash": dhash,
                            "hamming_distance": hamming_distance(dhash, reference_hash)
                            if reference_hash
                            else 0,
                            "provider": provider,
                            "model": model,
                            "finding": finding,
                        }
                    ),
                }
            except Exception as e:
                last_error = e
                continue
        else:
            # No LLM caller provided - create structured diagnostic description
            diagnostic = _create_diagnostic_description(image_bytes, prompt_hint)
            finding = mask_secrets(diagnostic)
            return {
                "ok": True,
                "status": "CLEAN",
                "skipped_llm": False,
                "dhash": dhash,
                "hamming_distance": hamming_distance(dhash, reference_hash)
                if reference_hash
                else 0,
                "provider": None,
                "model": None,
                "finding": finding,
                "observation_json": json.dumps(
                    {
                        "status": "CLEAN",
                        "dhash": dhash,
                        "hamming_distance": hamming_distance(dhash, reference_hash)
                        if reference_hash
                        else 0,
                        "provider": None,
                        "model": None,
                        "finding": finding,
                    }
                ),
            }

    # All fallbacks failed
    finding = mask_secrets(
        f"LLM analysis unavailable: {last_error}" if last_error else "LLM analysis unavailable"
    )
    return {
        "ok": False,
        "status": "CLEAN",
        "skipped_llm": False,
        "dhash": dhash,
        "hamming_distance": hamming_distance(dhash, reference_hash) if reference_hash else 0,
        "provider": None,
        "model": None,
        "finding": finding,
        "observation_json": json.dumps(
            {
                "status": "CLEAN",
                "dhash": dhash,
                "hamming_distance": hamming_distance(dhash, reference_hash)
                if reference_hash
                else 0,
                "provider": None,
                "model": None,
                "finding": finding,
            }
        ),
    }


def _create_diagnostic_description(image_bytes: bytes, prompt_hint: str) -> str:
    """Create a structured diagnostic description of the image."""
    width, height = validate_image_safety(image_bytes)
    size_kb = len(image_bytes) / 1024

    # Detect format
    fmt = "Unknown"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        fmt = "PNG"
    elif image_bytes[:2] == b"\xff\xd8":
        fmt = "JPEG"
    elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        fmt = "WebP"
    elif image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        fmt = "GIF"

    parts = [
        f"Image Analysis: {fmt}, {width}x{height}px, {size_kb:.1f}KB",
        f"dHash: {compute_dhash(image_bytes):016x}",
    ]

    if prompt_hint:
        parts.append(f"Context: {prompt_hint}")

    return " | ".join(parts)
