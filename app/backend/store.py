"""Files under the data dir: settings, autosave and the project library."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("ledsign.store")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
MAX_NAME = 80


def atomic_write(path: str, data: bytes | str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(tmp, mode, **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as fh:
        fh.write(data)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # a reader has the file open on Windows; retry briefly
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        log.warning("could not read %s", path, exc_info=True)
        return default


def write_json(path: str, obj) -> None:
    atomic_write(path, json.dumps(obj, separators=(",", ":")))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Settings:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        data = read_json(path, {})
        self.data: dict = data if isinstance(data, dict) else {}

    def get(self, key: str, default=None):
        with self._lock:
            return self.data.get(key, default)

    def update(self, **kw) -> None:
        with self._lock:
            changed = any(self.data.get(k) != v for k, v in kw.items())
            self.data.update(kw)
            snapshot = dict(self.data)
        if changed:
            try:
                write_json(self.path, snapshot)
            except OSError:
                log.exception("could not save settings")


class Store:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.autosave_path = os.path.join(data_dir, "autosave.json")
        self.lib_dir = os.path.join(data_dir, "library")
        os.makedirs(self.lib_dir, exist_ok=True)
        self._lock = threading.Lock()

    # -- autosave ---------------------------------------------------------------

    def load_autosave(self) -> dict:
        p = read_json(self.autosave_path, {})
        return p if isinstance(p, dict) else {}

    def save_autosave(self, project: dict) -> None:
        with self._lock:
            write_json(self.autosave_path, project)

    # -- library ----------------------------------------------------------------

    @staticmethod
    def slugify(name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60].strip("-")
        if not slug:
            slug = "project-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        return slug

    def _paths(self, slug: str) -> tuple[str, str]:
        if not SLUG_RE.match(slug or ""):
            raise KeyError(slug)
        return os.path.join(self.lib_dir, f"{slug}.json"), os.path.join(self.lib_dir, f"{slug}.meta.json")

    def save(self, name: str, project: dict) -> str:
        name = name.strip()[:MAX_NAME]
        slug = self.slugify(name)
        frames = project.get("frames") or []
        first = frames[0] if frames and isinstance(frames[0], dict) else {}
        project = dict(project, name=name)
        meta = {
            "slug": slug,
            "name": name,
            "frames": len(frames),
            "width": project.get("width"),
            "height": project.get("height"),
            "thumb": first.get("pixels") if isinstance(first.get("pixels"), str) else None,
            "updated": now_iso(),
        }
        proj_path, meta_path = self._paths(slug)
        with self._lock:
            write_json(proj_path, project)
            write_json(meta_path, meta)
        return slug

    def list(self) -> list[dict]:
        out = []
        with self._lock:
            names = os.listdir(self.lib_dir)
        for fn in names:
            if not fn.endswith(".meta.json"):
                continue
            meta = read_json(os.path.join(self.lib_dir, fn), None)
            if isinstance(meta, dict) and SLUG_RE.match(str(meta.get("slug", ""))):
                if os.path.exists(os.path.join(self.lib_dir, f"{meta['slug']}.json")):
                    out.append(meta)
        out.sort(key=lambda m: str(m.get("updated", "")), reverse=True)
        return out

    def load(self, slug: str) -> dict | None:
        try:
            proj_path, _ = self._paths(slug)
        except KeyError:
            return None
        p = read_json(proj_path, None)
        return p if isinstance(p, dict) else None

    def delete(self, slug: str) -> bool:
        try:
            paths = self._paths(slug)
        except KeyError:
            return False
        found = False
        with self._lock:
            for p in paths:
                try:
                    os.remove(p)
                    found = True
                except FileNotFoundError:
                    pass
        return found
