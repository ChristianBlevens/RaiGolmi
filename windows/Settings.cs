// The launcher's own settings: how Windows treats RaiGolmi and what QEMU is started with. They
// live in %LOCALAPPDATA%\RaiGolmi\settings.ini as `key=value` lines and are read once at start;
// only the ntfy topic reaches inside the OS, handed in at boot. Every one is off, or the
// launcher's long-standing value, until it is chosen, from the window's title-bar menu or by
// editing the file.
//
//   start-at-login = on | off     the launcher in HKCU\...\Run, so Windows starts it at sign-in
//   keep-awake     = on | off     Windows does not sleep while the launcher runs
//   memory         = 8 | 12 | 16 | 24     the machine's RAM in GB, at its next start
//   venus          = on | off     the GPU offers Vulkan (Venus), at its next start
//   ntfy-topic     = <topic>      the phone alerts' ntfy topic, at the machine's next start;
//                                 empty is no alerts (raigolmid/raigolmid/lineout.py)
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Windows.Forms;
using Microsoft.Win32;

sealed class Settings
{
    public bool StartAtLogin, KeepAwake, Venus;
    public string Memory = DefaultMemory;
    public string NtfyTopic = "";
    readonly string path;

    public const string DefaultMemory = "8";
    public static readonly string[] MemoryChoices = { "8", "12", "16", "24" };
    // What Windows keeps for itself whatever the machine is given.
    const long HostReserveMiB = 4096;

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
                case "memory":
                    if (Array.IndexOf(MemoryChoices, value) < 0)
                        throw s.Bad(number, "gives memory as " + value + "; it is one of " +
                                            string.Join(", ", MemoryChoices));
                    s.Memory = value;
                    break;
                case "ntfy-topic":
                    string refused = TopicRefusal(value);
                    if (refused != null)
                        throw s.Bad(number, "gives ntfy-topic as " + value + ": " + refused);
                    s.NtfyTopic = value;
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
        File.WriteAllText(path,
            "start-at-login=" + OnOff(StartAtLogin) + Environment.NewLine +
            "keep-awake=" + OnOff(KeepAwake) + Environment.NewLine +
            "memory=" + Memory + Environment.NewLine +
            "venus=" + OnOff(Venus) + Environment.NewLine +
            "ntfy-topic=" + NtfyTopic + Environment.NewLine);
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
