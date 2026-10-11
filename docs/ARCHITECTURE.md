# Architecture

```
iledsign.py            the sign driver (scan, connect, upload, brightness) and a command line tool
app/server.py          local HTTP server on 127.0.0.1 with a per-run token, serving the UI and /api
app/backend/           Bluetooth connection manager (signmgr), fake sign, GIF encoding and import
                       (media), fonts and pixel fonts, project storage (store), self-test
app/backend/ambient/   weather (Open-Meteo), news (RSS), screens, rotation GIF, scheduler, service
app/ui/                the web interface: plain HTML, CSS and JavaScript modules, no build step
host/                  the Windows app: WinForms + WebView2 around the web interface
tools/                 developer helpers: Bluetooth scan and GATT dump
app/data/              settings, autosave, library, caches and logs (created at runtime, not in git)
```

- **One owner of the sign.** Bluetooth runs on one asyncio thread in the backend, and every sign
  operation goes through one lock. A real-sign backend also holds `app/data/backend.lock`, so a
  second one exits with code 3 instead of fighting over the link.
- **The desktop app** shows the backend's page in WebView2. If the background service is running,
  it attaches to it. Otherwise it starts its own backend (`pythonw app\server.py`) inside a Job
  object, so the backend never outlives the window, and asks it to let go of the sign on close.
- **The background service** is `app\server.py --service`: a fixed port and token in
  `app/data/service.json`, the ambient scheduler, and it ignores normal shutdown requests. The
  "Start with Windows" switch registers it as a per-user logon task.
- **Ambient mode** builds one GIF that covers a few minutes (clock, weather, news), times the upload
  so the sign switches on a planned moment, and uploads the next one before it runs out. At night
  it uploads a black (or dim text) screen once and waits for morning.
- **Fake mode** (`--fake`) replaces Bluetooth with a simulated sign that writes what it was sent
  to `<data dir>/fake/`. The self-test and the unit tests use it, so they never touch a real sign.
