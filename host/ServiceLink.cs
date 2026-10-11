using System.Diagnostics;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace LedSignStudio;

/// <summary>What app\data\service.json says: where the always-on service listens.</summary>
internal sealed record ServiceInfo(int Port, string Token, int Pid, string? Started, bool? Fake)
{
    public string Origin => $"http://127.0.0.1:{Port}";
}

/// <summary>A service that answered /api/health with its token.</summary>
internal sealed record HealthyService(ServiceInfo Info, bool Fake);

internal enum ServiceTaskState
{
    Missing,
    Disabled,
    Enabled,
}

/// <summary>
/// The always-on background service (app\server.py --service), started at logon by the
/// "LED Sign Studio Service" task. Only one process may own the sign's Bluetooth link, so
/// whenever the service exists the window shows the service's page instead of running a
/// backend of its own.
///
/// Dev options: LEDSIGN_SERVICE_JSON replaces the service.json path, LEDSIGN_SERVICE_TASK
/// replaces the task name (for example "\MyTests\Stub service").
/// </summary>
internal static class ServiceLink
{
    public const string JsonEnv = "LEDSIGN_SERVICE_JSON";
    public const string TaskEnv = "LEDSIGN_SERVICE_TASK";
    public const string DefaultTaskName = "LED Sign Studio Service";

    public static string JsonPath => Env(JsonEnv) ?? Path.Combine(AppPaths.DataDir, "service.json");
    public static string TaskName => Env(TaskEnv) ?? DefaultTaskName;
    public static bool TaskOverridden => Env(TaskEnv) != null;

    private static string? Env(string name)
    {
        var v = Environment.GetEnvironmentVariable(name);
        return string.IsNullOrWhiteSpace(v) ? null : v.Trim().Trim('"');
    }

    /// <summary>Reads service.json. Null when it is missing or does not make sense.</summary>
    public static ServiceInfo? Read()
    {
        var path = JsonPath;
        for (var tries = 0; tries < 3; tries++)
        {
            try
            {
                if (!File.Exists(path))
                {
                    return null;
                }
                string text;
                using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                using (var reader = new StreamReader(fs, Encoding.UTF8))
                {
                    text = reader.ReadToEnd();
                }
                using var doc = JsonDocument.Parse(text);
                var root = doc.RootElement;
                if (root.ValueKind != JsonValueKind.Object)
                {
                    return null;
                }
                var port = root.TryGetProperty("port", out var p) && p.TryGetInt32(out var pv) ? pv : 0;
                var token = root.TryGetProperty("token", out var t) && t.ValueKind == JsonValueKind.String ? t.GetString() : null;
                var pid = root.TryGetProperty("pid", out var d) && d.TryGetInt32(out var dv) ? dv : 0;
                var started = root.TryGetProperty("started", out var s) && s.ValueKind != JsonValueKind.Null ? s.ToString() : null;
                bool? fake = root.TryGetProperty("fake", out var f) && f.ValueKind is JsonValueKind.True or JsonValueKind.False
                    ? f.GetBoolean()
                    : null;
                if (port is < 1 or > 65535 || string.IsNullOrEmpty(token))
                {
                    return null;
                }
                return new ServiceInfo(port, token, pid, started, fake);
            }
            catch (IOException)
            {
                // Being rewritten right now: try again in a moment.
                Thread.Sleep(50);
            }
            catch (Exception)
            {
                // Half-written or broken JSON reads as no service.
                return null;
            }
        }
        return null;
    }

    /// <summary>True when /api/health at the service's port answers ok with its token.</summary>
    public static async Task<bool> IsHealthyAsync(HttpClient http, string origin, string token, TimeSpan timeout)
    {
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, $"{origin}/api/health");
            req.Headers.Add("X-Token", token);
            using var cts = new CancellationTokenSource(timeout);
            using var resp = await http.SendAsync(req, cts.Token).ConfigureAwait(false);
            if (resp.StatusCode != HttpStatusCode.OK)
            {
                return false;
            }
            var body = await resp.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
            using var doc = JsonDocument.Parse(body);
            return doc.RootElement.ValueKind == JsonValueKind.Object &&
                   doc.RootElement.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>Whether the service runs a simulated sign (GET /api/status "fake"). Null when it does not say.</summary>
    private static async Task<bool?> IsFakeAsync(HttpClient http, ServiceInfo info)
    {
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, $"{info.Origin}/api/status");
            req.Headers.Add("X-Token", info.Token);
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            using var resp = await http.SendAsync(req, cts.Token).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode)
            {
                return null;
            }
            var body = await resp.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
            using var doc = JsonDocument.Parse(body);
            return doc.RootElement.TryGetProperty("fake", out var f) && f.ValueKind is JsonValueKind.True or JsonValueKind.False
                ? f.GetBoolean()
                : null;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>The service from service.json, if it is up and answers with its token.</summary>
    public static async Task<HealthyService?> FindHealthyAsync(HttpClient http)
    {
        var info = Read();
        if (info == null || !await IsHealthyAsync(http, info.Origin, info.Token, TimeSpan.FromSeconds(1.5)).ConfigureAwait(false))
        {
            return null;
        }
        // The live answer wins, then service.json's "fake"; unknown counts as the real sign.
        var fake = await IsFakeAsync(http, info).ConfigureAwait(false) ?? info.Fake ?? false;
        return new HealthyService(info, fake);
    }

    /// <summary>Polls service.json and /api/health until a service answers, at most <paramref name="budget"/>.</summary>
    public static async Task<HealthyService?> WaitForHealthyAsync(HttpClient http, TimeSpan budget, CancellationToken ct = default)
    {
        var sw = Stopwatch.StartNew();
        while (true)
        {
            var found = await FindHealthyAsync(http).ConfigureAwait(false);
            if (found != null || sw.Elapsed >= budget)
            {
                return found;
            }
            await Task.Delay(300, ct).ConfigureAwait(false);
        }
    }

    // ---- The login task ------------------------------------------------------------------

    /// <summary>Whether the login task exists and is enabled (schtasks /query /xml). Blocks briefly.</summary>
    public static ServiceTaskState QueryTask()
    {
        var name = TaskName;
        var (code, output) = RunSchtasks(["/query", "/tn", name, "/xml"], TimeSpan.FromSeconds(5));
        if (code != 0)
        {
            HostLog.Write($"service task \"{name}\" not found (schtasks exit {code})");
            return ServiceTaskState.Missing;
        }
        // The task's own switch is Task/Settings/Enabled (absent means enabled). Triggers have
        // an Enabled of their own, so look only inside Settings, minus its nested blocks.
        var enabled = true;
        var settings = Regex.Match(output, @"<Settings>(.*?)</Settings>", RegexOptions.Singleline | RegexOptions.CultureInvariant);
        if (settings.Success)
        {
            var inner = Regex.Replace(settings.Groups[1].Value,
                @"<(IdleSettings|NetworkSettings|MaintenanceSettings|RestartOnFailure)>.*?</\1>", "",
                RegexOptions.Singleline | RegexOptions.CultureInvariant);
            var e = Regex.Match(inner, @"<Enabled>\s*(true|false)\s*</Enabled>",
                RegexOptions.CultureInvariant | RegexOptions.IgnoreCase);
            enabled = !e.Success || e.Groups[1].Value.Equals("true", StringComparison.OrdinalIgnoreCase);
        }
        HostLog.Write($"service task \"{name}\" found, {(enabled ? "enabled" : "disabled")}");
        return enabled ? ServiceTaskState.Enabled : ServiceTaskState.Disabled;
    }

    /// <summary>Asks Task Scheduler to run the login task now (schtasks /run). Blocks briefly.</summary>
    public static bool RunTask()
    {
        var name = TaskName;
        var (code, output) = RunSchtasks(["/run", "/tn", name], TimeSpan.FromSeconds(5));
        HostLog.Write($"schtasks /run \"{name}\": exit {code} {output.Trim()}");
        return code == 0;
    }

    private static (int Code, string Output) RunSchtasks(string[] args, TimeSpan timeout)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.SystemDirectory, "schtasks.exe"),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            foreach (var a in args)
            {
                psi.ArgumentList.Add(a);
            }
            using var p = Process.Start(psi);
            if (p == null)
            {
                return (-1, "could not start schtasks");
            }
            var stdout = p.StandardOutput.ReadToEndAsync();
            var stderr = p.StandardError.ReadToEndAsync();
            if (!p.WaitForExit(timeout))
            {
                try
                {
                    p.Kill();
                }
                catch
                {
                    // Gone already.
                }
                return (-1, "schtasks timed out");
            }
            p.WaitForExit();
            // Some versions write UTF-16 when redirected; the bits we look for are ASCII either way.
            var text = (stdout.Result + stderr.Result).Replace("\0", "");
            return (p.ExitCode, text);
        }
        catch (Exception ex)
        {
            return (-1, ex.Message);
        }
    }
}
