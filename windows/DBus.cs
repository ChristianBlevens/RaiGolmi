// A D-Bus peer on one connected socket, for QEMU's display (Display.cs): ANONYMOUS auth as
// the client, little-endian marshalling of the types QEMU's display interfaces use, method
// calls out, and method calls in dispatched to one handler.
//
// Not Tmds.DBus: it registers handlers only after its connect (auth + Hello) completes, and
// QEMU sends GetAll on the listener the moment auth ends. That call answered UnknownMethod
// leaves QEMU's `Interfaces` empty and both Win32 frame paths off, with no error anywhere
// (GDBusProxy ignores a failed GetAll). Here the handler exists before the first byte is read.

#nullable enable

using System.Net.Sockets;
using System.Text;

sealed class DBusError : Exception
{
    public DBusError(string name, string message) : base($"{name}: {message}") { }
}

sealed class Msg
{
    public byte Type;            // 1 call, 2 return, 3 error, 4 signal
    public byte Flags;           // 0x1 no reply expected
    public uint Serial;
    public string? Path, Interface, Member, ErrorName, Signature;
    public uint ReplySerial;
    public byte[] Body = Array.Empty<byte>();
    public Reader Args() => new Reader(Body, 0);
}

sealed class Writer
{
    readonly List<byte> _b = new();
    public int Length => _b.Count;
    public byte[] ToArray() => _b.ToArray();

    public void Pad(int n) { while (_b.Count % n != 0) _b.Add(0); }
    public void Byte(byte v) => _b.Add(v);
    public void U16(ushort v) { Pad(2); _b.AddRange(BitConverter.GetBytes(v)); }
    public void U32(uint v) { Pad(4); _b.AddRange(BitConverter.GetBytes(v)); }
    public void I32(int v) { Pad(4); _b.AddRange(BitConverter.GetBytes(v)); }
    public void U64(ulong v) { Pad(8); _b.AddRange(BitConverter.GetBytes(v)); }
    public void Bool(bool v) => U32(v ? 1u : 0u);
    public void Str(string s) { var d = Encoding.UTF8.GetBytes(s); U32((uint)d.Length); _b.AddRange(d); _b.Add(0); }
    public void Sig(string s) { var d = Encoding.ASCII.GetBytes(s); _b.Add((byte)d.Length); _b.AddRange(d); _b.Add(0); }

    // An array's length counts the bytes after the padding to its first element.
    public void Array(int elementAlign, Action body)
    {
        Pad(4);
        int lenAt = _b.Count;
        _b.AddRange(new byte[4]);
        Pad(elementAlign);
        int start = _b.Count;
        body();
        var len = BitConverter.GetBytes((uint)(_b.Count - start));
        for (int i = 0; i < 4; i++) _b[lenAt + i] = len[i];
    }

    public void Bytes(byte[] d) => Array(1, () => _b.AddRange(d));
    public void Strings(IEnumerable<string> ss) => Array(4, () => { foreach (var s in ss) Str(s); });
}

sealed class Reader
{
    readonly byte[] _d;
    int _p;
    public Reader(byte[] d, int p) { _d = d; _p = p; }
    public int Pos => _p;

    void Align(int n) { while (_p % n != 0) _p++; }
    public byte Byte() => _d[_p++];
    public uint U32() { Align(4); var v = BitConverter.ToUInt32(_d, _p); _p += 4; return v; }
    public int I32() { Align(4); var v = BitConverter.ToInt32(_d, _p); _p += 4; return v; }
    public ulong U64() { Align(8); var v = BitConverter.ToUInt64(_d, _p); _p += 8; return v; }
    public bool Bool() => U32() != 0;
    public string Str() { int n = (int)U32(); var s = Encoding.UTF8.GetString(_d, _p, n); _p += n + 1; return s; }
    public string Sig() { int n = _d[_p++]; var s = Encoding.ASCII.GetString(_d, _p, n); _p += n + 1; return s; }
    public byte[] Bytes() { int n = (int)U32(); var r = new byte[n]; Buffer.BlockCopy(_d, _p, r, 0, n); _p += n; return r; }
    public void Struct() => Align(8);

    public object Variant()
    {
        var sig = Sig();
        return sig switch
        {
            "s" or "o" => Str(),
            "g" => Sig(),
            "u" => U32(),
            "i" => I32(),
            "t" => U64(),
            "b" => Bool(),
            "as" => ReadStrings(),
            "au" => ReadUInts(),
            _ => throw new NotSupportedException($"variant of signature '{sig}'"),
        };
    }

    public string[] ReadStrings()
    {
        uint n = U32(); Align(4);
        int end = _p + (int)n; var r = new List<string>();
        while (_p < end) r.Add(Str());
        return r.ToArray();
    }

    public uint[] ReadUInts()
    {
        uint n = U32(); Align(4);
        int end = _p + (int)n; var r = new List<uint>();
        while (_p < end) r.Add(U32());
        return r.ToArray();
    }

}

sealed class DBusPeer
{
    readonly Socket _sock;
    readonly NetworkStream _s;
    readonly object _writeLock = new();
    readonly Dictionary<uint, TaskCompletionSource<Msg>> _pending = new();
    readonly string _name;
    readonly Action<string> _log;
    uint _serial;

    // Every incoming method call goes here, on the read thread; it returns the reply body
    // (signature, bytes), or throws DBusError to answer with an error.
    public Func<Msg, (string sig, byte[] body)>? OnCall;

    public DBusPeer(Socket sock, string name, Action<string> log)
    {
        _sock = sock; _s = new NetworkStream(sock, ownsSocket: true); _name = name; _log = log;
    }

    // SASL as the client. GLib's server takes ANONYMOUS with any initial response and answers
    // OK at once (gdbusauthmechanismanon.c, server_initiate); QEMU allows it on Windows.
    public void Authenticate()
    {
        var hello = Convert.ToHexString(Encoding.ASCII.GetBytes("RaiGolmi")).ToLowerInvariant();
        WriteRaw(Encoding.ASCII.GetBytes($"\0AUTH ANONYMOUS {hello}\r\n"));
        var line = ReadLine();
        if (!line.StartsWith("OK ")) throw new IOException($"{_name}: auth refused: '{line}'");
        WriteRaw(Encoding.ASCII.GetBytes("BEGIN\r\n"));
        _log($"{_name}: authenticated ({line})");
    }

    public void Start()
    {
        var t = new Thread(ReadLoop) { IsBackground = true, Name = _name };
        t.Start();
    }

    public Msg Call(string path, string iface, string member, string sig, Action<Writer>? args)
    {
        var body = new Writer();
        args?.Invoke(body);
        var tcs = new TaskCompletionSource<Msg>(TaskCreationOptions.RunContinuationsAsynchronously);
        uint serial;
        lock (_writeLock)
        {
            serial = ++_serial;
            lock (_pending) _pending[serial] = tcs;
            WriteRaw(Build(1, 0, serial, path, iface, member, null, 0, sig, body.ToArray()));
        }
        if (!tcs.Task.Wait(TimeSpan.FromSeconds(10)))
            throw new TimeoutException($"{_name}: no reply to {iface}.{member} in 10 s");
        var reply = tcs.Task.Result;
        if (reply.Type == 3)
            throw new DBusError(reply.ErrorName ?? "?", reply.Signature == "s" ? reply.Args().Str() : "");
        return reply;
    }

    // A call whose answer nobody waits for — input, sent from the window's thread. An error
    // QEMU answers with still reaches `onError`, on the read thread.
    public void Post(string path, string iface, string member, string sig, Action<Writer> args,
                     Action<string> onError)
    {
        var body = new Writer();
        args(body);
        var tcs = new TaskCompletionSource<Msg>(TaskCreationOptions.RunContinuationsAsynchronously);
        tcs.Task.ContinueWith(t =>
        {
            if (t.IsFaulted) onError($"{iface}.{member}: {t.Exception!.InnerException!.Message}");
            else if (t.Result.Type == 3)
                onError($"{iface}.{member}: {t.Result.ErrorName}: {(t.Result.Signature == "s" ? t.Result.Args().Str() : "")}");
        });
        lock (_writeLock)
        {
            uint serial = ++_serial;
            lock (_pending) _pending[serial] = tcs;
            WriteRaw(Build(1, 0, serial, path, iface, member, null, 0, sig, body.ToArray()));
        }
    }

    void ReadLoop()
    {
        try
        {
            while (true)
            {
                var m = ReadMessage();
                if (m.Type is 2 or 3)
                {
                    TaskCompletionSource<Msg>? tcs;
                    lock (_pending) { _pending.Remove(m.ReplySerial, out tcs); }
                    if (tcs == null) throw new IOException($"{_name}: reply to unknown serial {m.ReplySerial}");
                    tcs.SetResult(m);
                }
                else if (m.Type == 1)
                {
                    Dispatch(m);
                }
                else
                {
                    _log($"{_name}: signal {m.Interface}.{m.Member} on {m.Path}");
                }
            }
        }
        catch (Exception e)
        {
            _log($"{_name}: connection ended: {e.GetType().Name}: {e.Message}");
            lock (_pending) foreach (var t in _pending.Values) t.TrySetException(e);
            Closed?.Invoke();
        }
    }

    public event Action? Closed;

    void Dispatch(Msg m)
    {
        byte[] reply;
        try
        {
            if (OnCall == null) throw new DBusError("org.freedesktop.DBus.Error.UnknownMethod", "no objects");
            var (sig, body) = OnCall(m);
            reply = Build(2, 0, 0, null, null, null, null, m.Serial, sig, body);
        }
        catch (DBusError e)
        {
            _log($"{_name}: {m.Interface}.{m.Member} on {m.Path} -> error {e.Message}");
            var w = new Writer(); w.Str(e.Message);
            var name = e.Message.Split(':')[0];
            reply = Build(3, 0, 0, null, null, null, name, m.Serial, "s", w.ToArray());
        }
        if ((m.Flags & 1) != 0) return;
        lock (_writeLock)
        {
            // The serial is patched in under the lock so serials stay in send order.
            uint serial = ++_serial;
            BitConverter.GetBytes(serial).CopyTo(reply, 8);
            WriteRaw(reply);
        }
    }

    static byte[] Build(byte type, byte flags, uint serial, string? path, string? iface, string? member,
                        string? errorName, uint replySerial, string sig, byte[] body)
    {
        var w = new Writer();
        w.Byte((byte)'l'); w.Byte(type); w.Byte(flags); w.Byte(1);
        w.U32((uint)body.Length); w.U32(serial);
        w.Array(8, () =>
        {
            void Field(byte code, string vsig, Action value) { w.Pad(8); w.Byte(code); w.Sig(vsig); value(); }
            if (path != null) Field(1, "o", () => w.Str(path));
            if (iface != null) Field(2, "s", () => w.Str(iface));
            if (member != null) Field(3, "s", () => w.Str(member));
            if (errorName != null) Field(4, "s", () => w.Str(errorName));
            if (replySerial != 0) Field(5, "u", () => w.U32(replySerial));
            if (sig.Length > 0) Field(8, "g", () => w.Sig(sig));
        });
        w.Pad(8);
        var head = w.ToArray();
        var all = new byte[head.Length + body.Length];
        head.CopyTo(all, 0); body.CopyTo(all, head.Length);
        return all;
    }

    Msg ReadMessage()
    {
        var fixedPart = ReadExact(16);
        if (fixedPart[0] != (byte)'l') throw new IOException($"{_name}: big-endian message");
        uint bodyLen = BitConverter.ToUInt32(fixedPart, 4);
        uint fieldsLen = BitConverter.ToUInt32(fixedPart, 12);
        int headerLen = 16 + (int)fieldsLen;
        int padded = (headerLen + 7) & ~7;
        var rest = ReadExact(padded - 16 + (int)bodyLen);
        var all = new byte[16 + rest.Length];
        fixedPart.CopyTo(all, 0); rest.CopyTo(all, 16);

        var m = new Msg { Type = all[1], Flags = all[2], Serial = BitConverter.ToUInt32(all, 8) };
        var r = new Reader(all, 16);
        while (r.Pos < headerLen)
        {
            r.Struct();
            byte code = r.Byte();
            var v = r.Variant();
            switch (code)
            {
                case 1: m.Path = (string)v; break;
                case 2: m.Interface = (string)v; break;
                case 3: m.Member = (string)v; break;
                case 4: m.ErrorName = (string)v; break;
                case 5: m.ReplySerial = (uint)v; break;
                case 8: m.Signature = (string)v; break;
            }
        }
        m.Body = new byte[bodyLen];
        Buffer.BlockCopy(all, padded, m.Body, 0, (int)bodyLen);
        return m;
    }

    byte[] ReadExact(int n)
    {
        var b = new byte[n];
        int got = 0;
        while (got < n)
        {
            int k = _s.Read(b, got, n - got);
            if (k == 0) throw new EndOfStreamException($"{_name}: peer closed");
            got += k;
        }
        return b;
    }

    // Byte at a time, so nothing after the auth line is consumed before the message reader.
    string ReadLine()
    {
        var sb = new StringBuilder();
        while (true)
        {
            int c = _s.ReadByte();
            if (c < 0) throw new EndOfStreamException($"{_name}: peer closed during auth");
            if (c == '\n') return sb.ToString().TrimEnd('\r');
            sb.Append((char)c);
        }
    }

    void WriteRaw(byte[] d) => _s.Write(d, 0, d.Length);
}
