// camctl - read/write DirectShow camera controls (IAMCameraControl / IAMVideoProcAmp / KS) for AstroCam.
//   camctl list
//   camctl get    "<device>"                       -> JSON
//   camctl set    "<device>" Name=value[:auto|:manual] ...
//   camctl dialog "<device>"                       -> driver property pages
// Build: C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /optimize /out:camctl.exe camctl.cs
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

[ComImport, Guid("29840822-5B84-11D0-BD3B-00A0C911CE86"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface ICreateDevEnum { [PreserveSig] int CreateClassEnumerator([In] ref Guid clsid, out IEnumMoniker e, int flags); }

[ComImport, Guid("55272A00-42CB-11CE-8135-00AA004BB851"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyBag {
    [PreserveSig] int Read([MarshalAs(UnmanagedType.LPWStr)] string name, out object val, IntPtr log);
    [PreserveSig] int Write([MarshalAs(UnmanagedType.LPWStr)] string name, ref object val);
}

[ComImport, Guid("C6E13370-30AC-11d0-A18C-00A0C9118956"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAMCameraControl {
    [PreserveSig] int GetRange(int p, out int min, out int max, out int step, out int def, out int caps);
    [PreserveSig] int Set(int p, int v, int flags);
    [PreserveSig] int Get(int p, out int v, out int flags);
}

[ComImport, Guid("C6E13360-30AC-11d0-A18C-00A0C9118956"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAMVideoProcAmp {
    [PreserveSig] int GetRange(int p, out int min, out int max, out int step, out int def, out int caps);
    [PreserveSig] int Set(int p, int v, int flags);
    [PreserveSig] int Get(int p, out int v, out int flags);
}

[ComImport, Guid("28F54685-06FD-11D2-B27A-00A0C9223196"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IKsControl {
    [PreserveSig] int KsProperty(IntPtr prop, int propLen, IntPtr data, int dataLen, out int returned);
    [PreserveSig] int KsMethod(IntPtr m, int mLen, IntPtr data, int dataLen, out int returned);
    [PreserveSig] int KsEvent(IntPtr e, int eLen, IntPtr data, int dataLen, out int returned);
}

[ComImport, Guid("B196B28B-BAB4-101A-B69C-00AA00341D07"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface ISpecifyPropertyPages { [PreserveSig] int GetPages(out CAUUID pages); }

[StructLayout(LayoutKind.Sequential)]
struct CAUUID { public int cElems; public IntPtr pElems; }

static class Program {
    static readonly Guid CLSID_SystemDeviceEnum = new Guid("62BE5D10-60EB-11d0-BD3B-00A0C911CE86");
    static readonly Guid CLSID_VideoInputDeviceCategory = new Guid("860BB310-5D01-11d0-BD3B-00A0C911CE86");
    static readonly Guid IID_IBaseFilter = new Guid("56a86895-0ad4-11ce-b03a-0020af0ba770");
    static readonly Guid IID_IPropertyBag = new Guid("55272A00-42CB-11CE-8135-00AA004BB851");
    static readonly Guid PROPSETID_VIDCAP_CAMERACONTROL = new Guid("C6E13370-30AC-11d0-A18C-00A0C9118956");

    static readonly string[] CamNames = { "Pan", "Tilt", "Roll", "Zoom", "Exposure", "Iris", "Focus" };
    static readonly string[] AmpNames = { "Brightness", "Contrast", "Hue", "Saturation", "Sharpness", "Gamma",
                                          "ColorEnable", "WhiteBalance", "BacklightCompensation", "Gain" };
    const int KS_AUTO_EXPOSURE_PRIORITY = 19;   // KSPROPERTY_CAMERACONTROL_AUTO_EXPOSURE_PRIORITY

    [DllImport("oleaut32.dll", CharSet = CharSet.Unicode)]
    static extern int OleCreatePropertyFrame(IntPtr hwnd, int x, int y, string caption, int cObjects,
        [MarshalAs(UnmanagedType.Interface)] ref object ppUnk, int cPages, IntPtr pPageClsID, int lcid, int dwReserved, IntPtr pvReserved);

    [STAThread]
    static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        try {
            if (args.Length == 0) { Console.Error.WriteLine("usage: camctl list | get <dev> | set <dev> k=v[:auto|:manual].. | dialog <dev>"); return 2; }
            string cmd = args[0].ToLowerInvariant();
            if (cmd == "list") {
                var sb = new StringBuilder("[");
                bool first = true;
                foreach (var d in Devices()) { if (!first) sb.Append(","); first = false; sb.Append(Js(d.Key)); Marshal.ReleaseComObject(d.Value); }
                Console.WriteLine(sb.Append("]").ToString());
                return 0;
            }
            if (args.Length < 2) { Console.Error.WriteLine("device name required"); return 2; }
            object filter = Open(args[1]);
            if (filter == null) { Console.Error.WriteLine("device not found: " + args[1]); return 3; }
            try {
                if (cmd == "get") { Console.WriteLine(GetAll(filter)); return 0; }
                if (cmd == "set") {
                    int bad = 0;
                    for (int i = 2; i < args.Length; i++) if (!SetOne(filter, args[i])) bad++;
                    Console.WriteLine(GetAll(filter));
                    return bad == 0 ? 0 : 4;
                }
                if (cmd == "dialog") { ShowDialog(filter, args[1]); return 0; }
                Console.Error.WriteLine("unknown command " + cmd); return 2;
            } finally { Marshal.ReleaseComObject(filter); }
        } catch (Exception ex) {
            Console.Error.WriteLine(ex.GetType().Name + ": " + ex.Message);
            return 1;
        }
    }

    static IEnumerable<KeyValuePair<string, IMoniker>> Devices() {
        var t = Type.GetTypeFromCLSID(CLSID_SystemDeviceEnum);
        var de = (ICreateDevEnum)Activator.CreateInstance(t);
        IEnumMoniker em;
        Guid cat = CLSID_VideoInputDeviceCategory;
        if (de.CreateClassEnumerator(ref cat, out em, 0) != 0 || em == null) yield break;
        var ms = new IMoniker[1];
        while (em.Next(1, ms, IntPtr.Zero) == 0) {
            var m = ms[0];
            string name = null;
            try {
                object bagObj; Guid iid = IID_IPropertyBag;
                m.BindToStorage(null, null, ref iid, out bagObj);
                var bag = (IPropertyBag)bagObj;
                object v;
                if (bag.Read("FriendlyName", out v, IntPtr.Zero) == 0) name = v as string;
                Marshal.ReleaseComObject(bagObj);
            } catch { }
            yield return new KeyValuePair<string, IMoniker>(name ?? "?", m);
        }
        Marshal.ReleaseComObject(em);
    }

    static object Open(string device) {
        foreach (var d in Devices()) {
            if (string.Equals(d.Key, device, StringComparison.OrdinalIgnoreCase)) {
                object f; Guid iid = IID_IBaseFilter;
                d.Value.BindToObject(null, null, ref iid, out f);
                Marshal.ReleaseComObject(d.Value);
                return f;
            }
            Marshal.ReleaseComObject(d.Value);
        }
        return null;
    }

    // ---- KS property (for controls DirectShow does not wrap) ----
    // KSPROPERTY_CAMERACONTROL_S: { Guid Set; uint Id; uint Flags; int Value; uint Flags; uint Caps } = 36 bytes
    static bool KsCam(object filter, int id, bool set, ref int value, ref int flags) {
        var ks = filter as IKsControl;
        if (ks == null) return false;
        IntPtr p = Marshal.AllocHGlobal(36);
        try {
            byte[] z = new byte[36]; Marshal.Copy(z, 0, p, 36);
            Marshal.Copy(PROPSETID_VIDCAP_CAMERACONTROL.ToByteArray(), 0, p, 16);
            Marshal.WriteInt32(p, 16, id);
            Marshal.WriteInt32(p, 20, set ? 2 : 1);   // KSPROPERTY_TYPE_SET / GET
            Marshal.WriteInt32(p, 24, value);
            Marshal.WriteInt32(p, 28, flags);
            int ret;
            int hr = ks.KsProperty(p, 36, p, 36, out ret);
            if (hr != 0) return false;
            if (!set) { value = Marshal.ReadInt32(p, 24); flags = Marshal.ReadInt32(p, 28); }
            return true;
        } finally { Marshal.FreeHGlobal(p); }
    }

    static string GetAll(object filter) {
        var sb = new StringBuilder("{\"props\":[");
        bool first = true;
        var cc = filter as IAMCameraControl;
        var pa = filter as IAMVideoProcAmp;
        for (int g = 0; g < 2; g++) {
            string[] names = g == 0 ? CamNames : AmpNames;
            for (int i = 0; i < names.Length; i++) {
                int min, max, step, def, caps, v = 0, fl = 0, hr;
                if (g == 0) { if (cc == null) break; hr = cc.GetRange(i, out min, out max, out step, out def, out caps); if (hr == 0) cc.Get(i, out v, out fl); }
                else { if (pa == null) break; hr = pa.GetRange(i, out min, out max, out step, out def, out caps); if (hr == 0) pa.Get(i, out v, out fl); }
                if (hr != 0) continue;
                if (!first) sb.Append(","); first = false;
                sb.AppendFormat(CultureInfo.InvariantCulture,
                    "{{\"name\":{0},\"group\":\"{1}\",\"min\":{2},\"max\":{3},\"step\":{4},\"default\":{5},\"caps\":{6},\"value\":{7},\"auto\":{8}}}",
                    Js(names[i]), g == 0 ? "camera" : "procamp", min, max, step, def, caps, v, (fl & 1) != 0 ? "true" : "false");
            }
        }
        sb.Append("]");
        int aev = 0, aef = 0;
        if (KsCam(filter, KS_AUTO_EXPOSURE_PRIORITY, false, ref aev, ref aef))
            sb.AppendFormat(",\"ExposurePriority\":{0}", aev);
        return sb.Append("}").ToString();
    }

    static bool SetOne(object filter, string spec) {
        int eq = spec.IndexOf('=');
        if (eq < 1) { Console.Error.WriteLine("bad spec " + spec); return false; }
        string name = spec.Substring(0, eq);
        string rest = spec.Substring(eq + 1);
        int flags = 2;   // manual
        int colon = rest.IndexOf(':');
        if (colon >= 0) {
            string mode = rest.Substring(colon + 1).ToLowerInvariant();
            rest = rest.Substring(0, colon);
            if (mode == "auto") flags = 1;
        }
        int value;
        if (rest == "" || rest == "auto") { value = 0; flags = 1; }
        else if (!int.TryParse(rest, NumberStyles.Integer, CultureInfo.InvariantCulture, out value)) { Console.Error.WriteLine("bad value " + spec); return false; }

        int hr = -1;
        if (string.Equals(name, "ExposurePriority", StringComparison.OrdinalIgnoreCase)) {
            int f = 2;
            hr = KsCam(filter, KS_AUTO_EXPOSURE_PRIORITY, true, ref value, ref f) ? 0 : -1;
        } else {
            int ci = Array.FindIndex(CamNames, n => string.Equals(n, name, StringComparison.OrdinalIgnoreCase));
            int ai = Array.FindIndex(AmpNames, n => string.Equals(n, name, StringComparison.OrdinalIgnoreCase));
            if (ci >= 0) {
                var cc = filter as IAMCameraControl;
                if (cc != null) {
                    if (flags == 1) { int cur, cf; cc.Get(ci, out cur, out cf); value = cur; }
                    hr = cc.Set(ci, value, flags);
                }
            } else if (ai >= 0) {
                var pa = filter as IAMVideoProcAmp;
                if (pa != null) {
                    if (flags == 1) { int cur, cf; pa.Get(ai, out cur, out cf); value = cur; }
                    hr = pa.Set(ai, value, flags);
                }
            } else { Console.Error.WriteLine("unknown control " + name); return false; }
        }
        if (hr != 0) Console.Error.WriteLine(string.Format("set {0} failed: 0x{1:X8}", spec, hr));
        return hr == 0;
    }

    static void ShowDialog(object filter, string caption) {
        var spp = filter as ISpecifyPropertyPages;
        if (spp == null) throw new Exception("device has no property pages");
        CAUUID pages;
        Marshal.ThrowExceptionForHR(spp.GetPages(out pages));
        try {
            object o = filter;
            Marshal.ThrowExceptionForHR(OleCreatePropertyFrame(IntPtr.Zero, 0, 0, caption, 1, ref o, pages.cElems, pages.pElems, 0, 0, IntPtr.Zero));
        } finally { Marshal.FreeCoTaskMem(pages.pElems); }
    }

    static string Js(string s) {
        var sb = new StringBuilder("\"");
        foreach (char c in s) {
            if (c == '"' || c == '\\') sb.Append('\\').Append(c);
            else if (c < 32) sb.AppendFormat("\\u{0:x4}", (int)c);
            else sb.Append(c);
        }
        return sb.Append('"').ToString();
    }
}

