"""Pure-Python PNG decoding and perceptual hashes for frame deduplication."""

from __future__ import annotations

import math
import stat
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path

from video_evidence_pipeline.errors import integrity_error

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def _png_scanlines(compressed: bytearray, row_size: int, height: int) -> Iterator[bytearray]:
    """Inflate one bounded row at a time, checking the complete zlib stream."""
    decompressor = zlib.decompressobj()
    offset = 0
    pending: bytes | memoryview = b""

    def take(limit: int) -> bytes:
        nonlocal offset, pending
        if not pending and offset < len(compressed):
            pending = memoryview(compressed)[offset : offset + 64 * 1024]
            offset += len(pending)
        if not pending:
            raise integrity_error("FRAME_IMAGE_INVALID", "PNG decoded size is invalid")
        output = decompressor.decompress(pending, limit)
        pending = decompressor.unconsumed_tail
        return output

    try:
        for _ in range(height):
            scanline = bytearray()
            while len(scanline) < row_size:
                if decompressor.eof:
                    raise integrity_error("FRAME_IMAGE_INVALID", "PNG decoded size is invalid")
                scanline.extend(take(row_size - len(scanline)))
            yield scanline
        # Consume the trailer with a one-byte overflow guard. No extra pixel,
        # concatenated stream or trailing compressed data may be accepted.
        while not decompressor.eof:
            if take(1):
                raise integrity_error("FRAME_IMAGE_INVALID", "PNG decoded size is invalid")
        if pending or decompressor.unused_data or offset != len(compressed):
            raise integrity_error("FRAME_IMAGE_INVALID", "PNG decoded size is invalid")
    except zlib.error:
        raise integrity_error("FRAME_IMAGE_INVALID", "PNG payload is malformed") from None


def _decode_png_grayscale(
    payload: bytes, *, maximum_pixels: int, sample_size: tuple[int, int] | None = None
) -> tuple[int, int, bytes]:
    if not payload.startswith(_PNG_SIGNATURE):
        raise integrity_error("FRAME_IMAGE_INVALID", "extracted frame is not a PNG image")
    offset = len(_PNG_SIGNATURE)
    width = height = bit_depth = color_type = interlace = None
    compressed = bytearray()
    saw_end = False
    while offset + 12 <= len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if length > len(payload) or chunk_end > len(payload):
            raise integrity_error("FRAME_IMAGE_INVALID", "PNG chunk is truncated")
        data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length : chunk_end])[0]
        if zlib.crc32(chunk_type + data) & 0xFFFFFFFF != expected_crc:
            raise integrity_error("FRAME_IMAGE_INVALID", "PNG chunk checksum is invalid")
        if chunk_type == b"IHDR":
            if len(data) != 13 or width is not None:
                raise integrity_error("FRAME_IMAGE_INVALID", "PNG header is invalid")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if (
                width <= 0
                or height <= 0
                or width * height > maximum_pixels
                or bit_depth != 8
                or color_type not in {0, 2, 4, 6}
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                raise integrity_error("FRAME_IMAGE_INVALID", "PNG format is unsupported")
        elif chunk_type == b"IDAT":
            compressed.extend(data)
        elif chunk_type == b"IEND":
            saw_end = True
            break
        offset = chunk_end
    if width is None or height is None or not compressed or not saw_end:
        raise integrity_error("FRAME_IMAGE_INVALID", "PNG structure is incomplete")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    row_bytes = width * channels
    output_width, output_height = sample_size or (width, height)
    # Reconstruct every row (including unsampled rows used by PNG filters), but
    # convert only the exact nearest-neighbour pixels consumed by the pHash.
    columns = [min(width - 1, column * width // output_width) for column in range(output_width)]
    rows: dict[int, list[int]] = {}
    for row in range(output_height):
        rows.setdefault(min(height - 1, row * height // output_height), []).append(row)
    grayscale = bytearray(output_width * output_height)
    previous = bytearray(row_bytes)
    for _row, scanline in enumerate(_png_scanlines(compressed, row_bytes + 1, height)):
        filter_type = scanline[0]
        current = scanline[1:]
        if filter_type > 4:
            raise integrity_error("FRAME_IMAGE_INVALID", "PNG row filter is invalid")
        # Filter 0 already contains reconstructed bytes; avoid a Python loop.
        for index in range(row_bytes) if filter_type else ():
            left = current[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                current[index] = (current[index] + left) & 0xFF
            elif filter_type == 2:
                current[index] = (current[index] + above) & 0xFF
            elif filter_type == 3:
                current[index] = (current[index] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                current[index] = (current[index] + _paeth(left, above, upper_left)) & 0xFF
        sample = bytearray()
        for column in columns if _row in rows else ():
            pixel = column * channels
            if color_type in {0, 4}:
                luminance = current[pixel]
            else:
                red, green, blue = current[pixel : pixel + 3]
                luminance = (299 * red + 587 * green + 114 * blue + 500) // 1000
            sample.append(luminance)
        for output_row in rows.get(_row, ()):
            output_offset = output_row * output_width
            grayscale[output_offset : output_offset + output_width] = sample
        previous = current
    return output_width, output_height, bytes(grayscale)


def _resize_nearest(
    pixels: bytes,
    width: int,
    height: int,
    *,
    target_width: int,
    target_height: int,
) -> list[int]:
    return [
        pixels[
            min(height - 1, row * height // target_height) * width
            + min(width - 1, column * width // target_width)
        ]
        for row in range(target_height)
        for column in range(target_width)
    ]


def perceptual_hash_png(path: Path, *, maximum_bytes: int, maximum_pixels: int) -> str:
    try:
        metadata = path.stat(follow_symlinks=False)
        size = metadata.st_size
        if (
            size <= 0
            or size > maximum_bytes
            or path.is_symlink()
            or getattr(path, "is_junction", lambda: False)()
            or not stat.S_ISREG(metadata.st_mode)
        ):
            raise integrity_error("FRAME_IMAGE_INVALID", "extracted frame size is invalid")
        payload = path.read_bytes()
    except OSError:
        raise integrity_error("FRAME_IMAGE_INVALID", "extracted frame could not be read") from None
    if len(payload) != size:
        raise integrity_error("FRAME_IMAGE_INVALID", "extracted frame changed while being read")
    width, height, grayscale = _decode_png_grayscale(
        payload, maximum_pixels=maximum_pixels, sample_size=(32, 32)
    )
    values = _resize_nearest(
        grayscale,
        width,
        height,
        target_width=32,
        target_height=32,
    )
    coefficients: list[float] = []
    cosine = [
        [math.cos(math.pi * (2 * position + 1) * frequency / 64) for position in range(32)]
        for frequency in range(8)
    ]
    for vertical_frequency in range(8):
        for horizontal_frequency in range(8):
            coefficient = 0.0
            for row in range(32):
                row_factor = cosine[vertical_frequency][row]
                base = row * 32
                coefficient += row_factor * sum(
                    values[base + column] * cosine[horizontal_frequency][column]
                    for column in range(32)
                )
            coefficients.append(coefficient)
    median_values = sorted(coefficients[1:])
    median = median_values[len(median_values) // 2]
    bits = 0
    for coefficient in coefficients:
        bits = (bits << 1) | int(coefficient >= median)
    return f"{bits:016x}"


def perceptual_hash_distance(left: str, right: str) -> int:
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        raise ValueError("perceptual hashes must be hexadecimal") from None
