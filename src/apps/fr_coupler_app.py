# /vipxpert_minimal_os/src/apps/fr_coupler_app.py
import asyncio
import json
import math
import os
import signal
import time
from typing import List, Optional
from src.ui.base_app import BaseApp

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DSP_BIN = os.path.join(_REPO_ROOT, "bin", "fr_meas_dsp")
_EXPORT_DIR = os.path.join(_REPO_ROOT, "fr_measurements")


class FRCouplerApp(BaseApp):
    """
    IEC-711 Acoustic Coupler / Frequency Response measurement app using onboard I2S mics,
    quiet-room noise gate, and native Rust Farina log-sine sweep deconvolution (`bin/fr_meas_dsp`).
    """

    def __init__(self, display, ui_manager, db, parent_app_id: str = "audio_settings"):
        super().__init__(display)
        self.ui = ui_manager
        self.db = db
        self.parent_app_id = parent_app_id
        self.measuring = False
        self.status_msg = "Press C to Sweep"
        self.points: List[float] = []
        self.gate_passed: Optional[bool] = None
        self.noise_db: float = -120.0
        self.snr_db: float = 0.0
        self.cursor_idx: int = 60  # ~630 Hz default
        self.comp_mode: str = "none"  # 'none' (RAW) | 'iec711' (COMP)
        self.smooth_oct: int = 12       # 6 | 12 | 24
        self.channel: str = "avg"       # 'avg' | 'L' | 'R'
        self.last_rec_wav = "/tmp/vip_fr_rec.wav"

    async def on_start(self):
        self.ui.request_render()

    async def on_stop(self):
        self.measuring = False

    async def update(self):
        self._draw_state()
        self.display.show()

    def _idx_to_freq(self, idx: int) -> float:
        frac = max(0, min(119, idx)) / 119.0
        return 20.0 * (1000.0 ** frac)

    async def handle_key(self, action: str, char: str):
        if action not in ("CLICK", "HOLD", "PRESS"):
            return
        if char == 'D' and action == "CLICK":
            await self.ui.switch_app(self.parent_app_id)
            return
        if self.measuring:
            return
        if char in ('C', '5') and action == "CLICK":
            asyncio.create_task(self._run_measurement())
        elif char in ('4', '<') and action in ("CLICK", "HOLD"):
            step = 4 if action == "HOLD" else 1
            self.cursor_idx = max(0, self.cursor_idx - step)
            self.ui.request_render()
        elif char in ('6', '>') and action in ("CLICK", "HOLD"):
            step = 4 if action == "HOLD" else 1
            self.cursor_idx = min(119, self.cursor_idx + step)
            self.ui.request_render()
        elif char == 'A' and action == "CLICK":
            self.comp_mode = "none" if self.comp_mode == "iec711" else "iec711"
            if os.path.isfile(self.last_rec_wav) and self.points:
                asyncio.create_task(self._reanalyze_last())
            else:
                self.ui.request_render()
        elif char == 'B' and action == "CLICK":
            opts = [6, 12, 24]
            self.smooth_oct = opts[(opts.index(self.smooth_oct) + 1) % len(opts)]
            if os.path.isfile(self.last_rec_wav) and self.points:
                asyncio.create_task(self._reanalyze_last())
            else:
                self.ui.request_render()
        # Comment (constraint): Key '*' is reserved globally for Lock Screen; use '2' for mic channel cycle.
        elif char == '2' and action == "CLICK":
            opts = ["avg", "L", "R", "usb"]
            prev_ch = self.channel
            self.channel = opts[(opts.index(self.channel) + 1) % len(opts)]
            if self.channel != "usb" and prev_ch != "usb" and os.path.isfile(self.last_rec_wav) and self.points:
                asyncio.create_task(self._reanalyze_last())
            else:
                self.ui.request_render()
        elif char == '#' and action == "CLICK":
            if os.path.isfile(self.last_rec_wav) and self.points:
                asyncio.create_task(self._export_txt())
            else:
                self.ui.show_toast("Measure First!", duration=1.4)

    async def _run_measurement(self):
        self.measuring = True
        self.status_msg = "Generating Sweep..."
        self.ui.request_render()
        sweep_wav = "/tmp/vip_fr_sweep.wav"
        rec_wav = self.last_rec_wav
        kernel = getattr(self.ui, "os_kernel", None)
        try:
            if not os.path.isfile(_DSP_BIN):
                self.status_msg = "Missing fr_meas_dsp"
                return

            p_gen = await asyncio.create_subprocess_exec(
                _DSP_BIN, "gen", sweep_wav,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(p_gen.communicate(), timeout=5.0)
            if p_gen.returncode != 0:
                self.status_msg = "Sweep Gen Failed"
                return

            # Comment (invariant): Unlink stale rec_wav before capture; USB arecord runs as root while I2S pw-record runs as vipxpert, so a leftover root-owned file blocks pw-record.
            try:
                os.remove(rec_wav)
            except FileNotFoundError:
                pass

            if self.channel == "usb":
                usb_dev = await self._find_usb_capture_device()
                if not usb_dev:
                    self.status_msg = "No USB Mic Found"
                    return
                self.status_msg = "USB Sweep (Quiet)..."
                self.ui.request_render()
                # Comment (hardware): USB-C dongles (e.g. BGVP MX1) natively expose S16_LE/S24_3LE; S32_LE via plughw can record silence.
                rec_cmd = f"arecord -D {usb_dev} -f S16_LE -r 48000 -c 1 -d 4 {rec_wav}"
            else:
                if kernel and hasattr(kernel, "enforce_ahub0_24bit_i2s"):
                    kernel.enforce_ahub0_24bit_i2s()
                self.status_msg = "Sweeping (Quiet!)..."
                self.ui.request_render()
                # Comment (order): Start pw-record slightly before pw-play so the 0.6s pre-sweep quiet-room gate window is captured cleanly.
                rec_cmd = (
                    "sudo -u vipxpert env XDG_RUNTIME_DIR=/run/user/1000 "
                    "timeout -s INT -k 0.5s 4.2s pw-record "
                    "--target=alsa_input.platform-soc_ahub0_mach.stereo-fallback "
                    f"--rate=48000 --channels=2 --format=s32 {rec_wav}"
                )
            play_cmd = (
                "sudo -u vipxpert env XDG_RUNTIME_DIR=/run/user/1000 "
                f"pw-play {sweep_wav}"
            )
            p_rec = await asyncio.create_subprocess_shell(
                rec_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True
            )
            await asyncio.sleep(0.08)
            p_play = await asyncio.create_subprocess_shell(
                play_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True
            )
            try:
                await asyncio.wait_for(asyncio.gather(p_rec.wait(), p_play.wait()), timeout=6.5)
            except Exception:
                for p in (p_rec, p_play):
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except Exception:
                        pass

            if kernel and hasattr(kernel, "enforce_ahub0_24bit_i2s"):
                kernel.enforce_ahub0_24bit_i2s()
            await self.ui.sync_mic_dsp()

            if not os.path.isfile(rec_wav):
                self.status_msg = "Capture Failed"
                return
            await self._analyze_wav(rec_wav)
        except Exception as e:
            self.status_msg = f"Err: {str(e)[:18]}"
        finally:
            self.measuring = False
            self.ui.request_render()

    async def _find_usb_capture_device(self) -> Optional[str]:
        """Discovers the first connected USB Audio ALSA capture device (e.g. plughw:2,0 for BGVP MX1)."""
        import re
        p = await asyncio.create_subprocess_exec(
            "arecord", "-l", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(p.communicate(), timeout=2.0)
        for line in out.decode("utf-8", errors="ignore").splitlines():
            if "USB Audio" in line:
                m = re.search(r"card\s+(\d+):.*device\s+(\d+):", line)
                if m:
                    return f"plughw:{m.group(1)},{m.group(2)}"
        return None

    async def _reanalyze_last(self):
        self.measuring = True
        self.status_msg = "Re-analyzing..."
        self.ui.request_render()
        try:
            await self._analyze_wav(self.last_rec_wav)
        finally:
            self.measuring = False
            self.ui.request_render()

    async def _analyze_wav(self, wav_path: str, out_txt: str = ""):
        self.status_msg = "Analyzing FFT..."
        self.ui.request_render()
        args = [
            _DSP_BIN, "analyze", wav_path,
            "--channel", "avg" if self.channel == "usb" else self.channel,
            "--comp", self.comp_mode,
            "--smooth", str(self.smooth_oct),
        ]
        if out_txt:
            args.extend(["--out-txt", out_txt])
        p = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(p.communicate(), timeout=8.0)
        if p.returncode != 0:
            self.status_msg = "Analyze Failed"
            return
        data = json.loads(out.decode("utf-8").strip())
        if not data.get("ok"):
            self.status_msg = data.get("error", "DSP Error")[:20]
            return
        self.points = [float(x) for x in data.get("points_120", [])]
        self.gate_passed = bool(data.get("gate_passed", False))
        self.noise_db = float(data.get("noise_rms_db", -120.0))
        self.snr_db = float(data.get("snr_db", 0.0))
        self.status_msg = ""

    async def _export_txt(self):
        os.makedirs(_EXPORT_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"fr_{ts}_{self.comp_mode}.txt"
        fpath = os.path.join(_EXPORT_DIR, fname)
        await self._analyze_wav(self.last_rec_wav, out_txt=fpath)
        if os.path.isfile(fpath):
            self.ui.show_toast(f"Saved {fname[:16]}", duration=1.8)
        else:
            self.ui.show_toast("Export Failed", duration=1.5)
        self.ui.request_render()

    def _draw_state(self):
        self.display.clear()
        d = self.display.draw
        font = self.display.font_small

        # Header bar (y=0..10), compact to prevent 128px overflow
        comp_tag = "CMP" if self.comp_mode == "iec711" else "RAW"
        ch_tag = self.channel[0].upper()
        hdr = f"FR {comp_tag} 1/{self.smooth_oct} {ch_tag}"
        d.text((2, 0), hdr, font=font, fill=1)
        if self.gate_passed is not None:
            gate_txt = "OK" if self.gate_passed else "NOIS"
            d.text((100, 0), gate_txt, font=font, fill=1)
        d.line([(0, 10), (127, 10)], fill=1)

        # Graph area: x=4..123 (120 px), y=12..52 (40 px)
        gx0, gy0, gh = 4, 12, 40
        gy_mid = gy0 + gh // 2

        if self.measuring or not self.points:
            msg = self.status_msg or "Press C to Sweep"
            d.text((4, 20), msg[:20], font=font, fill=1)
            d.text((4, 34), "C:Run 4/6:Cur D:Back", font=font, fill=1)
            d.text((4, 46), "A:Cmp B:Sm 2:Ch #:Sv", font=font, fill=1)
        else:
            # Auto-scale Y-axis symmetrically around 0dB so peaks/dips never clip
            peak_abs = max((abs(v) for v in self.points[:120]), default=12.0)
            y_span = 12.0
            for step in (12.0, 18.0, 24.0, 36.0, 48.0, 60.0):
                if peak_abs * 1.1 <= step:
                    y_span = step
                    break
            else:
                y_span = math.ceil(peak_abs / 12.0) * 12.0

            # 0 dB dashed centerline + 100Hz / 1kHz / 10kHz vertical tick marks
            for x in range(gx0, gx0 + 120, 4):
                d.point((x, gy_mid), fill=1)
            for f_mark in (100.0, 1000.0, 10000.0):
                fx = gx0 + int(round((math.log10(f_mark / 20.0) / 3.0) * 119.0))
                d.line([(fx, gy0), (fx, gy0 + 2)], fill=1)
                d.line([(fx, gy0 + gh - 2), (fx, gy0 + gh)], fill=1)

            # Plot 120-point FR curve with auto-scaled Y span
            prev = None
            half_h = (gh // 2) - 1
            for i, db in enumerate(self.points[:120]):
                clamped = max(-y_span, min(y_span, db))
                py = gy_mid - int(round((clamped / y_span) * half_h))
                px = gx0 + i
                if prev is not None:
                    d.line([prev, (px, py)], fill=1)
                prev = (px, py)

            # Cursor crosshair
            cx = gx0 + self.cursor_idx
            c_db = self.points[min(self.cursor_idx, len(self.points) - 1)]
            cy = gy_mid - int(round((max(-y_span, min(y_span, c_db)) / y_span) * half_h))
            for y in range(gy0, gy0 + gh, 3):
                d.point((cx, y), fill=1)
            d.rectangle([cx - 1, cy - 1, cx + 1, cy + 1], outline=1, fill=1)

            # Compact footer readout (y=54..63)
            cf = self._idx_to_freq(self.cursor_idx)
            f_str = f"{cf/1000.0:.1f}k" if cf >= 1000.0 else f"{int(round(cf))}Hz"
            d.text((2, 54), f"{f_str} {c_db:+.1f}dB", font=font, fill=1)
            d.text((88, 54), f"+/-{int(y_span)}", font=font, fill=1)

        self.display.show()
