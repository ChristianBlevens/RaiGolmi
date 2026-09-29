// QEMU's D-Bus display (`-display dbus,p2p=yes,gl=on`), with this program as its one client:
// frames in, keyboard and pointer out, and the window's size offered to the guest.
//
// The connection is qemu-display's (display.rs, console.rs). An AF_UNIX pair's far end is
// handed to QEMU as a WSAPROTOCOL_INFOW over QMP (`get-win32-socket`, then `add_client
// @dbus-display` on the same QMP connection, since the socket is kept by the monitor that
// imported it, monitor/fds.c). On that connection `Console.RegisterListener` hands QEMU a second
// pair's end the same way, and QEMU calls the listener on it. That second socket must be
// AF_UNIX: QEMU reads this process's pid from its credentials, and without the pid it cannot
// duplicate texture or mapping handles into it, so both Win32 frame paths stay off without a
// word (dbus-listener.c, dbus_display_listener_setup_peer_process).
//
// Keys are the hook's QEMU key numbers (Keyboard); buttons are QAPI InputButton values.

using System.Net.Sockets;
using System.Runtime.InteropServices;

sealed class Display
{
    const string ConsolePath = "/org/qemu/Display1/Console_0";
    public const uint ButtonLeft = 0, ButtonMiddle = 1, ButtonRight = 2, WheelUp = 3, WheelDown = 4;

    readonly DBusPeer _display;
    readonly Action<string> _note;
    DBusPeer _listener;
    volatile bool _stopped;

    Display(DBusPeer display, Action<string> note) { _display = display; _note = note; }

    // QEMU stops sending frames here: its listener goes when this connection closes
    // (ui/dbus-console.c listener_vanished_cb). Before a shutdown, because QEMU 11.1.1 aborts
    // reading back a frame whose size changed between the desktop and the guest's console
    // (ui/dbus-listener.c dbus_call_update_gl, ui/egl-helpers.c egl_fb_read_rect), and nothing
    // reads back with no listener. Input still goes over the display connection.
    public void StopListening()
    {
        if (_stopped) return;
        _stopped = true;
        _note("listener: closed before the shutdown, so QEMU sends no more frames");
        _listener.Close();
    }

    // Blocks until QEMU is listening to us; `qmp` is a connection no one else is using.
    // `lost` is told when QEMU stops listening to us or calling us while it still runs.
    public static Display Connect(Qmp qmp, int qemuPid, Renderer renderer, Action<string> note, Action<string> lost)
    {
        var (ours, theirs) = Win32Socket.Pair();
        qmp.Call("get-win32-socket", "{\"info\":\"" + Convert.ToBase64String(Win32Socket.DuplicateFor(theirs, qemuPid)) +
                                     "\",\"fdname\":\"dbus\"}");
        qmp.Call("add_client", "{\"protocol\":\"@dbus-display\",\"fdname\":\"dbus\"}");
        theirs.Dispose();
        var display = new DBusPeer(ours, "display", note);
        display.Closed += () => lost("the display connection");
        display.Authenticate();
        display.Start();

        // The listener's handler exists before its socket is read: QEMU asks for its
        // `Interfaces` the moment authentication ends.
        var (lOurs, lTheirs) = Win32Socket.Pair();
        var listener = new Listener(renderer, note);
        var peer = new DBusPeer(lOurs, "listener", note) { OnCall = listener.Handle };
        var result = new Display(display, note) { _listener = peer };
        peer.Closed += () => { if (!result._stopped) lost("the listener connection"); };
        var info = Win32Socket.DuplicateFor(lTheirs, qemuPid);
        display.Call(ConsolePath, "org.qemu.Display1.Console", "RegisterListener", "ay", w => w.Bytes(info));
        lTheirs.Dispose();
        peer.Authenticate();
        peer.Start();
        return result;
    }

    public void Key(bool down, int qnum) =>
        _display.Post(ConsolePath, "org.qemu.Display1.Keyboard", down ? "Press" : "Release", "u",
                      w => w.U32((uint)qnum), _note);

    public void Pointer(int x, int y) =>
        _display.Post(ConsolePath, "org.qemu.Display1.Mouse", "SetAbsPosition", "uu",
                      w => { w.U32((uint)x); w.U32((uint)y); }, _note);

    public void Button(bool down, uint button) =>
        _display.Post(ConsolePath, "org.qemu.Display1.Mouse", down ? "Press" : "Release", "u",
                      w => w.U32(button), _note);

    // The guest's preferred mode; it answers with a scanout of that size.
    public void OfferSize(int width, int height) =>
        _display.Post(ConsolePath, "org.qemu.Display1.Console", "SetUIInfo", "qqiiuu", w =>
        {
            w.U16(0); w.U16(0); w.I32(0); w.I32(0); w.U32((uint)width); w.U32((uint)height);
        }, _note);
}

// The object QEMU calls at /org/qemu/Display1/Listener. Its `Interfaces` names both Win32 frame
// paths, and both are needed: QEMU sends a GL scanout down the D3D11 path whenever virgl hands
// it a shared texture, with no proxy for it unless the interface was named
// (dbus-listener.c, dbus_scanout_texture), and uses the map for its 2D surface.
sealed class Listener
{
    const string ListenerIface = "org.qemu.Display1.Listener";
    const string MapIface = "org.qemu.Display1.Listener.Win32.Map";
    const string D3dIface = "org.qemu.Display1.Listener.Win32.D3d11";
    static readonly string[] Advertised = { D3dIface, MapIface };
    static readonly (string, byte[]) Empty = ("", Array.Empty<byte>());

    readonly Renderer _r;
    readonly Action<string> _note;

    public Listener(Renderer r, Action<string> note) { _r = r; _note = note; }

    public (string, byte[]) Handle(Msg m)
    {
        var a = m.Args();
        switch (m.Interface, m.Member)
        {
            case ("org.freedesktop.DBus.Properties", "GetAll"):
            {
                var iface = a.Str();
                var w = new Writer();
                w.Array(8, () =>
                {
                    if (iface != ListenerIface) return;
                    w.Pad(8); w.Str("Interfaces"); w.Sig("as"); w.Strings(Advertised);
                });
                return ("a{sv}", w.ToArray());
            }
            case ("org.freedesktop.DBus.Properties", "Get"):
            {
                var (iface, prop) = (a.Str(), a.Str());
                if (iface != ListenerIface || prop != "Interfaces")
                    throw new DBusError("org.freedesktop.DBus.Error.UnknownProperty", $"{iface}.{prop}");
                var w = new Writer(); w.Sig("as"); w.Strings(Advertised);
                return ("v", w.ToArray());
            }
            case ("org.freedesktop.DBus.Peer", "Ping"):
                return Empty;

            case (D3dIface, "ScanoutTexture2d"):
            {
                ulong handle = a.U64(); a.U32(); a.U32(); bool y0Top = a.Bool();
                uint x = a.U32(), y = a.U32(), w = a.U32(), h = a.U32();
                _r.ScanoutTexture((IntPtr)(long)handle, y0Top, (int)x, (int)y, (int)w, (int)h);
                return Empty;
            }
            case (D3dIface, "UpdateTexture2d"):
                _r.UpdateTexture();
                return Empty;
            case (MapIface, "ScanoutMap"):
            {
                ulong handle = a.U64(); uint off = a.U32(), w = a.U32(), h = a.U32(), stride = a.U32(), fmt = a.U32();
                _r.ScanoutMap((IntPtr)(long)handle, off, (int)w, (int)h, (int)stride, fmt);
                return Empty;
            }
            case (MapIface, "UpdateMap"):
                _r.UpdateMap(a.I32(), a.I32(), a.I32(), a.I32());
                return Empty;
            case (ListenerIface, "Scanout"):
            {
                uint w = a.U32(), h = a.U32(), stride = a.U32(), fmt = a.U32();
                _r.ScanoutBytes((int)w, (int)h, (int)stride, fmt, a.Bytes());
                return Empty;
            }
            case (ListenerIface, "Update"):
            {
                int x = a.I32(), y = a.I32(), w = a.I32(), h = a.I32(); uint stride = a.U32(), fmt = a.U32();
                _r.UpdateBytes(x, y, w, h, (int)stride, fmt, a.Bytes());
                return Empty;
            }
            case (ListenerIface, "Disable"):
                _r.Disable();
                return Empty;
            case (ListenerIface, "MouseSet"):
            {
                a.I32(); a.I32();
                _r.CursorVisible(a.I32() != 0);
                return Empty;
            }
            case (ListenerIface, "CursorDefine"):
            {
                int w = a.I32(), h = a.I32(), hx = a.I32(), hy = a.I32();
                _r.CursorDefine(w, h, hx, hy, a.Bytes());
                return Empty;
            }
        }
        _note($"the display called {m.Interface}.{m.Member}, which this program does not answer");
        throw new DBusError("org.freedesktop.DBus.Error.UnknownMethod", $"{m.Interface}.{m.Member} on {m.Path}");
    }
}

static class Win32Socket
{
    // Windows has no socketpair for AF_UNIX: bind a path, connect to it, accept, remove the path.
    public static (Socket ours, Socket theirs) Pair()
    {
        var path = Path.Combine(Path.GetTempPath(), $"raigolmi-{Guid.NewGuid():N}.sock");
        using var l = new Socket(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);
        l.Bind(new UnixDomainSocketEndPoint(path));
        l.Listen(1);
        var ours = new Socket(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);
        ours.Connect(new UnixDomainSocketEndPoint(path));
        var theirs = l.Accept();
        File.Delete(path);
        return (ours, theirs);
    }

    [DllImport("ws2_32.dll", SetLastError = true)]
    static extern unsafe int WSADuplicateSocketW(IntPtr s, uint pid, byte* info);

    [DllImport("ws2_32.dll")]
    static extern int WSAGetLastError();

    // WSAPROTOCOL_INFOW is 628 bytes on x64; QEMU refuses any other length (monitor/fds.c).
    public static unsafe byte[] DuplicateFor(Socket s, int pid)
    {
        var info = new byte[628];
        fixed (byte* p = info)
        {
            if (WSADuplicateSocketW(s.Handle, (uint)pid, p) != 0)
                throw new IOException($"WSADuplicateSocketW for pid {pid}: WSA error {WSAGetLastError()}");
        }
        return info;
    }
}
