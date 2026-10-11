using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace LedSignStudio;

/// <summary>
/// Creates the Start Menu and Desktop shortcuts when they are missing. Runs on every launch,
/// so the first run from the exe sets them up. No AppUserModelID is set anywhere:
/// Windows then groups the window under the exe, and pinning either the running window or the
/// shortcut gives the same taskbar button.
/// </summary>
internal static class Shortcuts
{
    private const string Name = "LED Sign Studio.lnk";
    private const string Description = "Draw, animate and send pictures to the LED sign";

    public static void EnsureCreated()
    {
        try
        {
            var exe = Environment.ProcessPath;
            if (string.IsNullOrEmpty(exe))
            {
                return;
            }
            // A dev build under host\bin should not take over the shortcuts.
            var hostDir = Path.Combine(AppPaths.Root, "host") + Path.DirectorySeparatorChar;
            if (exe.StartsWith(hostDir, StringComparison.OrdinalIgnoreCase))
            {
                HostLog.Write("dev build: shortcuts not created");
                return;
            }

            var startMenu = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
            var desktop = Environment.GetFolderPath(Environment.SpecialFolder.Desktop);
            foreach (var folder in new[] { startMenu, desktop })
            {
                if (string.IsNullOrEmpty(folder))
                {
                    continue;
                }
                var path = Path.Combine(folder, Name);
                if (File.Exists(path))
                {
                    continue;
                }
                Directory.CreateDirectory(folder);
                Create(path, exe);
                HostLog.Write($"created shortcut {path}");
            }
        }
        catch (Exception ex)
        {
            HostLog.Write($"could not create shortcuts: {ex.Message}");
        }
    }

    private static void Create(string linkPath, string exe)
    {
        var link = (IShellLinkW)new ShellLink();
        try
        {
            link.SetPath(exe);
            link.SetWorkingDirectory(Path.GetDirectoryName(exe) ?? "");
            link.SetDescription(Description);
            link.SetIconLocation(exe, 0);
            ((IPersistFile)link).Save(linkPath, true);
        }
        finally
        {
            Marshal.FinalReleaseComObject(link);
        }
    }

    [ComImport]
    [Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink
    {
    }

    [ComImport]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    [Guid("000214F9-0000-0000-C000-000000000046")]
    private interface IShellLinkW
    {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cch, IntPtr pfd, uint fFlags);
        void GetIDList(out IntPtr ppidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cch);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cch);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cch);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
        void GetHotkey(out short pwHotkey);
        void SetHotkey(short wHotkey);
        void GetShowCmd(out int piShowCmd);
        void SetShowCmd(int iShowCmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cch, out int piIcon);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
        void Resolve(IntPtr hwnd, uint fFlags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
    }
}
