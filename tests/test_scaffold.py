"""T001 scaffold smoke test: extension imports cleanly under ComfyUI conventions."""

import importlib.util
from pathlib import Path


def test_root_mappings_exist():

    root = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("telegrampublisher_entry", root)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert isinstance(module.NODE_CLASS_MAPPINGS, dict)
    assert isinstance(module.NODE_DISPLAY_NAME_MAPPINGS, dict)
    # T009 registers the Send Image node on publisher_nodes import.
    assert "Telegram Send Image" in module.NODE_CLASS_MAPPINGS
    assert (
        module.NODE_DISPLAY_NAME_MAPPINGS["Telegram Send Image"]
        == "Telegram Send Image"
    )


def test_layer_packages_importable():
    import publisher_nodes
    import services
    import storage
    import telegram

    for pkg in (publisher_nodes, services, storage, telegram):
        assert pkg.__name__ in {
            "publisher_nodes",
            "services",
            "storage",
            "telegram",
        }
