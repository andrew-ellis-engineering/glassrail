"""Image generation integration — wraps mflux-generate as a tool.

Calls the mflux CLI as an async subprocess so the event loop is never
blocked during generation. Supports text-to-image and image-to-image
(img2img) via the same tool call — pass ``image_path`` and
``image_strength`` for img2img.

Opt-in: ``[tools.image] enabled = true`` in ``config.toml``.
The mflux binary is auto-discovered at ``~/.venvs/mflux/bin/mflux-generate``
or falls through to the system PATH; override with ``mflux_bin``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from glassrail.core import ToolExecutionError, ToolRegistrationError
from glassrail.harness.pathguard import ensure_within_roots

if TYPE_CHECKING:
    from glassrail.config import ImageToolConfig
    from glassrail.harness.registry import ToolHarness

log = logging.getLogger(__name__)

_WELL_KNOWN_BIN = Path.home() / ".venvs" / "mflux" / "bin" / "mflux-generate"


def _resolve_bin(config: ImageToolConfig) -> str:
    if config.mflux_bin:
        return config.mflux_bin
    if _WELL_KNOWN_BIN.exists():
        return str(_WELL_KNOWN_BIN)
    return "mflux-generate"


def _verify_bin(mflux_bin: str) -> None:
    """Raise ToolRegistrationError if an absolute path doesn't exist."""
    path = Path(mflux_bin)
    if path.is_absolute() and not path.exists():
        raise ToolRegistrationError(
            f"mflux-generate not found at {mflux_bin!r}. "
            "Install mflux with: uv venv ~/.venvs/mflux --python 3.12 "
            "&& uv pip install --python ~/.venvs/mflux/bin/python mflux"
        )


async def image_generate(
    prompt: str,
    output_path: str,
    *,
    width: int = 1024,
    height: int = 1024,
    steps: int,
    image_path: str | None,
    image_strength: float,
    mflux_bin: str,
    model: str,
    quantize: int,
    low_ram: bool,
    mlx_cache_limit_gb: int,
    timeout_s: float,
    fs_roots: list[Path] | None = None,
) -> dict[str, Any]:
    """Core implementation — called by the registered tool closure."""
    # Resolve path and create parent directory in a thread so we don't block
    # the event loop with synchronous filesystem calls.
    out = await asyncio.to_thread(ensure_within_roots, output_path, fs_roots)
    await asyncio.to_thread(out.parent.mkdir, parents=True, exist_ok=True)

    cmd = [
        mflux_bin,
        "--model",
        model,
        "--quantize",
        str(quantize),
        "--steps",
        str(steps),
        "--height",
        str(height),
        "--width",
        str(width),
        "--output",
        str(out),
        "--prompt",
        prompt,
    ]
    if low_ram:
        cmd.append("--low-ram")
    if mlx_cache_limit_gb > 0:
        cmd.extend(["--mlx-cache-limit-gb", str(mlx_cache_limit_gb)])
    if image_path:
        cmd.extend(["--image-path", image_path, "--image-strength", str(image_strength)])

    log.info("image_generate: %s %dx%d steps=%d", model, width, height, steps)
    start = time.monotonic()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        raise ToolExecutionError(f"image_generate timed out after {timeout_s}s") from None

    elapsed = round(time.monotonic() - start, 1)

    if proc.returncode != 0:
        err = (stderr.decode(errors="replace") or stdout.decode(errors="replace"))[:600]
        raise ToolExecutionError(f"mflux-generate exited {proc.returncode}: {err}")

    if not await asyncio.to_thread(out.exists):
        raise ToolExecutionError(f"mflux-generate exited 0 but output was not written to {out}")

    log.info("image_generate: wrote %s in %.1fs", out, elapsed)
    return {
        "path": str(out),
        "width": width,
        "height": height,
        "generation_time_s": elapsed,
    }


# ── Registration ─────────────────────────────────────────────────────────────


# fmt: off
def register_image(
    harness: ToolHarness, config: ImageToolConfig, *, fs_roots: list[Path] | None = None
) -> None:
    """Register the image_generate tool on ``harness`` per ``config``."""
    mflux_bin = _resolve_bin(config)
    _verify_bin(mflux_bin)

    steps = config.default_steps
    model = config.model
    quantize = config.quantize
    low_ram = config.low_ram
    mlx_cache_limit_gb = config.mlx_cache_limit_gb
    timeout_s = config.timeout_s

    async def _image_generate(
        prompt: str,
        output_path: str,
        width: int = config.default_width,
        height: int = config.default_height,
        steps: int = steps,
        image_path: str | None = None,
        image_strength: float = 0.75,
    ) -> dict[str, Any]:
        return await image_generate(
            prompt,
            output_path,
            width=width,
            height=height,
            steps=steps,
            image_path=image_path,
            image_strength=image_strength,
            mflux_bin=mflux_bin,
            model=model,
            quantize=quantize,
            low_ram=low_ram,
            mlx_cache_limit_gb=mlx_cache_limit_gb,
            timeout_s=timeout_s,
            fs_roots=fs_roots,
        )

    harness.tool(
        name="image_generate",
        description=(
            "Generate an image from a text prompt using Flux and save it to a file. "
            "For img2img (editing an existing image), supply image_path and "
            "image_strength (0.0 = ignore source, 1.0 = copy source; 0.6-0.8 "
            "is the useful range). "
            "Returns the saved file path and generation time. "
            "Generation takes 45-120 seconds; plan accordingly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed description of the image to generate.",
                },
                "output_path": {
                    "type": "string",
                    "description": (
                        "Absolute or home-relative path to save the PNG, "
                        "e.g. ~/Desktop/result.png"
                    ),
                },
                "width": {
                    "type": "integer",
                    "description": "Output width in pixels (default 1024)",
                },
                "height": {
                    "type": "integer",
                    "description": "Output height in pixels (default 1024)",
                },
                "steps": {
                    "type": "integer",
                    "description": "Diffusion steps (default 4 for schnell)",
                },
                "image_path": {
                    "type": "string",
                    "description": "Source image path for img2img. Omit for text-to-image.",
                },
                "image_strength": {
                    "type": "number",
                    "description": (
                        "Img2img blend strength 0.0-1.0 (default 0.75). "
                        "Lower = closer to source; higher = more creative freedom."
                    ),
                },
            },
            "required": ["prompt", "output_path"],
        },
        risk="write",
    )(_image_generate)
    log.info(
        "Registered image tool: image_generate (model=%s, quantize=%d)",
        model,
        quantize,
    )
# fmt: on
