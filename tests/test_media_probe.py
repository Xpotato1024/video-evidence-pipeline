from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_evidence_pipeline.domain.media import (
    MediaStreamType,
    NormalizationConfig,
    SubtitleKind,
)
from video_evidence_pipeline.errors import VepError
from video_evidence_pipeline.media.probe import parse_probe_payload, probe_media
from video_evidence_pipeline.media.runner import ToolRunResult


def payload(*, format_name: str = "mov,mp4", duration: str = "12.345") -> bytes:
    return json.dumps(
        {
            "format": {"format_name": format_name, "duration": duration, "start_time": "0"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 640,
                    "height": 360,
                    "time_base": "1/1000",
                    "avg_frame_rate": "30/1",
                    "duration": duration,
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                    "time_base": "1/48000",
                    "duration": duration,
                },
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "subrip",
                    "time_base": "1/1000",
                    "tags": {"language": "ja"},
                    "disposition": {"default": 1, "forced": 0},
                },
            ],
        }
    ).encode()


def test_probe_models_streams_and_integer_milliseconds() -> None:
    probe = parse_probe_payload(payload(), NormalizationConfig())
    assert probe.duration_ms == 12_345
    assert [stream.stream_type for stream in probe.streams] == [
        MediaStreamType.VIDEO,
        MediaStreamType.AUDIO,
        MediaStreamType.SUBTITLE,
    ]
    assert probe.streams[2].subtitle_kind is SubtitleKind.TEXT
    assert probe.streams[2].is_default is True


@pytest.mark.parametrize("format_name", ["hls", "dash", "concat", "image2"])
def test_probe_rejects_playlist_and_external_reference_formats(format_name: str) -> None:
    with pytest.raises(VepError) as captured:
        parse_probe_payload(payload(format_name=format_name), NormalizationConfig())
    assert captured.value.code == "MEDIA_EXTERNAL_REFERENCE_UNSUPPORTED"


def test_probe_rejects_malformed_json_without_retaining_payload() -> None:
    with pytest.raises(VepError) as captured:
        parse_probe_payload(b'{"secret":"synthetic-value",', NormalizationConfig())
    assert captured.value.code == "FFPROBE_OUTPUT_INVALID"
    assert "synthetic-value" not in repr(captured.value)


@pytest.mark.parametrize("malformed_duration", [{"secret": "synthetic-value"}, ["-1"], "-1"])
def test_probe_maps_untrusted_duration_type_or_range_to_safe_error(
    malformed_duration: object,
) -> None:
    decoded = json.loads(payload())
    decoded["streams"][0]["duration"] = malformed_duration
    with pytest.raises(VepError) as captured:
        parse_probe_payload(json.dumps(decoded).encode(), NormalizationConfig())
    assert captured.value.code == "FFPROBE_STREAM_INVALID"
    assert "synthetic-value" not in repr(captured.value)


@pytest.mark.parametrize("malformed_language", [{"secret": "synthetic-value"}, ["ja"]])
def test_probe_maps_untrusted_language_type_to_safe_error(malformed_language: object) -> None:
    decoded = json.loads(payload())
    decoded["streams"][2]["tags"]["language"] = malformed_language
    with pytest.raises(VepError) as captured:
        parse_probe_payload(json.dumps(decoded).encode(), NormalizationConfig())
    assert captured.value.code == "FFPROBE_STREAM_INVALID"
    assert "synthetic-value" not in repr(captured.value)


@pytest.mark.parametrize("field", ["tags", "disposition"])
def test_probe_rejects_falsy_non_mapping_stream_metadata(field: str) -> None:
    decoded = json.loads(payload())
    decoded["streams"][2][field] = []
    with pytest.raises(VepError) as captured:
        parse_probe_payload(json.dumps(decoded).encode(), NormalizationConfig())
    assert captured.value.code == "FFPROBE_STREAM_INVALID"


def test_probe_enforces_duration_dimensions_and_stream_bounds() -> None:
    with pytest.raises(VepError) as captured:
        parse_probe_payload(payload(duration="999"), NormalizationConfig(maximum_duration_ms=1000))
    assert captured.value.code == "MEDIA_DURATION_EXCEEDED"

    decoded = json.loads(payload())
    decoded["streams"][0]["width"] = 9000
    with pytest.raises(VepError) as captured:
        parse_probe_payload(json.dumps(decoded).encode(), NormalizationConfig())
    assert captured.value.code == "MEDIA_DIMENSIONS_EXCEEDED"

    with pytest.raises(VepError) as captured:
        parse_probe_payload(payload(), NormalizationConfig(maximum_streams=2))
    assert captured.value.code == "MEDIA_STREAM_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    ("field", "value"),
    [("sample_rate", "192001"), ("channels", 9)],
)
def test_probe_enforces_source_audio_rate_and_channel_bounds(field: str, value: object) -> None:
    decoded = json.loads(payload())
    decoded["streams"][1][field] = value

    with pytest.raises(VepError) as captured:
        parse_probe_payload(json.dumps(decoded).encode(), NormalizationConfig())

    assert captured.value.code == "MEDIA_AUDIO_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "frame_rate",
    ["0/0", "-1/1", "1/0", "1/1001", "1001/1", "2147483648/1"],
)
def test_probe_rejects_invalid_or_unbounded_frame_rate(frame_rate: str) -> None:
    decoded = json.loads(payload())
    decoded["streams"][0]["avg_frame_rate"] = frame_rate
    with pytest.raises(VepError) as captured:
        parse_probe_payload(json.dumps(decoded).encode(), NormalizationConfig())
    assert captured.value.code == "FFPROBE_STREAM_INVALID"


@pytest.mark.parametrize("stream_index", [0, 1, 2])
@pytest.mark.parametrize("time_base", ["1/1000000001", "2/1", "2147483647/1"])
def test_probe_rejects_time_base_outside_safe_range(stream_index: int, time_base: str) -> None:
    decoded = json.loads(payload())
    decoded["streams"][stream_index]["time_base"] = time_base
    with pytest.raises(VepError) as captured:
        parse_probe_payload(json.dumps(decoded).encode(), NormalizationConfig())
    assert captured.value.code == "FFPROBE_STREAM_INVALID"


@pytest.mark.parametrize("duration", ["1e999999999", "9" * 129])
def test_probe_maps_huge_decimal_timestamp_to_safe_error(duration: str) -> None:
    with pytest.raises(VepError) as captured:
        parse_probe_payload(payload(duration=duration), NormalizationConfig())
    assert captured.value.code == "MEDIA_DURATION_INVALID"


@pytest.mark.parametrize("start_time", ["999999", "-999999", "1e999999999"])
def test_probe_rejects_implausible_start_time(start_time: str) -> None:
    decoded = json.loads(payload())
    decoded["streams"][0]["start_time"] = start_time
    with pytest.raises(VepError) as captured:
        parse_probe_payload(json.dumps(decoded).encode(), NormalizationConfig())
    assert captured.value.code == "FFPROBE_STREAM_INVALID"


def test_probe_rejects_implausible_container_start_time() -> None:
    decoded = json.loads(payload())
    decoded["format"]["start_time"] = "999999"

    with pytest.raises(VepError) as captured:
        parse_probe_payload(json.dumps(decoded).encode(), NormalizationConfig())

    assert captured.value.code == "FFPROBE_STREAM_INVALID"


def test_probe_argv_allows_only_file_protocol(tmp_path: Path) -> None:
    observed: list[str] = []

    class StubRunner:
        def run_checked(self, tool: str, arguments: list[str], **_: object) -> ToolRunResult:
            assert tool == "ffprobe"
            observed.extend(arguments)
            return ToolRunResult(
                argv=("ffprobe", *arguments),
                returncode=0,
                stdout=payload(),
                stderr=b"",
                stdout_truncated=False,
                stderr_truncated=False,
                elapsed_seconds=0,
            )

    source = tmp_path / "synthetic.mp4"
    source.write_bytes(b"synthetic")
    probe_media(  # type: ignore[arg-type]
        StubRunner(),
        source,
        NormalizationConfig(
            maximum_streams=7,
            maximum_decoded_video_pixel_slots_per_frame=12_345,
            maximum_decoded_audio_sample_slots_per_frame=6_789,
        ),
    )
    whitelist = observed.index("-protocol_whitelist")
    assert observed[whitelist + 1] == "file"
    format_whitelist = observed.index("-format_whitelist")
    assert "hls" not in observed[format_whitelist + 1]
    assert "concat" not in observed[format_whitelist + 1]
    max_streams = observed.index("-max_streams")
    assert observed[max_streams + 1] == "7"
    assert max_streams < observed.index(str(source.absolute()))
    max_pixels = observed.index("-max_pixels")
    assert observed[max_pixels + 1] == "12345"
    max_samples = observed.index("-max_samples")
    assert observed[max_samples + 1] == "6789"
    assert max_pixels < observed.index(str(source.absolute()))
    assert max_samples < observed.index(str(source.absolute()))
    assert "-nostdin" not in observed
