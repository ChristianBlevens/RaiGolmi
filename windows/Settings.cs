// The launcher's own settings: how Windows treats RaiGolmi and what QEMU is started with. They
// live in settings.ini in the project folder's state\ (Machine.Home) as `key=value` lines and
// are read once at start; only the ntfy topic reaches inside the OS, handed in at boot. Every
// one is off, or the launcher's long-standing value, until it is chosen, from the window's
// title-bar menu or by editing the file (the keeper's sessions may, keeper\KEEPER.md).
//
//   start-at-login = on | off     the launcher in HKCU\...\Run, so Windows starts it at sign-in
//   keep-awake     = on | off     Windows does not sleep while the launcher runs
//   memory         = 8 | 12 | 16 | 24     the machine's RAM in GB, at its next start
//   disk           = 128 | 200 | 256 | 512 | 1024   the machine's disk in GB, grown to it at its
//                                 next start and never shrunk; empty is the size it was built
//   venus          = on | off     the GPU offers Vulkan (Venus), at its next start
//   ntfy-topic     = <topic>      the phone alerts' ntfy topic, at the machine's next start;
//                                 empty is no alerts (raigolmid/raigolmid/lineout.py)
//   heartbeat-url  = <https URL>  the outside dead-man's switch's ping URL (healthchecks.io);
//                                 empty is none
//   keeper         = on | off     an agent in Docker Desktop, outside the machine, repairs what
//                                 the launcher cannot (Keeper in RaiGolmi.cs, keeper\)
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Windows.Forms;
using Microsoft.Win32;

sealed class Settings
{
    public bool StartAtLogin, KeepAwake, Venus, Keeper;
    public string Memory = DefaultMemory;
    // Empty leaves the disk the size it was built.
    public string Disk = "";
    public string NtfyTopic = "";
    // The outside dead-man's switch's ping URL (healthchecks.io); empty is none.
    public string HeartbeatUrl = "";
    readonly string path;

    public const string DefaultMemory = "8";
    public static readonly string[] MemoryChoices = { "8", "12", "16", "24" };
    // What Windows keeps for itself whatever the machine is given.
    const long HostReserveMiB = 4096;
    public static readonly string[] DiskChoices = { "128", "200", "256", "512", "1024" };
    // What the disk's drive keeps free were the machine to fill every byte it is grown to.
    const long DriveReserveBytes = 32 * GiB;
    const long GiB = 1L << 30;

    Settings(string path) { this.path = path; }

    public static Settings Load(string home)
    {
        var s = new Settings(Path.Combine(home, "settings.ini"));
        if (!File.Exists(s.path))
            return s;
        var seen = new HashSet<string>();
        int number = 0;
        foreach (string raw in File.ReadAllLines(s.path))
        {
            number++;
            string line = raw.Trim();
            if (line.Length == 0 || line.StartsWith(";") || line.StartsWith("#"))
                continue;
            int eq = line.IndexOf('=');
            if (eq < 0)
                throw s.Bad(number, "is not key=value");
            string key = line.Substring(0, eq).Trim(), value = line.Substring(eq + 1).Trim();
            if (!seen.Add(key))
                throw s.Bad(number, "sets " + key + " a second time");
            switch (key)
            {
                case "start-at-login": s.StartAtLogin = s.Switch(number, value); break;
                case "keep-awake": s.KeepAwake = s.Switch(number, value); break;
                case "venus": s.Venus = s.Switch(number, value); break;
                case "keeper": s.Keeper = s.Switch(number, value); break;
                case "memory":
                    if (Array.IndexOf(MemoryChoices, value) < 0)
                        throw s.Bad(number, "gives memory as " + value + "; it is one of " +
                                            string.Join(", ", MemoryChoices));
                    s.Memory = value;
                    break;
                case "disk":
                    if (value.Length > 0 && Array.IndexOf(DiskChoices, value) < 0)
                        throw s.Bad(number, "gives disk as " + value + "; it is empty or one of " +
                                            string.Join(", ", DiskChoices));
                    s.Disk = value;
                    break;
                case "ntfy-topic":
                    string refused = TopicRefusal(value);
                    if (refused != null)
                        throw s.Bad(number, "gives ntfy-topic as " + value + ": " + refused);
                    s.NtfyTopic = value;
                    break;
                case "heartbeat-url":
                    string wrong = HeartbeatRefusal(value);
                    if (wrong != null)
                        throw s.Bad(number, "gives heartbeat-url as " + value + ": " + wrong);
                    s.HeartbeatUrl = value;
                    break;
                default:
                    throw s.Bad(number, "names " + key + ", which is not a setting");
            }
        }
        return s;
    }

    bool Switch(int number, string value)
    {
        if (value == "on") return true;
        if (value == "off") return false;
        throw Bad(number, "is " + value + " where on or off belongs");
    }

    LauncherError Bad(int number, string what)
    {
        return new LauncherError(path + " line " + number + " " + what + ". Fix it, or delete the " +
                                 "file and every setting goes back to off.");
    }

    public void Save()
    {
        Durable.Write(path,
            "start-at-login=" + OnOff(StartAtLogin) + Environment.NewLine +
            "keep-awake=" + OnOff(KeepAwake) + Environment.NewLine +
            "memory=" + Memory + Environment.NewLine +
            "disk=" + Disk + Environment.NewLine +
            "venus=" + OnOff(Venus) + Environment.NewLine +
            "ntfy-topic=" + NtfyTopic + Environment.NewLine +
            "heartbeat-url=" + HeartbeatUrl + Environment.NewLine +
            "keeper=" + OnOff(Keeper) + Environment.NewLine);
    }

    static string OnOff(bool on) { return on ? "on" : "off"; }

    // --- the phone alerts' topic ---------------------------------------------------------

    // ntfy's rule for a topic name (https://docs.ntfy.sh/publish/: letters, numbers, underscores
    // and dashes, up to 64 characters); empty is no alerts. The guest checks the same
    // (raigolmid/raigolmid/lineout.py TOPIC).
    static readonly Regex Topic = new Regex(@"\A[A-Za-z0-9_-]{0,64}\z");

    // Why `topic` is no ntfy topic, or null when it is one or is empty.
    public static string TopicRefusal(string topic)
    {
        if (Topic.IsMatch(topic))
            return null;
        return "an ntfy topic is letters, digits, _ and - only, at most 64 of them";
    }

    // --- the heartbeat -------------------------------------------------------------------

    // Why `url` is no heartbeat URL, or null when it is one or is empty. It is an https URL, as
    // healthchecks.io gives its ping URLs (https://hc-ping.com/<uuid>).
    public static string HeartbeatRefusal(string url)
    {
        if (url.Length == 0)
            return null;
        Uri uri;
        if (!Uri.TryCreate(url, UriKind.Absolute, out uri) || uri.Scheme != Uri.UriSchemeHttps ||
            url.IndexOfAny(new[] { ' ', '\t' }) >= 0)
            return "a heartbeat URL is an https:// address, as healthchecks.io gives it";
        return null;
    }

    // --- memory ------------------------------------------------------------------------

    // The machine's RAM for `-m`, in MiB: a whole number of GiB. The long-standing 8 GB is
    // given on any PC; a larger choice must leave Windows its reserve, and one that does not is
    // refused, never quietly made smaller.
    public static int MemoryMiB(string choice)
    {
        string refused = Refusal(choice);
        if (refused != null)
            throw new LauncherError(refused);
        return ChoiceMiB(choice);
    }

    static int ChoiceMiB(string choice) { return int.Parse(choice) * 1024; }

    // Why this PC cannot give the machine `choice`, or null when it can.
    public static string Refusal(string choice)
    {
        if (choice == DefaultMemory)
            return null;
        long host = HostMiB();
        if (ChoiceMiB(choice) <= host - HostReserveMiB)
            return null;
        return "The machine cannot have " + choice + " GB of memory: this PC has " + Gb(host) +
               " GB and Windows keeps " + Gb(HostReserveMiB) + " GB of it. Choose less in the " +
               "settings menu, or set memory=8 in settings.ini.";
    }

    static string Gb(long mib) { return (mib / 1024.0).ToString("0.#"); }

    public static long HostMiB()
    {
        var status = new MEMORYSTATUSEX { dwLength = (uint)Marshal.SizeOf<MEMORYSTATUSEX>() };
        if (!GlobalMemoryStatusEx(ref status))
            throw new LauncherError("Windows would not say how much memory this PC has " +
                                    "(GlobalMemoryStatusEx: error " + Marshal.GetLastWin32Error() + ").");
        return (long)(status.ullTotalPhys / (1024 * 1024));
    }

    [StructLayout(LayoutKind.Sequential)]
    struct MEMORYSTATUSEX
    {
        public uint dwLength, dwMemoryLoad;
        public ulong ullTotalPhys, ullAvailPhys, ullTotalPageFile, ullAvailPageFile,
                     ullTotalVirtual, ullAvailVirtual, ullAvailExtendedVirtual;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX status);

    // --- the disk ----------------------------------------------------------------------

    // The disk's size as the guest sees it: the qcow2 header's big-endian size at byte 24
    // (QEMU's docs/interop/qcow2.rst). Read beside a running QEMU, which holds the file open.
    public static long DiskBytes(string disk)
    {
        var header = new byte[32];
        using (var f = new FileStream(disk, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
            if (f.Read(header, 0, header.Length) != header.Length || header[0] != 'Q' || header[1] != 'F' ||
                header[2] != 'I' || header[3] != 0xfb)
                throw new LauncherError(disk + " is not a qcow2 disk.");
        }
        long size = 0;
        for (int i = 24; i < 32; i++)
            size = size << 8 | header[i];
        return size;
    }

    public static long ChoiceBytes(string choice) { return long.Parse(choice) * GiB; }

    // Why `disk` cannot be grown to `choice`, or null when it can or is that size already. A
    // disk is never made smaller, and it grows only as far as its drive could hold it full with
    // the reserve still free: the file's length counts what the guest has discarded as held.
    public static string DiskRefusal(string choice, string disk)
    {
        long now = DiskBytes(disk), wanted = ChoiceBytes(choice);
        if (wanted < now)
            return "The machine's disk is " + Gb(now / (1024 * 1024)) + " GB, more than the " + choice +
                   " GB chosen, and a disk is never made smaller. Choose a larger size in the " +
                   "settings menu, or set disk= (empty) in settings.ini.";
        if (wanted == now)
            return null;
        var drive = new DriveInfo(Path.GetPathRoot(Path.GetFullPath(disk)));
        long spare = drive.AvailableFreeSpace + new FileInfo(disk).Length - DriveReserveBytes;
        if (wanted <= spare)
            return null;
        return "The machine's disk cannot grow to " + choice + " GB: drive " + drive.Name + " has " +
               Gb(drive.AvailableFreeSpace / (1024 * 1024)) + " GB free and keeps " +
               Gb(DriveReserveBytes / (1024 * 1024)) + " GB of it. Choose less in the settings " +
               "menu, or set disk= (empty) in settings.ini.";
    }

    // --- start at login ----------------------------------------------------------------

    const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    const string RunName = "RaiGolmi";

    // The Run value follows the setting at every start, so it always names where this exe is
    // now. Windows runs it with no arguments at sign-in, which is what a double-click does.
    public void ApplyStartAtLogin()
    {
        using (RegistryKey run = Registry.CurrentUser.OpenSubKey(RunKey, true))
        {
            if (run == null)
                throw new LauncherError(@"HKEY_CURRENT_USER\" + RunKey + " does not exist, so RaiGolmi " +
                                        "cannot be set to start at sign-in.");
            if (StartAtLogin)
                run.SetValue(RunName, "\"" + Application.ExecutablePath + "\"", RegistryValueKind.String);
            else
                run.DeleteValue(RunName, false);
        }
    }

    // --- the keeper's door ----------------------------------------------------------------

    public const string DoorTask = "RaiGolmi keeper door";

    // A task that runs this exe with --keeper-door every five minutes while the user is signed
    // in, in their session so the window appears on their desktop. It is registered again at
    // every start while the keeper is on, as the Run value is, so it names where this exe is now,
    // and removed when the keeper is turned off. Registered through
    // the Task Scheduler API rather than schtasks.exe: an interactive-token logon with an empty
    // user and password stores no password and asks for none (ITaskFolder::RegisterTask), and
    // the XML sets what schtasks cannot. A task's defaults never start it on battery, stop it on
    // unplugging, and run it below normal priority (7), which QEMU would inherit: the door
    // becomes the launcher it starts, so it runs at normal priority with no time limit.
    public void ApplyKeeperDoor()
    {
        try
        {
            dynamic service = Activator.CreateInstance(Type.GetTypeFromProgID("Schedule.Service", true));
            service.Connect(null, null, null, null);
            dynamic root = service.GetFolder("\\");
            if (Keeper)
                root.RegisterTask(DoorTask, DoorXml(), TASK_CREATE_OR_UPDATE, null, null,
                                  TASK_LOGON_INTERACTIVE_TOKEN, "");
            else
            {
                try
                {
                    root.DeleteTask(DoorTask, 0);
                }
                catch (COMException e) when (e.HResult == TaskNotFound)
                {
                }
            }
        }
        catch (COMException e)
        {
            throw new LauncherError("Windows' Task Scheduler refused to " + (Keeper ? "make" : "remove") +
                                    " the task '" + DoorTask + "' (0x" + e.HResult.ToString("x8") + "): " +
                                    e.Message);
        }
    }

    const int TASK_CREATE_OR_UPDATE = 6, TASK_LOGON_INTERACTIVE_TOKEN = 3;
    // HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND): no task by that name.
    const int TaskNotFound = unchecked((int)0x80070002);

    // The Task Scheduler schema (learn.microsoft.com, Task Scheduler Schema): a time trigger
    // repeating every five minutes with no duration, which repeats indefinitely.
    static string DoorXml()
    {
        string exe = Application.ExecutablePath;
        return "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\n" +
            "<Task version=\"1.2\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">\n" +
            "  <RegistrationInfo><Description>Starts RaiGolmi when the keeper asks, or when the " +
            "machine should be running and is not.</Description></RegistrationInfo>\n" +
            "  <Triggers><TimeTrigger><StartBoundary>" + DateTime.Now.ToString("yyyy-MM-dd'T'HH:mm:ss") +
            "</StartBoundary><Repetition><Interval>PT5M</Interval></Repetition></TimeTrigger></Triggers>\n" +
            "  <Principals><Principal id=\"Author\"><LogonType>InteractiveToken</LogonType>" +
            "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n" +
            "  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" +
            "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" +
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" +
            "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>5</Priority></Settings>\n" +
            "  <Actions Context=\"Author\"><Exec><Command>" + Xml("\"" + exe + "\"") + "</Command>" +
            "<Arguments>" + Launcher.DoorFlag + "</Arguments>" +
            "<WorkingDirectory>" + Xml(Path.GetDirectoryName(exe)) + "</WorkingDirectory></Exec></Actions>\n" +
            "</Task>\n";
    }

    static string Xml(string text) { return System.Security.SecurityElement.Escape(text); }

    // --- keep Windows awake ------------------------------------------------------------

    // Held by the calling thread until it changes it or ends, so it is called on the window's
    // thread, which lives as long as the launcher. The display may still sleep.
    public void ApplyKeepAwake()
    {
        uint state = ES_CONTINUOUS | (KeepAwake ? ES_SYSTEM_REQUIRED : 0);
        if (SetThreadExecutionState(state) == 0)
            throw new LauncherError("Windows refused to " + (KeepAwake ? "stay awake" : "be allowed to sleep") +
                                    " for RaiGolmi (SetThreadExecutionState).");
    }

    const uint ES_CONTINUOUS = 0x80000000, ES_SYSTEM_REQUIRED = 0x00000001;
    [DllImport("kernel32.dll")] static extern uint SetThreadExecutionState(uint state);
}

// Whether Windows has the audio endpoints QEMU's DirectSound backend opens when it starts. It
// opens the default playback and the default recording device whether or not the guest ever
// records (audio/dsoundaudio.c, audio_dsound_realize), and a failure there ends QEMU before the
// guest boots (audio/audio.c, audio_init_audiodevs, error_fatal).
static class AudioEndpoints
{
    // Why the machine cannot have its sound card on this PC now, or null when it can.
    public static string Missing()
    {
        var enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        try
        {
            var missing = new List<string>();
            if (!HasDefault(enumerator, eRender))
                missing.Add("no playback device (speakers or headphones)");
            if (!HasDefault(enumerator, eCapture))
                missing.Add("no recording device (a microphone or line in)");
            if (missing.Count == 0)
                return null;
            return "The machine has no sound this time: Windows has " + string.Join(" and ", missing) +
                   ", and QEMU's Windows audio cannot start without both. Enable one in Windows' " +
                   "Sound settings and start RaiGolmi again.";
        }
        finally
        {
            Marshal.ReleaseComObject(enumerator);
        }
    }

    static bool HasDefault(IMMDeviceEnumerator enumerator, int flow)
    {
        IntPtr device;
        int hr = enumerator.GetDefaultAudioEndpoint(flow, eConsole, out device);
        if (hr == E_NOTFOUND)
            return false;
        if (hr != 0)
            throw new LauncherError("Windows would not say which audio device is its default (0x" +
                                    hr.ToString("x8") + ").");
        Marshal.Release(device);
        return true;
    }

    const int eRender = 0, eCapture = 1, eConsole = 0;
    const int E_NOTFOUND = unchecked((int)0x80070490);

    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    class MMDeviceEnumerator { }

    // Only the methods this calls, in their vtable order (mmdeviceapi.h).
    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator
    {
        [PreserveSig] int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);
        [PreserveSig] int GetDefaultAudioEndpoint(int dataFlow, int role, out IntPtr endpoint);
    }
}
