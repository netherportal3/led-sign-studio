"""Drive an iLedColor Bluetooth LED sign ("iledcolor-XXXX", Shenzhen I-ledshow) from this PC.

Protocol sources (public reverse-engineering work on the same hardware family):
  github.com/winnerplace/ha-iledcolor-led-matrix   program format, capability blob, effects
  github.com/zagi/iledcolor_small                  handshake, acks, GIF program fields, CRC-32C
                                                   (MIT License, see THIRD_PARTY_NOTICES.md)
docs/PROTOCOL.md describes the protocol and what was verified on real hardware.

Wire format, all on GATT service 0xA950:
  A951  write  command frames (connect, password check, program header, brightness, power)
  A952  write  program data chunks + end marker
  A953  notify acks from the sign
  Every frame: 54 <op> <len u16 BE = payload+2> <payload> <u16 BE sum of all prior bytes>

Use as a library:
    async with Sign.open() as sign:
        await sign.show_image(img)                 # PIL image, any size
        await sign.show_text("hello", effect="left")
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import struct
import sys
import time
from dataclasses import dataclass
from typing import Callable

from bleak import BleakClient, BleakScanner
from PIL import Image, ImageDraw, ImageFont, ImageSequence

CHAR_CMD = "0000a951-0000-1000-8000-00805f9b34fb"
CHAR_DATA = "0000a952-0000-1000-8000-00805f9b34fb"
CHAR_NOTIFY = "0000a953-0000-1000-8000-00805f9b34fb"

OP_CHUNK = 0x00
OP_END = 0x01
OP_PROGRAM = 0x06
OP_BRIGHTNESS = 0x09
OP_POWER = 0x0A
OP_CONNECT = 0x0D
OP_TESTPASS = 0x0F

# Effect ids from the vendor app (the HA integration lists them in Korean).
EFFECTS = {"static": 0, "left": 1, "right": 2, "up": 3, "down": 4, "snow": 5, "roll": 6, "laser": 7}

DEFAULT_SIZE = (64, 16)  # the tested panel; read live from the advertisement when possible
# Byte order the panel expects per pixel. The tested panel is plain RGB (checked with raw color blocks);
# --order exists for panels wired differently.
DEFAULT_ORDER = "rgb"
# GIF playback tops out around 25-33 fps: shorter frames stutter on the sign.
MIN_FRAME_MS = 30
# Paced uploads (send_payload finish_at): the longest pause between two chunks, and about how long
# the end marker and its ack take.
PACE_MAX_S = 0.25
END_SECONDS = 0.15
VERBOSE = False


def log(*a: object) -> None:
    if VERBOSE and sys.stderr is not None:
        print("[sign]", *a, file=sys.stderr)


class SignNotFound(Exception):
    """No iledcolor sign answered the scan."""


class StorageFull(RuntimeError):
    """The sign replied status 2 to a program header: its storage is full."""


ProgressCallback = Callable[[float], None]


# ---------------------------------------------------------------- framing


def frame(op: int, payload: bytes) -> bytes:
    body = bytes([0x54, op]) + (len(payload) + 2).to_bytes(2, "big") + payload
    return body + (sum(body) & 0xFFFF).to_bytes(2, "big")


# CRC-32C (Castagnoli), table driven. Adapted from zagi/iledcolor_small (MIT License,
# Copyright (c) 2026 Michal Zagalski; see THIRD_PARTY_NOTICES.md).
_CRC32C = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (0x82F63B78 if _c & 1 else 0)
    _CRC32C.append(_c)


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC32C[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFFFFFF


def reorder(rgb: bytes, order: str) -> bytes:
    """Permute each pixel's R,G,B bytes into the panel's wiring order."""
    if order == "rgb":
        return rgb
    out = bytearray(len(rgb))
    for dst, ch in enumerate(order):
        out[dst::3] = rgb["rgb".index(ch)::3]
    return bytes(out)


# Raw-pixel program parameters (22 bytes, big-endian): 4 zero bytes, width, height, 3 zero bytes,
# source type (0 = raw RGB), frame count, effect, speed, dwell, 0, brightness, 3 zero bytes.
PROGRAM_PARAMS = struct.Struct(">4xHH3xBHBBBBB3x")


def program_payload(width: int, height: int, frames: list[bytes], *, effect: int = 0,
                    speed: int = 1, dwell: int = 30, brightness: int = 100) -> bytes:
    """The stored "program": parameters + raw RGB frames, in the CRC-32C envelope.

    Several frames play as a slideshow with the sign's own transition (`effect`); they are
    followed by the speed as a 2-byte trailer. See docs/PROTOCOL.md.
    """
    n = len(frames)
    if n == 1 and effect == 0:
        dwell = max(dwell, 30)  # keep a still's hold time away from tiny values
    params = PROGRAM_PARAMS.pack(width, height, 0, n, effect & 0xFF, speed & 0xFF, dwell & 0xFF, 0,
                                 brightness & 0xFF)
    body = b"".join(frames)
    if n > 1:
        body += struct.pack(">H", speed & 0xFFFF)
    return wrap_program(params, body)


def gif_payload(width: int, height: int, gif: bytes) -> bytes:
    """A raw GIF file the sign decodes and loops by itself (source type 6)."""
    fields = (0, 0, width, height, 0, 0x0006, 0x0001, 0x0064, 0x0400, 0x0064, 0)
    return wrap_program(b"".join(f.to_bytes(2, "big") for f in fields), gif)


def wrap_program(params: bytes, body: bytes) -> bytes:
    data = bytes([1, 0, 0, 0]) + bytes(16) + params + body
    return crc32c(data).to_bytes(4, "big") + data


def brightness_frame(level: int) -> bytes:
    """Brightness command, UI level 1 (dim) to 10 (bright). Captured from the vendor app."""
    level = max(1, min(10, int(level)))
    return frame(OP_BRIGHTNESS, bytes([10 - level]) + bytes(8))


def power_frame(on: bool) -> bytes:
    """Display on/off command. Captured from the vendor app."""
    return frame(OP_POWER, bytes([1 if on else 0]) + bytes(8))


# ---------------------------------------------------------------- discovery


@dataclass
class Found:
    address: str
    name: str
    width: int
    height: int
    color_type: int
    ble_device: object


CAPABILITY_MARKER = b"TBD"  # 54 42 44: the start of the capability blob in the advertisement


def parse_capability(manufacturer_data: dict[int, bytes]) -> tuple[int, int, int] | None:
    """(width, height, color type) from the advertisement, or None.

    The capability blob (at least 16 bytes) starts with "TBD" somewhere in the manufacturer data,
    counting the 2-byte company id (little-endian) that bleak splits off. Height and width are
    big-endian at offsets 4 and 6, the color type (3 = full color) at offset 8.
    """
    for company_id, data in manufacturer_data.items():
        raw = struct.pack("<H", company_id) + bytes(data)
        start = raw.find(CAPABILITY_MARKER)
        if start >= 0 and len(raw) - start >= 16:
            height, width, color_type = struct.unpack_from(">HHB", raw, start + 4)
            return width, height, color_type
    return None


NOT_FOUND_MESSAGE = "No iledcolor sign found. Is it powered on and not connected to the phone app?"


async def find_sign(address: str | None = None, timeout: float = 10.0, *, prefer: str | None = None,
                    grace: float = 1.5) -> Found:
    """Scan for a sign. Raises SignNotFound when none answers within `timeout`.

    `address` accepts only that device. `prefer` (e.g. the last used address) wins over other signs
    when it shows up within `grace` seconds of the first sign being seen.
    """
    best: dict[str, Found] = {}
    done = asyncio.Event()
    preferred_seen = asyncio.Event()

    def on_adv(dev, adv) -> None:
        name = adv.local_name or dev.name or ""
        if address:
            if dev.address.upper() != address.upper():
                return
        elif not name.lower().startswith("iledcolor"):
            return
        cap = parse_capability(adv.manufacturer_data)
        prev = best.get(dev.address)
        if cap:
            best[dev.address] = Found(dev.address, name or (prev.name if prev else ""), *cap, dev)
            done.set()
            if prefer and dev.address.upper() == prefer.upper():
                preferred_seen.set()
        elif not prev:
            best[dev.address] = Found(dev.address, name, *DEFAULT_SIZE, 3, dev)

    deadline = time.monotonic() + timeout
    async with BleakScanner(on_adv):
        try:
            await asyncio.wait_for(done.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        if prefer and best and not preferred_seen.is_set():
            left = min(grace, deadline - time.monotonic())
            if left > 0:
                try:
                    await asyncio.wait_for(preferred_seen.wait(), left)
                except asyncio.TimeoutError:
                    pass
    if not best:
        raise SignNotFound(NOT_FOUND_MESSAGE)
    found = next(iter(best.values()))
    if prefer:
        found = next((f for f in best.values() if f.address.upper() == prefer.upper()), found)
    log(f"found {found.name} {found.address} {found.width}x{found.height} color_type={found.color_type}")
    return found


# ---------------------------------------------------------------- device


class Sign:
    def __init__(self, found: Found, order: str = DEFAULT_ORDER,
                 disconnected_callback: Callable[["Sign"], None] | None = None) -> None:
        """`disconnected_callback(sign)` runs when the Bluetooth link drops (also after close())."""
        self.found = found
        self.width = found.width
        self.height = found.height
        self.order = order
        self._disconnected_callback = disconnected_callback
        self.client = BleakClient(found.ble_device, disconnected_callback=self._on_disconnect, timeout=20)
        self.notes: asyncio.Queue[bytes] = asyncio.Queue()

    def _on_disconnect(self, _client) -> None:
        log("disconnected")
        if self._disconnected_callback:
            self._disconnected_callback(self)

    @property
    def is_connected(self) -> bool:
        return bool(self.client.is_connected)

    @classmethod
    def open(cls, address: str | None = None, size: tuple[int, int] | None = None,
             order: str = DEFAULT_ORDER) -> "_Opener":
        return _Opener(address, size, order)

    def _on_notify(self, _c, data: bytearray) -> None:
        log("<-", bytes(data).hex())
        self.notes.put_nowait(bytes(data))

    async def connect(self) -> None:
        await self.client.connect()
        await self.client.start_notify(CHAR_NOTIFY, self._on_notify)
        log(f"connected mtu={self.client.mtu_size}")
        await self._cmd(frame(OP_CONNECT, b"\x00"))
        await self._wait(OP_CONNECT, 1.0)
        await self._cmd(frame(OP_TESTPASS, bytes(6)))  # all-zero = "no password set"
        await self._wait(OP_TESTPASS, 1.0)

    async def close(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def _cmd(self, data: bytes) -> None:
        log("A951 ->", data.hex() if len(data) < 40 else f"{data[:24].hex()}... ({len(data)} B)")
        await self.client.write_gatt_char(CHAR_CMD, data, response=False)

    async def _data(self, data: bytes) -> None:
        await self.client.write_gatt_char(CHAR_DATA, data, response=False)

    async def _wait(self, op: int, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                n = await asyncio.wait_for(self.notes.get(), left)
            except asyncio.TimeoutError:
                break
            if len(n) >= 2 and n[1] == op:
                return n
        log(f"no ack for op 0x{op:02x}")
        return None

    def _drain(self) -> None:
        while not self.notes.empty():
            self.notes.get_nowait()

    # -- raw controls ------------------------------------------------------------

    async def power(self, on: bool) -> bool:
        """Display on/off. Returns True when the sign acknowledged it."""
        await self._cmd(power_frame(on))
        return await self._wait(OP_POWER, 0.5) is not None

    async def brightness(self, level: int) -> bool:
        """1 = dimmest, 10 = brightest. Returns True when acknowledged."""
        await self._cmd(brightness_frame(level))
        return await self._wait(OP_BRIGHTNESS, 0.5) is not None

    async def send_program(self, frames: list[bytes], *, effect: int = 0, speed: int = 1,
                           dwell: int = 30, width: int | None = None,
                           progress: ProgressCallback | None = None) -> bool:
        """`width` > panel width sends one wide strip that the sign itself scrolls."""
        frames = [reorder(f, self.order) for f in frames]
        return await self.send_payload(program_payload(width or self.width, self.height, frames,
                                                       effect=effect, speed=speed, dwell=dwell),
                                       progress=progress)

    async def send_gif(self, gif: bytes, progress: ProgressCallback | None = None,
                       finish_at: float | list[float] | None = None) -> bool:
        """Upload an animated GIF (must already be panel-sized); the sign loops it on its own."""
        return await self.send_payload(gif_payload(self.width, self.height, gif), progress=progress,
                                       finish_at=finish_at)

    async def send_payload(self, payload: bytes, progress: ProgressCallback | None = None,
                           finish_at: float | list[float] | None = None) -> bool:
        """Upload a program. `progress(fraction)` is called after each chunk (0..1).

        `finish_at` (a time.monotonic() value): when the upload should complete. The sign starts the
        new program when it completes, so a caller that starts early can land that moment exactly:
        short pauses are spread between the chunks (at most PACE_MAX_S each, like a slower link).
        A list gives fallbacks: the first moment the upload can still make is used. The time spent
        pausing is kept in `last_paced_seconds`.

        Returns False when the sign did not confirm the end marker. Raises StorageFull.
        """
        self._drain()
        self.last_paced_seconds = 0.0
        t0 = time.monotonic()
        await self._cmd(frame(OP_PROGRAM, payload[:4] + len(payload).to_bytes(4, "big") + bytes(3)))
        ack = await self._wait(OP_PROGRAM, 1.5)
        status = ack[4] if ack and len(ack) > 4 else None
        if status == 3:
            log("sign already shows this exact content")
            if progress:
                progress(1.0)
            return True
        if status == 2:
            raise StorageFull("the sign says its storage is full")
        size = max(20, min(self.client.mtu_size - 32, 480))
        chunks = [payload[o:o + size] for o in range(0, len(payload), size)]
        t_data = time.monotonic()
        targets = sorted(finish_at) if isinstance(finish_at, (list, tuple)) else (
            [finish_at] if finish_at is not None else [])
        for i, chunk in enumerate(chunks):
            await self._data(frame(OP_CHUNK, i.to_bytes(4, "big") + len(chunk).to_bytes(2, "big") + chunk))
            await self._wait(OP_CHUNK, 0.6)
            if progress:
                progress((i + 1) / len(chunks))
            if targets:
                left = len(chunks) - i - 1
                per = (time.monotonic() - t_data - self.last_paced_seconds) / (i + 1)
                done_at = time.monotonic() + left * per + END_SECONDS
                target = next((t for t in targets if t >= done_at), None)
                slack = (target - done_at) if target is not None else 0.0
                if slack > 0.002:
                    t_p = time.monotonic()
                    await asyncio.sleep(min(PACE_MAX_S, slack / (left + 1)))
                    self.last_paced_seconds += time.monotonic() - t_p
        await self._data(frame(OP_END, b"\x01"))
        end = await self._wait(OP_END, 2.0)
        ok = bool(end and len(end) > 4 and end[4] == 1)
        log(f"sent {len(payload)} B in {len(chunks)} chunks, "
            f"{time.monotonic() - t0:.2f}s, end={'ok' if ok else (end.hex() if end else 'none')}")
        return ok

    # -- pixels ------------------------------------------------------------------

    def to_rgb(self, img: Image.Image, fit: str = "contain") -> bytes:
        img = img.convert("RGB")
        w, h = self.width, self.height
        if img.size != (w, h):
            if fit == "stretch":
                img = img.resize((w, h), Image.LANCZOS)
            else:
                scale = (min if fit == "contain" else max)(w / img.width, h / img.height)
                img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
                canvas = Image.new("RGB", (w, h))
                canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
                img = canvas
        return img.tobytes()

    async def show_image(self, img: Image.Image, *, fit: str = "contain", effect: str = "static",
                         speed: int = 1, dwell: int = 30) -> bool:
        return await self.send_program([self.to_rgb(img, fit)], effect=EFFECTS[effect], speed=speed, dwell=dwell)

    async def show_frames(self, imgs: list[Image.Image], *, fit: str = "contain", speed: int = 1,
                          dwell: int = 10, effect: str = "static") -> bool:
        return await self.send_program([self.to_rgb(i, fit) for i in imgs], effect=EFFECTS[effect],
                                       speed=speed, dwell=dwell)

    async def show_color(self, rgb: tuple[int, int, int]) -> bool:
        return await self.show_image(Image.new("RGB", (self.width, self.height), rgb))

    async def show_text(self, text: str, *, color=(255, 255, 255), bg=(0, 0, 0), scroll: bool | None = None,
                        frame_ms: int = 30, step: int = 1, font: str | None = None,
                        size: int | None = None) -> bool:
        """Static when it fits (unless scroll=True), otherwise a smooth right-to-left marquee."""
        strip = render_text(text, self.height, color=color, bg=bg, font=font, size=size)
        if scroll is None:
            scroll = strip.width > self.width
        if not scroll:
            canvas = Image.new("RGB", (self.width, self.height), bg)
            canvas.paste(strip, ((self.width - strip.width) // 2, 0))
            return await self.show_image(canvas)
        # Loops seamlessly, with a half-screen gap between repeats.
        lead = Image.new("RGB", (strip.width + self.width // 2, self.height), bg)
        lead.paste(strip, (0, 0))
        return await self.send_gif(scroll_gif(lead, self.width, step=step, frame_ms=frame_ms, bg=bg))

    async def show_gif_file(self, path: str, *, fit: str = "contain", max_frames: int = 300) -> bool:
        src = Image.open(path)
        frames, durations = [], []
        for fr in ImageSequence.Iterator(src):
            if len(frames) >= max_frames:
                break
            rgba = fr.convert("RGBA")
            flat = Image.new("RGB", rgba.size)
            flat.paste(rgba, mask=rgba.getchannel("A"))
            frames.append(Image.frombytes("RGB", (self.width, self.height), self.to_rgb(flat, fit)))
            durations.append(max(MIN_FRAME_MS, int(fr.info.get("duration", src.info.get("duration", 100)) or 100)))
        return await self.send_gif(encode_gif(frames, durations))


class _Opener:
    def __init__(self, address: str | None, size: tuple[int, int] | None, order: str) -> None:
        self.address, self.size, self.order, self.sign = address, size, order, None

    async def __aenter__(self) -> Sign:
        found = await find_sign(self.address)
        if self.size:
            found.width, found.height = self.size
        self.sign = Sign(found, self.order)
        await self.sign.connect()
        return self.sign

    async def __aexit__(self, *exc: object) -> None:
        if self.sign:
            await self.sign.close()


# ---------------------------------------------------------------- text rendering

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def load_font(name: str | None, size: int) -> ImageFont.FreeTypeFont:
    candidates = [name] if name else ["arialbd.ttf", "segoeuib.ttf", "verdanab.ttf"]
    for c in candidates:
        for path in (c, os.path.join(FONT_DIR, c)):
            if path and os.path.exists(path):
                return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def render_text(text: str, height: int, *, color=(255, 255, 255), bg=(0, 0, 0), font: str | None = None,
                size: int | None = None) -> Image.Image:
    """Render text as a crisp (no anti-aliasing) strip exactly `height` pixels tall."""
    f = load_font(font, size or height)
    probe = ImageDraw.Draw(Image.new("1", (1, 1)))
    probe.fontmode = "1"
    l, t, r, b = probe.textbbox((0, 0), text, font=f)
    img = Image.new("RGB", (max(1, r - l + 2), height), bg)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    # vertically center on the ink box
    d.text((1 - l, (height - (b - t)) // 2 - t), text, font=f, fill=color)
    return img


def shared_palette(frames: list[Image.Image], colors: int = 256) -> Image.Image:
    """One palette image for all frames: exact when they use at most `colors` colors."""
    colors = max(2, min(256, int(colors)))
    sheet = Image.new("RGB", (frames[0].width, frames[0].height * len(frames)))
    for i, f in enumerate(frames):
        sheet.paste(f.convert("RGB"), (0, i * f.height))
    used = sheet.getcolors(colors)
    if used is not None:
        pal = Image.new("P", (1, 1))
        pal.putpalette([c for _n, rgb in sorted(used, key=lambda nc: -nc[0]) for c in rgb])
        return pal
    return sheet.quantize(colors=colors, dither=Image.Dither.NONE)


def encode_gif(frames: list[Image.Image], durations: int | list[int], colors: int = 256) -> bytes:
    """Encode panel-sized RGB frames as a looping GIF with one shared palette of up to `colors`.

    Every frame uses the one global color table: no frame carries a local table. (Without the
    explicit `palette=` Pillow writes a 768-byte local table into every frame after the first.)
    """
    pal = shared_palette(frames, colors)
    q = [f.convert("RGB").quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    palette = bytes(q[0].getpalette() or [])
    buf = io.BytesIO()
    q[0].save(buf, format="GIF", save_all=True, append_images=q[1:], duration=durations, loop=0,
              disposal=1, optimize=False, palette=palette)
    return buf.getvalue()


def scroll_gif(strip: Image.Image, width: int, *, step: int = 1, frame_ms: int = 30,
               bg=(0, 0, 0)) -> bytes:
    """Marquee: a `width`-wide window sliding across `strip`, wrapping seamlessly."""
    doubled = Image.new("RGB", (strip.width * 2, strip.height), bg)
    doubled.paste(strip, (0, 0))
    doubled.paste(strip, (strip.width, 0))
    frames = [doubled.crop((x, 0, x + width, strip.height)) for x in range(0, strip.width, step)]
    return encode_gif(frames, frame_ms)


def preview(img_bytes_list: list[bytes], w: int, h: int, path: str, scale: int = 10) -> None:
    """Save an LED-style enlarged preview of what is being sent."""
    frames = [Image.frombytes("RGB", (w, h), fb) for fb in img_bytes_list]
    sheet = Image.new("RGB", (w * scale, (h * scale + 6) * len(frames)), (25, 25, 25))
    d = ImageDraw.Draw(sheet)
    for fi, fr in enumerate(frames):
        oy = fi * (h * scale + 6)
        for y in range(h):
            for x in range(w):
                c = fr.getpixel((x, y))
                d.ellipse((x * scale + 1, oy + y * scale + 1, x * scale + scale - 2, oy + y * scale + scale - 2),
                          fill=c if c != (0, 0, 0) else (40, 40, 40))
    sheet.save(path)


# ---------------------------------------------------------------- CLI

NAMED = {"red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "white": (255, 255, 255),
         "yellow": (255, 200, 0), "cyan": (0, 255, 255), "magenta": (255, 0, 255), "orange": (255, 100, 0),
         "purple": (140, 0, 255), "pink": (255, 60, 150), "black": (0, 0, 0)}


def color_arg(s: str) -> tuple[int, int, int]:
    s = s.strip().lower()
    if s in NAMED:
        return NAMED[s]
    bad = argparse.ArgumentTypeError(f"bad color {s!r}: use a name, #rrggbb or r,g,b")
    if "," in s or " " in s:
        # r,g,b first: "12,6,0" is also 6 characters long but is not hex.
        try:
            parts = [int(p) for p in s.replace(",", " ").split()]
        except ValueError:
            raise bad from None
        if len(parts) == 3 and all(0 <= p <= 255 for p in parts):
            return tuple(parts)  # type: ignore[return-value]
        raise bad
    h = s.lstrip("#")
    if len(h) == 6:
        try:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            raise bad from None
    raise bad


def brightness_arg(s: str) -> int:
    try:
        level = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad level {s!r}: use 1 to 10") from None
    if not 1 <= level <= 10:
        raise argparse.ArgumentTypeError(f"bad level {level}: use 1 to 10")
    return level


def test_pattern(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    third = w // 3
    d.rectangle((0, 0, third - 1, h - 1), fill=(255, 0, 0))
    d.rectangle((third, 0, 2 * third - 1, h - 1), fill=(0, 255, 0))
    d.rectangle((2 * third, 0, w - 1, h - 1), fill=(0, 0, 255))
    d.rectangle((0, 0, w - 1, 0), fill=(255, 255, 255))  # top row white
    d.rectangle((0, h - 4, 3, h - 1), fill=(255, 255, 0))  # bottom-left 4x4 yellow
    # 2px black gaps between the blocks make the column boundaries countable
    d.rectangle((third - 1, 1, third, h - 1), fill=(0, 0, 0))
    d.rectangle((2 * third - 1, 1, 2 * third, h - 1), fill=(0, 0, 0))
    return img


async def main() -> None:
    global VERBOSE
    ap = argparse.ArgumentParser(description="Control the iledcolor LED sign over Bluetooth.")
    ap.add_argument("--addr", help="Bluetooth address (default: first iledcolor-* found)")
    ap.add_argument("--size", help="override panel size, e.g. 64x16")
    ap.add_argument("--order", default=DEFAULT_ORDER, choices=["rgb", "rbg", "grb", "gbr", "brg", "bgr"],
                    help=f"pixel byte order the panel expects (default {DEFAULT_ORDER})")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--preview", help="also save an enlarged PNG preview of what is sent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="find the sign and print its size")
    sub.add_parser("test", help="show a red/green/blue test pattern")
    p = sub.add_parser("color", help="fill the panel with one color")
    p.add_argument("color", type=color_arg)
    p = sub.add_parser("text", help="show text (scrolls automatically when too long)")
    p.add_argument("text")
    p.add_argument("--color", type=color_arg, default=(255, 255, 255))
    p.add_argument("--bg", type=color_arg, default=(0, 0, 0))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--scroll", dest="scroll", action="store_true", default=None, help="scroll even if it fits")
    g.add_argument("--static", dest="scroll", action="store_false", help="never scroll (long text gets cut off)")
    p.add_argument("--frame-ms", type=int, default=30, help="ms per step (default 30, about the sign's top speed)")
    p.add_argument("--step", type=int, default=1, help="pixels per step (default 1; higher is faster but jumpier)")
    p.add_argument("--font", help="font file name in C:\\Windows\\Fonts or a path")
    p.add_argument("--font-size", type=int)
    p = sub.add_parser("image", help="show a picture file")
    p.add_argument("path")
    p.add_argument("--fit", choices=["contain", "cover", "stretch"], default="contain")
    p.add_argument("--effect", choices=list(EFFECTS), default="static")
    p = sub.add_parser("gif", help="play an animated GIF (the sign loops it by itself)")
    p.add_argument("path")
    p.add_argument("--fit", choices=["contain", "cover", "stretch"], default="contain")
    p.add_argument("--max-frames", type=int, default=300)
    p = sub.add_parser("brightness", help="1 (dim) to 10 (bright)")
    p.add_argument("level", type=brightness_arg)
    sub.add_parser("on")
    sub.add_parser("off")
    a = ap.parse_args()
    VERBOSE = a.verbose
    size = tuple(int(x) for x in a.size.lower().split("x")) if a.size else None

    if a.cmd == "scan":
        f = await find_sign(a.addr)
        print(f"{f.name}  {f.address}  {f.width}x{f.height}  {'full color' if f.color_type == 3 else 'single color'}")
        return

    async with Sign.open(a.addr, size, a.order) as sign:  # type: ignore[arg-type]
        w, h = sign.width, sign.height
        if a.preview:
            orig = sign.send_program

            async def send_and_preview(frames, **kw):
                preview(frames, kw.get("width") or w, h, a.preview)
                return await orig(frames, **kw)

            sign.send_program = send_and_preview  # type: ignore[method-assign]
            orig_gif = sign.send_gif

            async def send_gif_and_save(gif):
                with open(os.path.splitext(a.preview)[0] + ".gif", "wb") as fh:
                    fh.write(gif)
                return await orig_gif(gif)

            sign.send_gif = send_gif_and_save  # type: ignore[method-assign]
        if a.cmd == "test":
            ok = await sign.show_image(test_pattern(w, h))
        elif a.cmd == "color":
            ok = await sign.show_color(a.color)
        elif a.cmd == "text":
            ok = await sign.show_text(a.text, color=a.color, bg=a.bg, scroll=a.scroll, frame_ms=a.frame_ms,
                                      step=a.step, font=a.font, size=a.font_size)
        elif a.cmd == "image":
            ok = await sign.show_image(Image.open(a.path), fit=a.fit, effect=a.effect)
        elif a.cmd == "gif":
            ok = await sign.show_gif_file(a.path, fit=a.fit, max_frames=a.max_frames)
        elif a.cmd == "brightness":
            await sign.brightness(a.level)
            ok = True
        else:
            await sign.power(a.cmd == "on")
            ok = True
        print("ok" if ok else "sent, but the sign did not confirm (run with -v to see its replies)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SignNotFound as e:
        raise SystemExit(str(e)) from None
