"""Comment-preserving edits to the ``sources`` / ``filters.rules`` lists.

``config.yaml`` is hand-written and heavily commented, so ``yaml.safe_dump``
(which throws every comment away) is not an option. Instead the file is treated
as text: the list block is located by indentation and only those lines are
replaced. The result is re-parsed with PyYAML and compared before anything is
written, so a bad edit can never corrupt the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import yaml


class ConfigFileError(ValueError):
    """Raised when the config file cannot be edited safely."""


# --------------------------------------------------------------------------- #
# Low level text helpers
# --------------------------------------------------------------------------- #
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _read(path: str | Path) -> Tuple[List[str], str, bool]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        raw = handle.read()
    eol = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(eol)
    trailing_newline = bool(lines) and lines[-1] == ""
    if trailing_newline:
        lines.pop()
    return lines, eol, trailing_newline


def _write(path: str | Path, lines: Sequence[str], eol: str, trailing_newline: bool) -> None:
    text = eol.join(lines) + (eol if trailing_newline else "")
    Path(path).write_text(text, encoding="utf-8", newline="")


def _find_header(lines: Sequence[str], key: str, indent: int, start: int = 0) -> int:
    """Index of a ``key:`` line at exactly ``indent`` (comments are ignored)."""
    for index in range(start, len(lines)):
        line = lines[index]
        if _is_blank_or_comment(line):
            continue
        if _indent(line) != indent:
            continue
        stripped = line.strip()
        if stripped == f"{key}:" or stripped.startswith(f"{key}:"):
            return index
    return -1


def _section_end(lines: Sequence[str], header: int) -> int:
    """Index of the first line that starts a new top-level key."""
    for index in range(header + 1, len(lines)):
        line = lines[index]
        if _is_blank_or_comment(line):
            continue
        if _indent(line) == 0:
            return index
    return len(lines)


def _block_end(lines: Sequence[str], header: int) -> int:
    """Index just past the last non-blank line belonging to ``header``'s block."""
    header_indent = _indent(lines[header])
    last = header
    for index in range(header + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if _indent(line) > header_indent:
            last = index
            continue
        break
    return last + 1


def _format_item(value: str, indent: int) -> List[str]:
    """One YAML list item, rendered by PyYAML so quoting/escaping is correct."""
    dumped = yaml.safe_dump(
        [value], default_flow_style=False, allow_unicode=True, width=100000
    )
    pad = " " * indent
    return [pad + line if line else line for line in dumped.rstrip("\n").split("\n")]


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _key_path(section: Optional[str], key: str) -> Tuple[str, ...]:
    return (key,) if section is None else (section, key)


def _extract(data: Any, section: Optional[str], key: str) -> List[str]:
    node = data
    for part in _key_path(section, key):
        if not isinstance(node, dict):
            return []
        node = node.get(part)
    if node is None:
        return []
    if not isinstance(node, list):
        return [str(node)]
    return [str(item) for item in node]


def read_list(path: str | Path, key: str, section: Optional[str] = None) -> List[str]:
    """Current list as stored in the file (no validation, no defaults)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except OSError as exc:
        raise ConfigFileError(f"Cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigFileError(f"{path} is not valid YAML: {exc}") from exc
    return _extract(data, section, key)


def write_list(
    path: str | Path,
    key: str,
    values: Sequence[str],
    section: Optional[str] = None,
) -> None:
    """Replace ``key``'s list in place, keeping every surrounding comment."""
    path = Path(path)
    lines, eol, trailing_newline = _read(path)

    header = -1
    indent = 2 if section is not None else 0
    if section is None:
        header = _find_header(lines, key, 0)
    else:
        section_header = _find_header(lines, section, 0)
        if section_header < 0:
            raise ConfigFileError(
                f"{path} has no '{section}:' section, so '{key}' cannot be edited automatically."
            )
        end = _section_end(lines, section_header)
        for index in range(section_header + 1, end):
            line = lines[index]
            if _is_blank_or_comment(line):
                continue
            if _indent(line) == 2 and line.strip() == f"{key}:":
                header = index
                indent = 2
                break

    item_lines: List[str] = []
    for value in values:
        item_lines.extend(_format_item(value, indent + 2))

    if header < 0:
        # The key is absent: add it right after the section header.
        if section is None:
            raise ConfigFileError(f"{path} has no '{key}:' line to edit.")
        insert_at = _find_header(lines, section, 0) + 1
        if values:
            lines[insert_at:insert_at] = [f"{' ' * indent}{key}:"] + item_lines
        else:
            lines.insert(insert_at, f"{' ' * indent}{key}: []")
    else:
        # Drop an inline value (`sources: ["@a"]`) in favour of a block list.
        header_line = lines[header]
        stripped = header_line.strip()
        inline = stripped[len(key) + 1 :]
        if not values:
            # An empty block would parse as null; `key: []` is unambiguous.
            lines[header] = " " * indent + f"{key}: []"
            start, stop = header + 1, _block_end(lines, header)
            del lines[start:stop]
        else:
            if inline.strip() and not inline.strip().startswith("#"):
                lines[header] = " " * indent + f"{key}:"
            start, stop = header + 1, _block_end(lines, header)
            lines[start:stop] = item_lines

    # Safety net: the edited file must still parse and contain exactly `values`.
    new_text = eol.join(lines) + (eol if trailing_newline else "")
    try:
        parsed = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise ConfigFileError(
            f"Refusing to write {path}: the edit produced invalid YAML ({exc})."
        ) from exc
    if _extract(parsed, section, key) != list(values):  # pragma: no cover - defensive
        raise ConfigFileError(
            f"Refusing to write {path}: the edit did not round-trip correctly."
        )

    _write(path, lines, eol, trailing_newline)
