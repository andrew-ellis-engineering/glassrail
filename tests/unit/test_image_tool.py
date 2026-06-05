"""Unit tests for the image_generate tool integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dagagent.config.settings import ImageToolConfig
from dagagent.core import ToolExecutionError, ToolRegistrationError
from dagagent.harness import ToolHarness
from dagagent.harness.integrations.image import image_generate, register_image

# ── Registration tests ────────────────────────────────────────────────────────


def test_register_image_adds_tool(tmp_path: Path) -> None:
    """register_image exposes image_generate in the harness."""
    fake_bin = tmp_path / "mflux-generate"
    fake_bin.touch()
    cfg = ImageToolConfig(enabled=True, mflux_bin=str(fake_bin))
    harness = ToolHarness()
    register_image(harness, cfg)
    assert "image_generate" in harness.all_names()


def test_register_image_risk_is_write(tmp_path: Path) -> None:
    """image_generate is declared write-risk."""
    fake_bin = tmp_path / "mflux-generate"
    fake_bin.touch()
    cfg = ImageToolConfig(enabled=True, mflux_bin=str(fake_bin))
    harness = ToolHarness()
    register_image(harness, cfg)
    assert harness.risk_for("image_generate") == "write"


def test_register_image_missing_absolute_bin_raises() -> None:
    """register_image fails fast when a configured absolute path doesn't exist."""
    cfg = ImageToolConfig(enabled=True, mflux_bin="/nonexistent/mflux-generate")
    harness = ToolHarness()
    with pytest.raises(ToolRegistrationError, match="not found"):
        register_image(harness, cfg)


def test_register_image_bare_command_skips_existence_check() -> None:
    """A bare command name (not absolute) is not checked at registration time."""
    cfg = ImageToolConfig(enabled=True, mflux_bin="mflux-generate")
    harness = ToolHarness()
    # PATH resolution is deferred to execution time — should not raise.
    register_image(harness, cfg)
    assert "image_generate" in harness.all_names()


# ── Execution tests ───────────────────────────────────────────────────────────


def _make_proc(returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    """Return a mock subprocess with communicate() returning (b'', stderr)."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    proc.kill = MagicMock()
    return proc


async def test_image_generate_happy_path(tmp_path: Path) -> None:
    """image_generate returns path + metadata when mflux exits 0."""
    out = tmp_path / "out.png"
    proc = _make_proc(returncode=0)

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.wait_for", new=AsyncMock(return_value=(b"", b""))),
        patch("asyncio.to_thread", new=AsyncMock(side_effect=[out, None, True])),
    ):
        result = await image_generate(
            "a red apple",
            str(out),
            width=512,
            height=512,
            steps=2,
            image_path=None,
            image_strength=0.75,
            mflux_bin="mflux-generate",
            model="schnell",
            quantize=4,
            low_ram=True,
            mlx_cache_limit_gb=8,
            timeout_s=60.0,
        )

    assert result["path"] == str(out)
    assert result["width"] == 512
    assert result["height"] == 512
    assert "generation_time_s" in result


async def test_image_generate_nonzero_exit_raises(tmp_path: Path) -> None:
    """A non-zero subprocess exit becomes ToolExecutionError."""
    out = tmp_path / "out.png"
    proc = _make_proc(returncode=1, stderr=b"OOM error")

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.wait_for", new=AsyncMock(return_value=(b"", b"OOM error"))),
        patch("asyncio.to_thread", new=AsyncMock(side_effect=[out, None])),
    ):
        with pytest.raises(ToolExecutionError, match="exited 1"):
            await image_generate(
                "prompt",
                str(out),
                width=1024,
                height=1024,
                steps=4,
                image_path=None,
                image_strength=0.75,
                mflux_bin="mflux-generate",
                model="schnell",
                quantize=4,
                low_ram=True,
                mlx_cache_limit_gb=8,
                timeout_s=60.0,
            )


async def test_image_generate_timeout_raises(tmp_path: Path) -> None:
    """TimeoutError becomes ToolExecutionError with a clear message."""
    out = tmp_path / "out.png"
    proc = _make_proc(returncode=0)

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.wait_for", new=AsyncMock(side_effect=TimeoutError)),
        patch("asyncio.to_thread", new=AsyncMock(side_effect=[out, None])),
    ):
        with pytest.raises(ToolExecutionError, match="timed out"):
            await image_generate(
                "prompt",
                str(out),
                width=1024,
                height=1024,
                steps=4,
                image_path=None,
                image_strength=0.75,
                mflux_bin="mflux-generate",
                model="schnell",
                quantize=4,
                low_ram=True,
                mlx_cache_limit_gb=8,
                timeout_s=1.0,
            )


async def test_image_generate_img2img_passes_args(tmp_path: Path) -> None:
    """img2img arguments are forwarded to the subprocess command."""
    out = tmp_path / "out.png"
    proc = _make_proc(returncode=0)

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec,
        patch("asyncio.wait_for", new=AsyncMock(return_value=(b"", b""))),
        patch("asyncio.to_thread", new=AsyncMock(side_effect=[out, None, True])),
    ):
        await image_generate(
            "transform to watercolor",
            str(out),
            width=1024,
            height=1024,
            steps=4,
            image_path="/tmp/source.png",
            image_strength=0.7,
            mflux_bin="mflux-generate",
            model="schnell",
            quantize=4,
            low_ram=True,
            mlx_cache_limit_gb=8,
            timeout_s=60.0,
        )

    cmd = mock_exec.call_args[0]
    assert "--image-path" in cmd
    assert "/tmp/source.png" in cmd
    assert "--image-strength" in cmd
    assert "0.7" in cmd


# ── Settings integration tests ────────────────────────────────────────────────


def test_image_tool_disabled_by_default() -> None:
    """The image tool is off in the default config (no config.toml override)."""
    assert not ImageToolConfig().enabled


def test_image_tool_schema_contains_required_fields(tmp_path: Path) -> None:
    """Registered tool schema lists prompt and output_path as required."""
    fake_bin = tmp_path / "mflux-generate"
    fake_bin.touch()
    cfg = ImageToolConfig(enabled=True, mflux_bin=str(fake_bin))
    harness = ToolHarness()
    register_image(harness, cfg)
    schema = harness.schema_for("image_generate")
    assert schema is not None
    required = schema["function"]["parameters"]["required"]
    assert "prompt" in required
    assert "output_path" in required
