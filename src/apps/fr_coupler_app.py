# /vipxpert_minimal_os/src/apps/fr_coupler_app.py
import asyncio
import json
import math
import os
import signal
import time
from typing import Dict, List, Optional
from src.ui.base_app import BaseApp
from src.ui.components import ListMenuApp, TextInputModal, CrudListApp

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DSP_BIN = os.path.join(_REPO_ROOT, "bin", "fr_meas_dsp")
_EXPORT_DIR = os.path.join(_REPO_ROOT, "fr_measurements")
_LIB_FILE = os.path.join(_EXPORT_DIR, "library.json")
_DEFAULT_MICS = ["Onboard I2S", "USB 3.5mm"]


def _load_fr_library() -> Dict:
    os.makedirs(_EXPORT_DIR, exist_ok=True)
    if os.path.isfile(_LIB_FILE):
        try:
            with open(_LIB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                mics = list(dict.fromkeys(data.get("mics") or _DEFAULT_MICS))
                graphs = data.get("graphs") if isinstance(data.get("graphs"), list) else []
                return {"mics": mics, "graphs": graphs}
        except Exception:
            pass
    return {"mics": list(_DEFAULT_MICS), "graphs": []}


def _save_fr_library(data: Dict):
    os.makedirs(_EXPORT_DIR, exist_ok=True)
    tmp = _LIB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _LIB_FILE)


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
        self.loaded_name: str = ""
        self.loaded_mic: str = ""

    def load_saved_graph(self, entry: Dict):
        self.points = [float(x) for x in entry.get("points", [])]
        self.comp_mode = str(entry.get("comp_mode", "none"))
        self.smooth_oct = int(entry.get("smooth_oct", 12))
        self.channel = str(entry.get("channel", "avg"))
        self.noise_db = float(entry.get("noise_db", -120.0))
        self.gate_passed = entry.get("gate_passed", True)
        self.loaded_name = str(entry.get("name", ""))
        self.loaded_mic = str(entry.get("mic", ""))
        self.status_msg = ""

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
            if self.points:
                asyncio.create_task(self._start_save_wizard())
            else:
                self.ui.show_toast("Measure First!", duration=1.4)
        elif char == '8' and action == "CLICK":
            lib_app = FRGraphLibraryApp(self.display, self.ui, self.db, parent_app_id="fr_coupler")
            await self.ui.switch_app("fr_library", fallback_app=lib_app)

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

    async def _start_save_wizard(self):
        """DRY save flow: universal ListMenuApp (Pick/Add Mic) -> TextInputModal (Graph Name)."""
        lib = _load_fr_library()
        mics = lib["mics"]

        async def _ask_graph_name(chosen_mic: str):
            default_name = f"FR {time.strftime('%m%d %H%M')}"

            async def _on_name(name_str: str):
                clean = (name_str or "").strip() or default_name
                await self._commit_saved_graph(clean, chosen_mic)
                await self.ui.switch_app("fr_coupler", fallback_app=self)

            async def _on_cancel():
                await self.ui.switch_app("fr_coupler", fallback_app=self)

            modal = TextInputModal(
                self.display, self.ui, f"Name ({chosen_mic[:8]})",
                on_complete=_on_name, on_cancel=_on_cancel, default_text=default_name
            )
            await self.ui.switch_app("fr_name_input", fallback_app=modal)

        async def _add_new_mic():
            async def _on_mic(mic_str: str):
                m_clean = (mic_str or "").strip()
                if m_clean:
                    cur = _load_fr_library()
                    if m_clean not in cur["mics"]:
                        cur["mics"].append(m_clean)
                        _save_fr_library(cur)
                    await _ask_graph_name(m_clean)
                else:
                    await self.ui.switch_app("fr_coupler", fallback_app=self)

            async def _on_cancel():
                await self.ui.switch_app("fr_coupler", fallback_app=self)

            modal = TextInputModal(
                self.display, self.ui, "New Mic Name",
                on_complete=_on_mic, on_cancel=_on_cancel, default_text=""
            )
            await self.ui.switch_app("fr_mic_input", fallback_app=modal)

        mic_items = [
            {"name": m, "action": (lambda mm=m: asyncio.create_task(_ask_graph_name(mm)))}
            for m in mics
        ]
        static_items = [
            {"name": "+ Add New Mic", "action": (lambda: asyncio.create_task(_add_new_mic()))}
        ]
        picker = ListMenuApp(
            self.display, self.ui, "Select Mic", mic_items,
            static_items=static_items, sort_methods=BaseApp.STANDARD_TEXT_SORTS,
            parent_app_id="fr_coupler"
        )
        await self.ui.switch_app("fr_mic_picker", fallback_app=picker)

    async def _commit_saved_graph(self, name: str, mic: str):
        lib = _load_fr_library()
        if mic not in lib["mics"]:
            lib["mics"].append(mic)
        ts_num = int(time.time())
        entry = {
            "id": f"fr_{ts_num}",
            "name": name,
            "mic": mic,
            "ts": ts_num,
            "comp_mode": self.comp_mode,
            "smooth_oct": self.smooth_oct,
            "channel": self.channel,
            "noise_db": round(self.noise_db, 2),
            "gate_passed": self.gate_passed,
            "points": [round(float(x), 2) for x in self.points[:120]],
        }
        lib["graphs"].append(entry)
        _save_fr_library(lib)
        self.loaded_name = name
        self.loaded_mic = mic
        if os.path.isfile(self.last_rec_wav):
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:24]
            fpath = os.path.join(_EXPORT_DIR, f"{safe}_{ts_num}.txt")
            try:
                await self._analyze_wav(self.last_rec_wav, out_txt=fpath)
            except Exception:
                pass
        self.ui.show_toast(f"Saved: {name[:14]}", duration=1.6)

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


class FRGraphLibraryApp(ListMenuApp):
    """DRY Saved FR Graphs browser grouped by Mic Category with universal '#' sort-order cycling."""

    def __init__(self, display, ui_manager, db, parent_app_id: str = "audio_settings"):
        self.db = db
        sort_modes = {
            "A-Z": (lambda x: x.get("name", "").lower(), False),
            "Z-A": (lambda x: x.get("name", "").lower(), True),
            "Count": (lambda x: x.get("_count", 0), True),
        }
        super().__init__(
            display, ui_manager, "FR Graphs (Mic)", [],
            sort_methods=sort_modes, parent_app_id=parent_app_id
        )
        self._rebuild_categories()

    async def on_start(self):
        self._rebuild_categories()
        await super().on_start()

    def _rebuild_categories(self):
        lib = _load_fr_library()
        graphs = lib["graphs"]
        counts: Dict[str, int] = {}
        for g in graphs:
            m = g.get("mic", "Unknown")
            counts[m] = counts.get(m, 0) + 1
        all_mics = list(dict.fromkeys(lib["mics"] + list(counts.keys())))
        items = []
        for m in all_mics:
            c = counts.get(m, 0)
            items.append({
                "name": m,
                "suffix": f"({c})",
                "_count": c,
                "action": (lambda mm=m: asyncio.create_task(self._open_category(mm)))
            })
        static_items = [
            {
                "name": "All Graphs",
                "suffix": f"({len(graphs)})",
                "action": (lambda: asyncio.create_task(self._open_category(None)))
            }
        ]
        self.update_items(items, preserve_cursor=True, new_static_items=static_items)

    async def _open_category(self, mic_filter: Optional[str]):
        lib = _load_fr_library()
        graphs = [g for g in lib["graphs"] if mic_filter is None or g.get("mic") == mic_filter]
        title = f"Mic: {mic_filter[:10]}" if mic_filter else "All FR Graphs"

        def _build_rows(g_list):
            rows = []
            for g in g_list:
                rows.append({
                    "id": g.get("id", ""),
                    "name": g.get("name", "Untitled"),
                    "suffix": g.get("mic", "")[:6] if mic_filter is None else "",
                    "_ts": int(g.get("ts", 0)),
                    "_mic": str(g.get("mic", "")).lower(),
                    "_entry": g,
                    "action": (lambda entry=g: asyncio.create_task(self._open_saved_graph(entry)))
                })
            return rows

        def _on_delete(item):
            gid = item.get("id")
            cur = _load_fr_library()
            cur["graphs"] = [g for g in cur["graphs"] if g.get("id") != gid]
            _save_fr_library(cur)
            self.ui.show_toast("Deleted Graph", duration=1.2)
            remaining = [g for g in cur["graphs"] if mic_filter is None or g.get("mic") == mic_filter]
            crud.raw_dynamic_items = _build_rows(remaining)
            crud._apply_mode("NORMAL")

        sort_modes = {
            "Newest": (lambda x: x.get("_ts", 0), True),
            "Oldest": (lambda x: x.get("_ts", 0), False),
            "A-Z": (lambda x: x.get("name", "").lower(), False),
            "Z-A": (lambda x: x.get("name", "").lower(), True),
            "Mic": (lambda x: (x.get("_mic", ""), -x.get("_ts", 0)), False),
        }
        crud = CrudListApp(
            self.display, self.ui, title, _build_rows(graphs),
            crud_config={"on_delete": _on_delete},
            sort_methods=sort_modes, default_sort_key="Newest",
            parent_app_id="fr_library"
        )
        await self.ui.switch_app("fr_library_cat", fallback_app=crud)

    async def _open_saved_graph(self, entry: Dict):
        app = FRCouplerApp(self.display, self.ui, self.db, parent_app_id="fr_library_cat")
        app.load_saved_graph(entry)
        await self.ui.switch_app("fr_coupler", fallback_app=app)
