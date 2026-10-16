"""Pure-ctypes WASAPI loopback recorder. No third-party audio deps needed.

Usage:
    python wasapi_loopback.py <seconds> <out_json>
    python wasapi_loopback.py --selftest

Captures the default RENDER device via AUDCLNT_STREAMFLAGS_LOOPBACK and reports
windowed RMS (0.1 s windows) exactly like a01_loopback.py's contract.
"""
import ctypes
import ctypes.wintypes as wt
import json
import sys
import threading
import time

# --- GUIDs (bytes_le) ---
def guid(s):
    import uuid
    return uuid.UUID(s).bytes_le

CLSID_MMDeviceEnumerator = guid('BCDE0395-E52F-467C-8E3D-C4579291692E')
IID_IMMDeviceEnumerator  = guid('A95664D2-9614-4F35-A746-DE8DB63617E6')
IID_IMMDevice            = guid('D666063F-1587-4E43-81F1-B948E807363F')
IID_IAudioClient         = guid('1CB9AD4C-DBFA-4C32-B178-C2F568A703B2')
IID_IAudioCaptureClient  = guid('C8ADBD64-E71E-48A0-A4DE-185C395CD317')

HRESULT = ctypes.HRESULT
POINTER = ctypes.POINTER
ole32 = ctypes.WinDLL('ole32')

def vtbl_fn(iface, index, proto):
    """Read the vtable pointer of a COM interface pointer and wrap slot `index`."""
    vtbl_ptr = ctypes.cast(iface, ctypes.POINTER(ctypes.c_void_p))[0]
    fn = ctypes.cast(vtbl_ptr, ctypes.POINTER(ctypes.c_void_p))[index]
    return proto(fn)

PVOID = ctypes.c_void_p

# IMMDeviceEnumerator.GetDefaultAudioEndpoint = vtable slot 4
_GetDefaultAudioEndpoint = ctypes.WINFUNCTYPE(HRESULT, PVOID, ctypes.c_int, ctypes.c_int, POINTER(PVOID))
# IMMDevice.Activate = slot 3
_Activate = ctypes.WINFUNCTYPE(HRESULT, PVOID, POINTER(ctypes.c_ubyte), ctypes.c_uint32, PVOID, POINTER(PVOID))
# IAudioClient: Initialize=3 GetBufferSize=4 ... Start=10 Stop=11 ... GetService=14 GetMixFormat=8
_Initialize = ctypes.WINFUNCTYPE(HRESULT, PVOID, ctypes.c_int, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_uint64, PVOID, PVOID)
_GetMixFormat = ctypes.WINFUNCTYPE(HRESULT, PVOID, POINTER(PVOID))
_Start = ctypes.WINFUNCTYPE(HRESULT, PVOID)
_Stop = ctypes.WINFUNCTYPE(HRESULT, PVOID)
_GetService = ctypes.WINFUNCTYPE(HRESULT, PVOID, POINTER(ctypes.c_ubyte), POINTER(PVOID))
# IAudioCaptureClient: GetBuffer=3 ReleaseBuffer=4 GetNextPacketSize=5
_GetBuffer = ctypes.WINFUNCTYPE(HRESULT, PVOID, POINTER(PVOID), POINTER(ctypes.c_uint32), POINTER(ctypes.c_uint32), POINTER(ctypes.c_uint64), POINTER(ctypes.c_uint32))
_ReleaseBuffer = ctypes.WINFUNCTYPE(HRESULT, PVOID, ctypes.c_uint32)
_GetNextPacketSize = ctypes.WINFUNCTYPE(HRESULT, PVOID, POINTER(ctypes.c_uint32))

class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wt.WORD), ("nChannels", wt.WORD), ("nSamplesPerSec", wt.DWORD),
                ("nAvgBytesPerSec", wt.DWORD), ("nBlockAlign", wt.WORD), ("wBitsPerSample", wt.WORD),
                ("cbSize", wt.WORD)]

def record_loopback(seconds, window_cb=None, stop_event=None):
    """Return (peak, avg, windows, active, device, rate, fmt) recording default output."""
    ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED? 0 = APARTMENTTHREADED; either works here
    enumerator = PVOID()
    cls = (ctypes.c_ubyte * 16).from_buffer_copy(CLSID_MMDeviceEnumerator)
    iid = (ctypes.c_ubyte * 16).from_buffer_copy(IID_IMMDeviceEnumerator)
    hr = ole32.CoCreateInstance(ctypes.byref(cls), None, 1, ctypes.byref(iid), ctypes.byref(enumerator))  # CLSCTX_INPROC_SERVER=1
    assert hr == 0, 'CoCreateInstance hr=0x%08X' % (hr & 0xFFFFFFFF)
    dev = PVOID()
    hr = vtbl_fn(enumerator, 4, _GetDefaultAudioEndpoint)(enumerator, 0, 0, ctypes.byref(dev))  # eRender, eConsole
    assert hr == 0, 'GetDefaultAudioEndpoint hr=0x%08X' % (hr & 0xFFFFFFFF)
    client = PVOID()
    iid_ac = (ctypes.c_ubyte * 16).from_buffer_copy(IID_IAudioClient)
    clsctx_all = 4
    hr = vtbl_fn(dev, 3, _Activate)(dev, iid_ac, clsctx_all, None, ctypes.byref(client))
    assert hr == 0, 'Activate hr=0x%08X' % (hr & 0xFFFFFFFF)
    fmt_ptr = PVOID()
    hr = vtbl_fn(client, 8, _GetMixFormat)(client, ctypes.byref(fmt_ptr))
    assert hr == 0, 'GetMixFormat hr=0x%08X' % (hr & 0xFFFFFFFF)
    fmt = ctypes.cast(fmt_ptr, POINTER(WAVEFORMATEX))[0]
    is_float = (fmt.wFormatTag == 3) or (fmt.wFormatTag == 0xFFFE and fmt.wBitsPerSample == 32)
    # AUDCLNT_SHAREMODE_SHARED=0, AUDCLNT_STREAMFLAGS_LOOPBACK=0x00020000, 1s buffer
    hr = vtbl_fn(client, 3, _Initialize)(client, 0, 0x00020000, 10000000, 0, fmt_ptr, None)
    assert hr == 0, 'Initialize hr=0x%08X' % (hr & 0xFFFFFFFF)
    capture = PVOID()
    iid_cc = (ctypes.c_ubyte * 16).from_buffer_copy(IID_IAudioCaptureClient)
    hr = vtbl_fn(client, 14, _GetService)(client, iid_cc, ctypes.byref(capture))
    assert hr == 0, 'GetService hr=0x%08X' % (hr & 0xFFFFFFFF)
    hr = vtbl_fn(client, 10, _Start)(client)
    assert hr == 0, 'Start hr=0x%08X' % (hr & 0xFFFFFFFF)

    import numpy as np
    rms_windows = []
    t0 = time.time()
    next_window = 0.0
    buf_all = bytearray()
    try:
        while time.time() - t0 < seconds and not (stop_event and stop_event.is_set()):
            time.sleep(0.02)
            packets = ctypes.c_uint32(0)
            while True:
                hr = vtbl_fn(capture, 5, _GetNextPacketSize)(capture, ctypes.byref(packets))
                if hr != 0 or packets.value == 0:
                    break
                data_ptr = PVOID(); nframes = ctypes.c_uint32(0); flags = ctypes.c_uint32(0)
                dev_pos = ctypes.c_uint64(0); qpc = ctypes.c_uint32(0)
                hr = vtbl_fn(capture, 3, _GetBuffer)(capture, ctypes.byref(data_ptr), ctypes.byref(nframes),
                                                     ctypes.byref(flags), ctypes.byref(dev_pos), ctypes.byref(qpc))
                if hr != 0 or not data_ptr.value:
                    break
                nbytes = nframes.value * fmt.nBlockAlign
                raw = ctypes.string_at(data_ptr.value, nbytes)
                buf_all += raw
                vtbl_fn(capture, 4, _ReleaseBuffer)(capture, nframes.value)
            now = time.time() - t0
            if now >= next_window:
                chunk = bytes(buf_all)
                if chunk:
                    if is_float:
                        arr = np.frombuffer(chunk, dtype=np.float32)
                    else:
                        arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    mono = arr.reshape(-1, fmt.nChannels).mean(axis=1)
                    rms = float(np.sqrt((mono ** 2).mean()))
                    rms_windows.append((round(now, 2), rms))
                next_window += 0.1
            if window_cb:
                window_cb(time.time() - t0)
    finally:
        vtbl_fn(client, 11, _Stop)(client)
    if not rms_windows:
        return None
    vals = [w[1] for w in rms_windows]
    return {"peakRms": round(max(vals), 6), "avgRms": round(sum(vals) / len(vals), 6),
            "activeWindows": sum(1 for v in vals if v > 0.002), "windows": len(rms_windows),
            "seconds": seconds, "fmt": ('float32' if is_float else 'int%d' % fmt.wBitsPerSample),
            "rate": fmt.nSamplesPerSec, "channels": fmt.nChannels, "ts": time.strftime('%H:%M:%S')}

def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--selftest':
        import numpy as np
        import sounddevice as sd
        result = {}
        def run():
            try:
                result['r'] = record_loopback(3.0)
            except Exception:
                import traceback
                traceback.print_exc()
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.6)
        sr = 48000
        tone = (0.25 * np.sin(2 * np.pi * 440 * np.arange(sr * 1.2) / sr)).astype(np.float32)
        stereo = np.stack([tone, tone], axis=1)
        sd.play(stereo, sr)
        sd.wait()
        t.join()
        print(json.dumps(result['r']))
        json.dump(result['r'], open('runtime/tests/_loopback_selftest.json', 'w'))
        ok = result['r'] and result['r']['peakRms'] > 0.01 and result['r']['activeWindows'] >= 8
        print('SELFTEST', 'PASS' if ok else 'FAIL')
        sys.exit(0 if ok else 1)
    secs = float(sys.argv[1]); out = sys.argv[2]
    r = record_loopback(secs)
    json.dump(r, open(out, 'w'))
    print(json.dumps(r))

if __name__ == '__main__':
    main()
