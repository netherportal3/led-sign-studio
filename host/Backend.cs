using System.ComponentModel;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Win32.SafeHandles;

namespace LedSignStudio;

internal enum StartFailure
{
    None,
    MissingPython,
    MissingServer,
    LaunchFailed,
    Exited,
    Timeout,
    /// <summary>The backend refused to start because another process owns the sign.</summary>
    Locked,
}

internal enum BackendMode
{
    None,
    /// <summary>Our own backend, in our job object.</summary>
    Own,
    /// <summary>The always-on service: not ours to stop.</summary>
    Attached,
}

internal sealed record StartResult(StartFailure Failure, string Message, string? Details, int? ExitCode = null)
{
    public bool Ok => Failure == StartFailure.None;
    public static readonly StartResult Success = new(StartFailure.None, "", null);
}

/// <summary>
/// The backend the window talks to: either the always-on service (attached, see
/// <see cref="ServiceLink"/>; never stopped from here), or our own Python backend
/// (app\server.py) inside a Job object with KILL_ON_JOB_CLOSE, so it dies with this process
/// however this process ends. The venv pythonw.exe is a launcher that starts the real
/// interpreter as a child; the job covers that child too.
///
/// Dev option: set LEDSIGN_SERVER_CMD to replace "pythonw.exe app\server.py" with another
/// command (for example a stub server). The usual arguments are still appended:
/// --port P --token T --parent-pid N [--fake[=scenario]].
/// </summary>
internal sealed class Backend : IDisposable
{
    public const string ServerCmdEnv = "LEDSIGN_SERVER_CMD";
    private static readonly TimeSpan HealthTimeout = TimeSpan.FromSeconds(20);
    private static readonly Regex FakeArgPattern = new(@"^--fake(=[A-Za-z0-9_\-]+)?$", RegexOptions.CultureInvariant);

    private readonly SafeJobHandle job;
    private readonly HttpClient http;
    private readonly string? fakeArg;
    private readonly string ownToken;
    private readonly object gate = new();
    private ChildProcess? child;
    private int generation;
    private bool disposed;
    private string? serviceToken;

    public Backend(string? fakeArg)
    {
        if (fakeArg != null && !FakeArgPattern.IsMatch(fakeArg))
        {
            HostLog.Write($"ignoring malformed fake argument {fakeArg}");
            fakeArg = "--fake";
        }
        this.fakeArg = fakeArg;
        ownToken = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(16));
        job = CreateKillOnCloseJob();
        http = new HttpClient(new SocketsHttpHandler
        {
            UseProxy = false,
            ConnectTimeout = TimeSpan.FromSeconds(1),
        });
    }

    /// <summary>Raised on a thread-pool thread when the backend exits while it should be running.</summary>
    public event Action<uint>? ExitedUnexpectedly;

    public BackendMode Mode { get; private set; }
    public bool IsAttached => Mode == BackendMode.Attached;
    public bool Fake => fakeArg != null;
    public HttpClient Http => http;
    public int Port { get; private set; }
    public string Token => IsAttached && serviceToken != null ? serviceToken : ownToken;
    public string Origin => $"http://127.0.0.1:{Port}";
    public string Url => $"{Origin}/?t={Token}";

    /// <summary>Own backend: its process is alive. Attached: assumed up (the health monitor watches it).</summary>
    public bool IsRunning => IsAttached || (child is { } c && !c.HasExited);

    /// <summary>True when our own backend process is alive.</summary>
    public bool OwnProcessRunning => child is { } c && !c.HasExited;

    /// <summary>
    /// Use the always-on service from now on. Our own backend, if one is still running, is
    /// asked to let go of the sign first. The service itself is never stopped by this window.
    /// </summary>
    public void Attach(ServiceInfo service)
    {
        if (OwnProcessRunning)
        {
            ShutdownGracefully(TimeSpan.FromSeconds(3));
        }
        Stop();
        lock (gate)
        {
            Mode = BackendMode.Attached;
            Port = service.Port;
            serviceToken = service.Token;
        }
        // A restarted service keeps its port and token, so it can answer before it rewrites
        // service.json: the pid here may be the previous run's.
        HostLog.Write($"attached to the service on port {service.Port} (service.json: pid {service.Pid}, started {service.Started ?? "?"})");
    }

    /// <summary>One /api/health call to whichever backend is in use.</summary>
    public Task<bool> CheckHealthAsync(TimeSpan timeout) =>
        Port > 0 ? ServiceLink.IsHealthyAsync(http, Origin, Token, timeout) : Task.FromResult(false);

    /// <summary>app\server.py's exit code when another backend holds the sign (its backend.lock).</summary>
    public const int ExitLocked = 3;

    /// <summary>
    /// True when a backend that exited during start refused because another process owns the
    /// sign (exit code 3, logged as "another backend (pid N) owns the sign"). Only the exit code
    /// counts: an old line in server.log must not turn a crash into "the sign is taken".
    /// </summary>
    public static bool LooksLikeLockExit(StartResult result) =>
        result.Failure == StartFailure.Exited && result.ExitCode == ExitLocked;

    private static SafeJobHandle CreateKillOnCloseJob()
    {
        var handle = Native.CreateJobObjectW(IntPtr.Zero, null);
        if (handle.IsInvalid)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");
        }
        var info = new Native.JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = Native.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if (!Native.SetInformationJobObject(handle, Native.JobObjectExtendedLimitInformation, ref info,
                (uint)Marshal.SizeOf<Native.JOBOBJECT_EXTENDED_LIMIT_INFORMATION>()))
        {
            var err = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(err, "SetInformationJobObject failed");
        }
        return handle;
    }

    /// <summary>Starts (or restarts) the backend and waits until /api/health answers.</summary>
    public async Task<StartResult> StartAsync(bool keepPort, CancellationToken ct = default)
    {
        Stop();

        string exe;
        string prefixArgs;
        var overrideCmd = Environment.GetEnvironmentVariable(ServerCmdEnv);
        if (!string.IsNullOrWhiteSpace(overrideCmd))
        {
            (exe, prefixArgs) = SplitCommand(overrideCmd.Trim());
            HostLog.Write($"{ServerCmdEnv} override in use: {overrideCmd}");
        }
        else
        {
            var python = AppPaths.FindPython();
            if (python == null)
            {
                return new StartResult(StartFailure.MissingPython,
                    $"Python was not found. Create the virtual environment in {AppPaths.Root}\\.venv " +
                    "(see the README), or put pythonw.exe on the PATH.", null);
            }
            if (!File.Exists(AppPaths.ServerScript))
            {
                return new StartResult(StartFailure.MissingServer,
                    $"The app files were not found at {AppPaths.ServerScript}.", null);
            }
            if (python != AppPaths.VenvPython)
            {
                HostLog.Write($"no .venv in {AppPaths.Root}; using {python} from PATH");
            }
            exe = python;
            prefixArgs = Quote(AppPaths.ServerScript);
        }

        // Never take the service's fixed port: it would block the service from starting later.
        var servicePort = ServiceLink.Read()?.Port ?? 0;
        var preferred = keepPort && Port > 0 && Mode == BackendMode.Own ? Port : HostState.Load().LastPort;
        lock (gate)
        {
            Mode = BackendMode.Own;
            serviceToken = null;
        }
        Port = preferred > 0 && preferred != servicePort && IsPortFree(preferred) ? preferred : PickFreePort(servicePort);

        var args = $"{prefixArgs} --port {Port} --token {Token} --parent-pid {Environment.ProcessId}";
        if (fakeArg != null)
        {
            args += " " + fakeArg;
        }
        var commandLine = Quote(exe) + " " + args;
        var workDir = Directory.Exists(AppPaths.AppDir) ? AppPaths.AppDir : AppPaths.Root;

        ChildProcess started;
        try
        {
            started = Launch(commandLine, workDir);
        }
        catch (Exception ex)
        {
            HostLog.Write($"launch failed: {ex}");
            return new StartResult(StartFailure.LaunchFailed,
                $"The background helper could not be started ({ex.Message}).", null);
        }

        HostLog.Write($"backend started pid={started.Pid} port={Port}{(fakeArg != null ? " " + fakeArg : "")}");
        int gen;
        lock (gate)
        {
            child = started;
            gen = ++generation;
        }
        started.WatchExit(code =>
        {
            bool current;
            lock (gate)
            {
                current = gen == generation && !disposed;
            }
            HostLog.Write($"backend pid={started.Pid} exited with code {(int)code}{(current ? " (unexpected)" : "")}");
            if (current)
            {
                ExitedUnexpectedly?.Invoke(code);
            }
        });

        var result = await WaitForHealthAsync(started, ct).ConfigureAwait(false);
        if (result.Ok)
        {
            HostLog.Write($"backend healthy on port {Port}");
            HostState.Save(new HostState { LastPort = Port });
        }
        else
        {
            HostLog.Write($"backend start failed: {result.Failure} {result.Message}");
            Stop();
        }
        return result;
    }

    private async Task<StartResult> WaitForHealthAsync(ChildProcess proc, CancellationToken ct)
    {
        var sw = Stopwatch.StartNew();
        while (sw.Elapsed < HealthTimeout)
        {
            ct.ThrowIfCancellationRequested();
            if (proc.HasExited)
            {
                return new StartResult(StartFailure.Exited,
                    $"The background helper stopped while starting (exit code {(int)proc.ExitCode}).",
                    ReadLogTail(), (int)proc.ExitCode);
            }
            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, $"{Origin}/api/health");
                req.Headers.Add("X-Token", Token);
                using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                cts.CancelAfter(TimeSpan.FromSeconds(1.5));
                using var resp = await http.SendAsync(req, cts.Token).ConfigureAwait(false);
                if (resp.StatusCode == HttpStatusCode.OK)
                {
                    var body = await resp.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
                    if (IsOkJson(body))
                    {
                        return StartResult.Success;
                    }
                }
            }
            catch (Exception) when (!ct.IsCancellationRequested)
            {
                // Not listening yet.
            }
            await Task.Delay(200, ct).ConfigureAwait(false);
        }
        return new StartResult(StartFailure.Timeout,
            "The background helper did not respond in time.", ReadLogTail());
    }

    private static bool IsOkJson(string body)
    {
        try
        {
            using var doc = JsonDocument.Parse(body);
            return doc.RootElement.ValueKind == JsonValueKind.Object &&
                   doc.RootElement.TryGetProperty("ok", out var ok) &&
                   ok.ValueKind == JsonValueKind.True;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    /// <summary>True when the backend reports an upload in progress. Fails quietly to false.</summary>
    public async Task<bool> IsBusyAsync()
    {
        if (!IsRunning)
        {
            return false;
        }
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, $"{Origin}/api/status");
            req.Headers.Add("X-Token", Token);
            using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(600));
            using var resp = await http.SendAsync(req, cts.Token).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode)
            {
                return false;
            }
            var body = await resp.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
            using var doc = JsonDocument.Parse(body);
            return doc.RootElement.TryGetProperty("busy", out var busy) && busy.ValueKind == JsonValueKind.True;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Asks the backend to let go of the sign and exit (POST /api/shutdown), and waits until it
    /// has, at most <paramref name="budget"/> in all. Killing it outright leaves the Bluetooth
    /// link open, and Windows then holds it for about 14 s, so the next launch cannot find the
    /// sign. Whatever is still running afterwards is killed by <see cref="Stop"/> / Dispose.
    /// Blocks the calling thread.
    /// </summary>
    public void ShutdownGracefully(TimeSpan budget)
    {
        ChildProcess? c;
        lock (gate)
        {
            c = child;
            // From here on the backend exiting is expected, not a crash to recover from.
            generation++;
        }
        if (c == null || c.HasExited || Port <= 0)
        {
            return;
        }
        var sw = Stopwatch.StartNew();
        try
        {
            using var cts = new CancellationTokenSource(budget);
            var answer = Task.Run(async () =>
            {
                using var req = new HttpRequestMessage(HttpMethod.Post, $"{Origin}/api/shutdown");
                req.Headers.Add("X-Token", Token);
                req.Content = new StringContent("{}", Encoding.UTF8, "application/json");
                using var resp = await http.SendAsync(req, cts.Token).ConfigureAwait(false);
                return $"{(int)resp.StatusCode} {await resp.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false)}";
            }).GetAwaiter().GetResult();
            HostLog.Write($"shutdown answered in {sw.ElapsedMilliseconds} ms: {answer}");
        }
        catch (Exception ex)
        {
            HostLog.Write($"shutdown request failed after {sw.ElapsedMilliseconds} ms: {ex.GetType().Name} {ex.Message}");
        }
        var left = budget - sw.Elapsed;
        if (left > TimeSpan.Zero)
        {
            c.WaitForExit(left);
        }
        HostLog.Write(c.HasExited
            ? $"backend exited by itself after {sw.ElapsedMilliseconds} ms"
            : $"backend still running after {sw.ElapsedMilliseconds} ms; stopping it");
    }

    /// <summary>Kills every process in the job (the backend and anything it started).</summary>
    public void Stop()
    {
        ChildProcess? old;
        lock (gate)
        {
            old = child;
            child = null;
            generation++;
        }
        if (old == null)
        {
            return;
        }
        if (!job.IsInvalid && !job.IsClosed)
        {
            Native.TerminateJobObject(job, 1);
        }
        old.WaitForExit(TimeSpan.FromSeconds(3));
        old.Dispose();
    }

    public void Dispose()
    {
        lock (gate)
        {
            if (disposed)
            {
                return;
            }
            disposed = true;
        }
        Stop();
        // Closing the last job handle kills anything still in it (KILL_ON_JOB_CLOSE).
        job.Dispose();
        http.Dispose();
    }

    private ChildProcess Launch(string commandLine, string workDir)
    {
        AppPaths.EnsureDataDirs();
        // Capture the backend's stdout/stderr so a crash before its own logging starts
        // (an import error, for example) still leaves a trace.
        using var output = new FileStream(AppPaths.ServerOutputLog, FileMode.Create, FileAccess.Write,
            FileShare.ReadWrite | FileShare.Delete);
        var header = Encoding.UTF8.GetBytes(
            $"# {DateTime.Now:yyyy-MM-dd HH:mm:ss} {commandLine.Replace(Token, "<token>")}{Environment.NewLine}");
        output.Write(header);
        output.Flush();

        var outHandle = output.SafeFileHandle;
        if (!Native.SetHandleInformation(outHandle, Native.HANDLE_FLAG_INHERIT, Native.HANDLE_FLAG_INHERIT))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "SetHandleInformation failed");
        }

        // Inherit only the output handle, nothing else this process has open.
        var size = IntPtr.Zero;
        Native.InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
        var attrList = Marshal.AllocHGlobal(size);
        var handleList = Marshal.AllocHGlobal(IntPtr.Size);
        var attrInit = false;
        try
        {
            if (!Native.InitializeProcThreadAttributeList(attrList, 1, 0, ref size))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "InitializeProcThreadAttributeList failed");
            }
            attrInit = true;
            Marshal.WriteIntPtr(handleList, outHandle.DangerousGetHandle());
            if (!Native.UpdateProcThreadAttribute(attrList, 0, Native.PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                    handleList, IntPtr.Size, IntPtr.Zero, IntPtr.Zero))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "UpdateProcThreadAttribute failed");
            }

            var si = new Native.STARTUPINFOEX();
            si.StartupInfo.cb = Marshal.SizeOf<Native.STARTUPINFOEX>();
            si.StartupInfo.dwFlags = Native.STARTF_USESTDHANDLES;
            si.StartupInfo.hStdInput = IntPtr.Zero;
            si.StartupInfo.hStdOutput = outHandle.DangerousGetHandle();
            si.StartupInfo.hStdError = outHandle.DangerousGetHandle();
            si.lpAttributeList = attrList;

            var cmd = (commandLine + "\0").ToCharArray();
            var flags = Native.CREATE_SUSPENDED | Native.CREATE_NO_WINDOW | Native.EXTENDED_STARTUPINFO_PRESENT;
            if (!Native.CreateProcessW(null, cmd, IntPtr.Zero, IntPtr.Zero, true, flags, IntPtr.Zero, workDir,
                    ref si, out var pi))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }

            // Join the job while still suspended, so the interpreter it starts is in the job too.
            if (!Native.AssignProcessToJobObject(job, pi.hProcess))
            {
                var err = Marshal.GetLastWin32Error();
                Native.TerminateProcess(pi.hProcess, 1);
                Native.CloseHandle(pi.hThread);
                Native.CloseHandle(pi.hProcess);
                throw new Win32Exception(err, "AssignProcessToJobObject failed");
            }
            Native.ResumeThread(pi.hThread);
            Native.CloseHandle(pi.hThread);
            return new ChildProcess(pi.hProcess, pi.dwProcessId);
        }
        finally
        {
            if (attrInit)
            {
                Native.DeleteProcThreadAttributeList(attrList);
            }
            Marshal.FreeHGlobal(attrList);
            Marshal.FreeHGlobal(handleList);
        }
    }

    /// <summary>Last lines of the captured output, or of server.log if there was none.</summary>
    public static string? ReadLogTail()
    {
        static string? Tail(string path, int lines)
        {
            try
            {
                if (!File.Exists(path))
                {
                    return null;
                }
                using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                var len = fs.Length;
                var take = (int)Math.Min(len, 8192);
                fs.Seek(len - take, SeekOrigin.Begin);
                var buf = new byte[take];
                fs.ReadExactly(buf);
                var all = Encoding.UTF8.GetString(buf).Replace("\r\n", "\n").Split('\n')
                    .Where(l => l.Length > 0 && !l.StartsWith("# ", StringComparison.Ordinal)).ToArray();
                if (all.Length == 0)
                {
                    return null;
                }
                return string.Join(Environment.NewLine, all.Skip(Math.Max(0, all.Length - lines)));
            }
            catch
            {
                return null;
            }
        }

        return Tail(AppPaths.ServerOutputLog, 14) ?? Tail(AppPaths.ServerLog, 10);
    }

    private static bool IsPortFree(int port)
    {
        try
        {
            var l = new TcpListener(IPAddress.Loopback, port) { ExclusiveAddressUse = true };
            l.Start();
            l.Stop();
            return true;
        }
        catch (SocketException)
        {
            return false;
        }
    }

    private static int PickFreePort(int avoid)
    {
        for (var i = 0; ; i++)
        {
            var l = new TcpListener(IPAddress.Loopback, 0);
            l.Start();
            var port = ((IPEndPoint)l.LocalEndpoint).Port;
            l.Stop();
            if (port != avoid || i >= 5)
            {
                return port;
            }
        }
    }

    private static (string Exe, string Args) SplitCommand(string cmd)
    {
        if (cmd.StartsWith('"'))
        {
            var end = cmd.IndexOf('"', 1);
            if (end > 0)
            {
                return (cmd[1..end], cmd[(end + 1)..].Trim());
            }
        }
        var space = cmd.IndexOf(' ');
        return space < 0 ? (cmd, "") : (cmd[..space], cmd[(space + 1)..].Trim());
    }

    /// <summary>Quotes one argument by the CommandLineToArgvW rules.</summary>
    private static string Quote(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny([' ', '\t', '"']) < 0)
        {
            return arg;
        }
        var sb = new StringBuilder("\"");
        var backslashes = 0;
        foreach (var c in arg)
        {
            if (c == '\\')
            {
                backslashes++;
                continue;
            }
            if (c == '"')
            {
                sb.Append('\\', backslashes * 2 + 1);
            }
            else
            {
                sb.Append('\\', backslashes);
            }
            backslashes = 0;
            sb.Append(c);
        }
        sb.Append('\\', backslashes * 2);
        sb.Append('"');
        return sb.ToString();
    }

    /// <summary>A process handle we own, with exit notification.</summary>
    private sealed class ChildProcess : IDisposable
    {
        private readonly ManualResetEvent waitHandle;
        private RegisteredWaitHandle? registration;

        public ChildProcess(IntPtr hProcess, int pid)
        {
            Pid = pid;
            waitHandle = new ManualResetEvent(false)
            {
                SafeWaitHandle = new SafeWaitHandle(hProcess, ownsHandle: true),
            };
        }

        public int Pid { get; }

        public bool HasExited
        {
            get
            {
                try
                {
                    return waitHandle.WaitOne(0);
                }
                catch (ObjectDisposedException)
                {
                    return true;
                }
            }
        }

        public uint ExitCode
        {
            get
            {
                try
                {
                    return Native.GetExitCodeProcess(waitHandle.SafeWaitHandle.DangerousGetHandle(), out var code)
                        ? code
                        : 0;
                }
                catch (ObjectDisposedException)
                {
                    return 0;
                }
            }
        }

        public void WatchExit(Action<uint> onExit)
        {
            registration = ThreadPool.RegisterWaitForSingleObject(waitHandle,
                (_, _) => onExit(ExitCode), null, Timeout.Infinite, executeOnlyOnce: true);
        }

        public void WaitForExit(TimeSpan timeout)
        {
            try
            {
                waitHandle.WaitOne(timeout);
            }
            catch (ObjectDisposedException)
            {
                // Already gone.
            }
        }

        public void Dispose()
        {
            var r = registration;
            registration = null;
            if (r != null)
            {
                using var done = new ManualResetEvent(false);
                if (r.Unregister(done))
                {
                    done.WaitOne(TimeSpan.FromSeconds(2));
                }
            }
            waitHandle.Dispose();
        }
    }
}

/// <summary>Small bits of shell state kept between launches (app\data\host.json).</summary>
internal sealed class HostState
{
    public int LastPort { get; set; }

    public static HostState Load()
    {
        try
        {
            if (File.Exists(AppPaths.HostStateFile))
            {
                return JsonSerializer.Deserialize<HostState>(File.ReadAllText(AppPaths.HostStateFile)) ?? new HostState();
            }
        }
        catch
        {
            // A broken file just means no remembered state.
        }
        return new HostState();
    }

    public static void Save(HostState state)
    {
        try
        {
            AppPaths.EnsureDataDirs();
            File.WriteAllText(AppPaths.HostStateFile, JsonSerializer.Serialize(state));
        }
        catch (Exception ex)
        {
            HostLog.Write($"could not save host state: {ex.Message}");
        }
    }
}
