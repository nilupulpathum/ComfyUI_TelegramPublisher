"""Caption template renderer (services layer).

Substitutes ``{{name}}`` placeholders from a variables mapping (typically
:meth:`services.metadata.GenerationMetadata.as_template_vars`). FR-009
fail-safe: unknown or missing variables resolve to ``""`` with a warning
instead of failing. Over-long captions are truncated, never rejected.

Pure stdlib (``re``). No logging here; the caller logs ``warnings``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: Telegram photo-caption limit.
MAX_CAPTION_LENGTH = 1024

#: ``{{name}}`` placeholder; whitespace inside the braces is tolerated.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


@dataclass
class RenderedCaption:
    """Rendered caption text plus fail-safe warnings for the caller to log."""

    text: str
    warnings: list[str] = field(default_factory=list)


def render_caption(
    template: str | None, variables: Mapping[str, object]
) -> RenderedCaption:
    """Render ``template`` by substituting ``{{name}}`` placeholders.

    Args:
        template: Caption template, or ``None``/``""`` for no caption.
        variables: Placeholder values; non-string values are stringified.

    Returns:
        RenderedCaption with the rendered text and any warnings
        (missing/unknown variable left blank, truncation applied).

    Raises:
        ValueError: If ``template`` is neither ``str`` nor ``None``.
    """
    if template is None:
        return RenderedCaption(text="", warnings=[])
    if not isinstance(template, str):
        raise ValueError(
            f"caption template must be str or None; got {type(template).__name__}."
        )
    if template == "":
        return RenderedCaption(text="", warnings=[])

    if variables is None or not isinstance(variables, Mapping):
        variables = {}

    warnings: list[str] = []
    warned: set[str] = set()

    def _warn_once(name: str, message: str) -> None:
        if name not in warned:
            warned.add(name)
            warnings.append(message)

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            value: Any = variables.get(name)
        except Exception:
            value = None
        if value is None or (isinstance(value, str) and value == ""):
            _warn_once(
                name,
                f"caption variable '{name}' is missing or empty; left blank.",
            )
            return ""
        if isinstance(value, str):
            return value
        try:
            text = str(value)
        except Exception:
            _warn_once(
                name,
                f"caption variable '{name}' could not be stringified; left blank.",
            )
            return ""
        if text == "":
            _warn_once(
                name,
                f"caption variable '{name}' is missing or empty; left blank.",
            )
            return ""
        return text

    rendered = _PLACEHOLDER_RE.sub(_replace, template)
    if len(rendered) > MAX_CAPTION_LENGTH:
        rendered = rendered[:MAX_CAPTION_LENGTH]
        warnings.append(
            f"caption exceeded {MAX_CAPTION_LENGTH} characters "
            "(Telegram photo-caption limit); truncated."
        )
    return RenderedCaption(text=rendered, warnings=warnings)
