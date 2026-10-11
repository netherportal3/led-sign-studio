"""Pixel buffers, GIF encoding, file import and export."""
from __future__ import annotations

import base64
import binascii
import io
import logging

from PIL import Image, ImageOps, ImageSequence

import iledsign

log = logging.getLogger("ledsign.media")

MIN_FRAME_MS = iledsign.MIN_FRAME_MS
MAX_FRAME_MS = 60000
MAX_ANIMATION_FRAMES = 1000
MAX_SLIDESHOW_FRAMES = 30
MAX_IMPORT_FRAMES = 1000
MAX_SOURCE_FRAMES = 5000
UPLOAD_BYTES_PER_SECOND = 6500
DEFAULT_IMPORT_MS = 100

# Refuse absurd images before decoding them (decompression bombs).
Image.MAX_IMAGE_PIXELS = 64_000_000


class BadRequest(ValueError):
    """The request body is wrong. The message is shown to the user."""


# ---------------------------------------------------------------- pixel buffers


def b64decode(s: object, what: str) -> bytes:
    if not isinstance(s, str):
        raise BadRequest(f"{what} must be a base64 string")
    try:
        return base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        raise BadRequest(f"{what} is not valid base64") from None


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_frame(s: object, w: int, h: int, what: str = "pixels") -> bytes:
    raw = b64decode(s, what)
    if len(raw) != w * h * 3:
        raise BadRequest(f"{what} has {len(raw)} bytes, expected {w * h * 3} for {w}x{h}")
    return raw


def decode_frames(frames: object, w: int, h: int, max_frames: int) -> list[bytes]:
    if not isinstance(frames, list) or not frames:
        raise BadRequest("frames must be a non-empty list")
    if len(frames) > max_frames:
        raise BadRequest(f"Too many frames ({len(frames)}). The limit is {max_frames}.")
    return [decode_frame(f, w, h, f"frame {i + 1}") for i, f in enumerate(frames)]


def clamp_ms(v: object, default: int = DEFAULT_IMPORT_MS) -> int:
    try:
        ms = int(round(float(v)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        ms = default
    return max(MIN_FRAME_MS, min(MAX_FRAME_MS, ms))


def decode_durations(durations: object, n: int) -> list[int]:
    """A list (one per frame), a single number for all, or missing (100 ms each)."""
    if durations is None:
        return [DEFAULT_IMPORT_MS] * n
    if isinstance(durations, (int, float)):
        return [clamp_ms(durations)] * n
    if not isinstance(durations, list) or len(durations) != n:
        raise BadRequest("durations must be a list with one entry per frame")
    return [clamp_ms(d) for d in durations]


def to_images(frames: list[bytes], w: int, h: int) -> list[Image.Image]:
    return [Image.frombytes("RGB", (w, h), f) for f in frames]


# ---------------------------------------------------------------- GIF


def encode_gif(frames: list[bytes], durations: list[int], w: int, h: int, scale: int = 1) -> bytes:
    imgs = to_images(frames, w, h)
    if scale > 1:
        imgs = [im.resize((w * scale, h * scale), Image.NEAREST) for im in imgs]
    return iledsign.encode_gif(imgs, [clamp_ms(d) for d in durations], colors=256)


def still_payload(frame: bytes, w: int, h: int) -> bytes:
    return iledsign.program_payload(w, h, [frame])


def animation_payload(frames: list[bytes], durations: list[int], w: int, h: int) -> tuple[str, bytes]:
    """('still', program payload) for one frame, else ('gif', gif payload)."""
    if len(frames) == 1:
        return "still", still_payload(frames[0], w, h)
    return "gif", iledsign.gif_payload(w, h, encode_gif(frames, durations, w, h))


def estimate(frames: list[bytes], durations: list[int], w: int, h: int) -> dict:
    _kind, payload = animation_payload(frames, durations, w, h)
    return {"bytes": len(payload), "seconds": round(len(payload) / UPLOAD_BYTES_PER_SECOND + 1, 1)}


# ---------------------------------------------------------------- import


def _flatten(frame: Image.Image) -> Image.Image:
    """Any mode to RGB, transparent areas over black."""
    rgba = frame.convert("RGBA")
    black = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    return Image.alpha_composite(black, rgba).convert("RGB")


def fit_image(img: Image.Image, w: int, h: int, fit: str) -> Image.Image:
    if img.size == (w, h):
        return img
    if fit == "stretch":
        up = img.width <= w and img.height <= h
        return img.resize((w, h), Image.NEAREST if up else Image.LANCZOS)
    scale = (min if fit == "contain" else max)(w / img.width, h / img.height)
    nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    # Upscaling keeps pixel art crisp; downscaling keeps photos smooth.
    resized = img.resize((nw, nh), Image.NEAREST if scale >= 1 else Image.LANCZOS)
    canvas = Image.new("RGB", (w, h))
    canvas.paste(resized, ((w - nw) // 2, (h - nh) // 2))
    return canvas


def _frame_ms(fr: Image.Image, src: Image.Image) -> int:
    d = fr.info.get("duration", src.info.get("duration"))
    try:
        d = int(d) if d is not None else 0
    except (TypeError, ValueError):
        d = 0
    # Browsers play 0 and 10 ms GIF frames at 100 ms; do the same.
    return DEFAULT_IMPORT_MS if d <= 10 else d


def import_media(data: bytes, w: int, h: int, fit: str = "contain", max_frames: int = 300) -> dict:
    if fit not in ("contain", "cover", "stretch"):
        raise BadRequest("fit must be contain, cover or stretch")
    max_frames = max(1, min(MAX_IMPORT_FRAMES, int(max_frames or 300)))
    try:
        src = Image.open(io.BytesIO(data))
        src.load()
    except Image.DecompressionBombError:
        raise BadRequest("That picture is too large.") from None
    except Exception:  # noqa: BLE001  (Pillow raises many types for bad files)
        raise BadRequest("Could not read that file. Use a GIF, PNG, JPG, WebP or BMP.") from None

    raw_frames: list[Image.Image] = []
    raw_ms: list[int] = []
    animated = getattr(src, "is_animated", False) and getattr(src, "n_frames", 1) > 1
    try:
        if animated:
            for fr in ImageSequence.Iterator(src):
                if len(raw_frames) >= MAX_SOURCE_FRAMES:
                    break
                raw_frames.append(fit_image(_flatten(fr), w, h, fit))
                raw_ms.append(_frame_ms(fr, src))
        else:
            img = ImageOps.exif_transpose(src) or src
            raw_frames.append(fit_image(_flatten(img), w, h, fit))
            raw_ms.append(DEFAULT_IMPORT_MS)
    except Exception:  # noqa: BLE001
        if not raw_frames:
            raise BadRequest("Could not read that file. Use a GIF, PNG, JPG, WebP or BMP.") from None
        log.warning("import stopped after %d frames (decode error)", len(raw_frames), exc_info=True)

    n = len(raw_frames)
    if n > max_frames:
        # Keep the whole loop: sample evenly and give each kept frame the time of the ones it covers.
        picks = [i * n // max_frames for i in range(max_frames)]
        bounds = picks + [n]
        frames = [raw_frames[i] for i in picks]
        durations = [sum(raw_ms[bounds[k]:bounds[k + 1]]) for k in range(max_frames)]
    else:
        frames, durations = raw_frames, raw_ms
    return {
        "frames": [b64(f.tobytes()) for f in frames],
        "durations": [clamp_ms(d) for d in durations],
        "width": w,
        "height": h,
        "source_frames": max(n, int(getattr(src, "n_frames", 1) or 1)),
    }
