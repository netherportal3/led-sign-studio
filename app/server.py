"""LED Sign Studio backend: local HTTP API + static UI + Bluetooth connection manager.

    python app\\server.py [--open] [--fake[=ok|not_found|bluetooth_off]] [--data-dir D]
    pythonw app\\server.py --port P --token T [--parent-pid N] [--fake...]   (how the desktop app runs it)
    pythonw app\\server.py --service                                         (the always-on service)

Binds 127.0.0.1 only. Run from a console, it prints the page address. Writes <data-dir>\\server_url.txt with http://127.0.0.1:P/?t=T once listening.
Logs go to <data-dir>\\logs\\server.log (never to a console stream: pythonw has none).
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(APP_DIR)
for p in (ROOT_DIR, APP_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import iledsign  # noqa: E402
from backend import fonts, media  # noqa: E402
from backend.media import BadRequest  # noqa: E402
from backend.signmgr import BleTransport, FakeTransport, SignError, SignManager  # noqa: E402
from backend.store import Settings, Store, atomic_write  # noqa: E402
from backend.ambient import autostart  # noqa: E402
from backend.ambient.service import AmbientController  # noqa: E402

UI_DIR = os.path.join(APP_DIR, "ui")
ASSETS_DIR = os.path.join(APP_DIR, "assets")
DEFAULT_DATA_DIR = os.path.join(APP_DIR, "data")
MAX_BODY = 64 * 1024 * 1024
MIN_SLIDE_SPEED = 1
MIN_SLIDE_DWELL = 5

MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
}

log = logging.getLogger("ledsign.server")
ui_log = logging.getLogger("ledsign.ui")


# ---------------------------------------------------------------- logging


def setup_logging(data_dir: str) -> None:
    logs = os.path.join(data_dir, "logs")
    os.makedirs(logs, exist_ok=True)
    logging.raiseExceptions = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s")
    h = logging.handlers.RotatingFileHandler(os.path.join(logs, "server.log"), maxBytes=1_000_000,
                                             backupCount=3, encoding="utf-8")
    h.setFormatter(fmt)
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(h)
    root.setLevel(logging.INFO)

    uh = logging.handlers.RotatingFileHandler(os.path.join(logs, "ui.log"), maxBytes=1_000_000,
                                              backupCount=2, encoding="utf-8")
    uh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    ui_log.addHandler(uh)
    ui_log.propagate = False
    ui_log.setLevel(logging.DEBUG)

    # pythonw has no console: stray writes from libraries must not crash anything.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.path.join(logs, "stderr.log"), "a", encoding="utf-8", buffering=1)

    def excepthook(t, v, tb):
        logging.getLogger("ledsign").critical("uncaught exception", exc_info=(t, v, tb))

    def thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        logging.getLogger("ledsign").error("uncaught exception in thread %s",
                                           args.thread.name if args.thread else "?",
                                           exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


# ---------------------------------------------------------------- parent watchdog


def watch_parent(pid: int, on_exit) -> None:
    """Call on_exit() when process `pid` ends (Windows: wait on a SYNCHRONIZE handle)."""
    if os.name != "nt":
        def poll() -> None:
            while True:
                try:
                    os.kill(pid, 0)
                except OSError:
                    on_exit()
                    return
                time.sleep(1)
        threading.Thread(target=poll, name="parent-watch", daemon=True).start()
        return

    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF

    handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == 87:  # ERROR_INVALID_PARAMETER: no such process
            log.warning("parent process %d is already gone; exiting", pid)
            on_exit()
        else:
            log.warning("cannot watch parent process %d (error %d); not watching", pid, err)
        return

    def wait() -> None:
        k32.WaitForSingleObject(handle, INFINITE)
        k32.CloseHandle(handle)
        log.info("parent process %d exited; shutting down", pid)
        on_exit()

    threading.Thread(target=wait, name="parent-watch", daemon=True).start()


# ---------------------------------------------------------------- app


class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.data_dir = os.path.abspath(args.data_dir)
        self.token = args.token
        self.fake = args.fake
        self.settings = Settings(os.path.join(self.data_dir, "settings.json"))
        self.store = Store(self.data_dir)
        if args.fake:
            transport = FakeTransport(args.fake, os.path.join(self.data_dir, "fake"))
        else:
            transport = BleTransport()
        self.mgr = SignManager(transport, self.settings)
        self.service = bool(getattr(args, "service", False))
        self.ambient = AmbientController(self.data_dir, self.mgr,
                                         run_scheduler=self.service or bool(getattr(args, "ambient", False)),
                                         fake=bool(args.fake))
        self.autostart = (autostart.FakeAutostart(os.path.join(self.data_dir, "fake", "autostart.json"))
                          if args.fake else autostart)
        self.port = 0
        self.httpd: ThreadingHTTPServer | None = None
        self._shutdown_once = threading.Lock()

    def shutdown(self, bt_timeout: float = 4.0, http_delay: float = 0.0) -> bool:
        """Close the Bluetooth link, then stop serving (main() exits the process).

        `http_delay` keeps serving a moment longer, so the request that asked for this can still
        get its answer. Returns True when the link was closed cleanly. Only the first call acts.
        """
        if not self._shutdown_once.acquire(blocking=False):
            return False
        try:
            self.ambient.stop()
        except Exception:  # noqa: BLE001
            log.exception("ambient stop failed")
        # Hard stop if anything below hangs.
        killer = threading.Timer(bt_timeout + 4.0, lambda: os._exit(0))
        killer.daemon = True
        killer.start()
        clean = False
        try:
            clean = self.mgr.stop(timeout=bt_timeout)
        except Exception:  # noqa: BLE001
            log.exception("bluetooth stop failed")
        if self.httpd:
            t = threading.Timer(http_delay, self.httpd.shutdown)
            t.name = "http-shutdown"
            t.daemon = True
            t.start()
        return clean

    # -- helpers ------------------------------------------------------------------

    def size_from(self, body: dict) -> tuple[int, int]:
        w, h = body.get("width"), body.get("height")
        if w is None and h is None:
            return self.mgr.panel_size()
        try:
            w, h = int(w), int(h)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise BadRequest("width and height must be numbers") from None
        if not (1 <= w <= 512 and 1 <= h <= 512):
            raise BadRequest("width and height must be 1 to 512")
        return w, h

    # -- endpoints ----------------------------------------------------------------

    def api_health(self, _q, _b):
        # pid: the venv python.exe is a launcher, so callers need the real interpreter's pid to stop it.
        return {"ok": True, "pid": os.getpid()}

    def api_status(self, _q, _b):
        return dict(self.mgr.status(), service=self.service)

    def api_connect(self, _q, _b):
        return self.mgr.connect()

    def api_disconnect(self, _q, _b):
        return self.mgr.disconnect()

    def api_shutdown(self, _q, b):
        """The app window is closing: let go of the sign cleanly, answer, then exit.

        Without this the window's job object kills the process with the link still open, and
        Windows holds that link for about 14 s, so the next launch could not find the sign.
        """
        if self.service and not b.get("force"):
            # The always-on service outlives app windows: a host attached to it must not stop it.
            log.info("shutdown requested in service mode: ignored (send force: true to stop)")
            return {"ok": True, "ignored": True, "service": True}
        log.info("shutdown requested")
        t0 = time.monotonic()
        clean = self.shutdown(bt_timeout=2.5, http_delay=0.3)
        seconds = round(time.monotonic() - t0, 2)
        log.info("shutdown: Bluetooth %s after %.2f s; exiting", "closed cleanly" if clean else "not closed cleanly",
                 seconds)
        return {"ok": True, "disconnected": clean, "seconds": seconds}

    def api_fake_drop(self, _q, _b):
        if not self.fake:
            raise NotFound("Only available with --fake.")
        return self.mgr.fake_drop()

    def api_fonts(self, _q, _b):
        return fonts.font_list(self.mgr.panel_size()[1])

    def api_text_mask(self, _q, b):
        text = b.get("text", "")
        if not isinstance(text, str):
            raise BadRequest("text must be a string")
        size = b.get("size")
        if size is not None and not isinstance(size, (int, float)):
            raise BadRequest("size must be a number")
        height = b.get("height") or self.mgr.panel_size()[1]
        if not isinstance(height, int) or not 1 <= height <= 512:
            raise BadRequest("height must be 1 to 512")
        info: dict = {}
        w, h, mask = fonts.text_mask(text, b.get("font"), size, height, info)
        return {"width": w, "height": h, "mask": media.b64(mask), "cropped": bool(info.get("cropped"))}

    def api_send_still(self, _q, b):
        w, h = self.size_from(b)
        frame = media.decode_frame(b.get("pixels"), w, h)
        nbytes = len(media.still_payload(frame, w, h))
        self.ambient.on_manual_send()
        return self.mgr.upload("send_still", lambda s, p: s.send_program([frame], progress=p), nbytes, (w, h))

    def api_send_animation(self, _q, b):
        w, h = self.size_from(b)
        frames = media.decode_frames(b.get("frames"), w, h, media.MAX_ANIMATION_FRAMES)
        durations = media.decode_durations(b.get("durations"), len(frames))
        self.ambient.on_manual_send()
        if len(frames) == 1:
            nbytes = len(media.still_payload(frames[0], w, h))
            return self.mgr.upload("send_still", lambda s, p: s.send_program([frames[0]], progress=p),
                                   nbytes, (w, h))
        gif = media.encode_gif(frames, durations, w, h)
        nbytes = len(iledsign.gif_payload(w, h, gif))
        return self.mgr.upload("send_animation", lambda s, p: s.send_gif(gif, progress=p), nbytes, (w, h))

    def api_send_slideshow(self, _q, b):
        w, h = self.size_from(b)
        frames = media.decode_frames(b.get("frames"), w, h, media.MAX_SLIDESHOW_FRAMES)
        transition = b.get("transition", "left")
        if transition not in iledsign.EFFECTS:
            raise BadRequest(f"transition must be one of {', '.join(iledsign.EFFECTS)}")
        effect = iledsign.EFFECTS[transition]
        # The sign's units for speed and dwell (hold) are unknown. Keep both away from 0, which
        # could stop playback, and dwell high enough that frames cannot flash by.
        try:
            speed = max(MIN_SLIDE_SPEED, min(255, int(b.get("speed", 3))))
            dwell = max(MIN_SLIDE_DWELL, min(255, int(b.get("dwell", 30))))
        except (TypeError, ValueError):
            raise BadRequest("speed and dwell must be numbers") from None
        nbytes = len(iledsign.program_payload(w, h, frames, effect=effect, speed=speed, dwell=dwell))
        self.ambient.on_manual_send()
        return self.mgr.upload(
            "send_slideshow",
            lambda s, p: s.send_program(frames, effect=effect, speed=speed, dwell=dwell, progress=p),
            nbytes, (w, h))

    def api_estimate(self, _q, b):
        w, h = self.size_from(b)
        frames = media.decode_frames(b.get("frames"), w, h, media.MAX_ANIMATION_FRAMES)
        durations = media.decode_durations(b.get("durations"), len(frames))
        return media.estimate(frames, durations, w, h)

    def api_import(self, _q, b):
        w, h = self.size_from(b)
        data = media.b64decode(b.get("data"), "data")
        if not data:
            raise BadRequest("The file is empty.")
        try:
            max_frames = int(b.get("max_frames") or 300)
        except (TypeError, ValueError):
            raise BadRequest("max_frames must be a number") from None
        out = media.import_media(data, w, h, str(b.get("fit") or "contain"), max_frames)
        log.info("imported %r: %d of %d frames", str(b.get("name", ""))[:80], len(out["frames"]),
                 out["source_frames"])
        return out

    def api_export_gif(self, _q, b):
        w, h = self.size_from(b)
        frames = media.decode_frames(b.get("frames"), w, h, media.MAX_ANIMATION_FRAMES)
        durations = media.decode_durations(b.get("durations"), len(frames))
        try:
            scale = max(1, min(12, int(b.get("scale", 1))))
        except (TypeError, ValueError):
            raise BadRequest("scale must be a number") from None
        return {"gif": media.b64(media.encode_gif(frames, durations, w, h, scale))}

    def api_autosave_get(self, _q, _b):
        return self.store.load_autosave()

    def api_autosave_post(self, _q, b):
        project = b
        if "frames" not in b and isinstance(b.get("project"), dict):
            project = b["project"]
        self.store.save_autosave(project)
        return {"ok": True}

    def api_library(self, _q, _b):
        return self.store.list()

    def api_library_load(self, q, _b):
        slug = (q.get("slug") or [""])[0]
        project = self.store.load(slug)
        if project is None:
            raise NotFound("That project is not in the library.")
        return project

    def api_library_save(self, _q, b):
        name = b.get("name")
        project = b.get("project")
        if not isinstance(name, str) or not name.strip():
            raise BadRequest("Give the project a name.")
        if not isinstance(project, dict) or not isinstance(project.get("frames"), list):
            raise BadRequest("project must be an object with frames")
        return {"slug": self.store.save(name, project)}

    def api_library_delete(self, _q, b):
        slug = b.get("slug")
        if not isinstance(slug, str):
            raise BadRequest("slug must be a string")
        return {"ok": self.store.delete(slug)}

    def api_brightness(self, _q, b):
        level = b.get("level")
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise BadRequest("level must be a number from 1 to 10")
        return self.mgr.brightness(max(1, min(10, int(round(level)))))

    def api_power(self, _q, b):
        on = b.get("on")
        if not isinstance(on, bool):
            raise BadRequest("on must be true or false")
        return self.mgr.power(on)

    def api_open_bluetooth_settings(self, _q, _b):
        if self.fake:
            log.info("open Bluetooth settings (skipped in fake mode)")
            return {"ok": True, "fake": True}
        try:
            os.startfile("ms-settings:bluetooth")  # type: ignore[attr-defined]
        except OSError as e:
            log.warning("could not open Bluetooth settings: %r", e)
            return {"ok": False, "code": "error", "message": "Could not open Bluetooth settings."}
        return {"ok": True}

    def api_log(self, _q, b):
        level = str(b.get("level", "info")).lower()
        message = str(b.get("message", ""))[:8000]
        lvl = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING,
               "warning": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO)
        ui_log.log(lvl, "%s", message)
        return {"ok": True}

    # -- ambient mode -----------------------------------------------------------------

    def api_ambient_settings_get(self, _q, _b):
        return self.ambient.get_settings()

    def api_ambient_settings_post(self, _q, b):
        return self.ambient.update_settings(b)

    def api_ambient_status(self, _q, _b):
        return self.ambient.status()

    def api_ambient_preview(self, _q, b):
        return self.ambient.preview(b.get("minutes", 1))

    def api_ambient_run_now(self, _q, _b):
        return self.ambient.run_now()

    def api_ambient_pause(self, _q, b):
        return self.ambient.pause(b.get("minutes"))

    def api_ambient_resume(self, _q, _b):
        return self.ambient.resume()

    def api_ambient_geocode(self, q, _b):
        query = (q.get("q") or [""])[0].strip()
        if not query:
            return []
        if len(query) > 100:
            raise BadRequest("The search is too long.")
        try:
            return self.ambient.geocode(query)
        except (OSError, ValueError) as e:
            log.warning("geocode failed: %r", e)
            raise SignError("error", "Could not search places right now. Check the internet connection.") from None

    def api_autostart_get(self, _q, _b):
        return self.autostart.query()

    def api_autostart_post(self, _q, b):
        enabled = b.get("enabled")
        if not isinstance(enabled, bool):
            raise BadRequest("enabled must be true or false")
        try:
            return self.autostart.set_enabled(enabled)
        except (RuntimeError, OSError) as e:
            log.warning("autostart change failed: %s", e)
            raise SignError("error", "Could not change the Windows login task. See the log.") from None

    ROUTES = {
        ("GET", "/api/health"): "api_health",
        ("GET", "/api/status"): "api_status",
        ("POST", "/api/connect"): "api_connect",
        ("POST", "/api/disconnect"): "api_disconnect",
        ("GET", "/api/fonts"): "api_fonts",
        ("POST", "/api/text_mask"): "api_text_mask",
        ("POST", "/api/send_still"): "api_send_still",
        ("POST", "/api/send_animation"): "api_send_animation",
        ("POST", "/api/send_slideshow"): "api_send_slideshow",
        ("POST", "/api/estimate"): "api_estimate",
        ("POST", "/api/import"): "api_import",
        ("POST", "/api/export_gif"): "api_export_gif",
        ("GET", "/api/autosave"): "api_autosave_get",
        ("POST", "/api/autosave"): "api_autosave_post",
        ("GET", "/api/library"): "api_library",
        ("GET", "/api/library/load"): "api_library_load",
        ("POST", "/api/library/save"): "api_library_save",
        ("POST", "/api/library/delete"): "api_library_delete",
        ("POST", "/api/brightness"): "api_brightness",
        ("POST", "/api/power"): "api_power",
        ("POST", "/api/open_bluetooth_settings"): "api_open_bluetooth_settings",
        ("POST", "/api/log"): "api_log",
        ("POST", "/api/fake_drop"): "api_fake_drop",  # --fake only: simulate a dropped link
        ("POST", "/api/shutdown"): "api_shutdown",  # the host calls this when its window closes
        ("GET", "/api/ambient/settings"): "api_ambient_settings_get",
        ("POST", "/api/ambient/settings"): "api_ambient_settings_post",
        ("GET", "/api/ambient/status"): "api_ambient_status",
        ("POST", "/api/ambient/preview"): "api_ambient_preview",
        ("POST", "/api/ambient/run_now"): "api_ambient_run_now",
        ("POST", "/api/ambient/pause"): "api_ambient_pause",
        ("POST", "/api/ambient/resume"): "api_ambient_resume",
        ("GET", "/api/ambient/geocode"): "api_ambient_geocode",
        ("GET", "/api/autostart"): "api_autostart_get",
        ("POST", "/api/autostart"): "api_autostart_post",
    }


class NotFound(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "LedSignStudio/1"
    protocol_version = "HTTP/1.1"
    app: App  # set on the class before serving

    # -- plumbing -----------------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # default writes to stderr
        log.debug("%s " + fmt, self.address_string(), *args)

    def log_error(self, fmt: str, *args) -> None:
        log.info("http error: " + fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, err: str, message: str) -> None:
        self._json(code, {"ok": False, "code": err, "message": message})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.app.port
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _drain_body(self) -> None:
        # Keep-alive needs the unread body consumed before answering an early rejection.
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= MAX_BODY:
            self.rfile.read(n)
        elif n > MAX_BODY:
            self.close_connection = True

    # -- verbs --------------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._drain_body()
        self._error(405, "bad_request", "Method not allowed")

    do_DELETE = do_PUT
    do_PATCH = do_PUT

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                self._drain_body()
                log.warning("rejected request with Host %r", self.headers.get("Host"))
                self._error(403, "forbidden", "Bad host")
                return
            url = urlsplit(self.path)
            path = url.path
            if path == "/api" or path.startswith("/api/"):
                self._api(method, path, parse_qs(url.query))
            elif method in ("GET", "HEAD"):
                self._static(path)
            else:
                self._drain_body()
                self._error(405, "bad_request", "Method not allowed")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            log.exception("unhandled error for %s %s", method, self.path)
            try:
                self._error(500, "error", "Something went wrong. See the log.")
            except Exception:  # noqa: BLE001
                self.close_connection = True

    def _api(self, method: str, path: str, query: dict) -> None:
        token = self.headers.get("X-Token") or ""
        if not hmac.compare_digest(token.encode("utf-8"), self.app.token.encode("utf-8")):
            self._drain_body()
            self._error(403, "forbidden", "Missing or wrong token")
            return
        name = App.ROUTES.get(("GET" if method == "HEAD" else method, path))
        if not name:
            self._drain_body()
            known = any(p == path for (_m, p) in App.ROUTES)
            self._error(405 if known else 404, "bad_request", "Method not allowed" if known else "Unknown endpoint")
            return
        body: dict = {}
        if method == "POST":
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.close_connection = True
                self._error(400, "bad_request", "Bad Content-Length")
                return
            if n > MAX_BODY:
                self.close_connection = True
                self._error(413, "bad_request", "The request is too large.")
                return
            raw = self.rfile.read(n) if n else b""
            if raw.strip():
                try:
                    parsed = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    self._error(400, "bad_request", "The request body is not valid JSON.")
                    return
                if not isinstance(parsed, dict):
                    self._error(400, "bad_request", "The request body must be a JSON object.")
                    return
                body = parsed
        t0 = time.monotonic()
        try:
            result = getattr(self.app, name)(query, body)
        except BadRequest as e:
            log.info("%s %s: bad request: %s", method, path, e)
            self._error(400, "bad_request", str(e))
            return
        except NotFound as e:
            self._error(404, "bad_request", str(e))
            return
        except SignError as e:
            log.info("%s %s: %s: %s", method, path, e.code, e.message)
            self._error(200, e.code, e.message)
            return
        dt = time.monotonic() - t0
        if path not in ("/api/status", "/api/health", "/api/autosave", "/api/log", "/api/ambient/status"):
            log.info("%s %s %.0f ms", method, path, dt * 1000)
        self._json(200, result)

    def _static(self, path: str) -> None:
        rel = unquote(path)
        if rel in ("", "/"):
            base, rel = UI_DIR, "index.html"
        elif rel.startswith("/assets/"):
            base, rel = ASSETS_DIR, rel[len("/assets/"):]
        else:
            base, rel = UI_DIR, rel.lstrip("/")
        parts = rel.split("/")
        # No parent hops, Windows separators, drive letters or alternate data streams.
        if any(p == ".." for p in parts) or any(c in rel for c in ("\\", "\x00", ":")):
            self._error(404, "bad_request", "Not found")
            return
        root = os.path.realpath(base)
        full = os.path.realpath(os.path.join(root, *[p for p in parts if p]))
        if os.path.commonpath([full, root]) != root:
            self._error(404, "bad_request", "Not found")
            return
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            self._error(404, "bad_request", "Not found")
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # never share a port with another process on Windows
    request_queue_size = 64

    def handle_error(self, request, client_address) -> None:  # default prints to stderr
        log.warning("connection error from %s:\n%s", client_address, traceback.format_exc())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="LED Sign Studio backend")
    ap.add_argument("--port", type=int, default=0, help="0 picks a free port")
    ap.add_argument("--token", default=None, help="required X-Token for /api (random if omitted)")
    ap.add_argument("--parent-pid", type=int, default=None, help="exit when this process exits")
    ap.add_argument("--fake", nargs="?", const="ok", default=None, choices=["ok", "not_found", "bluetooth_off"],
                    help="no Bluetooth: a simulated sign")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--service", action="store_true",
                    help="always-on mode: fixed port, service.json, ambient scheduler, ignores /api/shutdown")
    ap.add_argument("--ambient", action="store_true", help="run the ambient scheduler without --service")
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    return ap.parse_args(argv)


def console_print(text: str) -> None:
    """Print only when run from a console (never under pythonw or the desktop app)."""
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            print(text, flush=True)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------- service mode and the BLE owner lock

LOCK_PATH = os.path.join(DEFAULT_DATA_DIR, "backend.lock")
EXIT_LOCKED = 3
_lock_handle = None


def acquire_backend_lock(path: str = LOCK_PATH) -> tuple[bool, str]:
    """One real-Bluetooth backend per machine: an exclusive byte lock held until the process exits.

    The file holds the owner's pid as text (readable by others; the locked byte is past it).
    Returns (acquired, owner pid text).
    """
    global _lock_handle
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(4096)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.lockf(fh, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 4096)
    except OSError:
        try:
            # unbuffered and short: a buffered read would reach the locked byte and fail
            with open(path, "rb", buffering=0) as r:
                owner = r.read(32).decode("ascii", "replace").strip()
        except OSError:
            owner = "?"
        fh.close()
        return False, owner
    fh.seek(0)
    fh.truncate(0)
    fh.write(f"{os.getpid()}\n".encode())
    fh.flush()
    _lock_handle = fh
    return True, str(os.getpid())


def read_service_json(data_dir: str) -> dict:
    try:
        with open(os.path.join(data_dir, "service.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.data_dir = os.path.abspath(args.data_dir)
    os.makedirs(args.data_dir, exist_ok=True)
    setup_logging(args.data_dir)
    log.info("starting pid=%d fake=%s service=%s data=%s python=%s", os.getpid(), args.fake, args.service,
             args.data_dir, sys.version.split()[0])

    if not args.fake:
        ok, owner = acquire_backend_lock()
        if not ok:
            log.warning("another backend (pid %s) owns the sign; exiting with code %d", owner, EXIT_LOCKED)
            console_print(f"Another LED Sign Studio backend (pid {owner}) is using the sign. Use the app "
                          "window, which attaches to it, or stop it first. Add --fake to try the app "
                          "without the sign.")
            logging.shutdown()
            return EXIT_LOCKED
    saved = read_service_json(args.data_dir) if args.service else {}
    if args.service:
        if not args.port and isinstance(saved.get("port"), int):
            args.port = saved["port"]
        if not args.token and isinstance(saved.get("token"), str) and len(saved["token"]) >= 16:
            args.token = saved["token"]
    if not args.token:
        args.token = secrets.token_urlsafe(18)

    app = App(args)
    Handler.app = app
    try:
        httpd = Server(("127.0.0.1", args.port), Handler)
    except OSError as e:
        if not args.service or not args.port:
            raise
        log.warning("saved port %d is not free (%r); picking a new one", args.port, e)
        httpd = Server(("127.0.0.1", 0), Handler)
    app.httpd = httpd
    app.port = httpd.server_address[1]
    url = f"http://127.0.0.1:{app.port}/?t={app.token}"

    app.mgr.start()
    app.ambient.start()
    if args.parent_pid:
        watch_parent(args.parent_pid, app.shutdown)
    if args.service:
        atomic_write(os.path.join(args.data_dir, "service.json"), json.dumps({
            "port": app.port, "token": app.token, "pid": os.getpid(),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "url": url, "fake": bool(args.fake)}, indent=2))
    atomic_write(os.path.join(args.data_dir, "server_url.txt"), url)
    log.info("listening on http://127.0.0.1:%d", app.port)
    console_print(f"LED Sign Studio is running at {url}\nPress Ctrl+C to stop.")
    if args.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            log.warning("could not open the browser", exc_info=True)
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        httpd.server_close()
        log.info("stopped")
        logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
