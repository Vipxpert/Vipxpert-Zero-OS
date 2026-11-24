# /vipxpert_minimal_os/src/hardware/display.py
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106, ssd1306
from luma.core.render import canvas
from PIL import Image, ImageDraw, ImageFont, ImageOps
import os
import json
import re
import time
import threading
import queue
import mmap
import struct

import multiprocessing
# Shared cross-process I2C0 hardware bus lock (Pins 3 SDA & 5 SCL: OLED 0x3C, UPS 0x43, PCM5122 DAC 0x4C)
I2C_BUS_LOCK = multiprocessing.RLock()

class Display:
    def __init__(self, port=None):
        # Enforce a strict 1-frame hardware buffer to completely bypass the ThreadPool
        self.frame_queue = queue.Queue(maxsize=1)
        self.bus_lock = I2C_BUS_LOCK
        self.invert_mode = False
        self.is_asleep = False
        self.device = None
        self.serial = None
        self.port = 0
        self.driver_name = "Unknown"
        self._consecutive_errors = 0
        self._last_recovery_time = 0.0
        self.contrast_level = 255

        # Auto-hunt for the correct I2C bus (Prioritize 0 for shared I2C0 bus on Pins 3/5, fallback to 3 or any available)
        if port is not None:
            candidate_buses = [port]
        else:
            try:
                active_buses = sorted([int(f.split('-')[1]) for f in os.listdir('/dev') if f.startswith('i2c-')])
            except Exception:
                active_buses = [0]
            candidate_buses = []
            for preferred in (0, 3):
                if preferred in active_buses:
                    candidate_buses.append(preferred)
            for b in active_buses:
                if b not in candidate_buses:
                    candidate_buses.append(b)
            if not candidate_buses:
                candidate_buses = [0]

        with self.bus_lock:
            self._write_twi0_ccr(0x28)
            for bus_id in candidate_buses:
                try:
                    serial_candidate = i2c(port=bus_id, address=0x3C)
                    try:
                        self.device = sh1106(serial_candidate)
                        driver_name = "SH1106"
                    except Exception:
                        self.device = ssd1306(serial_candidate)
                        driver_name = "SSD1306"
                    self.serial = serial_candidate
                    self.port = bus_id
                    self.driver_name = driver_name
                    self.target_fps = 20
                    self._overdrive_ccr = 0x90
                    self.device.command(0xD5, 0xF0)
                    self.device.command(0xD9, 0x11)
                    self._write_twi0_ccr(self._overdrive_ccr)
                    print(f"[Display] OLED ({driver_name}) initialized on I2C bus {bus_id} (0x3C).")
                    break
                except Exception as e:
                    self.device = None
                    print(f"[Display] Probe failed on I2C bus {bus_id} ({e}).")

        if not self.device:
            print("[Display] Initial I2C0 probe failed. Running hardware pin-reset recovery...", flush=True)
            self.port = 0
            self.driver_name = "SH1106"
            self.image = Image.new("1", (128, 64))
            self.reset_pins_and_recover()
        if not self.device:
            print("[Display] CRITICAL: Screen unresponsive across all I2C buses. Running headless.")

        # Define fallback canvas dimensions if device fails to initialize
        width = self.device.width if self.device else 128
        height = self.device.height if self.device else 64

        # State-driven offscreen rendering buffer + Isolated 125Hz Dual-Plane Hardware PWM Gray masks
        self.image = Image.new("1", (width, height))
        self.draw = ImageDraw.Draw(self.image)
        self.pwm_mask_dim = Image.new("1", (width, height), 0)   # ~50% duty (1 ON, 1 OFF at 125Hz = 62.5Hz carrier)
        self.pwm_draw_dim = ImageDraw.Draw(self.pwm_mask_dim)
        self.pwm_mask_mid = Image.new("1", (width, height), 0)   # ~67% duty (2 ON, 1 OFF at 125Hz)
        self.pwm_draw_mid = ImageDraw.Draw(self.pwm_mask_mid)
        self._has_pwm_gray = False

        # Font Engine Setup
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.font_path = os.path.join(repo_root, "fonts", "fusion-pixel.ttf")
        if not os.path.exists(self.font_path):
            self.font_path = "/home/vipxpert/vipxpert_minimal_os/fonts/fusion-pixel.ttf"
        self.font_small = ImageFont.load_default()
        self.font_large = self.font_small
        self.font_icons = self.font_small

        if os.path.exists(self.font_path):
            try:
                self.font_small = ImageFont.truetype(self.font_path, 12)
                self.font_large = ImageFont.truetype(self.font_path, 24)
                self.font_icons = ImageFont.truetype(self.font_path, 14)
            except Exception as e:
                print(f"[Display] Font load failed: {e}. Falling back to bitmap.")

        # Double-buffered 12KB lock-free mmap (Slot 0 @ 64..3136, Slot 1 @ 3136..6208 for ph0/ph1/lo planes)
        sfd = os.open("/dev/shm/vipxpert_oled_shm", os.O_CREAT | os.O_RDWR, 0o666)
        os.ftruncate(sfd, 12288)
        self._shm = mmap.mmap(sfd, 12288, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        os.close(sfd)
        # Comment (invariant): Load user-calibrated oled_pwm_dt from vipxpert_os.db (calibrated via Settings -> PWM Sync Cal to stop the slow rolling scan band); never overwrite this DB value from code.
        init_dt = 0.006451948
        self.gray_mode = "hybrid"
        self.dither_algo = "bayer8"
        self.dither_lo = 42
        self.dither_hi = 70
        self.dither_ramp = True
        try:
            import sqlite3 as _sq
            with _sq.connect("/home/vipxpert/vipxpert_minimal_os/vipxpert_os.db") as _c:
                _rows = dict(_c.execute(
                    "SELECT key, value FROM settings WHERE key IN ('oled_pwm_dt','oled_gray_mode','oled_dither_algo','oled_dither_lo','oled_dither_hi','oled_dither_ramp');"
                ).fetchall())
                if "oled_pwm_dt" in _rows and 0.004 < float(_rows["oled_pwm_dt"]) < 0.012:
                    init_dt = float(_rows["oled_pwm_dt"])
                if _rows.get("oled_gray_mode") in ("hybrid", "pwm", "dithering", "off"):
                    self.gray_mode = _rows["oled_gray_mode"]
                if _rows.get("oled_dither_algo") in ("bayer8", "bayer4", "bayer2", "cluster", "ign", "floyd", "temporal"):
                    self.dither_algo = _rows["oled_dither_algo"]
                if "oled_dither_lo" in _rows:
                    self.dither_lo = max(4, min(96, int(_rows["oled_dither_lo"])))
                if "oled_dither_hi" in _rows:
                    self.dither_hi = max(4, min(96, int(_rows["oled_dither_hi"])))
                if "oled_dither_ramp" in _rows:
                    self.dither_ramp = (_rows["oled_dither_ramp"] != "0")
        except Exception:
            pass
        self._pwm_base_dt = init_dt
        # Comment (invariant): shm[32:36] is reserved for Display's I2C0 pin-reset counter (rcnt) watched by UIManager._check_and_heal_pcm5122(); store adaptive Hybrid PWM byte budget in shm[36:40] so budget updates never trigger false 18..132ms PCM5122 re-latch stalls.
        self._shm[0:40] = struct.pack("<IIIIIIdII", 0, 0, 0, 0, 0, 0, init_dt, 0, 144)
        self._shm_write_slot = 0
        try:
            os.sched_setaffinity(0, {0, 2, 3})
        except Exception:
            pass
        self._parent_pid = os.getpid()
        self._render_thread = multiprocessing.Process(
            target=self._hardware_render_loop,
            daemon=True,
            name="OLED_Compositor"
        )
        self._render_thread.start()

    def reset_pins_and_recover(self) -> bool:
        """
        Actively un-sticks and resets Allwinner H618 I2C0 pins (PI5=SCL, PI6=SDA) and reinitializes the OLED:
        1. Configures PI5 & PI6 as GPIO outputs and drives both LOW for 10ms to discharge the bus.
        2. Clocks 16 SCL pulses on PI5 at ~50kHz with PI6 pulled high to release any stuck slave state.
        3. Issues a clean I2C STOP condition and restores pin multiplexing to Alt 5 (TWI0).
        4. Soft-resets the TWI0 controller engine via SRST register (0x0500201C).
        5. Re-instantiates the luma.oled driver (SH1106 / SSD1306), restores contrast, and displays active frame.
        """
        with self.bus_lock:
            # Stage 1: Hardware Pin-Reset via /dev/mem (16 SCL pulses on PI5 + clean STOP on PI5/PI6 + TWI0_SRST)
            try:
                fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
                try:
                    with mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x0300B000) as m:
                        # Ensure internal Pull-Up (0b01) on PI5 (bits 11:10) & PI6 (bits 13:12)
                        p13c = struct.unpack("<I", m[0x13C:0x140])[0]
                        m[0x13C:0x140] = struct.pack("<I", (p13c & ~0x00003C00) | 0x00001400)

                        cfg0_orig = struct.unpack("<I", m[0x120:0x124])[0]
                        cfg_out = (cfg0_orig & ~0x0FF00000) | 0x01100000

                        # Set PI5 = Output (SCL), PI6 = Input with Pull-Up (SDA)
                        cfg_rec = (cfg0_orig & ~0x0FF00000) | 0x00100000
                        m[0x120:0x124] = struct.pack("<I", cfg_rec)

                        # Clock 16 SCL pulses on PI5 to free stuck slave
                        for _ in range(16):
                            d = struct.unpack("<I", m[0x130:0x134])[0]
                            m[0x130:0x134] = struct.pack("<I", d & ~(1 << 5))
                            time.sleep(0.00002)
                            d = struct.unpack("<I", m[0x130:0x134])[0]
                            m[0x130:0x134] = struct.pack("<I", d | (1 << 5))
                            time.sleep(0.00002)

                        # Generate clean STOP condition: SCL=0, SDA=0 -> SCL=1, SDA=0 -> SCL=1, SDA=1
                        d = struct.unpack("<I", m[0x130:0x134])[0]
                        m[0x130:0x134] = struct.pack("<I", d & ~((1 << 5) | (1 << 6)))
                        m[0x120:0x124] = struct.pack("<I", cfg_out)
                        time.sleep(0.00005)
                        m[0x130:0x134] = struct.pack("<I", (d & ~(1 << 6)) | (1 << 5))
                        time.sleep(0.00005)
                        m[0x130:0x134] = struct.pack("<I", d | (1 << 5) | (1 << 6))
                        time.sleep(0.00005)

                        # Stage: NXP I2C Slave State-Machine Unwind (START + 9 SCL clocks + STOP to clear desynced SH1106)
                        for _ in range(2):
                            m[0x120:0x124] = struct.pack("<I", cfg_out)
                            d = struct.unpack("<I", m[0x130:0x134])[0]
                            m[0x130:0x134] = struct.pack("<I", d | (1 << 5) | (1 << 6))
                            time.sleep(0.00005)
                            m[0x130:0x134] = struct.pack("<I", (d | (1 << 5)) & ~(1 << 6))
                            time.sleep(0.00005)
                            m[0x130:0x134] = struct.pack("<I", d & ~((1 << 5) | (1 << 6)))
                            time.sleep(0.00005)
                            m[0x120:0x124] = struct.pack("<I", cfg_rec)
                            for _ in range(9):
                                d = struct.unpack("<I", m[0x130:0x134])[0]
                                m[0x130:0x134] = struct.pack("<I", d & ~(1 << 5))
                                time.sleep(0.00005)
                                m[0x130:0x134] = struct.pack("<I", d | (1 << 5))
                                time.sleep(0.00005)
                            m[0x120:0x124] = struct.pack("<I", cfg_out)
                            d = struct.unpack("<I", m[0x130:0x134])[0]
                            m[0x130:0x134] = struct.pack("<I", d & ~((1 << 5) | (1 << 6)))
                            time.sleep(0.00005)
                            m[0x130:0x134] = struct.pack("<I", (d & ~(1 << 6)) | (1 << 5))
                            time.sleep(0.00005)
                            m[0x130:0x134] = struct.pack("<I", d | (1 << 5) | (1 << 6))
                            time.sleep(0.00005)

                        # Restore PI5 & PI6 to Alt 5 (TWI0)
                        cfg_twi = (cfg0_orig & ~0x0FF00000) | 0x05500000
                        m[0x120:0x124] = struct.pack("<I", cfg_twi)
                finally:
                    os.close(fd)

                # Reset TWI0 controller engine and restore standard 400kHz clock before probing 0x3C
                fd_twi = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
                try:
                    with mmap.mmap(fd_twi, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x05002000) as twi:
                        twi[0x1C:0x20] = struct.pack("<I", 0x01)
                        time.sleep(0.005)
                        twi[0x14:0x18] = struct.pack("<I", 0x28)
                finally:
                    os.close(fd_twi)
            except Exception as e:
                print(f"[Display] Hardware pin reset error: {e}", flush=True)

            # Stage 2: Reinitialize OLED Driver & Device
            try:
                time.sleep(0.02)
                self._write_twi0_ccr(0x28)
                target_bus = self.port if self.port is not None else 0
                serial_candidate = i2c(port=target_bus, address=0x3C)
                if getattr(self, "driver_name", "SH1106") == "SH1106":
                    dev = sh1106(serial_candidate)
                    driver_name = "SH1106"
                else:
                    dev = ssd1306(serial_candidate)
                    driver_name = "SSD1306"
                dev.command(0x40, 0xA1, 0xC8)

                try:
                    if getattr(self, "_direct_smbus", None) is not None:
                        self._direct_smbus.close()
                except Exception:
                    pass
                try:
                    from smbus2 import SMBus, i2c_msg
                    self._direct_smbus = SMBus(target_bus)
                    self._direct_i2c_msg = i2c_msg.write
                except Exception:
                    self._direct_smbus = None
                self.serial = serial_candidate
                self.device = dev
                self.driver_name = driver_name
                self._last_frame_bytes = None
                self._prev_pages = None
                self._fs_tx_cache = {}
                self.apply_i2c_clock_profile(getattr(self, "target_fps", 20))
                self.apply_oled_drive_profile(
                    getattr(self, "pwm_mode", "max"),
                    getattr(self, "drive_mode", "normal")
                )
                disp_frame = ImageOps.invert(self.image.convert("L")).convert("1") if self.invert_mode else self.image
                self.device.display(disp_frame)
                print(f"[Display] OLED ({driver_name}) pin reset and auto-recovery succeeded.", flush=True)
                return True
            except Exception as e:
                print(f"[Display] OLED device reinitialization failed: {e}", flush=True)
                return False
            finally:
                shm = getattr(self, "_shm", None)
                if shm is not None:
                    rcnt = (struct.unpack("<I", shm[32:36])[0] + 1) & 0x7FFFFFFF
                    shm[32:36] = struct.pack("<I", rcnt)

    def _compile_page_buffers(self, frame, prev_compiled=None):
        """Compiles 8 hardware pages, reusing cached transpose buffers for any 8px page whose 128 raw bytes did not change."""
        if not hasattr(self, "_bitrev_lut"):
            self._bitrev_lut = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))
        raw = frame.tobytes()
        pages = []
        for p in range(8):
            base = p * 128
            p_raw = raw[base : base + 128]
            if prev_compiled is not None and prev_compiled[p][0] == p_raw:
                pages.append(prev_compiled[p])
            else:
                tr_bytes = frame.crop((0, p * 8, 128, (p + 1) * 8)).transpose(Image.Transpose.TRANSPOSE).tobytes()
                pages.append((p_raw, tr_bytes.translate(self._bitrev_lut)))
        return pages

    def _flush_precompiled_pages(self, target_pages):
        """Flushes dirty [x0:x1+1] spans using single-ioctl smbus2 i2c_rdwr bursts (bypasses luma 32-byte chunking!)."""
        driver = getattr(self, "driver_name", "SH1106")
        col_base = 2 if driver == "SH1106" else 0
        prev_pages = getattr(self, "_prev_pages", None)
        if not hasattr(self, "_direct_smbus"):
            try:
                from smbus2 import SMBus, i2c_msg
                self._direct_smbus = SMBus(0)
                self._direct_i2c_msg = i2c_msg.write
            except Exception:
                self._direct_smbus = None
                self._direct_i2c_msg = None
        bus = self._direct_smbus
        i2c_m = self._direct_i2c_msg
        addr = 0x3C
        self._i2c_rdwr_active = bool(bus is not None and i2c_m is not None)
        tx_msgs = []
        new_prev = list(prev_pages) if prev_pages is not None else [None] * 8
        cur_ph = getattr(self, "_active_pwm_ph", -1)
        flushed_mask = 0
        # Strict 3-Phase Isolation:
        # - cur_ph in (0, 1): ONLY flush particle pages (3, 4, 5, 2) using multi-span gap splitting (~90 bytes total, ~1.8ms!)
        # - cur_ph == 2: flush all dirty bar + marquee pages (3, 4, 5, 2, 0, 1, 6, 7) while particles remain OFF
        ph2_bytes = 0
        d_pages = 0
        t_f0 = time.monotonic()
        is_fs_pwm = getattr(self, "_is_fullscreen_pwm", False)
        cache_hit = 0
        if is_fs_pwm and bus is not None and i2c_m is not None:
            # Stage: Verified S-293 Zero-Overrun Full-Screen Pure PWM Engine with Persistent Per-Page Span Cache
            fs_cache = getattr(self, "_fs_tx_cache", None)
            if fs_cache is None:
                fs_cache = {}
                self._fs_tx_cache = fs_cache
            pg_cache = getattr(self, "_pg_span_cache", None)
            if pg_cache is None:
                pg_cache = {}
                self._pg_span_cache = pg_cache
            # Comment: Key fs_cache by the 1024-byte plane bytes stored in page 0's slot [0] (not id(prev_pages)), because has_new_shm allocates fresh list wrappers at 30 FPS which would otherwise cause id() cache misses on 4 out of every 5 sub-frames.
            cache_key = (prev_pages[0][0], target_pages[0][0]) if prev_pages is not None else None
            cached_entry = fs_cache.get(cache_key) if cache_key is not None else None
            if cached_entry is not None:
                tx_msgs, flushed_mask, d_pages, ph2_bytes, new_prev = cached_entry
                cache_hit = 1
            else:
                page_spans = []
                # Comment: Flush all 8 pages in linear 0..7 order on every MV sub-frame; never skip or defer pages across sub-frames with a cursor, as skipping a page alters its 1/3 or 2/3 PWM duty ratio and produces visible gray flicker.
                for p in range(8):
                    page_buf = target_pages[p][1]
                    if prev_pages is not None:
                        old_buf = prev_pages[p][1]
                        if old_buf == page_buf:
                            continue
                        p_key = (old_buf, page_buf)
                        spans = pg_cache.get(p_key)
                        if spans is None:
                            span_re = getattr(self, "_span_gap10_re", None)
                            if span_re is None:
                                span_re = re.compile(rb"[^\x00]+(?:\x00{1,9}[^\x00]+)*")
                                self._span_gap10_re = span_re
                            # Comment: Compute dirty spans via 1024-bit C int XOR + C regex instead of a Python byte loop (~0.025ms vs ~2.1ms) so 30 FPS cache-miss frames never slip past the 6.444ms deadline.
                            xor_b = (int.from_bytes(page_buf, "little") ^ int.from_bytes(old_buf, "little")).to_bytes(128, "little")
                            spans = tuple((m.start(), m.end() - 1) for m in span_re.finditer(xor_b))
                            if len(pg_cache) > 512:
                                pg_cache.clear()
                            pg_cache[p_key] = spans
                        if spans:
                            d_pages += 1
                            flushed_mask |= (1 << p)
                            page_spans.append((p, page_buf, [list(s) for s in spans]))
                    else:
                        d_pages += 1
                        flushed_mask |= (1 << p)
                        page_spans.append((p, page_buf, [[0, 127]]))
                    new_prev[p] = target_pages[p]
                total_spans = sum(len(s[2]) for s in page_spans)
                # Comment (optimization): Coalesce small gaps <=12B when total_spans*2 > 16 and never merge >12B (merging <=20B in CDR-256 raised 1:50 avgB from 232B to 277B in RP-263).
                while total_spans * 2 > 16:
                    best_gap = 999
                    best_ps = -1
                    best_sp = -1
                    for ps_i, (p, pb, spans) in enumerate(page_spans):
                        for sp_i in range(len(spans) - 1):
                            g = spans[sp_i + 1][0] - spans[sp_i][1] - 1
                            if g < best_gap:
                                best_gap = g
                                best_ps = ps_i
                                best_sp = sp_i
                    if best_ps < 0 or best_gap > 12:
                        break
                    sp = page_spans[best_ps][2]
                    sp[best_sp][1] = sp[best_sp + 1][1]
                    del sp[best_sp + 1]
                    total_spans -= 1
                # Comment (optimization): Store raw (cmd_4b, data_bytes) pairs instead of smbus2.i2c_msg.write() objects (R-327 proved 30x i2c_msg.write() took 2.61ms..4.62ms, causing 12..21 deadline slips/2s even when ov=0).
                for p, pb, spans in page_spans:
                    for x0, x1 in spans:
                        col = col_base + x0
                        tx_msgs.append((bytes((0x00, 0xB0 + p, col & 0x0F, 0x10 | ((col >> 4) & 0x0F))), pb[x0 : x1 + 1]))
                        ph2_bytes += (x1 - x0 + 1)
                if cache_key is not None:
                    if len(fs_cache) > 32:
                        fs_cache.clear()
                    fs_cache[cache_key] = (tx_msgs, flushed_mask, d_pages, ph2_bytes, new_prev)
        else:
            # Comment: For UI/Visualizer PWM (not is_fs_pwm), flush particle pages (3, 4, 5, 2) first using C-XOR multi-span gap splitting (not a single x0..x1 bounding box) so ph0/ph1 particle updates stay ~60B (~1.4ms constant time) and particle brightness never fluctuates when caps cross page boundaries; never inject 0xA8=0x0F scan-parking here as chunked multi-page flushes stripe rows 0..15.
            span_re = getattr(self, "_span_gap10_re", None)
            if span_re is None:
                span_re = re.compile(rb"[^\x00]+(?:\x00{1,9}[^\x00]+)*")
                self._span_gap10_re = span_re
            # Comment (invariant): Never hard-skip pages (0, 1, 6, 7) on ph0/ph1; has_new_shm latches strictly on ph2 so non-PWM marquee/bar pages already match prev_pages[p][1] and skip in O(1), while 2D PWM gray on y<16 or y>=48 (e.g. Gray Calib) must flush on ph0/ph1 or it blacks out.
            for p in (3, 4, 5, 2, 0, 1, 6, 7):
                if prev_pages is not None and prev_pages[p][1] == target_pages[p][1]:
                    continue
                d_pages += 1
                flushed_mask |= (1 << p)
                page_buf = target_pages[p][1]
                if prev_pages is not None:
                    old_buf = prev_pages[p][1]
                    xor_b = (int.from_bytes(page_buf, "little") ^ int.from_bytes(old_buf, "little")).to_bytes(128, "little")
                    for m in span_re.finditer(xor_b):
                        x0, x1 = m.start(), m.end() - 1
                        col = col_base + x0
                        ph2_bytes += (x1 - x0 + 1)
                        if bus is not None and i2c_m is not None:
                            tx_msgs.append(i2c_m(addr, bytes((0x00, 0xB0 + p, col & 0x0F, 0x10 | ((col >> 4) & 0x0F)))))
                            tx_msgs.append(i2c_m(addr, b"\x40" + page_buf[x0 : x1 + 1]))
                        else:
                            self.device.command(0xB0 + p, col & 0x0F, 0x10 | ((col >> 4) & 0x0F))
                            self.device.data(list(page_buf[x0 : x1 + 1]))
                else:
                    ph2_bytes += 128
                    if bus is not None and i2c_m is not None:
                        tx_msgs.append(i2c_m(addr, bytes((0x00, 0xB0 + p, col_base & 0x0F, 0x10))))
                        tx_msgs.append(i2c_m(addr, b"\x40" + page_buf))
                    else:
                        self.device.command(0xB0 + p, col_base & 0x0F, 0x10)
                        self.device.data(list(page_buf))
                new_prev[p] = target_pages[p]
        t_prep = time.monotonic()
        dl = getattr(self, "_next_io_dl", 0.0)
        t_wake = t_prep
        if dl > 0.0:
            # Comment (invariant): Never call time.sleep() before dl in OLED_Compositor (Cortex-A53 hrtimer/WFI wakeup overshoots dl by 1.5..3.0ms, causing 20..35 slips/2s even when io is 4.2ms in RP-255); with sched_rt_runtime_us=-1 and audio/BT daemons isolated onto Cores {0,2,3}, spin-wait cleanly to dl.
            while time.monotonic() < dl:
                pass
            # Comment: Only re-anchor _next_io_dl when lateness exceeds 3.0ms (matching live_pwm_tuner.py's >0.003s guard); re-anchoring on 0.00020s (200us) jitter permanently shifts the 155.18Hz phase grid on every 30 FPS frame update and causes rolling beat-frequency flicker.
            t_wake = time.monotonic()
            if (t_wake - dl) > 0.0030:
                self._next_io_dl = t_wake
        shm_ref = getattr(self, "_shm", None)
        if shm_ref is not None and struct.unpack("<I", shm_ref[12:16])[0] != 0:
            return 0, 0
        # Stage: Atomic Full-Screen PWM Burst Dispatcher (8-msg 0x90 chunks for full-screen MV; 4-msg 0x90 chunks for UI/Viz)
        # Comment: Keep TWI0_CCR strictly at 0x90 (800kHz, DUTY=1) and chunk MV bursts into 8-msg ioctls so I2C0 transactions never hold the kernel bus lock long enough to stall PipeWire/PCM5122 audio or trigger 52ms recovery stalls; never use 0x88 (1.2MHz) and never toggle 0x28 on sub-frame exit.
        with self.bus_lock:
            if shm_ref is not None and struct.unpack("<I", shm_ref[12:16])[0] != 0:
                return 0, 0
            target_ccr = getattr(self, "_overdrive_ccr", 0x90)
            self._write_twi0_ccr(target_ccr)
            t_ioctl0 = time.monotonic()
            self._last_ioctl0_t = t_ioctl0
            ccr_b = self._twi_mmap[0x14] if getattr(self, "_twi_mmap", None) is not None else -1
            if tx_msgs and bus is not None:
                if is_fs_pwm:
                    rdwr_pool = getattr(self, "_rdwr_pool", None)
                    if rdwr_pool is None:
                        import ctypes as _ct, fcntl as _fc
                        from smbus2 import i2c_msg as _im
                        from smbus2.smbus2 import i2c_rdwr_ioctl_data as _ird
                        _msgs = (_im * 8)()
                        _bufs = [_ct.create_string_buffer(130) for _ in range(8)]
                        for _i in range(8):
                            _msgs[_i].addr = addr
                            _msgs[_i].flags = 0
                            _msgs[_i].buf = _ct.cast(_bufs[_i], _ct.POINTER(_ct.c_char))
                        _data = _ird(msgs=_msgs, nmsgs=0)
                        rdwr_pool = (_fc.ioctl, _msgs, _bufs, _data)
                        self._rdwr_pool = rdwr_pool
                    _ioctl, _msgs, _bufs, _data = rdwr_pool
                    _bfd = bus.fd
                    for m_idx in range(0, len(tx_msgs), 4):
                        chunk = tx_msgs[m_idx : m_idx + 4]
                        _k = 0
                        for _cb, _db in chunk:
                            _bufs[_k][0:4] = _cb
                            _msgs[_k].len = 4
                            _dl = len(_db) + 1
                            _bufs[_k + 1][0] = 0x40
                            _bufs[_k + 1][1:_dl] = _db
                            _msgs[_k + 1].len = _dl
                            _k += 2
                        _data.nmsgs = _k
                        try:
                            _ioctl(_bfd, 0x0707, _data)
                        except OSError:
                            if getattr(self, "_twi_mmap", None) is not None:
                                self._twi_mmap[0x1C:0x20] = struct.pack("<I", 0x01)
                                time.sleep(0.0005)
                            self._write_twi0_ccr(0x28)
                            time.sleep(0.0003)
                            _ioctl(_bfd, 0x0707, _data)
                            self._write_twi0_ccr(target_ccr)
                else:
                    for m_idx in range(0, len(tx_msgs), 4):
                        chunk = tx_msgs[m_idx : m_idx + 4]
                        try:
                            bus.i2c_rdwr(*chunk)
                        except OSError as io_err:
                            if getattr(io_err, "errno", 0) in (6, 11, 121):
                                time.sleep(0.0004)
                                bus.i2c_rdwr(*chunk)
                            else:
                                raise
            t_ioctl1 = time.monotonic()
        self._last_viz_done_t = t_ioctl1
        prep_ms = round((t_prep - t_f0) * 1000.0, 2)
        late_ms = round((t_wake - dl) * 1000.0, 2) if dl > 0.0 else 0.0
        lock_ms = round((t_ioctl0 - t_wake) * 1000.0, 2)
        self._last_flush_meta = (len(tx_msgs) * 2 if is_fs_pwm else len(tx_msgs), round((t_ioctl1 - t_ioctl0) * 1000.0, 3), int(ccr_b), int(flushed_mask), int(cache_hit), prep_ms, late_ms, lock_ms, int(cur_ph))
        self._prev_pages = target_pages if is_fs_pwm else new_prev
        return d_pages, ph2_bytes

    def _hardware_render_loop(self):
        """
        Zero-Sleep Pre-Transposed Pure Solid PWM Engine (Zero Dithering Dots!):
        - Pre-transposes pages_hi (ON) and pages_lo (OFF) ONCE when a new UI frame arrives.
        - Between UI frames, polls frame_queue with get_nowait() (ZERO 2ms timeout sleep!) and swaps
          pre-transposed pages_hi <-> pages_lo at full 800kHz I2C0 speed (>100Hz complete ON/OFF cycles!).
        - Updates self._prev_pages on every sub-frame so solid white bars NEVER desync or turn gray!
        """
        from PIL import ImageChops
        import gc
        gc.disable()
        try:
            os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
            os.sched_setaffinity(0, {1})
            with open("/sys/devices/system/cpu/cpu1/cpufreq/scaling_governor", "w") as gf:
                gf.write("performance\n")
            # Comment (invariant): Route mv64xxx_i2c IRQ (300) to Core 3 (mask 0x8) so 4kHz I2C byte interrupts never preempt OLED_Compositor's phase timer on Core 1.
            with open("/proc/interrupts", "r") as irqf:
                for iln in irqf:
                    if "mv64xxx_i2c" in iln or "5002000.i2c" in iln:
                        irq_num = iln.split(":")[0].strip()
                        try:
                            with open(f"/proc/irq/{irq_num}/smp_affinity", "w") as af:
                                af.write("8\n")
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            import ctypes as _ct
            sys.setswitchinterval(0.005)
            _ct.CDLL("libc.so.6").prctl(29, 1, 0, 0, 0)
        except Exception:
            pass
        parent_pid = getattr(self, "_parent_pid", os.getppid())
        try:
            import ctypes, signal, glob
            ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM, 0, 0, 0)
            my_pid = os.getpid()
            os.sched_setaffinity(0, {1})
            os.nice(-20)
            try:
                with open("/sys/fs/cgroup/cgroup.procs", "w") as _cgf:
                    _cgf.write(f"{my_pid}\n")
            except Exception:
                pass
            # Comment (invariant): Keep OLED_Compositor in SCHED_OTHER at nice(-20) on isolated Core 1 (never SCHED_FIFO); CONFIG_RT_GROUP_SCHED=y rejects sched_rt_runtime_us=-1 (R-322), causing the kernel RT throttler to freeze SCHED_FIFO for 51..58ms (DR-329), whereas SCHED_OTHER is exempt from RT throttling while Core 1 is isolated from OS/audio/IRQ300.
            # Comment (invariant): Pin all parent OS threads and system audio/Bluetooth daemons (pipewire, wireplumber, pipewire-pulse, bluetoothd, mpv) to Cores {0, 2, 3} so SCHED_FIFO 80 on Core 1 never starves PipeWire/WirePlumber A2DP audio data-loops.
            for comm_p in glob.glob("/proc/[0-9]*/comm"):
                try:
                    p_str = comm_p.split("/")[2]
                    p_int = int(p_str)
                    if p_int == my_pid:
                        continue
                    c_name = open(comm_p).read().strip()
                    if p_int == parent_pid or c_name in ("pipewire", "wireplumber", "pipewire-pulse", "bluetoothd", "mpv"):
                        for te in os.listdir(f"/proc/{p_int}/task"):
                            if te.isdigit() and int(te) != my_pid:
                                try:
                                    os.sched_setaffinity(int(te), {0, 2, 3})
                                except Exception:
                                    pass
                except Exception:
                    pass
        except Exception:
            pass
        pages_hi = None
        pages_lo = None
        pwm_phase = 0
        f_seq_hw = 0
        last_shm_ver = 0
        hw_tick = 0
        # Stage: Lock-Guarded Compositor Init (Synchronizes initial SH1106 command setup with PowerMonitor on I2C0)
        with self.bus_lock:
            try:
                from smbus2 import SMBus, i2c_msg
                with SMBus(0) as _bi:
                    _bi.i2c_rdwr(i2c_msg.write(0x3C, b"\x00\x40\xA1\xC8\xA8\x3F\xD5\xF0\xD9\x11"))
            except Exception:
                pass
        next_dl = time.monotonic()
        shm = getattr(self, "_shm", None)
        while True:
            if os.getppid() != parent_pid:
                os._exit(0)
            item = None
            has_new_shm = False
            if shm is not None:
                shm_ver, shm_seq, shm_pwm, shm_sleep, shm_slot = struct.unpack("<IIIII", shm[0:20])
                if shm_sleep:
                    time.sleep(0.02)
                    next_dl = time.monotonic()
                    continue
                if pages_hi is not None and pages_lo is not None:
                    # Comment (constraint): Keep both full-screen MV and UI/Visualizer on 3-phase 51.73Hz (0,1,2); 4-step triangle (0,1,2,1) in CDR-371/R-384 dropped cycle rate to 38.8Hz below flicker fusion.
                    pwm_phase = (pwm_phase + 1) % 3
                    if pwm_phase == 2:
                        hw_tick = (hw_tick + 1) & 0x7FFFFFFF
                        shm[20:24] = struct.pack("<I", hw_tick)
                        if shm_ver != last_shm_ver:
                            has_new_shm = True
                elif shm_ver != last_shm_ver:
                    hw_tick = (hw_tick + 1) & 0x7FFFFFFF
                    shm[20:24] = struct.pack("<I", hw_tick)
                    has_new_shm = True
                    pwm_phase = 0
                else:
                    try:
                        item = self.frame_queue.get(timeout=0.004)
                    except queue.Empty:
                        continue
            if has_new_shm:
                t_start = time.monotonic()
                v1, f_seq_hw, shm_pwm, _, r_slot = struct.unpack("<IIIII", shm[0:20])
                self._is_fullscreen_pwm = (shm_pwm == 2)
                base_off = 64 if r_slot == 0 else 3136
                b_ph0 = bytes(shm[base_off : base_off + 1024])
                b_ph1 = bytes(shm[base_off + 1024 : base_off + 2048]) if shm_pwm else None
                b_lo = bytes(shm[base_off + 2048 : base_off + 3072]) if shm_pwm else None
                last_shm_ver = v1
                if hasattr(self, "_fs_tx_cache") and len(self._fs_tx_cache) > 32:
                    self._fs_tx_cache.clear()
                # Comment: Store the full 1024-byte plane bytes at p==0 slot [0] so _flush_precompiled_pages can use (prev_pages[0][0], target_pages[0][0]) as an O(1) content-hash cache key across 30 FPS SHM updates while keeping slot [1] as the 128-byte page slice.
                raw_hi = [(b_ph0 if p == 0 else b_ph0[p*128:(p+1)*128], b_ph0[p*128:(p+1)*128]) for p in range(8)]
                if b_ph1 is not None and b_lo is not None:
                    raw_ph1 = [(b_ph1 if p == 0 else b_ph1[p*128:(p+1)*128], b_ph1[p*128:(p+1)*128]) for p in range(8)]
                    raw_lo = [(b_lo if p == 0 else b_lo[p*128:(p+1)*128], b_lo[p*128:(p+1)*128]) for p in range(8)]
                    # Stage: In-Phase 3-Plane Full-Screen PWM Selector (Matches verified live_pwm_tuner.py 1:3Ph-InPhase 0..7 schedule)
                    # Comment: Keep all 8 pages in-phase (raw_hi, raw_ph1, raw_lo); never stagger even/odd pages across phases as out-of-phase adjacent 8px strips flicker visibly and prevent global ph0->ph1->ph2 span reuse.
                    pages_hi, pages_ph1, pages_lo = raw_hi, raw_ph1, raw_lo
                else:
                    pages_hi = raw_hi
                    pages_ph1 = None
                    pages_lo = None
                t_comp = time.monotonic()
                if pages_lo is None:
                    pwm_phase = 0
                    p0_lit = -1
                    next_dl = t_comp
                else:
                    p0_lit = 1 if pwm_phase == 0 else 0
            elif item is not None:
                t_start = time.monotonic()
                base_f = item[0] if isinstance(item, tuple) else item
                hi_img = ImageOps.invert(base_f.convert("L")).convert("1") if self.invert_mode else base_f
                c_tmp = self._compile_page_buffers(hi_img, None)
                pages_hi = [(c_tmp[p][1], c_tmp[p][1]) for p in range(8)]
                pages_ph1 = None
                pages_lo = None
                pwm_phase = 0
                p0_lit = -1
                t_comp = time.monotonic()
                next_dl = t_comp
            else:
                if pages_hi is None:
                    continue
                t_start = time.monotonic()
                t_comp = t_start
                if pages_lo is None:
                    time.sleep(0.004)
                    continue
                p0_lit = 1 if pwm_phase == 0 else 0

            # 3-Phase Multi-Shade Sub-Frame Selector (ph0=1/3+2/3+3/3, ph1=2/3+3/3, ph2=3/3 solid)
            self._active_pwm_ph = pwm_phase if pages_lo is not None else -1
            if pages_lo is None or pwm_phase == 0:
                active_pages = pages_hi
            elif pwm_phase == 1 and pages_ph1 is not None:
                active_pages = pages_ph1
            else:
                active_pages = pages_lo
            try:
                if pages_lo is not None:
                    b_dt = struct.unpack("<d", shm[24:32])[0] if shm is not None else 0.006443948
                    if not (0.004 < b_dt < 0.012):
                        b_dt = 0.006443948
                    step_dt = b_dt + (0.0000350 if pwm_phase == 1 else (-0.0000350 if pwm_phase == 2 else 0.0))
                    next_dl += step_dt
                    now_chk = time.monotonic()
                    if (now_chk - next_dl) > 0.003 or (next_dl - now_chk) > 0.020:
                        next_dl = now_chk + step_dt
                    self._next_io_dl = next_dl
                else:
                    self._next_io_dl = 0.0
                t_lock = time.monotonic()
                d_pages, d_bytes = self._flush_precompiled_pages(active_pages)
                if pages_lo is not None and getattr(self, "_next_io_dl", 0.0) > next_dl:
                    next_dl = self._next_io_dl
                t_done = getattr(self, "_last_viz_done_t", time.monotonic())
                if not hasattr(self, "_pwm_hw_log") or os.path.exists("/tmp/oled_diag_reset_hw"):
                    self._pwm_hw_log = []
                    self._pwm_hw_t0 = t_start
                    try:
                        os.remove("/tmp/oled_diag_reset_hw")
                    except Exception:
                        pass
                if True:
                    f_meta = getattr(self, "_last_flush_meta", (0, 0.0, -1, 0))
                    io0_cur = getattr(self, "_last_ioctl0_t", t_done)
                    io0_dt_ms = round((io0_cur - getattr(self, "_prev_io0_t", io0_cur)) * 1000.0, 3)
                    self._prev_io0_t = io0_cur
                    if not getattr(self, "_is_fullscreen_pwm", False):
                        if p0_lit != -1 and self._pwm_hw_log and self._pwm_hw_log[0][2] == -1:
                            self._pwm_hw_log.clear()
                        now_rel_ms = round((t_done - self._pwm_hw_t0) * 1000.0, 2)
                        prev_rel_ms = self._pwm_hw_log[-1][0] if self._pwm_hw_log else now_rel_ms
                        dt_hw_ms = now_rel_ms - prev_rel_ms
                        spike_info = f"pmask=0x{f_meta[3]:02x} io0_dt={io0_dt_ms}ms ioctl={f_meta[1]}ms"
                        if dt_hw_ms > 12.0:
                            try:
                                import sys as _sys
                                my_id = threading.get_ident()
                                t_map = {t.ident: t.name for t in threading.enumerate()}
                                act_t = []
                                for tid, fr in _sys._current_frames().items():
                                    if tid == my_id:
                                        continue
                                    fn = os.path.basename(fr.f_code.co_filename)
                                    act_t.append(f"{t_map.get(tid, tid)}@{fn}:{fr.f_lineno}[{fr.f_code.co_name}]")
                                spike_info = f"msgs={f_meta[0]} ioctl={f_meta[1]}ms ccr=0x{f_meta[2]:02x}->0x{f_meta[3]:02x} thr={'|'.join(act_t[:5])}"
                            except Exception as _ex:
                                spike_info = f"msgs={f_meta[0]} ioctl={f_meta[1]}ms err={_ex}"
                        self._pwm_hw_log.append((
                            now_rel_ms,
                            int(f_seq_hw),
                            int(p0_lit),
                            int(d_pages),
                            int(d_bytes),
                            round((t_comp - t_start) * 1000.0, 3),
                            round((t_lock - t_comp) * 1000.0, 3),
                            round((t_done - t_lock) * 1000.0, 3),
                            int(getattr(self, "_dropped_q_frames", 0)),
                            spike_info,
                        ))
                        if len(self._pwm_hw_log) > 3200:
                            self._pwm_hw_log = self._pwm_hw_log[-3200:]
                    # Stage: Deterministic Bus-Throughput Hybrid PWM Budget Controller + 2-Second PWM-DIAG Telemetry
                    if getattr(self, "_is_fullscreen_pwm", False):
                        # Comment (decision): Compute the Hybrid PWM byte budget deterministically from measured hardware bus throughput (d_bytes / io_ms), non-IO overhead (prep + lock), and ph2 frame-motion extra bytes; avoids step-heuristic pinning (CR-782 pinned at 48B despite 1.43ms io) and scales automatically across 400kHz/800kHz I2C and SPI boards.
                        dt_ms = (b_dt * 1000.0) if pages_lo is not None else 6.452
                        io_ms = f_meta[1]
                        ovh_ms = (f_meta[5] if len(f_meta) > 5 else 0.0) + (f_meta[7] if len(f_meta) > 7 else 0.0)
                        rate = getattr(self, "_bus_rate_bpms", 50.0)
                        if d_bytes >= 48 and io_ms >= 0.35:
                            inst_r = max(10.0, min(2000.0, d_bytes / io_ms))
                            rate = rate * 0.90 + inst_r * 0.10
                            self._bus_rate_bpms = rate
                        ovh_ema = getattr(self, "_bus_ovh_ms", 0.45)
                        ovh_ema = ovh_ema * 0.92 + min(2.0, ovh_ms) * 0.08
                        self._bus_ovh_ms = ovh_ema
                        ph01_b = getattr(self, "_last_ph01_bytes", 0)
                        mot_b = getattr(self, "_mot_extra_b", 0.0)
                        if pwm_phase in (0, 1):
                            ph01_b = d_bytes
                            self._last_ph01_bytes = ph01_b
                        elif has_new_shm:
                            extra = max(0.0, float(d_bytes - ph01_b))
                            mot_b = (mot_b * 0.70 + extra * 0.30) if extra > mot_b else (mot_b * 0.97 + extra * 0.03)
                            self._mot_extra_b = mot_b
                        else:
                            mot_b *= 0.985
                            self._mot_extra_b = mot_b
                        cap_b = int(max(48.0, min(1024.0, (dt_ms * 0.82 - ovh_ema) * rate)))
                        raw_bud = max(48, min(1024, int(cap_b - mot_b)))
                        bud = getattr(self, "_adapt_pwm_budget", 144)
                        if abs(raw_bud - bud) >= 16 or (raw_bud == 1024 and bud != 1024):
                            bud = max(48, min(1024, (raw_bud // 8) * 8))
                            self._adapt_pwm_budget = bud
                            if shm is not None:
                                shm[36:40] = struct.pack("<I", bud)

                        if not hasattr(self, "_diag_win_t0"):
                            self._diag_win_t0 = t_done
                            self._diag_win_rows = []
                        self._diag_win_rows.append((io0_dt_ms, f_meta[1], d_bytes, f_meta[0], f_meta[4] if len(f_meta) > 4 else 0, f_meta[5] if len(f_meta) > 5 else 0.0, f_meta[6] if len(f_meta) > 6 else 0.0, f_meta[7] if len(f_meta) > 7 else 0.0, f_meta[8] if len(f_meta) > 8 else -1))
                        if (t_done - self._diag_win_t0) >= 2.0 and len(self._diag_win_rows) > 10:
                            rows_s = self._diag_win_rows[1:]
                            r_dts = [r[0] for r in rows_s]
                            r_ios = [r[1] for r in rows_s]
                            r_bs  = [r[2] for r in rows_s]
                            r_ms  = [r[3] for r in rows_s]
                            r_ch  = sum(r[4] for r in rows_s)
                            avg_dt = sum(r_dts) / len(r_dts)
                            ov_c = sum(1 for x in r_ios if x > dt_ms)
                            sl_c = sum(1 for x in r_dts if abs(x - dt_ms) > 0.30)
                            w_row = max(rows_s, key=lambda r: abs(r[0] - dt_ms))
                            print(
                                f"[PWM-DIAG] sub_hz={1000.0/max(0.1, avg_dt):.1f}Hz | actual_dt={avg_dt:.3f}ms(max={max(r_dts):.2f}) "
                                f"| io={sum(r_ios)/len(r_ios):.2f}ms(max={max(r_ios):.2f}) | avgB={sum(r_bs)/len(r_bs):.0f} | rate={rate:.0f}B/ms cap={cap_b}B mot={mot_b:.0f}B bud={bud}B "
                                f"| avgM={sum(r_ms)/len(r_ms):.1f} | ov={ov_c}/{len(r_ios)} | slips={sl_c}/{len(r_dts)} | cache={r_ch}/{len(r_dts)} "
                                f"| worst=(dt={w_row[0]:.2f},io={w_row[1]:.2f},prep={w_row[5]:.2f},late={w_row[6]:.2f},lock={w_row[7]:.2f},ph={w_row[8]})",
                                flush=True
                            )
                            self._diag_win_t0 = t_done
                            self._diag_win_rows.clear()
                    if os.path.exists("/tmp/oled_diag_dump_hw"):
                        try:
                            os.remove("/tmp/oled_diag_dump_hw")
                            with open("/tmp/oled_hw_diag.json.tmp", "w") as df:
                                json.dump(self._pwm_hw_log[:-4] if len(self._pwm_hw_log) > 8 else self._pwm_hw_log, df)
                            os.replace("/tmp/oled_hw_diag.json.tmp", "/tmp/oled_hw_diag.json")
                        except Exception:
                            pass
                self._consecutive_errors = 0
                if pages_lo is None:
                    time.sleep(0.001)
            except Exception as e:
                if hasattr(self, "_pwm_hw_log"):
                    ne = round((time.monotonic() - getattr(self, "_pwm_hw_t0", 0.0)) * 1000.0, 2)
                    self._pwm_hw_log.append((ne, int(f_seq_hw), int(p0_lit), -1, -1, 0.0, 0.0, 0.0, 0, f"EXC={e}"))
                self._consecutive_errors += 1
                if self._consecutive_errors >= 2:
                    now = time.time()
                    if now - self._last_recovery_time > 2.0:
                        self._last_recovery_time = now
                        print(f"[Display] Screen freeze detected ({e}). Resetting stuck I2C0 pins (PI5/PI6) & reinitializing...", flush=True)
                        self.reset_pins_and_recover()
                        self._consecutive_errors = 0

    def clear(self):
        """Wipes the canvas, resets VRR wake flags, and advances a deterministic integer V-Sync step."""
        self.has_active_marquee = False
        self.next_marquee_wake = 0.0
        self._last_p0_lit = -1
        self._pwm_rects = []
        self._pwm_gray_planes = None
        now = time.monotonic()
        import src.ui.components as ui_components
        ui_components.CURRENT_FRAME_TIME = now
        seq = getattr(self, "_frame_seq", 0) + 1
        self._frame_seq = seq
        self._frame_now = now
        # Stage: Zero-Judder Integer V-Sync Marquee Stepper (Strictly 1px per 51.73Hz hardware PWM latch; phase-locked 19.33ms PLL grid when shm_pwm!=1)
        # Comment (invariant): Never use fractional 1-1-1-2 pulldown patterns on 1-bit OLED text (causes visible 13Hz telecine judder, P-487), and when shm_pwm != 1 (where shm[20:24] ticks at 120Hz UI FPS instead of 51.73Hz), step strictly 1px per 19.331ms PLL grid period so text scrolls with zero pulldown jitter at a rock-steady 51.73 px/s across both PWM and 1-bit screens.
        spd = max(0.5, float(getattr(ui_components, "DEFAULT_MARQUEE_SPEED", 3.0)))
        shm = getattr(self, "_shm", None)
        shm_pwm_mode = struct.unpack("<I", shm[8:12])[0] if shm is not None else 0
        hw_seq = struct.unpack("<I", shm[20:24])[0] if shm is not None else seq
        unit_px = 2 if spd >= 4.5 else 1
        if shm_pwm_mode == 1 and shm is not None:
            self._marq_grid_t = now
            last_hw_seq = getattr(self, "_last_marq_hw_seq", -1)
            if hw_seq != last_hw_seq:
                self._last_marq_hw_seq = hw_seq
                self._frame_px_step = (1 if (hw_seq & 1) == 0 else 0) if spd <= 2.1 else unit_px
            else:
                self._frame_px_step = 0
        else:
            self._last_marq_hw_seq = hw_seq
            period = (2.0 / 51.73) if spd <= 2.1 else (1.0 / 51.73)
            grid_t = getattr(self, "_marq_grid_t", None)
            if grid_t is None or (now - grid_t) > 0.20 or (grid_t - now) > period:
                self._marq_grid_t = now
                self._frame_px_step = unit_px
            elif now >= grid_t + period:
                steps = int((now - grid_t) / period)
                self._marq_grid_t = grid_t + steps * period
                self._frame_px_step = min(unit_px * 2, steps * unit_px)
            else:
                self._frame_px_step = 0
        if hasattr(self, "_marquee_slots"):
            stale = [k for k, v in self._marquee_slots.items() if now - v["last_seen"] > 0.6]
            for k in stale:
                del self._marquee_slots[k]
        self.draw.rectangle((0, 0, self.image.width, self.image.height), fill="black")

    def _flush_dirty_pages(self, frame):
        """Flushes only modified 8px pages AND trims unchanged leading/trailing columns [x0:x1+1] to maximize I2C0 Hz."""
        driver = getattr(self, "driver_name", "SH1106")
        if driver not in ("SH1106", "SSD1306"):
            self.device.display(frame)
            return
        col_base = 2 if driver == "SH1106" else 0
        raw = frame.tobytes()
        prev_pages = getattr(self, "_prev_pages", None)
        new_pages = []
        if not hasattr(self, "_bitrev_lut"):
            self._bitrev_lut = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))
        for p in range(8):
            base = p * 128
            p_raw = raw[base : base + 128]
            if prev_pages is not None and prev_pages[p][0] == p_raw:
                new_pages.append(prev_pages[p])
                continue
            tr_bytes = frame.crop((0, p * 8, 128, (p + 1) * 8)).transpose(Image.Transpose.TRANSPOSE).tobytes()
            page_buf = list(tr_bytes.translate(self._bitrev_lut))
            if prev_pages is not None:
                old_buf = prev_pages[p][1]
                x0 = 0
                while x0 < 128 and page_buf[x0] == old_buf[x0]:
                    x0 += 1
                if x0 < 128:
                    x1 = 127
                    while x1 > x0 and page_buf[x1] == old_buf[x1]:
                        x1 -= 1
                    col = col_base + x0
                    self.device.command(0xB0 + p, col & 0x0F, 0x10 | ((col >> 4) & 0x0F))
                    self.device.data(page_buf[x0 : x1 + 1])
            else:
                self.device.command(0xB0 + p, col_base & 0x0F, 0x10)
                self.device.data(page_buf)
            new_pages.append((p_raw, page_buf))
        self._prev_pages = new_pages

    def _write_twi0_ccr(self, ccr_val: int):
        """Writes Allwinner H618 TWI0_CCR (0x05002014) via cached mmap and prevents Runtime PM clock gating (ccr=0x00->0x59)."""
        try:
            if getattr(self, "_twi_mmap", None) is None:
                import glob as _glob
                for pm_p in _glob.glob("/sys/bus/platform/devices/*i2c*/power/control"):
                    try:
                        with open(pm_p, "w") as pf:
                            pf.write("on\n")
                    except Exception:
                        pass
                self._twi_fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
                self._twi_mmap = mmap.mmap(self._twi_fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x05002000)
            if self._twi_mmap[0x14] != (ccr_val & 0xFF):
                self._twi_mmap[0x14:0x18] = struct.pack("<I", ccr_val)
        except Exception:
            pass

    def set_pwm_base_dt(self, dt_sec: float):
        """Updates the live 3-phase PWM sub-frame base period in shared memory."""
        val = max(0.004, min(0.012, float(dt_sec)))
        self._pwm_base_dt = val
        if getattr(self, "_shm", None) is not None:
            self._shm[24:32] = struct.pack("<d", val)

    def apply_i2c_clock_profile(self, fps: int = 20):
        """Sets baseline I2C0 clock to 400kHz for INA219/PCM5122 safety and configures artifact-free 0x90 DUTY=1 OLED burst up to 120Hz."""
        self.target_fps = max(10, min(120, int(fps)))
        self._overdrive_ccr = 0x90  # Verified zero-NACK Fast Mode+ (0 I2C NACKs/phase skips)
        self._write_twi0_ccr(self._overdrive_ccr)

    def draw_text(self, x: int, y: int, text: str, font_type="small"):
        """Stateful V-Sync Slot Marquee Engine with 1.2s read-pauses, telemetry immunity, and synchronized integer frame steps."""
        if font_type == "large":
            active_font = self.font_large
        elif font_type == "icons":
            active_font = self.font_icons
        else:
            active_font = self.font_small

        if not hasattr(self, "_text_sprite_cache"):
            self._text_sprite_cache = {}

        def _blit_cached(tx: int, ty: int, s_txt: str) -> int:
            if not s_txt:
                return 0
            ck = (font_type, s_txt)
            entry = self._text_sprite_cache.get(ck)
            if entry is None:
                if len(self._text_sprite_cache) > 256:
                    self._text_sprite_cache.clear()
                w_p = max(1, int(round(active_font.getlength(s_txt))) if hasattr(active_font, "getlength") else len(s_txt) * 6)
                h_p = 28 if font_type == "large" else 13
                sp = Image.new("1", (w_p + 2, h_p), 0)
                ImageDraw.Draw(sp).text((0, 0), s_txt, font=active_font, fill=1)
                entry = (sp, w_p)
                self._text_sprite_cache[ck] = entry
            self.image.paste(1, (int(tx), int(ty)), mask=entry[0])
            return entry[1]

        if isinstance(text, str) and "\x1f" in text:
            parts = text.split("\x1f")
            if len(parts) == 4:
                pre_txt, meta, cycle_txt, post_txt = parts
                try:
                    m_parts = meta.split(":")
                    speed = float(m_parts[0])
                    max_ch = int(m_parts[1])
                    pause_ms = int(m_parts[2]) if len(m_parts) > 2 else 1200
                    max_sc = int(m_parts[3]) if len(m_parts) > 3 else 0
                except Exception:
                    speed, max_ch, pause_ms, max_sc = 3.0, 14, 1200, 0

                cur_x = x
                if pre_txt:
                    cur_x += _blit_cached(cur_x, y, pre_txt)

                # Reserve 4px on the right edge (x <= 124) so marquees never collide with the LayoutEngine scrollbar
                clip_w = max(12, min(124 - cur_x, max_ch * 7))
                if not hasattr(self, "_marquee_slots"):
                    self._marquee_slots = {}
                now = getattr(self, "_frame_now", time.monotonic())
                frame_seq = getattr(self, "_frame_seq", 0)
                pause_sec = pause_ms / 1000.0

                raw_core = cycle_txt.strip()
                if raw_core.startswith(("Pi:", "DAC:", "Bat:")):
                    anchor_id = "TELEM:" + raw_core.split(":", 1)[0]
                else:
                    anchor_id = raw_core

                # Key by (cur_x, anchor_id) instead of (cur_x, y) so scrolling the Dashboard viewport up/down
                # moves the row vertically without resetting its marquee progress or triggering a 1.2s freeze!
                slot_key = (cur_x, anchor_id)
                slot = self._marquee_slots.get(slot_key)
                if slot is None:
                    slot = {
                        "anchor_id": anchor_id,
                        "px_shift": 0,
                        "pause_until": now + pause_sec,
                        "laps": 0,
                        "last_seq": frame_seq,
                        "last_seen": now,
                        "sprite_txt": None,
                        "sprite_img": None,
                        "cycle_w": 1,
                    }
                    self._marquee_slots[slot_key] = slot
                else:
                    slot["last_seen"] = now

                # Pre-render the 1-bit sprite texture ONLY when the string actually changes (0 per-frame TTF overhead!)
                if slot["sprite_txt"] != cycle_txt or slot["sprite_img"] is None or slot.get("sp_h") != 13:
                    c_w = int(round(active_font.getlength(cycle_txt))) if hasattr(active_font, "getlength") else len(cycle_txt) * 6
                    c_w = max(1, c_w)
                    sp_w = c_w * 2 + clip_w
                    sp_img = Image.new("1", (sp_w, 13), 0)
                    ImageDraw.Draw(sp_img).text((0, 0), cycle_txt + cycle_txt, font=active_font, fill=1)
                    slot["sprite_txt"] = cycle_txt
                    slot["sprite_img"] = sp_img
                    slot["cycle_w"] = c_w
                    slot["sp_h"] = 13

                cycle_w = slot["cycle_w"]
                if max_sc > 0 and slot["laps"] >= max_sc:
                    px_shift = 0
                else:
                    self.has_active_marquee = True
                    if now < slot["pause_until"]:
                        px_shift = 0
                        slot["last_seq"] = frame_seq
                    else:
                        step_px = getattr(self, "_frame_px_step", 1)
                        if slot["last_seq"] != frame_seq and step_px > 0:
                            slot["px_shift"] += step_px
                            if slot["px_shift"] >= cycle_w:
                                slot["px_shift"] = 0
                                slot["laps"] += 1
                                slot["pause_until"] = now + pause_sec
                        slot["last_seq"] = frame_seq
                        px_shift = slot["px_shift"] % cycle_w

                # Hardware-style C-level sprite window blit (crop 1-bit texture & paste)
                sub_strip = slot["sprite_img"].crop((px_shift, 0, px_shift + clip_w, 13))
                self.image.paste(1, (cur_x, y), mask=sub_strip)
                if post_txt:
                    _blit_cached(cur_x + clip_w, y, post_txt)
                return

        _blit_cached(x, y, str(text))

    async def show(self):
        """Hands the frame buffer to the compositor thread, skipping identical frames to save I2C0 bandwidth."""
        if self.is_asleep:
            return
        if not self.device:
            now = time.time()
            if now - self._last_recovery_time > 3.0:
                self._last_recovery_time = now
                self.reset_pins_and_recover()
            if not self.device:
                return

        # Multi-Shade 3-Phase Plane Compiler (Compiles ph0 [>=33%], ph1 [>=67%], and lo [100%] sub-frames)
        pwm_rects = getattr(self, "_pwm_rects", None)
        gray_planes = getattr(self, "_pwm_gray_planes", None)
        raw_bytes = self.image.tobytes()
        pwm_sig = (tuple(pwm_rects) if pwm_rects else (), gray_planes[2] if gray_planes else None)
        if raw_bytes == getattr(self, "_last_frame_bytes", None) and pwm_sig == getattr(self, "_last_pwm_sig", None):
            return
        self._last_frame_bytes = raw_bytes
        self._last_pwm_sig = pwm_sig

        hi_img = ImageOps.invert(self.image.convert("L")).convert("1") if self.invert_mode else self.image
        c_hi = self._compile_page_buffers(hi_img, getattr(self, "_ui_pages_hi", None))
        self._ui_pages_hi = c_hi
        c_ph1 = c_hi
        c_lo = None
        if pwm_rects or gray_planes:
            ph1_copy = gray_planes[0].copy() if gray_planes else self.image.copy()
            lo_copy = gray_planes[1].copy() if gray_planes else self.image.copy()
            if pwm_rects:
                ph1_draw = ImageDraw.Draw(ph1_copy)
                lo_draw = ImageDraw.Draw(lo_copy)
                # Comment: lock_app.py draws 'pwm' particle caps with shade=0.50 and 'shaded'/'trail' bars with shade=0.65; clearing ph1_draw when rsh <= 0.55 gives 0.50 particles a 1/3 duty cycle (33.3% true gray, ON only in ph0) while preserving a 2/3 duty cycle (66.7% light gray, ON in ph0+ph1) for 0.65 bars.
                for rx1, ry1, rx2, ry2, rsh in pwm_rects:
                    lo_draw.rectangle((rx1, ry1, rx2, ry2), fill=0)
                    if rsh <= 0.55:
                        ph1_draw.rectangle((rx1, ry1, rx2, ry2), fill=0)
            i_ph1 = ImageOps.invert(ph1_copy.convert("L")).convert("1") if self.invert_mode else ph1_copy
            i_lo = ImageOps.invert(lo_copy.convert("L")).convert("1") if self.invert_mode else lo_copy
            c_ph1 = self._compile_page_buffers(i_ph1, c_hi)
            c_lo = self._compile_page_buffers(i_lo, c_ph1)
            # Comment (invariant): Enforce the closed-loop hardware-adaptive Hybrid PWM-vs-Dither budget (read from shm[36:40], updated by OLED_Compositor from measured io+prep+lock headroom vs oled_pwm_dt) directly on exact SH1106 vertical-page XOR bytes so any board/bus stays at 0 overruns.
            _force_pwm = getattr(self, "_gray_force_pwm", False)
            self._gray_force_pwm = False
            if gray_planes and not _force_pwm and getattr(self, "gray_mode", "hybrid") == "hybrid" and hasattr(self, "_apply_dither_img"):
                _shm_b = getattr(self, "_shm", None)
                pwm_budget = struct.unpack("<I", _shm_b[36:40])[0] if (_shm_b is not None and len(_shm_b) >= 40) else 144
                if not (48 <= pwm_budget <= 1024):
                    pwm_budget = 144
                p_costs = []
                total_pwm_b = 0
                for p in range(8):
                    x_b = (int.from_bytes(c_hi[p][1], "little") ^ int.from_bytes(c_lo[p][1], "little")).to_bytes(128, "little")
                    cost = 128 - x_b.count(0)
                    if cost > 0:
                        p_costs.append((cost, p))
                        total_pwm_b += cost
                if total_pwm_b > pwm_budget:
                    c_hi = list(c_hi)
                    c_ph1 = list(c_ph1)
                    c_lo = list(c_lo)
                    p_costs.sort(reverse=True)
                    for cost, p in p_costs:
                        if total_pwm_b <= pwm_budget:
                            break
                        y0 = p * 8
                        box = (0, y0, 128, y0 + 8)
                        b0 = hi_img.crop(box).convert("L").tobytes()
                        b1 = i_ph1.crop(box).convert("L").tobytes()
                        b2 = i_lo.crop(box).convert("L").tobytes()
                        strip_l = Image.frombytes("L", (128, 8), bytes((a + b + c) // 3 for a, b, c in zip(b0, b1, b2)))
                        d_px = self._apply_dither_img(strip_l).convert("L").tobytes()
                        pg_b = bytes(
                            (1 if d_px[x] else 0) | (2 if d_px[128 + x] else 0) | (4 if d_px[256 + x] else 0) | (8 if d_px[384 + x] else 0)
                            | (16 if d_px[512 + x] else 0) | (32 if d_px[640 + x] else 0) | (64 if d_px[768 + x] else 0) | (128 if d_px[896 + x] else 0)
                            for x in range(128)
                        )
                        c_hi[p] = (True, pg_b)
                        c_ph1[p] = (True, pg_b)
                        c_lo[p] = (True, pg_b)
                        total_pwm_b -= cost

        shm = getattr(self, "_shm", None)
        if shm is not None:
            w_slot = getattr(self, "_shm_write_slot", 0) ^ 1
            self._shm_write_slot = w_slot
            base_off = 64 if w_slot == 0 else 3136
            shm[base_off : base_off + 1024] = b"".join(p[1] for p in c_hi)
            if c_ph1 is not None and c_lo is not None:
                shm[base_off + 1024 : base_off + 2048] = b"".join(p[1] for p in c_ph1)
                shm[base_off + 2048 : base_off + 3072] = b"".join(p[1] for p in c_lo)
            ver = (struct.unpack("<I", shm[0:4])[0] + 1) & 0x7FFFFFFF
            if ver == 0:
                ver = 1
            # Stage: Full-Screen PWM Flag (Sets shm_pwm=2 when gray_planes are active so all 8 pages flush across all 3 PWM phases)
            shm_pwm_flag = 2 if gray_planes else (1 if c_lo is not None else 0)
            shm[0:20] = struct.pack("<IIIII", ver, getattr(self, "_frame_seq", 0), shm_pwm_flag, 1 if self.is_asleep else 0, w_slot)
            
    def show_test_screen(self):
        if not self.device:
            return
            
        with canvas(self.device) as draw:
            draw.text((0, 0), "VipXpert Minimal OS", fill="white")
            draw.text((0, 20), "OLED initialized.", fill="white")

    def set_invert(self, inverted: bool):
        """Toggles high-contrast inverted pixel rendering mode."""
        self.invert_mode = bool(inverted)
        self._last_frame_bytes = None
        self._prev_pages = None

    def sleep(self):
        """Physically cuts power to the OLED panel and silences I2C0 frame flushes."""
        self.is_asleep = True
        if getattr(self, "_shm", None) is not None:
            self._shm[12:16] = struct.pack("<I", 1)
        self._last_frame_bytes = None
        self._prev_pages = None
        while not self.frame_queue.empty():
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                break
        if self.device:
            try:
                with self.bus_lock:
                    self.device.hide()
            except OSError:
                pass

    def wake(self):
        """Restores power to the OLED panel and re-enables I2C0 frame flushes."""
        self.is_asleep = False
        self._last_frame_bytes = None
        self._prev_pages = None
        if self.device:
            try:
                with self.bus_lock:
                    self.device.show()
            except OSError:
                pass
        if getattr(self, "_shm", None) is not None:
            self._shm[12:16] = struct.pack("<I", 0)

    def set_brightness(self, level: int):
        """Adjusts hardware contrast register (0-255) while respecting sub-1% night drive mode."""
        self.contrast_level = max(0, min(255, level))
        self.apply_oled_drive_profile(
            getattr(self, "pwm_mode", "max"),
            getattr(self, "drive_mode", "normal")
        )

    def apply_oled_drive_profile(self, pwm_mode: str = "max", drive_mode: str = "normal"):
        """Configures SH1106/SSD1306 silicon 0xD5 PWM oscillator and 0xD9 pre-charge drive registers."""
        import math
        self.pwm_mode = "max"
        self.drive_mode = drive_mode if drive_mode in ("night", "soft", "normal", "crisp") else "normal"
        d9_map = {"night": 0x11, "soft": 0x11, "normal": 0x11, "crisp": 0x11}
        d5_val = 0xF0
        d9_val = d9_map[self.drive_mode]
        base_c = max(0, min(255, getattr(self, "contrast_level", 255)))
        if self.drive_mode == "night":
            c_val = 1
        elif self.drive_mode == "soft":
            c_val = max(8, min(53, int(round(base_c * 0.35))))
        else:
            c_val = base_c
        if self.device:
            try:
                with self.bus_lock:
                    self.device.command(0xD5, d5_val)
                    self.device.command(0xD9, d9_val)
                    self.device.contrast(c_val)
            except Exception:
                pass

    def set_gray_engine_config(self, gray_mode: str = None, dither_algo: str = None, dither_lo: int = None, dither_hi: int = None, dither_ramp: bool = None):
        """Updates universal Gray Engine parameters live and invalidates cached dither tiles/LUTs."""
        if gray_mode in ("hybrid", "pwm", "dithering", "off"):
            self.gray_mode = gray_mode
        if dither_algo in ("bayer8", "bayer4", "bayer2", "cluster", "ign", "floyd", "temporal"):
            self.dither_algo = dither_algo
        if dither_lo is not None:
            self.dither_lo = max(4, min(96, int(dither_lo)))
        if dither_hi is not None:
            self.dither_hi = max(4, min(96, int(dither_hi)))
        if dither_ramp is not None:
            self.dither_ramp = bool(dither_ramp)
        self._dither_tile_cache = None

    def _apply_dither_img(self, g_img: Image.Image, algo: str = None, d_lo: int = None, d_hi: int = None, ramp: bool = None) -> Image.Image:
        """Universal 8-bit grayscale -> 1-bit dither engine supporting 7 algorithms with calibrated density LUT."""
        from PIL import ImageChops
        w, h = g_img.size
        algo = algo or getattr(self, "dither_algo", "bayer8")
        d_lo = getattr(self, "dither_lo", 42) if d_lo is None else int(d_lo)
        d_hi = getattr(self, "dither_hi", 70) if d_hi is None else int(d_hi)
        ramp = getattr(self, "dither_ramp", True) if ramp is None else bool(ramp)
        lo_v = max(1, min(254, int(round(d_lo * 2.55))))
        hi_v = max(lo_v, min(254, int(round(d_hi * 2.55))))
        cache = getattr(self, "_dither_tile_cache", None)
        ck = (w, h, algo, lo_v, hi_v, ramp)
        if cache is None or cache[0] != ck:
            # Comment (invariant): Anchor Smooth Ramp piecewise-linearly at 85->lo_v and 170->hi_v so 4-shade Dither & Calib (0, 85, 170, 255) matches Hybrid and Dithering identically with zero luminance drift.
            if ramp:
                d_lut = [
                    0 if p < 48 else (
                        int((p - 48) * lo_v / 37.0) if p < 85 else (
                            int(lo_v + (p - 85) * (hi_v - lo_v) / 85.0) if p < 170 else (
                                int(hi_v + (p - 170) * (255 - hi_v) / 38.0) if p < 208 else 255
                            )
                        )
                    )
                    for p in range(256)
                ]
            else:
                d_lut = [0 if p < 48 else (255 if p >= 208 else (lo_v if p < 128 else hi_v)) for p in range(256)]
            nz_lut = [0] + [255] * 255
            tile_img = None
            if algo == "bayer2":
                # Comment (invariant): Center 2x2 Bayer thresholds at [32, 96, 160, 224] instead of [0, 64, 128, 192] so 75..88% Hi gray maps to 3/4 pixels ON (75%) instead of clipping to 100% solid white.
                m2 = [[32, 160], [224, 96]]
                rb = [bytes(m2[y][x & 1] for x in range(w)) for y in range(2)]
                tile_img = Image.frombytes("L", (w, h), b"".join(rb * ((h + 1) // 2))[:w * h])
            elif algo == "bayer4":
                m4 = [[0, 127, 31, 159], [191, 63, 223, 95], [47, 175, 15, 143], [239, 111, 207, 79]]
                rb = [bytes(m4[y][x & 3] for x in range(w)) for y in range(4)]
                tile_img = Image.frombytes("L", (w, h), b"".join(rb * ((h + 3) // 4))[:w * h])
            elif algo == "cluster":
                m_cl = [[192, 112, 96, 160], [128, 16, 32, 80], [144, 48, 64, 176], [240, 208, 224, 255]]
                rb = [bytes(m_cl[y][x & 3] for x in range(w)) for y in range(4)]
                tile_img = Image.frombytes("L", (w, h), b"".join(rb * ((h + 3) // 4))[:w * h])
            elif algo == "ign":
                buf = bytes(int(((52.9829189 * ((0.06711056 * x + 0.00583715 * y) % 1.0)) % 1.0) * 255.0) for y in range(h) for x in range(w))
                tile_img = Image.frombytes("L", (w, h), buf)
            elif algo == "temporal":
                m_t = [[64, 192], [192, 64]]
                rb = [bytes(m_t[y][x & 1] for x in range(w)) for y in range(2)]
                tile_img = Image.frombytes("L", (w, h), b"".join(rb * ((h + 1) // 2))[:w * h])
            elif algo != "floyd":
                m8 = [
                    [ 0, 32,  8, 40,  2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
                    [12, 44,  4, 36, 14, 46,  6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
                    [ 3, 35, 11, 43,  1, 33,  9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
                    [15, 47,  7, 39, 13, 45,  5, 37], [63, 31, 55, 23, 61, 29, 53, 21],
                ]
                rb = [bytes(int(m8[y][x & 7] * 255 / 64) for x in range(w)) for y in range(8)]
                tile_img = Image.frombytes("L", (w, h), b"".join(rb * ((h + 7) // 8))[:w * h])
            cache = (ck, tile_img, d_lut, nz_lut)
            self._dither_tile_cache = cache
        mapped = g_img.point(cache[2], mode="L")
        if algo == "floyd":
            return mapped.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
        return ImageChops.subtract(mapped, cache[1]).point(cache[3], mode="1")

    def _resolve_gray_mode(self, mode_override: str = None, allow_off_fallback: bool = False) -> str:
        """Resolves the active system-wide Gray/Dither mode ('pwm' ★, 'spatial', 'temporal', or 'off')."""
        # Comment (decision): Use self.gray_mode ('hybrid', 'pwm', 'dithering', 'off') as the single source of truth; falling back to legacy self.dither_mode caused 'hybrid'/'pwm' to stay unshaded whenever oled_dither_mode defaulted to 'off'.
        if mode_override:
            mode = mode_override
        else:
            gm = getattr(self, "gray_mode", "hybrid")
            if gm == "dithering":
                mode = "temporal" if getattr(self, "dither_algo", "bayer8") == "temporal" else "spatial"
            elif gm == "off":
                mode = "off"
            else:
                mode = "pwm"
        if mode == "off" and allow_off_fallback:
            return "pwm"
        return mode if mode in ("pwm", "spatial", "temporal", "off") else "pwm"

    @staticmethod
    def _pwm_delta_sigma_on(frame_seq: int, phase_id: int, shade: float) -> bool:
        """
        Stateless 1st-Order Delta-Sigma Sub-Frame PWM Modulator (100% Solid, Dot-Free!).
        Maps `shade` into high-frequency duty cycles where the element is NEVER dark for 2 consecutive frames:
          - shade < 0.58 -> 1/2 duty (1 frame ON, 1 frame OFF = fastest possible Nyquist 1:1 alternation)
          - shade < 0.72 -> 2/3 duty (2 frames ON, 1 frame OFF = smooth medium gray)
          - shade < 0.85 -> 3/4 duty (3 frames ON, 1 frame OFF = bright silver gray)
          - shade >= 0.85-> 4/5 duty (4 frames ON, 1 frame OFF)
        Because an OFF pulse never lasts more than 1 single frame, retinal persistence is maximized with ZERO dots!
        """
        s = frame_seq + phase_id
        if shade < 0.58:
            return (s & 1) == 0
        if shade < 0.72:
            return (s % 3) != 2
        if shade < 0.85:
            return (s & 3) != 3
        return (s % 5) != 4

    def draw_shaded_rect(self, x1: int, y1: int, x2: int, y2: int, shade: float = 0.5, mode_override: str = None, phase_id: int = 0):
        """
        System-Wide 2D Grayscale Primitive (100% unified with render_wireframe_3d via self.dither_mode):
        - 'pwm' ★: 100% SOLID, DOT-FREE geometry driven by _pwm_delta_sigma_on() (max 1-frame dark gap).
        - 'spatial': Static 17-level Ordered 4x4 Bayer matrix.
        - 'temporal': 50% 2x2 alternating checkerboard.
        """
        if not self.device or not self.draw:
            return
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(127, int(x2)), min(63, int(y2))
        if x1 > x2 or y1 > y2 or shade <= 0.02:
            return
        if shade >= 0.96:
            self.draw.rectangle((x1, y1, x2, y2), fill=1)
            return

        mode = self._resolve_gray_mode(mode_override, allow_off_fallback=True)
        if mode == "pwm":
            self.has_active_marquee = True
            self._last_p0_lit = 1
            self.draw.rectangle((x1, y1, x2, y2), fill=1)
            if not hasattr(self, "_pwm_rects"):
                self._pwm_rects = []
            self._pwm_rects.append((x1, y1, x2, y2, float(shade)))
            return
        if mode == "spatial":
            # Comment (decision): Route 2D shaded rects through the universal _apply_dither_img() engine so all 7 dither_algo modes and calibrated densities apply system-wide.
            rw, rh = x2 - x1 + 1, y2 - y1 + 1
            val = max(0, min(255, int(round(shade * 255.0))))
            d_patch = self._apply_dither_img(Image.new("L", (rw, rh), val))
            self.image.paste(d_patch, (x1, y1))
            return
        px = self.image.load()
        if mode == "temporal":
            self.has_active_marquee = True
            parity = getattr(self, "_frame_seq", 0) & 1
            for y in range(y1, y2 + 1):
                for x in range(x1, x2 + 1):
                    px[x, y] = 1 if ((x + y + parity) & 1) == 0 else 0

    def draw_gray_frame(self, x0: int, y0: int, w: int, h: int, gray_bytes: bytes, mode_override: str = None):
        # Universal 8-Bit Grayscale Engine (supports gray_mode: 'hybrid' [default], 'pwm', 'dithering')
        if not self.device or not self.draw:
            return
        g_img = Image.frombytes("L", (w, h), gray_bytes)
        gm = mode_override if mode_override in ("hybrid", "pwm", "dithering", "off") else getattr(self, "gray_mode", "hybrid")
        self._gray_force_pwm = (gm == "pwm")
        if gm == "off":
            bw_lut = getattr(self, "_bw_thresh_lut", None)
            if bw_lut is None:
                bw_lut = [255 if p >= 128 else 0 for p in range(256)]
                self._bw_thresh_lut = bw_lut
            self.image.paste(g_img.point(bw_lut, mode="1"), (x0, y0))
            self._pwm_gray_planes = None
            self.has_active_marquee = True
            return
        if gm == "dithering":
            self.image.paste(self._apply_dither_img(g_img), (x0, y0))
            self._pwm_gray_planes = None
            self.has_active_marquee = True
            return
        # Comment (decision): Emit clean 3-phase planes for both 'hybrid' and 'pwm' here; Display.show() enforces the unified SH1106 vertical-page <=176B Hybrid budget across all primitives on the frame.
        luts = getattr(self, "_pwm_gray_luts", None)
        if luts is None:
            luts = (
                [255 if p >= 48 else 0 for p in range(256)],
                [255 if p >= 128 else 0 for p in range(256)],
                [255 if p >= 208 else 0 for p in range(256)],
            )
            self._pwm_gray_luts = luts
        b_ph0 = g_img.point(luts[0], mode="1")
        b_ph1 = g_img.point(luts[1], mode="1")
        b_lo = g_img.point(luts[2], mode="1")
        # Comment (invariant): Preserve existing _pwm_gray_planes (e.g. from render_wireframe_3d on the same frame) instead of re-snapshotting self.image, so multiple gray primitives on one screen keep all 3 PWM planes intact.
        prev_planes = getattr(self, "_pwm_gray_planes", None)
        self.image.paste(b_ph0, (x0, y0))
        ph1_full = prev_planes[0].copy() if prev_planes else self.image.copy()
        lo_full = prev_planes[1].copy() if prev_planes else self.image.copy()
        ph1_full.paste(b_ph1, (x0, y0))
        lo_full.paste(b_lo, (x0, y0))
        prev_sig = prev_planes[2] if prev_planes else None
        self._pwm_gray_planes = (ph1_full, lo_full, (prev_sig, hash(gray_bytes)))
        self.has_active_marquee = True

    def render_wireframe_3d(self, vertices, edges, angle_x: float, angle_y: float, angle_z: float = 0.0, scale: float = 18.0, cx: int = 64, cy: int = 32, faces=None, dist: float = 4.2):
        """
        System-Wide 3D Wireframe & Solid-Shaded Engine (Cube & Octahedron).
        Shares the exact same self.dither_mode ('pwm' ★ dot-free solid sub-frame PWM, 'spatial' 4x4 Bayer,
        and 'temporal' 2x2) as draw_shaded_rect()!
        """
        if not self.device or not self.draw:
            return
        import math
        cx_a, sx_a = math.cos(angle_x), math.sin(angle_x)
        cy_a, sy_a = math.cos(angle_y), math.sin(angle_y)
        cz_a, sz_a = math.cos(angle_z), math.sin(angle_z)

        rot_v = []
        proj = []
        for vx, vy, vz in vertices:
            y1 = vy * cx_a - vz * sx_a
            z1 = vy * sx_a + vz * cx_a
            x2 = vx * cy_a + z1 * sy_a
            z2 = -vx * sy_a + z1 * cy_a
            x3 = x2 * cz_a - y1 * sz_a
            y3 = x2 * sz_a + y1 * cz_a
            rot_v.append((x3, y3, z2))
            fov = dist / max(1.8, dist - z2 * 0.45)
            px_coord = int(round(cx + x3 * scale * fov))
            py_coord = int(round(cy + y3 * scale * fov))
            proj.append((px_coord, py_coord, z2))

        if faces is None:
            if len(vertices) == 8:
                faces = ((0,1,2,3), (4,7,6,5), (0,4,5,1), (2,6,7,3), (0,3,7,4), (1,5,6,2))
            elif len(vertices) == 6:
                faces = ((0,2,4), (0,4,3), (0,3,5), (0,5,2), (1,4,2), (1,3,4), (1,5,3), (1,2,5))
        dither_mode = self._resolve_gray_mode(allow_off_fallback=False)
        if faces and dither_mode in ("pwm", "spatial", "temporal"):
            if dither_mode in ("pwm", "temporal"):
                self.has_active_marquee = True
            px = self.image.load()
            f_seq = getattr(self, "_frame_seq", 0)
            parity = f_seq & 1

            vis_faces = []
            for f_idx, f_verts in enumerate(faces):
                i0, i1, i2 = f_verts[0], f_verts[1], f_verts[2]
                x0, y0, _ = proj[i0]
                x1, y1, _ = proj[i1]
                x2, y2, _ = proj[i2]
                cross_z = (x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0)
                if cross_z >= 0:
                    continue
                n_fv = len(f_verts)
                avg_z = sum(rot_v[idx][2] for idx in f_verts) / float(n_fv)
                ux, uy, uz = rot_v[i1][0] - rot_v[i0][0], rot_v[i1][1] - rot_v[i0][1], rot_v[i1][2] - rot_v[i0][2]
                wx, wy, wz = rot_v[i2][0] - rot_v[i0][0], rot_v[i2][1] - rot_v[i0][1], rot_v[i2][2] - rot_v[i0][2]
                nx = uy * wz - uz * wy
                ny = uz * wx - ux * wz
                nz = ux * wy - uy * wx
                n_len = max(1e-6, math.sqrt(nx * nx + ny * ny + nz * nz))
                nx, ny, nz = nx / n_len, ny / n_len, nz / n_len
                lambert = max(0.0, nx * (-0.38) + ny * (-0.48) + nz * 0.79)
                # Map Lambertian illumination across the 1-frame-max-off PWM persistence tiers (0.50 .. 0.90)
                shade = min(0.90, max(0.48, 0.48 + 0.42 * lambert))
                vis_faces.append((avg_z, f_idx, f_verts, shade))

            # 3-Phase Multi-Shade 3D Polygon Rasterizer (Maps Lambertian shade to 33%, 67%, or 100% hardware PWM planes)
            vis_faces.sort(key=lambda item: item[0])
            ph1_img = self.image.copy() if dither_mode == "pwm" else None
            lo_img = self.image.copy() if dither_mode == "pwm" else None
            px_ph1 = ph1_img.load() if ph1_img is not None else None
            px_lo = lo_img.load() if lo_img is not None else None
            # Comment (bugfix): Keep 4x4 Bayer table local to render_wireframe_3d so removing module-level _BAYER_4X4 never aborts shaded 3D rendering in pwm/spatial/temporal modes.
            bayer4 = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))

            for _, f_idx, f_verts, shade in vis_faces:
                poly = [(proj[i][0], proj[i][1]) for i in f_verts]
                min_y = max(0, min(p[1] for p in poly))
                max_y = min(63, max(p[1] for p in poly))
                n_p = len(poly)
                t_eff = max(2, min(14, int(round(shade * 16.0))))
                on_ph1 = 1 if shade >= 0.66 else 0
                on_lo = 1 if shade >= 0.84 else 0

                for y in range(min_y, max_y + 1):
                    nodes = []
                    j = n_p - 1
                    for i in range(n_p):
                        yi, yj = poly[i][1], poly[j][1]
                        if (yi < y <= yj) or (yj < y <= yi):
                            xi, xj = poly[i][0], poly[j][0]
                            nodes.append(int(xi + (y - yi) * (xj - xi) / float(yj - yi)))
                        j = i
                    nodes.sort()
                    for k in range(0, len(nodes) - 1, 2):
                        sx0 = max(0, nodes[k])
                        sx1 = min(127, nodes[k + 1])
                        if dither_mode == "pwm":
                            for x in range(sx0, sx1 + 1):
                                px[x, y] = 1
                                px_ph1[x, y] = on_ph1
                                px_lo[x, y] = on_lo
                        elif dither_mode == "spatial":
                            b_row = bayer4[y & 3]
                            for x in range(sx0, sx1 + 1):
                                px[x, y] = 1 if b_row[x & 3] < t_eff else 0
                        else:
                            for x in range(sx0, sx1 + 1):
                                px[x, y] = 1 if ((x + y + parity + f_idx) & 1) == 0 else 0

            if dither_mode == "pwm" and ph1_img is not None and lo_img is not None:
                ed_ph1 = ImageDraw.Draw(ph1_img)
                ed_lo = ImageDraw.Draw(lo_img)
                for i, j in edges:
                    if i < len(proj) and j < len(proj):
                        ed_ph1.line((proj[i][0], proj[i][1], proj[j][0], proj[j][1]), fill=1)
                        ed_lo.line((proj[i][0], proj[i][1], proj[j][0], proj[j][1]), fill=1)
                self._pwm_gray_planes = (ph1_img, lo_img, (round(angle_x, 3), round(angle_y, 3), round(scale, 1), len(vertices)))

        for i, j in edges:
            if i < len(proj) and j < len(proj):
                self.draw.line((proj[i][0], proj[i][1], proj[j][0], proj[j][1]), fill=1)
