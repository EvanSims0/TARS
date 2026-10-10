"""Change values in config.toml in place, keeping the comments and layout people wrote.

Used by the Settings window and the setup wizard. A commented-out line (`# latitude = 40.71`)
is replaced where it sits; a missing key is added at the end of its section.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SECTION = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(#.*)?$")


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a TOML basic string
    if isinstance(value, (list, tuple)):
        if any(isinstance(v, str) and len(v) > 40 for v in value):
            return "[\n" + "".join(f"  {toml_value(v)},\n" for v in value) + "]"
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"can't write {type(value).__name__} to config")


def _comment_after(line: str) -> str:
    """A trailing comment, kept when the value is replaced (only after two spaces, as in the example)."""
    match = re.search(r"\s{2,}#\s.*$", line)
    if not match:
        return ""
    before = line[: match.start()]
    return match.group(0) if before.count('"') % 2 == 0 else ""


def set_values(path: Path, values: dict[str, Any]) -> None:
    """Set dotted keys, e.g. {"user_name": "Evan", "location.latitude": 40.7}."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for dotted, value in values.items():
        section, _, key = dotted.rpartition(".")
        lines = _set_one(lines, section, key, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _remove(lines: list[str], section: str, key: str) -> list[str]:
    found = _section_range(lines, section)
    if found is None:
        return lines
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    return [line for i, line in enumerate(lines) if not (found[0] <= i < found[1] and key_re.match(line))]


def _section_range(lines: list[str], section: str) -> tuple[int, int] | None:
    """Line range of a section's body; "" is the top level, before the first header."""
    headers = [(i, m.group(1)) for i, line in enumerate(lines) if (m := _SECTION.match(line))]
    if not section:
        return 0, headers[0][0] if headers else len(lines)
    for n, (i, name) in enumerate(headers):
        if name == section:
            return i + 1, headers[n + 1][0] if n + 1 < len(headers) else len(lines)
    return None


def _set_one(lines: list[str], section: str, key: str, value: Any) -> list[str]:
    if value is None:  # TOML has no "empty": leave the key out, so the default applies
        return _remove(lines, section, key)
    found = _section_range(lines, section)
    if found is None:
        lines = lines + ["", f"[{section}]"]
        found = (len(lines), len(lines))
    start, end = found
    key_re = re.compile(rf"^(\s*)(#\s*)?{re.escape(key)}\s*=")
    rendered = toml_value(value)
    for i in range(start, end):
        match = key_re.match(lines[i])
        if not match:
            continue
        stop = i  # a multi-line array runs to its closing bracket
        rhs = lines[i].split("=", 1)[1]
        if not match.group(2) and "[" in rhs and "]" not in rhs:
            while stop + 1 < end and not lines[stop].rstrip().endswith("]"):
                stop += 1
        comment = _comment_after(lines[i]) if not match.group(2) and stop == i else ""
        return lines[:i] + f"{match.group(1)}{key} = {rendered}{comment}".split("\n") + lines[stop + 1:]
    insert = end  # not there yet: add it at the end of the section, before blank lines
    while insert > start and not lines[insert - 1].strip():
        insert -= 1
    return lines[:insert] + f"{key} = {rendered}".split("\n") + lines[insert:]
