"""Shared fixed local-input arguments for every FFmpeg-family invocation."""

from __future__ import annotations

from pathlib import Path

SAFE_DEMUXER_WHITELIST = "aac,asf,avi,flac,flv,matroska,mov,mp3,mpeg,mpegts,ogg,wav"


def local_demuxer_options() -> list[str]:
    return [
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        SAFE_DEMUXER_WHITELIST,
    ]


def ffprobe_local_input(
    path: Path,
    maximum_streams: int,
    maximum_pixels: int,
    maximum_samples: int,
) -> list[str]:
    return [
        *local_demuxer_options(),
        "-max_streams",
        str(maximum_streams),
        "-max_pixels",
        str(maximum_pixels),
        "-max_samples",
        str(maximum_samples),
        str(path.absolute()),
    ]


def ffmpeg_local_input(
    path: Path,
    maximum_streams: int,
    maximum_pixel_slots: int,
    maximum_sample_slots: int,
) -> list[str]:
    return [
        *local_demuxer_options(),
        "-max_streams",
        str(maximum_streams),
        "-max_pixels",
        str(maximum_pixel_slots),
        "-max_samples",
        str(maximum_sample_slots),
        "-i",
        str(path.absolute()),
    ]
