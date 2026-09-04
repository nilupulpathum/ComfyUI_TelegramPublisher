"""Unit tests for services.metadata (backlog T032). Pure stdlib."""

from datetime import datetime

from services.metadata import GenerationMetadata

ALL_VARS = (
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
)


def test_from_mapping_happy_path() -> None:
    meta = GenerationMetadata.from_mapping(
        {
            "prompt": "a cat",
            "negative_prompt": "blurry",
            "seed": "42",
            "steps": "20",
            "cfg": "7.5",
            "sampler": "euler",
            "scheduler": "normal",
            "model": "sd15.safetensors",
            "width": 512,
            "height": 768,
            "filename": "out.png",
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
    )
    assert meta.prompt == "a cat"
    assert meta.negative_prompt == "blurry"
    assert meta.seed == "42"
    assert meta.steps == "20"
    assert meta.cfg == "7.5"
    assert meta.sampler == "euler"
    assert meta.scheduler == "normal"
    assert meta.model == "sd15.safetensors"
    assert meta.width == 512
    assert meta.height == 768
    assert meta.filename == "out.png"
    assert meta.timestamp == "2026-01-01T00:00:00+00:00"


def test_from_mapping_none_source() -> None:
    meta = GenerationMetadata.from_mapping(None)
    assert meta.prompt is None
    assert meta.negative_prompt is None
    assert meta.seed is None
    assert meta.steps is None
    assert meta.cfg is None
    assert meta.sampler is None
    assert meta.scheduler is None
    assert meta.model is None
    assert meta.width is None
    assert meta.height is None
    assert meta.filename is None
    # Timestamp still defaults to now.
    assert isinstance(meta.timestamp, str) and meta.timestamp != ""
    datetime.fromisoformat(meta.timestamp)


def test_from_mapping_empty_mapping() -> None:
    meta = GenerationMetadata.from_mapping({})
    assert meta.prompt is None
    assert meta.width is None
    assert isinstance(meta.timestamp, str) and meta.timestamp != ""


def test_from_mapping_ignores_unknown_keys() -> None:
    meta = GenerationMetadata.from_mapping(
        {"prompt": "hi", "bogus": "x", "lora": "y", "width": 64}
    )
    assert meta.prompt == "hi"
    assert meta.width == 64
    assert not hasattr(meta, "bogus")
    assert not hasattr(meta, "lora")


def test_from_mapping_numeric_stringification() -> None:
    meta = GenerationMetadata.from_mapping(
        {"seed": 42, "steps": 20, "cfg": 7.5, "sampler": "euler"}
    )
    assert meta.seed == "42"
    assert meta.steps == "20"
    assert meta.cfg == "7.5"
    assert meta.sampler == "euler"


def test_from_mapping_none_and_empty_string_become_none() -> None:
    meta = GenerationMetadata.from_mapping(
        {"prompt": None, "negative_prompt": "", "seed": 0, "steps": ""}
    )
    assert meta.prompt is None
    assert meta.negative_prompt is None
    # Numeric zero is a real value, not missing.
    assert meta.seed == "0"
    assert meta.steps is None


def test_from_mapping_width_height_ints() -> None:
    meta = GenerationMetadata.from_mapping({"width": 512, "height": 768})
    assert meta.width == 512 and isinstance(meta.width, int)
    assert meta.height == 768 and isinstance(meta.height, int)

    coerced = GenerationMetadata.from_mapping({"width": "1024", "height": "512"})
    assert coerced.width == 1024
    assert coerced.height == 512

    bad = GenerationMetadata.from_mapping(
        {"width": "wide", "height": None, "prompt": "x"}
    )
    assert bad.width is None
    assert bad.height is None
    assert bad.prompt == "x"


def test_from_mapping_never_raises_on_odd_input() -> None:
    meta = GenerationMetadata.from_mapping(
        {
            "prompt": {"nested": ["objects"]},
            "seed": ["a", "list"],
            "width": {"not": "a number"},
            "height": [1, 2],
            "steps": object(),
        }
    )
    assert isinstance(meta.prompt, str)
    assert isinstance(meta.seed, str)
    assert meta.width is None
    assert meta.height is None
    assert isinstance(meta.steps, str)


def test_from_mapping_explicit_timestamp_none_stays_none() -> None:
    meta = GenerationMetadata.from_mapping({"prompt": "x", "timestamp": None})
    assert meta.prompt == "x"
    assert meta.timestamp is None


def test_timestamp_defaults_to_now_iso8601() -> None:
    before = datetime.now().astimezone()
    meta = GenerationMetadata()
    after = datetime.now().astimezone()
    assert isinstance(meta.timestamp, str)
    parsed = datetime.fromisoformat(meta.timestamp)
    assert before <= parsed <= after


def test_as_template_vars_completeness() -> None:
    meta = GenerationMetadata.from_mapping(
        {
            "prompt": "a cat",
            "negative_prompt": "blurry",
            "seed": "42",
            "steps": "20",
            "cfg": "7",
            "sampler": "euler",
            "scheduler": "normal",
            "model": "m.safetensors",
            "width": 512,
            "height": 768,
            "filename": "out.png",
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
    )
    variables = meta.as_template_vars()
    assert set(variables) == set(ALL_VARS)
    assert len(variables) == 12
    assert variables["prompt"] == "a cat"
    assert variables["width"] == "512"
    assert variables["height"] == "768"


def test_as_template_vars_none_becomes_empty_string() -> None:
    variables = GenerationMetadata.from_mapping(None).as_template_vars()
    assert set(variables) == set(ALL_VARS)
    for key in ALL_VARS:
        if key == "timestamp":
            continue
        assert variables[key] == "", key
    # No value is ever None.
    assert all(isinstance(value, str) for value in variables.values())
