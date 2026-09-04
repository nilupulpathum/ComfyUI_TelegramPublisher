"""Unit tests for services.captions (backlog T033). Pure stdlib."""

import pytest

from services.captions import MAX_CAPTION_LENGTH, RenderedCaption, render_caption

ALL_VARS = {
    "prompt": "a cat",
    "negative_prompt": "blurry",
    "seed": "42",
    "steps": "20",
    "cfg": "7",
    "sampler": "euler",
    "scheduler": "normal",
    "model": "m.safetensors",
    "width": "512",
    "height": "768",
    "filename": "out.png",
    "timestamp": "2026-01-01T00:00:00+00:00",
}


def test_all_twelve_vars_render_without_warnings() -> None:
    template = "\n".join(f"{name}: {{{{{name}}}}}" for name in ALL_VARS)
    result = render_caption(template, dict(ALL_VARS))
    assert isinstance(result, RenderedCaption)
    assert result.warnings == []
    for name, value in ALL_VARS.items():
        assert f"{name}: {value}" in result.text


def test_unknown_var_renders_empty_with_warning() -> None:
    result = render_caption("hello {{nope}} world", dict(ALL_VARS))
    assert result.text == "hello  world"
    assert len(result.warnings) == 1
    assert "nope" in result.warnings[0]


def test_missing_none_var_warns() -> None:
    variables = dict(ALL_VARS)
    del variables["seed"]
    variables["model"] = None
    result = render_caption("{{seed}}/{{model}}/{{prompt}}", variables)
    assert result.text == "//a cat"
    assert len(result.warnings) == 2
    assert any("seed" in warning for warning in result.warnings)
    assert any("model" in warning for warning in result.warnings)


def test_empty_string_var_warns() -> None:
    variables = dict(ALL_VARS, prompt="")
    result = render_caption("[{{prompt}}]", variables)
    assert result.text == "[]"
    assert len(result.warnings) == 1
    assert "prompt" in result.warnings[0]


def test_whitespace_inside_braces_tolerated() -> None:
    result = render_caption("[{{ seed }}|{{  prompt}}|{{cfg  }}]", dict(ALL_VARS))
    assert result.text == "[42|a cat|7]"
    assert result.warnings == []


def test_non_string_values_stringified() -> None:
    result = render_caption("{{seed}} {{width}}x{{height}}", {"seed": 42, "width": 512, "height": 768})
    assert result.text == "42 512x768"
    assert result.warnings == []


def test_numeric_zero_is_not_missing() -> None:
    result = render_caption("seed={{seed}}", {"seed": 0})
    assert result.text == "seed=0"
    assert result.warnings == []


def test_none_template_renders_empty_without_warnings() -> None:
    assert render_caption(None, dict(ALL_VARS)) == RenderedCaption(text="", warnings=[])


def test_empty_template_renders_empty_without_warnings() -> None:
    assert render_caption("", dict(ALL_VARS)) == RenderedCaption(text="", warnings=[])


def test_multiline_template_preserved() -> None:
    template = "line1 {{prompt}}\nline2 {{seed}}\n\nline4"
    result = render_caption(template, dict(ALL_VARS))
    assert result.text == "line1 a cat\nline2 42\n\nline4"
    assert result.warnings == []


def test_overlong_caption_truncated_with_warning() -> None:
    template = "{{prompt}}"
    variables = {"prompt": "x" * (MAX_CAPTION_LENGTH + 100)}
    result = render_caption(template, variables)
    assert len(result.text) == MAX_CAPTION_LENGTH
    assert result.text == "x" * MAX_CAPTION_LENGTH
    assert any("truncat" in warning for warning in result.warnings)


def test_caption_at_limit_not_truncated() -> None:
    variables = {"prompt": "y" * MAX_CAPTION_LENGTH}
    result = render_caption("{{prompt}}", variables)
    assert len(result.text) == MAX_CAPTION_LENGTH
    assert result.warnings == []


def test_max_caption_length_is_telegram_limit() -> None:
    assert MAX_CAPTION_LENGTH == 1024


@pytest.mark.parametrize("bad", [123, 4.5, True, ["{{prompt}}"], {"t": 1}, b"{{prompt}}"])
def test_non_string_template_raises_value_error(bad: object) -> None:
    with pytest.raises(ValueError):
        render_caption(bad, dict(ALL_VARS))  # type: ignore[arg-type]
