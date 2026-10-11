using System.Text.Json;
using System.Text.Json.Serialization;

namespace LedSignStudio;

/// <summary>Remembered window position and size (app\data\window.json), in screen pixels.</summary>
internal sealed class WindowPlacement
{
    public int X { get; set; }
    public int Y { get; set; }
    public int Width { get; set; }
    public int Height { get; set; }
    public bool Maximized { get; set; }

    [JsonIgnore]
    public Rectangle Bounds => new(X, Y, Width, Height);

    public static WindowPlacement? Load()
    {
        try
        {
            if (!File.Exists(AppPaths.WindowFile))
            {
                return null;
            }
            var state = JsonSerializer.Deserialize<WindowPlacement>(File.ReadAllText(AppPaths.WindowFile));
            if (state == null || state.Width < 200 || state.Height < 150)
            {
                return null;
            }
            return state;
        }
        catch (Exception ex)
        {
            HostLog.Write($"could not read window.json: {ex.Message}");
            return null;
        }
    }

    public void Save()
    {
        try
        {
            AppPaths.EnsureDataDirs();
            File.WriteAllText(AppPaths.WindowFile,
                JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            HostLog.Write($"could not save window.json: {ex.Message}");
        }
    }

    /// <summary>True when enough of the title bar is on a screen to grab and move the window.</summary>
    public bool IsReachable()
    {
        var titleBar = new Rectangle(X + 40, Y, Math.Max(1, Width - 80), 32);
        foreach (var screen in Screen.AllScreens)
        {
            var hit = Rectangle.Intersect(screen.WorkingArea, titleBar);
            if (hit.Width >= 120 && hit.Height >= 16)
            {
                return true;
            }
        }
        return false;
    }
}
