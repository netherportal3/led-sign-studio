# LED Sign Studio host

The Windows shell: a WinForms window with WebView2 that shows the web UI of the Python backend
(`app\server.py`). It uses the always-on background service when there is one, and otherwise
starts a backend of its own. It gives the app a real exe to pin to the taskbar.

## Build

Needs the .NET 10 SDK. Run from the repo root:

```
dotnet publish host -c Release -r win-x64 --self-contained false -o LedSignStudio
```

The icon is already in `app\assets`. To redraw it, run `.venv\Scripts\python host\make_icon.py`
before publishing: it writes `app\assets\icon.ico` (16 to 256 px, each size drawn on its own pixel
grid) and `app\assets\icon.png`, and the publish step embeds the icon. Close the app before
publishing, or the exe is locked.

## Run

```
LedSignStudio\LedSignStudio.exe                 the real sign
LedSignStudio\LedSignStudio.exe --fake          simulated sign, no Bluetooth
LedSignStudio\LedSignStudio.exe --fake=not_found
LedSignStudio\LedSignStudio.exe --fake=bluetooth_off
```

The exe finds the repo root from its own location: the first folder at or above it that holds
`app\server.py`. It runs the backend with `.venv\Scripts\pythonw.exe`, or with the first
`pythonw.exe` on the PATH when there is no `.venv`.

What it does on launch:

- One instance only. A second launch brings the open window to the front and quits. If that
  window is just closing, the second launch waits for it and then starts normally.
- Only one process may own the sign's Bluetooth link, so it picks a backend in this order:
  1. **The service is running.** `app\data\service.json` (`{port, token, pid, started}`, written
     by `app\server.py --service`) points to a port where `/api/health` answers with that token:
     attach. The window loads `http://127.0.0.1:<port>/?t=<token>` and starts nothing.
  2. **The service is down but its login task exists.** The per-user task
     `LED Sign Studio Service` (created by the "Start with Windows" switch) exists and is
     enabled: run it (`schtasks /run`), show "Starting the background service...", wait up to
     15 s until service.json points to a healthy `/api/health`, then attach. (The service keeps
     its saved port and token across restarts, so it may answer before it rewrites
     service.json; that is the same service.)
  3. **Otherwise** start its own backend (below). If that backend exits at once with code 3
     (another process owns the sign: `app\data\backend.lock`), it waits up to 10 s for
     service.json to point to a healthy service and attaches to it. If there is none, it shows
     "Another program is using the sign right now" with Try again.
- `--fake` and the service: a `--fake` window only attaches to a service that runs a fake sign
  (`GET /api/status` says `fake: true`, or service.json does), a normal window only to one that
  runs the real sign. A `--fake` window never starts the real login task (it would wake the real
  sign), only a task named by `LEDSIGN_SERVICE_TASK`. Fake backends do not take the sign lock,
  so a `--fake` window still runs its own fake backend while the real service runs.
- Attached to the service:
  - It checks `/api/health` every 2 s. If the service restarts with a new token (or port), the
    page reloads from it straight away. After about 10 s without an answer it shows
    "Reconnecting..." and goes through the order above again (service, task, own backend). If
    all of that fails, the error screen offers Try again.
  - On close it asks the page to save and closes. It does NOT call `/api/shutdown` and kills
    nothing: the service keeps running, and an upload it is doing carries on (so there is no
    "still being updated" question either). The service also ignores `/api/shutdown` unless
    it is sent `force: true`.
- Its own backend:
  - Picks a free port (the last one if still free, so browser storage survives restarts; never
    the service's port) and a random token, then starts `pythonw.exe app\server.py --port P
    --token T --parent-pid <host pid> [--fake...]` inside a Job object with KILL_ON_JOB_CLOSE.
    The backend dies with the window, also when the exe is killed.
  - Shows a dark "Starting..." screen, waits up to 20 s for `/api/health`, then loads
    `http://127.0.0.1:P/?t=T`. On failure it shows a plain error screen with the log path and a
    Try again button. If the backend dies later, it is restarted on the same port (3 times per
    5 minutes at most).
  - On close it asks the page to save, hides the window, then calls `POST /api/shutdown` so the
    backend closes the Bluetooth link and exits by itself (up to 3 s). Only then does the job
    object kill whatever is left. A killed backend leaves the link open and Windows holds it for
    about 14 s, which makes the next launch slow to find the sign.
- Creates `LED Sign Studio.lnk` in the Start Menu and on the Desktop if missing (not from a dev
  build under `host\bin`). No AppUserModelID is set, so the running window and the shortcut
  share one taskbar button. Windows 11 cannot pin programmatically: right-click the running
  app's taskbar button and choose "Pin to taskbar".

Runtime data, all under `app\data`: `window.json` (size, position, maximized), `host.json`
(last port), `webview2\` (browser profile), `logs\host.log` (this shell),
`logs\server-output.log` (backend stdout/stderr), `logs\server.log` (written by the backend).

Keys: F12 opens DevTools. Downloads (Export GIF) open a Save dialog.

## Dev options

`LEDSIGN_SERVER_CMD` replaces `pythonw.exe app\server.py` with another command, for example
a stub server. The usual arguments are still appended.

```
set LEDSIGN_SERVER_CMD="%CD%\.venv\Scripts\pythonw.exe" "C:\path\to\stub.py"
LedSignStudio\LedSignStudio.exe --fake
```

`LEDSIGN_SERVICE_JSON` replaces the service.json path, so a test never touches the real data
folder. `LEDSIGN_SERVICE_TASK` replaces the login task name (a path such as
`\MyTests\Stub service` works too). For example, against a fake test service:

```
.venv\Scripts\pythonw.exe app\server.py --service --fake --data-dir app\data\test-host-svc
set LEDSIGN_SERVICE_JSON=%CD%\app\data\test-host-svc\service.json
LedSignStudio\LedSignStudio.exe --fake
```

`host.log` records which way it went: "attached to the service on port ...", "service task ...
found, enabled", "schtasks /run ...", "the service did not answer within 15 s; starting our own
backend", "leaving the service on port ... running".
