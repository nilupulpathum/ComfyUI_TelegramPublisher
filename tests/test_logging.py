"""T023 tests: structured logging with secret redaction. Fake only, no network."""

from __future__ import annotations

import logging
import socket
import urllib.request
from pathlib import Path

import numpy as np
import pytest

import telegram.logging as tlog
from publisher_nodes.send_image import TelegramSendImage
from services.retry import RetryPolicy
from storage.config import Account, ConfigStore, Destination, FileSecretStore
from telegram.client import TelegramClient
from telegram.logging import RedactingFilter, get_logger

FAKE_TOKEN = "TEST_TOKEN_xxx"
# Synthetic token-shaped value (fake only) matching errors._TOKEN_RE.
LEAK_SHAPE = "999888:" + "B" * 35
ACCOUNT_ID = "acc1"
DEST_ID = "dst1"
CHAT_ID = "-100123"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("tests must not make real network calls")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


def _enable(caplog, logger_name: str):
    logger = logging.getLogger(logger_name)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=logger_name)
    return old_level


# -- filter unit tests -----------------------------------------------------


def test_filter_redacts_token_shape_in_message():
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, f"oops {LEAK_SHAPE}", (), None)
    assert RedactingFilter().filter(rec) is True
    assert LEAK_SHAPE not in rec.getMessage()
    assert "***" in rec.getMessage()


def test_filter_redacts_bot_url_shape():
    url = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, f"calling {url}", (), None)
    assert RedactingFilter().filter(rec) is True
    assert FAKE_TOKEN not in rec.getMessage()
    assert "/bot***" in rec.getMessage()


def test_filter_redacts_args_percent_formatting():
    rec = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "token is %s", (LEAK_SHAPE,), None
    )
    assert RedactingFilter().filter(rec) is True
    assert LEAK_SHAPE not in rec.getMessage()


def test_filter_redacts_bot_url_in_args():
    url = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendPhoto"
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "url=%s", (url,), None)
    assert RedactingFilter().filter(rec) is True
    assert FAKE_TOKEN not in rec.getMessage()


def test_filter_never_raises_on_weird_records():
    f = RedactingFilter()
    weird = [
        logging.LogRecord("t", logging.INFO, __file__, 1, None, (), None),
        logging.LogRecord("t", logging.INFO, __file__, 1, 12345, (), None),
        logging.LogRecord("t", logging.INFO, __file__, 1, "msg", (object(),), None),
        logging.LogRecord("t", logging.INFO, __file__, 1, "msg %s %s", ("a",), None),
    ]
    dict_rec = logging.LogRecord("t", logging.INFO, __file__, 1, "msg %(k)s", (), None)
    dict_rec.args = {"k": LEAK_SHAPE}
    weird.append(dict_rec)
    for rec in weird:
        assert f.filter(rec) is True


def test_get_logger_idempotent_single_filter():
    name = "test.t023.idempotent.logger"
    logger = logging.getLogger(name)
    logger.filters = [fl for fl in logger.filters if not isinstance(fl, RedactingFilter)]
    a = get_logger(name)
    b = get_logger(name)
    assert a is b
    count = sum(1 for fl in a.filters if isinstance(fl, RedactingFilter))
    assert count == 1
    assert len(a.handlers) == 0


def test_get_logger_attaches_no_handlers():
    logger = get_logger("test.t023.no.handlers")
    assert len(logger.handlers) == 0


# -- client logging --------------------------------------------------------


def _ok_transport(url_check_token=FAKE_TOKEN):
    def fake(url, *, files, data, timeout):
        assert url_check_token in url
        return 200, {"ok": True, "result": {"message_id": 5, "chat": {"id": "@chan"}}}

    return fake


def test_client_success_logs_attempt_and_success_without_token_or_url(caplog):
    _enable(caplog, "telegram.client")
    caplog.clear()
    client = TelegramClient(FAKE_TOKEN, transport=_ok_transport())
    client.send_message("@chan", "hi")
    texts = [r.getMessage() for r in caplog.records]
    assert any("attempt" in t and "sendMessage" in t for t in texts)
    assert any("succeeded" in t or "success" in t for t in texts)
    for t in texts:
        assert FAKE_TOKEN not in t
        assert LEAK_SHAPE not in t
        assert "https://" not in t
        assert "api.telegram.org" not in t
    # method-only: attempt line carries the method name
    assert any("sendMessage" in t for t in texts)


def test_client_failure_logs_warning_without_token_or_url(caplog):
    _enable(caplog, "telegram.client")
    caplog.clear()

    def bad(url, *, files, data, timeout):
        return 400, {"ok": False, "error_code": 400, "description": "bad chat"}

    client = TelegramClient(FAKE_TOKEN, transport=bad)
    with pytest.raises(Exception):
        client.send_message("@chan", "hi")
    texts = [r.getMessage() for r in caplog.records]
    assert any(r.levelno >= logging.WARNING and "sendMessage" in r.getMessage() for r in caplog.records)
    for t in texts:
        assert FAKE_TOKEN not in t
        assert "https://" not in t
        assert "api.telegram.org" not in t


def test_client_retry_hook_logs_attempt_numbers(caplog):
    _enable(caplog, "telegram.client")
    caplog.clear()
    sleeps: list = []
    script = [
        (500, {"ok": False, "error_code": 500, "description": "boom1"}),
        (200, {"ok": True, "result": {"message_id": 7, "chat": {"id": "@c"}}}),
    ]
    state = {"n": 0}

    def fake(url, *, files, data, timeout):
        item = script[min(state["n"], len(script) - 1)]
        state["n"] += 1
        return item

    client = TelegramClient(
        FAKE_TOKEN,
        transport=fake,
        retry_policy=RetryPolicy(
            max_attempts=3, base_delay=1.0, jitter=0.0, sleep=sleeps.append
        ),
    )
    res = client.send_message("@c", "hi")
    assert res.message_id == 7
    assert len(sleeps) == 1
    retry_texts = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "attempt" in r.getMessage().lower()
    ]
    assert retry_texts, "expected a retry WARNING record with attempt number"
    assert any("1" in t for t in retry_texts)
    for t in retry_texts:
        assert FAKE_TOKEN not in t
        assert "https://" not in t


# -- node logging ----------------------------------------------------------


def _make_node(tmp_path: Path, transport):
    config = ConfigStore(tmp_path / "config.json")
    secrets = FileSecretStore(tmp_path / "secrets.json")
    config.add_account(Account(id=ACCOUNT_ID, name="Main bot"))
    config.add_destination(
        Destination(id=DEST_ID, name="Channel", account_id=ACCOUNT_ID, chat_id=CHAT_ID)
    )
    secrets.set_token(ACCOUNT_ID, FAKE_TOKEN)

    def factory(token: str) -> TelegramClient:
        assert token == FAKE_TOKEN
        return TelegramClient(token, transport=transport)

    return TelegramSendImage(config_store=config, secret_store=secrets, client_factory=factory)


def _frame() -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.random((8, 8, 3)).astype(np.float32)


def test_node_start_success_contain_ids_not_caption_or_token(tmp_path, caplog):
    _enable(caplog, "publisher_nodes.send_image")
    caplog.clear()

    def ok(url, *, files, data, timeout):
        return 200, {"ok": True, "result": {"message_id": 42, "chat": {"id": -100123}}}

    node = _make_node(tmp_path, ok)
    caption = "SECRET_CAPTION_XYZ_123"
    (out,) = node.publish(
        np.stack([_frame()]), ACCOUNT_ID, DEST_ID, caption=caption, format="png", quality=90
    )
    assert out is not None
    texts = [r.getMessage() for r in caplog.records]
    assert any("start" in t.lower() for t in texts)
    assert any("success" in t.lower() for t in texts)
    start = next(t for t in texts if "start" in t.lower())
    assert ACCOUNT_ID in start and DEST_ID in start
    for t in texts:
        assert caption not in t
        assert FAKE_TOKEN not in t
        assert "https://" not in t


def test_node_failure_contains_ids_not_caption_or_token(tmp_path, caplog):
    _enable(caplog, "publisher_nodes.send_image")
    caplog.clear()

    def bad(url, *, files, data, timeout):
        return 400, {"ok": False, "error_code": 400, "description": "bad chat"}

    node = _make_node(tmp_path, bad)
    caption = "SECRET_CAPTION_FAIL_456"
    with pytest.raises(RuntimeError):
        node.publish(
            np.stack([_frame()]), ACCOUNT_ID, DEST_ID, caption=caption, format="png", quality=90
        )
    texts = [r.getMessage() for r in caplog.records]
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    err = next(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert ACCOUNT_ID in err and DEST_ID in err
    for t in texts:
        assert caption not in t
        assert FAKE_TOKEN not in t
