"""ComfyUI Telegram Publisher extension entry point.

ComfyUI discovers custom nodes by importing this file and reading
``NODE_CLASS_MAPPINGS`` / ``NODE_DISPLAY_NAME_MAPPINGS``.

The mappings live in the ``publisher_nodes`` package (single source of truth);
node classes land there in later tasks (T009, T031).
This module must import without ComfyUI installed, without secrets, and
without network access so core services stay unit-testable.
"""

import os
import sys

# ComfyUI loads this file by path without adding the extension directory to
# sys.path, so sibling packages would not be importable. Bootstrap our own
# directory (guarded against duplicates). This is the standard pattern for
# multi-file custom nodes.
_EXTENSION_DIR = os.path.dirname(os.path.realpath(__file__))
if _EXTENSION_DIR not in sys.path:
    sys.path.insert(0, _EXTENSION_DIR)

from publisher_nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from publisher_nodes import routes  # Unguarded: our own bugs fail loudly.

__version__ = "0.1.0"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "__version__",
]

#: Frontend extension directory served by ComfyUI (web/settings.js, T050).
WEB_DIRECTORY = "web"

# T053/T054: attach the settings/status HTTP API when running inside
# ComfyUI. Standalone import (tests, tooling) keeps working: without the
# ComfyUI ``server`` module there is nothing to register on.
# ``PromptServer.instance.routes`` is a ``web.RouteTableDef`` (decorator
# style; verified against ComfyUI server.py), which is what
# ``routes.register_routes`` expects. Any registration failure is loud
# (console warning) so a half-wired extension is never silent.
try:
    from server import PromptServer  # type: ignore[import-not-found]
except ImportError:
    PromptServer = None  # type: ignore[assignment]

if PromptServer is not None:
    try:
        routes.register_routes(PromptServer.instance.routes)
    except Exception as exc:  # Loud startup warning, never a silent skip.
        print(f"[ComfyUI-TelegramPublisher] route registration failed: {exc}")
