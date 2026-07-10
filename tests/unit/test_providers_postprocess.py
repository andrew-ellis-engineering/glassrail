"""Tests for provider output post-processing."""

from __future__ import annotations

from glassrail.providers import strip_model_output


def test_strip_model_output_removes_multiline_think_block() -> None:
    raw = """
<think>
I should reason before answering.
</think>
{"answer": 42}
"""

    assert strip_model_output(raw) == '{"answer": 42}'


def test_strip_model_output_unwraps_top_level_json_fence() -> None:
    raw = """```json
{"answer": 42}
```"""

    assert strip_model_output(raw) == '{"answer": 42}'


def test_strip_model_output_unwraps_top_level_plain_fence() -> None:
    raw = """```
{"answer": 42}
```"""

    assert strip_model_output(raw) == '{"answer": 42}'


def test_strip_model_output_leaves_fenceless_text_unchanged() -> None:
    assert strip_model_output('{"answer": 42}') == '{"answer": 42}'


def test_strip_model_output_leaves_mid_string_fence_unchanged() -> None:
    raw = '{"text": "prefix ```json\\nnot a wrapper\\n``` suffix"}'

    assert strip_model_output(raw) == raw


def test_strip_model_output_handles_empty_string() -> None:
    assert strip_model_output("") == ""
