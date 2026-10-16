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
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

static class Launcher
{
    const string Title = "RaiGolmi";
    // The project folder, the parent of windows\, where this exe is: setup.ps1, the keeper's
    // keeper\ and the machine's state\ are in it.
    public static readonly string Project = Path.GetFullPath(Path.Combine(Application.StartupPath, ".."));
    // MSYS2's UCRT64 tree; RAIGOLMI_MSYS2 names an MSYS2 installed elsewhere, as for build.ps1.
    static readonly string QemuDir = Path.Combine(
        Environment.GetEnvironmentVariable("RAIGOLMI_MSYS2") ?? @"C:\msys64", "ucrt64");

    [STAThread]
    static int Main(string[] args)
    {
        Native.SetProcessDPIAware();
        int recoveries = args.Length == 2 && args[0] == RecoverFlag ? int.Parse(args[1]) : 0;
        bool door = args.Length == 1 && args[0] == DoorFlag;
        using (var single = new Mutex(false, @"Local\RaiGolmi-launcher"))
        {
            if (!Own(single, recoveries > 0))
            {
                // The door finds the launcher it would start, and leaves the user's desktop alone.
                if (!door)
                    RaiseRunningInstance();
                return 0;
            }
            bool tookStart = false;
            if (door)
            {
                string reason;
                try
                {
                    // A door outliving its setting (settings.ini edited by hand) opens nothing.
                    if (!Settings.Load(Machine.Home).Keeper)
                        return 0;
                    reason = Keeper.DoorReason(out tookStart);
                }
                catch (Exception e) when (e is IOException || e is UnauthorizedAccessException || e is LauncherError)
                {
                    DoorFailed(e.Message, false, recoveries);
                    return 1;
                }
                if (reason == null)
                    return 0;
                Log("door: starting the machine: " + reason);
            }
            Application.EnableVisualStyles();
            Machine machine;
            try
            {
                machine = Machine.Prepare(recoveries, door || recoveries > 0);
            }
            catch (Exception e)
            {
                // Anything else from Prepare is a defect, said with its type and stack: before
                // the window no handler would otherwise record it, and the process ends unseen.
                string why = e is LauncherError ? e.Message : "starting the machine failed: " + e;
                // A box would hold the mutex the door needs until somebody came: a successor and
                // a launch with the keeper on (the sign-in one among them) may have nobody there.
                if (door || recoveries > 0 || KeeperSetting())
                    DoorFailed(why, tookStart, recoveries);
                else
                {
                    Log("could not start: " + why);
                    Fail(why);
                }
                return 1;
            }
            if (machine == null)
                return 0;        // the disk picker was cancelled
            if (tookStart && machine.Keeper != null)
                machine.Keeper.Record("start", "the door started a launcher");
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.ThrowException);
            AppDomain.CurrentDomain.UnhandledException += (sender, e) => Crashed(machine, e.ExceptionObject);
            Application.Run(new Window(machine, recoveries));
            return 0;
        }
    }

    // Passed to the launcher a recovering one starts, with how many recoveries in a row led to it.
    public const string RecoverFlag = "--recovered";
    // The keeper's door, the scheduled task that starts a launcher when none runs and one should.
    public const string DoorFlag = "--keeper-door";

    // Whether the keeper setting is on; false for settings that cannot be read, which no launch
    // can run with.
    static bool KeeperSetting()
    {
        try
        {
            return Settings.Load(Machine.Home).Keeper;
        }
        catch (Exception e) when (e is IOException || e is UnauthorizedAccessException || e is LauncherError)
        {
            return false;
        }
    }

    // An unattended launch could not start: nobody may be at the screen to read a box, so it
    // goes to the log and, with the keeper on, to launcher.json as a launcher that has given up,
    // for the keeper to repair. With no keeper there to hear it, the phone is told. Settings that
    // cannot be read say neither whether the keeper is on nor the topic: launcher.json is written
    // only if one is there (Retire removes it with the setting), and no alert can be sent.
    static void DoorFailed(string why, bool tookStart, int recoveries)
    {
        Log("door: the launcher could not start: " + why);
        Settings settings = null;
        try
        {
            settings = Settings.Load(Machine.Home);
        }
        catch (Exception e) when (e is IOException || e is UnauthorizedAccessException || e is LauncherError)
        {
            Log("door: the settings could not be read, so no alert can be sent: " + e.Message);
        }
        if (settings != null ? settings.Keeper : Keeper.Wrote())
        {
            try
            {
                var keeper = new Keeper(recoveries);
                if (tookStart)
                    keeper.Record("start", "the launcher could not start: " + why);
                keeper.Final("gave_up", "the launcher could not start: " + why);
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException || e is LauncherError)
            {
                Log("door: launcher.json could not be written: " + e.Message);
            }
        }
        if (settings == null || settings.NtfyTopic.Length == 0 || (settings.Keeper && Keeper.Present()))
            return;
        string failed = System.Threading.Tasks.Task.Run(() => Window.Push(
            settings.NtfyTopic, "RaiGolmi needs you", Window.RestartThePc + " RaiGolmi stopped and could " +
            "not start again: " + why + " (" + Environment.MachineName + ")")).GetAwaiter().GetResult();
        Log("door: alert: " + (failed ?? "sent"));
    }

    // A line in the launcher log before any window has it, as the window's own lines read.
    public static void Log(string line)
    {
        File.AppendAllText(Path.Combine(Application.StartupPath, "qemu.log"),
                           "launcher: " + line + Environment.NewLine);
    }

    // A successor waits for the launcher that started it to let go; anyone else finding the
    // machine running hands its window back. A mutex its owner never released still passes.
    static bool Own(Mutex single, bool successor)
    {
        try
        {
            return single.WaitOne(successor ? TimeSpan.FromMinutes(2) : TimeSpan.Zero);
        }
        catch (AbandonedMutexException)
        {
            return true;
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
        // With the keeper on, a box would hold this process, and with it the mutex the door
        // needs, until somebody came; launcher.json keeps the state it had, so the door starts
        // the machine again.
        Keeper keeper = machine.Keeper;
        if (keeper != null)
        {
            var e = error as Exception;
            keeper.Final(null, "the launcher crashed: " + (e != null ? e.GetType().Name + ": " + e.Message : error));
            return;
        }
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

// Everything this machine's QEMU needs: its state in the project folder's state\, its log and
// screen beside the exe.
class Machine
{
    public string Qemu, Firmware, Vars, Disk, Log, Screen, Placement;
    // With Venus on, beside qemu.log: QEMU's guest errors (-D) and virglrenderer's own log
    // (VIRGL_LOG_FILE); null otherwise. Each is an ASCII path, as QEMU and virglrenderer open
    // them with narrow-character fopen.
    public string GuestErrorsLog, VirglLog;
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
    public Settings Settings;
    // The machine's RAM in MiB, from the memory setting.
    public int MemoryMiB;
    // Why the machine has no sound card this start, or null when it has one.
    public string NoSound;
    // The keeper's side, while the keeper setting is on; null otherwise.
    public volatile Keeper Keeper;

    // The settings, keys, UEFI variables and disk record, kept with the project so the folder
    // copied to another PC carries all of it; the keeper reads and writes it (keeper\compose.yml).
    public static readonly string Home = Path.Combine(Launcher.Project, "state");

    // `door`: started by the keeper's door, where nobody may be at the screen, so nothing is asked.
    public static Machine Prepare(int recoveries, bool door)
    {
        string home = Home;
        Directory.CreateDirectory(home);
        var m = new Machine();
        m.Settings = Settings.Load(home);
        m.Settings.ApplyStartAtLogin();
        if (m.Settings.Keeper)
            m.Keeper = new Keeper(recoveries);
        m.MemoryMiB = Settings.MemoryMiB(m.Settings.Memory);
        m.NoSound = AudioEndpoints.Missing();
        m.Qemu = m.Settings.Venus ? RequireVenus(Launcher.QemuPath(VenusQemu))
                                  : Require(Launcher.QemuPath(@"bin\qemu-system-x86_64w.exe"));
        m.Firmware = Require(Launcher.QemuPath(@"share\qemu\edk2-x86_64-code.fd"));
        m.Log = Path.Combine(Application.StartupPath, "qemu.log");
        if (m.Settings.Venus)
        {
            string dir = Native.Ascii(Application.StartupPath);
            m.GuestErrorsLog = Path.Combine(dir, "qemu-guest-errors.log");
            m.VirglLog = Path.Combine(dir, "virglrenderer.log");
        }
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
        m.Disk = ChooseDisk(Path.Combine(home, "disk.txt"), door);
        if (m.Disk == null)
            return null;
        m.ControlPort = FreePort();
        m.WatchPort = FreePort();
        m.CommandLine = m.Arguments();
        return m;
    }

    // Venus runs the patched QEMU's copy beside the winq-emu fork's virglrenderer, which
    // raigolmi-packages' virglrenderer-venus installs; the default DLL in bin is a different
    // build, so without the copy Venus does not start at all.
    const string VenusQemu = @"raigolmi-venus\qemu-system-x86_64w.exe";

    static string RequireVenus(string path)
    {
        if (!File.Exists(path))
            throw new LauncherError(path + " does not exist, and the experimental Venus (Vulkan) " +
                                    "setting runs QEMU from there. Run setup.bat, which installs it, " +
                                    "or set venus=off in " + Path.Combine(Home, "settings.ini") +
                                    ", then start RaiGolmi again.");
        return path;
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
    static string ChooseDisk(string record, bool door)
    {
        if (File.Exists(record))
        {
            string kept = File.ReadAllText(record).Trim();
            if (File.Exists(kept))
                return kept;
            if (door)
                throw new LauncherError("The RaiGolmi disk was at " + kept + " and is not there any more.");
            Launcher.Fail("The RaiGolmi disk was at " + kept + " and is not there any more. " +
                          "Choose where it is now.");
        }
        if (door)
            throw new LauncherError("No RaiGolmi disk is recorded in " + record + ".");
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
    //
    // Sound is HDA (ich9-intel-hda, the q35 chipset's own, with an output-only codec, so Windows
    // is never asked for its microphone) into DirectSound, the one Windows backend that stops
    // when the guest is silent: the guest closing its stream stops the codec's timer
    // (hw/audio/hda-codec.c hda_audio_set_running), the voice's disable stops the DirectSound
    // buffer, and the mixer's timer runs only while a voice is enabled (audio/audio-mixeng-be.c
    // audio_is_timer_needed). SDL, the other needing no server, feeds its WASAPI stream silence
    // from its own thread while paused (SDL2 SDL_audio.c SDL_RunAudio). The DirectSound buffer is
    // QEMU's default, 92880 us (audio/dsound_template.h, audio_buffer_bytes): the mixer's timer
    // fills it on Windows' ~15.6 ms timer resolution, which a shorter buffer underruns between
    // ticks however steadily the guest plays.
    string Arguments()
    {
        var a = new List<string>();
        a.Add("-name RaiGolmi");
        a.Add("-machine type=q35,accel=whpx,kernel-irqchip=on -cpu max -m " + MemoryMiB + " -smp " + Environment.ProcessorCount);
        // A hung guest kernel stops pinging the q35 chipset's watchdog (host/systemd/
        // runtime-watchdog.conf). QEMU pauses the guest then, and Watch restarts the machine
        // cold, the path this launcher owns and counts, never a reset in place behind it.
        a.Add("-action watchdog=pause");
        a.Add("-drive " + Quote("if=pflash,format=raw,readonly=on,file=" + Opt(Native.Ascii(Firmware))));
        a.Add("-drive " + Quote("if=pflash,format=raw,file=" + Opt(Native.Ascii(Vars))));
        // What the guest discards (its trim every ten minutes and at shutdown) the patched QEMU
        // releases from the sparse file, so the file shrinks with the guest's use while it runs.
        a.Add("-drive " + Quote("file=" + Opt(Native.Ascii(Disk)) + ",format=qcow2,discard=unmap,detect-zeroes=unmap"));
        // Venus needs blob resources and host memory for them (hw/display/virtio-gpu.c
        // virtio_gpu_device_realize), and a virglrenderer built with it.
        a.Add("-device " + (Settings.Venus ? "virtio-vga-gl,blob=on,hostmem=4G,venus=on" : "virtio-vga-gl") +
              " -device qemu-xhci,id=xhci -device usb-tablet,bus=xhci.0");
        if (NoSound == null)
            a.Add("-audiodev dsound,id=snd0 -device ich9-intel-hda " +
                  "-device hda-output,audiodev=snd0");
        a.Add("-netdev user,id=net0,hostfwd=tcp:127.0.0.1:" + SshPort + "-:22 -device virtio-net,netdev=net0");
        // Boot credentials (systemd reads SMBIOS type 11): the login keys, Windows' own
        // timezone, which the guest makes its own (host/systemd/set-timezone), and the phone
        // alerts' ntfy topic when one is set (host/tmpfiles/raigolmi.conf); none is no alerts.
        var credentials = new List<string>();
        if (Ssh != null)
            credentials.Add("path=" + Opt(Native.Ascii(Ssh.Credential)));
        credentials.Add("value=" + Opt("io.systemd.credential:raigolmi.timezone=" +
                                       Native.IanaZone(TimeZoneInfo.Local.Id)));
        if (Settings.NtfyTopic.Length > 0)
            credentials.Add("value=" + Opt("io.systemd.credential:raigolmi.ntfy_topic=" +
                                           Settings.NtfyTopic));
        a.Add("-smbios " + Quote("type=11," + string.Join(",", credentials)));
        a.Add("-display dbus,p2p=yes,gl=on");
        a.Add("-qmp tcp:127.0.0.1:" + ControlPort + ",server=on,wait=off");
        a.Add("-qmp tcp:127.0.0.1:" + WatchPort + ",server=on,wait=off");
        // A blob the guest's Venus asks for and virglrenderer refuses is said only as a guest
        // error (hw/display/virtio-gpu-virgl.c virgl_cmd_resource_create_blob).
        if (GuestErrorsLog != null)
            a.Add("-d guest_errors -D " + Quote(GuestErrorsLog));
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
    bool shutdownSent, exited, poweredOff;
    // Why this launcher is restarting the machine, once it has decided to; null until then.
    string recovering;
    // Windows is ending the session: the machine goes down with it and should come back after.
    bool windowsEnding;
    // setup.ps1 -Unattended, started for the keeper, while it runs with this launcher open.
    volatile bool upgrading;
    Process setup;
    // A request from the keeper's inbox waits for the window's thread.
    volatile bool requestQueued;
    int unanswered;
    readonly int recoveries;
    readonly DateTime started = DateTime.UtcNow;
    readonly StringBuilder stderr = new StringBuilder();
    System.Threading.Timer watcher;
    string lastStatus;
    readonly object qmpLock = new object();
    readonly System.Windows.Forms.Timer sizeSettle = new System.Windows.Forms.Timer();
    readonly Keyboard keyboard;
    readonly Bridge bridge;

    public Window(Machine machine, int recoveries)
    {
        this.machine = machine;
        this.recoveries = recoveries;
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
        if (machine.Keeper != null)
            machine.Keeper.Note = Note;
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
        // QEMU's DLLs (ANGLE, virglrenderer, GLib) are MSYS2's, in bin. Venus's copy of QEMU
        // sits elsewhere with only virglrenderer beside it, which Windows takes from the exe's
        // own directory before any on PATH.
        info.Environment["PATH"] = info.WorkingDirectory + ";" + Launcher.QemuPath("bin") + ";" +
                                   info.Environment["PATH"];
        // virglrenderer logs below ERROR only at the level asked for (virgl_util.c
        // virgl_default_logger), and Venus's render server logs at INFO.
        if (machine.VirglLog != null)
        {
            info.Environment["VIRGL_LOG_FILE"] = machine.VirglLog;
            info.Environment["VIRGL_LOG_LEVEL"] = "debug";
        }
        qemu = new Process();
        qemu.StartInfo = info;
        qemu.EnableRaisingEvents = true;
        qemu.ErrorDataReceived += Record;
        qemu.OutputDataReceived += Record;
        qemu.Exited += delegate { BeginInvoke(new Action(OnQemuExited)); };
        KeepPrevious(machine.Log);
        File.WriteAllText(machine.Log, "\"" + machine.Qemu + "\" " + machine.CommandLine +
                                       Environment.NewLine);
        // Emptied here: virglrenderer appends to its log, and a QEMU that stops before opening
        // its -D file would leave the last run's.
        foreach (string log in new[] { machine.GuestErrorsLog, machine.VirglLog })
            if (log != null)
            {
                KeepPrevious(log);
                File.WriteAllText(log, "");
            }
        qemu.Start();
        // QEMU dies with this program: one outliving its window keeps the disk locked and the
        // ssh port taken, so no later launch could start, and nothing could reach it but ssh.
        job = Native.KillOnClose(qemu);
        qemu.BeginErrorReadLine();
        qemu.BeginOutputReadLine();
        renderer = new Renderer(this, Note);
        new Thread(Connect) { IsBackground = true, Name = "display" }.Start();
        watcher = new System.Threading.Timer(delegate { Watch(); }, null, 5000, 10000);
        ApplySetting(machine.Settings.ApplyKeepAwake);
        if (machine.Keeper != null)
        {
            ApplySetting(machine.Settings.ApplyKeeperDoor);
            TellKeeper();
            Keeper.Compose("up -d", Note);
        }
        if (machine.NoSound != null)
            Warn(machine.NoSound);
        if (bridge == null)
        {
            Warn(machine.NoSsh);
            return;
        }
        bridge.Start();
    }

    // The last run's log is kept beside this one's, for the run that ended badly. One that ended
    // in a restart is also kept under when it was last written, so the restarts that follow
    // cannot overwrite what took the machine down.
    void KeepPrevious(string log)
    {
        if (!File.Exists(log))
            return;
        File.Copy(log, Path.ChangeExtension(log, ".previous.log"), true);
        if (recoveries > 0)
            File.Copy(log, Path.ChangeExtension(log, ".restarted-" +
                File.GetLastWriteTime(log).ToString("yyyyMMdd-HHmmss") + ".log"), true);
    }

    // --- the transfer bridge's Windows side ----------------------------------------------

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        if (bridge != null && !Native.AddClipboardFormatListener(Handle))
            Warn("Windows' clipboard cannot be followed, so copies made in Windows will not reach "
                 + "the OS: error " + Marshal.GetLastWin32Error());
        AddSettingsMenu();
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
        if (m.Msg == Native.WM_SYSCOMMAND && (m.WParam.ToInt64() & 0xFFF0) == MenuSettings)
        {
            ShowSettings();
            m.Result = IntPtr.Zero;
            return;
        }
        // Answered here rather than by Form, whose answer to WM_QUERYENDSESSION is the close
        // button's: a cancelled close, which tells Windows the session may not end, and asks
        // the user on a second one (Form.WmClose).
        if (m.Msg == Native.WM_QUERYENDSESSION)
        {
            WindowsEnding();
            m.Result = (IntPtr)1;
            return;
        }
        if (m.Msg == Native.WM_ENDSESSION)
        {
            if (m.WParam != IntPtr.Zero)
                WindowsEnded();
            else
                WindowsStays();
            m.Result = IntPtr.Zero;
            return;
        }
        base.WndProc(ref m);
    }

    // --- the settings drop-down, opened from the window menu the title bar's icon opens --
    //
    // The caption is Windows' own, which takes no buttons of a program's; its icon's menu is
    // the drop-down that title bar already has, and it holds one item that opens this.
    // A Win32 menu closes on every choice, so the settings are a ToolStripDropDown, which stays
    // open while its items are clicked (an ItemClicked close is cancelled, in the Memory
    // drop-down too, since a nested item's click closes only its own drop-down first) and closes
    // when the user clicks off it, presses Esc, or leaves for another program.
    // It is shown without taking activation (ToolStripDropDown shows SW_SHOWNOACTIVATE), so
    // the window stays the active form: `KeysToGuest` is false while it is open, or the
    // keyboard hook would hand its keys to the guest. A click in the topic box activates the
    // drop-down and focuses the box (WinForms ToolStrip.WndProc, WM_MOUSEACTIVATE), and the menu
    // filter leaves keys with the box while it has focus (ToolStripManager.ModalMenuFilter); the
    // window is then not the active form, so `KeysToGuest` is false on both counts.

    const uint MenuSettings = 0x1000;
    ContextMenuStrip settings;
    ToolStripMenuItem startAtLogin, keepAwake, venus, keeperItem, keeperTerminal;
    // Docker is being asked whether it can run the keeper.
    bool keeperChecking;
    ToolStripTextBox ntfyTopic, heartbeatUrl;
    readonly List<ToolStripMenuItem> memory = new List<ToolStripMenuItem>();

    void AddSettingsMenu()
    {
        IntPtr system = Native.GetSystemMenu(Handle, false);
        if (system == IntPtr.Zero)
            throw new LauncherError("The window's menu could not be read, so Settings cannot be added to it.");
        MenuItem(system, Native.MF_SEPARATOR, 0, null);
        MenuItem(system, Native.MF_STRING, MenuSettings, "Settings");

        settings = new ContextMenuStrip();
        startAtLogin = Toggle("Start RaiGolmi when I sign in to Windows", delegate
        {
            machine.Settings.StartAtLogin = !machine.Settings.StartAtLogin;
            ApplySetting(machine.Settings.ApplyStartAtLogin);
        });
        keepAwake = Toggle("Keep Windows awake while RaiGolmi runs", delegate
        {
            machine.Settings.KeepAwake = !machine.Settings.KeepAwake;
            ApplySetting(machine.Settings.ApplyKeepAwake);
        });
        var memoryItem = new ToolStripMenuItem("Memory (applies at next start)");
        foreach (string choice in Settings.MemoryChoices)
        {
            string chosen = choice;
            bool refused = Settings.Refusal(choice) != null;
            var item = new ToolStripMenuItem(choice + " GB" + (refused ? " — more than this PC can spare" : ""));
            item.Enabled = !refused;
            item.Click += delegate
            {
                machine.Settings.Memory = chosen;
                ApplySetting(null);
            };
            memory.Add(item);
            memoryItem.DropDownItems.Add(item);
        }
        venus = Toggle("Venus (Vulkan), experimental (applies at next start)", delegate
        {
            machine.Settings.Venus = !machine.Settings.Venus;
            ApplySetting(null);
        });
        keeperItem = Toggle("Keeper: an agent outside the machine repairs it (needs Docker Desktop)",
                        delegate { ToggleKeeper(); });
        keeperTerminal = new ToolStripMenuItem("Open the keeper's terminal", null,
                                               delegate { Keeper.OpenTerminal(); });
        var ntfyLabel = new ToolStripLabel("Phone alerts: ntfy topic, empty for none (applies at next start)");
        ntfyTopic = new ToolStripTextBox { AutoSize = false, Width = 320, MaxLength = 64 };
        ntfyTopic.KeyDown += (sender, e) =>
        {
            if (e.KeyCode != Keys.Enter)
                return;
            e.SuppressKeyPress = true;
            CommitTopic();
        };
        ntfyTopic.LostFocus += delegate { CommitTopic(); };
        var testAlert = new ToolStripMenuItem("Send a test alert", null, delegate { SendTestAlert(); });
        var heartbeatLabel = new ToolStripLabel("Heartbeat: healthchecks.io ping URL, empty for none");
        heartbeatUrl = new ToolStripTextBox { AutoSize = false, Width = 320 };
        heartbeatUrl.KeyDown += (sender, e) =>
        {
            if (e.KeyCode != Keys.Enter)
                return;
            e.SuppressKeyPress = true;
            CommitHeartbeat();
        };
        heartbeatUrl.LostFocus += delegate { CommitHeartbeat(); };
        settings.Items.AddRange(new ToolStripItem[] {
            startAtLogin, keepAwake, memoryItem, venus, new ToolStripSeparator(), keeperItem, keeperTerminal,
            new ToolStripSeparator(), ntfyLabel, ntfyTopic, testAlert,
            new ToolStripSeparator(), heartbeatLabel, heartbeatUrl });
        settings.Closing += StayOpenOnClick;
        memoryItem.DropDown.Closing += StayOpenOnClick;
        settings.Opening += delegate { keyboard.Release(); };
        // The window has the keyboard back unless the user went to another program.
        settings.Closed += (sender, e) =>
        {
            if (e.CloseReason != ToolStripDropDownCloseReason.AppFocusChange)
                Activate();
        };
        CheckSettings();
    }

    static ToolStripMenuItem Toggle(string text, EventHandler click)
    {
        var item = new ToolStripMenuItem(text);
        item.Click += click;
        return item;
    }

    static void StayOpenOnClick(object sender, ToolStripDropDownClosingEventArgs e)
    {
        if (e.CloseReason == ToolStripDropDownCloseReason.ItemClicked)
            e.Cancel = true;
    }

    static void MenuItem(IntPtr menu, uint flags, ulong id, string text)
    {
        if (!Native.AppendMenu(menu, flags, (UIntPtr)id, text))
            throw new LauncherError("The window's settings menu could not be made: error " +
                                    Marshal.GetLastWin32Error());
    }

    // Opened just below the caption icon, after the window menu's own loop has returned.
    void ShowSettings()
    {
        BeginInvoke(new Action(() => settings.Show(this, Point.Empty)));
    }

    public bool KeysToGuest { get { return Form.ActiveForm == this && !settings.Visible; } }

    void CheckSettings()
    {
        Settings s = machine.Settings;
        startAtLogin.Checked = s.StartAtLogin;
        keepAwake.Checked = s.KeepAwake;
        venus.Checked = s.Venus;
        keeperItem.Checked = s.Keeper;
        keeperItem.Enabled = !keeperChecking;
        keeperTerminal.Enabled = s.Keeper && !keeperChecking;
        for (int i = 0; i < Settings.MemoryChoices.Length; i++)
            memory[i].Checked = s.Memory == Settings.MemoryChoices[i];
        ntfyTopic.Text = s.NtfyTopic;
        heartbeatUrl.Text = s.HeartbeatUrl;
    }

    // The topic box's text becomes the setting when it is one; any other is refused aloud and
    // the box shows the setting again.
    void CommitTopic()
    {
        string typed = ntfyTopic.Text.Trim();
        if (typed == machine.Settings.NtfyTopic)
            return;
        string refused = Settings.TopicRefusal(typed);
        if (refused != null)
        {
            ntfyTopic.Text = machine.Settings.NtfyTopic;
            Warn("The ntfy topic '" + typed + "' is refused: " + refused + ".");
            return;
        }
        machine.Settings.NtfyTopic = typed;
        ApplySetting(null);
    }

    // As the topic box; a new URL is pinged at the next Watch tick the machine is running.
    void CommitHeartbeat()
    {
        string typed = heartbeatUrl.Text.Trim();
        if (typed == machine.Settings.HeartbeatUrl)
            return;
        string refused = Settings.HeartbeatRefusal(typed);
        if (refused != null)
        {
            heartbeatUrl.Text = machine.Settings.HeartbeatUrl;
            Warn("The heartbeat URL '" + typed + "' is refused: " + refused + ".");
            return;
        }
        machine.Settings.HeartbeatUrl = typed;
        pingedAt = DateTime.MinValue;
        ApplySetting(null);
    }

    // One push to the saved topic straight from Windows (https://docs.ntfy.sh/publish/: a POST
    // to ntfy.sh/<topic>, the body the message, `Title` a header), so a phone's subscription can
    // be checked without the machine. What came of it is shown either way.
    async void SendTestAlert()
    {
        CommitTopic();
        string topic = machine.Settings.NtfyTopic;
        if (topic.Length == 0)
        {
            Warn("Type an ntfy topic into the settings first; the test alert goes to it.");
            return;
        }
        string outcome = await Push(topic, "RaiGolmi test alert",
                                    "A test from RaiGolmi on " + Environment.MachineName +
                                    ": this phone gets its alerts.");
        if (outcome != null)
        {
            Warn("The test alert to '" + topic + "' did not go: " + outcome);
            return;
        }
        Note("test alert sent to the ntfy topic");
        MessageBox.Show(this, "A test alert went to '" + topic + "'. A phone subscribed to it in the " +
                        "ntfy app shows it now.", "RaiGolmi", MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    static readonly System.Net.Http.HttpClient Ntfy =
        new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(30) };

    // Saves the settings, then makes `apply` true of Windows now; either failing is shown.
    void ApplySetting(Action apply)
    {
        try
        {
            machine.Settings.Save();
            if (apply != null)
                apply();
        }
        catch (Exception e) when (e is LauncherError || e is IOException || e is UnauthorizedAccessException ||
                                  e is System.Security.SecurityException)
        {
            Warn("A setting could not be applied: " + e.Message);
        }
        Settings s = machine.Settings;
        Note("settings: start-at-login " + s.StartAtLogin + ", keep-awake " + s.KeepAwake +
             ", memory " + s.Memory + ", venus " + s.Venus +
             ", ntfy topic " + (s.NtfyTopic.Length > 0 ? "set" : "none") + ", keeper " + s.Keeper +
             ", heartbeat " + (s.HeartbeatUrl.Length > 0 ? "set" : "none"));
        CheckSettings();
    }

    // On only once Docker answers, asked off the window's thread; off at once. The image's first
    // build takes minutes, so compose runs on a thread of its own and says what it does in the log.
    void ToggleKeeper()
    {
        Settings s = machine.Settings;
        if (s.Keeper)
        {
            s.Keeper = false;
            Keeper retired = machine.Keeper;
            machine.Keeper = null;
            if (retired != null)
                retired.Retire();
            ApplySetting(s.ApplyKeeperDoor);
            Keeper.Compose("down", Note);
            return;
        }
        keeperChecking = true;
        CheckSettings();
        new Thread(() =>
        {
            string refused = Keeper.DockerRefusal();
            BeginInvoke(new Action(() => KeeperChecked(refused)));
        }) { IsBackground = true, Name = "docker check" }.Start();
    }

    void KeeperChecked(string refused)
    {
        keeperChecking = false;
        if (refused != null)
        {
            Warn(refused);
            CheckSettings();
            return;
        }
        try
        {
            machine.Keeper = new Keeper(recoveries) { Note = Note };
        }
        catch (Exception e) when (e is LauncherError || e is IOException || e is UnauthorizedAccessException)
        {
            Warn("The keeper could not be turned on: " + e.Message);
            CheckSettings();
            return;
        }
        machine.Settings.Keeper = true;
        ApplySetting(machine.Settings.ApplyKeeperDoor);
        TellKeeper();
        Keeper.Compose("up -d --build", Note);
    }

    // --- Windows ending the session ------------------------------------------------------
    //
    // Windows asks first (WM_QUERYENDSESSION) and the guest is asked to shut down then, so it
    // has every second Windows gives. The session is never refused: Windows' guidance is to
    // let it end and do the work in WM_ENDSESSION, with a reason registered for the screen it
    // shows while a program is still working (learn.microsoft.com, Shutdown Changes for
    // Windows Vista). The session ends once that message returns, and this process with it,
    // taking QEMU (Native.KillOnClose), so it returns only when QEMU has exited. A forced end
    // that does not wait (an update's restart can be one) cuts the guest off as a power loss
    // would; nothing a program does prevents that.

    void WindowsEnding()
    {
        Note("Windows is ending the session");
        windowsEnding = true;
        SavePlacement();
        if (exited)
            return;
        if (!Native.ShutdownBlockReasonCreate(Handle, "Shutting down the RaiGolmi machine so its disk is left whole"))
            Note("the reason for the wait could not be given to Windows: error " + Marshal.GetLastWin32Error());
        if (shutdownSent)
            return;
        try
        {
            AskGuestToShutDown();
        }
        catch (Exception ex)
        {
            Note("the shutdown request failed, so the guest ends with Windows: " + ex.Message);
        }
    }

    void WindowsEnded()
    {
        // A guest that could not be asked would never end on its own.
        if (exited || !shutdownSent)
            return;
        // The keyboard hook is this thread's, and a thread that is waiting delays every key
        // Windows delivers by the hook's timeout.
        keyboard.Dispose();
        Note("waiting for the guest to shut down before Windows ends the session");
        qemu.WaitForExit();
        Note("QEMU exited with code " + qemu.ExitCode + " before Windows ended the session");
    }

    // The end was cancelled after the guest was asked to shut down; it goes on shutting down
    // as after the close button.
    void WindowsStays()
    {
        Native.ShutdownBlockReasonDestroy(Handle);
        Note("Windows did not end the session after all");
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
            Tell("RaiGolmi's screen could not be connected, so the window stays black. What went " +
                 "wrong is in " + machine.Log + ":\n\n" + e.Message, MessageBoxIcon.Error);
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
        Tell("RaiGolmi's screen stopped: " + which + " to QEMU ended. Why is in " + machine.Log +
             ". Close the window to shut the machine down.", MessageBoxIcon.Error);
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
            unanswered = 0;
            // QEMU's run states for a guest it paused and that nothing resumes: its watchdog fired
            // (-action watchdog=pause); a disk write failed, which a full Windows disk does (the
            // -drive default werror=enospc pauses the guest); or the accelerator failed.
            using (var answer = System.Text.Json.JsonDocument.Parse(status))
            {
                string paused = answer.RootElement.GetProperty("return").GetProperty("status").GetString();
                if (paused == "running")
                    Heartbeat();
                else if (paused == "watchdog")
                    BeginInvoke(new Action(() => Recover("the guest's watchdog fired")));
                else if (paused == "io-error")
                    BeginInvoke(new Action(() => Recover("QEMU paused the guest on a disk write that failed " +
                                                         "(io-error; a full Windows disk is one cause)")));
                else if (paused == "internal-error")
                    BeginInvoke(new Action(() => Recover("QEMU paused the guest on an internal error " +
                                                         "(internal-error)")));
            }
        }
        catch (Exception e)
        {
            Note("could not ask QEMU how the guest is: " + e.Message);
            // QEMU answers its monitor from its main loop, so a monitor that stays silent is a
            // QEMU that has stopped running the guest: a Venus allocation the host GPU refused
            // has hung it with the process alive (docs/findings.md). Only a restart brings it back.
            if (++unanswered == HungAfter && lastStatus != null && !shutdownSent)
                BeginInvoke(new Action(() => Recover("QEMU stopped answering for " +
                                                     HungAfter * 10 + " seconds")));
        }
        try
        {
            SaveScreen();
        }
        catch (Exception e)
        {
            Note("could not write " + machine.Screen + ": " + e.Message);
        }
        try
        {
            TendKeeper();
        }
        catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
        {
            Note("keeper: the inbox could not be read: " + e.Message);
        }
        if (machine.Keeper != null)
            BeginInvoke(new Action(MindKeeper));
    }

    // --- the heartbeat: an outside dead-man's switch ------------------------------------------
    //
    // While the machine runs, the heartbeat URL (healthchecks.io) is pinged every HeartbeatEvery;
    // silence is the signal, so a PC that is off, a Windows nobody signed in to, a launcher gone,
    // restarting or given up all reach the owner's phone through healthchecks.io's own alert,
    // which nothing on this PC could send. A GET to a ping URL is a success signal
    // (https://healthchecks.io/docs/http_api/), answered 200 with the body "OK".

    // Well inside the check's period, so a ping lost on the way never alerts on its own; far
    // under healthchecks.io's five a minute, past which pings are dropped.
    static readonly TimeSpan HeartbeatEvery = TimeSpan.FromMinutes(5);
    DateTime pingedAt = DateTime.MinValue;
    int pinging;

    static readonly System.Net.Http.HttpClient Pinger =
        new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(30) };

    // From the Watch tick that found QEMU running the guest, off the window's thread; only a
    // machine this launcher is not stopping is healthy.
    void Heartbeat()
    {
        string url = machine.Settings.HeartbeatUrl;
        if (url.Length == 0 || recovering != null || shutdownSent || exited ||
            DateTime.UtcNow - pingedAt < HeartbeatEvery || Interlocked.Exchange(ref pinging, 1) == 1)
            return;
        pingedAt = DateTime.UtcNow;
        System.Threading.Tasks.Task.Run(async () =>
        {
            try
            {
                using (var answer = await Pinger.GetAsync(url))
                {
                    string body = (await answer.Content.ReadAsStringAsync()).Trim();
                    if (!answer.IsSuccessStatusCode || body != "OK")
                        Note("heartbeat: healthchecks.io answered " + (int)answer.StatusCode + " " +
                             answer.ReasonPhrase + ": " + body);
                }
            }
            catch (Exception e) when (e is System.Net.Http.HttpRequestException ||
                                      e is System.Threading.Tasks.TaskCanceledException)
            {
                Note("heartbeat: the ping could not reach " + new Uri(url).Host + ": " + e.Message);
            }
            finally
            {
                Interlocked.Exchange(ref pinging, 0);
            }
        });
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

    // --- the keeper: an agent outside the machine (Keeper) ---------------------------------

    // What the machine is doing, in launcher.json, from this launcher's own state.
    void TellKeeper()
    {
        Keeper keeper = machine.Keeper;
        if (keeper == null)
            return;
        if (upgrading)
            keeper.Set("upgrading", Upgrading);
        else if (recovering != null)
            keeper.Set("restarting", recovering);
        else if (lastStatus == null)
            keeper.Set("starting", null);
        else
            keeper.Set("running", null);
    }

    static readonly string Upgrading = "setup.ps1 -Unattended is upgrading the machine; its output is in " +
                                       Keeper.SetupLog;

    // Whether the keeper has been started again since it was last there.
    bool keeperRetried;
    bool keeperAlerting;
    // Docker Desktop starting at sign-in takes a few minutes, and compose up starts a built
    // container in seconds. A keeper not there this long after compose finished was not started by it.
    static readonly TimeSpan KeeperRetry = TimeSpan.FromMinutes(10);

    // On the window's thread, each Watch tick while the keeper setting is on: a keeper that is not
    // there (Docker Desktop not running, its container failing) is started again; one still not
    // there after that is the person's to fix, and they are told once, until it has been back.
    async void MindKeeper()
    {
        if (machine.Keeper == null || exited || keeperAlerting)
            return;
        string absence = Keeper.Absence();
        try
        {
            if (absence == null)
            {
                keeperRetried = false;
                if (Keeper.AbsentAlertSent)
                {
                    Keeper.AbsentAlertSent = false;
                    Note("keeper: it is there again");
                }
                return;
            }
            if (Keeper.ComposedWithin(KeeperRetry))
                return;
            if (keeperRetried && !Keeper.AbsentAlertSent)
            {
                keeperAlerting = true;
                bool taken = await Alert("RaiGolmi needs you", RestartThePc + " The keeper, the helper " +
                                         "outside RaiGolmi that repairs it, is not running: " + absence +
                                         ". Docker Desktop may not have started.");
                keeperAlerting = false;
                if (taken)
                    Keeper.AbsentAlertSent = true;
            }
        }
        catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
        {
            keeperAlerting = false;
            Note("keeper: whether its alert was sent could not be kept: " + e.Message);
        }
        if (machine.Keeper == null || exited)
            return;
        Note("keeper: not there (" + absence + "), so it is started again");
        keeperRetried = true;
        Keeper.Compose("up -d", Note);
    }

    // Each Watch tick: the state is written, and the oldest request is acted on, on the window's
    // thread, one at a time.
    void TendKeeper()
    {
        if (machine.Keeper == null)
            return;
        TellKeeper();
        if (requestQueued)
            return;
        Keeper.Request request = Keeper.Next();
        if (request == null)
            return;
        requestQueued = true;
        BeginInvoke(new Action(() =>
        {
            requestQueued = false;
            try
            {
                Act(request);
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                Note("keeper: " + Path.GetFileName(request.File) + " could not be acted on: " + e.Message);
            }
        }));
    }

    // A request is taken from the inbox once it is acted on, and what came of it is recorded.
    // One this launcher cannot act on because it is ending is left for the launcher after it.
    void Act(Keeper.Request request)
    {
        Keeper keeper = machine.Keeper;
        if (keeper == null || exited)
            return;
        if (request.Refused != null)
        {
            Note("keeper: refused " + Path.GetFileName(request.File) + ": " + request.Refused);
            Keeper.Take(request);
            keeper.Record(request.Kind ?? Path.GetFileName(request.File), "refused: " + request.Refused);
            return;
        }
        string asked = "the keeper asked" + (request.Why != null ? ": " + request.Why : "");
        Note("keeper: " + request.Kind + " (" + asked + ")");
        bool stopping = shutdownSent || recovering != null;
        switch (request.Kind)
        {
            case "start":
                Keeper.Take(request);
                keeper.Record("start", "nothing to do: this launcher is running the machine");
                break;
            case "restart":
                // Asked of the machine an earlier launcher ran: this one booted it since.
                if (request.Written < started)
                {
                    Keeper.Take(request);
                    keeper.Record("restart", "nothing to do: it was asked at " +
                                  request.Written.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'") +
                                  ", before this launcher started the machine");
                    break;
                }
                // A shutdown that never finishes holds the launcher; setup's own, while it still
                // waits on the window, is setup's to end.
                if (shutdownSent && recovering == null && !upgrading)
                {
                    Keeper.Take(request);
                    keeper.Record("restart", "the guest was asked to shut down and had not finished, " +
                                  "so it is powered off, and a new launcher starts it again");
                    Recover(asked + "; the guest had not finished shutting down", true);
                    break;
                }
                if (stopping)
                {
                    if (upgrading)
                        break;
                    Keeper.Take(request);
                    keeper.Record("restart", "nothing to do: the machine is already restarting");
                    break;
                }
                Keeper.Take(request);
                keeper.Record("restart", "the guest is shutting down, and a new launcher starts it again");
                RestartForKeeper(asked);
                break;
            case "upgrade":
                if (upgrading)
                {
                    Keeper.Take(request);
                    keeper.Record("upgrade", "nothing to do: setup is already upgrading the machine");
                    break;
                }
                if (stopping)
                    break;
                Keeper.Take(request);
                StartUpgrade();
                break;
        }
    }

    // setup.ps1 upgrades the machine in place while it runs, then closes this window
    // (Apply-Upgrade), which is a clean shutdown with nobody asked, and opens the new launcher.
    void StartUpgrade()
    {
        Keeper keeper = machine.Keeper;
        try
        {
            setup = Keeper.StartSetup();
        }
        catch (Exception e) when (e is LauncherError || e is System.ComponentModel.Win32Exception)
        {
            keeper.Record("upgrade", "refused: setup could not be started: " + e.Message);
            return;
        }
        upgrading = true;
        Note("keeper: " + Upgrading);
        keeper.Record("upgrade", "setup.ps1 -Unattended started; its output is in " + Keeper.SetupLog);
        TellKeeper();
        setup.EnableRaisingEvents = true;
        setup.Exited += delegate { BeginInvoke(new Action(SetupEnded)); };
    }

    // Setup ended with this launcher still running: the machine already ran the release, or
    // setup stopped before it closed the window.
    void SetupEnded()
    {
        if (exited)
            return;
        int code = setup.ExitCode;
        upgrading = false;
        Note("keeper: setup exited with code " + code);
        Keeper keeper = machine.Keeper;
        if (keeper == null)
            return;
        keeper.Record("upgrade", (code == 0 ? "setup finished with the machine still running" :
                                  "setup failed with code " + code) + "; its output is in " + Keeper.SetupLog);
        TellKeeper();
    }

    // A failure the user has to know about: in the log, and in front of them.
    public void Warn(string message)
    {
        Note(message);
        Tell(message, MessageBoxIcon.Warning);
    }

    // With the keeper on, a box is shown from a thread of its own, which has no active window
    // to own it (WinForms MessageBox takes GetActiveWindow's): it never disables this window,
    // which setup closes to upgrade and Process.CloseMainWindow refuses when disabled, and it
    // never keeps this process alive.
    void Tell(string message, MessageBoxIcon icon)
    {
        if (machine.Keeper == null)
        {
            BeginInvoke(new Action(() => MessageBox.Show(this, message, "RaiGolmi", MessageBoxButtons.OK, icon)));
            return;
        }
        var shown = new Thread(() => MessageBox.Show(message, "RaiGolmi", MessageBoxButtons.OK, icon))
            { IsBackground = true, Name = "box" };
        shown.SetApartmentState(ApartmentState.STA);
        shown.Start();
    }

    public void Note(string line)
    {
        Record(this, "launcher: " + line);
    }

    // QEMU's own lines. A renderer that fails to start is reported by QEMU, which keeps
    // running with a screen that stays black (hw/display/virtio-gpu-virgl.c
    // virtio_gpu_virgl_init), so that line is shown, not left in the log.
    void Record(object sender, DataReceivedEventArgs e)
    {
        if (e.Data == null)
            return;
        Record(sender, e.Data);
        if (e.Data.Contains("virgl could not be initialized"))
            Warn("QEMU could not start the machine's GPU, so the screen stays black. QEMU said:\n\n" +
                 e.Data + (machine.Settings.Venus ? "\n\n" + VenusRefused : ""));
    }

    const string VenusRefused = "The experimental Venus (Vulkan) setting is on, and this QEMU or its " +
        "virglrenderer may not support it. Turn it off under Settings in the menu of the window's " +
        "title-bar icon (or set venus=off in settings.ini), then start RaiGolmi again.";

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
                AskGuestToShutDown();
            }
            catch (Exception ex)
            {
                Note("the shutdown request failed: " + ex.Message);
                // Setup closes this window to upgrade and waits for it, with nobody to answer.
                if (upgrading)
                {
                    Note("powered off without asking: setup is upgrading the machine");
                    poweredOff = true;
                    qemu.Kill();
                }
                else
                    OfferPowerOff("RaiGolmi could not be asked to shut down:\n\n" + ex.Message);
            }
            return;
        }
        // A second close while the guest is still shutting down is the user deciding.
        OfferPowerOff("RaiGolmi was asked to shut down and has not finished.");
    }

    // As the machine's power button: the guest shuts itself down and QEMU then exits.
    void AskGuestToShutDown()
    {
        Display d = display;
        if (d != null)
            d.StopListening();
        Qmp.Execute(machine.ControlPort, "system_powerdown", null);
        shutdownSent = true;
        Note("asked the guest to shut down");
        ShowShuttingDown();
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
        // Closing it is a second close of the machine. Windows ending the session closes it
        // too, and that is answered by the window (WindowsEnding), never refused here.
        dialog.FormClosing += (_, e) =>
        {
            if (exited || e.CloseReason == CloseReason.WindowsShutDown)
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
            poweredOff = true;
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
        // A QEMU that ends on its own after the guest was running is restarted rather than
        // reported to a screen nobody may be at, and so is a restart's QEMU that fails to start,
        // which counts toward the restarts in a row. A first start that never ran the guest
        // fails the same way again, and is reported.
        string why = recovering;
        if (why == null && qemu.ExitCode != 0 && !poweredOff && !shutdownSent &&
            (lastStatus != null || recoveries > 0))
            why = "QEMU exited with code " + qemu.ExitCode;
        if (why != null)
        {
            Note("stopped: " + why);
            if (!RestartAfter(why))
                GiveUp(why);
            return;
        }
        ReportExit();
    }

    // The restarts in a row are used up: the one alert, sent before anything can end this
    // process, because a machine that cannot restart itself is what needs the owner. With the
    // keeper there, the keeper is told instead, and alerts the owner itself if it cannot repair it.
    async void GiveUp(string why)
    {
        Note("not restarting: " + MostRecoveries + " restarts in a row each ended within " +
             StableRun.TotalMinutes + " minutes");
        string said = why + ", after " + MostRecoveries + " restarts in a row. It is not restarting " +
                      "again; its log is " + machine.Log + ".";
        Keeper keeper = machine.Keeper;
        if (keeper != null)
            keeper.Final("gave_up", said);
        if (keeper == null || !Keeper.Present())
            await Alert("RaiGolmi needs you", RestartThePc + " RaiGolmi stopped and could not start " +
                        "itself again. For the owner: " + said);
        ReportExit(true);
    }

    // The one physical thing a person near the machine can do, said first in every alert.
    public const string RestartThePc = "Please restart the computer: Start, Power, Restart.";

    // How the machine ended, for whoever is there. With the keeper on, only a close the user made
    // is shown to them: anything else ends with nobody asked, because a box would keep this
    // process, and the mutex the door needs, until somebody came. The keeper reads it instead,
    // and with no keeper there, the phone is told. `alerted`: GiveUp has told it already.
    async void ReportExit(bool alerted = false)
    {
        bool onPurpose = recovering == null && (shutdownSent || poweredOff || qemu.ExitCode == 0);
        string log;
        lock (stderr) log = stderr.ToString();
        bool memoryRefused = log.Contains(MemoryMapRefused) && machine.Settings.Memory != Settings.DefaultMemory;
        string said = memoryRefused ? MemoryRefusedSaid() : Explain(log);
        Keeper keeper = machine.Keeper;
        // A session Windows ends leaves launcher.json as it was, so the door starts the machine
        // again once the user is back.
        if (keeper != null && !windowsEnding)
        {
            string ended = upgrading ? Upgrading : qemu.ExitCode == 0 ? null :
                           (said != null ? said + " " : "") + "QEMU exited with code " + qemu.ExitCode +
                           "; its output is in " + machine.Log + ".";
            keeper.Final(upgrading ? "upgrading" : onPurpose ? "closed" : "gave_up", ended);
            if (!upgrading && !onPurpose && !alerted && !Keeper.Present())
                await Alert("RaiGolmi needs you", RestartThePc + " RaiGolmi stopped and could not start " +
                            "itself again. For the owner: " + ended);
        }
        if (qemu.ExitCode != 0 && (keeper == null || (onPurpose && !upgrading)))
        {
            string tail = log.Length > 3000 ? log.Substring(log.Length - 3000) : log;
            string exit = "QEMU exited with code " + qemu.ExitCode + ". Its output is in " +
                          machine.Log + ":\n\n" + tail;
            if (memoryRefused)
                OfferDefaultMemory(exit);
            else
                Launcher.Fail((said != null ? said + "\n\n" : "") + exit);
        }
        Close();
    }

    // Checks of QEMU's monitor in a row that go unanswered before QEMU is taken to be hung; at
    // one every ten seconds, long past anything a running QEMU holds its main loop for.
    const int HungAfter = 12;
    // Restarts in a row before the launcher stops and says so: each run shorter than StableRun
    // counts toward it, and one that lasted longer starts the count again.
    const int MostRecoveries = 5;
    static readonly TimeSpan StableRun = TimeSpan.FromMinutes(30);

    // On the UI thread: QEMU is killed, and OnQemuExited restarts the machine. A guest already
    // asked to shut down is left to finish, unless `overShutdown` (the keeper's restart, Act).
    void Recover(string why, bool overShutdown = false)
    {
        if (recovering != null || exited || (shutdownSent && !overShutdown))
            return;
        recovering = why;
        Note("recovering: " + why);
        TellKeeper();
        qemu.Kill();
    }

    // A restart the keeper asked for: the guest shuts down cleanly, and OnQemuExited restarts the
    // machine through the same counted path as a recovery, so restarts that never settle end at
    // GiveUp whoever asks for them. A guest still up after KeeperShutdown is wedged, which is
    // why a restart was asked for, and is powered off.
    void RestartForKeeper(string why)
    {
        recovering = why;
        Note("recovering: " + why);
        TellKeeper();
        try
        {
            AskGuestToShutDown();
        }
        catch (Exception e)
        {
            Note("the shutdown request failed, so the guest is powered off: " + e.Message);
            qemu.Kill();
            return;
        }
        var deadline = new System.Windows.Forms.Timer { Interval = (int)KeeperShutdown.TotalMilliseconds };
        deadline.Tick += delegate
        {
            deadline.Dispose();
            if (exited)
                return;
            Note("the guest did not shut down within " + KeeperShutdown.TotalMinutes + " minutes, so it is powered off");
            qemu.Kill();
        };
        deadline.Start();
    }

    static readonly TimeSpan KeeperShutdown = TimeSpan.FromMinutes(2);

    // Starts a fresh launcher for the same machine and closes this one, with no alert: a machine
    // that restarts itself needs nobody. False when the restarts in a row are used up.
    bool RestartAfter(string why)
    {
        int next = DateTime.UtcNow - started >= StableRun ? 1 : recoveries + 1;
        if (next > MostRecoveries)
            return false;
        Note("restarting the machine (" + next + " in a row)");
        if (machine.Keeper != null)
            machine.Keeper.Final("restarting", why);
        Process.Start(Environment.ProcessPath, Launcher.RecoverFlag + " " + next);
        Close();
        return true;
    }

    // A push to the ntfy topic when one is set, its outcome in the log; never raised, because
    // what it reports goes ahead either way. False only when a push was not taken, so a caller
    // that tells once can try again; with no topic set there is nobody to tell.
    async System.Threading.Tasks.Task<bool> Alert(string title, string message)
    {
        string topic = machine.Settings.NtfyTopic;
        if (topic.Length == 0)
        {
            Note("alert: " + title + ": not sent, no ntfy topic is set: " + message);
            return true;
        }
        string failed = await Push(topic, title, message + " (" + Environment.MachineName + ")");
        Note("alert: " + title + ": " + (failed ?? "sent"));
        return failed == null;
    }

    // What came of one POST to ntfy.sh/<topic>, or null when it was taken.
    public static async System.Threading.Tasks.Task<string> Push(string topic, string title, string message)
    {
        try
        {
            using (var request = new System.Net.Http.HttpRequestMessage(
                       System.Net.Http.HttpMethod.Post, "https://ntfy.sh/" + topic))
            {
                request.Content = new System.Net.Http.StringContent(message, Encoding.UTF8);
                request.Headers.Add("Title", title);
                using (var answer = await Ntfy.SendAsync(request))
                {
                    string body = await answer.Content.ReadAsStringAsync();
                    return answer.IsSuccessStatusCode ? null :
                        "ntfy.sh answered " + (int)answer.StatusCode + " " + answer.ReasonPhrase + ": " + body.Trim();
                }
            }
        }
        catch (Exception e) when (e is System.Net.Http.HttpRequestException ||
                                  e is System.Threading.Tasks.TaskCanceledException)
        {
            return "it could not reach ntfy.sh: " + e.Message;
        }
    }

    // What QEMU prints when WHvMapGpaRange refuses the guest's RAM; the HRESULT is not printed
    // (accel/whpx/whpx-common.c, whpx_set_phys_mem), so the setting is the one thing to name.
    const string MemoryMapRefused = "WHPX: failed to map GPA range";

    string MemoryRefusedSaid()
    {
        return "Windows' hypervisor refused the machine's memory setting, " + machine.Settings.Memory +
               " GB (" + machine.MemoryMiB + " MiB), so the machine could not start.";
    }

    // The memory setting goes back to 8 GB only on the user's Yes.
    void OfferDefaultMemory(string exit)
    {
        Settings s = machine.Settings;
        DialogResult answer = MessageBox.Show(
            MemoryRefusedSaid() + " Set memory back to " + Settings.DefaultMemory + " GB?\n\n" + exit,
            "RaiGolmi", MessageBoxButtons.YesNo, MessageBoxIcon.Error);
        if (answer != DialogResult.Yes)
            return;
        s.Memory = Settings.DefaultMemory;
        try
        {
            s.Save();
        }
        catch (Exception e) when (e is IOException || e is UnauthorizedAccessException ||
                                  e is System.Security.SecurityException)
        {
            Launcher.Fail("Memory could not be set back to " + Settings.DefaultMemory + " GB: " +
                          e.Message + "\n\nSet memory=" + Settings.DefaultMemory + " in settings.ini.");
            return;
        }
        Note("settings: memory set back to " + s.Memory + " after the hypervisor refused it");
        MessageBox.Show("Memory is set back to " + Settings.DefaultMemory + " GB. Open RaiGolmi again.",
                        "RaiGolmi", MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    // The failures a stranger's PC meets, by what QEMU says of each
    // (target/i386/whpx/whpx-all.c, net/slirp.c, ui/egl-helpers.c, hw/display/virtio-gpu.c),
    // in one sentence each.
    string Explain(string log)
    {
        if (machine.Settings.Venus && (log.Contains("venus") || log.Contains("blob")))
            return VenusRefused;
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

// The keeper's half on Windows. The keeper is a Claude Code agent in a Docker Desktop container
// (keeper\compose.yml), outside the machine, that repairs what this launcher cannot. The launcher
// says how the machine is in keeper\outbox\launcher.json, written whole on every Watch tick and
// every change, and takes the keeper's requests from keeper\inbox\ (start, restart, upgrade); the
// door, a scheduled task running --keeper-door, starts a launcher when none runs and one should.
// Nothing here exists while the keeper setting is off.
sealed class Keeper
{
    static readonly string Folder = Path.Combine(Launcher.Project, "keeper");
    static readonly string Inbox = Path.Combine(Folder, "inbox");
    static readonly string Outbox = Path.Combine(Folder, "outbox");
    static readonly string StateFile = Path.Combine(Outbox, "launcher.json");
    public static readonly string SetupLog = Path.Combine(Outbox, "setup.log");
    static readonly string ComposeFile = Path.Combine(Folder, "compose.yml");
    const string Container = "raigolmi-keeper";

    readonly object gate = new object();
    readonly int recoveries;
    string state = "starting", why;
    // The last request acted on: request, outcome, at. A new launcher carries it on, so the
    // outcome of a restart or an upgrade is still there when the launcher after it writes.
    string[] last;
    // The launcher has said how it ended; nothing after it says otherwise.
    bool final;
    // Where a write that failed is said: the launcher log.
    public Action<string> Note = Launcher.Log;

    public Keeper(int recoveries)
    {
        this.recoveries = recoveries;
        Directory.CreateDirectory(Inbox);
        Directory.CreateDirectory(Outbox);
        last = Read().LastRequest;
    }

    public void Set(string state, string why)
    {
        lock (gate)
        {
            if (final)
                return;
            this.state = state;
            this.why = why;
            Write();
        }
    }

    // How this launcher ended; a null state keeps the one it had.
    public void Final(string state, string why)
    {
        lock (gate)
        {
            if (final)
                return;
            this.state = state ?? this.state;
            this.why = why;
            Write();
            final = true;
        }
    }

    public void Record(string request, string outcome)
    {
        lock (gate)
        {
            last = new[] { request, outcome, Now() };
            Write();
        }
    }

    // The setting turned off: no keeper, no file.
    public void Retire()
    {
        lock (gate)
        {
            final = true;
            File.Delete(StateFile);
        }
    }

    // Whole or not at all, so a reader never meets half of it.
    void Write()
    {
        var buffer = new MemoryStream();
        using (var json = new Utf8JsonWriter(buffer, new JsonWriterOptions { Indented = true }))
        {
            json.WriteStartObject();
            json.WriteString("state", state);
            json.WriteNumber("pid", Environment.ProcessId);
            json.WriteString("at", Now());
            if (why == null)
                json.WriteNull("why");
            else
                json.WriteString("why", why);
            json.WriteNumber("recoveries", recoveries);
            if (last == null)
                json.WriteNull("last_request");
            else
            {
                json.WriteStartObject("last_request");
                json.WriteString("request", last[0]);
                json.WriteString("outcome", last[1]);
                json.WriteString("at", last[2]);
                json.WriteEndObject();
            }
            json.WriteEndObject();
        }
        string partial = StateFile + ".partial";
        try
        {
            File.WriteAllBytes(partial, buffer.ToArray());
            File.Move(partial, StateFile, true);
        }
        catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
        {
            Note("keeper: " + StateFile + " could not be written: " + e.Message);
        }
    }

    static string Now() { return DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'"); }

    // launcher.json is there: it exists only while the keeper setting is on.
    public static bool Wrote() { return File.Exists(StateFile); }

    // --- whether the keeper is there -------------------------------------------------------------

    // The keeper writes keeper.json (state, `at` in ISO UTC) at the end of each check, one a
    // minute (keeper.py CHECK_SECONDS), a check itself taking up to a few minutes when ssh, the
    // VM's answer and ntfy.sh each wait out their timeouts. Five minutes is several of its writes
    // missed: a keeper that is gone, not one that is slow. A keeper in a session ("repairing")
    // writes nothing until the session ends, which it bounds at four hours (SESSION_SECONDS).
    static readonly string KeeperFile = Path.Combine(Outbox, "keeper.json");
    static readonly TimeSpan PresentWithin = TimeSpan.FromMinutes(5);
    static readonly TimeSpan RepairingWithin = TimeSpan.FromHours(4) + PresentWithin;

    // keeper.json is the keeper's; one this launcher cannot read says no keeper is there, and why.
    public static bool Present() { return Absence() == null; }

    // Why the keeper is not there, or null when it is.
    public static string Absence()
    {
        if (!File.Exists(KeeperFile))
            return KeeperFile + " does not exist";
        string state;
        DateTime at;
        try
        {
            using (var doc = JsonDocument.Parse(File.ReadAllText(KeeperFile)))
            {
                state = doc.RootElement.GetProperty("state").GetString();
                at = DateTime.ParseExact(doc.RootElement.GetProperty("at").GetString(), "yyyy-MM-dd'T'HH:mm:ss'Z'",
                                         System.Globalization.CultureInfo.InvariantCulture,
                                         System.Globalization.DateTimeStyles.AdjustToUniversal |
                                         System.Globalization.DateTimeStyles.AssumeUniversal);
            }
        }
        catch (Exception e) when (e is IOException || e is UnauthorizedAccessException || e is JsonException ||
                                  e is KeyNotFoundException || e is InvalidOperationException ||
                                  e is FormatException || e is ArgumentNullException)
        {
            return KeeperFile + " could not be read: " + e.Message;
        }
        TimeSpan age = DateTime.UtcNow - at;
        if (age <= (state == "repairing" ? RepairingWithin : PresentWithin))
            return null;
        return KeeperFile + " was last written " + (int)age.TotalMinutes + " minutes ago (" + state + ")";
    }

    // The alert that the keeper is gone was sent, and it has not been back since: kept beside the
    // settings, so the launchers that follow this one do not send it again.
    static readonly string AbsentAlerted = Path.Combine(Machine.Home, "keeper-gone-alerted");

    public static bool AbsentAlertSent
    {
        get { return File.Exists(AbsentAlerted); }
        set
        {
            if (value)
                File.WriteAllText(AbsentAlerted, Now());
            else
                File.Delete(AbsentAlerted);
        }
    }

    sealed class Written { public string State; public int Pid; public string[] LastRequest; }

    // launcher.json is this program's own; one it cannot read is a defect.
    static Written Read()
    {
        var w = new Written();
        if (!File.Exists(StateFile))
            return w;
        try
        {
            using (var doc = JsonDocument.Parse(File.ReadAllText(StateFile)))
            {
                JsonElement root = doc.RootElement;
                w.State = root.GetProperty("state").GetString();
                w.Pid = root.GetProperty("pid").GetInt32();
                JsonElement request = root.GetProperty("last_request");
                if (request.ValueKind == JsonValueKind.Object)
                    w.LastRequest = new[] { request.GetProperty("request").GetString(),
                                            request.GetProperty("outcome").GetString(),
                                            request.GetProperty("at").GetString() };
            }
        }
        catch (Exception e) when (e is JsonException || e is KeyNotFoundException || e is InvalidOperationException ||
                                  e is FormatException)
        {
            throw new LauncherError(StateFile + " is not what the launcher writes (" + e.Message + "). " +
                                    "Delete it, and the launcher writes it again.");
        }
        return w;
    }

    // --- the inbox ---------------------------------------------------------------------------

    public sealed class Request
    {
        public string File, Kind, Why;
        // When the file was written (UTC).
        public DateTime Written;
        // Why this is no request, or null when it is one.
        public string Refused;
    }

    static readonly string[] Kinds = { "start", "restart", "upgrade" };

    // The oldest request, or null when there is none.
    public static Request Next()
    {
        foreach (Request r in Requests())
            return r;
        return null;
    }

    static List<Request> Requests()
    {
        Directory.CreateDirectory(Inbox);
        FileInfo[] files = new DirectoryInfo(Inbox).GetFiles("*.json");
        Array.Sort(files, (a, b) => a.LastWriteTimeUtc.CompareTo(b.LastWriteTimeUtc));
        var requests = new List<Request>();
        foreach (FileInfo f in files)
        {
            Request r = Parse(f);
            if (r != null)
                requests.Add(r);
        }
        return requests;
    }

    // Null for a file the keeper may still be writing: one being written cannot be opened, and
    // one that is not JSON yet is refused only once it is older than a Watch tick.
    static Request Parse(FileInfo file)
    {
        var r = new Request { File = file.FullName, Written = file.LastWriteTimeUtc };
        string text;
        try
        {
            text = File.ReadAllText(file.FullName);
        }
        catch (IOException)
        {
            return null;
        }
        try
        {
            using (var doc = JsonDocument.Parse(text))
            {
                JsonElement root = doc.RootElement, kind, why;
                if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("request", out kind) ||
                    kind.ValueKind != JsonValueKind.String)
                {
                    r.Refused = "it names no request";
                    return r;
                }
                r.Kind = kind.GetString();
                if (Array.IndexOf(Kinds, r.Kind) < 0)
                    r.Refused = r.Kind + " is not one of " + string.Join(", ", Kinds);
                if (root.TryGetProperty("why", out why) && why.ValueKind == JsonValueKind.String)
                    r.Why = why.GetString();
            }
        }
        catch (JsonException e)
        {
            if (DateTime.UtcNow - file.LastWriteTimeUtc < TimeSpan.FromSeconds(10))
                return null;
            r.Refused = "it is not JSON: " + e.Message;
        }
        return r;
    }

    public static void Take(Request r)
    {
        File.Delete(r.File);
    }

    // --- the door ----------------------------------------------------------------------------

    // Why the door starts a launcher, or null when it leaves things as they are. Asked holding
    // the launcher's mutex, so no launcher runs: a start or an upgrade request, or a launcher.json
    // that says the machine should be running while the launcher that wrote it is gone (it
    // crashed, or Windows restarted under it). A start is taken here; an upgrade is left for the
    // launcher the door starts.
    public static string DoorReason(out bool tookStart)
    {
        tookStart = false;
        string reason = null;
        foreach (Request r in Requests())
        {
            if (r.Refused != null)
                continue;
            if (r.Kind == "start" && !tookStart)
            {
                Take(r);
                tookStart = true;
                reason = "the keeper asked" + (r.Why != null ? ": " + r.Why : "");
            }
            else if (r.Kind == "upgrade" && reason == null)
                reason = "the keeper asked for an upgrade";
        }
        if (reason != null)
            return reason;
        Written w = Read();
        if ((w.State == "running" || w.State == "restarting" || w.State == "starting") && !Alive(w.Pid))
            return "launcher.json says " + w.State + ", and the launcher that wrote it (" + w.Pid + ") is gone";
        return null;
    }

    static bool Alive(int pid)
    {
        if (pid == Environment.ProcessId)
            return false;
        try
        {
            using (Process p = Process.GetProcessById(pid))
                return p.ProcessName == Process.GetCurrentProcess().ProcessName;
        }
        catch (ArgumentException)
        {
            return false;     // no process has that id
        }
    }

    // --- Docker --------------------------------------------------------------------------------

    // Why Docker cannot run the keeper, in plain words, or null when it can. `docker version`
    // asks the engine as well as the command, so it fails while Docker Desktop is not running.
    public static string DockerRefusal()
    {
        Ssh.Result r;
        try
        {
            r = Ssh.Finish(Process.Start(Docker("version")));
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return "The keeper needs Docker Desktop, and it is not installed (Windows has no docker " +
                   "command). Install Docker Desktop from docker.com, start it, set it to start when " +
                   "you sign in, then turn the keeper on again.";
        }
        if (r.Code == 0)
            return null;
        return "The keeper needs Docker Desktop running, and it is not answering. Start Docker " +
               "Desktop, set it to start when you sign in, then turn the keeper on again.\n\n" +
               "docker version said: " + (r.Err.Length > 0 ? r.Err : r.Out.Trim());
    }

    static ProcessStartInfo Docker(string arguments)
    {
        var info = new ProcessStartInfo("docker", arguments);
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        info.StandardOutputEncoding = Encoding.UTF8;
        info.StandardErrorEncoding = Encoding.UTF8;
        return info;
    }

    // Compose runs now, and when the last one finished.
    static int composing;
    static DateTime composed;

    // A compose is running, or one finished within `span`.
    public static bool ComposedWithin(TimeSpan span)
    {
        return Volatile.Read(ref composing) > 0 || DateTime.UtcNow - composed < span;
    }

    // docker compose on the keeper's compose.yml, on a thread of its own: the image's first build
    // takes minutes. What it prints goes to `note`, and how it ended.
    public static void Compose(string arguments, Action<string> note)
    {
        string command = "compose -f " + Ssh.Quote(ComposeFile) + " " + arguments;
        Interlocked.Increment(ref composing);
        new Thread(() =>
        {
            note("keeper: docker " + command);
            try
            {
                using (var p = new Process { StartInfo = Docker(command) })
                {
                    DataReceivedEventHandler said = (sender, e) =>
                    {
                        if (e.Data != null)
                            note("keeper: " + e.Data);
                    };
                    p.OutputDataReceived += said;
                    p.ErrorDataReceived += said;
                    p.Start();
                    p.BeginOutputReadLine();
                    p.BeginErrorReadLine();
                    p.WaitForExit();
                    note("keeper: docker compose " + arguments + " exited with code " + p.ExitCode);
                }
            }
            catch (System.ComponentModel.Win32Exception e)
            {
                note("keeper: docker could not be run, so Docker Desktop may not be installed: " + e.Message);
            }
            finally
            {
                composed = DateTime.UtcNow;
                Interlocked.Decrement(ref composing);
            }
        }) { IsBackground = true, Name = "keeper compose" }.Start();
    }

    // A console of its own on the keeper's container, as ClaudeCode's window.bat opens one: where
    // the owner signs the agent in.
    public static void OpenTerminal()
    {
        Process.Start(new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "cmd.exe"),
                                           "/k docker exec -it " + Container + " bash") { UseShellExecute = true });
    }

    // --- the unattended upgrade ------------------------------------------------------------------

    // setup.ps1 with nothing in it waiting on a person, in Windows PowerShell as setup.bat runs
    // it. Through cmd, whose redirection makes the log the process's own output, the native
    // programs' included, and outlives this launcher, which setup closes on the way.
    public static Process StartSetup()
    {
        string script = Path.Combine(Launcher.Project, "setup.ps1");
        if (!File.Exists(script))
            throw new LauncherError(script + " does not exist.");
        string powershell = Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe");
        // /s: cmd drops the first and the last quote and runs what is between them as it is.
        var info = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "cmd.exe"),
            "/s /c \"" + Ssh.Quote(powershell) + " -NoProfile -ExecutionPolicy Bypass -File " + Ssh.Quote(script) +
            " -Unattended > " + Ssh.Quote(SetupLog) + " 2>&1\"");
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.WorkingDirectory = Launcher.Project;
        return Process.Start(info);
    }
}

// The keyboard is the guest's while this window is active, its settings drop-down closed and
// the pointer over it, and Windows' otherwise — Super, Alt+Tab and all. A low-level hook
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
        if (code == Native.HC_ACTION && hovering && window.KeysToGuest)
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

    // Twice when Windows ends the session: once before waiting for QEMU, once at its exit.
    bool unhooked;

    public void Dispose()
    {
        if (unhooked)
            return;
        unhooked = true;
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
        OwnerOnly(s.Key);
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
        OwnerOnly(s.Config);
        return s;
    }

    // Windows' OpenSSH refuses a private key, and a config file, that anyone but the user,
    // SYSTEM and Administrators can reach; the state folder sits in the project folder, whose
    // drive may give every signed-in user access, and a copied folder brings its drive's
    // inheritance. So each start sets the file's ACL to exactly those three, inheriting nothing.
    // Only the DACL is written: its owner may always write it, while setting the owner needs
    // WRITE_OWNER, which ssh-keygen's own ACL on the key (the user at Modify) does not grant.
    static void OwnerOnly(string path)
    {
        var acl = new FileSecurity();
        acl.SetAccessRuleProtection(true, false);
        foreach (var who in new[] { WindowsIdentity.GetCurrent().User,
                                    new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
                                    new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null) })
            acl.AddAccessRule(new FileSystemAccessRule(who, FileSystemRights.FullControl, AccessControlType.Allow));
        try
        {
            new FileInfo(path).SetAccessControl(acl);
        }
        catch (UnauthorizedAccessException e)
        {
            throw new LauncherError("Windows refused to limit " + path + " to this user, SYSTEM and " +
                                    "Administrators (" + e.Message + "), so OpenSSH would refuse it. " +
                                    "It belongs to someone else: delete it and start RaiGolmi again.");
        }
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
    // on a button), and it must paste what was copied on Windows. The type is named: left to
    // sniff the content, wl-copy offers some text as application/octet-stream alone, which
    // every `wl-paste --type text` here, the AI terminal's right-click among them, cannot take.
    const string Feed = Session + @"
echo 'ready for what is copied on Windows' >&2
while IFS= read -r line; do
    if printf %s ""$line"" | base64 -d | wl-copy --type 'text/plain;charset=utf-8' && printf %s ""$line"" | base64 -d | wl-copy --primary --type 'text/plain;charset=utf-8'; then
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
            // Windows holds something else now, so the guest's next copy is news even when it
            // repeats the last one.
            fromGuest = null;
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
    public const int WM_QUERYENDSESSION = 0x0011, WM_ENDSESSION = 0x0016, WM_SYSCOMMAND = 0x0112;
    public const uint MF_STRING = 0x0, MF_SEPARATOR = 0x800;
    [DllImport("user32.dll")] public static extern IntPtr GetSystemMenu(IntPtr hwnd, bool revert);
    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool AppendMenu(IntPtr menu, uint flags, UIntPtr id, string text);
    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool ShutdownBlockReasonCreate(IntPtr hwnd, string reason);
    [DllImport("user32.dll")] public static extern bool ShutdownBlockReasonDestroy(IntPtr hwnd);
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
