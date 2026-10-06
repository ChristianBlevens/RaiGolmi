// The launcher: the OS in one window on Windows.
//
// It lives outside the OS and the OS does not know it exists. QEMU runs with no window of
// its own and hands the guest's screen to this program over its D-Bus display (Display.cs):
// the guest renders on the GPU through virgl, and each frame arrives as a D3D11 texture
// shared on the GPU (Renderer.cs). This program's window is the only one, and it draws that
// screen, sends it the keyboard and pointer, and turns closing into a clean shutdown.
//
// ⚠ Why QEMU's own window is not used: GTK recomputes its window's frame styles on every
// geometry-hint change (gdk/win32 _gdk_win32_window_update_style_bits), which QEMU makes on
// every guest mode change, so a frame stripped from outside comes back. And its close is
// qmp_quit, a power cut.
//
// The log and a snapshot of the guest's screen are written beside RaiGolmi.exe, the one
// folder remote tooling can read.
//
// .NET 8, built by the root build.bat. QEMU is MSYS2's: its Windows build has virgl, ANGLE and the
// D-Bus display, and QEMU's own GTK GL window crashes there.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

static class Launcher
{
    const string Title = "RaiGolmi";
    // MSYS2's UCRT64 tree; RAIGOLMI_MSYS2 names an MSYS2 installed elsewhere, as for build.ps1.
    static readonly string QemuDir = Path.Combine(
        Environment.GetEnvironmentVariable("RAIGOLMI_MSYS2") ?? @"C:\msys64", "ucrt64");

    [STAThread]
    static int Main()
    {
        Native.SetProcessDPIAware();
        bool first;
        using (var single = new Mutex(true, @"Local\RaiGolmi-launcher", out first))
        {
            if (!first)
            {
                RaiseRunningInstance();
                return 0;
            }
            Application.EnableVisualStyles();
            Machine machine;
            try
            {
                machine = Machine.Prepare();
            }
            catch (LauncherError e)
            {
                Fail(e.Message);
                return 1;
            }
            if (machine == null)
                return 0;        // the disk picker was cancelled
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.ThrowException);
            AppDomain.CurrentDomain.UnhandledException += (sender, e) => Crashed(machine, e.ExceptionObject);
            Application.Run(new Window(machine));
            return 0;
        }
    }

    // The second launch hands the window back rather than booting the disk twice, which the
    // qcow2 lock would refuse.
    static void RaiseRunningInstance()
    {
        var self = Process.GetCurrentProcess();
        foreach (var p in Process.GetProcessesByName(self.ProcessName))
        {
            if (p.Id != self.Id && p.MainWindowHandle != IntPtr.Zero)
            {
                Native.ShowWindow(p.MainWindowHandle, Native.SW_RESTORE);
                Native.SetForegroundWindow(p.MainWindowHandle);
            }
        }
    }

    // Whatever killed the launcher goes in the log, and the guest is asked to shut down: a QEMU
    // outliving its window keeps the disk locked and the ssh port taken, so the next launch
    // could not start.
    static void Crashed(Machine machine, object error)
    {
        string what = "launcher: crashed: " + error + Environment.NewLine;
        try
        {
            Qmp.Execute(machine.ControlPort, "system_powerdown", null);
            what += "launcher: asked the guest to shut down" + Environment.NewLine;
        }
        catch (Exception e)
        {
            what += "launcher: could not ask the guest to shut down, so QEMU may still be running: " +
                    e.Message + Environment.NewLine;
        }
        File.AppendAllText(machine.Log, what);
        Fail("RaiGolmi's window crashed. What happened is in " + machine.Log + ":\n\n" + error);
    }

    public static void Fail(string message)
    {
        MessageBox.Show(message, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    public static string QemuPath(string file)
    {
        return Path.Combine(QemuDir, file);
    }
}

class LauncherError : Exception
{
    public LauncherError(string message) : base(message) { }
}

// Everything this machine's QEMU needs: its state under %LOCALAPPDATA%\RaiGolmi, its log
// and screen beside the exe.
class Machine
{
    public string Qemu, Firmware, Vars, Disk, Log, Screen, Placement;
    // Two control sockets, because QEMU serves one client per socket: the close has one no
    // probe can hold, and everything that only looks shares the other.
    public int ControlPort, WatchPort;
    // QEMU's arguments, made once here so a path it cannot be given is said before any window.
    public string CommandLine;
    // The guest's sshd, forwarded on the loopback only: a login there is root in the guest.
    // 2222 when it is free, so what already knows it keeps working; any free port otherwise.
    public int SshPort;
    const int UsualSshPort = 2222;
    // The transfer bridge's tools and key, or why there are none.
    public Ssh Ssh;
    public string NoSsh;

    public static Machine Prepare()
    {
        string home = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "RaiGolmi");
        Directory.CreateDirectory(home);
        var m = new Machine();
        m.Qemu = Require(Launcher.QemuPath(@"bin\qemu-system-x86_64w.exe"));
        m.Firmware = Require(Launcher.QemuPath(@"share\qemu\edk2-x86_64-code.fd"));
        m.Log = Path.Combine(Application.StartupPath, "qemu.log");
        m.Screen = Path.Combine(Application.StartupPath, "screen.ppm");
        // The UEFI variable store is written by the firmware, so it is this machine's copy.
        m.Vars = Path.Combine(home, "uefi-vars.fd");
        // Where the window was when it last closed, and its size: "x y width height".
        m.Placement = Path.Combine(home, "window.txt");
        if (!File.Exists(m.Vars))
            File.Copy(Require(Launcher.QemuPath(@"share\qemu\edk2-i386-vars.fd")), m.Vars);
        m.SshPort = PortOr(UsualSshPort);
        try
        {
            m.Ssh = Ssh.Prepare(home, m.SshPort);
        }
        catch (LauncherError e)
        {
            m.NoSsh = e.Message;     // the window works without the bridge, and says so
        }
        m.Disk = ChooseDisk(Path.Combine(home, "disk.txt"));
        if (m.Disk == null)
            return null;
        m.ControlPort = FreePort();
        m.WatchPort = FreePort();
        m.CommandLine = m.Arguments();
        return m;
    }

    static string Require(string path)
    {
        if (!File.Exists(path))
            throw new LauncherError(path + " does not exist. RaiGolmi runs in MSYS2's QEMU: install " +
                                    "MSYS2 from https://www.msys2.org, then in its UCRT64 window run " +
                                    "pacman -S --noconfirm mingw-w64-ucrt-x86_64-qemu");
        return path;
    }

    // The disk is gigabytes, so it is used where the build left it and only its path is kept.
    static string ChooseDisk(string record)
    {
        if (File.Exists(record))
        {
            string kept = File.ReadAllText(record).Trim();
            if (File.Exists(kept))
                return kept;
            Launcher.Fail("The RaiGolmi disk was at " + kept + " and is not there any more. " +
                          "Choose where it is now.");
        }
        using (var pick = new OpenFileDialog())
        {
            pick.Title = "Choose the RaiGolmi disk";
            pick.Filter = "RaiGolmi disk (*.qcow2)|*.qcow2";
            if (pick.ShowDialog() != DialogResult.OK)
                return null;
            File.WriteAllText(record, pick.FileName);
            return pick.FileName;
        }
    }

    // `preferred` if this program can bind it as QEMU's forward will, else a free one.
    static int PortOr(int preferred)
    {
        var l = new TcpListener(IPAddress.Loopback, preferred);
        try
        {
            l.Start();
        }
        catch (SocketException)
        {
            return FreePort();
        }
        l.Stop();
        return preferred;
    }

    static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        int port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    // The device list is load-bearing (no usb-tablet, no absolute pointer). The GPU is virgl
    // and QEMU has no window: its display goes to this program alone, over a socket handed to
    // it (Display.cs). A vCPU is a QEMU thread Windows schedules like any other, not a reserved
    // core, so the guest gets every logical processor and an idle one costs nothing. The
    // hypervisor emulates each vCPU's local APIC (kernel-irqchip=on, required, so a host that
    // cannot fails QEMU's start): off, every IPI, timer and EOI exits to QEMU under its one lock.
    string Arguments()
    {
        var a = new List<string>();
        a.Add("-name RaiGolmi");
        a.Add("-machine type=q35,accel=whpx,kernel-irqchip=on -cpu max -m 8192 -smp " + Environment.ProcessorCount);
        a.Add("-drive " + Quote("if=pflash,format=raw,readonly=on,file=" + Opt(Native.Ascii(Firmware))));
        a.Add("-drive " + Quote("if=pflash,format=raw,file=" + Opt(Native.Ascii(Vars))));
        // What the guest discards (its trim every ten minutes and at shutdown) the patched QEMU
        // releases from the sparse file, so the file shrinks with the guest's use while it runs.
        a.Add("-drive " + Quote("file=" + Opt(Native.Ascii(Disk)) + ",format=qcow2,discard=unmap,detect-zeroes=unmap"));
        a.Add("-device virtio-vga-gl -device qemu-xhci,id=xhci -device usb-tablet,bus=xhci.0");
        a.Add("-netdev user,id=net0,hostfwd=tcp:127.0.0.1:" + SshPort + "-:22 -device virtio-net,netdev=net0");
        // Boot credentials (systemd reads SMBIOS type 11): the login keys, and Windows' own
        // timezone, which the guest makes its own (host/systemd/set-timezone).
        var credentials = new List<string>();
        if (Ssh != null)
            credentials.Add("path=" + Opt(Native.Ascii(Ssh.Credential)));
        credentials.Add("value=" + Opt("io.systemd.credential:raigolmi.timezone=" +
                                       Native.IanaZone(TimeZoneInfo.Local.Id)));
        a.Add("-smbios " + Quote("type=11," + string.Join(",", credentials)));
        a.Add("-display dbus,p2p=yes,gl=on");
        a.Add("-qmp tcp:127.0.0.1:" + ControlPort + ",server=on,wait=off");
        a.Add("-qmp tcp:127.0.0.1:" + WatchPort + ",server=on,wait=off");
        return string.Join(" ", a);
    }

    // A comma inside a QEMU option value is written twice.
    static string Opt(string value) { return value.Replace(",", ",,"); }
    static string Quote(string arg) { return "\"" + arg + "\""; }
}

// The one window: the guest's screen drawn 1:1 in the client area, centred when the window
// is larger. Its size is offered to the guest as its preferred mode.
class Window : Form
{
    readonly Machine machine;
    Process qemu;
    IntPtr job;
    Renderer renderer;
    volatile Display display;
    bool shutdownSent, exited;
    readonly StringBuilder stderr = new StringBuilder();
    System.Threading.Timer watcher;
    string lastStatus;
    readonly object qmpLock = new object();
    readonly System.Windows.Forms.Timer sizeSettle = new System.Windows.Forms.Timer();
    readonly Keyboard keyboard;
    readonly Bridge bridge;

    public Window(Machine machine)
    {
        this.machine = machine;
        Text = "RaiGolmi";
        BackColor = Color.Black;
        // The swap chain owns every pixel of the client area; nothing is painted over it.
        SetStyle(ControlStyles.Opaque | ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint, true);
        StartPosition = FormStartPosition.CenterScreen;
        Rectangle area = Screen.PrimaryScreen.WorkingArea;
        ClientSize = new Size(area.Width * 3 / 4, area.Height * 3 / 4);
        // Opened where and as large as it last closed, so the guest boots at the size it will
        // be shown at: its console keeps its boot size, and QEMU aborts when the scanout
        // changes size between the console and the desktop (ui/egl-helpers.c egl_fb_read_rect).
        Rectangle? last = LastPlacement(machine.Placement);
        if (last.HasValue)
        {
            StartPosition = FormStartPosition.Manual;
            Bounds = last.Value;
        }
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        // A drag resizes the window many times a second; the guest is asked once it stops.
        sizeSettle.Interval = 300;
        sizeSettle.Tick += delegate { sizeSettle.Stop(); OfferSize(); };
        keyboard = new Keyboard(this);
        if (machine.Ssh != null)
        {
            bridge = new Bridge(this, machine.Ssh);
            AllowDrop = true;
        }
    }

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);
        var info = new ProcessStartInfo(machine.Qemu, machine.CommandLine);
        info.UseShellExecute = false;
        info.RedirectStandardError = true;
        info.RedirectStandardOutput = true;
        info.WorkingDirectory = Path.GetDirectoryName(machine.Qemu);
        // QEMU's DLLs (ANGLE, virglrenderer, GLib) are MSYS2's, beside it.
        info.Environment["PATH"] = info.WorkingDirectory + ";" + info.Environment["PATH"];
        qemu = new Process();
        qemu.StartInfo = info;
        qemu.EnableRaisingEvents = true;
        qemu.ErrorDataReceived += Record;
        qemu.OutputDataReceived += Record;
        qemu.Exited += delegate { BeginInvoke(new Action(OnQemuExited)); };
        // The last run's log is kept beside this one's, for the run that ended badly.
        if (File.Exists(machine.Log))
            File.Copy(machine.Log, Path.ChangeExtension(machine.Log, ".previous.log"), true);
        File.WriteAllText(machine.Log, "\"" + machine.Qemu + "\" " + machine.CommandLine +
                                       Environment.NewLine);
        qemu.Start();
        // QEMU dies with this program: one outliving its window keeps the disk locked and the
        // ssh port taken, so no later launch could start, and nothing could reach it but ssh.
        job = Native.KillOnClose(qemu);
        qemu.BeginErrorReadLine();
        qemu.BeginOutputReadLine();
        renderer = new Renderer(this, Note);
        new Thread(Connect) { IsBackground = true, Name = "display" }.Start();
        watcher = new System.Threading.Timer(delegate { Watch(); }, null, 5000, 10000);
        if (bridge == null)
        {
            Note(machine.NoSsh);
            MessageBox.Show(this, machine.NoSsh, "RaiGolmi", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        bridge.Start();
    }

    // --- the transfer bridge's Windows side ----------------------------------------------

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        if (bridge != null && !Native.AddClipboardFormatListener(Handle))
            Warn("Windows' clipboard cannot be followed, so copies made in Windows will not reach "
                 + "the OS: error " + Marshal.GetLastWin32Error());
    }

    // Windows' clipboard reaches the guest when the window gains focus, and only its latest
    // text: a change while the user works in the guest, such as a clipboard
    // manager's write, would otherwise overwrite what they just copied there. True at start, so
    // the first focus sends what Windows holds. The bridge's own writes come back as changes
    // and `WindowsCopied` drops them.
    bool windowsChanged = true;

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == Native.WM_CLIPBOARDUPDATE)
            windowsChanged = true;
        base.WndProc(ref m);
    }

    protected override void OnActivated(EventArgs e)
    {
        base.OnActivated(e);
        if (bridge == null || !windowsChanged)
            return;
        windowsChanged = false;
        string text = ClipboardText();
        if (text != null)
            bridge.WindowsCopied(text);
    }

    // Text only; null when the clipboard holds none.
    //
    // ⚠ **Retried, for the same reason `SetDataObject` below is given retries.** Windows opens
    // the clipboard to one program at a time, and `WM_CLIPBOARDUPDATE` arrives while the
    // program that did the copying may still have it open — a read at that instant throws.
    // Answering that with null says *there is no text*, which is what an image on the
    // clipboard says too, so the copy is dropped and nothing ever asks again: pasting is not
    // what re-reads Windows' clipboard, a copy and the next focus are. One refused read is therefore a machine
    // that cannot be pasted into until the user copies something a second time, with every
    // other part of the bridge working perfectly.
    //
    // On the message loop's own thread, because that is where the clipboard may be touched,
    // and bounded so that the window cannot stop answering for longer than a copy is worth.
    // A refusal that outlasts the budget is said, never reported as an empty clipboard.
    const int ClipboardTries = 5;
    const int ClipboardPause = 40;

    string ClipboardText()
    {
        ExternalException refused = null;
        for (int attempt = 0; attempt < ClipboardTries; attempt++)
        {
            try
            {
                return Clipboard.ContainsText() ? Clipboard.GetText() : null;
            }
            catch (ExternalException e)
            {
                refused = e;
                Thread.Sleep(ClipboardPause);
            }
        }
        Warn("Windows' clipboard could not be read after " + ClipboardTries + " tries, so what "
             + "was copied has not reached the OS; copy it again: " + refused.Message);
        return null;
    }

    public void SetClipboard(string text)
    {
        try
        {
            Clipboard.SetDataObject(text, true, 10, 100);
        }
        catch (ExternalException e)
        {
            Warn("Windows' clipboard could not be written, so what was copied in the OS has not "
                 + "reached Windows; copy it again: " + e.Message);
        }
    }

    protected override void OnDragEnter(DragEventArgs e)
    {
        base.OnDragEnter(e);
        e.Effect = e.Data.GetDataPresent(DataFormats.FileDrop) ? DragDropEffects.Copy : DragDropEffects.None;
    }

    protected override void OnDragDrop(DragEventArgs e)
    {
        base.OnDragDrop(e);
        var paths = e.Data.GetData(DataFormats.FileDrop) as string[];
        if (paths != null)
            bridge.Dropped(paths);
    }

    // QEMU opens its control socket moments after it starts; until then the connection is
    // refused. A refusal after QEMU has exited is its exit, which is reported there. The
    // display is connected once, over the close's socket, which it holds only for these calls.
    void Connect()
    {
        var deadline = DateTime.UtcNow.AddSeconds(30);
        Qmp qmp;
        while (true)
        {
            try
            {
                qmp = Qmp.Open(machine.ControlPort);
                break;
            }
            // Refused before QEMU listens; reset when it accepts and then exits.
            catch (Exception e) when (e is SocketException || e is IOException)
            {
                if (qemu.HasExited)
                    return;
                if (DateTime.UtcNow > deadline)
                {
                    Note("could not reach QEMU's control socket on port " + machine.ControlPort + ": " + e.Message);
                    return;
                }
                Thread.Sleep(200);
            }
        }
        try
        {
            using (qmp)
                display = Display.Connect(qmp, qemu.Id, renderer, Note, ScreenLost);
        }
        catch (Exception e)
        {
            if (qemu.HasExited)
                return;
            Note("the screen could not be connected: " + e);
            BeginInvoke(new Action(() => Launcher.Fail("RaiGolmi's screen could not be connected, so " +
                "the window stays black. What went wrong is in " + machine.Log + ":\n\n" + e.Message)));
            return;
        }
        BeginInvoke(new Action(() =>
        {
            Note("connected to the screen");
            OfferSize();
        }));
    }

    // A connection to the display ending while QEMU runs leaves the window frozen on its last
    // frame; that is said, not left to look like a quiet guest. Its reason is already logged.
    void ScreenLost(string which)
    {
        if (qemu.HasExited)
            return;
        BeginInvoke(new Action(() => Launcher.Fail("RaiGolmi's screen stopped: " + which + " to QEMU ended. " +
            "Why is in " + machine.Log + ". Close the window to shut the machine down.")));
    }

    void OfferSize()
    {
        Display d = display;
        if (d == null || WindowState == FormWindowState.Minimized)
            return;
        Size want = ClientSize;
        d.OfferSize(want.Width, want.Height);
        Note("offered the guest " + want.Width + "x" + want.Height);
    }

    protected override void OnResize(EventArgs e)
    {
        base.OnResize(e);
        if (renderer != null && WindowState != FormWindowState.Minimized)
            renderer.Resize(ClientSize.Width, ClientSize.Height);
        sizeSettle.Stop();
        sizeSettle.Start();
    }

    // Where the guest's screen sits in the client area: its top-left corner, as Renderer
    // places it.
    Point Origin()
    {
        return new Point(Math.Max(0, (ClientSize.Width - renderer.FrameWidth) / 2),
                         Math.Max(0, (ClientSize.Height - renderer.FrameHeight) / 2));
    }

    protected override void OnPaintBackground(PaintEventArgs e) { }

    protected override void OnPaint(PaintEventArgs e)
    {
        if (renderer != null)
            renderer.Redraw();
    }

    // --- the pointer: absolute, in the guest's pixels. Its shape is the guest's, drawn by
    // Windows as this window's cursor (Renderer.CursorDefine).

    // False when the point is outside the guest's screen, where nothing is sent.
    bool Guest(Point client, out int x, out int y)
    {
        Point o = Origin();
        x = client.X - o.X;
        y = client.Y - o.Y;
        return display != null && x >= 0 && y >= 0 && x < renderer.FrameWidth && y < renderer.FrameHeight;
    }

    // The guest's buttons this window pressed and has not yet released. A press starts only
    // over the guest's screen, but its release is sent wherever it lands: outside the window
    // (the press captured the mouse, so it still arrives here), in the margin around a smaller
    // frame, or never, when focus or capture is lost mid-press. QEMU holds a button it saw
    // pressed until it sees it released, so a dropped release is a button stuck down.
    readonly HashSet<uint> held = new HashSet<uint>();

    // Null for a button the guest has none of. QEMU numbers the left button 0, so 0 is a
    // button, never "none".
    static uint? GuestButton(MouseButtons button)
    {
        if (button == MouseButtons.Left) return Display.ButtonLeft;
        if (button == MouseButtons.Middle) return Display.ButtonMiddle;
        if (button == MouseButtons.Right) return Display.ButtonRight;
        return null;
    }

    void SendButton(MouseEventArgs e, bool down)
    {
        uint? guest = GuestButton(e.Button);
        int x, y;
        bool over = Guest(e.Location, out x, out y);
        // A button not sent is said: a click the guest never saw is otherwise identical, from
        // inside it, to one Windows never delivered here.
        string what = e.Button + (down ? " down" : " up") + " at " + e.Location.X + "," + e.Location.Y;
        if (guest == null || display == null || (down ? !over : !held.Contains(guest.Value)))
        {
            Note("pointer: " + what + " not sent: " + (guest == null ? "not a guest button"
                 : display == null ? "no screen" : down ? "outside the guest's screen" : "not held"));
            return;
        }
        uint button = guest.Value;
        if (over)
            display.Pointer(x, y);
        display.Button(down, button);
        if (down) held.Add(button); else held.Remove(button);
    }

    // Every held button, or only those Windows says are up: WinForms gives up capture on any
    // button's release, so a chord's other button is still down when capture changes.
    void ReleaseButtons(bool onlyUp)
    {
        MouseButtons down = onlyUp ? Control.MouseButtons : MouseButtons.None;
        foreach (MouseButtons b in new[] { MouseButtons.Left, MouseButtons.Middle, MouseButtons.Right })
        {
            uint button = GuestButton(b)!.Value;
            if (!held.Contains(button) || (down & b) != 0)
                continue;
            if (display != null)
                display.Button(false, button);
            held.Remove(button);
        }
    }

    // One notch of the guest's wheel per event, whatever the delta's size.
    void SendWheel(MouseEventArgs e)
    {
        int x, y;
        if (e.Delta == 0 || !Guest(e.Location, out x, out y))
            return;
        uint notch = e.Delta > 0 ? Display.WheelUp : Display.WheelDown;
        display.Pointer(x, y);
        display.Button(true, notch);
        display.Button(false, notch);
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        base.OnMouseMove(e);
        keyboard.Hover(true);
        int x, y;
        if (Guest(e.Location, out x, out y))
            display.Pointer(x, y);
    }
    protected override void OnMouseDown(MouseEventArgs e) { base.OnMouseDown(e); SendButton(e, true); }
    protected override void OnMouseUp(MouseEventArgs e) { base.OnMouseUp(e); SendButton(e, false); }
    protected override void OnMouseWheel(MouseEventArgs e) { base.OnMouseWheel(e); SendWheel(e); }
    protected override void OnMouseLeave(EventArgs e) { base.OnMouseLeave(e); keyboard.Hover(false); }
    protected override void OnDeactivate(EventArgs e) { base.OnDeactivate(e); keyboard.Release(); ReleaseButtons(false); }
    protected override void OnMouseCaptureChanged(EventArgs e) { base.OnMouseCaptureChanged(e); if (!Capture) ReleaseButtons(true); }

    public bool Active { get { return Form.ActiveForm == this; } }

    public void SendKey(bool down, int qnum)
    {
        Display d = display;
        if (d != null)
            d.Key(down, qnum);
    }

    // Ctrl+Alt+R: throw away what is on screen and draw the guest's last frame again.
    //
    // ⚠ It also tells the two halves apart, which is why it is worth having beyond the
    // repair. Litter on the display is either pixels this program drew wrong or pixels the
    // guest scanned out: this redraws the whole of the last frame QEMU handed over, so if the
    // litter goes the drawing had failed, and if it stays it is in the guest's own scanout.
    public void RedrawWholeScreen()
    {
        renderer.Redraw();
        Note("redrawing the whole screen");
    }

    // --- what the guest is doing, for a reader who cannot see the window -------------

    // Two questions apart, so one failing never hides the other's answer. The screen is the
    // frame this window draws: QEMU's screendump has no surface to read under the D-Bus display.
    void Watch()
    {
        try
        {
            string status;
            lock (qmpLock)
                status = Qmp.Execute(machine.WatchPort, "query-status", null);
            if (status != lastStatus)
                Note("status " + status);
            lastStatus = status;
        }
        catch (Exception e)
        {
            Note("could not ask QEMU how the guest is: " + e.Message);
        }
        try
        {
            SaveScreen();
        }
        catch (Exception e)
        {
            Note("could not write " + machine.Screen + ": " + e.Message);
        }
    }

    // A PPM, written whole and then renamed so a reader never meets half of one; removed while
    // the guest shows nothing, so an old picture is never read as the current one.
    void SaveScreen()
    {
        var frame = renderer.ReadFrame();
        if (frame == null)
        {
            File.Delete(machine.Screen);
            return;
        }
        var (w, h, rgb) = frame.Value;
        string partial = machine.Screen + ".partial";
        using (var file = File.Create(partial))
        {
            var header = System.Text.Encoding.ASCII.GetBytes($"P6\n{w} {h}\n255\n");
            file.Write(header, 0, header.Length);
            file.Write(rgb, 0, rgb.Length);
        }
        File.Move(partial, machine.Screen, true);
    }

    // A failure the user has to know about: in the log, and in front of them.
    public void Warn(string message)
    {
        Note(message);
        BeginInvoke(new Action(() =>
            MessageBox.Show(this, message, "RaiGolmi", MessageBoxButtons.OK, MessageBoxIcon.Warning)));
    }

    public void Note(string line)
    {
        Record(this, "launcher: " + line);
    }

    void Record(object sender, DataReceivedEventArgs e)
    {
        if (e.Data != null)
            Record(sender, e.Data);
    }

    void Record(object sender, string line)
    {
        lock (stderr)
        {
            stderr.AppendLine(line);
            File.AppendAllText(machine.Log, line + Environment.NewLine);
        }
    }

    // --- closing is a clean shutdown ---------------------------------------------------

    // The window's last placement, or null when there is none or it is on no screen now (a
    // monitor since unplugged). The file is this program's own; one it cannot read is a defect.
    static Rectangle? LastPlacement(string path)
    {
        if (!File.Exists(path))
            return null;
        string[] parts = File.ReadAllText(path).Trim().Split(' ');
        int x, y, w, h;
        if (parts.Length != 4 || !int.TryParse(parts[0], out x) || !int.TryParse(parts[1], out y) ||
            !int.TryParse(parts[2], out w) || !int.TryParse(parts[3], out h) || w <= 0 || h <= 0)
            throw new LauncherError(path + " is not a window placement (\"x y width height\"). " +
                                    "Delete it and RaiGolmi opens at its default size.");
        var bounds = new Rectangle(x, y, w, h);
        foreach (Screen screen in Screen.AllScreens)
            if (screen.WorkingArea.IntersectsWith(bounds))
                return bounds;
        return null;
    }

    void SavePlacement()
    {
        Rectangle b = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
        File.WriteAllText(machine.Placement, b.X + " " + b.Y + " " + b.Width + " " + b.Height);
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        Note("close requested (" + e.CloseReason + ")");
        SavePlacement();
        if (exited)
        {
            base.OnFormClosing(e);
            return;
        }
        e.Cancel = true;
        if (!shutdownSent)
        {
            try
            {
                Display d = display;
                if (d != null)
                    d.StopListening();
                Qmp.Execute(machine.ControlPort, "system_powerdown", null);
                shutdownSent = true;
                Note("asked the guest to shut down");
                ShowShuttingDown();
            }
            catch (Exception ex)
            {
                Note("the shutdown request failed: " + ex.Message);
                OfferPowerOff("RaiGolmi could not be asked to shut down:\n\n" + ex.Message);
            }
            return;
        }
        // A second close while the guest is still shutting down is the user deciding.
        OfferPowerOff("RaiGolmi was asked to shut down and has not finished.");
    }

    // From the request until QEMU exits: a guest that is shutting down stops drawing, and a
    // window left on its last frame reads as hung. Its own top-level window, owned by this one,
    // because nothing else is drawn over the swap chain.
    void ShowShuttingDown()
    {
        Text = "RaiGolmi \u2014 shutting down";
        var column = new FlowLayoutPanel
        {
            FlowDirection = FlowDirection.TopDown, WrapContents = false, AutoSize = true,
            Padding = new Padding(24),
        };
        column.Controls.Add(new Label
        {
            Text = "RaiGolmi is shutting down\u2026", AutoSize = true,
            Margin = new Padding(0, 0, 0, 12),
        });
        column.Controls.Add(new ProgressBar { Style = ProgressBarStyle.Marquee, Width = 280, Height = 14 });
        var dialog = new Form
        {
            Text = "RaiGolmi", FormBorderStyle = FormBorderStyle.FixedDialog, MaximizeBox = false,
            MinimizeBox = false, ShowInTaskbar = false, StartPosition = FormStartPosition.Manual,
            AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink,
        };
        dialog.Controls.Add(column);
        dialog.Shown += delegate
        {
            dialog.Location = new Point(Left + (Width - dialog.Width) / 2, Top + (Height - dialog.Height) / 2);
        };
        // Closing it is a second close of the machine.
        dialog.FormClosing += (_, e) =>
        {
            if (exited)
                return;
            e.Cancel = true;
            OfferPowerOff("RaiGolmi was asked to shut down and has not finished.");
        };
        dialog.Show(this);
    }

    void OfferPowerOff(string why)
    {
        var answer = MessageBox.Show(
            why + "\n\nPower it off now? Anything unsaved in it is lost.", "RaiGolmi",
            MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2);
        if (answer == DialogResult.Yes)
        {
            Note("powered off by the user");
            qemu.Kill();
        }
    }

    void OnQemuExited()
    {
        exited = true;
        if (watcher != null)
            watcher.Dispose();
        if (bridge != null)
            bridge.Stop();
        keyboard.Dispose();
        qemu.WaitForExit();      // drains the redirected streams
        if (qemu.ExitCode != 0)
        {
            string log;
            lock (stderr) log = stderr.ToString();
            string tail = log.Length > 3000 ? log.Substring(log.Length - 3000) : log;
            string said = Explain(log);
            Launcher.Fail((said != null ? said + "\n\n" : "") +
                          "QEMU exited with code " + qemu.ExitCode + ". Its output is in " +
                          machine.Log + ":\n\n" + tail);
        }
        Close();
    }

    // The failures a stranger's PC meets, by what QEMU says of each
    // (target/i386/whpx/whpx-all.c, net/slirp.c, ui/egl-helpers.c), in one sentence each.
    static string Explain(string log)
    {
        if (log.Contains("WHPX: No accelerator found"))
            return "Windows' hypervisor is not running, so the machine cannot start. Windows " +
                   "Hypervisor Platform may be off, or virtualization turned off in the PC's " +
                   "BIOS/UEFI: run setup.bat, which checks both and says which.";
        if (log.Contains("WHPX: kernel irqchip requested, but unavailable"))
            return "This Windows' hypervisor does not emulate the interrupt controller the " +
                   "machine needs. Updating Windows is the first thing to try.";
        if (log.Contains("Could not set up host forwarding rule"))
            return "Another program took the port RaiGolmi chose for the machine's ssh just as " +
                   "it started. Start RaiGolmi again, and it picks a free one.";
        if (log.Contains("egl:"))
            return "QEMU could not start its graphics on this PC's GPU. Updating the graphics " +
                   "driver is the first thing to try.";
        return null;
    }
}

// The keyboard is the guest's while this window is active and the pointer is over it, and
// Windows' otherwise — Super, Alt+Tab and all. A low-level hook
// is the only way to take Super from Windows, as QEMU's own ui/win32-kbd-hook.c does. Keys
// travel as scancodes, so the guest's layout decides what they mean.
//
// **Ctrl+Alt+R is the exception and the only one**: it redraws the whole screen and is not
// passed on. Everything else about this program is outside the OS and unknown to it, and a
// repaint is the one thing that cannot be asked for from inside — the guest
// does not know it is being watched over a wire, so it cannot know the wire dropped anything.
class Keyboard : IDisposable
{
    readonly Window window;
    readonly Native.LowLevelKeyboardProc proc;   // held so the collector cannot free it
    readonly IntPtr hook;
    readonly HashSet<int> down = new HashSet<int>();
    bool hovering;
    // Set 1 scancodes. The extended (right-hand) twin of each is the same code with 0x80 on,
    // which is how `qnum` is built below, so `Held` accepts either hand.
    const int CtrlScan = 0x1D, AltScan = 0x38, RedrawScan = 0x13;

    bool Held(int scan)
    {
        return down.Contains(scan) || down.Contains(scan | 0x80);
    }

    public Keyboard(Window window)
    {
        this.window = window;
        proc = OnKey;
        hook = Native.SetWindowsHookEx(Native.WH_KEYBOARD_LL, proc,
                                       Native.GetModuleHandle(null), 0);
        if (hook == IntPtr.Zero)
            throw new LauncherError("The keyboard hook could not be installed (error " +
                                    Marshal.GetLastWin32Error() + "), so keys cannot reach the OS.");
    }

    public void Hover(bool over)
    {
        if (hovering && !over)
            Release();
        hovering = over;
    }

    // Keys the guest saw pressed are released there when the keyboard goes back to Windows,
    // or they stay held in the guest.
    public void Release()
    {
        foreach (int qnum in down)
            window.SendKey(false, qnum);
        down.Clear();
    }

    IntPtr OnKey(int code, IntPtr wparam, IntPtr lparam)
    {
        if (code == Native.HC_ACTION && hovering && window.Active)
        {
            var k = (Native.KBDLLHOOKSTRUCT)Marshal.PtrToStructure(lparam, typeof(Native.KBDLLHOOKSTRUCT));
            // AltGr sends a phantom left Ctrl with this scancode bit set; the guest's AltGr
            // is the right Alt that follows it.
            if ((k.scanCode & 0x200) != 0)
                return new IntPtr(1);
            int qnum = (int)(k.scanCode & 0x7F) | ((k.flags & Native.LLKHF_EXTENDED) != 0 ? 0x80 : 0);
            bool press = wparam == (IntPtr)Native.WM_KEYDOWN || wparam == (IntPtr)Native.WM_SYSKEYDOWN;
            // ⚠ The one combination this program keeps for itself, and it is never forwarded:
            // the guest cannot redraw a window it does not know it is in, so a key it could
            // not act on would only be a key taken away from whatever has focus there. Ctrl
            // and Alt still reach the guest — only the R is swallowed — so nothing is left
            // held down on the other side.
            if (qnum == RedrawScan && Held(CtrlScan) && Held(AltScan))
            {
                if (press)
                    window.BeginInvoke(new Action(window.RedrawWholeScreen));
                return new IntPtr(1);
            }
            if (press)
                down.Add(qnum);
            else
                down.Remove(qnum);
            window.SendKey(press, qnum);
            return new IntPtr(1);
        }
        return Native.CallNextHookEx(hook, code, wparam, lparam);
    }

    public void Dispose()
    {
        Native.UnhookWindowsHookEx(hook);
    }
}

// Windows' own OpenSSH client and tar, which the transfer bridge drives; the launcher carries
// no SSH of its own. The client is an optional Windows feature, so its absence is said, not
// assumed away. Everything it needs lives in its own ssh_config, so the user's is never read.
class Ssh
{
    const string Host = "raigolmi";
    public string Key, Config, Credential;
    // Windows' Downloads\RaiGolmi, where ~/Transfer/out empties to.
    public string Outbox;
    string client, tar;

    public static Ssh Prepare(string home, int port)
    {
        string system = Environment.GetFolderPath(Environment.SpecialFolder.System);
        var s = new Ssh();
        s.client = Path.Combine(system, @"OpenSSH\ssh.exe");
        string keygen = Path.Combine(system, @"OpenSSH\ssh-keygen.exe");
        s.tar = Path.Combine(system, "tar.exe");
        if (!File.Exists(s.client) || !File.Exists(keygen))
            throw new LauncherError(
                "Windows' OpenSSH client is not installed (" + s.client + " is missing), so the " +
                "clipboard and files cannot cross between Windows and RaiGolmi. It is an optional " +
                "Windows feature: Settings > System > Optional features > OpenSSH Client, or " +
                "Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0 as administrator.");
        if (!File.Exists(s.tar))
            throw new LauncherError(s.tar + " is missing, so files cannot cross between Windows " +
                                    "and RaiGolmi.");
        s.Key = Path.Combine(home, "id_ed25519");
        s.Config = Path.Combine(home, "ssh_config");
        s.Credential = Path.Combine(home, "login.credential");
        s.Outbox = Path.Combine(Native.KnownFolder(Native.Downloads), "RaiGolmi");
        if (!File.Exists(s.Key))
            Generate(keygen, s.Key);
        string pub = s.Key + ".pub";
        if (!File.Exists(pub))
            throw new LauncherError(s.Key + " has no public half beside it. Delete it and start " +
                                    "RaiGolmi again, and a new pair is made.");
        // The guest installs this for the desktop user at boot (host/tmpfiles/raigolmi.conf):
        // the launcher's key, and any in authorized_keys beside the program, which is how
        // another machine driving the VM gets in.
        var keys = new StringBuilder(File.ReadAllText(pub).Trim() + "\n");
        string others = Path.Combine(Application.StartupPath, "authorized_keys");
        if (File.Exists(others))
            foreach (string line in File.ReadAllLines(others))
                if (line.Trim().Length > 0)
                    keys.Append(line.Trim() + "\n");
        File.WriteAllText(s.Credential, "io.systemd.credential:ssh.authorized_keys.agent=" + keys,
                          Encoding.ASCII);
        // Trusted on first use for one run: every disk has its own host key, and the forward
        // is on the loopback only.
        string known = Path.Combine(home, "known_hosts");
        File.Delete(known);
        File.WriteAllText(s.Config, string.Join("\n", new[] {
            "Host " + Host,
            "    HostName 127.0.0.1",
            "    Port " + port,
            "    User agent",
            "    IdentityFile " + ConfigPath(s.Key),
            "    IdentitiesOnly yes",
            "    BatchMode yes",
            "    StrictHostKeyChecking accept-new",
            "    UserKnownHostsFile " + ConfigPath(known),
            "    ConnectTimeout 10",
            "    ServerAliveInterval 15",
            "    ServerAliveCountMax 3",
            "    LogLevel ERROR",
            "" }), new UTF8Encoding(false));
        return s;
    }

    // ssh_config reads a backslash as an escape and expands % tokens in these paths.
    static string ConfigPath(string path)
    {
        return "\"" + path.Replace('\\', '/').Replace("%", "%%") + "\"";
    }

    static void Generate(string keygen, string key)
    {
        var p = Process.Start(Info(keygen, "-q -t ed25519 -N \"\" -C RaiGolmi-launcher -f " +
                                           Quote(key)));
        p.StandardInput.Close();
        var r = Finish(p);
        if (r.Code != 0)
            throw new LauncherError("ssh-keygen could not make the launcher's key (code " + r.Code +
                                    "):\n\n" + r.Err + r.Out);
    }

    // A bash script run in the guest as the desktop user. The script and its arguments travel
    // as base64, the one form Windows' argument quoting, ssh's joining of the command and the
    // guest's shell all leave alone; the script sees its arguments decoded as $1, $2...
    const string Decode = "decoded=()\nfor a; do decoded+=(\"$(printf %s \"$a\" | base64 -d)\"); done\n" +
                          "set -- \"${decoded[@]}\"\n";
    public Process Start(string script, params string[] args)
    {
        var remote = new StringBuilder("bash <(echo " +
            Base64(Decode + script.Replace("\r\n", "\n")) + "|base64 -d)");
        foreach (string a in args)
            remote.Append(" " + Base64(a));
        return Process.Start(Info(client, "-F " + Quote(Config) + " " + Host + " " +
                                          Quote(remote.ToString())));
    }

    public Result Run(string script, params string[] args)
    {
        var p = Start(script, args);
        p.StandardInput.Close();
        return Finish(p);
    }

    public Process Tar(string arguments)
    {
        return Process.Start(Info(tar, arguments));
    }

    static ProcessStartInfo Info(string exe, string arguments)
    {
        var info = new ProcessStartInfo(exe, arguments);
        info.UseShellExecute = false;
        info.CreateNoWindow = true;      // console programs; this one has no console to share
        info.RedirectStandardInput = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        info.StandardErrorEncoding = Encoding.UTF8;
        info.StandardOutputEncoding = Encoding.UTF8;
        return info;
    }

    public class Result { public int Code; public string Out, Err; }

    // Its output, with stderr read alongside so that neither pipe can fill and stall it.
    public static Result Finish(Process p)
    {
        var err = new StringBuilder();
        p.ErrorDataReceived += delegate(object o, DataReceivedEventArgs e)
        {
            if (e.Data != null) lock (err) err.AppendLine(e.Data);
        };
        p.BeginErrorReadLine();
        string output = p.StandardOutput.ReadToEnd();
        p.WaitForExit();
        var r = new Result { Code = p.ExitCode, Out = output };
        lock (err) r.Err = err.ToString().Trim();
        p.Dispose();
        return r;
    }

    public static string Base64(string text) { return Convert.ToBase64String(new UTF8Encoding(false).GetBytes(text)); }
    // One Windows command-line argument. A backslash before the closing quote would escape it.
    public static string Quote(string arg)
    {
        return "\"" + (arg.EndsWith("\\") ? arg + "\\" : arg) + "\"";
    }
}

// The transfer bridge: the clipboard both ways, and files through the guest's
// ~/Transfer. It reaches the guest only through its sshd, over the loopback forward, with the
// key handed in at boot, and the OS has no knowledge of it. Windows' OpenSSH shares nothing
// between connections, so the clipboard and the outbox each hold one session for the life
// of a boot, and a session that ends is started again once the guest answers.
class Bridge
{
    // The graphical session's display, which a login over ssh does not have
    // Clipboard text is Wayland text: UTF-8, LF line ends.
    const string Session = @"
export XDG_RUNTIME_DIR=/run/user/$(id -u)
display=$(systemctl --user show-environment | grep -E '^WAYLAND_DISPLAY=') || {
    echo 'the graphical session is not up: the user manager has no WAYLAND_DISPLAY' >&2; exit 3; }
export ""$display""
";
    // Each change to the guest's selection, as a line of base64. A cleared selection
    // (CLIPBOARD_STATE=nil) says nothing, so it never empties Windows' clipboard.
    const string Watch = Session + @"
exec wl-paste --no-newline --type text --watch bash -c '[ ""$CLIPBOARD_STATE"" = data ] || exit 0; base64 -w0; echo'
";
    // ⚠ It says what it did with every line, because nothing else can. The launcher knows it
    // wrote to the channel and `wl-paste` is not watching this side, so a token that never
    // arrives and a token that arrived and was refused look identical from Windows — and both
    // look identical to a terminal that cannot paste. The count is of base64 characters, so
    // the log says a text of about the right size landed without saying what it was. The
    // primary selection too: a terminal's right-click pastes it (foot has no clipboard paste
    // on a button), and it must paste what was copied on Windows.
    const string Feed = Session + @"
echo 'ready for what is copied on Windows' >&2
while IFS= read -r line; do
    if printf %s ""$line"" | base64 -d | wl-copy && printf %s ""$line"" | base64 -d | wl-copy --primary; then
        echo ""took ${#line} base64 characters"" >&2
    else
        echo 'wl-copy refused what the launcher sent' >&2
    fi
done
echo 'the launcher closed the channel' >&2
";
    // Files cross as tar streams rather than SFTP: Windows' sftp treats a path as a glob and
    // turns `\` into `/`, so some names cannot be addressed at all.
    // The same bytes for the same tree every time, so a hash of this stream names what was
    // sent: no atime or ctime, no process id in the extended header names.
    const string Pack = "tar --sort=name --format=pax " +
        "--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime -cf - -- \"$1\"";
    // Every tab can write ~/Transfer, so `out` is entered only when it is the directory itself:
    // a link put in its place would have its target emptied into Downloads.
    const string IntoOut = "mkdir -p ~/Transfer/out && cd -P ~/Transfer/out || exit\n" +
        "[ \"$(pwd)\" = \"$(cd -P ~ && pwd)/Transfer/out\" ] || " +
        "{ echo \"~/Transfer/out is a link, not a directory, so nothing is taken from it\" >&2; exit 5; }\n";
    // Everything in ~/Transfer/out that has not changed since the last look, as
    // `<fingerprint> <base64 name>` lines ended by a blank line, printed when that set changes.
    const string Outbox = "export LC_ALL=C.UTF-8\n" + IntoOut + @"
shopt -s nullglob dotglob
declare -A last now
shown=none
while :; do
    now=()
    listing=
    for e in *; do
        h=$(find ""./$e"" -printf '%P\t%y\t%s\t%T@\n' | sort | sha256sum)
        now[$e]=${h%% *}
        [ ""${last[$e]-}"" = ""${now[$e]}"" ] && listing+=""${now[$e]} $(printf %s ""$e"" | base64 -w0)""$'\n'
    done
    last=()
    for k in ""${!now[@]}""; do last[$k]=${now[$k]}; done
    if [ ""$listing"" != ""$shown"" ]; then printf '%s\n' ""$listing""; shown=$listing; fi
    sleep 2
done
";
    const string Send = "export LC_ALL=C.UTF-8\n" + IntoOut + "exec " + Pack + "\n";
    // Removes an entry from out only if it is still exactly what the launcher received.
    const string Remove = "export LC_ALL=C.UTF-8\nset -o pipefail\n" + IntoOut +
        "h=$(" + Pack + @" | sha256sum) || exit
h=${h%% *}
if [ ""$h"" != ""$2"" ]; then
    echo ""it changed while it was copied, so it stays in ~/Transfer/out and the new one follows"" >&2
    exit 4
fi
rm -rf -- ""$1""
";
    // A dropped file lands in ~/Transfer under its own name, or `name (2)` beside one already
    // there, and only once it has all arrived. The stream's hash comes back first so the
    // launcher can say whether what landed is what it sent.
    const string Receive = @"
export LC_ALL=C.UTF-8
set -e
mkdir -p ~/Transfer
t=$(mktemp -d ~/Transfer/.incoming.XXXXXX)
trap 'rm -rf -- ""$t"" ""$t.tar""' EXIT
cat > ""$t.tar""
h=$(sha256sum < ""$t.tar"")
echo ""${h%% *}""
tar -xf ""$t.tar"" -C ""$t""
shopt -s nullglob dotglob
for e in ""$t""/*; do
    b=${e##*/} stem=${e##*/} ext= i=2
    case $b in ?*.*) stem=${b%.*} ext=.${b##*.} ;; esac
    d=""$HOME/Transfer/$b""
    while [ -e ""$d"" ] || [ -L ""$d"" ]; do d=""$HOME/Transfer/$stem ($i)$ext""; i=$((i + 1)); done
    mv -nT -- ""$e"" ""$d""
    echo ""${d##*/}""
done
";

    readonly Window window;
    readonly Ssh ssh;
    readonly string downloads;
    Process watch, feed, outbox;
    volatile bool stopped;
    readonly object gate = new object();
    // The last thing each topic logged, so a boot's worth of refusals is one line.
    readonly Dictionary<string, string> said = new Dictionary<string, string>();

    // Clipboard text is compared in the guest's form. Each side's own write comes back to it
    // as a change: `fromGuest` is what Windows was last given, and `echoes` what the guest was
    // given and has not yet reported back, oldest first.
    string fromGuest, pending;
    readonly LinkedList<string> echoes = new LinkedList<string>();
    readonly AutoResetEvent fed = new AutoResetEvent(false);

    readonly Queue<string> drops = new Queue<string>();
    // The outbox's listings as `(fingerprint, base64 name)`: the one being read, the newest
    // complete one not yet acted on, and what has been acted on, so each entry is tried once.
    List<KeyValuePair<string, string>> building = new List<KeyValuePair<string, string>>();
    List<KeyValuePair<string, string>> pendingListing;
    readonly HashSet<string> handled = new HashSet<string>();

    public Bridge(Window window, Ssh ssh)
    {
        this.window = window;
        this.ssh = ssh;
        downloads = ssh.Outbox;
    }

    public void Start()
    {
        new Thread(Run) { IsBackground = true, Name = "bridge" }.Start();
        new Thread(Feeder) { IsBackground = true, Name = "clipboard to the guest" }.Start();
    }

    public void Stop()
    {
        stopped = true;
        fed.Set();
        foreach (var p in new[] { watch, feed, outbox })
            End(p);
    }

    void Run()
    {
        try
        {
            Loop();
        }
        catch (Exception e)
        {
            Tell("The clipboard and file transfer stopped; the OS itself is unaffected.\n\n" + e);
            Stop();
        }
    }

    bool keyRefusalTold;

    void Loop()
    {
        while (!stopped)
        {
            if (!Alive(watch) || !Alive(feed) || !Alive(outbox))
            {
                var r = ssh.Run("true");
                if (r.Code != 0)
                {
                    Say("ssh", "the guest does not take the launcher's login yet (code " + r.Code +
                               "): " + r.Err);
                    // Not up yet is retried; a refused key is not, because the keys are handed
                    // in at boot and nothing changes them before the next one.
                    if (r.Err.Contains("Permission denied") && !keyRefusalTold)
                    {
                        keyRefusalTold = true;
                        Tell("The OS refuses the launcher's login, so the clipboard and file "
                             + "transfer will not work until the window is closed and opened "
                             + "again: " + r.Err);
                    }
                    Thread.Sleep(2000);
                    continue;
                }
                Say("ssh", "the guest takes the launcher's login");
                if (!Alive(feed))
                {
                    feed = Open("clipboard to the guest", Feed, null);
                    fed.Set();
                }
                if (!Alive(watch))
                    watch = Open("clipboard from the guest", Watch, FromGuest);
                if (!Alive(outbox))
                {
                    handled.Clear();     // a new boot: what failed before is tried again
                    outbox = Open("~/Transfer/out", Outbox, OutboxLine);
                }
                // A session that cannot run yet (no graphical session) ends at once; this is
                // the pace it is tried again at.
                Thread.Sleep(2000);
            }
            Upload();
            Deliver();
            Thread.Sleep(500);
        }
    }

    Process Open(string topic, string script, Action<string> line)
    {
        var p = ssh.Start(script);
        p.EnableRaisingEvents = true;
        p.ErrorDataReceived += delegate(object o, DataReceivedEventArgs e)
        {
            if (e.Data != null) Say(topic, e.Data);
        };
        p.OutputDataReceived += delegate(object o, DataReceivedEventArgs e)
        {
            if (e.Data != null && line != null) line(e.Data);
        };
        p.Exited += delegate { if (!stopped) Say(topic + " exit", "ended with code " + p.ExitCode); };
        p.BeginErrorReadLine();
        p.BeginOutputReadLine();
        return p;
    }

    static bool Alive(Process p) { return p != null && !p.HasExited; }

    static void End(Process p)
    {
        if (p == null) return;
        try
        {
            p.Kill();
        }
        catch (InvalidOperationException)
        {
            // it had already exited
        }
    }

    void Say(string topic, string line)
    {
        lock (said)
        {
            string last;
            if (said.TryGetValue(topic, out last) && last == line)
                return;
            said[topic] = line;
        }
        window.Note(topic + ": " + line);
    }

    // --- the clipboard ---------------------------------------------------------------

    // Windows' clipboard changed (on the window's thread).
    public void WindowsCopied(string text)
    {
        string g = text.Replace("\r\n", "\n");
        lock (gate)
        {
            if (g == fromGuest || g == pending)
                return;
            pending = g;
        }
        fed.Set();
    }

    // The latest Windows text goes to the guest; one copied while it is unreachable waits.
    void Feeder()
    {
        while (true)
        {
            fed.WaitOne();
            if (stopped) return;
            string g;
            Process p = feed;
            lock (gate)
            {
                if (pending == null || !Alive(p)) continue;
                g = pending;
                pending = null;
                echoes.AddLast(g);
                // A text wl-copy refused never comes back; the list is only memory.
                while (echoes.Count > 16) echoes.RemoveFirst();
            }
            try
            {
                byte[] line = Encoding.ASCII.GetBytes(Ssh.Base64(g) + "\n");
                p.StandardInput.BaseStream.Write(line, 0, line.Length);
                p.StandardInput.BaseStream.Flush();
            }
            catch (IOException e)
            {
                Say("clipboard to the guest", "the session closed before the text was sent: " + e.Message);
                lock (gate) if (pending == null) pending = g;
            }
        }
    }

    void FromGuest(string line)
    {
        string g;
        try
        {
            g = new UTF8Encoding(false).GetString(Convert.FromBase64String(line));
        }
        catch (FormatException e)
        {
            Tell("A copy made in the OS arrived garbled and was not put on Windows' clipboard: "
                 + e.Message);
            return;
        }
        lock (gate)
        {
            var node = echoes.Find(g);
            if (node != null)
            {
                while (echoes.First != node) echoes.RemoveFirst();
                echoes.RemoveFirst();
                return;
            }
            if (g == fromGuest) return;
            fromGuest = g;
        }
        window.BeginInvoke(new Action(() => window.SetClipboard(g.Replace("\n", "\r\n"))));
    }

    // --- files in ----------------------------------------------------------------------

    public void Dropped(string[] paths)
    {
        lock (drops)
            foreach (string p in paths) drops.Enqueue(p);
        foreach (string p in paths) window.Note("~/Transfer: dropped " + p);
    }

    void Upload()
    {
        while (!stopped)
        {
            string path;
            lock (drops)
            {
                if (drops.Count == 0) return;
                path = drops.Dequeue();
            }
            string name = Path.GetFileName(path.TrimEnd('\\'));
            try
            {
                if (name.Length == 0)
                    throw new IOException("a whole drive cannot be dropped");
                string landed = Pipe(ssh.Tar("-cf - -C " + Ssh.Quote(Path.GetDirectoryName(path)) +
                                             " " + Ssh.Quote(name)),
                                     ssh.Start(Receive), true);
                window.Note("~/Transfer: " + path + " arrived as ~/Transfer/" + landed);
            }
            catch (Exception e)
            {
                Tell(path + " did not reach ~/Transfer: " + e.Message);
            }
        }
    }

    // --- files out ---------------------------------------------------------------------

    void OutboxLine(string line)
    {
        lock (gate)
        {
            if (line.Length == 0)
            {
                pendingListing = building;
                building = new List<KeyValuePair<string, string>>();
                return;
            }
            int space = line.IndexOf(' ');
            building.Add(new KeyValuePair<string, string>(line.Substring(0, space), line.Substring(space + 1)));
        }
    }
    void Deliver()
    {
        List<KeyValuePair<string, string>> entries;
        lock (gate)
        {
            if (pendingListing == null) return;
            entries = pendingListing;
            pendingListing = null;
        }
        var present = new HashSet<string>();
        foreach (var entry in entries)
        {
            string key = entry.Key + " " + entry.Value;
            present.Add(key);
            if (handled.Contains(key)) continue;
            handled.Add(key);
            Fetch(entry.Key, entry.Value);
        }
        handled.IntersectWith(present);
    }

    // A tar stream from the guest, unpacked beside its destination and moved in whole, then
    // removed from out only if the guest still packs the same bytes. So a copy that failed or
    // that raced a change loses nothing.
    void Fetch(string fingerprint, string name64)
    {
        string name = new UTF8Encoding(false).GetString(Convert.FromBase64String(name64));
        string partial = Path.Combine(downloads, ".partial");
        // The guest names the entry, and a Linux name may hold `C:\` or `\\server`, which
        // Path.Combine takes as the whole path: only one name Windows can give a file is fetched.
        if (!PlainName(name))
        {
            Tell(name + " stays in ~/Transfer/out: Windows cannot name a file that.");
            return;
        }
        try
        {
            if (Directory.Exists(partial))
                Directory.Delete(partial, true);
            Directory.CreateDirectory(partial);
            string hash = Pipe(ssh.Start(Send, name), ssh.Tar("-xf - -C " + Ssh.Quote(partial)), false);
            string unpacked = Path.Combine(partial, name);
            if (!File.Exists(unpacked) && !Directory.Exists(unpacked))
                throw new IOException("tar.exe unpacked something other than " + name);
            FromElsewhere(unpacked);
            string target = Unique(downloads, name);
            if (Directory.Exists(unpacked)) Directory.Move(unpacked, target);
            else File.Move(unpacked, target);
            Directory.Delete(partial, true);
            var r = ssh.Run(Remove, name, hash);
            if (r.Code == 4)
                window.Note("~/Transfer/out: " + name + ": " + r.Err);
            else if (r.Code != 0)
                Tell(name + " was copied to " + target + " but could not be removed from " +
                     "~/Transfer/out (code " + r.Code + "): " + r.Err);
            else
                window.Note("~/Transfer/out: " + name + " moved to " + target);
        }
        catch (Exception e)
        {
            Tell(name + " stays in ~/Transfer/out: it could not be copied to " + downloads + ": " +
                 e.Message);
        }
    }

    // Windows' device names, which it reads as the device with any extension and before
    // trailing spaces.
    static readonly Regex Device = new Regex(
        @"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³]) *(\..*)?$",
        RegexOptions.IgnoreCase);

    // Windows drops a name's trailing dots and spaces, so `...` or `a.` would name another file.
    static bool PlainName(string name)
    {
        return name.Length > 0 && name.Length <= 255 && !name.EndsWith(".") &&
               !name.EndsWith(" ") && name.IndexOfAny(Path.GetInvalidFileNameChars()) < 0 &&
               !Device.IsMatch(name);
    }

    // Marked as from the internet, as a browser marks a download: a tab wrote it, so Windows
    // asks before running it. Links are left alone, so no mark lands outside Downloads.
    static void FromElsewhere(string path)
    {
        var files = Directory.Exists(path)
            ? Directory.EnumerateFiles(path, "*", new EnumerationOptions {
                  RecurseSubdirectories = true, AttributesToSkip = FileAttributes.ReparsePoint })
            : new[] { path };
        foreach (string file in files)
            if (!File.GetAttributes(file).HasFlag(FileAttributes.ReparsePoint))
                File.WriteAllText(file + ":Zone.Identifier", "[ZoneTransfer]\r\nZoneId=3\r\n");
    }

    static string Unique(string dir, string name)
    {
        string stem = Path.GetFileNameWithoutExtension(name), ext = Path.GetExtension(name);
        string p = Path.Combine(dir, name);
        for (int i = 2; File.Exists(p) || Directory.Exists(p); i++)
            p = Path.Combine(dir, stem + " (" + i + ")" + ext);
        return p;
    }

    // Copies one process's output into another's input and hashes the bytes on the way.
    // Upload: the first line the guest prints is its hash of what it received, which must
    // match, and the rest is where it landed. Fetch: returns the hash for the removal to check.
    string Pipe(Process from, Process to, bool upload)
    {
        var toResult = new Ssh.Result();
        var reader = new Thread(() => { toResult = Ssh.Finish(to); });
        string hash;
        Ssh.Result fromResult;
        from.StandardInput.Close();
        var err = new StringBuilder();
        from.ErrorDataReceived += delegate(object o, DataReceivedEventArgs e)
        {
            if (e.Data != null) lock (err) err.AppendLine(e.Data);
        };
        from.BeginErrorReadLine();
        reader.Start();
        using (var sha = System.Security.Cryptography.SHA256.Create())
        {
            var buffer = new byte[65536];
            var src = from.StandardOutput.BaseStream;
            var dst = to.StandardInput.BaseStream;
            int n;
            while ((n = src.Read(buffer, 0, buffer.Length)) > 0)
            {
                sha.TransformBlock(buffer, 0, n, null, 0);
                dst.Write(buffer, 0, n);
            }
            sha.TransformFinalBlock(buffer, 0, 0);
            dst.Close();
            hash = BitConverter.ToString(sha.Hash).Replace("-", "").ToLowerInvariant();
        }
        from.WaitForExit();
        reader.Join();
        fromResult = new Ssh.Result { Code = from.ExitCode };
        lock (err) fromResult.Err = err.ToString().Trim();
        from.Dispose();
        if (fromResult.Code != 0)
            throw new IOException((upload ? "tar.exe" : "the guest's tar") + " failed (code " +
                                  fromResult.Code + "): " + fromResult.Err);
        if (toResult.Code != 0)
            throw new IOException((upload ? "the guest" : "tar.exe") + " failed (code " +
                                  toResult.Code + "): " + toResult.Err);
        if (!upload)
            return hash;
        var lines = toResult.Out.Replace("\r", "").Trim().Split('\n');
        if (lines[0] != hash)
            throw new IOException("the guest received different bytes from those sent (" +
                                  lines[0] + ", sent " + hash + "); what landed may be damaged: " +
                                  string.Join(", ", lines, 1, lines.Length - 1));
        return string.Join(", ", lines, 1, lines.Length - 1);
    }

    void Tell(string message) { window.Warn(message); }

}

// QEMU's machine protocol: one JSON object per line, a greeting, then capabilities
// negotiation before any command. Events may arrive between a command and its answer.
sealed class Qmp : IDisposable
{
    readonly TcpClient client;
    readonly StreamReader reader;
    readonly StreamWriter writer;

    Qmp(TcpClient client)
    {
        this.client = client;
        var stream = client.GetStream();
        reader = new StreamReader(stream, Encoding.UTF8);
        writer = new StreamWriter(stream, new UTF8Encoding(false));
        writer.NewLine = "\r\n";
        writer.AutoFlush = true;
    }

    public static Qmp Open(int port)
    {
        var client = new TcpClient();
        try
        {
            client.Connect(IPAddress.Loopback, port);
            client.ReceiveTimeout = 5000;
            var q = new Qmp(client);
            string greeting = q.reader.ReadLine();
            if (greeting == null || !greeting.Contains("\"QMP\""))
                throw new IOException("QEMU's control socket did not greet: " + greeting);
            q.Call("qmp_capabilities", null);
            return q;
        }
        catch
        {
            client.Dispose();
            throw;
        }
    }

    // The command's answer line; `arguments` is a JSON object or null.
    public static string Execute(int port, string command, string arguments)
    {
        using (var q = Open(port))
            return q.Call(command, arguments);
    }

    public string Call(string command, string arguments)
    {
        writer.WriteLine("{\"execute\":\"" + command + "\"" +
                         (arguments == null ? "" : ",\"arguments\":" + arguments) + "}");
        while (true)
        {
            string line = reader.ReadLine();
            if (line == null)
                throw new IOException("QEMU closed its control socket during " + command);
            if (line.StartsWith("{\"return\""))
                return line;
            if (line.StartsWith("{\"error\""))
                throw new IOException(command + " was refused: " + line);
        }
    }

    public void Dispose()
    {
        client.Dispose();
    }
}

static class Native
{
    // A job that kills its processes when its last handle closes — this process's end, however
    // it ends.
    public static IntPtr KillOnClose(Process process)
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero)
            throw new LauncherError("CreateJobObject failed: error " + Marshal.GetLastWin32Error());
        var limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, ref limits,
                                     (uint)Marshal.SizeOf<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>()))
            throw new LauncherError("SetInformationJobObject failed: error " + Marshal.GetLastWin32Error());
        if (!AssignProcessToJobObject(job, process.Handle))
            throw new LauncherError("QEMU could not be tied to this window (AssignProcessToJobObject: error " +
                                    Marshal.GetLastWin32Error() + ")");
        return job;
    }

    const int JobObjectExtendedLimitInformation = 9;
    const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount,
                     ReadTransferCount, WriteTransferCount, OtherTransferCount;   // IO_COUNTERS
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, uint length);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    public const int SW_RESTORE = 9;
    public const int WH_KEYBOARD_LL = 13, HC_ACTION = 0;
    public const int WM_KEYDOWN = 0x0100, WM_SYSKEYDOWN = 0x0104;
    public const uint LLKHF_EXTENDED = 0x01;

    [StructLayout(LayoutKind.Sequential)]
    public struct KBDLLHOOKSTRUCT { public uint vkCode, scanCode, flags, time; public IntPtr extra; }

    public delegate IntPtr LowLevelKeyboardProc(int code, IntPtr wparam, IntPtr lparam);

    // A path as QEMU can be handed it: QEMU takes its command line in the ANSI code page (a
    // plain `main`, no UTF-8 manifest), so a path with any other character is given as its
    // 8.3 short name, which is ASCII where Windows keeps one.
    public static string Ascii(string path)
    {
        if (IsAscii(path))
            return path;
        var shortened = new StringBuilder(1024);
        uint length = GetShortPathName(path, shortened, (uint)shortened.Capacity);
        if (length == 0 || length >= shortened.Capacity || !IsAscii(shortened.ToString()))
            throw new LauncherError(path + " has characters QEMU cannot be given, and Windows " +
                                    "keeps no short ASCII name for it. Move RaiGolmi's files to " +
                                    "a folder whose path is plain letters and digits.");
        return shortened.ToString();
    }

    static bool IsAscii(string s)
    {
        foreach (char c in s)
            if (c > 127)
                return false;
        return true;
    }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern uint GetShortPathName(string longPath, StringBuilder shortPath, uint size);

    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hwnd, int cmd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll", SetLastError = true)]
    public static extern IntPtr SetWindowsHookEx(int id, LowLevelKeyboardProc proc, IntPtr module, uint thread);
    [DllImport("user32.dll")] public static extern bool UnhookWindowsHookEx(IntPtr hook);
    [DllImport("user32.dll")]
    public static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr wparam, IntPtr lparam);
    [DllImport("kernel32.dll")] public static extern IntPtr GetModuleHandle(string name);

    public const int WM_CLIPBOARDUPDATE = 0x031D;
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool AddClipboardFormatListener(IntPtr hwnd);

    public static readonly Guid Downloads = new Guid("374DE290-123F-4565-9164-39C4925E467B");
    [DllImport("shell32.dll")]
    static extern int SHGetKnownFolderPath([MarshalAs(UnmanagedType.LPStruct)] Guid id, uint flags,
                                           IntPtr token, out IntPtr path);
    [DllImport("ole32.dll")] static extern void CoTaskMemFree(IntPtr p);

    // .NET maps a Windows zone to its IANA name with Windows' own ICU data.
    public static string IanaZone(string windowsId)
    {
        string iana;
        if (!TimeZoneInfo.TryConvertWindowsIdToIanaId(windowsId, out iana))
            throw new LauncherError("Windows' timezone '" + windowsId + "' has no IANA name that " +
                                    ".NET can find, so RaiGolmi cannot take it.");
        return iana;
    }

    public static string KnownFolder(Guid id)
    {
        IntPtr p;
        int hr = SHGetKnownFolderPath(id, 0, IntPtr.Zero, out p);
        if (hr != 0)
            throw new LauncherError("Windows could not say where the folder " + id + " is (0x" +
                                    hr.ToString("x8") + ").");
        string path = Marshal.PtrToStringUni(p);
        CoTaskMemFree(p);
        return path;
    }
}
