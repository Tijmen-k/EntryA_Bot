"""Small, dependency-free formatting helpers reused across every cog."""
from __future__ import annotations


def usdt(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "—"
    fmt = "{:+,.2f}" if signed else "{:,.2f}"
    return f"${fmt.format(value)}"


def pct(value: float | None, signed: bool = True, already_pct: bool = True) -> str:
    """`already_pct=True` means `value` is already e.g. 12.3 (not 0.123)."""
    if value is None:
        return "—"
    v = value if already_pct else value * 100
    fmt = "{:+.2f}%" if signed else "{:.2f}%"
    return fmt.format(v)


def code_table(headers: list[str], rows: list[list[str]], max_rows: int | None = None) -> str:
    """Render a monospaced, column-aligned table inside a Discord code block."""
    if max_rows is not None:
        rows = rows[:max_rows]
    if not rows:
        return "```\n(no data)\n```"

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [fmt_row(headers), fmt_row(["-" * w for w in widths])]
    lines.extend(fmt_row(row) for row in rows)
    return "```\n" + "\n".join(lines) + "\n```"


def progress_bar(pct_complete: float, width: int = 20) -> str:
    pct_complete = max(0.0, min(100.0, pct_complete))
    filled = round(width * pct_complete / 100)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct_complete:.1f}%"


def duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def truncate(text: str, max_len: int = 1000) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
