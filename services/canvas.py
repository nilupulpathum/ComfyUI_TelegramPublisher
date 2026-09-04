"""Canvas-format workflow conversion + publish-node auto-append (T080).

ComfyUI persists workflows in two shapes:

- **canvas format** (what the frontend saves): a dict with ``nodes`` (list
  of node entries carrying widget values and link ids) and ``links`` (list
  of ``[id, from_node, from_slot, to_node, to_slot, type]`` rows);
- **API format** (what ``POST /prompt`` accepts): a bare map of
  ``{"<id>": {"class_type": ..., "inputs": {...}}}``, optionally wrapped
  as ``{"prompt": <map>}``.

This module converts canvas to API format. ``object_info`` (the exact
per-node input specs) is NOT available offline, so the converter fetches
it live from the target ComfyUI via :func:`fetch_object_info` and takes
the specs as data (injectable for tests). Pure conversion lives in
:func:`canvas_to_prompt`; :func:`append_publish` wires a
``Telegram Send Image`` node onto a converted map.

Loopback enforcement is the CALLER's job (``services.commands._run``
already validates ``comfy_host``/``comfy_port`` before fetching); this
module performs no allowlist checks itself.

Error contract (documented so callers map it to replies):

- :func:`fetch_object_info` raises :class:`ConnectionError` for transport
  failures (including non-200 HTTP) and :class:`ValueError` for malformed
  payloads (bad JSON, non-object shape). Callers map these to
  ``"trigger failed: ..."`` / ``"trigger misconfigured: ..."``.
- :func:`canvas_to_prompt` and :func:`append_publish` raise
  :class:`ValueError` naming the offending node/input. They fail loudly
  and never silently rewire: a kept input whose link is missing or points
  at a dropped/missing node is an error, not a guess.

Stdlib only. No ComfyUI imports. No tokens here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

#: Frontend node types that never reach the backend (annotations / routing).
_DROPPED_TYPES = frozenset({"Note", "Reroute"})

#: Widget value entries that are plain dicts are custom-node UI state
#: (e.g. LoraManager's ``__lm_autocomplete_meta_*`` record, which mirrors
#: the sibling text widget) — never backend inputs. They are skipped when
#: mapping ``widgets_values`` positionally onto widget-marked inputs.
OBJECT_INFO_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def detect_format(raw: Any) -> str:
    """Classify a parsed prompt file as ``"canvas"``/``"api_wrapped"``/``"api_bare"``.

    - ``"canvas"``: dict with list ``"nodes"`` + list ``"links"``.
    - ``"api_wrapped"``: dict whose single top key is ``"prompt"``
      holding a dict (of node dicts).
    - ``"api_bare"``: non-empty dict whose values are all dicts holding
      ``"class_type"``.

    Raises:
        ValueError: If ``raw`` (including any non-dict input — callers
            pass parsed JSON) matches none of the shapes.
    """
    if not isinstance(raw, dict):
        raise ValueError(
            "prompt file must hold a JSON object "
            f"(canvas nodes/links or API prompt map); got {type(raw).__name__}"
        )
    nodes = raw.get("nodes")
    links = raw.get("links")
    if isinstance(nodes, list) and isinstance(links, list):
        return "canvas"
    if set(raw.keys()) == {"prompt"} and isinstance(raw["prompt"], dict):
        return "api_wrapped"
    if raw and all(
        isinstance(value, dict) and "class_type" in value
        for value in raw.values()
    ):
        return "api_bare"
    raise ValueError(
        "prompt file is neither canvas format (nodes/links lists) "
        "nor API format (class_type map, optionally wrapped in 'prompt')"
    )


# ---------------------------------------------------------------------------
# object_info fetch (live, from the target ComfyUI)
# ---------------------------------------------------------------------------


def fetch_object_info(base_url: str, *, timeout: float = OBJECT_INFO_TIMEOUT) -> dict:
    """GET ``{base_url}/object_info`` from the target ComfyUI (read-only).

    Args:
        base_url: ``"http://<host>:<port>"`` — loopback enforcement is the
            CALLER's job (``_run`` passes only its validated host/port).
        timeout: Seconds for the HTTP read.

    Returns:
        The parsed ``object_info`` mapping
        (``{"Type": {"input": {"required": {name: [typedef, opts]}}}}``).

    Raises:
        ConnectionError: Transport failure or non-200 HTTP.
        ValueError: Bad JSON or a non-object payload.
    """
    url = base_url.rstrip("/") + "/object_info"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ConnectionError(
            f"cannot fetch object_info from {base_url}: "
            f"ComfyUI error HTTP {exc.code}"
        ) from exc
    except Exception as exc:
        raise ConnectionError(
            f"cannot fetch object_info from {base_url} "
            f"({type(exc).__name__})"
        ) from exc
    if status != 200:
        raise ConnectionError(
            f"cannot fetch object_info from {base_url}: "
            f"ComfyUI error HTTP {status}"
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"object_info from {base_url} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"object_info from {base_url} must hold a JSON object"
        )
    return parsed


# ---------------------------------------------------------------------------
# Canvas -> API prompt conversion (pure)
# ---------------------------------------------------------------------------


def _is_dropped(node_type: str) -> bool:
    return node_type in _DROPPED_TYPES or node_type.startswith("Label")


def canvas_to_prompt(canvas: dict, specs: dict) -> dict:
    """Convert a canvas-format workflow to a bare API prompt map.

    Args:
        canvas: Parsed canvas JSON (``nodes``/``links`` lists).
        specs: ``object_info`` shape
            (``{"Type": {"input": {"required": {name: [typedef, opts]}}}}``).

    Rules:

    - Keep nodes with ``mode`` in ``(0, None/missing)``; DROP any other
      ``mode``, types ``{"Note", "Reroute"}``, and types starting with
      ``"Label"``. Entries missing ``id``/``type`` are garbage ->
      :class:`ValueError` listing them.
    - Links resolve via the links table (``[id, from, slot, ...]``). A
      kept input whose link id is missing from the table or points at a
      dropped/missing node -> :class:`ValueError` naming node+input
      (fail loudly, never silently rewire).
    - Per kept node (output keys are ``str(id)``):
      ``{"class_type": type, "inputs": {...}}`` with inputs in canvas
      order. Linked inputs become ``[str(from_node), from_slot]``.
      Unlinked inputs WITH a ``"widget"`` marker consume
      ``widgets_values`` positionally, SKIPPING top-level dict values
      (custom-node UI state such as LoraManager autocomplete metadata —
      never backend inputs); a widget-marked input with no remaining
      value falls back to the spec required default. Unlinked inputs
      WITHOUT a marker take the spec required default; no default and
      no value -> :class:`ValueError` naming node+input. A widget-marked
      unlinked input whose name is optional-only is skipped (its
      positional value is still consumed, preserving alignment for the
      inputs that follow); a name in neither required nor optional ->
      :class:`ValueError`.
    - Spec defaults: ``required[name] = [typedef, opts]``; the default is
      ``opts.get("default")`` when ``opts`` is a dict, else none.
      Unknown node types (absent from ``specs``) -> :class:`ValueError`
      naming the type. Optional inputs are included ONLY when linked
      (frontend parity).

    Known approximation: ``widgets_values`` is positional, so widgets
    without a canvas input entry (e.g. KSampler's
    ``control_after_generate``) shift the values that follow. The live
    trial (T082) must confirm real workflows convert faithfully.
    """
    if not isinstance(canvas, dict):
        raise ValueError("canvas workflow must be a JSON object")
    nodes = canvas.get("nodes")
    links = canvas.get("links")
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise ValueError("canvas workflow must hold 'nodes' and 'links' lists")
    if not isinstance(specs, dict):
        raise ValueError("object_info specs must be a JSON object")

    # -- link table: link id -> (from_node, from_slot) ---------------------
    link_table: dict[Any, tuple[Any, Any]] = {}
    for entry in links:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) < 5
            or not isinstance(entry[0], (int, str, float))
            or isinstance(entry[0], bool)
        ):
            raise ValueError(f"canvas links table holds a malformed entry: {entry!r}")
        link_table[entry[0]] = (entry[1], entry[2])

    # -- split kept / dropped ----------------------------------------------
    kept: dict[str, dict] = {}
    kept_order: list[str] = []
    dropped_ids: set[str] = set()
    garbage: list[str] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or "id" not in node or "type" not in node:
            garbage.append(f"index {index}")
            continue
        node_id = node["id"]
        node_type = node["type"]
        if not isinstance(node_type, str) or not node_type:
            garbage.append(f"index {index}")
            continue
        key = str(node_id)
        if key in kept or key in dropped_ids:
            raise ValueError(f"canvas workflow has a duplicate node id '{key}'")
        mode = node.get("mode", 0)
        if mode not in (0, None) or _is_dropped(node_type):
            dropped_ids.add(key)
            continue
        kept[key] = node
        kept_order.append(key)
    if garbage:
        raise ValueError(
            "canvas workflow has node entries missing id/type "
            f"({', '.join(garbage)})"
        )

    # -- per-node conversion -------------------------------------------------
    prompt: dict[str, dict] = {}
    for key in kept_order:
        node = kept[key]
        node_type = node["type"]
        spec_entry = specs.get(node_type)
        if not isinstance(spec_entry, dict):
            raise ValueError(
                f"canvas node '{key}' has unknown type '{node_type}' "
                "(absent from object_info)"
            )
        spec_inputs = spec_entry.get("input", {})
        if not isinstance(spec_inputs, dict):
            spec_inputs = {}
        required = spec_inputs.get("required", {})
        optional = spec_inputs.get("optional", {})
        if not isinstance(required, dict):
            required = {}
        if not isinstance(optional, dict):
            optional = {}

        raw_values = node.get("widgets_values", [])
        if raw_values is None:
            raw_values = []
        if not isinstance(raw_values, list):
            raise ValueError(
                f"canvas node '{key}' ({node_type}) has malformed widgets_values"
            )
        # Positional values, skipping top-level dicts (custom-node UI
        # state, never backend inputs).
        values = [v for v in raw_values if not isinstance(v, dict)]
        cursor = 0

        inputs: dict[str, Any] = {}
        canvas_inputs = node.get("inputs", [])
        if canvas_inputs is None:
            canvas_inputs = []
        if not isinstance(canvas_inputs, list):
            raise ValueError(
                f"canvas node '{key}' ({node_type}) has malformed inputs"
            )
        for entry in canvas_inputs:
            if not isinstance(entry, dict) or "name" not in entry:
                raise ValueError(
                    f"canvas node '{key}' ({node_type}) has a malformed input entry"
                )
            name = entry["name"]
            link = entry.get("link")
            if link is not None:
                if link not in link_table:
                    raise ValueError(
                        f"canvas node '{key}' ({node_type}) input '{name}' "
                        f"links to missing link id '{link}'"
                    )
                from_node, from_slot = link_table[link]
                from_key = str(from_node)
                if from_key not in kept:
                    raise ValueError(
                        f"canvas node '{key}' ({node_type}) input '{name}' "
                        f"links to dropped or missing node '{from_key}'"
                    )
                inputs[str(name)] = [from_key, from_slot]
                continue
            # Unlinked.
            in_required = isinstance(name, str) and name in required
            in_optional = isinstance(name, str) and name in optional
            marked = entry.get("widget") is not None
            if marked:
                value: Any = None
                have_value = False
                if cursor < len(values):
                    value = values[cursor]
                    have_value = True
                cursor += 1
                if not in_required:
                    if in_optional:
                        continue  # optional-only: value consumed, not sent.
                    raise ValueError(
                        f"canvas node '{key}' ({node_type}) input '{name}' "
                        "is unknown to object_info"
                    )
                if have_value:
                    inputs[str(name)] = value
                    continue
                default = _spec_default(required, name)
                if default is None and not _has_default(required, name):
                    raise ValueError(
                        f"canvas node '{key}' ({node_type}) input '{name}' "
                        "has no widget value and no object_info default"
                    )
                inputs[str(name)] = default
                continue
            if not in_required:
                if in_optional:
                    continue  # optional-only and unlinked: omit.
                raise ValueError(
                    f"canvas node '{key}' ({node_type}) input '{name}' "
                    "is unknown to object_info"
                )
            if not _has_default(required, name):
                raise ValueError(
                    f"canvas node '{key}' ({node_type}) input '{name}' "
                    "has no widget value and no object_info default"
                )
            inputs[str(name)] = _spec_default(required, name)
        prompt[key] = {"class_type": node_type, "inputs": inputs}
    return prompt


def _spec_default(required: dict, name: str) -> Any:
    """Return the ``object_info`` required default for ``name`` (or None)."""
    typedef = required.get(name)
    if not isinstance(typedef, (list, tuple)) or len(typedef) < 2:
        return None
    opts = typedef[1]
    if isinstance(opts, dict):
        return opts.get("default")
    return None


def _has_default(required: dict, name: str) -> bool:
    """Whether ``required[name]`` declares an explicit ``default``."""
    typedef = required.get(name)
    if not isinstance(typedef, (list, tuple)) or len(typedef) < 2:
        return False
    opts = typedef[1]
    return isinstance(opts, dict) and "default" in opts


# ---------------------------------------------------------------------------
# Publish-node auto-append
# ---------------------------------------------------------------------------


def append_publish(
    prompt_map: dict, spec: Any, *, qualname: str = "Telegram Send Image"
) -> dict:
    """Return a copy of ``prompt_map`` with a Send Image node appended.

    Args:
        prompt_map: Bare API prompt map (never mutated; copied).
        spec: PublishSpec-like object with ``account``, ``destination``,
            ``source`` (``"id:slot"`` or None for auto-detect),
            ``caption_template`` (already rendered by the caller — used
            verbatim as the ``caption`` input), ``format``, ``quality``.
        qualname: ``class_type`` of the appended node.

    Source resolution: an explicit ``source`` of ``"id:slot"`` is used
    after validating the node exists in the map; otherwise the first
    node with ``class_type == "VAEDecode"`` supplies slot 0 (documented
    convention — the API map carries no output metadata, so the image
    output slot cannot be discovered). No VAEDecode (or a bad explicit
    source) -> :class:`ValueError`.

    The new id is ``str(max(int ids) + 1)`` over the int-parseable keys,
    or ``"telegram_send_1"`` when no key parses as an int.
    """
    if not isinstance(prompt_map, dict):
        raise ValueError("prompt map must be a JSON object")
    account = getattr(spec, "account", None)
    destination = getattr(spec, "destination", None)
    source = getattr(spec, "source", None)
    caption = getattr(spec, "caption_template", "")
    fmt = getattr(spec, "format", "png")
    quality = getattr(spec, "quality", 90)

    if source is not None:
        src_id, src_slot = _parse_source(source, prompt_map)
    else:
        src_id, src_slot = _autodetect_source(prompt_map)

    int_ids: list[int] = []
    for key in prompt_map.keys():
        try:
            int_ids.append(int(str(key)))
        except (ValueError, TypeError):
            continue
    new_id = str(max(int_ids) + 1) if int_ids else "telegram_send_1"

    publish_node = {
        "class_type": qualname,
        "inputs": {
            "image": [src_id, src_slot],
            "account": account,
            "destination": destination,
            "caption": caption,
            "format": fmt,
            "quality": quality,
            "protect_content": False,
            "disable_notification": False,
            "skip_duplicate": False,
            "wait_for_upload": True,
        },
    }
    updated = dict(prompt_map)
    updated[new_id] = publish_node
    return updated


def _parse_source(source: Any, prompt_map: dict) -> tuple[str, int]:
    """Parse an explicit ``"id:slot"`` source against ``prompt_map``."""
    if not isinstance(source, str) or ":" not in source:
        raise ValueError(
            f"publish source must look like 'id:slot'; got {source!r}"
        )
    node_part, _, slot_part = source.partition(":")
    node_part = node_part.strip()
    slot_part = slot_part.strip()
    if node_part not in prompt_map:
        raise ValueError(
            f"publish source node '{node_part}' is not in the prompt map"
        )
    try:
        slot = int(slot_part)
    except (ValueError, TypeError):
        raise ValueError(
            f"publish source slot must be an int; got {slot_part!r}"
        ) from None
    if slot < 0:
        raise ValueError(
            f"publish source slot must be >= 0; got {slot_part!r}"
        )
    return node_part, slot


def _autodetect_source(prompt_map: dict) -> tuple[str, int]:
    """First ``VAEDecode`` node (map order) supplies image slot 0."""
    for key, node in prompt_map.items():
        if isinstance(node, dict) and node.get("class_type") == "VAEDecode":
            return str(key), 0
    raise ValueError(
        "publish auto-detect found no VAEDecode node; "
        "set an explicit publish source ('id:slot')"
    )
