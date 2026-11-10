import asyncio
import json
import struct
import zlib

import pytest

from botex import vision


def _png_chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _grayscale_png(width, height, row, *, compression=6):
    """Build a real PNG without requiring Pillow or allocating the full image."""
    compressor = zlib.compressobj(compression)
    compressed = []
    for _ in range(height):
        compressed.append(compressor.compress(b"\x00" + row))
    compressed.append(compressor.flush())
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", b"".join(compressed))
        + _png_chunk(b"IEND", b"")
    )


@pytest.fixture
def image_pair():
    ascending = bytes(range(0, 225, 28))
    return (
        _grayscale_png(9, 8, ascending, compression=0),
        _grayscale_png(9, 8, ascending[::-1], compression=0),
    )


def test_dhash_and_hamming_distance(image_pair):
    original, modified = image_pair
    original_hash = vision.compute_dhash(original)
    assert vision.hamming_distance(original_hash, vision.compute_dhash(original)) == 0
    assert vision.hamming_distance(original_hash, vision.compute_dhash(modified)) > 0
    assert vision.hamming_distance(0, 0b1011) == 3


@pytest.mark.parametrize("validator", [vision.validate_image_safety, vision.compute_dhash])
def test_empty_image_is_rejected(validator):
    with pytest.raises(ValueError, match="Decompression Bomb guard"):
        validator(b"")


@pytest.mark.parametrize("validator", [vision.validate_image_safety, vision.compute_dhash])
def test_oversized_buffer_is_rejected(validator):
    with pytest.raises(ValueError, match="Decompression Bomb guard"):
        validator(b"x" * (vision.MAX_IMAGE_BYTES + 1))


@pytest.mark.parametrize("validator", [vision.validate_image_safety, vision.compute_dhash])
def test_png_exceeding_pixel_limit_is_rejected(validator):
    height = 3163
    width = vision.MAX_IMAGE_PIXELS // height + 1
    image = _grayscale_png(width, height, b"\x00" * width)
    assert len(image) <= vision.MAX_IMAGE_BYTES
    with pytest.raises(ValueError, match="Decompression Bomb guard"):
        validator(image)


@pytest.mark.parametrize("distance", [0, 1, 4])
def test_close_reference_skips_llm(image_pair, distance):
    image = image_pair[0]
    image_hash = vision.compute_dhash(image)
    reference = image_hash ^ ((1 << distance) - 1)
    calls = []

    def llm_caller(*args):
        calls.append(args)
        raise AssertionError("LLM must not be called for a close reference")

    result = asyncio.run(
        vision.triage_visual_defect(image, reference_hash=reference, llm_caller=llm_caller)
    )

    assert result["ok"] is True
    assert result["status"] == "NO_VISUAL_DIFF"
    assert result["skipped_llm"] is True
    assert result["dhash"] == image_hash
    assert result["hamming_distance"] == distance
    assert result["provider"] is None
    assert result["model"] is None
    assert calls == []
    assert json.loads(result["observation_json"])["hamming_distance"] == distance


@pytest.mark.parametrize("failures", [1, 2])
@pytest.mark.parametrize("async_caller", [False, True])
def test_fallback_chain_masks_model_response(image_pair, failures, async_caller):
    image = image_pair[0]
    hint = "Inspect the layout"
    secret = "sk-or-v1-exampleSecret123456789"
    response = f"Misaligned button; exposed key: {secret}"
    chain = [
        ("google", "gemini-2.5-flash"),
        ("nvidia", "meta/llama-3.2-11b-vision-instruct"),
        ("openrouter-free", "meta-llama/llama-3.2-11b-vision-instruct:free"),
    ]
    calls = []

    def call(provider, model, image_bytes, prompt_hint):
        calls.append((provider, model, image_bytes, prompt_hint))
        if len(calls) <= failures:
            raise RuntimeError("provider unavailable")
        return {"finding": response}

    async def async_call(provider, model, image_bytes, prompt_hint):
        return call(provider, model, image_bytes, prompt_hint)

    result = asyncio.run(
        vision.triage_visual_defect(
            image, prompt_hint=hint, llm_caller=async_call if async_caller else call
        )
    )

    assert calls == [(provider, model, image, hint) for provider, model in chain[: failures + 1]]
    assert result["ok"] is True
    assert result["status"] == "DEFECT_DETECTED"
    assert result["skipped_llm"] is False
    assert (result["provider"], result["model"]) == chain[failures]
    assert result["finding"] == "Misaligned button; exposed key: [REDACTED]"
    assert secret not in result["observation_json"]
    assert json.loads(result["observation_json"])["finding"] == result["finding"]
