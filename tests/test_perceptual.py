from __future__ import annotations

import zlib

import pytest

from scripts.benchmark_perceptual import synthetic_png
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.media.perceptual import (
    _decode_png_grayscale,
    _png_scanlines,
    _resize_nearest,
)


@pytest.mark.parametrize("color", [0, 2, 4, 6])
@pytest.mark.parametrize("filtering", range(5))
@pytest.mark.parametrize("size", [(17, 11), (32, 32), (71, 43)])
def test_sampled_decode_is_exact_nearest_neighbor(color, filtering, size):
    width, height = size
    payload = synthetic_png(width, height, color_type=color, filter_type=filtering)
    _, _, full = _decode_png_grayscale(payload, maximum_pixels=width * height)
    # Reconstruct expected luminance independently of the PNG decoder.
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    expected = bytearray()
    for row in range(height):
        pixels = [
            (column * 17 + row * 31 + (column // channels) ** 2) % 256
            for column in range(width * channels)
        ]
        for column in range(width):
            index = column * channels
            expected.append(
                pixels[index]
                if color in {0, 4}
                else (299 * pixels[index] + 587 * pixels[index + 1] + 114 * pixels[index + 2] + 500)
                // 1000
            )
    assert full == expected
    out_width, out_height, sampled = _decode_png_grayscale(
        payload, maximum_pixels=width * height, sample_size=(32, 32)
    )
    assert (out_width, out_height) == (32, 32)
    assert list(sampled) == _resize_nearest(
        bytes(expected), width, height, target_width=32, target_height=32
    )


def test_sampled_decode_still_checks_unsampled_rows_and_crc():
    payload = synthetic_png(71, 43, filter_type=4)
    with pytest.raises(VepError):
        _decode_png_grayscale(
            payload[:-15] + b"corrupt bytes!!", maximum_pixels=71 * 43, sample_size=(32, 32)
        )
    # CRC-valid invalid filter on a row not selected by nearest-neighbour.
    import struct

    length = struct.unpack(">I", payload[33:37])[0]
    raw = bytearray(zlib.decompress(payload[41 : 41 + length]))
    raw[3 * (71 * 3 + 1)] = 5
    compressed = zlib.compress(raw)
    altered = (
        payload[:33]
        + struct.pack(">I", len(compressed))
        + b"IDAT"
        + compressed
        + struct.pack(">I", zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF)
        + payload[45 + length :]
    )
    with pytest.raises(VepError) as caught:
        _decode_png_grayscale(altered, maximum_pixels=71 * 43, sample_size=(32, 32))
    assert caught.value.code == "FRAME_IMAGE_INVALID"


@pytest.mark.parametrize(
    "compressed",
    [
        zlib.compress(b"abcdef")[:-1],
        zlib.compress(b"abcde"),
        zlib.compress(b"abcdefg"),
        zlib.compress(b"abcdef") + b"trailing",
        zlib.compress(b"abcdef") + zlib.compress(b""),
        b"malformed",
    ],
)
def test_streaming_scanlines_reject_incomplete_excess_and_trailing_streams(compressed):
    with pytest.raises(VepError) as caught:
        list(_png_scanlines(bytearray(compressed), 3, 2))
    assert caught.value.code == "FRAME_IMAGE_INVALID"


def test_streaming_scanlines_cross_input_chunk_boundary():
    import random

    payload = random.Random(42).randbytes(210_000)
    assert b"".join(_png_scanlines(bytearray(zlib.compress(payload)), 700, 300)) == payload
