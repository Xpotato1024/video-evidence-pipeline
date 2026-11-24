"""Read edits to the VEP portable MDX subset without executing MDX expressions."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from itertools import pairwise
from typing import Any

from video_evidence_pipeline.errors import integrity_error

_MARKER = re.compile(r"\{\/\* vep:(?:block ([A-Za-z0-9._-]+)|sources) \*\/\}")
_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
_MATH_FIELDS = ("symbolic", "substitution", "intermediate", "result")
_Edit = tuple[Any, str | int, str, str, Callable[[str], str], Callable[[str], str]]


def _escape(value: str) -> str:
    return re.sub("[&<>\"']", lambda match: _ESCAPES[match.group()], value)


def _markdown(value: str) -> str:
    value = re.sub(r"[\\`*_{}\[\]$]", lambda match: "\\" + match.group(), _escape(value))
    value = re.sub(r"(^|\n)(?=[#>+\-]|\d+[.)])", lambda match: match.group() + "\\", value)

    value = re.sub(
        r"(^|\n)(import|export)(?=[^A-Za-z0-9_]|$)",
        lambda match: match[1] + ("&#105;mport" if match[2] == "import" else "&#101;xport"),
        value,
    )
    return value.replace("\r\n", "\n")


def _decode_markdown(value: str) -> str:
    value = re.sub(r"(?m)^\\(?=\d+[.)])", "", value)
    return html.unescape(re.sub(r"\\([\\`*_{}\[\]$#>+\-])", r"\1", value))


def _identity(value: str) -> str:
    return value


def _error(message: str):
    return integrity_error("LECTURE_MDX_INVALID", message)


def _sections(value: str) -> tuple[str, list[tuple[str, str]], str]:
    value = value.replace("\r\n", "\n")
    markers = list(_MARKER.finditer(value))
    if not markers or markers[-1].group(1) is not None:
        raise _error("MDX lacks VEP portable content markers")
    section_pairs = []
    for start, end in pairwise(markers):
        if start.group(1) is None:
            raise _error("MDX source appendix must follow all blocks")
        section_pairs.append((start.group(1), value[start.end() : end.start()]))
    return value[: markers[0].start()], section_pairs, value[markers[-1].end() :]


def _replace(base: str, edited: str, fields: list[_Edit], changes: dict) -> None:
    cursor = 0
    separators = []
    for item, field, prefix, suffix, encode, _decode in fields:
        literal = encode(item[field])
        position = base.find(f"{prefix}{literal}{suffix}", cursor)
        if position < 0:
            raise _error("MDX base view does not match its structured content")
        start = position + len(prefix)
        separators.append(base[cursor:start])
        cursor = start + len(literal)
    separators.append(base[cursor:])
    if not edited.startswith(separators[0]):
        raise _error("MDX edits changed the surrounding component or source reference")
    cursor = len(separators[0])
    for index, (item, field, _prefix, _suffix, encode, decode) in enumerate(fields):
        separator = separators[index + 1]
        if index + 1 == len(fields) and edited.endswith(separator):
            end = len(edited) - len(separator)
        else:
            end = edited.find(separator, cursor)
        if end < cursor or (end == cursor and item[field]):
            raise _error("MDX edits changed the surrounding component or source reference")
        captured = edited[cursor:end]
        if captured == encode(item[field]):
            cursor = end + len(separator)
            continue
        value = decode(captured)
        if encode(value) != captured:
            raise _error("MDX edit uses unsupported markup; enter Markdown-escaped plain text")
        if value != item[field]:
            key = (id(item), field)
            if key in changes and changes[key][2] != value:
                raise _error("The same shared field has conflicting edits in two placements")
            changes[key] = (item, field, value)
        cursor = end + len(separator)
    if cursor != len(edited):
        raise _error("MDX edits changed the surrounding component or source reference")


def apply_mdx_edits(content: dict[str, Any], original: str, edited: str) -> dict[str, Any]:
    """Use the verified source view as the fixed grammar for each editable value."""
    header, blocks, sources = _sections(original)
    new_header, new_blocks, new_sources = _sections(edited)
    ids = [item["id"] for item in content["blocks"]]
    if (
        ids != [key for key, _ in blocks]
        or ids != [key for key, _ in new_blocks]
        or sources != new_sources
    ):
        raise _error("MDX source-binding and block order must remain fixed")
    changes: dict = {}
    _replace(
        header, new_header, [(content, "title", "# ", "\n", _markdown, _decode_markdown)], changes
    )
    numbers: dict[str, int] = {}
    for block in content["blocks"]:
        entries = [block] if block["type"] == "equation" else block.get("steps", [])
        for entry in entries:
            if entry.get("numbered"):
                numbers[entry["id"]] = len(numbers) + 1
    reference_prefix = "式" if content.get("language", "en").startswith("ja") else "Eq."
    identifier = r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}"
    link = rf"\[([^\[\]\n]+)\]\(#block-({identifier})\)"
    token = re.compile(
        rf"(\\\([^\n]*?\\\)|@eq\({identifier}\)|\[[^\[\]\n]+\]\(#block-{identifier}\))"
    )
    rendered = re.compile(rf"(?<!\\)\[(?:式|Eq\.) \([0-9]+\)\]\(#eq-({identifier})\)")

    def linked_markdown(value: str) -> str:
        if "@eq(" not in value and "](#block-" not in value:
            return _markdown(value)
        pieces = []
        for part in token.split(value):
            if re.fullmatch(rf"@eq\({identifier}\)", part):
                key = part[4:-1]
                if key not in numbers:
                    raise _error(f"Unknown numbered equation: {key}")
                pieces.append(f"[{reference_prefix} ({numbers[key]})](#eq-{key})")
            elif match := re.fullmatch(link, part):
                pieces.append(f"[{_markdown(match[1])}](#block-{match[2]})")
            else:
                pieces.append(_markdown(part))
        return "".join(pieces)

    def decode_linked(value: str) -> str:
        return _decode_markdown(rendered.sub(lambda match: f"@eq({match[1]})", value))

    def table_cell(value: str) -> str:
        # Table cells keep list markers literal; pipes and line breaks remain cell data.
        rendered = linked_markdown(value)
        rendered = re.sub(r"(?m)^\\(?=[#>+\-]|\d+[.)])", "", rendered)
        return rendered.replace("|", "\\|").replace("\n", " / ")

    def legacy_cell(value: str) -> str:
        value = re.sub(r"(?m)^\\(?=[#>+\-]|\d+[.)])", "", _markdown(value))
        return value.replace("|", "\\|").replace("\n", " / ")

    def decode_cell(value: str) -> str:
        return decode_linked(value.replace("\\|", "|"))

    figures = {item["id"]: item for item in content["figures"]}
    equations = {item["id"]: item for item in content["equations"]}
    for block, (_, before), (_, after) in zip(content["blocks"], blocks, new_blocks, strict=True):
        if before == after:
            continue
        fields: list[_Edit] = []
        match block["type"]:
            case "heading":
                fields.append(
                    (
                        block,
                        "text",
                        "#" * block["level"] + " ",
                        "\n",
                        linked_markdown,
                        decode_linked,
                    )
                )
            case "bullets":
                fields.append((block, "title", "**", "**", linked_markdown, decode_linked))
                for index in range(len(block["items"])):
                    fields.append(
                        (block["items"], index, "- ", "\n", linked_markdown, decode_linked)
                    )
            case "comparison":
                fields.append((block, "title", "**", "**", linked_markdown, decode_linked))
                for entry in block["cases"]:
                    fields.append(
                        (entry, "label", '<ComparisonCase label="', '">', _escape, html.unescape)
                    )
                    for index in range(len(entry["items"])):
                        fields.append(
                            (entry["items"], index, "- ", "\n", linked_markdown, decode_linked)
                        )
            case "paragraph":
                fields.append((block, "text", "\n\n", "\n\n", linked_markdown, decode_linked))
            case "background":
                fields.append((block, "title", '<Background title="', '">', _escape, html.unescape))
                for index in range(len(block["paragraphs"])):
                    fields.append(
                        (block["paragraphs"], index, "\n\n", "\n\n", linked_markdown, decode_linked)
                    )
            case "callout":
                fields.extend(
                    [
                        (block, "title", '<Callout title="', '">', _escape, html.unescape),
                        (block, "text", "\n\n", "\n\n", linked_markdown, decode_linked),
                    ]
                )
            case "table":
                fields.append((block, "caption", "**", "**", linked_markdown, decode_linked))
                for row in [block["headers"], *block["rows"]]:
                    for index in range(len(row)):
                        fields.append((row, index, "| ", " |", table_cell, decode_cell))
            case "figure":
                figure = figures[block["figure_id"]]
                fields.extend(
                    [
                        (figure, "alt", ' alt="', '"', _escape, html.unescape),
                        (figure, "caption", ' caption="', '"', _escape, html.unescape),
                    ]
                )
            case "equation":
                equation = equations[block["equation_id"]]
                opening = f'```math id="{equation["id"]}"\n'
                fields.append((equation, "tex", opening, "\n```", _identity, _identity))
            case "steps" | "procedure":
                for step in block["steps"]:
                    fields.append((step, "title", '<Step title="', '">', _escape, html.unescape))
                    if block["type"] == "procedure" and step.get("figure_id"):
                        figure = figures[step["figure_id"]]
                        fields.extend(
                            [
                                (figure, "alt", ' alt="', '"', _escape, html.unescape),
                                (figure, "caption", ' caption="', '"', _escape, html.unescape),
                            ]
                        )
                    fields.append(
                        (step, "rationale", "\n\n", "\n\n", linked_markdown, decode_linked)
                    )
                    parts = ("expression",) if step.get("type") == "operation" else _MATH_FIELDS
                    for part in parts:
                        if step.get(part) is not None:
                            opening = f'```math id="{block["id"]}-{step["id"]}-{part}"\n'
                            fields.append((step, part, opening, "\n```", _identity, _identity))
                if block["type"] == "procedure":
                    for step in block["steps"]:
                        fields.append(
                            (step, "explanation", "\n\n", "\n\n", linked_markdown, decode_linked)
                        )
        # An older package may escape a link or leave its equation reference literal.
        for index, (item, field, prefix, suffix, encode, _decode) in enumerate(fields):
            original_value = item[field]
            if "@eq(" not in original_value and "](#block-" not in original_value:
                continue
            if encode is linked_markdown:
                old_encode, old_decode = _markdown, _decode_markdown
            elif encode is table_cell:
                old_encode, old_decode = legacy_cell, decode_cell
            else:
                continue
            if prefix + old_encode(original_value) + suffix in before:
                fields[index] = (item, field, prefix, suffix, old_encode, old_decode)
        _replace(before, after, fields, changes)
    for item, field, value in changes.values():
        item[field] = value
    return content
