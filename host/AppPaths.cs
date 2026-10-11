namespace LedSignStudio;

/// <summary>
/// Where everything lives, found from the exe's own location. All runtime data stays under app\data.
/// </summary>
internal static class AppPaths
{
    public static readonly string Root = FindRoot();
    public static string AppDir => Path.Combine(Root, "app");
    public static string ServerScript => Path.Combine(AppDir, "server.py");
    public static string VenvPython => Path.Combine(Root, ".venv", "Scripts", "pythonw.exe");
    public static string DataDir => Path.Combine(AppDir, "data");
    public static string LogsDir => Path.Combine(DataDir, "logs");
    public static string ServerLog => Path.Combine(LogsDir, "server.log");
    public static string ServerOutputLog => Path.Combine(LogsDir, "server-output.log");
    public static string HostLog => Path.Combine(LogsDir, "host.log");
    public static string WebViewData => Path.Combine(DataDir, "webview2");
    public static string WindowFile => Path.Combine(DataDir, "window.json");
    public static string HostStateFile => Path.Combine(DataDir, "host.json");
    public static string PublishedExe => Path.Combine(Root, "LedSignStudio", "LedSignStudio.exe");

    /// <summary>
    /// The pythonw.exe that runs the backend: the repo's .venv, else the first pythonw.exe on PATH.
    /// Null when there is neither.
    /// </summary>
    public static string? FindPython()
    {
        if (File.Exists(VenvPython))
        {
            return VenvPython;
        }
        var path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var part in path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try
            {
                var candidate = Path.Combine(part.Trim().Trim('"'), "pythonw.exe");
                if (Path.IsPathFullyQualified(candidate) && File.Exists(candidate))
                {
                    return candidate;
                }
            }
            catch (ArgumentException)
            {
                // A malformed PATH entry: skip it.
            }
        }
        return null;
    }

    /// <summary>
    /// The repo root: the first folder at or above the exe that holds app\server.py. The published
    /// exe sits in Root\LedSignStudio, a dev build in Root\host\bin\... If none is found, the
    /// folder above the exe's folder.
    /// </summary>
    private static string FindRoot()
    {
        var exeDir = new DirectoryInfo(AppContext.BaseDirectory);
        try
        {
            for (var dir = exeDir; dir != null; dir = dir.Parent)
            {
                if (File.Exists(Path.Combine(dir.FullName, "app", "server.py")))
                {
                    return dir.FullName;
                }
            }
        }
        catch
        {
            // Use the fallback below.
        }
        return (exeDir.Parent ?? exeDir).FullName;
    }

    public static void EnsureDataDirs()
    {
        Directory.CreateDirectory(DataDir);
        Directory.CreateDirectory(LogsDir);
    }
}

/// <summary>Tiny append-only log for the shell itself (app\data\logs\host.log).</summary>
internal static class HostLog
{
    private static readonly object Gate = new();

    public static void Init()
    {
        try
        {
            AppPaths.EnsureDataDirs();
            var f = new FileInfo(AppPaths.HostLog);
            if (f.Exists && f.Length > 512 * 1024)
            {
                File.Move(AppPaths.HostLog, AppPaths.HostLog + ".1", overwrite: true);
            }
        }
        catch
        {
            // Logging must never stop the app.
        }
    }

    public static void Write(string message)
    {
        try
        {
            var line = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} [{Environment.ProcessId}] {message}{Environment.NewLine}";
            lock (Gate)
            {
                File.AppendAllText(AppPaths.HostLog, line);
            }
        }
        catch
        {
            // Logging must never stop the app.
        }
    }
}
