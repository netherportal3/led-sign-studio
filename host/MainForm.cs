using System.Diagnostics;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace LedSignStudio;

internal sealed class MainForm : Form
{
    private const string AppTitle = "LED Sign Studio";
    private static readonly Size DefaultLogicalSize = new(1280, 820);
    private static readonly Size MinimumLogicalClient = new(1000, 700);
    private static readonly TimeSpan ServiceCheckInterval = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan ServiceLostAfter = TimeSpan.FromSeconds(10);

    private readonly Backend backend;
    private readonly WebView2 webView;
    private readonly StatusPanel statusPanel;
    private readonly bool fake;
    private Task<Exception?>? webViewInit;
    private bool pageShown;
    private bool startInProgress;
    private bool closeConfirmed;
    private bool closeChecking;
    private bool shuttingDown;
    private int starts;
    private readonly List<DateTime> restarts = [];
    private DateTime lastRenderReload = DateTime.MinValue;
    private CancellationTokenSource? monitorCts;
    private string? navigatedUrl;

    public MainForm(Backend backend, bool fake)
    {
        this.backend = backend;
        this.fake = fake;

        Text = fake ? AppTitle + " (fake sign)" : AppTitle;
        AutoScaleMode = AutoScaleMode.None;
        BackColor = StatusPanel.Background;
        KeyPreview = true;
        Icon = LoadIcon();
        StartPosition = FormStartPosition.Manual;
        PlaceWindow();

        webView = new WebView2
        {
            Dock = DockStyle.Fill,
            DefaultBackgroundColor = StatusPanel.Background,
        };
        statusPanel = new StatusPanel(LoadLogo()) { Dock = DockStyle.Fill };
        Controls.Add(webView);
        Controls.Add(statusPanel);
        statusPanel.BringToFront();

        backend.ExitedUnexpectedly += code =>
        {
            if (!IsDisposed && IsHandleCreated)
            {
                BeginInvoke(() => OnBackendExited(code));
            }
        };
    }

    // ---- Window placement -------------------------------------------------------------

    private void PlaceWindow()
    {
        var saved = WindowPlacement.Load();
        if (saved != null && saved.IsReachable())
        {
            Bounds = saved.Bounds;
            if (saved.Maximized)
            {
                base.WindowState = FormWindowState.Maximized;
            }
            return;
        }

        var screen = Screen.FromPoint(Cursor.Position);
        var area = screen.WorkingArea;
        var scale = Native.DpiAt(new Point(area.X + area.Width / 2, area.Y + area.Height / 2)) / 96f;
        var w = Math.Min((int)(DefaultLogicalSize.Width * scale), area.Width);
        var h = Math.Min((int)(DefaultLogicalSize.Height * scale), area.Height);
        Bounds = new Rectangle(area.X + (area.Width - w) / 2, area.Y + (area.Height - h) / 2, w, h);
    }

    private void UpdateMinimumSize()
    {
        var s = DeviceDpi / 96f;
        var client = new Size((int)(MinimumLogicalClient.Width * s), (int)(MinimumLogicalClient.Height * s));
        var min = SizeFromClientSize(client);
        // Never demand more than the screen can show.
        var area = Screen.FromControl(this).WorkingArea;
        MinimumSize = new Size(Math.Min(min.Width, area.Width), Math.Min(min.Height, area.Height));
    }

    private void SaveWindowState()
    {
        var normal = base.WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
        if (normal.Width <= 0 || normal.Height <= 0)
        {
            return;
        }
        new WindowPlacement
        {
            X = normal.X,
            Y = normal.Y,
            Width = normal.Width,
            Height = normal.Height,
            Maximized = base.WindowState == FormWindowState.Maximized,
        }.Save();
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        var on = 1;
        if (Native.DwmSetWindowAttribute(Handle, Native.DWMWA_USE_IMMERSIVE_DARK_MODE, ref on, sizeof(int)) != 0)
        {
            Native.DwmSetWindowAttribute(Handle, Native.DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, ref on, sizeof(int));
        }
        UpdateMinimumSize();
    }

    protected override void OnDpiChanged(DpiChangedEventArgs e)
    {
        base.OnDpiChanged(e);
        UpdateMinimumSize();
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == (int)Program.ShowMeMessage && Program.ShowMeMessage != 0)
        {
            BringToFrontFromOtherInstance();
            return;
        }
        base.WndProc(ref m);
    }

    private void BringToFrontFromOtherInstance()
    {
        if (Native.IsIconic(Handle))
        {
            Native.ShowWindow(Handle, Native.SW_RESTORE);
        }
        Activate();
        Native.SetForegroundWindow(Handle);
    }

    // ---- Startup ----------------------------------------------------------------------

    protected override async void OnShown(EventArgs e)
    {
        base.OnShown(e);
        _ = Task.Run(Shortcuts.EnsureCreated);

        string? runtime = null;
        try
        {
            runtime = CoreWebView2Environment.GetAvailableBrowserVersionString();
        }
        catch (Exception ex)
        {
            HostLog.Write($"WebView2 runtime check failed: {ex.Message}");
        }
        if (string.IsNullOrEmpty(runtime))
        {
            ShowWebView2Missing();
            return;
        }
        HostLog.Write($"WebView2 runtime {runtime}");

        webViewInit = InitWebViewAsync();
        await StartBackendAndNavigateAsync(keepPort: false);
    }

    private async Task<Exception?> InitWebViewAsync()
    {
        try
        {
            AppPaths.EnsureDataDirs();
            var options = new CoreWebView2EnvironmentOptions();
            var env = await CoreWebView2Environment.CreateAsync(null, AppPaths.WebViewData, options);
            await webView.EnsureCoreWebView2Async(env);
            ConfigureWebView(webView.CoreWebView2);
            return null;
        }
        catch (Exception ex)
        {
            HostLog.Write($"WebView2 init failed: {ex}");
            return ex;
        }
    }

    private async Task StartBackendAndNavigateAsync(bool keepPort, string startingText = "Starting...")
    {
        var attempt = ++starts;
        StopServiceMonitor();
        pageShown = false;
        statusPanel.ShowStarting(startingText);

        StartResult result;
        startInProgress = true;
        try
        {
            result = await ConnectAsync(keepPort, attempt, startingText);
        }
        catch (Exception ex)
        {
            HostLog.Write($"backend start threw: {ex}");
            result = new StartResult(StartFailure.LaunchFailed, ex.Message, null);
        }
        finally
        {
            startInProgress = false;
        }
        if (IsDisposed || shuttingDown || attempt != starts)
        {
            return;
        }

        if (!result.Ok)
        {
            ShowStartFailure(result);
            return;
        }

        var initError = webViewInit == null ? null : await webViewInit;
        if (IsDisposed || shuttingDown || attempt != starts)
        {
            return;
        }
        if (initError != null || webView.CoreWebView2 == null)
        {
            statusPanel.ShowMessage("LED Sign Studio could not start",
                "The app window could not be set up. Close the app and open it again.",
                AppPaths.HostLog, initError?.Message,
                ("Close", Close, true));
            return;
        }

        NavigateToApp();
        StartServiceMonitor();
    }

    /// <summary>
    /// Finds a backend to use, in this order: the always-on service if it answers; the login
    /// task, started and given 15 s to come up; our own backend. If our own backend refuses to
    /// start because another process owns the sign, use that owner when it is the service.
    /// </summary>
    private async Task<StartResult> ConnectAsync(bool keepPort, int attempt, string startingText)
    {
        // 1. The service is already running.
        var service = await ServiceLink.FindHealthyAsync(backend.Http);
        if (service != null)
        {
            if (ServiceMatches(service))
            {
                backend.Attach(service.Info);
                return StartResult.Success;
            }
            HostLog.Write($"service on port {service.Info.Port} is {(service.Fake ? "a fake sign" : "the real sign")}; " +
                          $"this window is {(fake ? "--fake" : "for the real sign")}, so not attaching");
        }
        else
        {
            HostLog.Write($"no running service ({ServiceLink.JsonPath})");
        }

        // 2. The login task exists but the service is down: start it and wait for it.
        //    A --fake window never starts the real task (that would wake the real sign); with
        //    LEDSIGN_SERVICE_TASK set it starts that test task instead.
        if (service == null && (!fake || ServiceLink.TaskOverridden))
        {
            var state = await Task.Run(ServiceLink.QueryTask);
            if (IsDisposed || shuttingDown || attempt != starts)
            {
                return StartResult.Success;
            }
            if (state == ServiceTaskState.Enabled && await Task.Run(ServiceLink.RunTask))
            {
                statusPanel.ShowStarting("Starting the background service...");
                var started = await ServiceLink.WaitForHealthyAsync(backend.Http, TimeSpan.FromSeconds(15));
                if (started != null && ServiceMatches(started))
                {
                    backend.Attach(started.Info);
                    return StartResult.Success;
                }
                HostLog.Write(started == null
                    ? "the service did not answer within 15 s; starting our own backend"
                    : "the service that started does not match this window; starting our own backend");
                statusPanel.ShowStarting(startingText);
            }
        }

        // 3. Our own backend, as before the service existed.
        if (IsDisposed || shuttingDown || attempt != starts)
        {
            return StartResult.Success;
        }
        return await StartOwnBackendAsync(keepPort);
    }

    /// <summary>Starts our own backend; if it refuses because the sign is taken, attaches to the owner.</summary>
    private async Task<StartResult> StartOwnBackendAsync(bool keepPort)
    {
        var result = await backend.StartAsync(keepPort);
        if (result.Ok || result.Failure != StartFailure.Exited)
        {
            return result;
        }
        // It stopped right away. If the service took the sign meanwhile, use the service. Give it
        // longer when the backend said the sign is taken (the owner may still be starting up).
        var locked = Backend.LooksLikeLockExit(result);
        HostLog.Write($"our backend exited during start{(locked ? " (the sign is taken)" : "")}; looking for the service");
        var owner = await ServiceLink.WaitForHealthyAsync(backend.Http, TimeSpan.FromSeconds(locked ? 10 : 1));
        if (owner != null && ServiceMatches(owner))
        {
            backend.Attach(owner.Info);
            return StartResult.Success;
        }
        if (locked)
        {
            return new StartResult(StartFailure.Locked,
                "Another program is using the sign right now. Close it, then try again.", result.Details);
        }
        return result;
    }

    /// <summary>A --fake window only uses a fake-sign service, and a normal window only a real one.</summary>
    private bool ServiceMatches(HealthyService service) => service.Fake == fake;

    private void NavigateToApp()
    {
        if (webView.CoreWebView2 == null)
        {
            return;
        }
        HostLog.Write($"navigating to the app page ({(backend.IsAttached ? "service" : "own backend")} on port {backend.Port})");
        navigatedUrl = backend.Url;
        webView.CoreWebView2.Navigate(backend.Url);
    }

    private void ShowStartFailure(StartResult result)
    {
        StopServiceMonitor();
        var text = result.Failure switch
        {
            StartFailure.Timeout => "The background helper did not respond in time.",
            _ => result.Message,
        };
        statusPanel.ShowMessage("LED Sign Studio could not start", text, AppPaths.ServerLog, result.Details,
            ("Try again", () => _ = StartBackendAndNavigateAsync(keepPort: false), true),
            ("Open log folder", OpenLogFolder, false));
    }

    // ---- Attached to the service: watch it ------------------------------------------------

    private void StartServiceMonitor()
    {
        StopServiceMonitor();
        if (!backend.IsAttached)
        {
            return;
        }
        monitorCts = new CancellationTokenSource();
        _ = MonitorServiceAsync(monitorCts.Token);
    }

    private void StopServiceMonitor()
    {
        monitorCts?.Cancel();
        monitorCts?.Dispose();
        monitorCts = null;
    }

    /// <summary>
    /// Every 2 s: is the service still there? A restart with a new token is picked up at once.
    /// After about 10 s without an answer, show "Reconnecting..." and go through the start
    /// order again (service, login task, our own backend).
    /// </summary>
    private async Task MonitorServiceAsync(CancellationToken ct)
    {
        var lastOk = DateTime.UtcNow;
        var failing = false;
        while (true)
        {
            try
            {
                await Task.Delay(ServiceCheckInterval, ct);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            if (ct.IsCancellationRequested || IsDisposed || shuttingDown || !backend.IsAttached)
            {
                return;
            }
            var ok = await backend.CheckHealthAsync(TimeSpan.FromSeconds(1.5));
            if (ct.IsCancellationRequested || IsDisposed || shuttingDown)
            {
                return;
            }
            if (ok)
            {
                if (failing)
                {
                    HostLog.Write("the service answers again");
                    failing = false;
                }
                lastOk = DateTime.UtcNow;
                continue;
            }
            if (!failing)
            {
                HostLog.Write("the service did not answer");
                failing = true;
            }

            // Restarted with a new token (or port)? Reload the page from it straight away.
            var now = await ServiceLink.FindHealthyAsync(backend.Http);
            if (ct.IsCancellationRequested || IsDisposed || shuttingDown)
            {
                return;
            }
            if (now != null && ServiceMatches(now) &&
                (now.Info.Port != backend.Port || now.Info.Token != backend.Token))
            {
                HostLog.Write("the service restarted; reloading the page from it");
                backend.Attach(now.Info);
                NavigateToApp();
                lastOk = DateTime.UtcNow;
                failing = false;
                continue;
            }

            if (DateTime.UtcNow - lastOk >= ServiceLostAfter)
            {
                HostLog.Write($"the service has not answered for {(DateTime.UtcNow - lastOk).TotalSeconds:0} s; reconnecting");
                _ = StartBackendAndNavigateAsync(keepPort: false, startingText: "Reconnecting...");
                return;
            }
        }
    }

    private void ShowWebView2Missing()
    {
        statusPanel.ShowMessage("One more thing is needed",
            "LED Sign Studio needs the Microsoft Edge WebView2 Runtime, which is missing on this PC. " +
            "Install it from Microsoft, then open the app again.",
            null, null,
            ("Get WebView2", () => OpenExternal("https://developer.microsoft.com/microsoft-edge/webview2/"), true),
            ("Close", Close, false));
    }

    private void OnBackendExited(uint code)
    {
        // During a start the start code reports the failure itself; before the page is up
        // there is nothing to keep alive. Attached to the service, the monitor watches instead.
        if (shuttingDown || IsDisposed || startInProgress || !pageShown || backend.IsAttached)
        {
            return;
        }
        var now = DateTime.UtcNow;
        restarts.RemoveAll(t => now - t > TimeSpan.FromMinutes(5));
        if (restarts.Count < 3)
        {
            // Bring it back on the same port and token, so the open page keeps working.
            restarts.Add(now);
            HostLog.Write($"backend exited (code {(int)code}), restarting");
            _ = RestartQuietlyAsync();
            return;
        }
        ShowStartFailure(new StartResult(StartFailure.Exited,
            $"The background helper stopped (exit code {(int)code}).", Backend.ReadLogTail()));
    }

    private async Task RestartQuietlyAsync()
    {
        StartResult result;
        startInProgress = true;
        try
        {
            result = await StartOwnBackendAsync(keepPort: true);
        }
        catch (Exception ex)
        {
            HostLog.Write($"backend restart threw: {ex}");
            result = new StartResult(StartFailure.LaunchFailed, ex.Message, null);
        }
        finally
        {
            startInProgress = false;
        }
        if (shuttingDown || IsDisposed)
        {
            return;
        }
        if (!result.Ok)
        {
            ShowStartFailure(result);
        }
        else if (backend.Url != navigatedUrl)
        {
            // The port changed after all, or the service took over: reload the page from there.
            NavigateToApp();
            StartServiceMonitor();
        }
    }

    // ---- WebView2 ---------------------------------------------------------------------

    private void ConfigureWebView(CoreWebView2 core)
    {
        var s = core.Settings;
        s.AreDevToolsEnabled = true; // F12
        s.IsStatusBarEnabled = false;
        s.IsZoomControlEnabled = false;
        s.IsGeneralAutofillEnabled = false;
        s.IsPasswordAutosaveEnabled = false;
        s.IsSwipeNavigationEnabled = false;
        s.AreDefaultScriptDialogsEnabled = true;

        core.NavigationStarting += OnNavigationStarting;
        core.NavigationCompleted += OnNavigationCompleted;
        core.NewWindowRequested += OnNewWindowRequested;
        core.DownloadStarting += OnDownloadStarting;
        core.ContextMenuRequested += OnContextMenuRequested;
        core.ProcessFailed += OnProcessFailed;
    }

    private bool IsAppUri(string uri) =>
        backend.Port > 0 && uri.StartsWith(backend.Origin + "/", StringComparison.OrdinalIgnoreCase);

    private void OnNavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
    {
        var uri = e.Uri;
        if (IsAppUri(uri) || uri.StartsWith("about:", StringComparison.OrdinalIgnoreCase) ||
            uri.StartsWith("data:", StringComparison.OrdinalIgnoreCase) ||
            uri.StartsWith("blob:", StringComparison.OrdinalIgnoreCase))
        {
            return;
        }
        e.Cancel = true;
        OpenIfWebLink(uri);
    }

    private void OnNavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs e)
    {
        if (e.IsSuccess)
        {
            if (!pageShown)
            {
                pageShown = true;
                statusPanel.Visible = false;
                webView.Focus();
                HostLog.Write("app page loaded");
            }
            return;
        }
        if (e.WebErrorStatus == CoreWebView2WebErrorStatus.OperationCanceled || pageShown)
        {
            return;
        }
        HostLog.Write($"app page failed to load: {e.WebErrorStatus}");
        statusPanel.ShowMessage("LED Sign Studio could not start",
            "The app page did not load.", AppPaths.ServerLog, Backend.ReadLogTail(),
            ("Try again", () => _ = StartBackendAndNavigateAsync(keepPort: true), true),
            ("Open log folder", OpenLogFolder, false));
    }

    private void OnNewWindowRequested(object? sender, CoreWebView2NewWindowRequestedEventArgs e)
    {
        // Keep everything in this one window; web links open in the default browser.
        e.Handled = true;
        OpenIfWebLink(e.Uri);
    }

    private void OnDownloadStarting(object? sender, CoreWebView2DownloadStartingEventArgs e)
    {
        var deferral = e.GetDeferral();
        BeginInvoke(() =>
        {
            try
            {
                var suggested = e.ResultFilePath;
                var ext = Path.GetExtension(suggested);
                using var dialog = new SaveFileDialog
                {
                    Title = "Save file",
                    FileName = Path.GetFileName(suggested),
                    InitialDirectory = Path.GetDirectoryName(suggested) ?? "",
                    OverwritePrompt = true,
                    AddExtension = true,
                    Filter = string.IsNullOrEmpty(ext)
                        ? "All files (*.*)|*.*"
                        : $"{ext.TrimStart('.').ToUpperInvariant()} file (*{ext})|*{ext}|All files (*.*)|*.*",
                };
                if (dialog.ShowDialog(this) == DialogResult.OK)
                {
                    e.ResultFilePath = dialog.FileName;
                    e.Handled = true; // no browser download panel; the file goes where the user chose
                    HostLog.Write($"download saved to {dialog.FileName}");
                }
                else
                {
                    e.Cancel = true;
                }
            }
            catch (Exception ex)
            {
                HostLog.Write($"download dialog failed: {ex.Message}");
            }
            finally
            {
                deferral.Complete();
            }
        });
    }

    private static readonly HashSet<string> EditMenuItems = new(StringComparer.OrdinalIgnoreCase)
    {
        "undo", "redo", "cut", "copy", "paste", "pasteAndMatchStyle", "selectAll", "emoji", "spellCheck",
    };

    private void OnContextMenuRequested(object? sender, CoreWebView2ContextMenuRequestedEventArgs e)
    {
        // An app, not a browser: only the edit commands, and only where they make sense.
        var target = e.ContextMenuTarget;
        if (!target.IsEditable && !target.HasSelection)
        {
            e.Handled = true;
            return;
        }
        var items = e.MenuItems;
        for (var i = items.Count - 1; i >= 0; i--)
        {
            var item = items[i];
            if (item.Kind == CoreWebView2ContextMenuItemKind.Separator)
            {
                continue;
            }
            if (!EditMenuItems.Contains(item.Name))
            {
                items.RemoveAt(i);
            }
        }
        // Tidy separators left at the ends or doubled up.
        for (var i = items.Count - 1; i >= 0; i--)
        {
            var sep = items[i].Kind == CoreWebView2ContextMenuItemKind.Separator;
            var edge = i == 0 || i == items.Count - 1;
            var doubled = i > 0 && items[i - 1].Kind == CoreWebView2ContextMenuItemKind.Separator;
            if (sep && (edge || doubled))
            {
                items.RemoveAt(i);
            }
        }
        if (items.Count == 0)
        {
            e.Handled = true;
        }
    }

    private void OnProcessFailed(object? sender, CoreWebView2ProcessFailedEventArgs e)
    {
        HostLog.Write($"WebView2 process failed: {e.ProcessFailedKind} {e.Reason}");
        if (e.ProcessFailedKind is CoreWebView2ProcessFailedKind.RenderProcessExited
            or CoreWebView2ProcessFailedKind.RenderProcessUnresponsive)
        {
            if (DateTime.UtcNow - lastRenderReload > TimeSpan.FromSeconds(10))
            {
                lastRenderReload = DateTime.UtcNow;
                webView.CoreWebView2?.Reload();
            }
            return;
        }
        if (e.ProcessFailedKind == CoreWebView2ProcessFailedKind.BrowserProcessExited)
        {
            statusPanel.ShowMessage("The window stopped working",
                "Close LED Sign Studio and open it again.", AppPaths.HostLog, null,
                ("Close", Close, true));
        }
    }

    // ---- Keys -------------------------------------------------------------------------

    protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
    {
        // F12 also works while the start screen is up (the web view has no focus then).
        if (keyData == Keys.F12 && webView.CoreWebView2 != null)
        {
            webView.CoreWebView2.OpenDevToolsWindow();
            return true;
        }
        return base.ProcessCmdKey(ref msg, keyData);
    }

    // ---- Closing ----------------------------------------------------------------------

    protected override async void OnFormClosing(FormClosingEventArgs e)
    {
        if (closeChecking && e.CloseReason != CloseReason.WindowsShutDown)
        {
            // Already on the way out (saving or asking); a second click changes nothing.
            e.Cancel = true;
            base.OnFormClosing(e);
            return;
        }
        if (!closeConfirmed && e.CloseReason != CloseReason.WindowsShutDown && pageShown && backend.IsRunning)
        {
            e.Cancel = true;
            base.OnFormClosing(e);
            closeChecking = true;
            try
            {
                // Closing does not stop the service, so an upload it is doing carries on.
                if (e.CloseReason == CloseReason.UserClosing && !backend.IsAttached)
                {
                    var busy = await backend.IsBusyAsync();
                    if (IsDisposed)
                    {
                        return;
                    }
                    if (busy && MessageBox.Show(this,
                            "The sign is still being updated. Close anyway?",
                            AppTitle, MessageBoxButtons.YesNo, MessageBoxIcon.Warning,
                            MessageBoxDefaultButton.Button2) != DialogResult.Yes)
                    {
                        return;
                    }
                }
                await SavePageAsync(TimeSpan.FromSeconds(2));
            }
            finally
            {
                closeChecking = false;
            }
            if (IsDisposed)
            {
                return;
            }
            closeConfirmed = true;
            Close();
            return;
        }

        base.OnFormClosing(e);
        if (e.Cancel)
        {
            return;
        }
        shuttingDown = true;
        StopServiceMonitor();
        SaveWindowState();
        HostLog.Write($"closing ({e.CloseReason})");
        // Gone from the screen right away; our own backend gets up to 3 s to let go of the sign.
        Hide();
    }

    /// <summary>Asks the page to save the project now and waits (briefly) until it has.</summary>
    private async Task SavePageAsync(TimeSpan budget)
    {
        var core = webView.CoreWebView2;
        if (core == null)
        {
            return;
        }
        var sw = Stopwatch.StartNew();
        var state = "?";
        try
        {
            while (sw.Elapsed < budget)
            {
                var call = core.ExecuteScriptAsync("window.ledsignBeforeClose ? window.ledsignBeforeClose() : 'saved'");
                if (await Task.WhenAny(call, Task.Delay(budget - sw.Elapsed)) != call)
                {
                    state = "no answer";
                    break;
                }
                state = (await call).Trim('"');
                if (state != "saving")
                {
                    break;
                }
                await Task.Delay(100);
            }
        }
        catch (Exception ex)
        {
            state = ex.Message;
        }
        HostLog.Write($"save before close: {state} ({sw.ElapsedMilliseconds} ms)");
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        base.OnFormClosed(e);
        try
        {
            webView.Dispose();
        }
        catch (Exception ex)
        {
            HostLog.Write($"web view dispose failed: {ex.Message}");
        }
        if (backend.IsAttached)
        {
            // The service runs on without this window: no /api/shutdown, nothing to kill.
            HostLog.Write($"leaving the service on port {backend.Port} running");
        }
        else
        {
            backend.ShutdownGracefully(TimeSpan.FromSeconds(3));
        }
        backend.Dispose();
    }

    // ---- Helpers ----------------------------------------------------------------------

    private static void OpenIfWebLink(string uri)
    {
        if (uri.StartsWith("https://", StringComparison.OrdinalIgnoreCase) ||
            uri.StartsWith("http://", StringComparison.OrdinalIgnoreCase) ||
            uri.StartsWith("mailto:", StringComparison.OrdinalIgnoreCase))
        {
            OpenExternal(uri);
        }
    }

    private static void OpenExternal(string target)
    {
        try
        {
            Process.Start(new ProcessStartInfo(target) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            HostLog.Write($"could not open {target}: {ex.Message}");
        }
    }

    private static void OpenLogFolder()
    {
        try
        {
            AppPaths.EnsureDataDirs();
            if (File.Exists(AppPaths.ServerLog))
            {
                Process.Start(new ProcessStartInfo("explorer.exe", $"/select,\"{AppPaths.ServerLog}\"") { UseShellExecute = true });
            }
            else
            {
                Process.Start(new ProcessStartInfo(AppPaths.LogsDir) { UseShellExecute = true });
            }
        }
        catch (Exception ex)
        {
            HostLog.Write($"could not open the log folder: {ex.Message}");
        }
    }

    private static Icon? LoadIcon()
    {
        try
        {
            using var s = typeof(MainForm).Assembly.GetManifestResourceStream("LedSignStudio.icon.ico");
            return s == null ? null : new Icon(s);
        }
        catch
        {
            return null;
        }
    }

    private static Image? LoadLogo()
    {
        try
        {
            using var s = typeof(MainForm).Assembly.GetManifestResourceStream("LedSignStudio.icon.png");
            if (s == null)
            {
                return null;
            }
            using var tmp = Image.FromStream(s);
            return new Bitmap(tmp);
        }
        catch
        {
            return null;
        }
    }
}
