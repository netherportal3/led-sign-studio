// LED Sign Studio: the Windows shell around the local web app.
//
// Usage: LedSignStudio.exe [--fake[=ok|not_found|bluetooth_off]]
//   --fake   passed through to app\server.py: a simulated sign, no Bluetooth (for testing).
//            A --fake window only attaches to a fake-sign service and never starts the real
//            login task.
//
// The window uses the always-on service (app\server.py --service, see app\data\service.json)
// when it is running or its login task can start it, and runs its own backend otherwise.
//
// Dev options:
//   LEDSIGN_SERVER_CMD replaces "pythonw.exe app\server.py" with another command, for example
//     a stub server:  set LEDSIGN_SERVER_CMD="C:\path\python.exe" "C:\path\stub.py"
//     The usual arguments are still appended (--port P --token T --parent-pid N [--fake...]).
//   LEDSIGN_SERVICE_JSON replaces the service.json path.
//   LEDSIGN_SERVICE_TASK replaces the login task name ("LED Sign Studio Service").
//
// Logs: app\data\logs\host.log (this shell), server-output.log (backend stdout/stderr),
// server.log (written by the backend itself).

using System.Diagnostics;

namespace LedSignStudio;

internal static class Program
{
    private const string MutexName = @"Local\LedSignStudio.SingleInstance.v1";

    /// <summary>Posted by a second launch to ask the running window to come to the front.</summary>
    public static readonly uint ShowMeMessage = Native.RegisterWindowMessageW("LedSignStudio.ShowMainWindow.v1");

    [STAThread]
    private static int Main(string[] args)
    {
        HostLog.Init();

        string? fakeArg = null;
        foreach (var a in args)
        {
            if (a == "--fake" || a.StartsWith("--fake=", StringComparison.Ordinal))
            {
                fakeArg = a;
            }
            else
            {
                HostLog.Write($"ignoring argument {a}");
            }
        }

        using var mutex = new Mutex(true, MutexName, out var createdNew);
        // A second launch focuses the running window. If that window is just closing (it lets go
        // of the sign for up to 3 s first), wait for it to finish and start normally instead.
        if (!createdNew && ActivateRunningInstance(mutex))
        {
            return 0;
        }

        try
        {
            HostLog.Write($"start {Environment.ProcessPath} root={AppPaths.Root}{(fakeArg != null ? " " + fakeArg : "")}");
            // The backend's stdout/stderr go to a file; keep it unbuffered and UTF-8.
            Environment.SetEnvironmentVariable("PYTHONUNBUFFERED", "1");
            Environment.SetEnvironmentVariable("PYTHONIOENCODING", "utf-8");

            ApplicationConfiguration.Initialize();
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
            Application.ThreadException += (_, e) => HostLog.Write($"UI error: {e.Exception}");
            AppDomain.CurrentDomain.UnhandledException += (_, e) => HostLog.Write($"fatal: {e.ExceptionObject}");

            using var backend = new Backend(fakeArg);
            Application.Run(new MainForm(backend, fakeArg != null));
            HostLog.Write("exit");
            return 0;
        }
        catch (Exception ex)
        {
            HostLog.Write($"fatal: {ex}");
            MessageBox.Show($"LED Sign Studio could not start.\n\n{ex.Message}\n\nDetails are in {AppPaths.HostLog}",
                "LED Sign Studio", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally
        {
            mutex.ReleaseMutex();
        }
    }

    /// <summary>
    /// A second launch: bring the first window forward and return true (then quit). Returns
    /// false once this launch owns the mutex because the other instance exited meanwhile.
    /// </summary>
    private static bool ActivateRunningInstance(Mutex mutex)
    {
        using var me = Process.GetCurrentProcess();
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (DateTime.UtcNow < deadline)
        {
            try
            {
                if (mutex.WaitOne(0))
                {
                    HostLog.Write("second launch: the other instance has exited; starting normally");
                    return false;
                }
            }
            catch (AbandonedMutexException)
            {
                HostLog.Write("second launch: the other instance ended abruptly; starting normally");
                return false;
            }
            foreach (var p in Process.GetProcessesByName(me.ProcessName))
            {
                using (p)
                {
                    if (p.Id == me.Id)
                    {
                        continue;
                    }
                    var hwnd = p.MainWindowHandle;
                    if (hwnd == IntPtr.Zero)
                    {
                        continue;
                    }
                    // This process was just launched by the user, so it may hand over the foreground.
                    Native.AllowSetForegroundWindow(p.Id);
                    if (Native.IsIconic(hwnd))
                    {
                        Native.ShowWindow(hwnd, Native.SW_RESTORE);
                    }
                    Native.SetForegroundWindow(hwnd);
                    Native.PostMessageW(hwnd, ShowMeMessage, IntPtr.Zero, IntPtr.Zero);
                    HostLog.Write($"second launch: focused the running window (pid {p.Id})");
                    return true;
                }
            }
            Thread.Sleep(200);
        }
        HostLog.Write("second launch: no running window found");
        return true;
    }
}
