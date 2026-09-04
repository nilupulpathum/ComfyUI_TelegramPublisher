"""Generation metadata extractor (services layer).

Collects ComfyUI generation parameters (prompt, seed, sampler, dimensions,
...) into a typed container that the caption renderer and history storage
can consume. Pure stdlib; no ComfyUI imports, no network, no logging.
"""

from __future__ import annotations

import math
import operator
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _str_or_none(value: Any) -> str | None:
    """Coerce a mapping value to ``str``; ``None``/empty-string -> ``None``.

    Never raises: values that cannot be stringified resolve to ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value if value != "" else None
    try:
        text = str(value)
    except Exception:
        return None
    return text if text != "" else None


def _int_or_none(value: Any) -> int | None:
    """Coerce a mapping value to ``int``; anything unparseable -> ``None``.

    Accepts ints, finite floats (truncated), int-like objects, and numeric
    strings. Booleans, ``None``, empty strings, and non-numeric values
    resolve to ``None``. Never raises.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            if not math.isfinite(value):
                return None
            return int(value)
        except Exception:
            return None
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        try:
            return int(text)
        except ValueError:
            pass
        try:
            as_float = float(text)
        except (ValueError, OverflowError):
            return None
        try:
            if not math.isfinite(as_float):
                return None
            return int(as_float)
        except Exception:
            return None
    try:
        return operator.index(value)
    except Exception:
        return None


def _to_var(value: Any) -> str:
    """Render a dataclass field as a template variable (never ``None``)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


@dataclass
class GenerationMetadata:
    """Generation parameters for caption templating and history.

    Every field is optional: missing parameters stay ``None`` (rendered as
    ``""`` by :meth:`as_template_vars`). ``timestamp`` defaults to the
    construction time in ISO-8601 UTC.
    """

    prompt: str | None = None
    negative_prompt: str | None = None
    seed: str | None = None
    steps: str | None = None
    cfg: str | None = None
    sampler: str | None = None
    scheduler: str | None = None
    model: str | None = None
    width: int | None = None
    height: int | None = None
    filename: str | None = None
    timestamp: str | None = field(default_factory=_utc_now_iso)

    @classmethod
    def from_mapping(
        cls, source: Mapping[str, object] | None
    ) -> GenerationMetadata:
        """Build metadata from a loose key/value mapping.

        Tolerates ``None`` (all ``None`` except ``timestamp``), ignores
        unknown keys, stringifies scalar values, and never raises on odd
        input -- unparseable values resolve to ``None``. Keys absent from
        the mapping fall back to field defaults (so ``timestamp``
        defaults to now unless explicitly provided).
        """
        if source is None or not isinstance(source, Mapping):
            return cls()

        kwargs: dict[str, Any] = {}
        for key in (
            "prompt",
            "negative_prompt",
            "seed",
            "steps",
            "cfg",
            "sampler",
            "scheduler",
            "model",
            "width",
            "height",
            "filename",
            "timestamp",
        ):
            try:
                if key not in source:
                    continue
                value = source[key]
            except Exception:
                continue
            if key in ("width", "height"):
                kwargs[key] = _int_or_none(value)
            else:
                kwargs[key] = _str_or_none(value)
        try:
            return cls(**kwargs)
        except Exception:
            return cls()

    def as_template_vars(self) -> dict[str, str]:
        """Return all twelve caption variables with ``None`` -> ``""``."""
        return {
            "prompt": _to_var(self.prompt),
            "negative_prompt": _to_var(self.negative_prompt),
            "seed": _to_var(self.seed),
            "steps": _to_var(self.steps),
            "cfg": _to_var(self.cfg),
            "sampler": _to_var(self.sampler),
            "scheduler": _to_var(self.scheduler),
            "model": _to_var(self.model),
            "width": _to_var(self.width),
            "height": _to_var(self.height),
            "filename": _to_var(self.filename),
            "timestamp": _to_var(self.timestamp),
        }
