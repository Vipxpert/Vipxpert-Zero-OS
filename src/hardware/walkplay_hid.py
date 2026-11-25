# /vipxpert_minimal_os/src/hardware/walkplay_hid.py
"""
Offline Walkplay / KTMicro USB HID Driver (Zero Login, Zero Cloud).
Supports BGVP MX1 (3302:43EB) and compatible KTMicro 0x4B/0x54 64-byte HID USB-C DACs.
"""
import glob
import math
import os
import select
import struct
from typing import Dict, List, Optional, Tuple

REPORT_ID = 0x4B
FLAG_WRITE = 0x01
FLAG_READ = 0x80

CMD_FLASH_COMMIT = 0x01
CMD_PREAMP = 0x03
CMD_PEQ_BAND = 0x09
CMD_FW_VERSION = 0x0C
CMD_DAC_FILTER = 0x11
CMD_PEQ_ENABLE = 0x18
CMD_GAIN_MODE = 0x1D

# Filter types in byte 34 of CMD 0x09
FTYPE_LOW_SHELF = 1
FTYPE_PEAKING = 2
FTYPE_HIGH_SHELF = 3

WALKPLAY_VIDS = {"3302", "31b2", "2fc6", "0416"}

# Curated offline PEQ presets: list of (freq_hz, q, gain_db, ftype)
WALKPLAY_PRESETS: Dict[str, List[Tuple[int, float, float, int]]] = {
    "flat": [
        (32, 0.75, 0.0, FTYPE_PEAKING),
        (64, 0.75, 0.0, FTYPE_PEAKING),
        (125, 0.75, 0.0, FTYPE_PEAKING),
        (250, 0.75, 0.0, FTYPE_PEAKING),
        (500, 0.75, 0.0, FTYPE_PEAKING),
        (1000, 0.75, 0.0, FTYPE_PEAKING),
        (2000, 0.75, 0.0, FTYPE_PEAKING),
        (4000, 0.75, 0.0, FTYPE_PEAKING),
    ],
    "bass": [
        (60, 0.70, 3.5, FTYPE_LOW_SHELF),
        (140, 1.00, 1.5, FTYPE_PEAKING),
        (300, 0.80, -0.5, FTYPE_PEAKING),
        (1000, 0.75, 0.0, FTYPE_PEAKING),
        (2500, 0.75, 0.0, FTYPE_PEAKING),
        (5000, 0.75, 0.0, FTYPE_PEAKING),
        (8000, 0.75, 0.5, FTYPE_PEAKING),
        (12000, 0.70, 0.0, FTYPE_HIGH_SHELF),
    ],
    "vocal": [
        (60, 0.70, -1.0, FTYPE_LOW_SHELF),
        (200, 1.00, -1.0, FTYPE_PEAKING),
        (1200, 1.20, 1.5, FTYPE_PEAKING),
        (2800, 1.40, 2.5, FTYPE_PEAKING),
        (4500, 1.50, 1.0, FTYPE_PEAKING),
        (8000, 0.75, 0.0, FTYPE_PEAKING),
        (10000, 0.75, 0.0, FTYPE_PEAKING),
        (12000, 0.70, 0.5, FTYPE_HIGH_SHELF),
    ],
    "warm": [
        (80, 0.70, 2.0, FTYPE_LOW_SHELF),
        (220, 0.90, 1.0, FTYPE_PEAKING),
        (1000, 0.75, 0.0, FTYPE_PEAKING),
        (3000, 1.20, -1.0, FTYPE_PEAKING),
        (6000, 1.40, -1.5, FTYPE_PEAKING),
        (9000, 0.70, -1.5, FTYPE_HIGH_SHELF),
        (12000, 0.75, 0.0, FTYPE_PEAKING),
        (15000, 0.75, 0.0, FTYPE_PEAKING),
    ],
    "air": [
        (40, 0.70, 1.0, FTYPE_LOW_SHELF),
        (250, 0.75, 0.0, FTYPE_PEAKING),
        (1000, 0.75, 0.0, FTYPE_PEAKING),
        (3500, 1.20, 0.5, FTYPE_PEAKING),
        (7500, 1.50, 1.5, FTYPE_PEAKING),
        (11000, 0.70, 3.0, FTYPE_HIGH_SHELF),
        (14000, 0.75, 1.0, FTYPE_PEAKING),
        (16000, 0.75, 0.0, FTYPE_PEAKING),
    ],
}


def _compute_biquad_q30(freq: float, q: float, gain_db: float, ftype: int = FTYPE_PEAKING, fs: float = 96000.0) -> Tuple[int, int, int, int, int]:
    """Computes Audio EQ Cookbook biquad coefficients quantized to signed Q2.30 (matching KTMicro/Walkplay DSP)."""
    freq = max(16.0, min(fs * 0.45, float(freq)))
    q = max(0.1, min(16.0, float(q)))
    gain_db = max(-12.0, min(12.0, float(gain_db)))
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * freq / fs
    cw = math.cos(w0)
    sw = math.sin(w0)
    alpha = sw / (2.0 * q)

    if ftype == FTYPE_LOW_SHELF:
        s2a = 2.0 * math.sqrt(A) * alpha
        b0 = A * ((A + 1.0) - (A - 1.0) * cw + s2a)
        b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * cw)
        b2 = A * ((A + 1.0) - (A - 1.0) * cw - s2a)
        a0 = (A + 1.0) + (A - 1.0) * cw + s2a
        a1 = -2.0 * ((A - 1.0) + (A + 1.0) * cw)
        a2 = (A + 1.0) + (A - 1.0) * cw - s2a
    elif ftype == FTYPE_HIGH_SHELF:
        s2a = 2.0 * math.sqrt(A) * alpha
        b0 = A * ((A + 1.0) + (A - 1.0) * cw + s2a)
        b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cw)
        b2 = A * ((A + 1.0) + (A - 1.0) * cw - s2a)
        a0 = (A + 1.0) - (A - 1.0) * cw + s2a
        a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cw)
        a2 = (A + 1.0) - (A - 1.0) * cw - s2a
    else:
        b0 = 1.0 + alpha * A
        b1 = -2.0 * cw
        b2 = 1.0 - alpha * A
        a0 = 1.0 + alpha / A
        a1 = -2.0 * cw
        a2 = 1.0 - alpha / A

    # KTMicro DSP uses negated feedback signs (-a1/a0, -a2/a0) scaled by 2^30
    scale = float(1 << 30)
    clamp = lambda v: max(-2147483648, min(2147483647, int(round(v * scale))))
    return (
        clamp(b0 / a0),
        clamp(b1 / a0),
        clamp(b2 / a0),
        clamp(-a1 / a0),
        clamp(-a2 / a0),
    )


def discover_walkplay_device() -> Optional[Dict[str, str]]:
    """Scans /sys/class/hidraw/hidraw* for a connected Walkplay / KTMicro 0x4B USB DAC."""
    for sys_dir in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        uevent_p = os.path.join(sys_dir, "device", "uevent")
        desc_p = os.path.join(sys_dir, "device", "report_descriptor")
        if not os.path.exists(uevent_p):
            continue
        try:
            info = {}
            with open(uevent_p, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        info[k] = v
            hid_id = info.get("HID_ID", "")
            parts = hid_id.split(":")
            vid = parts[1][-4:].lower() if len(parts) >= 3 else ""
            pid = parts[2][-4:].lower() if len(parts) >= 3 else ""
            has_4b = False
            if os.path.exists(desc_p):
                with open(desc_p, "rb") as df:
                    raw_desc = df.read()
                # Vendor-defined Report ID 0x4B (85 4b) is the signature of KTMicro/Walkplay HID
                has_4b = b"\x85\x4b" in raw_desc
            if vid in WALKPLAY_VIDS or has_4b:
                dev_node = "/dev/" + os.path.basename(sys_dir)
                if os.path.exists(dev_node):
                    return {
                        "path": dev_node,
                        "name": info.get("HID_NAME", f"Walkplay {vid}:{pid}"),
                        "vid": vid,
                        "pid": pid,
                    }
        except Exception:
            continue
    return None


class WalkplayHID:
    """Thread-safe short-lived /dev/hidraw* session for offline Walkplay/KTMicro DAC control."""

    def __init__(self, path: str):
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None

    def _xfer(self, payload: List[int], timeout: float = 0.15) -> bytes:
        if self.fd is None:
            return b""
        # Drain stale unread reports before issuing a new command
        while select.select([self.fd], [], [], 0.0)[0]:
            try:
                os.read(self.fd, 64)
            except OSError:
                break
        pkt = bytes(payload) + b"\x00" * (64 - len(payload))
        os.write(self.fd, pkt)
        if select.select([self.fd], [], [], timeout)[0]:
            return os.read(self.fd, 64)
        return b""

    def read_firmware(self) -> str:
        r = self._xfer([REPORT_ID, FLAG_READ, CMD_FW_VERSION, 0x00])
        if len(r) >= 8:
            raw = r[4:16].split(b"\x00", 1)[0]
            return raw.decode("ascii", errors="ignore").strip() or "unknown"
        return "unknown"

    def _write(self, payload: List[int]) -> bool:
        """Writes a 64-byte KTMicro HID command (BGVP MX1 executes FLAG_WRITE without returning an ACK report)."""
        if self.fd is None:
            return False
        pkt = bytes(payload) + b"\x00" * (64 - len(payload))
        n = os.write(self.fd, pkt)
        # Brief 12ms settle so consecutive writes never overrun the HID endpoint
        select.select([self.fd], [], [], 0.012)
        while select.select([self.fd], [], [], 0.0)[0]:
            try:
                os.read(self.fd, 64)
            except OSError:
                break
        return n == 64

    def read_preamp(self) -> float:
        # Comment (invariant): CMD 0x03 stores per-device PEQ Preamp as signed Q8.8 dB (int16 LE / 256.0) at bytes 4..5.
        r = self._xfer([REPORT_ID, FLAG_READ, CMD_PREAMP, 0x00])
        if len(r) >= 6:
            return round(struct.unpack_from("<h", r, 4)[0] / 256.0, 2)
        return 0.0

    def set_preamp(self, db: float) -> bool:
        clamped = max(-12.0, min(6.0, float(db)))
        raw = int(round(clamped * 256.0))
        lo, hi = struct.pack("<h", raw)
        return self._write([REPORT_ID, FLAG_WRITE, CMD_PREAMP, 0x02, lo, hi])

    def read_dac_filter(self) -> int:
        r = self._xfer([REPORT_ID, FLAG_READ, CMD_DAC_FILTER, 0x00])
        return int(r[4]) if len(r) >= 5 else 1

    def set_dac_filter(self, mode: int) -> bool:
        return self._write([REPORT_ID, FLAG_WRITE, CMD_DAC_FILTER, 0x00, int(mode) & 0xFF])

    def read_gain_high(self) -> bool:
        # Comment (decision): CMD 0x1D toggles High (1) vs Low (0) Gain on BGVP MX1; never auto-reapplied on reconnect for hearing safety.
        r = self._xfer([REPORT_ID, FLAG_READ, CMD_GAIN_MODE, 0x00])
        return bool(len(r) >= 5 and r[4] != 0)

    def set_gain_high(self, high: bool) -> bool:
        return self._write([REPORT_ID, FLAG_WRITE, CMD_GAIN_MODE, 0x00, 1 if high else 0])

    def read_peq_enable(self) -> bool:
        r = self._xfer([REPORT_ID, FLAG_READ, CMD_PEQ_ENABLE, 0x00])
        return bool(len(r) >= 5 and r[4] != 0)

    def set_peq_enable(self, enabled: bool) -> bool:
        return self._write([REPORT_ID, FLAG_WRITE, CMD_PEQ_ENABLE, 0x00, 1 if enabled else 0])

    def read_band(self, band: int) -> Optional[Dict]:
        # Comment (invariant): On BGVP MX1 / KTMicro 0x4B protocol, byte 4 (not byte 3) is the PEQ band index on both READ and WRITE.
        r = self._xfer([REPORT_ID, FLAG_READ, CMD_PEQ_BAND, 0x00, band & 0xFF])
        if len(r) < 35:
            return None
        freq = struct.unpack_from("<H", r, 28)[0]
        q_val = round(struct.unpack_from("<H", r, 30)[0] / 256.0, 3)
        gain_db = round(struct.unpack_from("<h", r, 32)[0] / 256.0, 2)
        ftype = r[34]
        return {"band": band, "freq": freq, "q": q_val, "gain": gain_db, "ftype": ftype}

    def write_band(self, band: int, freq: int, q: float, gain_db: float, ftype: int = FTYPE_PEAKING) -> bool:
        """Writes a single PEQ band into volatile DAC RAM (live audition, does NOT burn flash)."""
        b0, b1, b2, a1, a2 = _compute_biquad_q30(freq, q, gain_db, ftype=ftype)
        buf = bytearray(64)
        buf[0] = REPORT_ID
        buf[1] = FLAG_WRITE
        buf[2] = CMD_PEQ_BAND
        buf[3] = 0x00
        buf[4] = band & 0xFF
        struct.pack_into("<iiiii", buf, 8, b0, b1, b2, a1, a2)
        struct.pack_into("<HHh", buf, 28, int(max(16, min(20000, freq))), int(round(q * 256.0)), int(round(gain_db * 256.0)))
        buf[34] = ftype & 0xFF
        return self._write(list(buf))

    def apply_preset(self, preset_name: str) -> bool:
        bands = WALKPLAY_PRESETS.get(preset_name)
        if not bands:
            return False
        ok = True
        for idx, (freq, q, gain_db, ftype) in enumerate(bands):
            if not self.write_band(idx, freq, q, gain_db, ftype):
                ok = False
        if preset_name != "flat":
            self.set_peq_enable(True)
        return ok

    def save_to_flash(self) -> bool:
        """Commits current RAM PEQ & filter settings to DAC internal NVRAM."""
        return self._write([REPORT_ID, FLAG_WRITE, CMD_FLASH_COMMIT, 0x00, 0x01])


def save_walkplay_profile(db, dev: Dict[str, str], dac_filter: Optional[int] = None, peq_en: Optional[bool] = None, bands: Optional[List[Dict]] = None, preamp: Optional[float] = None):
    """Persists remembered Filter + Preamp + PEQ settings per USB VID:PID in SQLite (Gain is intentionally excluded)."""
    import json
    if not db or not dev:
        return
    key = f"wp_prof_{dev.get('vid', '0000')}_{dev.get('pid', '0000')}"
    try:
        raw = db.get_setting(key, "")
        prof = json.loads(raw) if raw else {}
    except Exception:
        prof = {}
    if dac_filter is not None:
        prof["filter"] = int(dac_filter)
    if preamp is not None:
        prof["preamp"] = round(float(preamp), 2)
    if peq_en is not None:
        prof["peq_en"] = bool(peq_en)
    if bands is not None:
        prof["bands"] = [{"freq": b["freq"], "q": b["q"], "gain": b["gain"], "ftype": b["ftype"]} for b in bands[:8]]
    db.set_setting(key, json.dumps(prof))


def reapply_walkplay_profile(db, dev: Optional[Dict[str, str]] = None) -> bool:
    """Re-applies remembered Filter, Preamp & PEQ on USB DAC connect; intentionally leaves Gain mode untouched."""
    import json
    if not db:
        return False
    dev = dev or discover_walkplay_device()
    if not dev:
        return False
    key = f"wp_prof_{dev.get('vid', '0000')}_{dev.get('pid', '0000')}"
    raw = db.get_setting(key, "")
    if not raw:
        return False
    try:
        prof = json.loads(raw)
        with WalkplayHID(dev["path"]) as wp:
            if "filter" in prof:
                wp.set_dac_filter(int(prof["filter"]))
            if "preamp" in prof:
                wp.set_preamp(float(prof["preamp"]))
            if "bands" in prof and isinstance(prof["bands"], list):
                for idx, b in enumerate(prof["bands"][:8]):
                    wp.write_band(idx, int(b["freq"]), float(b["q"]), float(b["gain"]), int(b.get("ftype", FTYPE_PEAKING)))
            if "peq_en" in prof:
                wp.set_peq_enable(bool(prof["peq_en"]))
        return True
    except Exception:
        return False
