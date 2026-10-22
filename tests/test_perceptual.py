from __future__ import annotations

import struct
import zlib

import pytest

from scripts.benchmark_perceptual import synthetic_png
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.media.perceptual import (
    _decode_png_grayscale,
    _png_scanlines,
    _resize_nearest,
    perceptual_hash_png,
)


def _chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _header(payload, **changes):
    names = ("width", "height", "depth", "color", "compression", "filtering", "interlace")
    values = dict(zip(names, struct.unpack(">IIBBBBB", payload[16:29]), strict=True))
    values.update(changes)
    return (
        payload[:8]
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", *(values[name] for name in names)))
        + payload[33:]
    )


def _with_idat(payload, compressed):
    length = struct.unpack(">I", payload[33:37])[0]
    return payload[:33] + _chunk(b"IDAT", compressed) + payload[45 + length :]


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


@pytest.mark.parametrize("color", [0, 2, 4, 6])
def test_mixed_row_filters_and_split_idat_preserve_pixels(color):
    width, height = 37, 41
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    payload = synthetic_png(width, height, color_type=color)
    length = struct.unpack(">I", payload[33:37])[0]
    raw = zlib.decompress(payload[41 : 41 + length])
    stride = width * channels + 1
    previous = bytes(stride - 1)
    filtered = bytearray()
    for row in range(height):
        current = raw[row * stride + 1 : (row + 1) * stride]
        filtering = row % 5
        filtered.append(filtering)
        for index, value in enumerate(current):
            left = current[index - channels] if index >= channels else 0
            above = previous[index]
            corner = previous[index - channels] if index >= channels else 0
            estimate = left + above - corner
            paeth = min(
                enumerate((left, above, corner)),
                key=lambda item: (abs(estimate - item[1]), item[0]),
            )[1]
            filtered.append((value - (0, left, above, (left + above) // 2, paeth)[filtering]) & 255)
        previous = current
    compressed = zlib.compress(filtered)
    split_chunks = b"".join(
        _chunk(b"IDAT", compressed[index : index + 13]) for index in range(0, len(compressed), 13)
    )
    mixed = payload[:33] + split_chunks + _chunk(b"IEND", b"")
    assert _decode_png_grayscale(
        mixed, maximum_pixels=width * height, sample_size=(32, 32)
    ) == _decode_png_grayscale(payload, maximum_pixels=width * height, sample_size=(32, 32))


@pytest.mark.parametrize(
    "changes",
    [
        {"color": 3},
        {"color": 1},
        {"color": 5},
        {"depth": 1},
        {"depth": 2},
        {"depth": 4},
        {"depth": 16},
        {"interlace": 1},
        {"compression": 1},
        {"filtering": 1},
        {"width": 0},
        {"height": 0},
        {"width": 0xFFFFFFFF, "height": 0xFFFFFFFF},
    ],
)
def test_existing_unsupported_png_headers_remain_rejected(changes, monkeypatch):
    from video_evidence_pipeline.media import perceptual

    def forbidden(*args):
        raise AssertionError("unsupported header must be rejected before inflation")

    monkeypatch.setattr(perceptual, "_png_scanlines", forbidden)
    with pytest.raises(VepError) as caught:
        _decode_png_grayscale(
            _header(synthetic_png(9, 7), **changes), maximum_pixels=63, sample_size=(32, 32)
        )
    assert caught.value.code == "FRAME_IMAGE_INVALID"


@pytest.mark.parametrize("color", [4, 6])
def test_alpha_is_ignored_exactly_as_before(color, tmp_path):
    channels = {4: 2, 6: 4}[color]
    payload = synthetic_png(37, 41, color_type=color)
    length = struct.unpack(">I", payload[33:37])[0]
    raw = bytearray(zlib.decompress(payload[41 : 41 + length]))
    stride = 37 * channels + 1
    for row in range(41):
        for alpha in range(row * stride + channels, (row + 1) * stride, channels):
            raw[alpha] = 255 - raw[alpha]
    alternate = _with_idat(payload, zlib.compress(raw))
    path = tmp_path / "alpha.png"
    path.write_bytes(payload)
    expected = perceptual_hash_png(path, maximum_bytes=len(payload), maximum_pixels=37 * 41)
    path.write_bytes(alternate)
    assert (
        perceptual_hash_png(path, maximum_bytes=len(alternate), maximum_pixels=37 * 41) == expected
    )


def test_pixel_and_file_byte_limits_are_inclusive(tmp_path):
    payload = synthetic_png(1, 1)
    path = tmp_path / "limits.png"
    path.write_bytes(payload)
    assert len(perceptual_hash_png(path, maximum_bytes=len(payload), maximum_pixels=1)) == 16
    for byte_limit, pixel_limit in ((len(payload) - 1, 1), (len(payload), 0)):
        with pytest.raises(VepError) as caught:
            perceptual_hash_png(path, maximum_bytes=byte_limit, maximum_pixels=pixel_limit)
        assert caught.value.code == "FRAME_IMAGE_INVALID"


@pytest.mark.parametrize(
    "fault",
    [
        "signature",
        "crc",
        "truncated",
        "no-end",
        "duplicate-header",
        "too-short",
        "too-long",
        "bad-zlib",
    ],
)
def test_corrupt_pngs_fail_closed(fault):
    payload = synthetic_png(37, 41)
    if fault == "signature":
        payload = b"not PNG!" + payload[8:]
    elif fault == "crc":
        payload = payload[:29] + b"\0\0\0\0" + payload[33:]
    elif fault == "truncated":
        payload = payload[:-15]
    elif fault == "no-end":
        payload = payload[:-12]
    elif fault == "duplicate-header":
        payload = payload[:33] + payload[8:]
    else:
        raw = b"\0" * ((37 * 3 + 1) * 41 + (-1 if fault == "too-short" else 1))
        payload = _with_idat(payload, b"invalid" if fault == "bad-zlib" else zlib.compress(raw))
    with pytest.raises(VepError) as caught:
        _decode_png_grayscale(payload, maximum_pixels=37 * 41, sample_size=(32, 32))
    assert caught.value.code == "FRAME_IMAGE_INVALID"
