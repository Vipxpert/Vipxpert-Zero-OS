# /vipxpert_minimal_os/src/apps/audio_settings_app.py
from src.ui.components import ListMenuApp, ConfirmModal

class AudioSettingsApp(ListMenuApp):
    """Dedicated DSP and Audio Settings interface."""
    def __init__(self, display, ui_manager, db):
        self.db = db
        self._test_running = False
        self.current_menu = "root"
        super().__init__(
            display=display,
            ui_manager=ui_manager,
            title="Audio Settings",
            items=[]
        )

    async def on_start(self):
        self.current_menu = "root"
        self._rebuild_menu(preserve_cursor=False)
        if hasattr(super(), 'on_start'):
            await super().on_start()

    async def on_resume(self, previous_app_id=None):
        self._rebuild_menu(preserve_cursor=True)
        if hasattr(super(), 'on_resume'):
            await super().on_resume(previous_app_id=previous_app_id)

    def _open_submenu(self, menu_id: str):
        """Navigates into a categorized audio sub-menu and resets the cursor to row 0."""
        self.current_menu = menu_id
        self.is_tuning = False
        self._rebuild_menu(preserve_cursor=False)
        self.ui.request_render()

    async def handle_key(self, action: str, char: str):
        """Intercepts Back (Key 'D' CLICK) inside sub-menus to return to parent Audio Settings menu before exiting."""
        if not getattr(self, 'is_tuning', False) and self.current_menu != "root":
            if char == 'D' and action == 'CLICK':
                if self.current_menu == "walkplay_band":
                    self.current_menu = "walkplay_peq"
                elif self.current_menu == "walkplay_peq":
                    self.current_menu = "walkplay_dsp"
                elif self.current_menu == "walkplay_dsp":
                    self.current_menu = "usb_dac"
                else:
                    self.current_menu = "root"
                self._rebuild_menu(preserve_cursor=False)
                self.ui.request_render()
                return
        await super().handle_key(action, char)

    # Stage: Offline Walkplay / KTMicro USB DAC HID Control (BGVP MX1 & compatible 0x4B dongles)
    def _refresh_walkplay_state(self, toast: bool = False):
        from src.hardware.walkplay_hid import discover_walkplay_device, WalkplayHID
        dev = discover_walkplay_device()
        self._wp_dev = dev
        if not dev:
            self._wp_fw = ""
            self._wp_peq_en = False
            self._wp_bands = []
            if toast:
                self.show_info_toast("No Walkplay DAC")
            return
        try:
            with WalkplayHID(dev["path"]) as wp:
                self._wp_fw = wp.read_firmware()
                self._wp_peq_en = wp.read_peq_enable()
                self._wp_filter = wp.read_dac_filter()
                self._wp_gain_high = wp.read_gain_high()
                self._wp_preamp = wp.read_preamp()
                self._wp_bands = [b for i in range(8) if (b := wp.read_band(i)) is not None]
            if toast:
                self.show_info_toast(f"{dev['name'][:12]} v{self._wp_fw}")
        except Exception:
            self._wp_bands = []
            if toast:
                self.show_info_toast("HID Read Error")

    def _open_walkplay_dsp(self):
        self._refresh_walkplay_state(toast=False)
        if not getattr(self, "_wp_dev", None):
            self.show_info_toast("No Walkplay DAC")
            self._rebuild_menu(preserve_cursor=True)
            return
        self.current_menu = "walkplay_dsp"
        self.cursor_index = 0
        self._rebuild_menu(preserve_cursor=False)
        self.ui.request_render()

    def _open_walkplay_peq(self):
        self.current_menu = "walkplay_peq"
        self.cursor_index = 0
        self._rebuild_menu(preserve_cursor=False)
        self.ui.request_render()

    def _toggle_walkplay_gain(self):
        from src.hardware.walkplay_hid import WalkplayHID
        dev = getattr(self, "_wp_dev", None)
        if not dev:
            self.show_info_toast("No Walkplay DAC")
            return
        nxt = not getattr(self, "_wp_gain_high", True)
        try:
            with WalkplayHID(dev["path"]) as wp:
                if wp.set_gain_high(nxt):
                    self._wp_gain_high = nxt
                    self.show_info_toast("Gain: High" if nxt else "Gain: Low")
        except Exception:
            self.show_info_toast("HID Write Error")
        self._rebuild_menu(preserve_cursor=True)

    def _cycle_walkplay_filter(self, delta: int = 1):
        from src.hardware.walkplay_hid import WalkplayHID, save_walkplay_profile
        dev = getattr(self, "_wp_dev", None)
        if not dev:
            self.show_info_toast("No Walkplay DAC")
            return
        cur = getattr(self, "_wp_filter", 1)
        nxt = (cur + delta) % 5
        lbls = {0: "Fast Linear", 1: "Slow Linear", 2: "Fast MinPh", 3: "Slow MinPh", 4: "NOS"}
        try:
            with WalkplayHID(dev["path"]) as wp:
                if wp.set_dac_filter(nxt):
                    self._wp_filter = nxt
                    save_walkplay_profile(self.db, dev, dac_filter=nxt)
                    self.show_info_toast(f"Filter: {lbls[nxt]}")
        except Exception:
            self.show_info_toast("HID Write Error")
        self._rebuild_menu(preserve_cursor=True)

    def _tune_walkplay_preamp(self, delta: int):
        # Comment (invariant): BGVP MX1 CMD 0x03 quantizes Preamp in integer 1.0 dB steps.
        from src.hardware.walkplay_hid import WalkplayHID, save_walkplay_profile
        dev = getattr(self, "_wp_dev", None)
        if not dev:
            return
        cur = float(getattr(self, "_wp_preamp", 0.0))
        nxt = float(max(-12, min(6, int(round(cur)) + delta)))
        try:
            with WalkplayHID(dev["path"]) as wp:
                if wp.set_preamp(nxt):
                    self._wp_preamp = nxt
                    save_walkplay_profile(self.db, dev, preamp=nxt)
        except Exception:
            self.show_info_toast("HID Write Error")
        self._rebuild_menu(preserve_cursor=True)

    def _toggle_walkplay_peq(self):
        from src.hardware.walkplay_hid import WalkplayHID, save_walkplay_profile
        dev = getattr(self, "_wp_dev", None)
        if not dev:
            self.show_info_toast("No Walkplay DAC")
            return
        nxt = not getattr(self, "_wp_peq_en", True)
        try:
            with WalkplayHID(dev["path"]) as wp:
                if wp.set_peq_enable(nxt):
                    self._wp_peq_en = nxt
                    save_walkplay_profile(self.db, dev, peq_en=nxt)
                    self.show_info_toast("DAC PEQ: ON" if nxt else "DAC PEQ: OFF")
        except Exception:
            self.show_info_toast("HID Write Error")
        self._rebuild_menu(preserve_cursor=True)

    def _cycle_walkplay_preset(self, delta: int = 1):
        from src.hardware.walkplay_hid import WalkplayHID, save_walkplay_profile
        dev = getattr(self, "_wp_dev", None)
        if not dev:
            self.show_info_toast("No Walkplay DAC")
            return
        opts = ["flat", "bass", "vocal", "warm", "air"]
        cur = self.db.get_setting("walkplay_preset", "flat")
        idx = (opts.index(cur) + delta) % len(opts) if cur in opts else 0
        nxt = opts[idx]
        try:
            with WalkplayHID(dev["path"]) as wp:
                if wp.apply_preset(nxt):
                    self.db.set_setting("walkplay_preset", nxt)
                    self._wp_peq_en = wp.read_peq_enable()
                    self._wp_bands = [b for i in range(8) if (b := wp.read_band(i)) is not None]
                    save_walkplay_profile(self.db, dev, peq_en=self._wp_peq_en, bands=self._wp_bands)
                    self.show_info_toast(f"PEQ: {nxt.title()} (RAM)")
        except Exception:
            self.show_info_toast("HID Write Error")
        self._rebuild_menu(preserve_cursor=True)

    def _open_walkplay_band(self, band_idx: int):
        self._wp_band_idx = band_idx
        self.current_menu = "walkplay_band"
        self.cursor_index = 0
        self._rebuild_menu(preserve_cursor=False)
        self.ui.request_render()

    def _write_wp_band_param(self, band_idx: int, freq: int, q: float, gain: float, ftype: int):
        from src.hardware.walkplay_hid import WalkplayHID, save_walkplay_profile
        dev = getattr(self, "_wp_dev", None)
        bands = getattr(self, "_wp_bands", [])
        if not dev or band_idx >= len(bands):
            return
        try:
            with WalkplayHID(dev["path"]) as wp:
                if wp.write_band(band_idx, freq, q, gain, ftype):
                    bands[band_idx].update({"freq": freq, "q": q, "gain": gain, "ftype": ftype})
                    self.db.set_setting("walkplay_preset", "custom")
                    save_walkplay_profile(self.db, dev, bands=bands)
        except Exception:
            self.show_info_toast("HID Write Error")
        self._rebuild_menu(preserve_cursor=True)

    def _tune_walkplay_band(self, band_idx: int, delta: int):
        bands = getattr(self, "_wp_bands", [])
        if band_idx >= len(bands):
            return
        b = bands[band_idx]
        new_gain = max(-12.0, min(12.0, round((b["gain"] + delta * 0.5) * 2.0) / 2.0))
        self._write_wp_band_param(band_idx, b["freq"], b["q"], new_gain, b["ftype"])

    def _tune_walkplay_band_freq(self, band_idx: int, delta: int):
        bands = getattr(self, "_wp_bands", [])
        if band_idx >= len(bands):
            return
        b = bands[band_idx]
        cur_f = int(b["freq"])
        step = 5 if cur_f < 100 else (10 if cur_f < 300 else (25 if cur_f < 1000 else (100 if cur_f < 4000 else 250)))
        new_f = max(20, min(20000, cur_f + delta * step))
        self._write_wp_band_param(band_idx, new_f, b["q"], b["gain"], b["ftype"])

    def _tune_walkplay_band_q(self, band_idx: int, delta: int):
        bands = getattr(self, "_wp_bands", [])
        if band_idx >= len(bands):
            return
        b = bands[band_idx]
        step = 0.05 if b["q"] < 2.0 else 0.1
        new_q = max(0.30, min(10.0, round((b["q"] + delta * step) * 20.0) / 20.0))
        self._write_wp_band_param(band_idx, b["freq"], new_q, b["gain"], b["ftype"])

    def _cycle_walkplay_band_type(self, band_idx: int, delta: int = 1):
        bands = getattr(self, "_wp_bands", [])
        if band_idx >= len(bands):
            return
        b = bands[band_idx]
        opts = [2, 1, 3]
        idx = (opts.index(b["ftype"]) + delta) % len(opts) if b["ftype"] in opts else 0
        self._write_wp_band_param(band_idx, b["freq"], b["q"], b["gain"], opts[idx])

    def _save_walkplay_flash(self):
        from src.hardware.walkplay_hid import WalkplayHID
        dev = getattr(self, "_wp_dev", None)
        if not dev:
            self.show_info_toast("No Walkplay DAC")
            return
        try:
            with WalkplayHID(dev["path"]) as wp:
                if wp.save_to_flash():
                    self.show_info_toast("Saved to DAC Flash ★")
                else:
                    self.show_info_toast("Flash Save Failed")
        except Exception:
            self.show_info_toast("HID Write Error")

    def _toggle_i2s_dac_swap(self):
        """Toggles Left/Right channel swap strictly for the internal I2S DAC (ignores USB DAC)."""
        import asyncio
        current = self.db.get_setting("i2s_dac_swap", "0")
        new_state = "1" if current == "0" else "0"
        self.db.set_setting("i2s_dac_swap", new_state)
        asyncio.create_task(self.ui.sync_i2s_dac_channels())
        self._rebuild_menu()
        self.ui.request_render()

    def _cycle_i2s_drive(self):
        """Cycles Allwinner H618 PI1-PI4 I2S pad drive strength (0: 10mA, 1: 20mA, 2: 30mA, 3: 40mA) live in RAM and DB."""
        current = int(self.db.get_setting("i2s_drive_level", "2"))
        next_lvl = (current + 1) % 4
        kernel = getattr(self.ui, 'os_kernel', None)
        if kernel and hasattr(kernel, 'apply_h618_i2s_drive'):
            ok = kernel.apply_h618_i2s_drive(next_lvl)
            if not ok:
                self.show_info_toast("Drive Reg Error!", duration=1.8)
                return
        self.db.set_setting("i2s_drive_level", str(next_lvl))
        self._rebuild_menu()
        self.ui.request_render()

    def _tune_mic_gain(self, delta: int):
        """Callback attached to 'on_tune' for 1dB step adjustments (0dB to +130dB)."""
        current_db = int(self.db.get_setting("mic_gain_db", "46"))
        new_db = max(0, min(130, current_db + delta))
        self.db.set_setting("mic_gain_db", str(new_db))
        self.ui.trigger_mic_dsp_sync()
        self._rebuild_menu()
        self.ui.request_render()

    def _cycle_mic_range(self):
        """Cycles +0dB compensated Near/Far DSP modes: Near, Far, Far+HPF, Far+AGC (<=48dB safe ceiling)."""
        import asyncio
        options = ["near", "far", "far_hpf", "far_agc"]
        current = self.db.get_setting("mic_range_mode", "near")
        try:
            next_idx = (options.index(current) + 1) % len(options)
        except ValueError:
            next_idx = 0
        self.db.set_setting("mic_range_mode", options[next_idx])
        asyncio.create_task(self.ui.sync_mic_dsp(relink=False))
        self._rebuild_menu()
        self.ui.request_render()

    def _tune_mic_wind_hpf(self, delta: int):
        """
        Callback attached to 'on_tune' for configurable 4th-order (24dB/oct) Butterworth Wind Cut HPF:
        Off (0Hz) <-> 40Hz .. 300Hz in 10Hz steps (160Hz ★ recommended for outdoor wind suppression).
        """
        cur = int(self.db.get_setting("mic_wind_hpf_hz", "0"))
        if cur == 0:
            new_hz = 40 if delta > 0 else 0
        else:
            new_hz = cur + (delta * 10)
            if new_hz < 40:
                new_hz = 0
            elif new_hz > 300:
                new_hz = 300
        self.db.set_setting("mic_wind_hpf_hz", str(new_hz))
        self.ui.trigger_mic_dsp_sync()
        self._rebuild_menu()
        self.ui.request_render()

    def _cycle_mic_channel_mode(self):
        """Cycles Mic capsule mode between Stereo, Mono Left, and Mono Right."""
        import asyncio
        options = ["stereo", "left", "right"]
        current = self.db.get_setting("mic_channel_mode", "stereo")
        try:
            next_idx = (options.index(current) + 1) % len(options)
        except ValueError:
            next_idx = 0
        self.db.set_setting("mic_channel_mode", options[next_idx])
        asyncio.create_task(self.ui.sync_mic_dsp())
        self._rebuild_menu()
        self.ui.request_render()

    def _toggle_mic_swap(self):
        """Toggles Left/Right physical capsule assignment for the dual ICS-43434 I2S microphone array."""
        import asyncio
        current = self.db.get_setting("mic_swap_lr", "0")
        new_state = "1" if current == "0" else "0"
        self.db.set_setting("mic_swap_lr", new_state)
        asyncio.create_task(self.ui.sync_mic_dsp())
        self._rebuild_menu()
        self.ui.request_render()

    def _cycle_mic_i2s_framing(self):
        """
        Cycles Allwinner H618 AHUB I2S0 hardware RX sample resolution inside the 32-bit slot (I2S0_FMT0 +0x204):
        - '24bit': 24-bit SR (0x57) for TDK ICS-43434 (ignores 8 High-Z tri-state LSBs) ★
        - '20bit': 20-bit SR (0x47) for Knowles SPH0645LM4H (Adafruit 3421)
        - '32bit': 32-bit SR (0x77) raw unmasked ALSA slot ⚙
        """
        options = ["24bit", "20bit", "32bit"]
        current = self.db.get_setting("mic_i2s_framing", "24bit")
        try:
            next_mode = options[(options.index(current) + 1) % len(options)]
        except ValueError:
            next_mode = "24bit"
        self.db.set_setting("mic_i2s_framing", next_mode)
        kernel = getattr(self.ui, 'os_kernel', None)
        if kernel and hasattr(kernel, 'enforce_ahub0_24bit_i2s'):
            kernel.enforce_ahub0_24bit_i2s(framing_mode=next_mode)
        self._rebuild_menu()
        self.ui.request_render()

    def _toggle_mic_bclk_edge(self):
        """
        Toggles Allwinner H618 AHUB I2S0 BCLK sampling polarity (I2S0_FMT0 Bit 7):
        - 'normal':   Standard Philips I2S rising-edge latch (0x57) for ICS-43434 ★
        - 'invert':   Inverted BCLK edge (0xD7) for SPH0645LM4H hold-time workaround
        """
        current = self.db.get_setting("mic_bclk_edge", "normal")
        new_edge = "invert" if current == "normal" else "normal"
        self.db.set_setting("mic_bclk_edge", new_edge)
        kernel = getattr(self.ui, 'os_kernel', None)
        if kernel and hasattr(kernel, 'enforce_ahub0_24bit_i2s'):
            kernel.enforce_ahub0_24bit_i2s(bclk_inv=(new_edge == "invert"))
        self._rebuild_menu()
        self.ui.request_render()

    def _tune_mic_balance(self, delta: int):
        """
        Callback attached to 'on_tune' for 0.5dB step L/R microphone sensitivity/noise balance trim
        from L-30.0dB (-60 steps) to R-30.0dB (+60 steps). Default +39 steps (R-19.5dB) balances
        the hotter Slot 1 Left capsule against the quieter Slot 0 Right capsule.
        """
        cur = max(-60, min(60, int(self.db.get_setting("mic_balance_steps", "39"))))
        new_steps = max(-60, min(60, cur + delta))
        self.db.set_setting("mic_balance_steps", str(new_steps))
        self.ui.trigger_mic_dsp_sync()
        self._rebuild_menu()
        self.ui.request_render()

    def _trigger_mic_calibration(self):
        """Launches the non-blocking ambient-noise-guarded dual I2S microphone auto-calibration."""
        import asyncio
        if self._test_running:
            return
        asyncio.create_task(self._run_mic_calibration())

    def _apply_mic_fallback_preset(self):
        """Applies the verified safe fallback preset (Gain +46dB, Balance R-19.5dB) when room noise ruins calibration."""
        import asyncio
        self.db.set_setting("mic_gain_db", "46")
        self.db.set_setting("mic_balance_steps", "39")
        asyncio.create_task(self.ui.sync_mic_dsp(relink=False))
        self._rebuild_menu(preserve_cursor=True)
        self.show_info_toast("Fallback R-19.5dB Set", duration=1.8)
        self.ui.request_render()

    async def _run_mic_calibration(self):
        """
        Records 1.5s from the raw I2S stereo microphone array in 24-bit AHUB mode, checks for
        excessive ambient noise (> -43 dBFS) or transient instability (> 3.5 dB fluctuation),
        and either computes the optimal L/R balance trim or prompts the user via ConfirmModal
        to fall back to the safe preset (L-3.5dB).
        """
        import asyncio, wave, struct, math
        from src.ui.components import ConfirmModal
        self._test_running = True
        wav_path = "/tmp/vip_mic_cal.wav"
        try:
            self.show_info_toast("Calibrating (Quiet)...", duration=2.0)
            kernel = getattr(self.ui, 'os_kernel', None)
            if kernel and hasattr(kernel, 'enforce_ahub0_24bit_i2s'):
                kernel.enforce_ahub0_24bit_i2s()

            rec_cmd = (
                "sudo -u vipxpert env XDG_RUNTIME_DIR=/run/user/1000 "
                "timeout -s INT -k 0.5s 1.5s pw-record "
                "--target=alsa_input.platform-soc_ahub0_mach.stereo-fallback "
                f"--rate=48000 --channels=2 --format=s32 {wav_path}"
            )
            proc = await asyncio.create_subprocess_shell(
                rec_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception:
                try:
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass

            # Re-lock 24-bit AHUB register immediately after pw-record closes
            if kernel and hasattr(kernel, 'enforce_ahub0_24bit_i2s'):
                kernel.enforce_ahub0_24bit_i2s()
            await self.ui.sync_mic_dsp()

            def _analyze_wav():
                with wave.open(wav_path, "rb") as w:
                    raw = w.readframes(w.getnframes())
                s = struct.unpack(f"<{len(raw)//4}i", raw)
                ch0 = s[14400::2]  # Skip first 0.15s transient
                ch1 = s[14401::2]
                if len(ch0) < 9600:
                    return None

                def _ch_stats(c):
                    n = len(c)
                    m = sum(c) / n
                    rms = math.sqrt(sum((x - m) ** 2 for x in c) / n)
                    db_rms = 20 * math.log10(rms / 2147483648.0) if rms > 0 else -120.0
                    hf = math.sqrt(sum((c[i] - c[i - 1]) ** 2 for i in range(1, n)) / (n - 1))
                    db_hf = 20 * math.log10(hf / 2147483648.0) if hf > 0 else -120.0
                    win = 9600
                    w_dbs = []
                    for i in range(0, n - win, win):
                        seg = c[i:i + win]
                        sm = sum(seg) / win
                        sr = math.sqrt(sum((x - sm) ** 2 for x in seg) / win)
                        w_dbs.append(20 * math.log10(sr / 2147483648.0) if sr > 0 else -120.0)
                    var_db = (max(w_dbs) - min(w_dbs)) if w_dbs else 0.0
                    return db_rms, db_hf, var_db

                return _ch_stats(ch0), _ch_stats(ch1)

            res = await asyncio.to_thread(_analyze_wav)
            if not res:
                self.show_info_toast("Calib Read Failed", duration=2.0)
                return

            (rms0, hf0, var0), (rms1, hf1, var1) = res
            swap = self.db.get_setting("mic_swap_lr", "0") == "1"
            # In Normal (swap=False): Slot 0 (ch0) = Physical Right, Slot 1 (ch1) = Physical Left
            r_rms, r_hf, r_var = (rms1, hf1, var1) if swap else (rms0, hf0, var0)
            l_rms, l_hf, l_var = (rms0, hf0, var0) if swap else (rms1, hf1, var1)

            # 1. Detect single-capsule MEMS hardware storm fault (one mic quiet < -50dBFS, the other roaring > -46dBFS)
            if abs(l_rms - r_rms) > 14.0 and min(l_rms, r_rms) < -50.0 and max(l_rms, r_rms) > -46.0:
                faulty_side = "Right" if r_rms > l_rms else "Left"
                clean_mode = "left" if faulty_side == "Right" else "right"

                def _isolate_clean_mic():
                    import asyncio as _aio
                    self.db.set_setting("mic_channel_mode", clean_mode)
                    self.db.set_setting("mic_balance_steps", "0")
                    _aio.create_task(self.ui.sync_mic_dsp())
                    self._rebuild_menu(preserve_cursor=True)
                    self.show_info_toast(f"Isolated Mono {clean_mode[0].upper()}", duration=2.0)
                    self.ui.request_render()

                modal = ConfirmModal(
                    self.ui,
                    title=f"{faulty_side} Mic Storm!",
                    message=f"Isolate Clean {clean_mode.title()} Mic?",
                    on_confirm=_isolate_clean_mic
                )
                self.ui.show_system_overlay(modal)
                return

            # 2. Detect ambient room noise or transient speech/taps on both capsules
            noisy = max(l_rms, r_rms) > -43.0 or max(l_var, r_var) > 3.5
            raw_diff_db = (r_hf - l_hf) * 0.5
            extreme_diff = abs(raw_diff_db) > 9.0

            if noisy or extreme_diff:
                reason = "Room Too Noisy!" if noisy else "Mic Blocked/Extreme!"
                modal = ConfirmModal(
                    self.ui,
                    title=reason,
                    message="Use Safe Fallback (R-19.5dB)?",
                    on_confirm=self._apply_mic_fallback_preset
                )
                self.ui.show_system_overlay(modal)
                return

            new_steps = max(-60, min(60, int(round(raw_diff_db / 0.5))))
            self.db.set_setting("mic_balance_steps", str(new_steps))
            await self.ui.sync_mic_dsp(relink=False)
            self._rebuild_menu(preserve_cursor=True)
            self.show_info_toast(f"Calibrated: {new_steps * 0.5:+.1f}dB", duration=2.0)
            self.ui.request_render()
        except Exception as e:
            print(f"[AudioSettings] Mic calibration error: {e}", flush=True)
            self.show_info_toast("Calibration Error", duration=2.0)
        finally:
            self._test_running = False
            if os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except Exception:
                    pass

    def _toggle_transparency(self):
        """Toggles session-scoped live ambient microphone pass-through into the active headphones DAC."""
        self.ui.transparency_enabled = not getattr(self.ui, 'transparency_enabled', False)
        self.ui.trigger_transparency_sync()
        state_lbl = "ON" if self.ui.transparency_enabled else "OFF"
        self.show_info_toast(f"Transparency: {state_lbl}", duration=1.4)
        self._rebuild_menu()
        self.ui.request_render()

    def _tune_transparency_vol(self, delta: int):
        """Callback attached to 'on_tune' for 10% step adjustments (10% to 300%) on the Transparency stream."""
        current = int(self.db.get_setting("transparency_vol_pct", "100"))
        new_pct = max(10, min(300, current + (delta * 10)))
        self.db.set_setting("transparency_vol_pct", str(new_pct))
        self.ui.trigger_mix_vol_sync()
        self._rebuild_menu()
        self.ui.request_render()

    def _cycle_transparency_duck(self):
        """Cycles background music ducking level applied while Transparency Mode is ON: Off, -6dB, -12dB, Mute."""
        import asyncio
        options = ["off", "6db", "12db", "mute"]
        current = self.db.get_setting("transparency_duck", "off")
        next_mode = options[(options.index(current) + 1) % len(options)] if current in options else "off"
        self.db.set_setting("transparency_duck", next_mode)
        if getattr(self.ui, 'transparency_enabled', False):
            asyncio.create_task(self.ui.sync_exclusive_volumes())
        self._rebuild_menu()
        self.ui.request_render()

    def _cycle_mic_feed_mode(self):
        """Cycles session-scoped Bluetooth/Recorder Mic Input source: Board Mics -> Music Loop -> Mic + Music."""
        options = ["mic", "music", "mix"]
        current = getattr(self.ui, 'mic_feed_mode', 'mic')
        next_mode = options[(options.index(current) + 1) % len(options)] if current in options else "mic"
        self.ui.mic_feed_mode = next_mode
        self.ui.trigger_transparency_sync()
        lbl_map = {"mic": "Board Mics", "music": "Music Loop", "mix": "Mic + Music"}
        self.show_info_toast(f"Feed: {lbl_map[next_mode]}", duration=1.5)
        self._rebuild_menu()
        self.ui.request_render()

    def _tune_mic_feed_vol(self, delta: int):
        """Callback attached to 'on_tune' for 10% step adjustments (10% to 200%) on the Music-to-Mic injection stream."""
        current = int(self.db.get_setting("mic_feed_vol_pct", "100"))
        new_pct = max(10, min(200, current + (delta * 10)))
        self.db.set_setting("mic_feed_vol_pct", str(new_pct))
        self.ui.trigger_mix_vol_sync()
        self._rebuild_menu()
        self.ui.request_render()

    def _trigger_output_test(self):
        """Launches the non-blocking Right -> Left speaker peep test."""
        import asyncio
        if self._test_running:
            return
        asyncio.create_task(self._run_output_test())

    async def _run_output_test(self):
        """Synthesizes and plays a clean Right peep (880Hz) followed by a Left peep (660Hz)."""
        import asyncio, wave, struct, math
        self._test_running = True
        try:
            def _make_wav(path, freq, right_only):
                sr = 48000
                n_frames = int(sr * 0.45)
                with wave.open(path, 'w') as wf:
                    wf.setnchannels(2)
                    wf.setsampwidth(2)
                    wf.setframerate(sr)
                    frames = bytearray()
                    for i in range(n_frames):
                        # Smooth 15ms attack/release envelope to prevent DAC clicks
                        env = min(1.0, i / 720.0, (n_frames - i) / 720.0)
                        sample = int(env * 14000 * math.sin(2.0 * math.pi * freq * (i / sr)))
                        l_val = 0 if right_only else sample
                        r_val = sample if right_only else 0
                        frames.extend(struct.pack('<hh', l_val, r_val))
                    wf.writeframes(bytes(frames))

            await asyncio.to_thread(_make_wav, '/tmp/vip_test_r.wav', 880.0, True)
            await asyncio.to_thread(_make_wav, '/tmp/vip_test_l.wav', 660.0, False)

            self.show_info_toast("Peep: RIGHT...", duration=1.2)
            p_r = await asyncio.create_subprocess_shell(
                "sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 pw-play /tmp/vip_test_r.wav",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await p_r.communicate()
            await asyncio.sleep(0.25)

            self.show_info_toast("Peep: LEFT...", duration=1.2)
            p_l = await asyncio.create_subprocess_shell(
                "sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 pw-play /tmp/vip_test_l.wav",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await p_l.communicate()
            self.show_info_toast("Output Test Done", duration=1.2)
        except Exception as e:
            print(f"[AudioSettings] Output test error: {e}")
        finally:
            self._test_running = False

    async def _open_mic_scope(self):
        """Launches the Dual-Mic FFT & 18-24kHz Ultrasonic Heterodyne Scope directly from Audio Settings."""
        from src.apps.lock_app import LockScreenApp
        viewer = LockScreenApp(self.display, self.ui, standalone_scene="mic_ultrasonic", parent_app_id="audio_settings")
        await self.ui.switch_app("mic_scope_viewer", fallback_app=viewer)

    def _trigger_mic_test(self):
        """Launches the non-blocking Real-Time Right Mic -> Left Mic loopback test."""
        import asyncio
        if self._test_running:
            return
        asyncio.create_task(self._run_mic_test())

    async def _run_mic_test(self):
        """
        Real-time sidetone loopback test:
        1. Isolates the Logical Right I2S Mic capsule -> routes live to both DAC ears for 3s.
        2. Isolates the Logical Left I2S Mic capsule -> routes live to both DAC ears for 3s.
        3. Restores the user's saved Mic DSP configuration.
        """
        import asyncio
        self._test_running = True
        loop_proc = None
        try:
            await self.ui._ensure_stream_ids(force_i2s=True)
            if not self.ui.i2s_source_id:
                self.show_info_toast("No I2S Mic Found", duration=2.0)
                return

            base_db = int(self.db.get_setting("mic_gain_db", "46"))
            mic_range = self.db.get_setting("mic_range_mode", "near")
            total_db = min(48, base_db) if mic_range == "far_agc" else base_db
            linear_gain = round(10.0 ** (min(48, total_db) / 20.0), 4)
            swap = self.db.get_setting("mic_swap_lr", "0") == "1"

            # Helper to isolate a single physical capsule on the I2S source (Normal: Slot 0 = Right, Slot 1 = Left)
            async def _isolate_capsule(logical_side: str):
                if logical_side == "right":
                    phys_idx = 1 if swap else 0
                else:
                    phys_idx = 0 if swap else 1
                vols = [0.0, linear_gain] if phys_idx == 1 else [linear_gain, 0.0]
                ch_map = "[ FL, FR ]" if phys_idx == 0 else "[ FR, FL ]"
                props = f"{{ channelVolumes: [ {vols[0]}, {vols[1]} ], channelMap: {ch_map}, mute: false }}"
                cmd = f"sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 pw-cli s {self.ui.i2s_source_id} Props '{props}'"
                p = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await p.communicate()

            # Start real-time low-latency PipeWire loopback from I2S Mic (FL after map) to both DAC channels [FL FR]
            loop_cmd = (
                "sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 pw-loopback "
                "--capture-props='media.class=Audio/Source node.target=alsa_input.platform-soc_ahub0_mach.stereo-fallback audio.position=[FL]' "
                "--playback-props='audio.position=[FL FR]'"
            )
            loop_proc = await asyncio.create_subprocess_shell(
                loop_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )

            # Phase 1: Right Mic Live (3.0s)
            await _isolate_capsule("right")
            self.show_info_toast("Live: RIGHT Mic (3s)", duration=3.0)
            await asyncio.sleep(3.0)

            # Phase 2: Left Mic Live (3.0s)
            await _isolate_capsule("left")
            self.show_info_toast("Live: LEFT Mic (3s)", duration=3.0)
            await asyncio.sleep(3.0)

            self.show_info_toast("Mic Test Done", duration=1.5)
        except Exception as e:
            print(f"[AudioSettings] Mic test error: {e}")
        finally:
            if loop_proc and loop_proc.returncode is None:
                try:
                    loop_proc.terminate()
                except Exception:
                    pass
            await asyncio.create_subprocess_shell("pkill -f 'pw-loopback.*ahub0_mach' 2>/dev/null || true")
            # Restore user's normal Mic DSP settings
            await self.ui.sync_mic_dsp()
            self._test_running = False

    def _toggle_usb_phy_boost(self):
        """Cycles Allwinner H618 USB PHY profiles from Stock to more aggressive (0: Stock, 1: Gentle, 2: Low-EMI, 3: Max-Boost)."""
        options = ["0", "1", "2", "3"]
        current = str(self.db.get_setting("usb_phy_boost", "1"))
        next_state = options[(options.index(current) + 1) % len(options)] if current in options else "1"
        kernel = getattr(self.ui, 'os_kernel', None)
        if kernel and hasattr(kernel, 'apply_h618_usb_phy_boost'):
            ok = kernel.apply_h618_usb_phy_boost(next_state)
            if not ok:
                self.show_info_toast("USB PHY Reg Error!", duration=1.8)
                return
        self.db.set_setting("usb_phy_boost", next_state)
        self._rebuild_menu()
        self.ui.request_render()

    def _cycle_dac_timeout(self):
        """Cycles USB DAC power-saving options: 15s, 30s, 2m, 5m, 15m, 30m, 60m, 0 (Never)."""
        options = [15, 30, 120, 300, 900, 1800, 3600, 0]
        current = int(self.db.get_setting("timeout_dac", "900"))
        
        try:
            next_idx = (options.index(current) + 1) % len(options)
        except ValueError:
            next_idx = 0
            
        self.db.set_setting("timeout_dac", str(options[next_idx]))
        self._rebuild_menu()
        self.ui.request_render()

    def _cycle_usb_audio_mode(self):
        """Cycles USB-C Hub Audio Input mode across Auto (Takeover) ★, Always On, and Off."""
        import asyncio
        options = ["auto", "on", "off"]
        cur = self.db.get_setting("usb_audio_mode", "auto")
        nxt = options[(options.index(cur) + 1) % len(options)] if cur in options else "auto"
        self.db.set_setting("usb_audio_mode", nxt)
        arb = getattr(self.ui, "media_arbiter", None)
        if arb and hasattr(arb, "_sync_usb_audio_loopback"):
            asyncio.create_task(arb._sync_usb_audio_loopback())
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _trigger_usb_audio_takeover(self):
        """Triggers immediate USB Audio Mode takeover (last-active source wins) from the USB DAC menu."""
        import asyncio
        arb = getattr(self.ui, "media_arbiter", None)
        if not arb:
            return
        async def _run():
            ok = await arb.trigger_usb_takeover()
            if ok:
                self.show_info_toast("USB Audio Active ★", duration=1.5)
            else:
                self.show_info_toast("No USB Audio In", duration=1.6)
            self._rebuild_menu(preserve_cursor=True)
            self.ui.request_render()
        asyncio.create_task(_run())

    def _wake_usb_dac(self):
        import asyncio
        asyncio.create_task(self._exec_usb_dac_power(True))

    def _kill_usb_dac(self):
        import asyncio
        asyncio.create_task(self._exec_usb_dac_power(False))

    async def _exec_usb_dac_power(self, wake: bool):
        arbiter = getattr(self.ui, 'media_arbiter', None)
        if not arbiter or not hasattr(arbiter, 'set_usb_dac_power'):
            self.show_info_toast("Arbiter Unavailable", duration=1.5)
            return
        ok = await arbiter.set_usb_dac_power(wake=wake, manual=True)
        if ok:
            self.show_info_toast("USB DAC Woke" if wake else "USB DAC Killed", duration=1.5)
        else:
            self.show_info_toast("No USB DAC Found", duration=1.8)
        self._rebuild_menu()
        self.ui.request_render()

    def _wake_i2s_dac(self):
        import asyncio
        asyncio.create_task(self._exec_i2s_dac_power(False))

    def _kill_i2s_dac(self):
        import asyncio
        asyncio.create_task(self._exec_i2s_dac_power(True))

    async def _exec_i2s_dac_power(self, standby: bool):
        ok = await self.ui.set_pcm5122_power(standby=standby)
        if ok:
            self.show_info_toast("I2S DAC Killed" if standby else "I2S DAC Woke", duration=1.5)
        else:
            self.show_info_toast("I2S I2C Error!", duration=1.8)
        self._rebuild_menu()
        self.ui.request_render()

    def _toggle_audio_output(self):
        """Toggles the active DAC between USB and internal I2S, calling the centralized kernel router."""
        import asyncio
        
        current = self.db.get_setting("active_dac", "usb")
        new_state = "i2s" if current == "usb" else "usb"
        self.db.set_setting("active_dac", new_state)
        
        # Trigger the centralized preference-aware router
        if hasattr(self.ui, 'os_kernel'):
            asyncio.create_task(self.ui.os_kernel._enforce_audio_routing())
        
        self._rebuild_menu()
        self.ui.request_render()

    def _toggle_i2s_gain(self):
        """Toggles I2S DAC gain strictly via hardware I2C (0x4c/0x4d), showing a pop-up warning if I2C fails."""
        import asyncio
        asyncio.create_task(self._apply_hw_i2s_gain_toggle())

    async def _apply_hw_i2s_gain_toggle(self):
        current = self.db.get_setting("i2s_dac_gain", "high")
        new_state = "low" if current == "high" else "high"

        hw_ok = await self.ui._apply_pcm5122_i2c_gain(gain_mode=new_state)
        if not hw_ok:
            self.show_info_toast("I2C DAC Gain Error!", duration=2.0)
            return

        self.db.set_setting("i2s_dac_gain", new_state)
        self._rebuild_menu()
        self.ui.request_render()

    def _tune_i2s_digital_volume(self, delta: int):
        """Callback attached to 'on_tune' for exact 0.5dB hardware register steps (-60.0dB to +6.0dB)."""
        current_steps = int(self.db.get_setting("i2s_dac_dig_vol_steps", "0"))
        new_steps = max(-120, min(12, current_steps + delta))
        self.db.set_setting("i2s_dac_dig_vol_steps", str(new_steps))
        self.ui.trigger_dac_vol_sync()
        self._rebuild_menu()
        self.ui.request_render()

    def _get_usb_dac_mixer_state(self, force_refresh: bool = False) -> dict:
        """Caches discovered USB DAC ALSA hardware mixer state across rapid < > ticks."""
        import time
        now = time.monotonic()
        cached = getattr(self, "_usb_mixer_cache", None)
        last_ts = getattr(self, "_usb_mixer_cache_ts", 0.0)
        if force_refresh or cached is None or (now - last_ts) > 2.5:
            from src.hardware.usb_dac_mixer import discover_usb_dac_mixer
            cached = discover_usb_dac_mixer()
            self._usb_mixer_cache = cached
            self._usb_mixer_cache_ts = now
        return cached

    def _format_usb_hw_vol(self) -> str:
        usb_mix = self._get_usb_dac_mixer_state(force_refresh=False)
        if usb_mix.get("status") == "ok":
            db_s = usb_mix.get("db_str", "")
            return f"{usb_mix['pct']}% ({db_s})" if db_s else f"{usb_mix['cur_val']}/{usb_mix['max_val']}"
        if usb_mix.get("status") == "unsupported":
            return "Unsupported"
        return "No USB DAC"

    def _refresh_usb_dac_hw_volume(self):
        """Re-probes any connected USB DAC's ALSA hardware mixer on C click."""
        st = self._get_usb_dac_mixer_state(force_refresh=True)
        if st.get("status") == "ok":
            self.show_info_toast(f"{st['card_name']}: {st['control']} ({st['min_val']}-{st['max_val']})", duration=1.8)
        elif st.get("status") == "unsupported":
            self.show_info_toast(f"{st['card_name']}: No HW Vol", duration=1.8)
        else:
            self.show_info_toast("No USB DAC", duration=1.5)
        self._rebuild_menu()
        self.ui.request_render()

    def _tune_usb_dac_hw_volume(self, delta: int):
        """Steps any connected USB DAC's ALSA hardware pvolume control independently of PipeWire software volume."""
        import asyncio
        st = self._get_usb_dac_mixer_state(force_refresh=False)
        if st.get("status") != "ok":
            self._refresh_usb_dac_hw_volume()
            return
        lo = int(st["min_val"])
        hi = int(st["max_val"])
        new_val = max(lo, min(hi, int(st["cur_val"]) + int(delta)))
        if new_val == int(st["cur_val"]):
            return
        st["cur_val"] = new_val
        st["pct"] = round(((new_val - lo) / max(1, hi - lo)) * 100)
        self._usb_mixer_pending = (int(st["card"]), str(st["control"]), new_val)
        task = getattr(self, "_usb_mixer_sync_task", None)
        if task and not task.done():
            task.cancel()
        self._usb_mixer_sync_task = asyncio.create_task(self._debounced_usb_dac_vol_sync())
        self._rebuild_menu()
        self.ui.request_render()

    def _tune_software_volume(self, delta: int):
        """Steps OS PipeWire software volume (Standard or Exclusive) via UIManager.adjust_pi_volume."""
        import asyncio
        async def _run():
            await self.ui.adjust_pi_volume(delta)
            self._rebuild_menu()
            self.ui.request_render()
        asyncio.create_task(_run())

    async def _debounced_usb_dac_vol_sync(self):
        import asyncio
        from src.hardware.usb_dac_mixer import set_usb_dac_volume
        try:
            await asyncio.sleep(0.06)
            pending = getattr(self, "_usb_mixer_pending", None)
            if not pending:
                return
            card, control, raw_val = pending
            res = await asyncio.to_thread(set_usb_dac_volume, card, control, raw_val)
            if res and getattr(self, "_usb_mixer_cache", None):
                self._usb_mixer_cache.update(res)
                self._rebuild_menu()
                self.ui.request_render()
        except asyncio.CancelledError:
            pass

    def _tune_i2s_balance(self, delta: int):
        """Callback attached to 'on_tune' for 0.5dB hardware L/R balance trim (-20 = L+10.0dB to +20 = R+10.0dB)."""
        current_bal = int(self.db.get_setting("i2s_dac_balance_steps", "0"))
        new_bal = max(-20, min(20, current_bal + delta))
        self.db.set_setting("i2s_dac_balance_steps", str(new_bal))
        self.ui.trigger_dac_vol_sync()
        self._rebuild_menu()
        self.ui.request_render()

    def _cycle_i2s_vol_ramp(self):
        """Cycles PCM5122 hardware soft-ramp speed (Reg 0x3C): Fast (0x22) -> Medium (0x11) -> Smooth (0x00)."""
        import asyncio
        asyncio.create_task(self._apply_hw_i2s_ramp_cycle())

    async def _apply_hw_i2s_ramp_cycle(self):
        options = ["fast", "med", "smooth"]
        current = self.db.get_setting("i2s_dac_vol_ramp", "fast")
        try:
            next_mode = options[(options.index(current) + 1) % len(options)]
        except ValueError:
            next_mode = "fast"

        hw_ok = await self.ui._apply_pcm5122_i2c_gain(ramp_mode=next_mode)
        if not hw_ok:
            self.show_info_toast("I2C Ramp Error!", duration=2.0)
            return

        self.db.set_setting("i2s_dac_vol_ramp", next_mode)
        self._rebuild_menu()
        self.ui.request_render()

    def _is_4x_clock_incompatible(self, os_prog=None) -> bool:
        """Returns True if ROM Program 3 (4x Light) is active while any manual clock divider is enabled (d_div >= 0 or n_div > 0 or o_div > 0)."""
        if os_prog is None:
            os_prog = int(self.db.get_setting("i2s_dac_os_prog", "1"))
        if int(os_prog) != 3:
            return False
        d_div = int(self.db.get_setting("i2s_dac_clk_div", "0"))
        n_div = int(self.db.get_setting("i2s_dac_ncp_div", "127"))
        o_div = int(self.db.get_setting("i2s_dac_osr_div", "127"))
        return (d_div >= 0) or (n_div > 0) or (o_div > 0)

    def _maybe_warn_4x_incompatibility(self):
        """Spawns a warning-only ConfirmModal if 4x Light is paired with manual dividers and warning is not muted."""
        if not self._is_4x_clock_incompatible():
            return
        if self.db.get_setting("i2s_4x_warn_muted", "0") == "1":
            return

        def _on_yes_mute():
            self.db.set_setting("i2s_4x_warn_muted", "1")
            self.show_info_toast("Warning Muted", duration=1.2)
            self.ui.request_render()

        def _on_no_keep():
            self.ui.request_render()

        modal = ConfirmModal(
            display=self.display,
            ui_manager=self.ui,
            title="4x Clock Conflict",
            prompt=["Needs Auto Clocks!", "Mute this warning?"],
            on_yes=_on_yes_mute,
            on_no=_on_no_keep
        )
        self.ui.show_system_overlay(modal)

    # Stage: PCM5122 PLL & DSP 1:1 Synchronous Mode Toggle (Switches between 49.152MHz R=1 DSP=/1 1:1 sync and 98.304MHz R=2 DSP=/2)
    def _toggle_i2s_pll_mode(self):
        import asyncio
        asyncio.create_task(self._apply_hw_i2s_pll_toggle())

    async def _apply_hw_i2s_pll_toggle(self):
        cur = self.db.get_setting("i2s_dac_pll_mode", "sync_49m")
        nxt = "over_98m" if cur == "sync_49m" else "sync_49m"
        hw_ok = await self.ui._apply_pcm5122_i2c_gain(pll_mode=nxt, pulse_standby=True)
        if not hw_ok:
            self.show_info_toast("I2C PLL Mode Err!", duration=2.0)
            return
        self.db.set_setting("i2s_dac_pll_mode", nxt)
        self._rebuild_menu()
        self.ui.request_render()

    def _tune_i2s_dac_clk(self, delta: int):
        """< > slider for PCM5122 DAC Clock Divider (Reg 0x1C): Auto(/16) ⚙ <-> /1 ★ <-> /2 <-> /4 <-> /8 <-> /16 <-> /32."""
        options = [-1, 0, 1, 3, 7, 15, 31]
        current = int(self.db.get_setting("i2s_dac_clk_div", "0"))
        idx = options.index(current) if current in options else 1
        new_idx = max(0, min(len(options) - 1, idx + (1 if delta > 0 else -1)))
        new_val = options[new_idx]
        self.db.set_setting("i2s_dac_clk_div", str(new_val))
        self.ui.trigger_dac_vol_sync()
        self._rebuild_menu()
        self.ui.request_render()
        if current == -1 and new_val >= 0:
            self._maybe_warn_4x_incompatibility()

    def _tune_i2s_charge_pump(self, delta: int):
        """< > slider for PCM5122 Negative Charge Pump Divider (Reg 0x1D, safe range /20 .. /128): Auto(/4) ⚙ <-> /20 .. /128 ★."""
        options = [-1, 19, 23, 27, 31, 47, 63, 95, 127]
        current = int(self.db.get_setting("i2s_dac_ncp_div", "127"))
        if current == 0:
            current = -1
        idx = options.index(current) if current in options else 8
        new_idx = max(0, min(len(options) - 1, idx + (1 if delta > 0 else -1)))
        new_val = options[new_idx]
        self.db.set_setting("i2s_dac_ncp_div", str(new_val))
        self.ui.trigger_dac_vol_sync()
        self._rebuild_menu()
        self.ui.request_render()
        if current == -1 and new_val > 0:
            self._maybe_warn_4x_incompatibility()

    def _tune_i2s_mod_clk(self, delta: int):
        """< > slider for PCM5122 Modulator / OSR Clock Divider (Reg 0x1E, full 7-bit /2 .. /128): Auto(/8) ⚙ <-> /2 .. /128 ★."""
        options = [-1, 1, 2, 3, 5, 7, 11, 15, 23, 31, 47, 63, 79, 95, 111, 127]
        current = int(self.db.get_setting("i2s_dac_osr_div", "127"))
        if current == 0:
            current = -1
        idx = options.index(current) if current in options else 15
        new_idx = max(0, min(len(options) - 1, idx + (1 if delta > 0 else -1)))
        new_val = options[new_idx]
        self.db.set_setting("i2s_dac_osr_div", str(new_val))
        self.ui.trigger_dac_vol_sync()
        self._rebuild_menu()
        self.ui.request_render()
        if current == -1 and new_val > 0:
            self._maybe_warn_4x_incompatibility()

    def _cycle_i2s_automute(self):
        """Cycles PCM5122 Hardware Zero-Data Auto-Mute (Reg 0x40/0x41): Off -> 105ms (Safe) -> 21ms (Fast)."""
        import asyncio
        asyncio.create_task(self._apply_hw_i2s_automute_cycle())

    async def _apply_hw_i2s_automute_cycle(self):
        options = ["off", "105ms", "21ms"]
        current = self.db.get_setting("i2s_dac_automute", "off")
        try:
            next_mode = options[(options.index(current) + 1) % len(options)]
        except ValueError:
            next_mode = "off"

        hw_ok = await self.ui._apply_pcm5122_i2c_gain(automute_mode=next_mode)
        if not hw_ok:
            self.show_info_toast("I2C AutoMute Err!", duration=2.0)
            return

        self.db.set_setting("i2s_dac_automute", next_mode)
        self._rebuild_menu()
        self.ui.request_render()

    def _check_i2s_dac_status(self):
        """Queries live PCM5122 silicon status registers (0x5A, 0x66-0x68, 0x78) and displays an OLED toast."""
        import asyncio
        asyncio.create_task(self._run_i2s_dac_status_check())

    async def _run_i2s_dac_status_check(self):
        status_str = await self.ui.read_pcm5122_status()
        self.show_info_toast(status_str, duration=3.0)

    def _cycle_i2s_oversampling(self):
        """Cycles PCM5122 ROM Oversampling Program (Reg 0x2B) from Fast to Slower, with NOS last: 1 -> 2 -> 3 -> 7."""
        import asyncio
        asyncio.create_task(self._apply_hw_i2s_os_cycle())

    async def _apply_hw_i2s_os_cycle(self):
        options = [1, 2, 3, 7]
        current = int(self.db.get_setting("i2s_dac_os_prog", "1"))
        try:
            next_prog = options[(options.index(current) + 1) % len(options)]
        except ValueError:
            next_prog = 1

        hw_ok = await self.ui._apply_pcm5122_i2c_gain(os_prog=next_prog, pulse_standby=True)
        if not hw_ok:
            self.show_info_toast("I2C OS Prog Error!", duration=2.0)
            return

        self.db.set_setting("i2s_dac_os_prog", str(next_prog))
        self._rebuild_menu()
        self.ui.request_render()
        if next_prog == 3:
            self._maybe_warn_4x_incompatibility()

    def _toggle_i2s_filter(self):
        """Toggles PCM5122 Phase Filter (Reg 0x26) between Linear (0x00) and Min-Phase (0x01)."""
        import asyncio
        asyncio.create_task(self._apply_hw_i2s_filter_toggle())

    async def _apply_hw_i2s_filter_toggle(self):
        current = self.db.get_setting("i2s_dac_filter", "linear")
        new_state = "low_lat" if current == "linear" else "linear"

        hw_ok = await self.ui._apply_pcm5122_i2c_gain(filter_mode=new_state)
        if not hw_ok:
            self.show_info_toast("I2C Filter Error!", duration=2.0)
            return

        self.db.set_setting("i2s_dac_filter", new_state)
        self._rebuild_menu()
        self.ui.request_render()

    def _toggle_i2s_deemphasis(self):
        """Toggles PCM5122 Hardware De-Emphasis (Reg 0x07 Bit 4) between Off (Flat) and On (Warm)."""
        import asyncio
        asyncio.create_task(self._apply_hw_i2s_deemph_toggle())

    async def _apply_hw_i2s_deemph_toggle(self):
        current = self.db.get_setting("i2s_dac_deemph", "0")
        new_state = "1" if current == "0" else "0"

        hw_ok = await self.ui._apply_pcm5122_i2c_gain(deemph_mode=new_state)
        if not hw_ok:
            self.show_info_toast("I2C De-Emph Error!", duration=2.0)
            return

        self.db.set_setting("i2s_dac_deemph", new_state)
        self._rebuild_menu()
        self.ui.request_render()

    def _toggle_exclusive_mode(self):
        """Cycles 3-way Audio Mode (0: Standard, 1: Stream Scale, 2: Sink Scale), updates DB, and syncs."""
        import asyncio
        
        # Gracefully migrate legacy True/False strings to 0/1/2 integers
        raw_current = self.db.get_setting("exclusive_audio", "0")
        if raw_current == "False": raw_current = "0"
        if raw_current == "True": raw_current = "1"
        
        # Cycle 0 -> 1 -> 2 -> 0
        new_state = str((int(raw_current) + 1) % 3)
        self.db.set_setting("exclusive_audio", new_state)
        
        # Immediately update UIManager state and trigger the math synchronizer
        self.ui.exclusive_mode = int(new_state)
        asyncio.create_task(self.ui.sync_exclusive_volumes())
        
        self._rebuild_menu()
        self.ui.request_render()

    def _toggle_rt_cpu(self):
        """Toggles split-core Real-Time CPU scheduling (Core 3: data-loop + I2S DMA, Core 2: USB IRQs)."""
        current = self.db.get_setting("rt_cpu_audio", "0")
        new_state = "1" if current == "0" else "0"
        self.db.set_setting("rt_cpu_audio", new_state)

        kernel = getattr(self.ui, 'os_kernel', None)
        if kernel and hasattr(kernel, 'apply_rt_cpu_state'):
            kernel.apply_rt_cpu_state(new_state == "1")

        self._rebuild_menu()
        self.ui.request_render()

    def _cycle_pmic_mode(self):
        """Cycles AXP313A PMIC rail conditioning from Stock to more aggressive (0: Stock PFM, 1: DVM Smooth, 2: Fixed PWM, 3: Spread PWM)."""
        options = ["0", "1", "2", "3"]
        current = str(self.db.get_setting("pmic_power_mode", "2"))
        next_state = options[(options.index(current) + 1) % len(options)] if current in options else "2"
        kernel = getattr(self.ui, 'os_kernel', None)
        if kernel and hasattr(kernel, 'apply_axp313a_pmic_mode'):
            ok = kernel.apply_axp313a_pmic_mode(next_state)
            if not ok:
                self.show_info_toast("PMIC I2C Error!", duration=1.8)
                return
        self.db.set_setting("pmic_power_mode", next_state)
        self._rebuild_menu()
        self.ui.request_render()

    def _prompt_auto_best(self):
        """Mounts a ConfirmModal to verify before probing hardware and applying optimal ★ settings."""
        scope_names = {
            "root": "All Audio?",
            "i2s_dac": "I2S DAC?",
            "usb_dac": "USB DAC?",
            "mic": "Microphone?",
            "mix": "Transp & Mix?",
            "dsp": "DSP & Engine?"
        }
        prompt_lbl = scope_names.get(self.current_menu, "All Audio?")

        async def _on_yes():
            await self._apply_auto_best(self.current_menu)

        async def _on_no():
            self.ui.request_render()

        modal = ConfirmModal(
            display=self.display,
            ui_manager=self.ui,
            title="Auto-Tune ★",
            prompt=f"Optimize {prompt_lbl}",
            on_yes=_on_yes,
            on_no=_on_no
        )
        self.ui.show_system_overlay(modal)
        self.ui.request_render()

    def _prompt_restore_defaults(self):
        """Mounts a ConfirmModal to verify before resetting the current scope to factory stock ⚙ settings."""
        scope_names = {
            "root": "All Audio?",
            "i2s_dac": "I2S DAC?",
            "usb_dac": "USB DAC?",
            "mic": "Microphone?",
            "mix": "Transp & Mix?",
            "dsp": "DSP & Engine?"
        }
        prompt_lbl = scope_names.get(self.current_menu, "All Audio?")

        async def _on_yes():
            await self._apply_restore_defaults(self.current_menu)

        async def _on_no():
            self.ui.request_render()

        modal = ConfirmModal(
            display=self.display,
            ui_manager=self.ui,
            title="Restore Def ⚙",
            prompt=f"Reset {prompt_lbl}",
            on_yes=_on_yes,
            on_no=_on_no
        )
        self.ui.show_system_overlay(modal)
        self.ui.request_render()

    async def _apply_auto_best(self, scope: str):
        """
        Probes live hardware (USB DAC presence, PCM5122 I2C, AXP313A PMIC, H618 PIO/PHY)
        and applies verified optimal ★ settings while preserving physical L/R wiring swaps.
        """
        kernel = getattr(self.ui, 'os_kernel', None)
        arbiter = getattr(self.ui, 'media_arbiter', None)

        if scope in ("root", "usb_dac"):
            self.db.set_setting("timeout_dac", "900")
            self.db.set_setting("usb_phy_boost", "1")
            if kernel and hasattr(kernel, 'apply_h618_usb_phy_boost'):
                kernel.apply_h618_usb_phy_boost("1")

        if scope in ("root", "i2s_dac"):
            self.db.set_setting("i2s_dac_gain", "high")
            self.db.set_setting("i2s_dac_dig_vol_steps", "0")
            self.db.set_setting("i2s_dac_vol_ramp", "fast")
            self.db.set_setting("i2s_dac_os_prog", "1")
            self.db.set_setting("i2s_dac_filter", "linear")
            self.db.set_setting("i2s_dac_deemph", "0")
            self.db.set_setting("i2s_dac_pll_mode", "sync_49m")
            self.db.set_setting("i2s_dac_clk_div", "0")
            self.db.set_setting("i2s_dac_ncp_div", "127")
            self.db.set_setting("i2s_dac_osr_div", "127")
            self.db.set_setting("i2s_dac_automute", "off")
            self.db.set_setting("i2s_drive_level", "2")
            if kernel and hasattr(kernel, 'apply_h618_i2s_drive'):
                kernel.apply_h618_i2s_drive(2)
            await self.ui._apply_pcm5122_i2c_gain(pulse_standby=True)
            await self.ui.sync_i2s_dac_channels()

        if scope in ("root", "mic"):
            self.db.set_setting("mic_gain_db", "46")
            self.db.set_setting("mic_balance_steps", "39")
            self.db.set_setting("mic_range_mode", "near")
            self.db.set_setting("mic_wind_hpf_hz", "0")
            self.db.set_setting("mic_channel_mode", "stereo")
            self.db.set_setting("mic_swap_lr", "0")
            self.db.set_setting("mic_i2s_framing", "24bit")
            self.db.set_setting("mic_bclk_edge", "normal")
            await self.ui.sync_mic_dsp()

        if scope in ("root", "mix"):
            self.db.set_setting("transparency_vol_pct", "100")
            self.db.set_setting("transparency_duck", "off")
            self.db.set_setting("mic_feed_vol_pct", "100")
            self.ui.mic_feed_mode = "mic"
            await self.ui.sync_transparency_and_mic_feed()

        if scope in ("root", "dsp"):
            self.db.set_setting("exclusive_audio", "0")
            self.ui.exclusive_mode = 0
            self.db.set_setting("rt_cpu_audio", "1")
            self.db.set_setting("pmic_power_mode", "2")
            if kernel:
                if hasattr(kernel, 'apply_rt_cpu_state'):
                    kernel.apply_rt_cpu_state(True)
                if hasattr(kernel, 'apply_axp313a_pmic_mode'):
                    kernel.apply_axp313a_pmic_mode("2")
            await self.ui.sync_exclusive_volumes()

        if scope == "root":
            # Dynamically probe physical USB DAC attachment to select optimal Output path
            usb_present = bool(getattr(self.ui, 'active_hardware_dac', None))
            if not usb_present and arbiter and hasattr(arbiter, '_get_usb_audio_path'):
                usb_present = bool(await arbiter._get_usb_audio_path())
            self.db.set_setting("active_dac", "usb" if usb_present else "i2s")
            if kernel and hasattr(kernel, '_enforce_audio_routing'):
                await kernel._enforce_audio_routing()

        self._rebuild_menu(preserve_cursor=True)
        self.show_info_toast("Optimal ★ Applied", duration=1.8)
        self.ui.request_render()

    async def _apply_restore_defaults(self, scope: str):
        """
        Resets the target scope to un-tuned hardware factory stock ⚙ baselines
        (Auto clock dividers, Stock PHY/PMIC, 20mA L1 drive, RT CPU Off, Normal L/R).
        """
        kernel = getattr(self.ui, 'os_kernel', None)

        if scope in ("root", "usb_dac"):
            self.db.set_setting("timeout_dac", "900")
            self.db.set_setting("usb_phy_boost", "0")
            if kernel and hasattr(kernel, 'apply_h618_usb_phy_boost'):
                kernel.apply_h618_usb_phy_boost("0")

        if scope in ("root", "i2s_dac"):
            self.db.set_setting("i2s_dac_gain", "high")
            self.db.set_setting("i2s_dac_dig_vol_steps", "0")
            self.db.set_setting("i2s_dac_balance_steps", "0")
            self.db.set_setting("i2s_dac_vol_ramp", "fast")
            self.db.set_setting("i2s_dac_os_prog", "1")
            self.db.set_setting("i2s_dac_filter", "linear")
            self.db.set_setting("i2s_dac_deemph", "0")
            self.db.set_setting("i2s_dac_pll_mode", "sync_49m")
            self.db.set_setting("i2s_dac_clk_div", "-1")
            self.db.set_setting("i2s_dac_ncp_div", "-1")
            self.db.set_setting("i2s_dac_osr_div", "-1")
            self.db.set_setting("i2s_4x_warn_muted", "0")
            self.db.set_setting("i2s_dac_automute", "off")
            self.db.set_setting("i2s_drive_level", "1")
            self.db.set_setting("i2s_dac_swap", "0")
            if kernel and hasattr(kernel, 'apply_h618_i2s_drive'):
                kernel.apply_h618_i2s_drive(1)
            await self.ui._apply_pcm5122_i2c_gain(pulse_standby=True)
            await self.ui.sync_i2s_dac_channels()

        if scope in ("root", "mic"):
            self.db.set_setting("mic_gain_db", "20")
            self.db.set_setting("mic_balance_steps", "0")
            self.db.set_setting("mic_range_mode", "near")
            self.db.set_setting("mic_wind_hpf_hz", "0")
            self.db.set_setting("mic_channel_mode", "stereo")
            self.db.set_setting("mic_swap_lr", "0")
            self.db.set_setting("mic_i2s_framing", "32bit")
            self.db.set_setting("mic_bclk_edge", "normal")
            await self.ui.sync_mic_dsp()

        if scope in ("root", "mix"):
            self.ui.transparency_enabled = False
            self.ui.mic_feed_mode = "mic"
            self.db.set_setting("transparency_vol_pct", "100")
            self.db.set_setting("transparency_duck", "off")
            self.db.set_setting("mic_feed_vol_pct", "100")
            await self.ui.sync_transparency_and_mic_feed()

        if scope in ("root", "dsp"):
            self.db.set_setting("exclusive_audio", "0")
            self.ui.exclusive_mode = 0
            self.db.set_setting("rt_cpu_audio", "0")
            self.db.set_setting("pmic_power_mode", "0")
            if kernel:
                if hasattr(kernel, 'apply_rt_cpu_state'):
                    kernel.apply_rt_cpu_state(False)
                if hasattr(kernel, 'apply_axp313a_pmic_mode'):
                    kernel.apply_axp313a_pmic_mode("0")
            await self.ui.sync_exclusive_volumes()

        if scope == "root":
            self.db.set_setting("active_dac", "usb")
            if kernel and hasattr(kernel, '_enforce_audio_routing'):
                await kernel._enforce_audio_routing()

        self._rebuild_menu(preserve_cursor=True)
        self.show_info_toast("Defaults ⚙ Restored", duration=1.8)
        self.ui.request_render()

    def _rebuild_menu(self, preserve_cursor: bool = True):
        t_dac = int(self.db.get_setting("timeout_dac", "900"))
        if t_dac == 0:
            dac_str = "Never"
        elif t_dac < 60:
            dac_str = f"{t_dac}s"
        else:
            dac_str = f"{t_dac // 60}m ★" if t_dac == 900 else f"{t_dac // 60}m"
            
        # Map Active Audio Output (Preference-Aware)
        active_dac = self.db.get_setting("active_dac", "usb")
        if active_dac == "usb":
            if not getattr(self.ui, 'active_hardware_dac', None):
                dac_out_str = "USB (I2S Fallback)"
            else:
                dac_out_str = "USB ★"
        else:
            dac_out_str = "I2S ★" if not getattr(self.ui, 'active_hardware_dac', None) else "I2S"
            
        # Gracefully handle legacy booleans and map the 3 states
        raw_excl = self.db.get_setting("exclusive_audio", "0")
        if raw_excl == "False": raw_excl = "0"
        if raw_excl == "True": raw_excl = "1"
        mode_map = {"0": "Standard ★", "1": "Stream Scale", "2": "Sink Scale"}
        excl_mode = mode_map.get(raw_excl, "Standard ★")

        # Map the new Real-Time DB state (OFF = Stock ⚙, ON = Recommended ★)
        rt_state = self.db.get_setting("rt_cpu_audio", "0")
        rt_str = "ON ★" if rt_state == "1" else "OFF ⚙"

        # Map PCM5122 Analog Gain, Digital Volume, L/R Balance, Volume Ramp, Oversampling ROM, Phase Filter, De-Emphasis, Charge Pump, & Modulator Clock
        gain_state = self.db.get_setting("i2s_dac_gain", "high")
        gain_str = "1.05V (-6dB)" if gain_state == "low" else "2.1V (0dB) ★"

        dig_steps = max(-120, min(12, int(self.db.get_setting("i2s_dac_dig_vol_steps", "0"))))
        dig_db = dig_steps * 0.5
        dig_vol_str = f"{dig_db:+.1f}dB ★" if dig_steps == 0 else f"{dig_db:+.1f}dB"

        bal_steps = max(-20, min(20, int(self.db.get_setting("i2s_dac_balance_steps", "0"))))
        if bal_steps == 0:
            bal_str = "Center ★"
        elif bal_steps < 0:
            bal_str = f"L+{abs(bal_steps) * 0.5:.1f}dB"
        else:
            bal_str = f"R+{bal_steps * 0.5:.1f}dB"

        ramp_state = self.db.get_setting("i2s_dac_vol_ramp", "fast")
        ramp_map = {"fast": "Fast ★", "med": "Medium", "smooth": "Smooth"}
        ramp_str = ramp_map.get(ramp_state, "Fast ★")

        os_prog = int(self.db.get_setting("i2s_dac_os_prog", "1"))
        os_3_lbl = "4x Light !" if self._is_4x_clock_incompatible(os_prog) else "4x Light"
        os_map = {1: "8x Fast ★", 2: "8x Slow", 3: os_3_lbl, 7: "2x NOS"}
        os_str = os_map.get(os_prog, "8x Fast ★")

        flt_state = self.db.get_setting("i2s_dac_filter", "linear")
        flt_str = "Min-Phase" if flt_state == "low_lat" else "Linear ★"

        deemph_state = self.db.get_setting("i2s_dac_deemph", "0")
        deemph_str = "On (Warm)" if deemph_state == "1" else "Off (Flat) ★"
        pll_mode = self.db.get_setting("i2s_dac_pll_mode", "sync_49m")
        pll_str = "98MHz (2x)" if pll_mode == "over_98m" else "49MHz 1:1 ★"

        dac_div = int(self.db.get_setting("i2s_dac_clk_div", "0"))
        dac_div_map = {
            -1: "Auto (/16) ⚙", 0: "Div /1 ★", 1: "Div /2", 3: "Div /4",
            7: "Div /8", 15: "Div /16", 31: "Div /32"
        }
        dac_clk_str = dac_div_map.get(dac_div, "Div /1 ★")

        ncp_div = int(self.db.get_setting("i2s_dac_ncp_div", "127"))
        if ncp_div == 0:
            ncp_div = -1
        ncp_map = {
            -1: "Auto (/4) ⚙", 19: "Div /20", 23: "Div /24", 27: "Div /28",
            31: "Div /32", 47: "Div /48", 63: "Div /64", 95: "Div /96", 127: "Div /128 ★"
        }
        ncp_str = ncp_map.get(ncp_div, "Div /128 ★")

        osr_div = int(self.db.get_setting("i2s_dac_osr_div", "127"))
        if osr_div == 0:
            osr_div = -1
        osr_map = {
            -1: "Auto (/8) ⚙", 1: "Div /2", 2: "Div /3", 3: "Div /4", 5: "Div /6",
            7: "Div /8", 11: "Div /12", 15: "Div /16", 23: "Div /24", 31: "Div /32",
            47: "Div /48", 63: "Div /64", 79: "Div /80", 95: "Div /96", 111: "Div /112",
            127: "Div /128 ★"
        }
        osr_str = osr_map.get(osr_div, "Div /128 ★")

        am_state = self.db.get_setting("i2s_dac_automute", "off")
        am_map = {"off": "Off ★", "105ms": "105ms (Safe)", "21ms": "21ms (Fast)"}
        am_str = am_map.get(am_state, "Off ★")

        # Map I2S DAC L/R Swap state and Allwinner H618 PI_DRV0 BCK/I2S Drive Strength
        dac_swap = self.db.get_setting("i2s_dac_swap", "0")
        dac_swap_str = "Swapped (R/L)" if dac_swap == "1" else "Normal (L/R) ⚙"
        drv_lvl = int(self.db.get_setting("i2s_drive_level", "2"))
        drv_map = {0: "10mA (L0)", 1: "20mA (L1) ⚙", 2: "30mA (L2) ★", 3: "40mA (L3)"}
        drv_str = drv_map.get(drv_lvl, "30mA (L2) ★")

        # Map Mic Gain (dB slider), L/R Balance Trim, Range Mode, Channel Mode, and L/R Swap
        mic_db = int(self.db.get_setting("mic_gain_db", "46"))
        mic_gain_str = f"+{mic_db}dB ★" if mic_db == 46 else (f"+{mic_db}dB ⚙" if mic_db == 20 else f"+{mic_db}dB")

        mic_bal_steps = max(-60, min(60, int(self.db.get_setting("mic_balance_steps", "39"))))
        if mic_bal_steps == 0:
            mic_bal_str = "Center ⚙"
        elif mic_bal_steps < 0:
            mic_bal_str = f"L-{abs(mic_bal_steps) * 0.5:.1f}dB"
        else:
            star = " ★" if mic_bal_steps == 39 else ""
            mic_bal_str = f"R-{mic_bal_steps * 0.5:.1f}dB{star}"

        mic_range = self.db.get_setting("mic_range_mode", "near")
        range_map = {
            "near": "Near ★",
            "far": "Far",
            "far_hpf": "Far+HPF",
            "far_agc": "Far+AGC"
        }
        mic_range_str = range_map.get(mic_range, "Near ★")

        wind_hz = max(0, min(300, int(self.db.get_setting("mic_wind_hpf_hz", "0"))))
        if wind_hz < 40:
            if mic_range in ("far_hpf", "far_agc"):
                wind_hpf_str = "Auto (160Hz) ★"
            elif mic_range == "far":
                wind_hpf_str = "Auto (100Hz)"
            else:
                wind_hpf_str = "Off ⚙"
        else:
            star = " ★" if wind_hz == 160 else ""
            wind_hpf_str = f"{wind_hz}Hz{star}"

        mic_ch = self.db.get_setting("mic_channel_mode", "stereo")
        ch_map = {"stereo": "Stereo ★", "left": "Mono L", "right": "Mono R"}
        mic_ch_str = ch_map.get(mic_ch, "Stereo ★")

        mic_swap = self.db.get_setting("mic_swap_lr", "0")
        mic_swap_str = "Swapped (R/L)" if mic_swap == "1" else "Normal (L/R) ★"

        mic_framing = self.db.get_setting("mic_i2s_framing", "24bit")
        framing_map = {
            "24bit": "24-bit (43434) ★",
            "20bit": "20-bit (SPH0645)",
            "32bit": "32-bit (Raw) ⚙"
        }
        mic_framing_str = framing_map.get(mic_framing, "24-bit (43434) ★")

        mic_edge = self.db.get_setting("mic_bclk_edge", "normal")
        mic_edge_str = "Inverted (SPH)" if mic_edge == "invert" else "Normal ★"

        if self.current_menu == "i2s_dac":
            self.title = "I2S DAC"
            items = [
                {
                    "name": "Analog Gain",
                    "suffix": gain_str,
                    "action": self._toggle_i2s_gain
                },
                {
                    "name": "Digital Volume",
                    "suffix": dig_vol_str,
                    "on_tune": self._tune_i2s_digital_volume
                },
                {
                    "name": "L/R Balance",
                    "suffix": bal_str,
                    "on_tune": self._tune_i2s_balance
                },
                {
                    "name": "Volume Ramp",
                    "suffix": ramp_str,
                    "action": self._cycle_i2s_vol_ramp
                },
                {
                    "name": "Oversampling",
                    "suffix": os_str,
                    "action": self._cycle_i2s_oversampling
                },
                {
                    "name": "Phase Filter",
                    "suffix": flt_str,
                    "action": self._toggle_i2s_filter
                },
                {
                    "name": "De-Emphasis",
                    "suffix": deemph_str,
                    "action": self._toggle_i2s_deemphasis
                },
                {
                    "name": "PLL / DSP Sync",
                    "suffix": pll_str,
                    "action": self._toggle_i2s_pll_mode
                },
                {
                    "name": "DAC Clock",
                    "suffix": dac_clk_str,
                    "on_tune": self._tune_i2s_dac_clk
                },
                {
                    "name": "Charge Pump",
                    "suffix": ncp_str,
                    "on_tune": self._tune_i2s_charge_pump
                },
                {
                    "name": "Modulator Clk",
                    "suffix": osr_str,
                    "on_tune": self._tune_i2s_mod_clk
                },
                {
                    "name": "Auto-Mute",
                    "suffix": am_str,
                    "action": self._cycle_i2s_automute
                },
                {
                    "name": "I2S Drive",
                    "suffix": drv_str,
                    "action": self._cycle_i2s_drive
                },
                {
                    "name": "DAC Status",
                    "suffix": "Check >",
                    "action": self._check_i2s_dac_status
                },
                {
                    "name": "I2S Swap",
                    "suffix": dac_swap_str,
                    "action": self._toggle_i2s_dac_swap
                },
                {
                    "name": "Wake I2S DAC",
                    "suffix": "Manual",
                    "action": self._wake_i2s_dac
                },
                {
                    "name": "Kill I2S DAC",
                    "suffix": "Manual",
                    "action": self._kill_i2s_dac
                },
                {
                    "name": "Test Output",
                    "suffix": "R -> L",
                    "action": self._trigger_output_test
                },
                {
                    "name": "Auto-Tune Best",
                    "suffix": "★",
                    "action": self._prompt_auto_best
                },
                {
                    "name": "Restore Default",
                    "suffix": "⚙",
                    "action": self._prompt_restore_defaults
                }
            ]
        elif self.current_menu == "usb_dac":
            self.title = "USB DAC"
            usb_am = self.db.get_setting("usb_audio_mode", "auto")
            usb_am_lbl = {"auto": "Auto (Takeover) ★", "on": "Always On", "off": "Off"}.get(usb_am, "Auto (Takeover) ★")
            arb = getattr(self.ui, "media_arbiter", None)
            src_now = getattr(arb, "active_source", "NONE") if arb else "NONE"
            has_usb_in = bool(getattr(arb, "_usb_source_node", None) or getattr(self.ui, "active_hardware_dac", None))
            usb_st_lbl = "Active ★" if src_now == "USB" else ("Standby (Tap)" if has_usb_in else "No USB In")
            phy_boost = str(self.db.get_setting("usb_phy_boost", "1"))
            phy_map = {
                "0": "Stock (0x343) ⚙",
                "1": "Gentle (0x322) ★",
                "2": "Low-EMI (0x110)",
                "3": "Max-Boost (0x777)"
            }
            phy_str = phy_map.get(phy_boost, "Gentle (0x322) ★")
            usb_hw_vol_str = self._format_usb_hw_vol()
            from src.hardware.walkplay_hid import discover_walkplay_device
            wp_dev = discover_walkplay_device()
            wp_lbl = "Ready ★" if wp_dev else "None"
            items = [
                {
                    "name": "HW Volume",
                    "suffix": usb_hw_vol_str,
                    "on_tune": self._tune_usb_dac_hw_volume,
                    "action": self._refresh_usb_dac_hw_volume
                },
                {
                    "name": "Walkplay DSP",
                    "suffix": wp_lbl,
                    "end_indicator": ">" if wp_dev else "",
                    "action": self._open_walkplay_dsp
                },
                {
                    "name": "USB Audio In",
                    "suffix": usb_am_lbl,
                    "action": self._cycle_usb_audio_mode
                },
                {
                    "name": "USB Takeover",
                    "suffix": usb_st_lbl,
                    "action": self._trigger_usb_audio_takeover
                },
                {
                    "name": "USB DAC Auto-Off",
                    "suffix": dac_str,
                    "action": self._cycle_dac_timeout
                },
                {
                    "name": "USB PHY Boost",
                    "suffix": phy_str,
                    "action": self._toggle_usb_phy_boost
                },
                {
                    "name": "Wake USB DAC",
                    "suffix": "Manual",
                    "action": self._wake_usb_dac
                },
                {
                    "name": "Kill USB DAC",
                    "suffix": "Manual",
                    "action": self._kill_usb_dac
                },
                {
                    "name": "Test Output",
                    "suffix": "R -> L",
                    "action": self._trigger_output_test
                },
                {
                    "name": "Auto-Tune Best",
                    "suffix": "★",
                    "action": self._prompt_auto_best
                },
                {
                    "name": "Restore Default",
                    "suffix": "⚙",
                    "action": self._prompt_restore_defaults
                }
            ]
        elif self.current_menu == "walkplay_dsp":
            self.title = "Walkplay DSP"
            dev = getattr(self, "_wp_dev", None)
            if not dev:
                items = [
                    {"name": "Walkplay DAC", "suffix": "Not Found", "action": lambda: (self._refresh_walkplay_state(toast=True), self._rebuild_menu(preserve_cursor=False))}
                ]
            else:
                fw = getattr(self, "_wp_fw", "?")
                peq_on = getattr(self, "_wp_peq_en", True)
                gain_hi = getattr(self, "_wp_gain_high", True)
                flt_v = getattr(self, "_wp_filter", 1)
                flt_lbl = {0: "Fast Linear", 1: "Slow Lin ★", 2: "Fast MinPh", 3: "Slow MinPh", 4: "NOS"}.get(flt_v, f"Mode {flt_v}")
                short_name = dev["name"].replace("BGVP ", "")[:10]
                items = [
                    {"name": short_name, "suffix": f"v{fw}", "action": lambda: (self._refresh_walkplay_state(toast=True), self._rebuild_menu(preserve_cursor=True))},
                    {"name": "DAC Filter", "suffix": flt_lbl, "action": lambda: self._cycle_walkplay_filter(1), "on_tune": self._cycle_walkplay_filter},
                    {"name": "Gain Mode", "suffix": "High ★" if gain_hi else "Low", "action": self._toggle_walkplay_gain, "on_tune": lambda _d: self._toggle_walkplay_gain()},
                    {"name": "PEQ", "suffix": "ON ★" if peq_on else "OFF", "end_indicator": ">", "action": self._open_walkplay_peq},
                    {"name": "Save to DAC", "suffix": "Flash >", "action": self._save_walkplay_flash},
                ]
        elif self.current_menu == "walkplay_peq":
            self.title = "Walkplay PEQ"
            peq_on = getattr(self, "_wp_peq_en", True)
            pr = self.db.get_setting("walkplay_preset", "flat")
            pr_lbl = {"flat": "Flat ★", "bass": "Bass +3.5", "vocal": "Vocal", "warm": "Warm", "air": "Air", "custom": "Custom"}.get(pr, pr.title())
            pre_db = float(getattr(self, "_wp_preamp", 0.0))
            items = [
                {"name": "PEQ Power", "suffix": "ON ★" if peq_on else "OFF", "action": self._toggle_walkplay_peq, "on_tune": lambda _d: self._toggle_walkplay_peq()},
                {"name": "Preamp", "suffix": f"{int(round(pre_db)):+d}dB", "on_tune": self._tune_walkplay_preamp},
                {"name": "PEQ Preset", "suffix": pr_lbl, "action": lambda: self._cycle_walkplay_preset(1), "on_tune": self._cycle_walkplay_preset},
            ]
            for idx, b in enumerate(getattr(self, "_wp_bands", [])[:8]):
                f_hz = b["freq"]
                f_s = f"{f_hz/1000:.1f}k" if f_hz >= 1000 else f"{f_hz}Hz"
                g_s = f"{b['gain']:+.1f}dB"
                items.append({
                    "name": f"B{idx+1} {f_s}",
                    "suffix": g_s,
                    "end_indicator": ">",
                    "action": lambda bi=idx: self._open_walkplay_band(bi),
                    "on_tune": lambda d, bi=idx: self._tune_walkplay_band(bi, d),
                })
        elif self.current_menu == "walkplay_band":
            bi = getattr(self, "_wp_band_idx", 0)
            bands = getattr(self, "_wp_bands", [])
            self.title = f"PEQ Band {bi + 1}"
            if bi >= len(bands):
                items = [{"name": "No Band Data", "action": lambda: None}]
            else:
                b = bands[bi]
                ft_lbl = {1: "Low Shelf", 2: "Peaking ★", 3: "High Shelf"}.get(b["ftype"], f"Type {b['ftype']}")
                f_s = f"{b['freq']/1000:.2f}kHz" if b["freq"] >= 1000 else f"{b['freq']}Hz"
                items = [
                    {"name": "Gain", "suffix": f"{b['gain']:+.1f}dB", "on_tune": lambda d, i=bi: self._tune_walkplay_band(i, d)},
                    {"name": "Freq", "suffix": f_s, "on_tune": lambda d, i=bi: self._tune_walkplay_band_freq(i, d)},
                    {"name": "Q Factor", "suffix": f"{b['q']:.2f}", "on_tune": lambda d, i=bi: self._tune_walkplay_band_q(i, d)},
                    {"name": "Filter Type", "suffix": ft_lbl, "action": lambda i=bi: self._cycle_walkplay_band_type(i, 1), "on_tune": lambda d, i=bi: self._cycle_walkplay_band_type(i, d)},
                ]
        elif self.current_menu == "mic":
            self.title = "Microphone"
            items = [
                {
                    "name": "Mic Gain",
                    "suffix": mic_gain_str,
                    "on_tune": self._tune_mic_gain
                },
                {
                    "name": "Mic Balance",
                    "suffix": mic_bal_str,
                    "on_tune": self._tune_mic_balance
                },
                {
                    "name": "Calibrate Mics",
                    "suffix": "Auto L/R",
                    "action": self._trigger_mic_calibration
                },
                {
                    "name": "Mic Range",
                    "suffix": mic_range_str,
                    "action": self._cycle_mic_range
                },
                {
                    "name": "Wind Cut",
                    "suffix": wind_hpf_str,
                    "on_tune": self._tune_mic_wind_hpf
                },
                {
                    "name": "Mic Mode",
                    "suffix": mic_ch_str,
                    "action": self._cycle_mic_channel_mode
                },
                {
                    "name": "Mic Swap",
                    "suffix": mic_swap_str,
                    "action": self._toggle_mic_swap
                },
                {
                    "name": "I2S Framing",
                    "suffix": mic_framing_str,
                    "action": self._cycle_mic_i2s_framing
                },
                {
                    "name": "Mic BCLK Edge",
                    "suffix": mic_edge_str,
                    "action": self._toggle_mic_bclk_edge
                },
                {
                    "name": "Test Mic",
                    "suffix": "Live R->L",
                    "action": self._trigger_mic_test
                },
                {
                    "name": "Ultrasonic Scope",
                    "suffix": "Open >",
                    "action": lambda: asyncio.create_task(self._open_mic_scope())
                },
                {
                    "name": "Auto-Tune Best",
                    "suffix": "★",
                    "action": self._prompt_auto_best
                },
                {
                    "name": "Restore Default",
                    "suffix": "⚙",
                    "action": self._prompt_restore_defaults
                }
            ]
        elif self.current_menu == "mix":
            self.title = "Transp & Mix"
            transp_on = getattr(self.ui, 'transparency_enabled', False)
            transp_str = "ON" if transp_on else "OFF ⚙"

            transp_pct = int(self.db.get_setting("transparency_vol_pct", "100"))
            transp_vol_str = f"{transp_pct}% ★" if transp_pct == 100 else f"{transp_pct}%"

            duck_mode = self.db.get_setting("transparency_duck", "off")
            duck_map = {
                "off": "Off ★",
                "6db": "-6dB (50%)",
                "12db": "-12dB (25%)",
                "mute": "Mute (0%)"
            }
            duck_str = duck_map.get(duck_mode, "Off ★")

            feed_mode = getattr(self.ui, 'mic_feed_mode', 'mic')
            feed_map = {
                "mic": "Board Mics ★",
                "music": "Music Loop",
                "mix": "Mic + Music"
            }
            feed_str = feed_map.get(feed_mode, "Board Mics ★")

            feed_pct = int(self.db.get_setting("mic_feed_vol_pct", "100"))
            feed_vol_str = f"{feed_pct}% ★" if feed_pct == 100 else f"{feed_pct}%"

            items = [
                {
                    "name": "Transparency",
                    "suffix": transp_str,
                    "action": self._toggle_transparency
                },
                {
                    "name": "Transp. Vol",
                    "suffix": transp_vol_str,
                    "on_tune": self._tune_transparency_vol
                },
                {
                    "name": "Music Duck",
                    "suffix": duck_str,
                    "action": self._cycle_transparency_duck
                },
                {
                    "name": "Mic Feed",
                    "suffix": feed_str,
                    "action": self._cycle_mic_feed_mode
                },
                {
                    "name": "Music Feed Vol",
                    "suffix": feed_vol_str,
                    "on_tune": self._tune_mic_feed_vol
                },
                {
                    "name": "Auto-Tune Best",
                    "suffix": "★",
                    "action": self._prompt_auto_best
                },
                {
                    "name": "Restore Default",
                    "suffix": "⚙",
                    "action": self._prompt_restore_defaults
                }
            ]
        elif self.current_menu == "dsp":
            self.title = "DSP & Engine"
            pmic_mode = str(self.db.get_setting("pmic_power_mode", "2"))
            pmic_map = {
                "0": "Stock (PFM) ⚙",
                "1": "DVM Smooth",
                "2": "Fixed PWM ★",
                "3": "Spread PWM"
            }
            pmic_str = pmic_map.get(pmic_mode, "Fixed PWM ★")
            items = [
                {
                    "name": "DSP Mode",
                    "suffix": excl_mode,
                    "action": self._toggle_exclusive_mode
                },
                {
                    "name": "Real-Time CPU",
                    "suffix": rt_str,
                    "action": self._toggle_rt_cpu
                },
                {
                    "name": "PMIC Power",
                    "suffix": pmic_str,
                    "action": self._cycle_pmic_mode
                },
                {
                    "name": "Auto-Tune Best",
                    "suffix": "★",
                    "action": self._prompt_auto_best
                },
                {
                    "name": "Restore Default",
                    "suffix": "⚙",
                    "action": self._prompt_restore_defaults
                }
            ]
        elif self.current_menu == "volume":
            self.title = "Volume"
            sw_vol_str = getattr(self.ui, "pi_volume_str", None)
            if not sw_vol_str:
                _sw_key = "exclusive_volume" if raw_excl in ("1", "2") else "standard_volume"
                sw_vol_str = f"{float(self.db.get_setting(_sw_key, '1.0')) * 100:.0f}%"
            usb_hw_vol_str = self._format_usb_hw_vol()
            transp_pct = int(self.db.get_setting("transparency_vol_pct", "100"))
            transp_vol_str = f"{transp_pct}% ★" if transp_pct == 100 else f"{transp_pct}%"
            feed_pct = int(self.db.get_setting("mic_feed_vol_pct", "100"))
            feed_vol_str = f"{feed_pct}% ★" if feed_pct == 100 else f"{feed_pct}%"
            items = [
                {
                    "name": "Software Vol",
                    "suffix": sw_vol_str,
                    "on_tune": self._tune_software_volume
                },
                {
                    "name": "Volume Mode",
                    "suffix": excl_mode,
                    "action": self._toggle_exclusive_mode
                },
                {
                    "name": "USB HW Vol",
                    "suffix": usb_hw_vol_str,
                    "on_tune": self._tune_usb_dac_hw_volume,
                    "action": self._refresh_usb_dac_hw_volume
                },
                {
                    "name": "I2S HW Vol",
                    "suffix": dig_vol_str,
                    "on_tune": self._tune_i2s_digital_volume
                },
                {
                    "name": "I2S Analog",
                    "suffix": gain_str,
                    "action": self._toggle_i2s_gain
                },
                {
                    "name": "I2S Balance",
                    "suffix": bal_str,
                    "on_tune": self._tune_i2s_balance
                },
                {
                    "name": "I2S Ramp",
                    "suffix": ramp_str,
                    "action": self._cycle_i2s_vol_ramp
                },
                {
                    "name": "Mic Gain",
                    "suffix": mic_gain_str,
                    "on_tune": self._tune_mic_gain
                },
                {
                    "name": "Transp. Vol",
                    "suffix": transp_vol_str,
                    "on_tune": self._tune_transparency_vol
                },
                {
                    "name": "Music Feed Vol",
                    "suffix": feed_vol_str,
                    "on_tune": self._tune_mic_feed_vol
                }
            ]
        else:
            self.title = "Audio Settings"
            items = [
                {
                    "name": "Output",
                    "suffix": dac_out_str,
                    "action": self._toggle_audio_output
                },
                {
                    "name": "Volume",
                    "suffix": ">",
                    "action": lambda: self._open_submenu("volume")
                },
                {
                    "name": "I2S DAC",
                    "suffix": ">",
                    "action": lambda: self._open_submenu("i2s_dac")
                },
                {
                    "name": "USB DAC",
                    "suffix": ">",
                    "action": lambda: self._open_submenu("usb_dac")
                },
                {
                    "name": "Microphone",
                    "suffix": ">",
                    "action": lambda: self._open_submenu("mic")
                },
                {
                    "name": "Transp & Mix",
                    "suffix": ">",
                    "action": lambda: self._open_submenu("mix")
                },
                {
                    "name": "DSP & Engine",
                    "suffix": ">",
                    "action": lambda: self._open_submenu("dsp")
                },
                {
                    "name": "Auto-Tune Best",
                    "suffix": "★",
                    "action": self._prompt_auto_best
                },
                {
                    "name": "Restore Default",
                    "suffix": "⚙",
                    "action": self._prompt_restore_defaults
                }
            ]

        self.update_items(items, preserve_cursor=preserve_cursor)
        
