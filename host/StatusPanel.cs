using System.Drawing.Drawing2D;

namespace LedSignStudio;

/// <summary>
/// The dark screen shown while the app starts, and the plain message screen shown when it
/// cannot start. Covers the web view until the page has loaded.
/// </summary>
internal sealed class StatusPanel : Control
{
    public static readonly Color Background = Color.FromArgb(14, 16, 22);
    private static readonly Color TitleColor = Color.FromArgb(242, 244, 248);
    private static readonly Color TextColor = Color.FromArgb(196, 203, 214);
    private static readonly Color MutedColor = Color.FromArgb(132, 141, 158);
    private static readonly Color BoxColor = Color.FromArgb(22, 25, 34);

    private const int DotCount = 10;

    private readonly Image? logo;
    private readonly System.Windows.Forms.Timer animation;
    private readonly TextBox detailsBox;
    private readonly List<Button> buttons = [];
    private bool starting = true;
    private string title = "LED Sign Studio";
    private string status = "Starting...";
    private string? message;
    private string? logPath;
    private int tick;
    private int fontsDpi;
    private Font? titleFont;
    private Font? textFont;
    private Font? smallFont;
    private Font? monoFont;
    private Rectangle dotsRect;

    // Layout results used by OnPaint.
    private Rectangle logoRect;
    private Rectangle titleRect;
    private Rectangle statusRect;
    private Rectangle messageRect;
    private Rectangle logLabelRect;
    private Rectangle logPathRect;

    public StatusPanel(Image? logo)
    {
        this.logo = logo;
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.UserPaint | ControlStyles.ResizeRedraw, true);
        BackColor = Background;
        ForeColor = TextColor;

        detailsBox = new TextBox
        {
            Multiline = true,
            ReadOnly = true,
            BorderStyle = BorderStyle.None,
            BackColor = BoxColor,
            ForeColor = TextColor,
            ScrollBars = ScrollBars.Vertical,
            WordWrap = true,
            Visible = false,
            TabStop = false,
        };
        detailsBox.HandleCreated += (_, _) => Native.SetWindowTheme(detailsBox.Handle, "DarkMode_Explorer", null);
        Controls.Add(detailsBox);

        animation = new System.Windows.Forms.Timer { Interval = 50 };
        animation.Tick += (_, _) =>
        {
            tick++;
            if (!dotsRect.IsEmpty)
            {
                Invalidate(Rectangle.Inflate(dotsRect, 8, 8));
            }
        };
        animation.Start();
    }

    private float DpiScale => DeviceDpi / 96f;

    private int S(float v) => (int)Math.Round(v * DpiScale);

    public void ShowStarting(string text = "Starting...")
    {
        starting = true;
        title = "LED Sign Studio";
        status = text;
        message = null;
        logPath = null;
        detailsBox.Visible = false;
        ClearButtons();
        animation.Start();
        Visible = true;
        BringToFront();
        PerformLayout();
        Invalidate();
    }

    public void ShowMessage(string heading, string text, string? log, string? details,
        params (string Text, Action OnClick, bool Primary)[] actions)
    {
        starting = false;
        animation.Stop();
        title = heading;
        message = text;
        logPath = log;
        detailsBox.Text = details ?? "";
        detailsBox.Visible = !string.IsNullOrWhiteSpace(details);
        ClearButtons();
        foreach (var (label, onClick, primary) in actions)
        {
            var b = MakeButton(label, primary);
            b.Click += (_, _) => onClick();
            buttons.Add(b);
            Controls.Add(b);
        }
        Visible = true;
        BringToFront();
        PerformLayout();
        Invalidate();
        if (buttons.Count > 0)
        {
            buttons[0].Focus();
        }
    }

    private void ClearButtons()
    {
        foreach (var b in buttons)
        {
            Controls.Remove(b);
            b.Dispose();
        }
        buttons.Clear();
    }

    private static Button MakeButton(string text, bool primary)
    {
        var b = new Button
        {
            Text = text,
            FlatStyle = FlatStyle.Flat,
            UseVisualStyleBackColor = false,
            BackColor = primary ? Color.FromArgb(37, 99, 235) : Color.FromArgb(38, 43, 56),
            ForeColor = Color.White,
            Cursor = Cursors.Hand,
            TabStop = true,
        };
        b.FlatAppearance.BorderColor = primary ? Color.FromArgb(59, 130, 246) : Color.FromArgb(58, 65, 82);
        b.FlatAppearance.MouseOverBackColor = primary ? Color.FromArgb(59, 116, 240) : Color.FromArgb(50, 56, 72);
        b.FlatAppearance.MouseDownBackColor = primary ? Color.FromArgb(29, 78, 216) : Color.FromArgb(30, 34, 45);
        return b;
    }

    private void EnsureFonts()
    {
        if (fontsDpi == DeviceDpi && titleFont != null)
        {
            return;
        }
        fontsDpi = DeviceDpi;
        titleFont?.Dispose();
        textFont?.Dispose();
        smallFont?.Dispose();
        monoFont?.Dispose();
        float Px(float pt) => pt * DeviceDpi / 72f;
        titleFont = new Font("Segoe UI Semibold", Px(17f), FontStyle.Regular, GraphicsUnit.Pixel);
        textFont = new Font("Segoe UI", Px(11f), FontStyle.Regular, GraphicsUnit.Pixel);
        smallFont = new Font("Segoe UI", Px(9.5f), FontStyle.Regular, GraphicsUnit.Pixel);
        monoFont = new Font("Consolas", Px(9.5f), FontStyle.Regular, GraphicsUnit.Pixel);
        detailsBox.Font = monoFont;
        foreach (var b in buttons)
        {
            b.Font = textFont;
        }
    }

    private const TextFormatFlags WrapCenter =
        TextFormatFlags.WordBreak | TextFormatFlags.HorizontalCenter | TextFormatFlags.NoPrefix |
        TextFormatFlags.TextBoxControl;

    protected override void OnLayout(LayoutEventArgs e)
    {
        base.OnLayout(e);
        EnsureFonts();
        var w = ClientSize.Width;
        var h = ClientSize.Height;
        var contentW = Math.Max(S(200), Math.Min(w - S(48), S(620)));
        var left = (w - contentW) / 2;

        int Measure(string text, Font font, int width) =>
            TextRenderer.MeasureText(text, font, new Size(width, int.MaxValue), WrapCenter).Height;

        var logoSize = starting ? S(96) : S(72);
        var titleH = Measure(title, titleFont!, contentW);
        var total = logoSize + S(18) + titleH;

        int statusH = 0, messageH = 0, logLabelH = 0, logPathH = 0, detailsH = 0, buttonsH = 0;
        if (starting)
        {
            statusH = Measure(status, textFont!, contentW);
            total += S(8) + statusH + S(22) + S(12);
        }
        else
        {
            messageH = message == null ? 0 : Measure(message, textFont!, contentW);
            total += S(10) + messageH;
            if (logPath != null)
            {
                logLabelH = Measure("Details are in the log file:", smallFont!, contentW);
                logPathH = Measure(logPath, monoFont!, contentW);
                total += S(16) + logLabelH + S(2) + logPathH;
            }
            if (detailsBox.Visible)
            {
                detailsH = S(150);
                total += S(14) + detailsH;
            }
            if (buttons.Count > 0)
            {
                buttonsH = S(38);
                total += S(22) + buttonsH;
            }
        }

        var y = Math.Max(S(24), (h - total) / 2);
        logoRect = new Rectangle((w - logoSize) / 2, y, logoSize, logoSize);
        y += logoSize + S(18);
        titleRect = new Rectangle(left, y, contentW, titleH);
        y += titleH;

        if (starting)
        {
            y += S(8);
            statusRect = new Rectangle(left, y, contentW, statusH);
            y += statusH + S(22);
            var dot = S(10);
            var gap = S(8);
            var dotsW = DotCount * dot + (DotCount - 1) * gap;
            dotsRect = new Rectangle((w - dotsW) / 2, y, dotsW, dot);
            messageRect = logLabelRect = logPathRect = Rectangle.Empty;
            return;
        }

        dotsRect = Rectangle.Empty;
        statusRect = Rectangle.Empty;
        y += S(10);
        messageRect = new Rectangle(left, y, contentW, messageH);
        y += messageH;
        if (logPath != null)
        {
            y += S(16);
            logLabelRect = new Rectangle(left, y, contentW, logLabelH);
            y += logLabelH + S(2);
            logPathRect = new Rectangle(left, y, contentW, logPathH);
            y += logPathH;
        }
        else
        {
            logLabelRect = logPathRect = Rectangle.Empty;
        }
        if (detailsBox.Visible)
        {
            y += S(14);
            var pad = S(10);
            detailsBox.Bounds = new Rectangle(left + pad, y + pad, contentW - 2 * pad, detailsH - 2 * pad);
            detailsBox.Tag = new Rectangle(left, y, contentW, detailsH);
            y += detailsH;
        }
        if (buttons.Count > 0)
        {
            y += S(22);
            var widths = buttons.Select(b =>
                Math.Max(S(120), TextRenderer.MeasureText(b.Text, textFont!).Width + S(36))).ToArray();
            var gap = S(10);
            var rowW = widths.Sum() + gap * (buttons.Count - 1);
            var x = (w - rowW) / 2;
            for (var i = 0; i < buttons.Count; i++)
            {
                buttons[i].Font = textFont;
                buttons[i].Bounds = new Rectangle(x, y, widths[i], buttonsH);
                x += widths[i] + gap;
            }
        }
    }

    protected override void OnDpiChangedAfterParent(EventArgs e)
    {
        base.OnDpiChangedAfterParent(e);
        PerformLayout();
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        EnsureFonts();
        var g = e.Graphics;
        g.Clear(Background);
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.InterpolationMode = InterpolationMode.HighQualityBicubic;
        g.PixelOffsetMode = PixelOffsetMode.HighQuality;

        if (logo != null)
        {
            g.DrawImage(logo, logoRect);
        }
        TextRenderer.DrawText(g, title, titleFont, titleRect, TitleColor, WrapCenter);

        if (starting)
        {
            TextRenderer.DrawText(g, status, textFont, statusRect, MutedColor, WrapCenter);
            PaintDots(g);
            return;
        }

        if (message != null)
        {
            TextRenderer.DrawText(g, message, textFont, messageRect, TextColor, WrapCenter);
        }
        if (logPath != null)
        {
            TextRenderer.DrawText(g, "Details are in the log file:", smallFont, logLabelRect, MutedColor, WrapCenter);
            TextRenderer.DrawText(g, logPath, monoFont, logPathRect, TextColor, WrapCenter);
        }
        if (detailsBox.Visible && detailsBox.Tag is Rectangle box)
        {
            using var brush = new SolidBrush(BoxColor);
            using var path = RoundRect(box, S(6));
            g.FillPath(brush, path);
        }
    }

    private void PaintDots(Graphics g)
    {
        if (dotsRect.IsEmpty)
        {
            return;
        }
        var dot = dotsRect.Height;
        var gap = DotCount > 1 ? (dotsRect.Width - DotCount * dot) / (DotCount - 1) : 0;
        var phase = tick * 0.35;
        for (var i = 0; i < DotCount; i++)
        {
            // A soft wave of light runs along a rainbow row of LEDs.
            var wave = Math.Cos((i - phase) * 0.7);
            var level = 0.18 + 0.82 * Math.Pow(Math.Max(0, wave), 3);
            var hue = (i / (double)DotCount * 300 + tick * 2) % 360;
            var c = FromHsv(hue, 1, level);
            var x = dotsRect.X + i * (dot + gap);
            var r = new Rectangle(x, dotsRect.Y, dot, dot);
            if (level > 0.4)
            {
                var glowAlpha = (int)(90 * (level - 0.4) / 0.6);
                using var glow = new SolidBrush(Color.FromArgb(glowAlpha, c));
                g.FillEllipse(glow, Rectangle.Inflate(r, dot / 3, dot / 3));
            }
            using var brush = new SolidBrush(c);
            g.FillEllipse(brush, r);
        }
    }

    private static Color FromHsv(double h, double s, double v)
    {
        var c = v * s;
        var x = c * (1 - Math.Abs(h / 60 % 2 - 1));
        var m = v - c;
        var (r, g, b) = (h % 360) switch
        {
            < 60 => (c, x, 0d),
            < 120 => (x, c, 0d),
            < 180 => (0d, c, x),
            < 240 => (0d, x, c),
            < 300 => (x, 0d, c),
            _ => (c, 0d, x),
        };
        return Color.FromArgb((int)((r + m) * 255), (int)((g + m) * 255), (int)((b + m) * 255));
    }

    private static GraphicsPath RoundRect(Rectangle r, int radius)
    {
        var d = radius * 2;
        var p = new GraphicsPath();
        p.AddArc(r.X, r.Y, d, d, 180, 90);
        p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
        p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
        p.CloseFigure();
        return p;
    }

    protected override void OnVisibleChanged(EventArgs e)
    {
        base.OnVisibleChanged(e);
        if (!Visible)
        {
            animation.Stop();
        }
        else if (starting)
        {
            animation.Start();
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            animation.Dispose();
            titleFont?.Dispose();
            textFont?.Dispose();
            smallFont?.Dispose();
            monoFont?.Dispose();
        }
        base.Dispose(disposing);
    }
}
