#!/usr/bin/env python3
"""Portable Gemini image generator.

A dependency-free (except google-genai) image generator adapted from the podforge
project's renderer, generalized for reuse in any project. Generate an image from a
text prompt — with optional reference images for visual conditioning, aspect ratio,
and seed — via the Google Gen AI SDK.

Use as a library:
    from gemini_image import render_image, save_image
    save_image("a serene crystal city at dusk", "out.png", aspect="16:9")

Or as a CLI:
    python gemini_image.py "a serene crystal city at dusk" -o out.png --aspect 16:9
    python gemini_image.py "..." -o out.png --ref moodboard.png --env-file ../.env

Key: reads GEMINI_API_KEY (or GOOGLE_API_KEY) from the environment, or pass
api_key=, or point --env-file at a .env containing GEMINI_API_KEY=...
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

MODEL_DEFAULT = "gemini-2.5-flash-image"
_TRANSIENT_CODES = {429, 500, 502, 503, 504}
_BACKOFF_SECONDS = (4, 8, 16, 32, 60)


class RenderError(Exception):
    pass


def render_image(
    prompt: str,
    *,
    api_key: str | None = None,
    model: str = MODEL_DEFAULT,
    aspect: str = "1:1",
    seed: int | None = None,
    reference_images: list[str | Path] | None = None,
) -> bytes:
    """Render an image from a prompt via Gemini; return PNG bytes.

    reference_images are sent alongside the prompt as multi-modal conditioning
    (useful for style/character consistency).
    """
    from google import genai
    from google.genai import types, errors as genai_errors

    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RenderError("No API key. Set GEMINI_API_KEY, pass api_key=, or use --env-file.")

    client = genai.Client(api_key=key)
    config = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(aspect_ratio=aspect),
    )
    if seed is not None:
        config.seed = seed

    contents: list = [prompt]
    for ref in reference_images or []:
        contents.append(types.Part.from_bytes(data=Path(ref).read_bytes(), mime_type="image/png"))

    last_exc: Exception | None = None
    for attempt, delay in enumerate([0, *_BACKOFF_SECONDS]):
        if delay:
            time.sleep(delay)
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
            break
        except genai_errors.APIError as e:
            last_exc = e
            code = getattr(e, "code", None)
            if code in _TRANSIENT_CODES and attempt < len(_BACKOFF_SECONDS):
                continue
            raise RenderError(f"Gemini call failed (code={code}): {e}") from e
        except Exception as e:  # noqa: BLE001
            raise RenderError(f"Gemini call failed: {e}") from e
    else:
        raise RenderError(f"Gemini call failed after retries: {last_exc}")

    blocked: list[str] = []
    for candidate in response.candidates or []:
        if candidate.content is None:  # safety-filtered
            blocked.append(str(getattr(candidate, "finish_reason", None) or "no content"))
            continue
        for part in getattr(candidate.content, "parts", None) or []:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                return part.inline_data.data
    detail = f" (finish_reason={'/'.join(blocked)})" if blocked else ""
    raise RenderError(f"Gemini returned no image bytes{detail} for: {prompt[:80]}...")


def save_image(prompt: str, out_path: str | Path, **kwargs) -> Path:
    """render_image + write to disk. Returns the output path."""
    data = render_image(prompt, **kwargs)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


def load_env_file(path: str | Path) -> None:
    """Minimal .env loader (KEY=VALUE lines) into os.environ. No dependency."""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate an image from a text prompt via Gemini.")
    ap.add_argument("prompt", help="Text prompt.")
    ap.add_argument("-o", "--out", required=True, help="Output image path (.png).")
    ap.add_argument("--aspect", default="1:1", help="Aspect ratio, e.g. 1:1, 16:9, 3:4.")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--ref", action="append", help="Reference image path (repeatable).")
    ap.add_argument("--env-file", help="Path to a .env file with GEMINI_API_KEY.")
    args = ap.parse_args()

    if args.env_file:
        load_env_file(args.env_file)
    out = save_image(
        args.prompt, args.out,
        model=args.model, aspect=args.aspect, seed=args.seed, reference_images=args.ref,
    )
    print(f"saved {out}")


if __name__ == "__main__":
    main()
