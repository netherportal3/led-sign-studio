"""Backend self-check. Starts the server in fake mode and exercises every endpoint.

    .venv\\Scripts\\python app\\backend\\selftest.py [--quick]

Uses its own data dir (app\\data\\test-backend). Never talks to a real sign. Exit code 0 = all passed.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

from PIL import Image, ImageDraw

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(APP_DIR)
sys.path.insert(0, ROOT_DIR)
import iledsign  # noqa: E402
SERVER = os.path.join(APP_DIR, "server.py")
DATA_DIR = os.path.join(APP_DIR, "data", "test-backend")
# The Python running this test (the repo's .venv when run as documented), and its pythonw.exe.
VENV_PY = sys.executable
VENV_PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.isfile(VENV_PYW):
    VENV_PYW = sys.executable
# A sample place for the service tests (ambient mode stays off without one).
TEST_PLACE = {"name": "Example City", "lat": 51.5072, "lon": -0.1276, "timezone": "Europe/London"}
W, H = 64, 16

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    return bool(cond)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(s: str) -> bytes:
    return base64.b64decode(s)


# ---------------------------------------------------------------- server process


class Server:
    def __init__(self, fake: str | None = "ok", parent_pid: int | None = None, python: str = VENV_PY,
                 data_dir: str = DATA_DIR, extra: list[str] | None = None, token: str | None = "random") -> None:
        self.token = secrets.token_urlsafe(12) if token == "random" else token
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.url_file = os.path.join(data_dir, "server_url.txt")
        before = os.path.getmtime(self.url_file) if os.path.exists(self.url_file) else 0
        cmd = [python, SERVER, "--data-dir", data_dir]
        if "--service" not in (extra or []):
            cmd += ["--port", "0"]
        if self.token:
            cmd += ["--token", self.token]
        if fake:
            cmd.append(f"--fake={fake}")
        if parent_pid:
            cmd += ["--parent-pid", str(parent_pid)]
        cmd += extra or []
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     cwd=ROOT_DIR)
        deadline = time.time() + 20
        self.url = None
        while time.time() < deadline:
            if os.path.exists(self.url_file) and os.path.getmtime(self.url_file) > before:
                text = open(self.url_file, encoding="utf-8").read().strip()
                if self.token is None and "?t=" in text:
                    self.token = text.split("?t=", 1)[1]
                if text.endswith(f"?t={self.token}"):
                    self.url = text
                    break
            time.sleep(0.1)
        if not self.url:
            raise RuntimeError("server did not write server_url.txt")
        self.base = self.url.split("/?t=")[0]
        self.port = int(self.base.rsplit(":", 1)[1])
        self.pid = self.get("/api/health")["pid"]

    def request(self, method: str, path: str, body=None, token: bool = True, host: str | None = None,
                raw: bytes | None = None, timeout: float = 120) -> tuple[int, dict | list | bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        headers = {}
        if token:
            headers["X-Token"] = self.token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        for k, v in headers.items():
            conn.putheader(k, v)
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders()
        if data is not None:
            conn.send(data)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type") or ""
        if "json" in ctype:
            return resp.status, json.loads(payload)
        return resp.status, payload

    def get(self, path: str, **kw):
        code, out = self.request("GET", path, **kw)
        if code != 200:
            raise RuntimeError(f"GET {path} -> {code} {out!r}")
        return out

    def post(self, path: str, body=None, **kw):
        code, out = self.request("POST", path, body if body is not None else {}, **kw)
        if code != 200:
            raise RuntimeError(f"POST {path} -> {code} {out!r}")
        return out

    def wait_state(self, states: tuple[str, ...], timeout: float) -> dict:
        deadline = time.time() + timeout
        st = {}
        while time.time() < deadline:
            st = self.get("/api/status")
            if st["state"] in states:
                return st
            time.sleep(0.1)
        return st

    def alive(self) -> bool:
        try:
            self.request("GET", "/api/health", timeout=2)
            return True
        except OSError:
            return False

    def stop(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)  # TerminateProcess on Windows
        except OSError:
            pass
        try:
            self.proc.kill()
        except OSError:
            pass
        self.proc.wait(10)
        deadline = time.time() + 5
        while time.time() < deadline and self.alive():
            time.sleep(0.1)


# ---------------------------------------------------------------- test data


def solid(rgb) -> bytes:
    return bytes(rgb) * (W * H)


def moving_dot_frames(n: int) -> list[bytes]:
    frames = []
    for i in range(n):
        im = Image.new("RGB", (W, H))
        d = ImageDraw.Draw(im)
        x = i % W
        # the color repeats every 200 frames: at most 202 colors, so the shared GIF palette must be exact
        d.rectangle((x, 0, x, H - 1), fill=((i * 7) % 200, 80, 200 - (i % 100)))
        d.point(((i * 3) % W, (i // 4) % H), fill=(255, 255, 0))
        frames.append(im.tobytes())
    return frames


def make_animated_gif() -> bytes:
    """8 frames, 32x32, a red square moving over a transparent background, varied durations."""
    frames = []
    for i in range(8):
        im = Image.new("P", (32, 32), 0)
        im.putpalette([0, 255, 0] + [255, 0, 0] + [0, 0, 255] + [0, 0, 0] * 253)
        d = ImageDraw.Draw(im)
        d.rectangle((i * 3, 8, i * 3 + 7, 15), fill=1)
        d.rectangle((0, 28, 31, 31), fill=2)
        frames.append(im)
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=[50, 50, 0, 100, 100, 200, 20, 80], loop=0, transparency=0, disposal=2)
    return buf.getvalue()


def make_png() -> bytes:
    im = Image.new("RGBA", (128, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 63, 31), fill=(255, 0, 0, 255))
    d.rectangle((64, 0, 127, 31), fill=(0, 0, 255, 128))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- tests


def test_security(s: Server) -> None:
    print("security")
    code, out = s.request("GET", "/api/status", token=False)
    check("403 without token", code == 403, f"{code} {out!r}")
    conn =http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
    conn.request("GET", "/api/status", headers={"X-Token": "wrong"})
    r = conn.getresponse()
    r.read()
    check("403 with wrong token", r.status == 403, str(r.status))
    code, _ = s.request("GET", "/api/status", host="evil.example:80")
    check("403 with bad Host header", code == 403, str(code))
    code, _ = s.request("GET", "/api/status", host=f"localhost:{s.port}")
    check("localhost Host accepted", code == 200, str(code))
    code, _ = s.request("GET", "/api/status", host="127.0.0.1")
    check("Host without port rejected", code == 403, str(code))
    code, body = s.request("GET", "/", token=False)
    ui_exists = os.path.exists(os.path.join(APP_DIR, "ui", "index.html"))
    check("GET / without token", code == (200 if ui_exists else 404), f"{code}, ui exists={ui_exists}")
    code, _ = s.request("GET", "/", token=False, host="attacker.test")
    check("static with bad Host rejected", code == 403, str(code))
    for path in ("/../server.py", "/%2e%2e/server.py", "/..%5cserver.py", "/assets/../server.py",
                 "/C:/Windows/win.ini", "/index.html::$DATA"):
        code, _ = s.request("GET", path, token=False)
        check(f"traversal blocked {path}", code == 404, str(code))
    if os.path.exists(os.path.join(APP_DIR, "assets", "icon.png")):
        code, body = s.request("GET", "/assets/icon.png", token=False)
        check("asset served", code == 200 and isinstance(body, bytes) and body[:4] == b"\x89PNG", str(code))
    code, out = s.request("POST", "/api/send_still", raw=b"{not json")
    check("bad JSON is 400 bad_request", code == 400 and out.get("code") == "bad_request", f"{code} {out!r}")
    code, out = s.request("POST", "/api/send_still", raw=b"[1,2]")
    check("non-object JSON is 400", code == 400, str(code))
    code, out = s.request("GET", "/api/nope")
    check("unknown endpoint 404", code == 404, str(code))
    code, out = s.request("GET", "/api/send_still")
    check("wrong method 405", code == 405, str(code))


def test_status(s: Server) -> None:
    print("status / connection (fake ok)")
    st = s.get("/api/status")
    keys = {"state", "message", "device", "busy", "job", "progress", "fake", "last_error"}
    check("status has all fields", keys <= set(st), str(sorted(st)))
    check("status fake=true", st["fake"] is True)
    st = s.wait_state(("connected",), 6)
    check("fake connects", st["state"] == "connected", st.get("state", ""))
    dev = st.get("device") or {}
    check("device is iledcolor-FAKE 64x16", dev.get("name") == "iledcolor-FAKE" and dev.get("width") == 64
          and dev.get("height") == 16, str(dev))
    check("health", s.get("/api/health").get("ok") is True)


def test_fonts(s: Server) -> None:
    print("fonts / text_mask")
    fl = s.get("/api/fonts")
    ids = [f["id"] for f in fl]
    check("pixel fonts first", ids[:2] == ["pixel5x7", "tiny3x5"], str(ids[:3]))
    check("font entries well formed", all({"id", "label", "kind", "default_size"} <= set(f) for f in fl))
    ttf = [f for f in fl if f["kind"] == "ttf"]
    check("windows fonts listed", len(ttf) >= 3, str([f["id"] for f in ttf]))

    def mask(text, font, size):
        out = s.post("/api/text_mask", {"text": text, "font": font, "size": size})
        m = unb64(out["mask"])
        return out["width"], out["height"], m

    w, h, m = mask("Hello", "pixel5x7", 1)
    check("5x7 mask size", h == 16 and len(m) == w * h and set(m) <= {0, 255} and 20 <= w <= 30, f"{w}x{h}")
    rows = [y for y in range(h) if any(m[y * w:(y + 1) * w])]
    check("5x7 text vertically centered", rows and rows[0] >= 3 and rows[-1] <= 12, str(rows))
    w2, h2, m2 = mask("Hello", "pixel5x7", 2)
    check("5x7 scale 2 doubles width", w2 == 2 * w, f"{w2} vs {w}")
    printable = "".join(chr(c) for c in range(32, 127))
    w, h, m = mask(printable, "pixel5x7", 1)
    check("5x7 covers ASCII 32-126", w > 300 and any(m), str(w))
    w, h, m = mask("TWO\nLINES", "tiny3x5", 1)
    rows = [y for y in range(h) if any(m[y * w:(y + 1) * w])]
    bands = sum(1 for i, y in enumerate(rows) if i == 0 or rows[i - 1] != y - 1)
    check("tiny 3x5 two lines fit in 16 rows", h == 16 and bands == 2 and len(rows) == 10, f"rows={rows}")
    w, h, m = mask("ONE\nTWO", "pixel5x7", 1)
    rows = [y for y in range(h) if any(m[y * w:(y + 1) * w])]
    check("5x7 two lines fit", h == 16 and len(rows) >= 13 and rows[0] >= 0 and rows[-1] <= 15, str(rows))
    w, h, m = mask("Hi there", ttf[0]["id"], ttf[0]["default_size"])
    check("ttf mask renders crisp", h == 16 and set(m) <= {0, 255} and any(m) and w > 30, f"{w}x{h}")
    w, h, m = mask("", "pixel5x7", 1)
    check("empty text gives a blank mask", h == 16 and w >= 1 and not any(m), f"{w}x{h}")
    w, h, m = mask("x", "no-such-font", 1)
    check("unknown font falls back", h == 16 and any(m))


def wait_busy(s: Server, seen: dict, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            st = s.get("/api/status")
        except Exception:  # noqa: BLE001
            return
        if st["busy"]:
            seen["busy"] = True
            seen["job"] = st["job"]
            p = st["progress"]
            if p is not None and 0 < p < 1:
                seen["mid"] = True
        time.sleep(0.05)


def test_sends(s: Server) -> None:
    print("sends")
    fake_dir = os.path.join(DATA_DIR, "fake")
    px = solid((10, 40, 80))
    out = s.post("/api/send_still", {"pixels": b64(px)})
    check("send_still ok", out.get("ok") is True and out.get("bytes", 0) > W * H * 3 and "seconds" in out, str(out))
    im = Image.open(os.path.join(fake_dir, "last.png")).convert("RGB")
    check("fake last.png matches", im.size == (W, H) and im.tobytes() == px)

    code, out = s.request("POST", "/api/send_still", {"pixels": b64(b"\x00" * 10)})
    check("wrong pixel length is bad_request", code == 400 and out["code"] == "bad_request", f"{code} {out}")

    frames = moving_dot_frames(300)
    seen: dict = {}
    stop = threading.Event()
    t = threading.Thread(target=wait_busy, args=(s, seen, stop))
    t.start()
    t0 = time.time()
    out = s.post("/api/send_animation", {"frames": [b64(f) for f in frames], "durations": [30] * 300})
    stop.set()
    t.join()
    check("300-frame animation ok", out.get("ok") is True, str(out))
    check("progress reported while uploading", seen.get("busy") and seen.get("mid") and
          seen.get("job") == "send_animation", str(seen))
    gif = Image.open(os.path.join(fake_dir, "last.gif"))
    check("last.gif has 300 frames", gif.n_frames == 300, str(gif.n_frames))
    check("last.gif is 64x16", gif.size == (W, H), str(gif.size))
    gif.seek(0)
    first = gif.convert("RGB").tobytes()
    check("last.gif first frame exact colors", first == frames[0])
    gif.seek(299)
    last = gif.convert("RGB").tobytes()
    check("last.gif last frame exact colors", last == frames[299])
    st = s.get("/api/status")
    check("not busy after upload", st["busy"] is False and st["job"] is None and st["progress"] is None, str(st))
    print(f"    300 frames: {out.get('bytes')} bytes, {out.get('seconds')} s fake upload, {time.time() - t0:.1f} s total")

    many = []
    for i in range(6):
        im = Image.new("RGB", (W, H))
        im.putdata([((x * 4 + i) % 256, (y * 16) % 256, (x * y + i * 40) % 256) for y in range(H) for x in range(W)])
        many.append(im.tobytes())
    out = s.post("/api/send_animation", {"frames": [b64(f) for f in many], "durations": [60] * 6})
    gif = Image.open(os.path.join(fake_dir, "last.gif"))
    check("more than 256 colors still encodes", out.get("ok") is True and gif.n_frames == 6, str(out))

    out = s.post("/api/send_animation", {"frames": [b64(f) for f in frames[:5]], "durations": [10, 20, 30, 45, 0]})
    gif = Image.open(os.path.join(fake_dir, "last.gif"))
    ds = []
    for i in range(gif.n_frames):
        gif.seek(i)
        ds.append(gif.info.get("duration"))
    check("durations clamped to >= 30 ms", out.get("ok") and all(d >= 30 for d in ds), str(ds))

    out = s.post("/api/send_animation", {"frames": [b64(px)], "durations": [100]})
    meta = json.load(open(os.path.join(fake_dir, "last.json")))
    check("1-frame animation sends a still", out.get("ok") and meta["kind"] == "still", str(meta))

    # busy: a second upload while one runs
    big = moving_dot_frames(200)
    holder: dict = {}

    def long_send():
        holder["out"] = s.post("/api/send_animation", {"frames": [b64(f) for f in big], "durations": 40})

    t = threading.Thread(target=long_send)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline and not s.get("/api/status")["busy"]:
        time.sleep(0.02)
    out = s.post("/api/send_still", {"pixels": b64(px)})
    check("second upload while busy -> busy", out.get("ok") is False and out.get("code") == "busy", str(out))
    t.join()
    check("first upload still succeeded", holder.get("out", {}).get("ok") is True, str(holder.get("out")))

    slides = [solid((60, 0, 0)), solid((0, 60, 0)), solid((0, 0, 60))]
    out = s.post("/api/send_slideshow", {"frames": [b64(f) for f in slides], "transition": "left",
                                         "speed": 1, "dwell": 10})
    meta = json.load(open(os.path.join(fake_dir, "last.json")))
    check("slideshow ok", out.get("ok") is True and meta["kind"] == "slideshow" and meta["frames"] == 3
          and meta["effect"] == 1, f"{out} {meta}")
    code, out = s.request("POST", "/api/send_slideshow", {"frames": [b64(px)] * 31, "transition": "left"})
    check("slideshow > 30 frames rejected", code == 400, f"{code} {out}")
    code, out = s.request("POST", "/api/send_slideshow", {"frames": [b64(px)] * 2, "transition": "spin"})
    check("bad transition rejected", code == 400, f"{code} {out}")


def test_estimate_export(s: Server) -> None:
    print("estimate / export")
    frames = moving_dot_frames(20)
    est = s.post("/api/estimate", {"frames": [b64(f) for f in frames], "durations": [50] * 20})
    check("estimate fields", est.get("bytes", 0) > 0 and abs(est["seconds"] - (est["bytes"] / 6500 + 1)) < 0.06,
          str(est))
    est1 = s.post("/api/estimate", {"frames": [b64(frames[0])], "durations": [50]})
    check("estimate 1 frame = still size", est1["bytes"] > W * H * 3, str(est1))
    for scale in (1, 8):
        out = s.post("/api/export_gif", {"frames": [b64(f) for f in frames], "durations": [50] * 20,
                                         "scale": scale})
        gif = Image.open(io.BytesIO(unb64(out["gif"])))
        check(f"export x{scale} size and frames", gif.size == (W * scale, H * scale) and gif.n_frames == 20,
              f"{gif.size} {gif.n_frames}")
        gif.seek(3)
        small = gif.convert("RGB").resize((W, H), Image.NEAREST).tobytes()
        check(f"export x{scale} nearest-neighbor exact", small == frames[3])
    out = s.post("/api/export_gif", {"frames": [b64(frames[0])], "durations": [50], "scale": 99})
    gif = Image.open(io.BytesIO(unb64(out["gif"])))
    check("export scale clamped to 12", gif.size == (W * 12, H * 12), str(gif.size))


def test_import(s: Server) -> None:
    print("import")
    data = make_animated_gif()
    out = s.post("/api/import", {"name": "anim.gif", "data": b64(data), "fit": "contain", "max_frames": 300})
    check("gif import frame count", len(out["frames"]) == 8 and out["source_frames"] == 8, str(out["source_frames"]))
    # 0 ms plays as 100 ms (browser rule), 20 ms is raised to the sign's 30 ms floor
    check("gif import durations", out["durations"] == [50, 50, 100, 100, 100, 200, 30, 80], str(out["durations"]))
    check("gif import size", out["width"] == W and out["height"] == H)
    f0 = unb64(out["frames"][0])
    check("frame byte length", len(f0) == W * H * 3)
    im = Image.frombytes("RGB", (W, H), f0)
    # contain: 32x32 -> 16x16 centered at x=24..39; transparent -> black; red square near the left
    check("transparency over black", im.getpixel((30, 1)) == (0, 0, 0) and im.getpixel((0, 8)) == (0, 0, 0),
          str(im.getpixel((30, 1))))
    check("contain letterboxes", im.getpixel((10, 8)) == (0, 0, 0) and im.getpixel((25, 5)) == (255, 0, 0),
          f"{im.getpixel((10, 8))} {im.getpixel((25, 5))}")
    im7 = Image.frombytes("RGB", (W, H), unb64(out["frames"][7]))
    check("later frames composited (blue bar kept)", im7.getpixel((30, 15)) == (0, 0, 255), str(im7.getpixel((30, 15))))
    out = s.post("/api/import", {"name": "anim.gif", "data": b64(data), "fit": "stretch", "max_frames": 4})
    check("max_frames samples evenly, keeps loop time", len(out["frames"]) == 4 and sum(out["durations"]) == 700,
          str(out["durations"]))
    im = Image.frombytes("RGB", (W, H), unb64(out["frames"][0]))
    check("stretch fills width", im.getpixel((63, 15)) == (0, 0, 255), str(im.getpixel((63, 15))))

    png = make_png()
    for fit, probe, want in (("contain", (0, 0), (255, 0, 0)), ("cover", (0, 0), (255, 0, 0)),
                             ("stretch", (63, 0), (0, 0, 128))):
        out = s.post("/api/import", {"name": "pic.png", "data": b64(png), "fit": fit, "max_frames": 300})
        im = Image.frombytes("RGB", (W, H), unb64(out["frames"][0]))
        px = im.getpixel(probe)
        check(f"png import {fit}", len(out["frames"]) == 1 and out["source_frames"] == 1 and
              all(abs(a - b) <= 2 for a, b in zip(px, want)), f"{px} want {want}")
    code, out = s.request("POST", "/api/import", {"name": "x.gif", "data": b64(b"not an image"), "fit": "contain"})
    check("garbage import is bad_request", code == 400 and out["code"] == "bad_request", f"{code} {out}")
    code, out = s.request("POST", "/api/import", {"name": "x.png", "data": b64(png), "fit": "zoom"})
    check("bad fit is bad_request", code == 400, str(code))


def test_storage(s: Server) -> None:
    print("autosave / library")
    frames = moving_dot_frames(3)
    project = {"version": 1, "name": "Auto", "width": W, "height": H,
               "frames": [{"pixels": b64(f), "ms": 100} for f in frames], "current": 1}
    out = s.post("/api/autosave", project)
    check("autosave post", out.get("ok") is True, str(out))
    check("autosave round-trip", s.get("/api/autosave") == project)

    name = "Selftest Project"
    out = s.post("/api/library/save", {"name": name, "project": project})
    slug = out.get("slug")
    check("library save returns slug", slug == "selftest-project", str(out))
    lib = s.get("/api/library")
    entry = next((e for e in lib if e["slug"] == slug), None)
    check("library lists it", entry is not None and entry["name"] == name and entry["frames"] == 3 and
          entry["width"] == W and entry["height"] == H and entry["thumb"] == project["frames"][0]["pixels"] and
          entry.get("updated"), str(entry and {k: v for k, v in entry.items() if k != "thumb"}))
    loaded = s.get(f"/api/library/load?slug={slug}")
    check("library load", loaded.get("frames") == project["frames"] and loaded.get("name") == name)
    code, _ = s.request("GET", "/api/library/load?slug=..%2Fsettings")
    check("library load rejects bad slug", code == 404, str(code))
    out = s.post("/api/library/delete", {"slug": slug})
    check("library delete", out.get("ok") is True and all(e["slug"] != slug for e in s.get("/api/library")))
    code, _ = s.request("GET", f"/api/library/load?slug={slug}")
    check("deleted project is gone", code == 404, str(code))
    code, out = s.request("POST", "/api/library/save", {"name": "  ", "project": project})
    check("library save needs a name", code == 400, str(code))


def test_controls(s: Server) -> None:
    print("brightness / power / misc")
    fake_dir = os.path.join(DATA_DIR, "fake")
    out = s.post("/api/brightness", {"level": 3})
    cmd = json.load(open(os.path.join(fake_dir, "last_command.json")))
    check("brightness ok", out.get("ok") is True and cmd["level"] == 3, f"{out} {cmd}")
    check("brightness payload as captured from the vendor app", cmd["frame"] == iledsign.frame(0x09, bytes([10 - 3]) + bytes(8)).hex(),
          cmd["frame"])
    check("status reports brightness", s.get("/api/status").get("brightness") == 3)
    code, out = s.request("POST", "/api/brightness", {"level": "high"})
    check("bad brightness is 400", code == 400, str(code))
    out = s.post("/api/power", {"on": False})
    cmd = json.load(open(os.path.join(fake_dir, "last_command.json")))
    check("power off ok", out.get("ok") is True and cmd["on"] is False, f"{out} {cmd}")
    check("power payload as captured from the vendor app", cmd["frame"] == iledsign.frame(0x0A, bytes([0]) + bytes(8)).hex(), cmd["frame"])
    s.post("/api/power", {"on": True})
    check("status reports display_on", s.get("/api/status").get("display_on") is True)
    out = s.post("/api/open_bluetooth_settings")
    check("open_bluetooth_settings ok", out.get("ok") is True)
    marker = f"selftest-{secrets.token_hex(4)}"
    s.post("/api/log", {"level": "error", "message": marker})
    ui_log = open(os.path.join(DATA_DIR, "logs", "ui.log"), encoding="utf-8").read()
    check("ui log written", marker in ui_log)


def test_disconnect(s: Server) -> None:
    print("disconnect / reconnect")
    st = s.post("/api/disconnect")
    check("disconnect -> disconnected", st["state"] == "disconnected" and st["device"] is None, st["state"])
    time.sleep(2.5)
    st = s.get("/api/status")
    check("stays disconnected (auto-reconnect paused)", st["state"] == "disconnected", st["state"])
    t0 = time.time()
    out = s.post("/api/send_still", {"pixels": b64(solid((5, 5, 5)))})
    check("send while disconnected connects first", out.get("ok") is True and time.time() - t0 < 10, str(out))
    check("connected after send", s.get("/api/status")["state"] == "connected")
    s.post("/api/disconnect")
    st = s.post("/api/connect")
    check("connect -> searching", st["state"] in ("searching", "connecting", "connected"), st["state"])
    st = s.wait_state(("connected",), 6)
    check("connect -> connected", st["state"] == "connected", st["state"])
    s.post("/api/fake_drop")
    st = s.wait_state(("searching", "connecting"), 3)
    check("dropped link is noticed", st["state"] in ("searching", "connecting"), st["state"])
    st = s.wait_state(("connected",), 6)
    check("auto-reconnects after a drop", st["state"] == "connected", st["state"])


def test_scenarios() -> None:
    print("fake scenarios")
    s = Server("not_found")
    try:
        st = s.wait_state(("not_found",), 5)
        check("not_found state", st["state"] == "not_found", st["state"])
        time.sleep(3)
        check("stays not_found while rescanning", s.get("/api/status")["state"] == "not_found")
        t0 = time.time()
        out = s.post("/api/send_still", {"pixels": b64(solid((1, 2, 3)))})
        dt = time.time() - t0
        check("send while not found -> not_connected after ~10 s", out.get("code") == "not_connected"
              and 9 <= dt <= 14, f"{out} {dt:.1f}s")
        out = s.post("/api/brightness", {"level": 5})
        check("brightness while not found -> not_connected", out.get("code") == "not_connected", str(out))
        st = s.post("/api/connect")
        check("try again -> searching", st["state"] == "searching", st["state"])
        st = s.wait_state(("not_found",), 5)
        check("back to not_found", st["state"] == "not_found", st["state"])
    finally:
        s.stop()
    s = Server("bluetooth_off")
    try:
        st = s.wait_state(("bluetooth_off",), 5)
        check("bluetooth_off state", st["state"] == "bluetooth_off" and st["device"] is None, st["state"])
        time.sleep(6)
        check("stays bluetooth_off", s.get("/api/status")["state"] == "bluetooth_off")
    finally:
        s.stop()


def test_watchdog() -> None:
    print("parent-pid watchdog and pythonw")
    parent = subprocess.Popen(["ping", "-n", "120", "127.0.0.1"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    s = Server("ok", parent_pid=parent.pid, python=VENV_PYW)
    try:
        check("pythonw server answers", s.alive())
        st = s.wait_state(("connected",), 6)
        check("pythonw server connects (fake)", st["state"] == "connected", st["state"])
        out = s.post("/api/send_still", {"pixels": b64(solid((9, 9, 9)))})
        check("pythonw server sends", out.get("ok") is True, str(out))
        parent.kill()
        parent.wait(5)
        t0 = time.time()
        try:
            s.proc.wait(10)  # the venv launcher exits when the real interpreter does
        except subprocess.TimeoutExpired:
            pass
        check("server exits when parent exits", not s.alive() and s.proc.poll() is not None,
              f"{time.time() - t0:.1f}s")
    finally:
        try:
            parent.kill()
        except OSError:
            pass
        s.stop()
    # A parent pid that does not exist: the server must exit by itself right away.
    proc = subprocess.Popen([VENV_PY, SERVER, "--port", "0", "--token", "x", "--data-dir", DATA_DIR,
                             "--fake", "--parent-pid", "99999996"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT_DIR)
    try:
        proc.wait(15)
        check("server exits when parent pid does not exist", True)
    except subprocess.TimeoutExpired:
        check("server exits when parent pid does not exist", False, "still running after 15 s")
        proc.kill()


def real_connect_check(hold: float) -> int:
    """Real Bluetooth: scan, connect + handshake, hold, disconnect, reconnect. Sends NOTHING to the display."""
    global DATA_DIR
    DATA_DIR = os.path.join(APP_DIR, "data", "test-real")
    print("real sign connect check (no uploads, no brightness/power)")
    t0 = time.time()
    s = Server(None)
    try:
        timeline = []
        last = None
        deadline = time.time() + 40
        st = {}
        while time.time() < deadline:
            st = s.get("/api/status")
            if st["state"] != last:
                last = st["state"]
                timeline.append((round(time.time() - t0, 1), last, st.get("message"), st.get("last_error")))
                print(f"  t={timeline[-1][0]:>5}s  {last}: {st.get('message')}  {st.get('last_error') or ''}")
            if last == "connected":
                break
            time.sleep(0.2)
        check("real sign connected", last == "connected", str(st))
        if last == "connected":
            print(f"  device: {st['device']}")
            end = time.time() + hold
            dropped = False
            while time.time() < end:
                st = s.get("/api/status")
                if st["state"] != "connected":
                    dropped = True
                    print(f"  t={time.time() - t0:.1f}s link changed: {st['state']} {st.get('message')}")
                    break
                time.sleep(1)
            check(f"link stayed up for {hold:.0f} s idle", not dropped)
            if dropped:
                st = s.wait_state(("connected",), 30)
                check("auto-reconnected after a drop", st["state"] == "connected", st["state"])
            st = s.post("/api/disconnect")
            check("disconnect", st["state"] == "disconnected", st["state"])
            time.sleep(3)
            check("stays disconnected", s.get("/api/status")["state"] == "disconnected")
            t1 = time.time()
            s.post("/api/connect")
            st = s.wait_state(("connected",), 40)
            check("reconnect after Connect", st["state"] == "connected", st["state"])
            print(f"  reconnect took {time.time() - t1:.1f} s")
            s.post("/api/disconnect")
    finally:
        s.stop()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


def gif_blocks(gif: bytes) -> dict:
    """Walk the GIF block structure: frame count and color tables."""
    packed = gif[10]
    pos = 13 + (3 * (2 << (packed & 7)) if packed & 0x80 else 0)
    frames = local = 0
    while pos < len(gif) and gif[pos] != 0x3B:
        if gif[pos] == 0x21:
            pos += 2
        elif gif[pos] == 0x2C:
            p = gif[pos + 9]
            frames += 1
            pos += 10
            if p & 0x80:
                local += 1
                pos += 3 * (2 << (p & 7))
            pos += 1
        else:
            raise ValueError(f"bad GIF block at {pos}")
        while gif[pos]:
            pos += gif[pos] + 1
        pos += 1
    return {"global": bool(packed & 0x80), "frames": frames, "local_tables": local}


def test_qa_fixes(s: Server) -> None:
    print("shared GIF palette / cropped text / slideshow values")
    fake_dir = os.path.join(DATA_DIR, "fake")
    many = []
    for i in range(12):
        im = Image.new("RGB", (W, H))
        im.putdata([((x * 4 + i * 9) % 256, (y * 16 + i) % 256, (x * y + i * 40) % 256) for y in range(H) for x in range(W)])
        many.append(im.tobytes())
    for label, frames in (("<= 256 colors", moving_dot_frames(40)), ("> 256 colors", many)):
        out = s.post("/api/send_animation", {"frames": [b64(f) for f in frames], "durations": 40})
        gif = open(os.path.join(fake_dir, "last.gif"), "rb").read()
        info = gif_blocks(gif)
        check(f"GIF {label}: one global palette, no local tables",
              out.get("ok") and info["global"] and info["local_tables"] == 0 and info["frames"] == len(frames),
              str(info))
    out = s.post("/api/export_gif", {"frames": [b64(f) for f in many], "durations": 50, "scale": 8})
    info = gif_blocks(unb64(out["gif"]))
    check("exported GIF has no local tables", info["local_tables"] == 0 and info["frames"] == len(many), str(info))
    est_new = s.post("/api/estimate", {"frames": [b64(f) for f in many], "durations": 40})["bytes"]
    check("estimate matches the shared-palette GIF", est_new < 12 * 768 + 20000, str(est_new))

    out = s.post("/api/text_mask", {"text": "TWO\nLINES", "font": "pixel5x7", "size": 2})
    check("two lines at scale 2 report cropped", out.get("cropped") is True, str(out.get("cropped")))
    out = s.post("/api/text_mask", {"text": "TWO\nLINES", "font": "tiny3x5", "size": 1})
    check("two tiny lines are not cropped", out.get("cropped") is False, str(out.get("cropped")))

    slides = [solid((40, 0, 0)), solid((0, 40, 0))]
    out = s.post("/api/send_slideshow", {"frames": [b64(f) for f in slides], "transition": "left", "speed": 0,
                                         "dwell": 0})
    meta = json.load(open(os.path.join(fake_dir, "last.json")))
    check("slideshow never sends speed 0 or a tiny dwell", out.get("ok") and meta["speed"] >= 1 and meta["dwell"] >= 5,
          str(meta))


def test_shutdown() -> None:
    print("shutdown endpoint")
    s = Server("ok")
    s.wait_state(("connected",), 6)
    code, _ = s.request("POST", "/api/shutdown", {}, token=False)
    check("shutdown needs the token", code == 403 and s.alive(), str(code))
    t0 = time.time()
    out = s.post("/api/shutdown", {})
    check("shutdown answers ok after closing the link", out.get("ok") is True and out.get("disconnected") is True,
          str(out))
    try:
        s.proc.wait(5)
        exited = True
    except subprocess.TimeoutExpired:
        exited = False
    check("backend exits by itself after shutdown", exited and not s.alive(), f"{time.time() - t0:.1f} s")
    print(f"    exited {time.time() - t0:.2f} s after the request")
    if not exited:
        s.stop()


# ---------------------------------------------------------------- ambient mode and the service

SERVICE_DIR = os.path.join(APP_DIR, "data", "test-service")
AMBIENT_KEYS = {"enabled", "location", "units", "clock_24h", "active_start", "active_end", "refresh_minutes",
                "screens", "news_sources", "day_brightness", "day_color_scale", "night_brightness", "night_text",
                "pause_minutes_on_manual", "news_speed"}
AMBIENT_STATUS_KEYS = {"mode", "enabled", "paused_until", "last_upload", "last_upload_ok", "last_upload_bytes",
                       "last_upload_seconds", "next_upload", "last_error", "weather", "headlines", "news_updated",
                       "feeds"}


def gif_ms(data: bytes) -> tuple[int, int, tuple]:
    im = Image.open(io.BytesIO(data))
    total = 0
    for i in range(im.n_frames):
        im.seek(i)
        total += im.info.get("duration", 0)
    return im.n_frames, total, im.size


def wait_for(fetch, ok, timeout: float, every: float = 0.25):
    deadline = time.time() + timeout
    v = fetch()
    while time.time() < deadline:
        if ok(v):
            return v
        time.sleep(every)
        v = fetch()
    return v


def test_ambient_api(s: Server) -> None:
    st = s.get("/api/status")
    check("status has service=false", st.get("service") is False, str(st.get("service")))
    cfg = s.get("/api/ambient/settings")
    check("ambient settings shape", AMBIENT_KEYS <= set(cfg), str(sorted(set(cfg) ^ AMBIENT_KEYS)))
    check("ambient defaults", cfg["units"] in ("F", "C") and cfg["active_start"] == "08:00"
          and cfg["active_end"] == "22:00" and cfg["refresh_minutes"] == 5 and cfg["night_text"] == ""
          and cfg["screens"]["clock"]["seconds"] == 25 and cfg["location"] is None
          and isinstance(cfg["clock_24h"], bool) and cfg["news_speed"] == "normal", json.dumps(cfg)[:300])
    st = s.get("/api/ambient/status")
    check("no location yet: status says so", st.get("location_set") is False, str(st.get("location_set")))
    out = s.post("/api/ambient/settings", {"location": TEST_PLACE})
    check("location saved", out["location"] == TEST_PLACE, str(out["location"]))
    speeds = [s.post("/api/ambient/settings", {"news_speed": v})["news_speed"] for v in ("fast", 40, "999", "junk")]
    check("news speed: presets, px/s, clamped, junk is normal", speeds == ["fast", 40, 75, "normal"], str(speeds))
    check("status reports the news speed in px/s", s.get("/api/ambient/status").get("news_px_per_s") == 33.3,
          str(s.get("/api/ambient/status").get("news_px_per_s")))
    out = s.post("/api/ambient/settings", {"refresh_minutes": 7, "screens": {"week": {"on": False}}})
    check("ambient settings deep-merge", out["refresh_minutes"] == 7 and out["screens"]["week"]["on"] is False
          and out["screens"]["now"]["on"] is True and out["screens"]["clock"]["seconds"] == 25, json.dumps(out)[:300])
    check("ambient settings persist", s.get("/api/ambient/settings")["refresh_minutes"] == 7)
    out = s.post("/api/ambient/settings", {"refresh_minutes": 99, "active_start": "nope", "day_color_scale": -1})
    check("ambient settings clamped", out["refresh_minutes"] == 15 and out["active_start"] == "08:00"
          and out["day_color_scale"] == 0.1, json.dumps(out)[:200])
    s.post("/api/ambient/settings", {"refresh_minutes": 5, "screens": {"week": {"on": True}}, "day_color_scale": 0.7})
    st = s.get("/api/ambient/status")
    check("ambient status shape", AMBIENT_STATUS_KEYS <= set(st), str(sorted(AMBIENT_STATUS_KEYS - set(st))))
    check("ambient status off without --service", st["mode"] == "off" and st.get("running") is False, st["mode"])
    check("ambient status lists feeds", len(st["feeds"]) >= 6 and all({"id", "name", "ok", "last_error"} <= set(f)
                                                                       for f in st["feeds"]), str(st["feeds"])[:200])
    pv = s.post("/api/ambient/preview", {"minutes": 1})
    n, total, size = gif_ms(unb64(pv["gif"]))
    check("preview GIF is 64x16, about a minute", size == (W, H) and 55000 <= total <= 125000 and n == pv["frames"],
          f"{size} {total} ms {n} frames")
    check("preview fields", pv["bytes"] == len(unb64(pv["gif"])) and abs(pv["seconds"] * 1000 - total) < 50,
          str({k: pv[k] for k in ("bytes", "frames", "seconds")}))
    try:
        os.remove(os.path.join(DATA_DIR, "fake", "autostart.json"))  # left by an earlier run
    except OSError:
        pass
    a = s.get("/api/autostart")
    check("autostart query (fake)", a.get("installed") is False and a.get("enabled") is False, str(a))
    a = s.post("/api/autostart", {"enabled": True})
    check("autostart enable (fake)", a.get("installed") is True and a.get("enabled") is True, str(a))
    a = s.post("/api/autostart", {"enabled": False})
    check("autostart disable (fake)", a.get("installed") is True and a.get("enabled") is False, str(a))
    code, out = s.request("POST", "/api/autostart", {"enabled": "yes"})
    check("autostart rejects non-bool", code == 400, f"{code} {out}")
    code, out = s.request("GET", "/api/ambient/geocode?q=Springfield%2C%20IL")
    if code == 200 and isinstance(out, list) and out:
        check("geocode prefers the state hint", out[0].get("admin1") == "Illinois"
              and abs(out[0]["lat"] - 39.8) < 0.2 and out[0].get("timezone") == "America/Chicago", str(out[0]))
    else:
        check("geocode answers (offline is ok)", code == 200, f"{code} {out}")
    check("geocode empty query", s.get("/api/ambient/geocode?q=") == [])


def _hhmm(t) -> str:
    return t.strftime("%H:%M")


def test_service() -> None:
    import shutil
    from datetime import datetime, timedelta
    shutil.rmtree(SERVICE_DIR, ignore_errors=True)
    now = datetime.now()
    night = {"active_start": _hhmm(now + timedelta(hours=2)), "active_end": _hhmm(now + timedelta(hours=3)),
             "location": TEST_PLACE}
    os.makedirs(os.path.join(SERVICE_DIR, "ambient"), exist_ok=True)
    with open(os.path.join(SERVICE_DIR, "ambient", "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(night, fh)
    svc = Server("ok", data_dir=SERVICE_DIR, extra=["--service"], token=None)
    fake_dir = os.path.join(SERVICE_DIR, "fake")
    try:
        with open(os.path.join(SERVICE_DIR, "service.json"), encoding="utf-8") as fh:
            sj = json.load(fh)
        check("service.json has port, token, pid, started", sj.get("port") == svc.port and sj.get("token") == svc.token
              and sj.get("pid") == svc.pid and bool(sj.get("started")), str(sj))
        check("status service=true", svc.get("/api/status").get("service") is True)
        st = wait_for(lambda: svc.get("/api/ambient/status"), lambda v: v.get("last_upload_ok") is True, 30)
        check("service starts in night mode", st["mode"] == "night" and st["running"] is True, str(st["mode"]))
        check("night screen uploaded by itself", st.get("last_upload_kind") == "night" and st["last_upload_ok"] is True,
              str({k: st.get(k) for k in ("last_upload_kind", "last_upload_ok", "last_error")}))
        im = Image.open(os.path.join(fake_dir, "last.png")).convert("RGB")
        check("night screen is all black", im.size == (W, H) and im.getbbox() is None)
        check("night brightness 1", svc.get("/api/status").get("brightness") == 1, str(svc.get("/api/status").get("brightness")))
        check("next upload is the day start", (st.get("next_upload") or "")[11:16] == night["active_start"],
              str(st.get("next_upload")))
        t_night = st["last_upload"]
        time.sleep(1.5)
        st = svc.get("/api/ambient/status")
        check("night uploads only once", st["last_upload"] == t_night, f"{st['last_upload']} vs {t_night}")

        day = {"active_start": _hhmm(now - timedelta(hours=1)), "active_end": _hhmm(now + timedelta(hours=1))}
        svc.post("/api/ambient/settings", day)
        st = svc.post("/api/ambient/run_now")
        st = wait_for(lambda: svc.get("/api/ambient/status"),
                      lambda v: v.get("last_upload_kind") == "day" and v.get("last_upload_ok") is True, 150, 0.5)
        check("day rotation uploaded", st.get("last_upload_kind") == "day" and st["mode"] == "day",
              str({k: st.get(k) for k in ("mode", "last_upload_kind", "last_upload_ok", "last_error")}))
        with open(os.path.join(fake_dir, "last.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(os.path.join(fake_dir, "last.gif"), "rb") as fh:
            n, total, size = gif_ms(fh.read())
        tail = 10 * 60000  # the still weather tail after the content
        check("day rotation runs to a handover no sooner than refresh minutes", size == (W, H)
              and 5 * 60000 <= total - tail <= 7 * 60000 and n > 20, f"{size} {total} ms {n} frames")
        check("day rotation about 160 KB or less", meta.get("gif_bytes", 1e9) < 200_000, str(meta))
        plan = st.get("last_plan") or {}
        check("day rotation hands over on a clock", (plan.get("handover") or {}).get("kind") == "clock"
              and plan.get("news_px_per_s") == 33.3, str(plan))
        try:
            done = datetime.fromisoformat(st["last_upload"]).timestamp() + float(st["last_upload_seconds"])
            off = done - datetime.fromisoformat(plan["start"]).timestamp()
        except (KeyError, TypeError, ValueError):
            off = 99.0
        check("paced upload completes when the GIF was planned to start", abs(off) <= 2.0, f"{off:+.1f} s")
        check("next upload is timed before the handover", bool(st.get("next_upload"))
              and st["next_upload"] < (plan.get("handover") or {}).get("from", "") and st["next_upload"] > st["last_upload"],
              f"{st.get('next_upload')} vs {plan.get('handover')}")
        check("day brightness 3 and display on", svc.get("/api/status").get("brightness") == 3
              and svc.get("/api/status").get("display_on") is True, str(svc.get("/api/status")))
        check("status reports the upload", (st.get("last_upload_bytes") or 0) > 1000
              and st.get("last_upload_seconds") is not None and st.get("next_upload"), str(st)[:300])

        # clock off: a GIF the sign loops by itself, uploaded every refresh_minutes
        off = {k: {"on": False} for k in ("clock", "now", "today", "week")}
        svc.post("/api/ambient/settings", {"screens": off})
        before = st["last_upload"]
        svc.post("/api/ambient/run_now")
        st = wait_for(lambda: svc.get("/api/ambient/status"),
                      lambda v: v.get("last_upload") != before and v.get("busy") is False, 150, 0.5)
        plan = st.get("last_plan") or {}
        if st.get("headlines"):
            with open(os.path.join(fake_dir, "last.gif"), "rb") as fh:
                n, total, size = gif_ms(fh.read())
            check("news only: a looping GIF with no tail", st.get("last_upload_ok") is True and plan.get("loop") is True
                  and abs(total / 1000 - plan.get("seconds", 0)) < 0.05 and size == (W, H), f"{total} ms {plan}")
            check("news only: about 160 KB or less", (plan.get("bytes") or 1e9) <= 160_000, str(plan.get("bytes")))
            gap = (datetime.fromisoformat(st["next_upload"]) - datetime.fromisoformat(plan["start"])).total_seconds()
            check("news only: next upload a refresh later (5 min)", 4 * 60 <= gap <= 5 * 60 + plan["seconds"],
                  f"{gap:.0f} s")
        else:
            check("news only: no headlines offline, the clock stands in", st.get("last_upload_ok") is True, str(st)[:200])
        svc.post("/api/ambient/settings", {"screens": {k: {"on": True} for k in off}})

        out = svc.post("/api/send_still", {"pixels": b64(solid((40, 0, 0)))})
        st = svc.get("/api/ambient/status")
        check("manual send pauses ambient", out.get("ok") is True and st["mode"] == "paused" and st["paused_until"],
              str({k: st.get(k) for k in ("mode", "paused_until")}))
        st = svc.post("/api/ambient/resume")
        check("resume leaves pause", st["mode"] == "day", st["mode"])
        st = svc.post("/api/ambient/pause", {"minutes": 5})
        check("pause for 5 minutes", st["mode"] == "paused" and st["paused_until"], str(st.get("paused_until")))
        st = svc.post("/api/ambient/resume")
        check("resume again", st["mode"] == "day", st["mode"])

        out = svc.post("/api/shutdown")
        time.sleep(0.8)
        check("service ignores /api/shutdown", out.get("ignored") is True and svc.alive(), str(out))
        old_port, old_token = svc.port, svc.token
        svc.stop()
        svc = Server("ok", data_dir=SERVICE_DIR, extra=["--service"], token=None)
        check("service keeps its port and token across restarts", svc.port == old_port and svc.token == old_token,
              f"{svc.port} vs {old_port}")
        st = svc.get("/api/ambient/status")
        check("state survives a restart", st.get("last_upload_kind") in ("day", None) and st["mode"] in ("day", "paused"),
              str({k: st.get(k) for k in ("mode", "last_upload_kind")}))
        out = svc.post("/api/shutdown", {"force": True})
        deadline = time.time() + 10
        while time.time() < deadline and svc.alive():
            time.sleep(0.2)
        check("service stops with force", out.get("ok") is True and not svc.alive(), str(out))
    finally:
        svc.stop()


def test_backend_lock() -> None:
    path = os.path.join(SERVICE_DIR, "lock-test.lock")
    code = ("import sys, time; sys.path.insert(0, r'%s'); import server; ok, o = server.acquire_backend_lock(r'%s'); "
            "print('held' if ok else 'fail', flush=True); time.sleep(8)") % (APP_DIR, path)
    p = subprocess.Popen([VENV_PY, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                         cwd=ROOT_DIR)
    try:
        line = (p.stdout.readline() or "").strip()
        check("lock: first backend holds it", line == "held", line)
        if APP_DIR not in sys.path:
            sys.path.insert(0, APP_DIR)
        import server as srv  # noqa: E402
        ok, owner = srv.acquire_backend_lock(path)
        check("lock: a second backend is refused and sees the owner pid", not ok and owner.isdigit(), f"{ok} {owner}")
    finally:
        p.kill()
        p.wait(10)
    time.sleep(0.5)
    ok, _owner = srv.acquire_backend_lock(path)
    check("lock: free again once the owner exits", ok)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the slow scenario and watchdog runs")
    ap.add_argument("--real-connect", action="store_true",
                    help="instead: connect to the REAL sign (handshake only, nothing is displayed)")
    ap.add_argument("--hold", type=float, default=45, help="seconds to hold the real connection idle")
    a = ap.parse_args()
    if a.real_connect:
        return real_connect_check(a.hold)
    t0 = time.time()
    log_path = os.path.join(DATA_DIR, "logs", "server.log")
    log_start = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    try:
        os.remove(os.path.join(DATA_DIR, "ambient", "settings.json"))  # start from the defaults
    except OSError:
        pass
    s = Server("ok")
    print(f"server {s.base} pid {s.pid}")
    try:
        for fn in (test_security, test_status, test_fonts, test_sends, test_estimate_export, test_import,
                   test_storage, test_controls, test_qa_fixes, test_ambient_api, test_disconnect):
            try:
                fn(s)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, repr(e))
    finally:
        s.stop()
        check("server stopped", not s.alive())
    try:
        test_shutdown()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        check("test_shutdown raised", False, repr(e))
    for fn in (test_service, test_backend_lock):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            check(f"{fn.__name__} raised", False, repr(e))
    if not a.quick:
        for fn in (test_scenarios, test_watchdog):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, repr(e))
    with open(log_path, encoding="utf-8") as fh:
        log_text = fh.read()
    if len(log_text.encode("utf-8")) >= log_start:
        log_text = log_text.encode("utf-8")[log_start:].decode("utf-8", "replace")
    check("no tracebacks in server.log", "Traceback" not in log_text,
          log_text[log_text.find("Traceback") - 300:log_text.find("Traceback") + 600] if "Traceback" in log_text else "")
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed in {time.time() - t0:.0f} s")
    for name, _ok, detail in failed:
        print(f"FAILED: {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
