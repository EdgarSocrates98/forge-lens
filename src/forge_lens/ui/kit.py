"""Stdlib interactive UI kit — ANSI, zero-dependency, TTY-gated.

Everything degrades honestly:

- Non-TTY (CI, pipes, SSH without terminal): interactive functions raise
  ``NonInteractive`` — callers convert to a clear error or JSON output.
- No color (``NO_COLOR``, ``TERM=dumb``): still navigable, no escapes.
- cp1252/narrow terminals: ASCII-only glyphs; content never truncated
  without an ellipsis.

Key handling: ``msvcrt`` on Windows, ``termios``+``tty`` elsewhere.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from forge_lens.ui.i18n import t

if sys.platform != "win32":  # pragma: no cover - platform
    import termios
    import tty


class NonInteractive(Exception):
    """Raised when an interactive prompt is requested without a TTY."""


@dataclass(frozen=True, kw_only=True)
class UIContext:
    """Terminal capability snapshot — detected once, passed around."""

    interactive: bool
    color: bool
    unicode: bool
    width: int
    height: int = 24
    ansi: bool = True
    language: str = "en"
    # glyphs chosen by capability
    cursor: str = ">"
    checked: str = "x"
    unchecked: str = " "
    ok: str = "OK"
    fail: str = "FAIL"
    arrow: str = "->"

    @staticmethod
    def detect(*, force_plain: bool = False, language: str | None = None) -> UIContext:
        from forge_lens.ui.i18n import lang

        interactive = sys.stdin.isatty() and sys.stdout.isatty() and not force_plain
        term = os.environ.get("TERM", "")
        # cursor-addressing escapes are unsafe on dumb terminals even when a
        # tty is present — `ansi` gates \x1b[.. moves, `color` gates SGR.
        ansi = interactive and term != "dumb"
        no_color = bool(os.environ.get("NO_COLOR")) or not ansi
        encoding = (sys.stdout.encoding or "").lower()
        uni = _supports_unicode(encoding)
        size = terminal_size()
        return UIContext(
            interactive=interactive,
            color=not no_color,
            unicode=uni,
            width=size[0],
            height=size[1],
            ansi=ansi,
            language=language or lang(),
        )


def terminal_size(
    *,
    stream: TextIO | None = None,
    fallback: tuple[int, int] = (80, 24),
) -> tuple[int, int]:
    """Return usable terminal cells, preferring active stream and clamping bad probes."""
    try:
        if stream is not None:
            size = os.get_terminal_size(stream.fileno())
            raw = (size.columns, size.lines)
        else:
            size = shutil.get_terminal_size(fallback)
            raw = (size.columns, size.lines)
    except (AttributeError, OSError, ValueError):
        raw = fallback
    return max(1, int(raw[0])), max(1, int(raw[1]))


def _c(ctx: UIContext, code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if ctx.color else text


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _supports_unicode(encoding: str) -> bool:
    """Recognize UTF-8 aliases used by Windows Terminal and POSIX shells."""

    normalized = encoding.lower().replace("-", "").replace("_", "")
    return "utf" in normalized or normalized.endswith("65001")


def cell_width(text: str) -> int:
    """Terminal cell width — not len(): CJK/W chars take 2, combining 0."""
    import unicodedata

    w = 0
    for ch in _ANSI_RE.sub("", text):
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad_visible(text: str, width: int) -> str:
    """Pad to a visible width — ANSI escapes don't count."""
    return text + " " * max(0, width - cell_width(text))


def _ellipsize(ctx: UIContext, text: str, width: int | None = None) -> str:
    w = min(width if width is not None else ctx.width - 2, ctx.width - 2)
    if w <= 0:
        return ""
    if cell_width(text) <= w:
        return text
    out, used = [], 0
    for ch in text:
        cw = cell_width(ch)
        if used + cw > w - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + ("…" if ctx.unicode else ".")


# -- key reading -------------------------------------------------------------

_UP = "up"
_DOWN = "down"
_ENTER = "enter"
_SPACE = "space"
_ESC = "esc"
_CTRL_C = "ctrl-c"


def _read_key() -> str:
    """Blocking read of one normalized key press."""
    if sys.platform == "win32":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {"H": _UP, "P": _DOWN}.get(code, "other")
        if ch in ("\r", "\n"):
            return _ENTER
        if ch == " ":
            return _SPACE
        if ch == "\x1b":
            return _ESC
        if ch == "\x03":
            return _CTRL_C
        if ch == "\x0b":
            return "ctrl-k"
        return f"char:{ch}"
    ch = sys.stdin.read(1)  # pragma: no cover - platform
    if not ch:  # pragma: no cover - platform
        return "ctrl-d"
    if ch == "\x1b":  # pragma: no cover - platform
        # A bare Esc must not block: the CSI suffix may never arrive. Poll
        # briefly for "[A"-style sequences; timeout means it was just Esc.
        import select

        seq = ""
        for _ in range(2):
            if select.select([sys.stdin], [], [], 0.06)[0]:
                seq += sys.stdin.read(1)
            else:
                break
        return {"[A": _UP, "[B": _DOWN}.get(seq, _ESC)
    if ch in ("\r", "\n"):
        return _ENTER
    if ch == " ":
        return _SPACE
    if ch == "\x03":
        return _CTRL_C
    if ch == "\x0b":
        return "ctrl-k"
    return f"char:{ch}"


class _RawMode:
    def __enter__(self) -> _RawMode:
        if sys.platform != "win32":  # pragma: no cover - platform
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        return self

    def __exit__(self, *exc: object) -> None:
        if sys.platform != "win32":  # pragma: no cover - platform
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)


def _rewrite_lines(stream: TextIO, n: int) -> None:
    """Move up n lines and clear to end — redraw a menu in place."""
    if n > 0:
        stream.write(f"\x1b[{n}A")
    stream.write("\x1b[J")
    stream.flush()


def _redraw(ctx: UIContext, stream: TextIO, lines_drawn: int, out: list[str]) -> int:
    """Redraw the frame. Dumb terminals get a full re-print instead of
    cursor-addressing escapes they cannot honor."""
    if ctx.ansi:
        _rewrite_lines(stream, lines_drawn)
    else:
        stream.write("\n")
    rendered = "\n".join(out) + "\n"
    stream.write(rendered)
    stream.flush()
    return rendered.count("\n")


def _option_window(ctx: UIContext, n: int, idx: int, extra: int = 0) -> tuple[int, int]:
    """Visible slice of an option list — never draw past the screen or the
    in-place redraw corrupts. Keeps the cursor inside the window."""
    avail = max(3, ctx.height - 5 - extra)  # title + hint + blank + margin
    if n <= avail:
        return 0, n
    half = avail // 2
    start = min(max(0, idx - half), n - avail)
    return start, start + avail


def _more_marker(ctx: UIContext, direction: str, count: int) -> str:
    arrow = (
        ("↑" if direction == "up" else "↓") if ctx.unicode else ("^" if direction == "up" else "v")
    )
    return _c(ctx, "2", f"    {arrow} {count} more")


# -- primitives ---------------------------------------------------------------


def select(
    title: str,
    options: list[str],
    *,
    ctx: UIContext | None = None,
    descriptions: list[str] | None = None,
) -> int | None:
    """Arrow-key single select. Returns index, or None on cancel."""
    ctx = ctx or UIContext.detect()
    if not ctx.interactive:
        raise NonInteractive("select requires a TTY")
    if not options:
        return None
    idx = 0
    lines_drawn = 0
    with _RawMode():
        while True:
            out = [
                f"{_c(ctx, '1', title)}",
                f"  {_c(ctx, '2', t('select_hint', ctx.language))}",
                "",
            ]
            start, end = _option_window(ctx, len(options), idx, extra=1 if descriptions else 0)
            if start:
                out.append(_more_marker(ctx, "up", start))
            for i in range(start, end):
                opt = options[i]
                mark = ctx.cursor if i == idx else " "
                line = f" {mark} {_ellipsize(ctx, opt, ctx.width - 6)}"
                out.append(_c(ctx, "7", line) if i == idx and ctx.color else line)
                if descriptions and i == idx and descriptions[i]:
                    out.append(
                        f"      {_c(ctx, '2', _ellipsize(ctx, descriptions[i], ctx.width - 10))}"
                    )
            if end < len(options):
                out.append(_more_marker(ctx, "down", len(options) - end))
            lines_drawn = _redraw(ctx, sys.stdout, lines_drawn, out)

            key = _read_key()
            if key == _UP or key == "char:k":
                idx = (idx - 1) % len(options)
            elif key == _DOWN or key == "char:j":
                idx = (idx + 1) % len(options)
            elif key == _ENTER:
                return idx
            elif key in (_ESC, _CTRL_C) or key in ("char:q", "char:Q"):
                return None


def multi_select(
    title: str,
    options: list[str],
    *,
    checked: set[int] | None = None,
    ctx: UIContext | None = None,
) -> set[int] | None:
    """Space-toggle multi select. Returns chosen indices or None."""
    ctx = ctx or UIContext.detect()
    if not ctx.interactive:
        raise NonInteractive("multi_select requires a TTY")
    chosen = set(checked or set())
    idx = 0
    lines_drawn = 0
    if not options:
        return set()
    with _RawMode():
        while True:
            out = [
                f"{_c(ctx, '1', title)}",
                f"  {_c(ctx, '2', t('toggle_hint', ctx.language))}",
                "",
            ]
            start, end = _option_window(ctx, len(options), idx)
            if start:
                out.append(_more_marker(ctx, "up", start))
            for i in range(start, end):
                opt = options[i]
                mark = ctx.cursor if i == idx else " "
                box = ctx.checked if i in chosen else ctx.unchecked
                line = f" {mark} [{box}] {_ellipsize(ctx, opt, ctx.width - 10)}"
                out.append(_c(ctx, "7", line) if i == idx and ctx.color else line)
            if end < len(options):
                out.append(_more_marker(ctx, "down", len(options) - end))
            lines_drawn = _redraw(ctx, sys.stdout, lines_drawn, out)

            key = _read_key()
            if key == _UP or key == "char:k":
                idx = (idx - 1) % len(options)
            elif key == _DOWN or key == "char:j":
                idx = (idx + 1) % len(options)
            elif key == _SPACE:
                chosen ^= {idx}
            elif key == _ENTER:
                return chosen
            elif key in (_ESC, _CTRL_C) or key in ("char:q", "char:Q"):
                return None


def prompt(label: str, *, default: str = "", ctx: UIContext | None = None) -> str | None:
    """Single-line text prompt (cooked mode — safe for paste)."""
    ctx = ctx or UIContext.detect()
    if not ctx.interactive:
        raise NonInteractive("prompt requires a TTY")
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{label}{suffix}: ")
    except (EOFError, KeyboardInterrupt):
        return None
    return answer.strip() or default or None


def confirm(question: str, *, default: bool = False, ctx: UIContext | None = None) -> bool:
    """y/N confirm — cooked mode, forgiving input."""
    ctx = ctx or UIContext.detect()
    if not ctx.interactive:
        raise NonInteractive("confirm requires a TTY")
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not answer:
        return default
    return answer in ("y", "yes", "s", "sim")


# -- static rendering ---------------------------------------------------------


def _state_icon(ctx: UIContext, state: str) -> str:
    good = {"OK", "HEALTHY", "READY", "ACTIVE_NOW", "COMPLETED", "DETECTED", "PASS"}
    bad = {"FAIL", "FAILED", "DEGRADED", "BLOCKED", "UNSUPPORTED"}
    if state.upper() in good:
        return _c(ctx, "32", ctx.ok)
    if state.upper() in bad:
        return _c(ctx, "31", ctx.fail)
    return _c(ctx, "33", state.upper()[:8])


@dataclass(kw_only=True)
class Section:
    title: str
    rows: list[tuple[str, str]] = field(default_factory=list)


def dashboard(
    title: str,
    sections: list[Section | tuple[str, list[tuple[str, str]]]],
    *,
    ctx: UIContext | None = None,
) -> str:
    """Render a one-shot status dashboard; returns the text written."""
    ctx = ctx or UIContext.detect()
    out = [""]
    bar = "─" if ctx.unicode else "-"
    out.append(_c(ctx, "1", f" {title.upper()}"))
    out.append(_c(ctx, "2", bar * min(ctx.width - 1, max(10, cell_width(title) + 8))))
    for sec in sections:
        stitle, rows = (sec.title, sec.rows) if isinstance(sec, Section) else sec
        out.append("")
        out.append(_c(ctx, "36", stitle))
        for label, value in rows:
            val = _ellipsize(ctx, value, ctx.width - 26)
            out.append(f"  {_pad_visible(label, 22)} {val}")
    out.append("")
    rendered = "\n".join(out)
    sys.stdout.write(rendered + "\n")
    sys.stdout.flush()
    return rendered


def status_table(
    rows: list[tuple[str, str, str]],
    *,
    ctx: UIContext | None = None,
    headers: tuple[str, str, str] = ("", "", ""),
) -> str:
    """``(name, state, detail)`` rows with state icon; returns text."""
    ctx = ctx or UIContext.detect()
    w_name = max([cell_width(r[0]) for r in rows] + [cell_width(headers[0]), 8])
    out = []
    if any(headers):
        out.append(_c(ctx, "2", f"  {headers[0]:<{w_name}}  {headers[1]:<10} {headers[2]}"))
    for name, state, detail in rows:
        icon = _pad_visible(_state_icon(ctx, state), 10)
        line = f"  {_pad_visible(name, w_name)}  {icon} {_ellipsize(ctx, detail)}"
        out.append(line)
    rendered = "\n".join(out)
    sys.stdout.write(rendered + "\n")
    sys.stdout.flush()
    return rendered


# -- forge/* document rendering ----------------------------------------------

_GOOD = {
    "OK",
    "PASS",
    "HEALTHY",
    "COMPLETED",
    "INSTALLED",
    "DETECTED",
    "READY",
    "ACTIVE_NOW",
    "DONE",
    "SUPPORTED",
}
_BAD = {
    "FAIL",
    "FAILED",
    "BROKEN",
    "MISSING",
    "UNSUPPORTED",
    "REFUSED",
    "BLOCKED",
}
_WARN = {
    "DEGRADED",
    "UNVERIFIED",
    "PLANNED",
    "SKIPPED",
    "VENDORED",
    "NOT_INSTALLED",
    "PENDING",
    "MODIFIED",
}


def state_color(ctx: UIContext, state: str, text: str | None = None) -> str:
    """Color a lifecycle/doctor state; unknown states stay dim."""
    label = text if text is not None else state
    s = state.upper()
    if s in _GOOD:
        return _c(ctx, "32", label)
    if s in _BAD:
        return _c(ctx, "31", label)
    if s in _WARN:
        return _c(ctx, "33", label)
    return _c(ctx, "2", label)


def _check_icon(ctx: UIContext, status: str) -> str:
    s = (status or "").upper()
    if s in _GOOD:
        mark = "✓" if ctx.unicode else "v"
        return _c(ctx, "32", mark)
    if s in _BAD:
        mark = "✗" if ctx.unicode else "x"
        return _c(ctx, "31", mark)
    if s == "NOT_APPLICABLE":
        return _c(ctx, "2", "—" if ctx.unicode else "-")
    mark = "?" if not ctx.unicode else "◇"
    return _c(ctx, "33", mark)


def _kv(ctx: UIContext, label: str, value: object, width: int = 12) -> str | None:
    if value in (None, "", [], {}):
        return None
    return f"  {_pad_visible(label, width)} {_ellipsize(ctx, str(value))}"


def format_status_doc(doc: dict[str, Any], *, ctx: UIContext | None = None) -> str:
    """Render any ``forge/*`` install/health/receipt doc for humans.

    Pure — returns the text; callers write it. Honors ``ctx`` color /
    unicode / width so pipes and ``NO_COLOR`` stay clean. Only sections
    with real data are emitted; unknown statuses render dim, never red.
    """
    ctx = ctx or UIContext.detect()
    out: list[str] = [""]
    bar = "─" if ctx.unicode else "-"
    op = doc.get("operation") or doc.get("command") or "status"
    title = f" {doc.get('forge_id', 'forge')} · {op}".upper()
    out.append(_c(ctx, "1", title))
    out.append(_c(ctx, "2", bar * min(ctx.width - 1, max(10, cell_width(title) + 4))))

    meta = [
        _kv(
            ctx,
            t("doc_status", ctx.language),
            doc.get("status")
            and state_color(ctx, str(doc["status"]), str(doc["status"]).upper())
            or None,
        ),
        _kv(ctx, t("doc_target", ctx.language), doc.get("target_root")),
        _kv(ctx, t("doc_scope", ctx.language), doc.get("scope")),
        _kv(ctx, t("doc_version", ctx.language), doc.get("version")),
        _kv(
            ctx,
            t("doc_profile", ctx.language),
            (doc.get("context") or {}).get("profile") or doc.get("profile"),
        ),
        _kv(ctx, t("doc_state_dir", ctx.language), doc.get("state_dir")),
    ]
    if doc.get("ledger_entries") is not None:
        meta.append(
            _kv(
                ctx,
                t("doc_managed", ctx.language),
                f"{doc['ledger_entries']} {t('doc_entries', ctx.language)}",
            )
        )
    for line in meta:
        if line:
            out.append(line)

    checks = doc.get("checks") or []
    if checks:
        out.append("")
        out.append(_c(ctx, "36", t("doc_checks", ctx.language)))
        w_id = min(28, max(cell_width(str(c.get("id", ""))) for c in checks) + 1)
        for c in checks:
            icon = _check_icon(ctx, str(c.get("status", "")))
            detail = str(c.get("detail") or "")
            if c.get("repairable"):
                detail = (detail + "  " if detail else "") + _c(
                    ctx, "36", t("doc_repairable", ctx.language)
                )
            line = f"  {icon} {_pad_visible(str(c.get('id', '?')), w_id)}"
            if detail:
                line += f" {_ellipsize(ctx, detail, ctx.width - w_id - 8)}"
            out.append(line)

    drift = doc.get("drift")
    if isinstance(drift, dict) and (drift.get("ok") is not None):
        modified = drift.get("modified") or []
        missing = drift.get("missing") or []
        out.append("")
        out.append(_c(ctx, "36", t("doc_drift", ctx.language)))
        ok_n = drift.get("ok", 0)
        mod_lbl = f"{t('doc_modified', ctx.language)} {len(modified)}"
        miss_lbl = f"{t('doc_missing', ctx.language)} {len(missing)}"
        line = (
            f"  {_c(ctx, '32', 'ok ' + str(ok_n))}  "
            f"{state_color(ctx, 'MODIFIED', mod_lbl)}  "
            f"{state_color(ctx, 'MISSING', miss_lbl)}"
        )
        out.append(line)
        for entry in (modified + missing)[:6]:
            path = entry.get("path") if isinstance(entry, dict) else str(entry)
            kind = entry.get("status") if isinstance(entry, dict) else "?"
            out.append(
                f"    {_c(ctx, '2', '·')} {state_color(ctx, str(kind or '?'), str(kind))} "
                f"{_ellipsize(ctx, str(path), ctx.width - 12)}"
            )
        if len(modified + missing) > 6:
            out.append(_c(ctx, "2", f"    … {len(modified + missing) - 6} more"))

    files = doc.get("managed_files") or doc.get("planned_files")
    if files:
        label = "doc_managed_files" if doc.get("managed_files") is not None else "doc_planned_files"
        out.append("")
        out.append(_c(ctx, "36", t(label, ctx.language)))
        out.append(f"  {len(files)}")
        for f in files[:5]:
            path = f.get("path") if isinstance(f, dict) else str(f)
            out.append(f"    {_c(ctx, '2', '·')} {_ellipsize(ctx, str(path), ctx.width - 8)}")
        if len(files) > 5:
            out.append(_c(ctx, "2", f"    … {len(files) - 5} more"))

    portal = doc.get("forge_portal")
    if isinstance(portal, dict):
        out.append("")
        out.append(_c(ctx, "36", "FORGE PORTAL"))
        state = str(portal.get("state", "?"))
        where = portal.get("install_root") or portal.get("manifest") or ""
        detail = state_color(ctx, state)
        if where:
            detail += f"  {_ellipsize(ctx, str(where), ctx.width - 18)}"
        elif state == "vendored":
            detail += _c(ctx, "2", f"  {t('doc_portal_vendored', ctx.language)}")
        out.append(f"  {detail}")

    ws = doc.get("workspace")
    if isinstance(ws, dict) and ws.get("members"):
        out.append("")
        out.append(_c(ctx, "36", t("doc_workspace", ctx.language)))
        out.append(f"  {len(ws['members'])} {t('doc_members', ctx.language)}")
        for m in ws.get("members") or []:
            path = m.get("path") if isinstance(m, dict) else str(m)
            out.append(f"    {_c(ctx, '2', '·')} {_ellipsize(ctx, str(path), ctx.width - 8)}")
        for c in ws.get("conflicts") or []:
            member = c.get("member", "?") if isinstance(c, dict) else str(c)
            fid = c.get("forge_id") if isinstance(c, dict) else None
            decision = c.get("decision") if isinstance(c, dict) else None
            lbl = f"{member}/{fid}" if fid else str(member)
            if decision:
                lbl += f": {decision}"
            out.append(f"    {_c(ctx, '33', '▲')} {_ellipsize(ctx, lbl, ctx.width - 10)}")
        for m in ws.get("unknown_members") or []:
            out.append(
                f"    {_c(ctx, '33', '·')} {m} "
                f"{_c(ctx, '2', t('doc_unknown_member', ctx.language))}"
            )
        for r in ws.get("member_results") or []:
            out.append(
                f"    {_c(ctx, '2', '·')} {r.get('member', '?')}: "
                f"{state_color(ctx, str(r.get('status', '?')))}"
            )

    verification = doc.get("verification")
    if isinstance(verification, dict) and verification.get("status"):
        out.append("")
        line = f"  {t('doc_verification', ctx.language):<13} "
        line += state_color(ctx, str(verification["status"]), str(verification["status"]).upper())
        out.append(line)

    error = doc.get("error")
    if isinstance(error, dict) and (error.get("kind") or error.get("detail")):
        out.append("")
        out.append(
            f"  {_c(ctx, '31', t('doc_refused', ctx.language))} "
            f"{_ellipsize(ctx, str(error.get('kind', '')))}"
        )
        if error.get("detail"):
            out.append(f"    {_ellipsize(ctx, str(error['detail']), ctx.width - 8)}")
        if error.get("unlock"):
            out.append(
                f"    {_c(ctx, '36', t('doc_unlock', ctx.language))} "
                f"{_ellipsize(ctx, str(error['unlock']), ctx.width - 12)}"
            )

    if doc.get("repair_hint"):
        out.append("")
        out.append(f"  {_c(ctx, '36', t('doc_repair', ctx.language))}  {doc['repair_hint']}")
    out.append("")
    return "\n".join(out)


def approve_plan(doc: dict[str, Any], *, ctx: UIContext | None = None) -> bool:
    """Print a planned install doc and ask to apply it. TTY only."""
    ctx = ctx or UIContext.detect()
    if not ctx.interactive:
        raise NonInteractive("approve_plan requires a TTY")
    sys.stdout.write(format_status_doc(doc, ctx=ctx) + "\n")
    sys.stdout.flush()
    return confirm(t("apply_q", ctx.language), default=True, ctx=ctx)
