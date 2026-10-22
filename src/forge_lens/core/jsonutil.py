"""Lexical JSON-depth bound shared by every inbound parser surface.

``json.loads`` recursion depth is platform- and build-dependent (the C
scanner's frame cost differs across CPython versions and OS builds —
e.g. a 100k-deep bracket payload overflows one build and parses fine
on another), so the bound is enforced lexically on the raw bytes before
any parser runs: identical on Linux, Windows and macOS. 512 sits far
above the semantic caps (AnyValue flattens at 32) and far below every
supported interpreter's parse-recursion limit.
"""

from __future__ import annotations

MAX_JSON_DEPTH = 512


def json_depth(raw: bytes | str) -> int:
    """Maximum ``{``/``[`` nesting outside strings — O(n), no recursion.

    Brace/bracket characters inside string literals (and their escapes)
    don't count; unbalanced closers can't push depth negative.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "surrogatepass")
    depth = max_depth = 0
    in_str = esc = False
    for b in raw:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:  # backslash
                esc = True
            elif b == 0x22:  # "
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b == 0x7B or b == 0x5B:  # { [
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif b == 0x7D or b == 0x5D:  # } ]
            depth -= 1
            if depth < 0:
                depth = 0
    return max_depth
