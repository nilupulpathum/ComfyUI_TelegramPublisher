"""T080/T081 trigger prompt override + publish auto-append via /run.

Loopback-only: a fake ComfyUI ``http.server`` thread (ephemeral port)
serves ``/object_info`` (mini spec) and ``/prompt``. ``TEST_TOKEN_xxx``
fakes only; no real ComfyUI/Telegram calls.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from typing import Any

from services.commands import (
    BotContext,
    CommandRouter,
    IncomingCommand,
    register_builtins,
    register_review,
)
from storage.config import (
    Account,
    BotSettings,
    ConfigStore,
    Destination,
    FileSecretStore,
    PromptTarget,
    PublishSpec,
    Trigger,
)
from storage.database import Database
from storage.repositories import GenerationRepository, PublishJobRepository
from telegram.models import SendMessageResult, SendPhotoResult

FAKE_TOKEN = "TEST_TOKEN_xxx"


def make_stores(tmp_path: Path):
    config = ConfigStore(tmp_path / "config.json")
    secrets = FileSecretStore(tmp_path / "secrets.json")
    config.add_account(Account(id="acc1", name="Main bot"))
    config.add_destination(
        Destination(id="dst1", name="Channel", account_id="acc1",
                    chat_id="-100123")
    )
    secrets.set_token("acc1", FAKE_TOKEN)
    return config, secrets


def make_repos(tmp_path: Path, name: str = "history.sqlite3"):
    db = Database(tmp_path / name)
    db.init_schema()
    return PublishJobRepository(db), GenerationRepository(db)


class FakeCmdClient:
    def send_photo(self, chat_id: str, image_bytes: bytes, filename: str,
                   caption: Any = None, **kwargs: Any) -> SendPhotoResult:
        return SendPhotoResult(message_id=77, chat_id=chat_id)

    def send_message(self, chat_id: str, text: str, **kwargs: Any) -> Any:
        return SendMessageResult(message_id=1, chat_id=chat_id)

    def get_updates(self, *args: Any, **kwargs: Any) -> list:
        return []


def make_router() -> CommandRouter:
    return register_review(register_builtins(CommandRouter()))


def cmd(name: str, args: list[str], chat_id: str = "111") -> IncomingCommand:
    return IncomingCommand(
        name=name, args=args, chat_id=chat_id, from_id="9", update_id=1
    )


def run_ctx(tmp_path: Path, triggers: tuple, port: int) -> BotContext:
    config, secrets = make_stores(tmp_path)
    jobs, _ = make_repos(tmp_path)
    settings = BotSettings(
        admin_chat_ids=("111",),
        comfy_host="127.0.0.1",
        comfy_port=port,
        triggers=triggers,
    )
    return BotContext(
        client=FakeCmdClient(), config=config, secrets=secrets, jobs=jobs,
        generations=None, queue=None, settings=settings,
        review_dir=tmp_path / "review",
    )


# ---------------------------------------------------------------------------
# Fake ComfyUI: /object_info + /prompt on loopback
# ---------------------------------------------------------------------------


MINI_SPECS = {
    "CLIPTextEncode": {
        "input": {
            "required": {
                "clip": ["CLIP", {}],
                "text": ["STRING", {"multiline": True, "default": ""}],
            }
        }
    },
    "VAELoader": {
        "input": {
            "required": {"vae_name": ["COMBO", {"default": "d.safetensors"}]}
        }
    },
    "KSampler": {
        "input": {
            "required": {
                "model": ["MODEL", {}],
                "seed": ["INT", {"default": 0}],
            }
        }
    },
    "VAEDecode": {
        "input": {
            "required": {"samples": ["LATENT", {}], "vae": ["VAE", {}]}
        }
    },
}


def start_fake_comfy():
    received: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/object_info":
                data = json.dumps(MINI_SPECS).encode()
                self.send_response(200)
            else:
                data = json.dumps({"error": "nope"}).encode()
                self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            received.append({"path": self.path, "body": body})
            data = json.dumps({"prompt_id": "pfx-123"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, received


def api_file(tmp_path: Path, name: str = "wf.json") -> str:
    payload = {
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["30", 1], "text": "file prompt here"}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["4", 0], "vae": ["15", 0]}},
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def anima_like_canvas_file(tmp_path: Path) -> str:
    # Grounded in the real Anima shapes: Prompt (LoraManager) node 28
    # with dict UI state in widgets_values; KSampler; VAEDecode 6.
    payload = {
        "nodes": [
            {"id": 30, "type": "VAELoader", "mode": 0,
             "inputs": [{"name": "vae_name", "type": "COMBO",
                         "widget": {"name": "vae_name"}, "link": None}],
             "outputs": [{"name": "VAE", "type": "VAE"}],
             "widgets_values": ["qwen_image_vae.safetensors"]},
            {"id": 28, "type": "CLIPTextEncode", "mode": 0,
             "inputs": [
                 {"name": "clip", "type": "CLIP", "link": 31},
                 {"name": "text", "type": "STRING",
                  "widget": {"name": "text"}, "link": None},
             ],
             "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}],
             "widgets_values": ["file prompt here"]},
            {"id": 4, "type": "KSampler", "mode": 0,
             "inputs": [
                 {"name": "model", "type": "MODEL", "link": 36},
                 {"name": "seed", "type": "INT",
                  "widget": {"name": "seed"}, "link": None},
             ],
             "outputs": [{"name": "LATENT", "type": "LATENT"}],
             "widgets_values": [42]},
            {"id": 6, "type": "VAEDecode", "mode": 0,
             "inputs": [
                 {"name": "samples", "type": "LATENT", "link": 8},
                 {"name": "vae", "type": "VAE", "link": 30},
             ],
             "outputs": [{"name": "IMAGE", "type": "IMAGE"}]},
            {"id": 14, "type": "Note", "mode": 0,
             "inputs": [], "outputs": [],
             "widgets_values": ["annotation"]},
            {"id": 22, "type": "Label (rgthree)", "mode": 0,
             "inputs": [], "outputs": []},
        ],
        "links": [
            [31, 30, 1, 28, 0, "CLIP"],
            [36, 30, 0, 4, 0, "MODEL"],
            [8, 4, 0, 6, 0, "LATENT"],
            [30, 30, 0, 6, 1, "VAE"],
        ],
    }
    path = tmp_path / "anima.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def trigger_for(prompt_file: str, **over: Any) -> Trigger:
    base: dict[str, Any] = {"name": "wf", "prompt_file": prompt_file}
    base.update(over)
    return Trigger(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T081 override semantics
# ---------------------------------------------------------------------------


def test_override_happy_path(tmp_path: Path) -> None:
    server, received = start_fake_comfy()
    try:
        port = server.server_address[1]
        ctx = run_ctx(
            tmp_path,
            (trigger_for(api_file(tmp_path),
                         prompt_targets=[PromptTarget(node="3")]),),
            port,
        )
        reply = make_router().handle(ctx, cmd("run", ["wf", "a", "cat"]))
        assert reply == "workflow started: pfx-123"
        body = json.loads(received[0]["body"].decode("utf-8"))
        assert body["prompt"]["3"]["inputs"]["text"] == "a cat"
    finally:
        server.shutdown()
        server.server_close()


def test_override_linked_target_refused(tmp_path: Path) -> None:
    server, received = start_fake_comfy()
    try:
        port = server.server_address[1]
        ctx = run_ctx(
            tmp_path,
            (trigger_for(api_file(tmp_path),
                         prompt_targets=[PromptTarget(node="3",
                                                      input="clip")]),),
            port,
        )
        reply = make_router().handle(ctx, cmd("run", ["wf", "hello"]))
        assert reply.startswith("trigger misconfigured")
        assert "not overridable (linked?)" in reply
        assert received == []
    finally:
        server.shutdown()
        server.server_close()


def test_override_missing_node_and_input(tmp_path: Path) -> None:
    server, _ = start_fake_comfy()
    try:
        port = server.server_address[1]
        for index, target in enumerate((PromptTarget(node="999"),
                                        PromptTarget(node="3",
                                                     input="nope"))):
            home = tmp_path / f"case{index}"
            home.mkdir()
            ctx = run_ctx(
                home, (trigger_for(api_file(home),
                                   prompt_targets=[target]),), port,
            )
            reply = make_router().handle(ctx, cmd("run", ["wf", "hi"]))
            assert reply.startswith("trigger misconfigured"), reply
    finally:
        server.shutdown()
        server.server_close()


def test_no_targets_with_text_refused(tmp_path: Path) -> None:
    server, received = start_fake_comfy()
    try:
        port = server.server_address[1]
        ctx = run_ctx(tmp_path, (trigger_for(api_file(tmp_path)),), port)
        reply = make_router().handle(ctx, cmd("run", ["wf", "extra"]))
        assert reply.startswith("usage: /run wf [text]")
        assert "takes no prompt text" in reply
        assert received == []  # nothing posted
        # Same trigger without text works and keeps file text.
        reply = make_router().handle(ctx, cmd("run", ["wf"]))
        assert reply == "workflow started: pfx-123"
        body = json.loads(received[0]["body"].decode("utf-8"))
        assert body["prompt"]["3"]["inputs"]["text"] == "file prompt here"
    finally:
        server.shutdown()
        server.server_close()


def test_prompt_required_usage(tmp_path: Path) -> None:
    server, received = start_fake_comfy()
    try:
        port = server.server_address[1]
        ctx = run_ctx(
            tmp_path,
            (trigger_for(api_file(tmp_path),
                         prompt_targets=[PromptTarget(node="3")],
                         prompt_required=True),),
            port,
        )
        assert make_router().handle(ctx, cmd("run", ["wf"])) == (
            "usage: /run wf <text>"
        )
        assert received == []
        assert make_router().handle(ctx, cmd("run", ["wf", "x"])) == (
            "workflow started: pfx-123"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_text_cap(tmp_path: Path) -> None:
    server, received = start_fake_comfy()
    try:
        port = server.server_address[1]
        ctx = run_ctx(
            tmp_path,
            (trigger_for(api_file(tmp_path),
                         prompt_targets=[PromptTarget(node="3")]),),
            port,
        )
        reply = make_router().handle(ctx, cmd("run", ["wf", "x" * 1501]))
        assert reply.startswith("prompt too long")
        assert received == []
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# T080 publish auto-append
# ---------------------------------------------------------------------------


def test_publish_auto_detect_and_caption(tmp_path: Path) -> None:
    server, received = start_fake_comfy()
    try:
        port = server.server_address[1]
        ctx = run_ctx(
            tmp_path,
            (trigger_for(
                api_file(tmp_path),
                prompt_targets=[PromptTarget(node="3")],
                publish=PublishSpec(account="acc1", destination="dst1"),
            ),),
            port,
        )
        reply = make_router().handle(ctx, cmd("run", ["wf", "a", "cat"]))
        assert reply == "workflow started: pfx-123"
        body = json.loads(received[0]["body"].decode("utf-8"))
        prompt = body["prompt"]
        assert prompt["3"]["inputs"]["text"] == "a cat"
        # Auto-detect: first VAEDecode is node 6 -> new id "7".
        assert prompt["7"]["class_type"] == "Telegram Send Image"
        assert prompt["7"]["inputs"]["image"] == ["6", 0]
        # Default caption template {{prompt}} renders the override text.
        assert prompt["7"]["inputs"]["caption"] == "a cat"
        assert prompt["7"]["inputs"]["wait_for_upload"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_publish_caption_falls_back_to_file_text(tmp_path: Path) -> None:
    server, received = start_fake_comfy()
    try:
        port = server.server_address[1]
        ctx = run_ctx(
            tmp_path,
            (trigger_for(
                api_file(tmp_path),
                prompt_targets=[PromptTarget(node="3")],
                publish=PublishSpec(account="acc1", destination="dst1"),
            ),),
            port,
        )
        assert make_router().handle(ctx, cmd("run", ["wf"])) == (
            "workflow started: pfx-123"
        )
        body = json.loads(received[0]["body"].decode("utf-8"))
        assert body["prompt"]["7"]["inputs"]["caption"] == "file prompt here"
    finally:
        server.shutdown()
        server.server_close()


def test_publish_explicit_source(tmp_path: Path) -> None:
    server, received = start_fake_comfy()
    try:
        port = server.server_address[1]
        ctx = run_ctx(
            tmp_path,
            (trigger_for(
                api_file(tmp_path),
                publish=PublishSpec(account="acc1", destination="dst1",
                                    source="6:0"),
            ),),
            port,
        )
        assert make_router().handle(ctx, cmd("run", ["wf"])) == (
            "workflow started: pfx-123"
        )
        body = json.loads(received[0]["body"].decode("utf-8"))
        assert body["prompt"]["7"]["inputs"]["image"] == ["6", 0]
    finally:
        server.shutdown()
        server.server_close()


def test_publish_missing_vae_refused(tmp_path: Path) -> None:
    payload = {"3": {"class_type": "CLIPTextEncode",
                     "inputs": {"clip": ["30", 1], "text": "x"}}}
    path = tmp_path / "novae.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    server, received = start_fake_comfy()
    try:
        port = server.server_address[1]
        ctx = run_ctx(
            tmp_path,
            (trigger_for(
                str(path),
                publish=PublishSpec(account="acc1", destination="dst1"),
            ),),
            port,
        )
        reply = make_router().handle(ctx, cmd("run", ["wf"]))
        assert reply.startswith("trigger misconfigured")
        assert "VAEDecode" in reply
        assert received == []
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Full canvas /run (Anima-like): object_info + prompt through one server
# ---------------------------------------------------------------------------


def test_run_canvas_end_to_end(tmp_path: Path) -> None:
    server, received = start_fake_comfy()
    try:
        port = server.server_address[1]
        ctx = run_ctx(
            tmp_path,
            (trigger_for(
                anima_like_canvas_file(tmp_path),
                prompt_targets=[PromptTarget(node="28")],
                publish=PublishSpec(account="acc1", destination="dst1"),
            ),),
            port,
        )
        reply = make_router().handle(
            ctx, cmd("run", ["wf", "anima", "test"]))
        assert reply == "workflow started: pfx-123"
        assert len(received) == 1
        assert received[0]["path"] == "/prompt"
        prompt = json.loads(received[0]["body"].decode("utf-8"))["prompt"]
        # Converted: canvas ids kept, annotations dropped, text overridden.
        assert prompt["28"]["inputs"]["text"] == "anima test"
        assert prompt["28"]["class_type"] == "CLIPTextEncode"
        assert "14" not in prompt and "22" not in prompt
        # Publish node appended from auto-detected VAEDecode 6.
        added = [k for k in prompt if k not in ("30", "28", "4", "6")]
        assert len(added) == 1
        node = prompt[added[0]]
        assert node["inputs"]["image"] == ["6", 0]
        assert node["inputs"]["caption"] == "anima test"
    finally:
        server.shutdown()
        server.server_close()


def test_run_canvas_conversion_failure_is_misconfigured(
    tmp_path: Path,
) -> None:
    # Dangling link: kept node points at a bypassed node -> loud refusal.
    payload = {
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
        "links": [[5, 2, 0, 1, 0, "LATENT"]],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    server, received = start_fake_comfy()
    try:
        port = server.server_address[1]
        ctx = run_ctx(tmp_path, (trigger_for(str(path)),), port)
        reply = make_router().handle(ctx, cmd("run", ["wf"]))
        assert reply.startswith("trigger misconfigured"), reply
        assert received == []
    finally:
        server.shutdown()
        server.server_close()
