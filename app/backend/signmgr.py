"""Bluetooth connection manager: one asyncio thread, one lock, auto-connect and auto-reconnect.

HTTP handler threads call the public methods; everything that touches the sign runs on the loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Awaitable, Callable

from PIL import Image

import iledsign
from iledsign import Found, SignNotFound, StorageFull

log = logging.getLogger("ledsign.mgr")

SCAN_SECONDS = 8.0
NOT_FOUND_PAUSE = 1.0      # scans are 8 s long, so a new scan starts about every 9 s
BLUETOOTH_OFF_RETRY = 5.0
ERROR_RETRY = 5.0
CONNECT_TIMEOUT = 25.0
SEND_CONNECT_WAIT = 10.0
LINK_POLL = 5.0

MSG = {
    "searching": "Looking for the sign...",
    "connecting": "Connecting...",
    "connected": "Connected",
    "not_found": "Sign not found. Still looking.",
    "bluetooth_off": "Bluetooth is off.",
    "disconnected": "Disconnected. Press Connect to use the sign again.",
    "error": "Could not connect to the sign. Trying again.",
}


class BluetoothOff(Exception):
    pass


class SignError(Exception):
    """An operation failed in a way the UI explains. `code` is one of the API error codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- transports


class BleTransport:
    fake = False

    async def scan(self, prefer: str | None) -> Found:
        from bleak.exc import BleakBluetoothNotAvailableError, BleakError

        try:
            return await iledsign.find_sign(timeout=SCAN_SECONDS, prefer=prefer)
        except BleakBluetoothNotAvailableError as e:
            raise BluetoothOff(str(e.args[0]) if e.args else "Bluetooth is not available") from e
        except BleakError as e:
            text = str(e)
            if "bluetooth" in text.lower() or "adapter" in text.lower() or "radio" in text.lower():
                raise BluetoothOff(text) from e
            raise
        except OSError as e:
            # WinRT reports a missing or disabled radio as "device not ready" style OS errors.
            raise BluetoothOff(str(e)) from e

    def make_sign(self, found: Found, on_disconnect: Callable[[object], None]):
        return iledsign.Sign(found, disconnected_callback=on_disconnect)


class FakeSign:
    """Same surface as iledsign.Sign. Uploads take simulated time; results go to <data>/fake/."""

    RATE = 20000          # bytes per second
    STORAGE = 1_000_000   # bigger payloads answer "storage full"

    def __init__(self, found: Found, on_disconnect, fake_dir: str) -> None:
        self.found = found
        self.width, self.height = found.width, found.height
        self._on_disconnect = on_disconnect
        self.fake_dir = fake_dir
        self._connected = False
        self.brightness_level: int | None = None
        self.display_on: bool | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        await asyncio.sleep(0.3)
        self._connected = True

    async def close(self) -> None:
        if self._connected:
            self._connected = False
            if self._on_disconnect:
                self._on_disconnect(self)

    async def brightness(self, level: int) -> bool:
        self.brightness_level = max(1, min(10, int(level)))
        self._record({"kind": "brightness", "level": self.brightness_level,
                      "frame": iledsign.brightness_frame(level).hex()}, "last_command.json")
        return True

    async def power(self, on: bool) -> bool:
        self.display_on = bool(on)
        self._record({"kind": "power", "on": self.display_on, "frame": iledsign.power_frame(on).hex()},
                     "last_command.json")
        return True

    async def send_program(self, frames: list[bytes], *, effect: int = 0, speed: int = 1, dwell: int = 30,
                           width: int | None = None, progress=None) -> bool:
        w = width or self.width
        if w > self.width:
            raise SignError("bad_request", "Programs wider than the panel are rejected by the sign.")
        payload = iledsign.program_payload(w, self.height, frames, effect=effect, speed=speed, dwell=dwell)
        ok = await self._upload(payload, progress)
        imgs = [Image.frombytes("RGB", (w, self.height), f) for f in frames]
        if len(frames) == 1:
            self._save_image(imgs[0], "last.png")
            meta = {"kind": "still"}
        else:
            sheet = Image.new("RGB", (w, self.height * len(imgs)))
            for i, im in enumerate(imgs):
                sheet.paste(im, (0, i * self.height))
            self._save_image(sheet, "last_slideshow.png")
            meta = {"kind": "slideshow", "frames": len(frames), "effect": effect, "speed": speed, "dwell": dwell}
        self._record(dict(meta, bytes=len(payload)), "last.json")
        return ok

    async def send_gif(self, gif: bytes, progress=None, finish_at: float | list | None = None) -> bool:
        payload = iledsign.gif_payload(self.width, self.height, gif)
        ok = await self._upload(payload, progress, finish_at)
        os.makedirs(self.fake_dir, exist_ok=True)
        with open(os.path.join(self.fake_dir, "last.gif"), "wb") as fh:
            fh.write(gif)
        self._record({"kind": "animation", "bytes": len(payload), "gif_bytes": len(gif)}, "last.json")
        return ok

    async def _upload(self, payload: bytes, progress, finish_at: float | list | None = None) -> bool:
        if not self._connected:
            raise ConnectionError("not connected")
        if len(payload) > self.STORAGE:
            raise StorageFull("the sign says its storage is full")
        total = 0.15 + len(payload) / self.RATE
        self.last_paced_seconds = 0.0
        targets = sorted(finish_at) if isinstance(finish_at, (list, tuple)) else (
            [finish_at] if finish_at is not None else [])
        target = next((t for t in targets if t - time.monotonic() >= total), None)
        if target is not None:   # paced like the real sign: complete on the first moment it can make
            self.last_paced_seconds = target - time.monotonic() - total
            total += self.last_paced_seconds
        t0 = time.monotonic()
        end = t0 + total        # against the clock, so timer overshoot does not add up
        while (left := end - time.monotonic()) > 0:
            await asyncio.sleep(min(0.05, left))
            if not self._connected:
                raise ConnectionError("link lost during upload")
            if progress:
                progress(min(1.0, (time.monotonic() - t0) / total))
        if progress:
            progress(1.0)
        return True

    def _save_image(self, img: Image.Image, name: str) -> None:
        os.makedirs(self.fake_dir, exist_ok=True)
        tmp = os.path.join(self.fake_dir, name + ".tmp")
        img.save(tmp, format="PNG")
        os.replace(tmp, os.path.join(self.fake_dir, name))

    def _record(self, obj: dict, name: str) -> None:
        os.makedirs(self.fake_dir, exist_ok=True)
        obj = dict(obj, time=time.time())
        with open(os.path.join(self.fake_dir, name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)


class FakeTransport:
    fake = True

    def __init__(self, scenario: str, fake_dir: str) -> None:
        self.scenario = scenario
        self.fake_dir = fake_dir

    async def scan(self, prefer: str | None) -> Found:
        if self.scenario == "bluetooth_off":
            await asyncio.sleep(0.3)
            raise BluetoothOff("Bluetooth radio is not powered on (fake)")
        await asyncio.sleep(1.5)
        if self.scenario == "not_found":
            raise SignNotFound(iledsign.NOT_FOUND_MESSAGE)
        return Found("FA:CE:00:00:00:01", "iledcolor-FAKE", 64, 16, 3, None)

    def make_sign(self, found: Found, on_disconnect):
        return FakeSign(found, on_disconnect, self.fake_dir)


# ---------------------------------------------------------------- manager


class SignManager:
    def __init__(self, transport, settings) -> None:
        self.transport = transport
        self.settings = settings
        self.fake = bool(getattr(transport, "fake", False))
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="ble-loop", daemon=True)
        self._ready = threading.Event()
        self._st = threading.Lock()
        self._state = "searching"
        self._message = MSG["searching"]
        self._device: dict | None = None
        self._job: str | None = None
        self._progress: float | None = None
        self._last_error: str | None = None
        self.sign = None
        self.paused = False
        self._kick = False
        self._stopping = False
        # After the app is closed abruptly, Windows keeps the old link up for several seconds and the
        # sign does not advertise meanwhile. Allow one extra scan at startup before saying "not found".
        self._grace_scans = 0 if self.fake else 1
        # The fake sign keeps its values in memory so a test run never changes the real settings.
        self._brightness = None if self.fake else settings.get("brightness")
        self._display_on = None if self.fake else settings.get("display_on")

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("Bluetooth thread did not start")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._connected = asyncio.Event()
        self._sup = self.loop.create_task(self._supervisor())
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            self.loop.close()

    def stop(self, timeout: float = 4.0) -> bool:
        """Drop the link politely and stop the loop. Safe to call from any thread.

        Returns True when the link was closed (or there was none) within `timeout`.
        """
        if self._stopping or not self.loop.is_running():
            return False
        self._stopping = True
        clean = False
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop).result(timeout)
            clean = True
        except Exception:  # noqa: BLE001
            log.warning("bluetooth shutdown did not finish cleanly", exc_info=True)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2)
        return clean

    async def _shutdown(self) -> None:
        self._sup.cancel()
        try:
            await self._sup
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        had_link = self.sign is not None
        t0 = time.monotonic()
        await self._drop()
        if had_link:
            log.info("closed the Bluetooth link in %.2f s", time.monotonic() - t0)

    # -- status -------------------------------------------------------------------

    def _set(self, **kw) -> None:
        with self._st:
            before = self._state
            for k, v in kw.items():
                setattr(self, "_" + k, v)
            after = self._state
        if before != after:
            log.info("state %s -> %s (%s)", before, after, self._message)

    def _set_progress(self, fraction: float) -> None:
        with self._st:
            self._progress = max(0.0, min(1.0, float(fraction)))

    def status(self) -> dict:
        with self._st:
            return {
                "state": self._state,
                "message": self._message,
                "device": dict(self._device) if self._device else None,
                "busy": self._job is not None,
                "job": self._job,
                "progress": self._progress,
                "fake": self.fake,
                "last_error": self._last_error,
                "paused": self.paused,
                "brightness": self._brightness,
                "display_on": self._display_on,
            }

    def panel_size(self) -> tuple[int, int]:
        with self._st:
            if self._device:
                return int(self._device["width"]), int(self._device["height"])
        w, h = self.settings.get("last_width"), self.settings.get("last_height")
        if isinstance(w, int) and isinstance(h, int) and 0 < w <= 512 and 0 < h <= 512:
            return w, h
        return iledsign.DEFAULT_SIZE

    @staticmethod
    def _device_dict(found: Found) -> dict:
        return {"name": found.name, "address": found.address, "width": found.width, "height": found.height}

    # -- supervisor ---------------------------------------------------------------

    async def _sleep(self, seconds: float | None) -> None:
        try:
            if seconds is None:
                await self._wake.wait()
            else:
                await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    async def _supervisor(self) -> None:
        while True:
            try:
                if self.paused:
                    if self.sign is not None:
                        await self._drop()
                    self._set(state="disconnected", message=MSG["disconnected"], device=None)
                    await self._sleep(None)
                    continue
                if self.sign is not None:
                    if self.sign.is_connected:
                        await self._sleep(LINK_POLL)
                        continue
                    log.warning("link to the sign dropped; reconnecting")
                    await self._drop()
                    self._kick = True
                    self._set(state="searching", message="Connection lost. Reconnecting...", device=None)
                    continue
                await self._attempt()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("connection manager error")
                self._set(state="error", message=MSG["error"], last_error=str(e) or type(e).__name__,
                          device=None)
                await self._sleep(ERROR_RETRY)

    async def _attempt(self) -> None:
        kicked, self._kick = self._kick, False
        if kicked or self._state not in ("not_found", "bluetooth_off", "error"):
            self._set(state="searching", message=MSG["searching"], device=None)
        try:
            found = await self.transport.scan(self.settings.get("last_address"))
        except BluetoothOff as e:
            if not self.paused:
                self._set(state="bluetooth_off", message=MSG["bluetooth_off"], last_error=str(e), device=None)
                await self._sleep(BLUETOOTH_OFF_RETRY)
            return
        except SignNotFound:
            if self._grace_scans > 0:
                self._grace_scans -= 1
                log.info("not found on the first scan; scanning once more before reporting it")
                return
            if not self.paused:
                self._set(state="not_found", message=MSG["not_found"], device=None)
                await self._sleep(NOT_FOUND_PAUSE)
            return
        self._grace_scans = 0
        if self.paused:
            return
        log.info("found %s %s %dx%d", found.name, found.address, found.width, found.height)
        self._set(state="connecting", message=f"Connecting to {found.name}...", device=self._device_dict(found))
        sign = self.transport.make_sign(found, self._on_link_lost)
        try:
            async with self._lock:
                await asyncio.wait_for(sign.connect(), CONNECT_TIMEOUT)
        except asyncio.CancelledError:
            # Shutting down mid-connect: let go of the half-open link too.
            await self._close_quietly(sign)
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("connect to %s failed: %r", found.address, e)
            await self._close_quietly(sign)
            if not self.paused:
                self._set(state="error", message=MSG["error"], last_error=str(e) or type(e).__name__, device=None)
                await self._sleep(ERROR_RETRY)
            return
        if self.paused:
            await self._close_quietly(sign)
            return
        self.sign = sign
        self._connected.set()
        self._set(state="connected", message=f"Connected to {found.name}", device=self._device_dict(found),
                  last_error=None)
        if not self.fake:
            self.settings.update(last_address=found.address, last_name=found.name,
                                 last_width=found.width, last_height=found.height)

    def _on_link_lost(self, sign) -> None:
        def handle() -> None:
            if sign is self.sign:
                self._connected.clear()
                self._wake.set()

        try:
            self.loop.call_soon_threadsafe(handle)
        except RuntimeError:  # loop already closed during shutdown
            pass

    async def _close_quietly(self, sign) -> None:
        try:
            await asyncio.wait_for(sign.close(), 5)
        except Exception:  # noqa: BLE001
            log.debug("close failed", exc_info=True)

    async def _drop(self) -> None:
        sign, self.sign = self.sign, None
        self._connected.clear()
        if sign is not None:
            async with self._lock:  # lets a running upload finish first
                await self._close_quietly(sign)

    # -- connect / disconnect -----------------------------------------------------

    def _run(self, coro: Awaitable, timeout: float):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)  # type: ignore[arg-type]
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise

    async def _connect_request(self) -> None:
        self.paused = False
        if not (self.sign is not None and self.sign.is_connected) and self._state not in ("searching", "connecting"):
            self._kick = True
            self._set(state="searching", message=MSG["searching"], device=None)
        self._wake.set()

    async def _disconnect_request(self) -> None:
        self.paused = True
        self._wake.set()
        await self._drop()
        self._set(state="disconnected", message=MSG["disconnected"], device=None)

    def connect(self) -> dict:
        self._run(self._connect_request(), 5)
        return self.status()

    def fake_drop(self) -> dict:
        """Fake mode only: simulate the Bluetooth link dropping (to test auto-reconnect)."""
        async def drop() -> None:
            sign = self.sign
            if isinstance(sign, FakeSign) and sign.is_connected:
                sign._connected = False
                self._on_link_lost(sign)

        self._run(drop(), 5)
        return self.status()

    def disconnect(self) -> dict:
        self._run(self._disconnect_request(), 60)
        return self.status()

    # -- sign operations ----------------------------------------------------------

    async def _ensure_connected(self, timeout: float):
        if self.sign is not None and self.sign.is_connected:
            return self.sign
        await self._connect_request()
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError:
            raise SignError("not_connected", "The sign is not connected.") from None
        if self.sign is None:
            raise SignError("not_connected", "The sign is not connected.")
        return self.sign

    async def _op(self, size: tuple[int, int] | None, fn: Callable[[object], Awaitable]):
        sign = await self._ensure_connected(SEND_CONNECT_WAIT)
        async with self._lock:
            if sign is not self.sign or not sign.is_connected:
                raise SignError("not_connected", "The sign is not connected.")
            if size and (sign.width, sign.height) != tuple(size):
                raise SignError("bad_request", f"The picture is {size[0]}x{size[1]} but the sign is "
                                               f"{sign.width}x{sign.height}.")
            try:
                return await fn(sign)
            except (SignError, asyncio.CancelledError):
                raise
            except StorageFull:
                raise SignError("storage_full", "The sign is out of space. Try fewer frames.") from None
            except Exception as e:  # noqa: BLE001  (bleak / WinRT / OS errors)
                log.warning("sign operation failed: %r", e, exc_info=True)
                if not sign.is_connected:
                    self._connected.clear()
                    self._wake.set()
                    raise SignError("not_connected", "Lost the connection to the sign. Try again.") from None
                raise SignError("error", f"Sending failed: {e}") from None

    def run_job(self, job: str, fn: Callable[[object], Awaitable], *, size: tuple[int, int] | None = None,
                timeout: float = 60.0):
        """Run `fn(sign)` on the loop under the sign lock. Raises SignError('busy') if a job is running."""
        with self._st:
            if self._job is not None:
                raise SignError("busy", "The sign is busy with another upload. Try again in a moment.")
            self._job, self._progress = job, None
        try:
            return self._run(self._op(size, fn), timeout + SEND_CONNECT_WAIT)
        except TimeoutError:
            raise SignError("error", "The sign took too long to answer.") from None
        finally:
            with self._st:
                self._job, self._progress = None, None

    def upload(self, job: str, fn: Callable[[object, Callable[[float], None]], Awaitable[bool]], nbytes: int,
               size: tuple[int, int]) -> dict:
        t0 = time.monotonic()

        async def call(sign):
            self._set_progress(0.0)
            return await fn(sign, self._set_progress)

        ok = self.run_job(job, call, size=size, timeout=30 + nbytes / 2000)
        seconds = round(time.monotonic() - t0, 1)
        if not ok:
            raise SignError("no_confirm", "The sign didn't confirm. Try again.")
        log.info("%s: %d bytes in %.1f s", job, nbytes, seconds)
        return {"ok": True, "bytes": nbytes, "seconds": seconds}

    def brightness(self, level: int) -> dict:
        confirmed = self.run_job("brightness", lambda s: s.brightness(level), timeout=10)
        with self._st:
            self._brightness = level
        if not self.fake:
            self.settings.update(brightness=level)
        return {"ok": True, "confirmed": bool(confirmed)}

    def power(self, on: bool) -> dict:
        confirmed = self.run_job("power", lambda s: s.power(on), timeout=10)
        with self._st:
            self._display_on = on
        if not self.fake:
            self.settings.update(display_on=on)
        return {"ok": True, "confirmed": bool(confirmed)}
