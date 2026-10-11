"""The per-user login task that starts the service: `LED Sign Studio Service`.

It is only ever created by the "Start with Windows" switch in the Ambient tab, never by itself.

Trigger: at logon of the current user. Action: pythonw (the repo's .venv, else the Python running
this) with `app\\server.py --service`. Runs only while the user is logged on, no admin, restarts
every minute on failure, no time limit, keeps running on battery.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

log = logging.getLogger("ledsign.ambient.autostart")

TASK_NAME = "LED Sign Studio Service"
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT_DIR = os.path.dirname(APP_DIR)
SERVER = os.path.join(APP_DIR, "server.py")


def find_pythonw() -> str:
    """The repo's .venv pythonw.exe, else the pythonw.exe next to the running interpreter."""
    venv = os.path.join(ROOT_DIR, ".venv", "Scripts", "pythonw.exe")
    if os.path.isfile(venv):
        return venv
    here = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return here if os.path.isfile(here) else sys.executable


PYTHONW = find_pythonw()
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def _run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW if os.name == "nt" else 0)


def _ps(script: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-Command", script], timeout)


def query() -> dict:
    """{installed, enabled, running} from Task Scheduler."""
    try:
        r = _ps(f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue; "
                "if ($t) { @{installed=$true; state=[string]$t.State; enabled=($t.Settings.Enabled)} | ConvertTo-Json -Compress }"
                " else { '{\"installed\":false}' }", timeout=30)
        d = json.loads((r.stdout or "").strip() or '{"installed": false}')
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.warning("could not query the login task: %r", e)
        return {"installed": False, "enabled": False, "running": False, "error": str(e)[:200]}
    if not d.get("installed"):
        return {"installed": False, "enabled": False, "running": False}
    state = str(d.get("state") or "")
    return {"installed": True, "enabled": bool(d.get("enabled")) and state != "Disabled",
            "running": state == "Running"}


def install_script(pythonw: str = PYTHONW, server: str = SERVER) -> str:
    q = lambda s: s.replace("'", "''")  # noqa: E731  PowerShell single-quote escape
    return f"""
$ErrorActionPreference = 'Stop'
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute '{q(pythonw)}' -Argument '"{q(server)}" --service' -WorkingDirectory '{q(ROOT_DIR)}'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 9999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName '{q(TASK_NAME)}' -Description 'Runs the LED Sign Studio service (ambient mode) in the background.' `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
'ok'
"""


def install() -> dict:
    r = _ps(install_script())
    if r.returncode != 0 or "ok" not in (r.stdout or ""):
        raise RuntimeError(f"Register-ScheduledTask failed: {(r.stderr or r.stdout or '').strip()[:400]}")
    log.info("registered the login task %r", TASK_NAME)
    return query()


def set_enabled(enabled: bool) -> dict:
    st = query()
    if enabled and not st["installed"]:
        return install()
    if st["installed"]:
        r = _run(["schtasks", "/change", "/tn", TASK_NAME, "/enable" if enabled else "/disable"])
        if r.returncode != 0:
            raise RuntimeError(f"schtasks /change failed: {(r.stderr or r.stdout or '').strip()[:300]}")
        log.info("login task %s", "enabled" if enabled else "disabled")
    return query()


def start() -> bool:
    r = _run(["schtasks", "/run", "/tn", TASK_NAME])
    return r.returncode == 0


class FakeAutostart:
    """For --fake runs: remembers the state in a file and never touches Task Scheduler."""

    def __init__(self, path: str) -> None:
        self.path = path

    def query(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = {}
        return {"installed": bool(d.get("installed")), "enabled": bool(d.get("enabled")), "running": False,
                "fake": True}

    def set_enabled(self, enabled: bool) -> dict:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        cur = self.query()
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"installed": cur["installed"] or enabled, "enabled": enabled}, fh)
        return self.query()
