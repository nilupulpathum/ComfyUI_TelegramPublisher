"""T080 canvas conversion + publish auto-append. Loopback-only fakes.

No real ComfyUI calls: ``fetch_object_info`` is tested against a loopback
``http.server`` thread on an ephemeral port serving ``/object_info``.
Synthetic fixtures are grounded in the real Anima canvas shapes (canvas
``nodes``/``links`` tables, ``widgets_values`` with LoraManager-style
dict UI state, KSampler-style widget lists).
"""

from __future__ import annotations

import http.server
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from services.canvas import (
    append_publish,
    canvas_to_prompt,
    detect_format,
    fetch_object_info,
    unconnected_node_ids,
)

# ---------------------------------------------------------------------------
# Mini object_info specs (data, injected — object_info is unavailable offline)
# ---------------------------------------------------------------------------


def mini_specs() -> dict:
    return {
        "CLIPTextEncode": {
            "input": {
                "required": {
                    "clip": ["CLIP", {}],
                    "text": ["STRING", {"multiline": True, "default": ""}],
                }
            }
        },
        "Prompt (LoraManager)": {
            "input": {
                "required": {
                    "clip": ["CLIP", {}],
                    "text": ["STRING", {"default": ""}],
                },
                "optional": {
                    "trigger_words2": ["STRING", {}],
                    "trigger_words3": ["STRING", {}],
                },
            }
        },
        "KSampler": {
            "input": {
                "required": {
                    "model": ["MODEL", {}],
                    "seed": ["INT", {"default": 0}],
                    "steps": ["INT", {"default": 20}],
                    "cfg": ["FLOAT", {"default": 8.0}],
                    "sampler_name": ["COMBO", {"default": "euler"}],
                    "scheduler": ["COMBO", {"default": "normal"}],
                    "denoise": ["FLOAT", {"default": 1.0}],
                }
            }
        },
        "VAEDecode": {
            "input": {
                "required": {
                    "samples": ["LATENT", {}],
                    "vae": ["VAE", {}],
                }
            }
        },
        "EmptyLatentImage": {
            "input": {
                "required": {
                    "width": ["INT", {"default": 512}],
                    "height": ["INT", {"default": 512}],
                    "batch_size": ["INT", {"default": 1}],
                }
            }
        },
        "VAELoader": {
            "input": {
                "required": {
                    "vae_name": ["COMBO", {"default": "default.safetensors"}],
                }
            }
        },
    }


def link(link_id: int, frm: int, slot: int, to: int, to_slot: int) -> list:
    return [link_id, frm, slot, to, to_slot, "X"]


# ---------------------------------------------------------------------------
# detect_format
# ---------------------------------------------------------------------------


def test_detect_canvas() -> None:
    assert detect_format({"nodes": [], "links": []}) == "canvas"
    # Extra top-level keys (groups/config/extra) do not change the verdict.
    assert (
        detect_format({"nodes": [], "links": [], "extra": {}}) == "canvas"
    )


def test_detect_api_wrapped_and_bare() -> None:
    wrapped = {"prompt": {"1": {"class_type": "KSampler", "inputs": {}}}}
    assert detect_format(wrapped) == "api_wrapped"
    bare = {"1": {"class_type": "KSampler", "inputs": {}}}
    assert detect_format(bare) == "api_bare"


def test_detect_garbage_raises() -> None:
    for bad in (
        {},
        {"prompt": [1, 2]},
        {"prompt": "x"},
        {"1": {"inputs": {}}},  # no class_type
        {"1": {"class_type": "X"}, "oops": 42},
        [],
        "nodes",
        None,
        42,
    ):
        with pytest.raises(ValueError):
            detect_format(bad)


# ---------------------------------------------------------------------------
# canvas_to_prompt
# ---------------------------------------------------------------------------


def anima_like_canvas() -> dict:
    """KSampler <- Prompt(LoraManager, dict-skipped widgets) + bypass drop."""
    return {
        "nodes": [
            {
                "id": 28,
                "type": "Prompt (LoraManager)",
                "mode": 0,
                "inputs": [
                    {"name": "clip", "type": "CLIP", "link": 31},
                    {"name": "text", "type": "STRING",
                     "widget": {"name": "text"}, "link": None},
                    {"name": "trigger_words2", "type": "STRING", "link": None},
                ],
                "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}],
                # LoraManager shape: leading dict is UI state, never input.
                "widgets_values": [
                    {"version": 1, "textWidgetName": "text"},
                    "masterpiece, 1girl",
                ],
            },
            {
                "id": 4,
                "type": "KSampler",
                "mode": 0,
                "inputs": [
                    {"name": "model", "type": "MODEL", "link": 36},
                    {"name": "seed", "type": "INT",
                     "widget": {"name": "seed"}, "link": None},
                    {"name": "steps", "type": "INT",
                     "widget": {"name": "steps"}, "link": None},
                ],
                "outputs": [{"name": "LATENT", "type": "LATENT"}],
                "widgets_values": [235398267971802, 10, 8.0, "euler",
                                   "normal", 1.0],
            },
            {"id": 99, "type": "Note", "mode": 0,
             "inputs": [], "outputs": [],
             "widgets_values": ["annotation"]},
            {"id": 100, "type": "Label (rgthree)", "mode": 0,
             "inputs": [], "outputs": []},
            {"id": 101, "type": "Reroute", "mode": 0,
             "inputs": [], "outputs": []},
            {"id": 55, "type": "KSampler", "mode": 4,
             "inputs": [], "outputs": [], "widgets_values": []},
        ],
        "links": [
            link(31, 30, 1, 28, 0),
            link(36, 30, 0, 4, 0),
        ],
    }


def test_canvas_happy_dict_skip_and_drops() -> None:
    canvas = anima_like_canvas()
    # Link sources 30 (dropped-missing) would fail loudly, so point the
    # kept links at kept nodes instead: add minimal source nodes.
    canvas["nodes"].extend(
        [
            {"id": 30, "type": "VAELoader", "mode": 0,
             "inputs": [{"name": "vae_name", "type": "COMBO",
                         "widget": {"name": "vae_name"}, "link": None}],
             "outputs": [{"name": "X", "type": "X"}],
             "widgets_values": ["qwen_image_vae.safetensors"]},
            {"id": 40, "type": "VAELoader", "mode": 0,
             "inputs": [{"name": "vae_name", "type": "COMBO",
                         "widget": {"name": "vae_name"}, "link": None}],
             "outputs": [{"name": "X", "type": "X"}],
             "widgets_values": ["other.safetensors"]},
        ]
    )
    canvas["links"] = [link(31, 30, 1, 28, 0), link(36, 40, 0, 4, 0)]
    prompt = canvas_to_prompt(canvas, mini_specs())
    assert set(prompt) == {"28", "4", "30", "40"}
    # Dict UI state skipped: text gets the real prompt string.
    assert prompt["28"]["inputs"]["text"] == "masterpiece, 1girl"
    assert prompt["28"]["inputs"]["clip"] == ["30", 1]
    # Optional-only unlinked input omitted (frontend parity).
    assert "trigger_words2" not in prompt["28"]["inputs"]
    # Widget-marked values map positionally.
    assert prompt["4"]["inputs"]["seed"] == 235398267971802
    assert prompt["4"]["inputs"]["steps"] == 10
    # Unlisted required KSampler inputs (cfg/...) are filled from spec
    # defaults (frontend parity), not omitted.
    assert prompt["4"]["inputs"]["cfg"] == 8.0


def test_canvas_dangling_link_fails_loud() -> None:
    # No links at all: the node is unconnected, so it drops quietly.
    canvas = {
        "nodes": [
            {"id": 1, "type": "CLIPTextEncode", "mode": 0,
             "inputs": [
                 {"name": "clip", "type": "CLIP", "link": 7},
                 {"name": "text", "type": "STRING",
                  "widget": {"name": "text"}, "link": None},
             ],
             "outputs": [], "widgets_values": ["hi"]},
        ],
        "links": [],
    }
    assert canvas_to_prompt(canvas, mini_specs()) == {}
    # Link id resolves in the table but points at a missing node: refusal.
    canvas["links"] = [link(7, 2, 0, 1, 0)]
    with pytest.raises(ValueError, match="'1'.*'clip'.*'2'"):
        canvas_to_prompt(canvas, mini_specs())


def test_canvas_link_to_bypassed_node_fails() -> None:
    canvas = {
        "nodes": [
            {"id": 1, "type": "VAEDecode", "mode": 0,
             "inputs": [
                 {"name": "samples", "type": "LATENT", "link": 5},
                 {"name": "vae", "type": "VAE", "link": 6},
             ],
             "outputs": []},
            {"id": 2, "type": "KSampler", "mode": 4,
             "inputs": [], "outputs": []},
        ],
        "links": [link(5, 2, 0, 1, 0)],
    }
    with pytest.raises(ValueError, match="'1'.*'samples'"):
        canvas_to_prompt(canvas, mini_specs())


def test_canvas_unknown_type_fails() -> None:
    # Connected (link touches it) but absent from object_info: loud.
    canvas = {
        "nodes": [{"id": 1, "type": "NopeNode", "mode": 0,
                   "inputs": [], "outputs": []},
                  {"id": 2, "type": "VAELoader", "mode": 0,
                   "inputs": [{"name": "vae_name", "type": "COMBO",
                               "widget": {"name": "vae_name"}, "link": None}],
                   "outputs": [], "widgets_values": ["x.safetensors"]}],
        "links": [link(9, 1, 0, 2, 0)],
    }
    with pytest.raises(ValueError, match="NopeNode"):
        canvas_to_prompt(canvas, mini_specs())


def test_canvas_garbage_entries_listed() -> None:
    canvas = {
        "nodes": [{"foo": 1}, {"id": 2}, {"type": "X"}],
        "links": [],
    }
    with pytest.raises(ValueError, match="id/type"):
        canvas_to_prompt(canvas, mini_specs())


def test_canvas_unmarked_uses_spec_default_or_fails() -> None:
    # EmptyLatentImage batch_size: unlinked, no widget marker.
    # NOTE: the links-table entry only marks node 5 connected; none of
    # its inputs reference it, so conversion is unaffected.
    canvas = {
        "nodes": [
            {"id": 5, "type": "EmptyLatentImage", "mode": 0,
             "inputs": [
                 {"name": "width", "type": "INT",
                  "widget": {"name": "width"}, "link": None},
                 {"name": "height", "type": "INT",
                  "widget": {"name": "height"}, "link": None},
                 {"name": "batch_size", "type": "INT", "link": None},
             ],
             "outputs": [], "widgets_values": [1024, 1024]},
        ],
        "links": [link(3, 5, 0, 5, 0)],
    }
    prompt = canvas_to_prompt(canvas, mini_specs())
    assert prompt["5"]["inputs"] == {
        "width": 1024, "height": 1024, "batch_size": 1
    }
    # No default anywhere and no value -> loud failure naming node+input.
    specs = {
        "EmptyLatentImage": {
            "input": {
                "required": {
                    "width": ["INT", {"default": 512}],
                    "height": ["INT", {"default": 512}],
                    "batch_size": ["INT", {}],
                }
            }
        }
    }
    with pytest.raises(ValueError, match="'5'.*'batch_size'"):
        canvas_to_prompt(canvas, specs)


def test_canvas_widget_exhaustion_falls_back_to_default() -> None:
    # NOTE: table-only link marks node 5 connected; conversion unaffected.
    canvas = {
        "nodes": [
            {"id": 5, "type": "EmptyLatentImage", "mode": 0,
             "inputs": [
                 {"name": "width", "type": "INT",
                  "widget": {"name": "width"}, "link": None},
             ],
             "outputs": [], "widgets_values": []},
        ],
        "links": [link(3, 5, 0, 5, 0)],
    }
    prompt = canvas_to_prompt(canvas, mini_specs())
    assert prompt["5"]["inputs"] == {"width": 512, "height": 512,
                                     "batch_size": 1}


def test_canvas_optional_only_when_linked() -> None:
    optional_linked = {
        "nodes": [
            {"id": 28, "type": "Prompt (LoraManager)", "mode": 0,
             "inputs": [
                 {"name": "clip", "type": "CLIP", "link": 31},
                 {"name": "text", "type": "STRING",
                  "widget": {"name": "text"}, "link": None},
                 {"name": "trigger_words2", "type": "STRING", "link": 32},
             ],
             "outputs": [], "widgets_values": ["hello"]},
            {"id": 30, "type": "VAELoader", "mode": 0,
             "inputs": [], "outputs": [], "widgets_values": []},
        ],
        "links": [link(31, 30, 1, 28, 0), link(32, 30, 2, 28, 2)],
    }
    prompt = canvas_to_prompt(optional_linked, mini_specs())
    assert prompt["28"]["inputs"]["trigger_words2"] == ["30", 2]


def test_canvas_mode_missing_or_none_kept() -> None:
    # NOTE: table-only link marks both nodes connected.
    canvas = {
        "nodes": [
            {"id": 1, "type": "VAELoader",
             "inputs": [{"name": "vae_name", "type": "COMBO",
                         "widget": {"name": "vae_name"}, "link": None}],
             "outputs": [], "widgets_values": ["a.safetensors"]},
            {"id": 2, "type": "VAELoader", "mode": None,
             "inputs": [{"name": "vae_name", "type": "COMBO",
                         "widget": {"name": "vae_name"}, "link": None}],
             "outputs": [], "widgets_values": ["b.safetensors"]},
        ],
        "links": [link(5, 1, 0, 2, 0)],
    }
    prompt = canvas_to_prompt(canvas, mini_specs())
    assert set(prompt) == {"1", "2"}


# ---------------------------------------------------------------------------
# append_publish
# ---------------------------------------------------------------------------


def publish_spec(**over: Any) -> Any:
    base: dict[str, Any] = {
        "account": "acc1",
        "destination": "dst1",
        "source": None,
        "caption_template": "hi",
        "format": "png",
        "quality": 90,
    }
    base.update(over)
    return SimpleNamespace(**base)


def vae_map() -> dict:
    return {
        "4": {"class_type": "KSampler", "inputs": {"seed": 1}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["4", 0], "vae": ["15", 0]}},
    }


def test_append_publish_auto_detect_and_wiring() -> None:
    before = vae_map()
    out = append_publish(before, publish_spec())
    assert before == vae_map()  # input map not mutated
    assert out is not before
    assert out["6"] == before["6"]  # existing nodes untouched
    node = out["7"]
    assert node["class_type"] == "Telegram Send Image"
    assert node["inputs"] == {
        "image": ["6", 0],
        "account": "acc1",
        "destination": "dst1",
        "caption": "hi",
        "format": "png",
        "quality": 90,
        "protect_content": False,
        "disable_notification": False,
        "skip_duplicate": False,
        "wait_for_upload": True,
    }


def test_append_publish_explicit_source() -> None:
    out = append_publish(vae_map(), publish_spec(source="4:0"))
    assert out["7"]["inputs"]["image"] == ["4", 0]


def test_append_publish_bad_source_and_missing_vae() -> None:
    with pytest.raises(ValueError, match="99"):
        append_publish(vae_map(), publish_spec(source="99:0"))
    with pytest.raises(ValueError, match="slot"):
        append_publish(vae_map(), publish_spec(source="4:xx"))
    with pytest.raises(ValueError, match="VAEDecode"):
        append_publish({"1": {"class_type": "KSampler", "inputs": {}}},
                       publish_spec())


def test_append_publish_id_selection() -> None:
    out = append_publish(vae_map(), publish_spec())
    assert "7" in out  # max int id + 1
    out2 = append_publish({"a": {"class_type": "VAEDecode", "inputs": {}}},
                          publish_spec())
    assert "telegram_send_1" in out2


# ---------------------------------------------------------------------------
# fetch_object_info (loopback fake ComfyUI)
# ---------------------------------------------------------------------------


def start_object_info_server(mode: str = "ok"):
    def _handler_factory():
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                assert self.path == "/object_info", self.path
                if mode == "error":
                    data = json.dumps({"error": "boom"}).encode()
                    self.send_response(500)
                elif mode == "junk":
                    data = b"{not json"
                    self.send_response(200)
                elif mode == "shape":
                    data = json.dumps([1, 2]).encode()
                    self.send_response(200)
                else:
                    data = json.dumps({"KSampler": {}}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        return Handler

    server = http.server.HTTPServer(("127.0.0.1", 0), _handler_factory())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def base_url(server: Any) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_fetch_object_info_ok() -> None:
    server = start_object_info_server("ok")
    try:
        assert fetch_object_info(base_url(server)) == {"KSampler": {}}
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_object_info_errors() -> None:
    server = start_object_info_server("error")
    try:
        with pytest.raises(ConnectionError, match="HTTP 500"):
            fetch_object_info(base_url(server))
    finally:
        server.shutdown()
        server.server_close()
    for mode in ("junk", "shape"):
        server = start_object_info_server(mode)
        try:
            with pytest.raises(ValueError, match="object_info"):
                fetch_object_info(base_url(server))
        finally:
            server.shutdown()
            server.server_close()


def test_fetch_object_info_unreachable() -> None:
    server = start_object_info_server("ok")
    port = server.server_address[1]
    server.shutdown()
    server.server_close()
    with pytest.raises(ConnectionError):
        fetch_object_info(f"http://127.0.0.1:{port}")


def _node(node_id: int, node_type: str, **kw: Any) -> dict:
    node: dict = {"id": node_id, "type": node_type, "mode": 0}
    node.update(kw)
    return node


def _vae_node(node_id: int, value: str) -> dict:
    # VAELoader's single required input (vae_name, no default) keeps
    # fixtures completeness-safe: no linked clip needed.
    return _node(
        node_id,
        "VAELoader",
        inputs=[{"name": "vae_name", "type": "COMBO",
                 "widget": {"name": "vae_name"}, "link": None}],
        widgets_values=[value],
    )


def test_unconnected_node_ids() -> None:
    nodes = [
        _node(1, "KSampler"),
        _node(58, "Fast Groups Bypasser (rgthree)"),
        _node(9, "Note"),
    ]
    links = [[10, 1, 0, 2, 0, "MODEL"]]
    assert unconnected_node_ids(nodes, links) == ["58", "9"]
    assert unconnected_node_ids(nodes, []) == ["1", "58", "9"]
    assert unconnected_node_ids("junk", links) == []
    assert unconnected_node_ids(nodes, "junk") == ["1", "58", "9"]


def test_unconnected_unknown_type_dropped_quietly() -> None:
    # Node 3 is link-touched (table-only entry); node 58 is not.
    canvas = {
        "nodes": [
            _vae_node(3, "hi.safetensors"),
            _node(58, "Fast Groups Bypasser (rgthree)", inputs=[],
                   outputs=[{"name": "OPT_CONNECTION", "type": "*",
                             "links": None}]),
        ],
        "links": [link(10, 3, 0, 3, 0)],
    }
    prompt = canvas_to_prompt(canvas, mini_specs())
    assert set(prompt) == {"3"}
    assert prompt["3"]["inputs"]["vae_name"] == "hi.safetensors"


def test_unconnected_known_type_dropped() -> None:
    canvas = {
        "nodes": [
            _vae_node(3, "hi.safetensors"),
            _vae_node(7, "stray.safetensors"),
        ],
        "links": [link(10, 3, 0, 3, 0)],
    }
    prompt = canvas_to_prompt(canvas, mini_specs())
    assert set(prompt) == {"3"}


def test_connected_unknown_type_still_loud() -> None:
    canvas = {
        "nodes": [
            _node(
                4,
                "Mystery Node (gone)",
                inputs=[{"name": "text", "type": "STRING", "link": 10,
                         "widget": {"name": "text"}}],
                widgets_values=["x"],
            ),
            _vae_node(2, "x.safetensors"),
        ],
        "links": [[10, 4, 0, 2, 0]],
    }
    with pytest.raises(ValueError, match="Mystery Node"):
        canvas_to_prompt(canvas, mini_specs())


def test_stale_scalar_value_skipped_not_shifted() -> None:
    # KSampler saved by another ComfyUI version carries a stale
    # control_after_generate value ("randomize") with no canvas input
    # and no spec entry: steps must still read 10, not "randomize".
    canvas = {
        "nodes": [
            {"id": 4, "type": "KSampler", "mode": 0,
             "inputs": [
                 {"name": "seed", "type": "INT",
                  "widget": {"name": "seed"}, "link": None},
                 {"name": "steps", "type": "INT",
                  "widget": {"name": "steps"}, "link": None},
             ],
             "outputs": [],
             "widgets_values": [235398267971802, "randomize", 10]},
        ],
        "links": [link(3, 4, 0, 4, 0)],
    }
    specs = {
        "KSampler": {
            "input": {
                "required": {
                    "seed": ["INT", {"default": 0}],
                    "steps": ["INT", {"default": 20}],
                }
            }
        }
    }
    prompt = canvas_to_prompt(canvas, specs)
    assert prompt["4"]["inputs"]["seed"] == 235398267971802
    assert prompt["4"]["inputs"]["steps"] == 10


def test_typedef_compat_matrix() -> None:
    from services.canvas import _typedef_compat

    assert _typedef_compat(["INT", {}], 3)
    assert not _typedef_compat(["INT", {}], "3")
    assert not _typedef_compat(["INT", {}], True)
    assert _typedef_compat(["FLOAT", {}], 1)
    assert _typedef_compat(["FLOAT", {}], 1.5)
    assert not _typedef_compat(["FLOAT", {}], "x")
    assert _typedef_compat(["STRING", {}], "hi")
    assert not _typedef_compat(["STRING", {}], 3)
    assert _typedef_compat([["a", "b"], {}], "a")
    assert not _typedef_compat([["a", "b"], {}], "z")
    assert _typedef_compat([[], {}], "anything")
    assert _typedef_compat("junk", "anything")
    assert _typedef_compat(["MODEL", {}], object())
