// The guest's screen in the window: a flip-model swap chain the size of the client area, and
// the guest's last frame drawn into it 1:1, centred when the window is larger.
//
// The frame is this program's own copy. QEMU holds the shared texture's keyed mutex except
// while an update is being answered — it releases key 0, calls UpdateTexture2d, and takes the
// key back when the reply arrives (dbus-listener.c, dbus_call_update_gl) — so the copy is taken
// between Acquire and Release, and every later draw (a resize, a repaint, Ctrl+Alt+R) reads
// the copy without waiting on QEMU. The shared texture's handle is an NT handle already
// duplicated into this process (IDXGIResource1::CreateSharedHandle), hence OpenSharedResource1,
// and it is ours to close.
//
// A GL frame may need turning over, so the draw is a textured quad that can flip it, not a
// copy. The texture is D3D's, row 0 at the top, and `y0_top` true is the case that is turned —
// rdw's rule (rdw4/src/paintable/imp.rs), seen upright on the user's screen. The 2D surface
// (firmware, boot) is pixman x8r8g8b8: BGRA in memory.
//
// The guest's pointer shape becomes the window's own cursor, so the pointer moves at Windows'
// rate while the guest is told where it is.
//
// Calls come from the listener's read thread and the window's thread, so one lock covers the
// device context.

using System.Runtime.InteropServices;
using Vortice.D3DCompiler;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;
using Vortice.Mathematics;

sealed class Renderer
{
    const uint PixmanX8R8G8B8 = 0x20020888;
    const uint PixmanA8R8G8B8 = 0x20028888;
    const Format SwapFormat = Format.B8G8R8A8_UNorm;

    // Four vertices from their ids, a strip covering the viewport; `flipped` reads the texture
    // bottom-up.
    const string Shaders = @"
Texture2D frame : register(t0);
SamplerState pick : register(s0);
struct V { float4 p : SV_Position; float2 uv : TEXCOORD0; };
V quad(uint id, bool flip)
{
    float2 uv = float2(id & 1, id >> 1);
    V v;
    v.p = float4(uv.x * 2 - 1, 1 - uv.y * 2, 0, 1);
    v.uv = flip ? float2(uv.x, 1 - uv.y) : uv;
    return v;
}
V upright(uint id : SV_VertexID) { return quad(id, false); }
V flipped(uint id : SV_VertexID) { return quad(id, true); }
float4 draw(V v) : SV_Target { return float4(frame.Sample(pick, v.uv).rgb, 1); }
";

    readonly object _gate = new();
    readonly Form _form;
    readonly Action<string> _note;
    readonly ID3D11Device1 _dev;
    readonly ID3D11DeviceContext _ctx;
    readonly IDXGISwapChain1 _swap;
    readonly ID3D11VertexShader _upright, _flipped;
    readonly ID3D11PixelShader _draw;
    readonly ID3D11SamplerState _pick;
    (int w, int h) _client;

    ID3D11Texture2D _frame;
    ID3D11ShaderResourceView _frameView;
    Format _frameFormat;
    bool _frameFlipped, _frameIsTexture;
    volatile int _frameW, _frameH;

    ID3D11Texture2D _shared;
    IDXGIKeyedMutex _mutex;
    (int x, int y, int w, int h) _sharedRect;

    IntPtr _view, _viewBase;
    int _viewStride;
    long _viewSize;

    IntPtr _guestCursor, _blankCursor;
    bool _cursorShown, _cursorSeen;

    // The guest's screen size, for placing the pointer; 0 before the first frame.
    public int FrameWidth => _frameW;
    public int FrameHeight => _frameH;

    public Renderer(Form form, Action<string> note)
    {
        _form = form;
        _note = note;
        D3D11.D3D11CreateDevice(null, DriverType.Hardware, DeviceCreationFlags.BgraSupport,
            new[] { FeatureLevel.Level_11_1, FeatureLevel.Level_11_0 },
            out ID3D11Device dev, out ID3D11DeviceContext ctx).CheckError();
        _dev = dev.QueryInterface<ID3D11Device1>();
        dev.Dispose();
        _ctx = ctx;
        using (var dxgi = _dev.QueryInterface<IDXGIDevice>())
        using (var adapter = dxgi.GetAdapter())
            note($"drawing on '{adapter.Description.Description}'");

        _upright = _dev.CreateVertexShader(Compiler.Compile(Shaders, "upright", "renderer", "vs_4_0", ShaderFlags.None, EffectFlags.None).Span, null);
        _flipped = _dev.CreateVertexShader(Compiler.Compile(Shaders, "flipped", "renderer", "vs_4_0", ShaderFlags.None, EffectFlags.None).Span, null);
        _draw = _dev.CreatePixelShader(Compiler.Compile(Shaders, "draw", "renderer", "ps_4_0", ShaderFlags.None, EffectFlags.None).Span, null);
        _pick = _dev.CreateSamplerState(new SamplerDescription
        {
            Filter = Filter.MinMagMipPoint,
            AddressU = TextureAddressMode.Clamp, AddressV = TextureAddressMode.Clamp, AddressW = TextureAddressMode.Clamp,
            ComparisonFunc = ComparisonFunction.Never, MaxLOD = float.MaxValue,
        });

        _client = (Math.Max(1, form.ClientSize.Width), Math.Max(1, form.ClientSize.Height));
        using var factory = DXGI.CreateDXGIFactory2<IDXGIFactory2>(false);
        _swap = factory.CreateSwapChainForHwnd(_dev, form.Handle, new SwapChainDescription1
        {
            Width = (uint)_client.w, Height = (uint)_client.h, Format = SwapFormat,
            BufferCount = 2, BufferUsage = Usage.RenderTargetOutput,
            SampleDescription = new SampleDescription(1, 0),
            SwapEffect = SwapEffect.FlipDiscard, Scaling = Scaling.None, AlphaMode = AlphaMode.Ignore,
        });

        _blankCursor = MakeCursor(1, 1, 0, 0, new byte[4]);
        form.Cursor = new Cursor(_blankCursor);
    }

    // --- the window's side ---------------------------------------------------------------

    public void Resize(int w, int h)
    {
        lock (_gate)
        {
            _client = (Math.Max(1, w), Math.Max(1, h));
            _ctx.ClearState();
            _swap.ResizeBuffers(2, (uint)_client.w, (uint)_client.h, SwapFormat, SwapChainFlags.None).CheckError();
            Draw();
        }
    }

    public void Redraw()
    {
        lock (_gate) Draw();
    }

    // --- QEMU's side -----------------------------------------------------------------------

    public void ScanoutTexture(IntPtr handle, bool y0Top, int x, int y, int w, int h)
    {
        lock (_gate)
        {
            _mutex?.Dispose();
            _shared?.Dispose();
            _shared = _dev.OpenSharedResource1<ID3D11Texture2D>(handle);
            if (!CloseHandle(handle)) throw new InvalidOperationException($"CloseHandle: {Marshal.GetLastWin32Error()}");
            _mutex = _shared.QueryInterface<IDXGIKeyedMutex>();
            _sharedRect = (x, y, w, h);
            EnsureFrame(w, h, _shared.Description.Format, flipped: y0Top);
            _frameIsTexture = true;
        }
    }

    public void UpdateTexture()
    {
        lock (_gate)
        {
            if (_shared == null) throw new InvalidOperationException("UpdateTexture2d before any ScanoutTexture2d");
            var (x, y, w, h) = _sharedRect;
            _mutex.AcquireSync(0, -1);
            _ctx.CopySubresourceRegion(_frame, 0, 0, 0, 0, _shared, 0, new Box(x, y, 0, x + w, y + h, 1));
            _mutex.ReleaseSync(0);
            Draw();
        }
    }

    public void ScanoutMap(IntPtr handle, uint offset, int w, int h, int stride, uint pixman)
    {
        lock (_gate)
        {
            Unmap();
            _view = MapViewOfFile(handle, FileMapRead, 0, 0, UIntPtr.Zero);
            if (_view == IntPtr.Zero) throw new InvalidOperationException($"MapViewOfFile: {Marshal.GetLastWin32Error()}");
            if (!CloseHandle(handle)) throw new InvalidOperationException($"CloseHandle: {Marshal.GetLastWin32Error()}");
            _viewSize = (long)Mapped(_view);
            if (offset + (long)stride * h > _viewSize)
                throw new InvalidOperationException($"ScanoutMap of {w}x{h}, stride {stride}, at offset {offset} " +
                                                    $"reaches past its {_viewSize}-byte mapping");
            _viewBase = _view + (nint)offset;
            _viewStride = stride;
            EnsureFrame(w, h, PixelFormat(pixman), flipped: false);
            _frameIsTexture = false;
            Upload(0, 0, w, h, _viewBase, stride);
        }
    }

    public void UpdateMap(int x, int y, int w, int h)
    {
        lock (_gate)
        {
            if (_viewBase == IntPtr.Zero) throw new InvalidOperationException("UpdateMap before any ScanoutMap");
            Within("UpdateMap", x, y, w, h);
            Upload(x, y, w, h, _viewBase + (nint)(y * _viewStride + x * 4), _viewStride);
        }
    }

    public void ScanoutBytes(int w, int h, int stride, uint pixman, byte[] data)
    {
        lock (_gate)
        {
            EnsureFrame(w, h, PixelFormat(pixman), flipped: false);
            _frameIsTexture = false;
            UploadBytes(0, 0, w, h, stride, data);
        }
    }

    public void UpdateBytes(int x, int y, int w, int h, int stride, uint pixman, byte[] data)
    {
        lock (_gate)
        {
            if (_frame == null || _frameFormat != PixelFormat(pixman))
                throw new InvalidOperationException($"Update in format 0x{pixman:x8} without a Scanout in it");
            Within("Update", x, y, w, h);
            if (data.Length < (long)stride * (h - 1) + w * 4)
                throw new InvalidOperationException($"Update of {w}x{h}, stride {stride}, came with {data.Length} bytes");
            UploadBytes(x, y, w, h, stride, data);
        }
    }

    // The guest's GL scanout is off. QEMU has already switched the console to its own 2D
    // surface and handed that over as a map (virgl's SET_SCANOUT of resource 0 sets the surface,
    // then disables the GL scanout), and it goes on updating that map: only the texture goes.
    public void Disable()
    {
        lock (_gate)
        {
            _mutex?.Dispose(); _mutex = null;
            _shared?.Dispose(); _shared = null;
            if (_frameIsTexture)
            {
                DropFrame();
                Draw();
            }
        }
    }

    // The frame as it is drawn, as RGB rows top to bottom, read back through a staging copy;
    // null while the guest shows nothing.
    public unsafe (int w, int h, byte[] rgb)? ReadFrame()
    {
        lock (_gate)
        {
            if (_frame == null) return null;
            var (r, b) = _frameFormat switch
            {
                Format.B8G8R8A8_UNorm or Format.B8G8R8A8_UNorm_SRgb or Format.B8G8R8X8_UNorm => (2, 0),
                Format.R8G8B8A8_UNorm or Format.R8G8B8A8_UNorm_SRgb => (0, 2),
                _ => throw new InvalidOperationException($"no readback for a frame in {_frameFormat}"),
            };
            int w = _frameW, h = _frameH;
            using var staging = _dev.CreateTexture2D(new Texture2DDescription
            {
                Width = (uint)w, Height = (uint)h, MipLevels = 1, ArraySize = 1, Format = _frameFormat,
                SampleDescription = new SampleDescription(1, 0),
                Usage = ResourceUsage.Staging, CPUAccessFlags = CpuAccessFlags.Read,
            });
            _ctx.CopyResource(staging, _frame);
            var mapped = _ctx.Map(staging, 0, MapMode.Read, Vortice.Direct3D11.MapFlags.None);
            try
            {
                var rgb = new byte[w * h * 3];
                for (int row = 0; row < h; row++)
                {
                    byte* src = (byte*)mapped.DataPointer + (nint)(_frameFlipped ? h - 1 - row : row) * mapped.RowPitch;
                    int dst = row * w * 3;
                    for (int x = 0; x < w; x++, src += 4, dst += 3)
                    {
                        rgb[dst] = src[r];
                        rgb[dst + 1] = src[1];
                        rgb[dst + 2] = src[b];
                    }
                }
                return (w, h, rgb);
            }
            finally
            {
                _ctx.Unmap(staging, 0);
            }
        }
    }

    // virglrenderer turns every cursor's rows over as it reads them back, for GL's bottom-up
    // storage (vrend_renderer_get_cursor_contents); ANGLE stores them top-down, as D3D does, so
    // they arrive upside down and are turned back here.
    public void CursorDefine(int w, int h, int hotX, int hotY, byte[] argb)
    {
        if (argb.Length != w * h * 4)
            throw new InvalidOperationException($"a {w}x{h} cursor came with {argb.Length} bytes");
        var upright = new byte[argb.Length];
        for (int row = 0; row < h; row++)
            Buffer.BlockCopy(argb, row * w * 4, upright, (h - 1 - row) * w * 4, w * 4);
        var cursor = MakeCursor(w, h, hotX, hotY, upright);
        if (!_cursorSeen)
        {
            _cursorSeen = true;
            _note($"the guest's pointer arrives as its own {w}x{h} image, drawn as the window's cursor");
        }
        _form.BeginInvoke(() =>
        {
            var old = _guestCursor;
            _guestCursor = cursor;
            ShowCursor();
            if (old != IntPtr.Zero) DestroyCursor(old);
        });
    }

    public void CursorVisible(bool on) => _form.BeginInvoke(() => { _cursorShown = on; ShowCursor(); });

    void ShowCursor() =>
        _form.Cursor = new Cursor(_cursorShown && _guestCursor != IntPtr.Zero ? _guestCursor : _blankCursor);

    // --- inside the lock -----------------------------------------------------------------

    void EnsureFrame(int w, int h, Format f, bool flipped)
    {
        _frameFlipped = flipped;
        if (_frame != null && _frameW == w && _frameH == h && _frameFormat == f) return;
        DropFrame();
        _frame = _dev.CreateTexture2D(new Texture2DDescription
        {
            Width = (uint)w, Height = (uint)h, MipLevels = 1, ArraySize = 1, Format = f,
            SampleDescription = new SampleDescription(1, 0),
            Usage = ResourceUsage.Default, BindFlags = BindFlags.ShaderResource,
        });
        _frameView = _dev.CreateShaderResourceView(_frame, null);
        _frameFormat = f;
        (_frameW, _frameH) = (w, h);
        _note($"the guest's screen is {w}x{h} ({f}{(flipped ? ", bottom-up" : "")})");
    }

    // An update outside the frame would have the driver write past the texture and the copy
    // read past what QEMU shared: an access violation that takes the process down unrecorded.
    void Within(string what, int x, int y, int w, int h)
    {
        if (x < 0 || y < 0 || w < 0 || h < 0 || x + w > _frameW || y + h > _frameH)
            throw new InvalidOperationException($"{what} of {w}x{h} at {x},{y} lies outside the {_frameW}x{_frameH} frame");
    }

    void DropFrame()
    {
        _frameView?.Dispose(); _frameView = null;
        _frame?.Dispose(); _frame = null;
        (_frameW, _frameH) = (0, 0);
    }

    void Upload(int x, int y, int w, int h, IntPtr src, int stride)
    {
        _ctx.UpdateSubresource(_frame, 0, new Box(x, y, 0, x + w, y + h, 1), src, (uint)stride, 0);
        Draw();
    }

    unsafe void UploadBytes(int x, int y, int w, int h, int stride, byte[] data)
    {
        fixed (byte* p = data) Upload(x, y, w, h, (IntPtr)p, stride);
    }

    void Draw()
    {
        using var back = _swap.GetBuffer<ID3D11Texture2D>(0);
        using var target = _dev.CreateRenderTargetView(back, null);
        _ctx.ClearRenderTargetView(target, new Color4(0, 0, 0, 1));
        if (_frame != null)
        {
            _ctx.OMSetRenderTargets(target, null);
            _ctx.RSSetViewport(Math.Max(0, (_client.w - _frameW) / 2), Math.Max(0, (_client.h - _frameH) / 2),
                               _frameW, _frameH, 0, 1);
            _ctx.IASetPrimitiveTopology(PrimitiveTopology.TriangleStrip);
            _ctx.VSSetShader(_frameFlipped ? _flipped : _upright, null, 0);
            _ctx.PSSetShader(_draw, null, 0);
            _ctx.PSSetShaderResource(0, _frameView);
            _ctx.PSSetSampler(0, _pick);
            _ctx.Draw(4, 0);
        }
        _swap.Present(0, PresentFlags.None).CheckError();
    }

    void Unmap()
    {
        if (_view != IntPtr.Zero && !UnmapViewOfFile(_view))
            throw new InvalidOperationException($"UnmapViewOfFile: {Marshal.GetLastWin32Error()}");
        _view = _viewBase = IntPtr.Zero;
    }

    static Format PixelFormat(uint pixman) => pixman switch
    {
        PixmanX8R8G8B8 or PixmanA8R8G8B8 => Format.B8G8R8A8_UNorm,
        _ => throw new NotSupportedException($"QEMU sent pixman format 0x{pixman:x8}, which this program does not draw"),
    };

    // --- a cursor from QEMU's 32-bit ARGB, the documented way to make one with alpha: a
    // 32-bit top-down DIB section with an alpha mask, and an empty monochrome mask.

    static IntPtr MakeCursor(int w, int h, int hotX, int hotY, byte[] argb)
    {
        if (argb.Length != w * h * 4)
            throw new InvalidOperationException($"a {w}x{h} cursor came with {argb.Length} bytes");
        var header = new BITMAPV5HEADER
        {
            bV5Size = (uint)Marshal.SizeOf<BITMAPV5HEADER>(), bV5Width = w, bV5Height = -h,
            bV5Planes = 1, bV5BitCount = 32, bV5Compression = 3,
            bV5RedMask = 0x00FF0000, bV5GreenMask = 0x0000FF00, bV5BlueMask = 0x000000FF, bV5AlphaMask = 0xFF000000,
        };
        IntPtr dc = GetDC(IntPtr.Zero);
        IntPtr color = CreateDIBSection(dc, ref header, 0, out IntPtr bits, IntPtr.Zero, 0);
        ReleaseDC(IntPtr.Zero, dc);
        if (color == IntPtr.Zero) throw new InvalidOperationException($"CreateDIBSection: {Marshal.GetLastWin32Error()}");
        Marshal.Copy(argb, 0, bits, argb.Length);
        IntPtr mask = CreateBitmap(w, h, 1, 1, IntPtr.Zero);
        var info = new ICONINFO { fIcon = false, xHotspot = hotX, yHotspot = hotY, hbmMask = mask, hbmColor = color };
        IntPtr cursor = CreateIconIndirect(ref info);
        int error = Marshal.GetLastWin32Error();
        DeleteObject(color);
        DeleteObject(mask);
        if (cursor == IntPtr.Zero) throw new InvalidOperationException($"CreateIconIndirect: {error}");
        return cursor;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct BITMAPV5HEADER
    {
        public uint bV5Size; public int bV5Width, bV5Height; public ushort bV5Planes, bV5BitCount;
        public uint bV5Compression, bV5SizeImage; public int bV5XPelsPerMeter, bV5YPelsPerMeter;
        public uint bV5ClrUsed, bV5ClrImportant, bV5RedMask, bV5GreenMask, bV5BlueMask, bV5AlphaMask, bV5CSType;
        public int e0, e1, e2, e3, e4, e5, e6, e7, e8;   // CIEXYZTRIPLE, unused
        public uint bV5GammaRed, bV5GammaGreen, bV5GammaBlue, bV5Intent, bV5ProfileData, bV5ProfileSize, bV5Reserved;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct ICONINFO { public bool fIcon; public int xHotspot, yHotspot; public IntPtr hbmMask, hbmColor; }

    // The size of the mapped view that starts at `view`.
    static ulong Mapped(IntPtr view)
    {
        if (VirtualQuery(view, out MEMORY_BASIC_INFORMATION info, (UIntPtr)Marshal.SizeOf<MEMORY_BASIC_INFORMATION>()) == UIntPtr.Zero)
            throw new InvalidOperationException($"VirtualQuery: {Marshal.GetLastWin32Error()}");
        return (ulong)info.RegionSize;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct MEMORY_BASIC_INFORMATION
    {
        public IntPtr BaseAddress, AllocationBase; public uint AllocationProtect; public ushort PartitionId;
        public UIntPtr RegionSize; public uint State, Protect, Type;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern UIntPtr VirtualQuery(IntPtr address, out MEMORY_BASIC_INFORMATION info, UIntPtr length);

    const uint FileMapRead = 0x0004;

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr MapViewOfFile(IntPtr mapping, uint access, uint offsetHigh, uint offsetLow, UIntPtr bytes);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool UnmapViewOfFile(IntPtr view);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr handle);
    [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr hwnd);
    [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr hwnd, IntPtr dc);
    [DllImport("gdi32.dll", SetLastError = true)]
    static extern IntPtr CreateDIBSection(IntPtr dc, ref BITMAPV5HEADER header, uint usage, out IntPtr bits, IntPtr section, uint offset);
    [DllImport("gdi32.dll")] static extern IntPtr CreateBitmap(int w, int h, uint planes, uint bitCount, IntPtr bits);
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr obj);
    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr CreateIconIndirect(ref ICONINFO info);
    [DllImport("user32.dll")] static extern bool DestroyCursor(IntPtr cursor);
}
