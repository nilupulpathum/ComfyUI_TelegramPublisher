"""T002: node registration accepts valid nodes and rejects bad ones loudly."""

import pytest

import publisher_nodes
from publisher_nodes import register


@pytest.fixture()
def clean_registry():
    class_mappings = dict(publisher_nodes.NODE_CLASS_MAPPINGS)
    display_mappings = dict(publisher_nodes.NODE_DISPLAY_NAME_MAPPINGS)
    try:
        yield
    finally:
        publisher_nodes.NODE_CLASS_MAPPINGS.clear()
        publisher_nodes.NODE_CLASS_MAPPINGS.update(class_mappings)
        publisher_nodes.NODE_DISPLAY_NAME_MAPPINGS.clear()
        publisher_nodes.NODE_DISPLAY_NAME_MAPPINGS.update(display_mappings)


def test_register_valid_node(clean_registry):
    class DummyNode:
        pass

    register(DummyNode, "Telegram Publisher Dummy", "Telegram Publisher Dummy")

    assert publisher_nodes.NODE_CLASS_MAPPINGS["Telegram Publisher Dummy"] is DummyNode
    assert (
        publisher_nodes.NODE_DISPLAY_NAME_MAPPINGS["Telegram Publisher Dummy"]
        == "Telegram Publisher Dummy"
    )


def test_register_duplicate_node_id_rejected(clean_registry):
    class NodeA:
        pass

    class NodeB:
        pass

    register(NodeA, "Telegram Publisher Dummy", "Dummy A")
    with pytest.raises(ValueError, match="duplicate node_id"):
        register(NodeB, "Telegram Publisher Dummy", "Dummy B")


def test_register_invalid_entries_rejected(clean_registry):
    class DummyNode:
        pass

    with pytest.raises(ValueError, match="node_id"):
        register(DummyNode, "", "Some Display Name")
    with pytest.raises(ValueError, match="node_class"):
        register("not-a-class", "Some Node Id", "Some Display Name")
    with pytest.raises(ValueError, match="display_name"):
        register(DummyNode, "Some Node Id", "")


def test_root_entry_point_reexports_nodes_registry():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("telegrampublisher_entry", root)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.NODE_CLASS_MAPPINGS == publisher_nodes.NODE_CLASS_MAPPINGS
    assert module.NODE_DISPLAY_NAME_MAPPINGS == publisher_nodes.NODE_DISPLAY_NAME_MAPPINGS
