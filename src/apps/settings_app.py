# /vipxpert_minimal_os/src/apps/settings_app.py
import asyncio
from src.ui.components import ListMenuApp

class SettingsApp(ListMenuApp):
    """Dedicated Settings App handling hardware states, UI customizations, diagnostics, and dynamic timeouts."""
    def __init__(self, display, ui_manager, db):
        self.db = db
        self.current_menu = "root"
        self._hw_dac_status = "Tap Refresh >"
        super().__init__(
            display=display,
            ui_manager=ui_manager,
            title="Settings",
            items=[],
            parent_app_id='app_selector'
        )

    async def on_start(self):
        self.current_menu = "root"
        current_brightness = int(self.db.get_setting("brightness", "128"))
        self.display.set_brightness(current_brightness)
        invert_disp = self.db.get_setting("invert_display", "0") == "1"
        if hasattr(self.display, 'set_invert'):
            self.display.set_invert(invert_disp)
        oled_fps = max(10, min(120, int(self.db.get_setting("oled_fps", "20"))))
        self.ui.frame_interval = 1.0 / oled_fps
        if hasattr(self.display, "apply_i2c_clock_profile"):
            self.display.apply_i2c_clock_profile(oled_fps)
        self._rebuild_menu(preserve_cursor=False)
        if hasattr(super(), 'on_start'):
            await super().on_start()

    async def on_resume(self, previous_app_id=None):
        self._rebuild_menu(preserve_cursor=True)
        if hasattr(super(), 'on_resume'):
            await super().on_resume(previous_app_id=previous_app_id)

    def _open_submenu(self, menu_id: str):
        # Submenu Router (Transitions internal menu view, resets tuning state, and refreshes menu items)
        self.current_menu = menu_id
        self.is_tuning = False
        if menu_id == "hw_diag":
            asyncio.create_task(self._refresh_hw_diag(toast=False))
        self._rebuild_menu(preserve_cursor=False)
        self.ui.request_render()

    def _enter_submenu(self, menu_id: str):
        # Submenu Alias (Routes Lock & Scenes submenu actions to _open_submenu)
        self._open_submenu(menu_id)

    def _open_pwm_calibrator(self):
        """Launches the interactive full-screen OLED PWM Sync Calibrator (4/6=Swipe, 2/8=Step, C=Save, D=Cancel)."""
        cur_s = self.db.get_setting("oled_pwm_dt", str(getattr(self.display, "_pwm_base_dt", 0.006443948)))
        try:
            self._cal_orig_dt = float(cur_s)
        except Exception:
            self._cal_orig_dt = 0.006443948
        self._cal_cur_dt = self._cal_orig_dt
        self._cal_step_dt = 0.0000040
        self._cal_last_dir = None
        self._cal_rev = 0
        self.current_menu = "pwm_cal"
        self.is_tuning = False
        self.ui.request_render()

    def _open_gray_calibrator(self):
        """Launches the interactive Gray Engine Calibrator matching dithered 33%/67% shades against Pure PWM."""
        self._gcal_orig_lo = int(self.db.get_setting("oled_dither_lo", str(getattr(self.display, "dither_lo", 42))))
        self._gcal_orig_hi = int(self.db.get_setting("oled_dither_hi", str(getattr(self.display, "dither_hi", 70))))
        self._gcal_orig_algo = self.db.get_setting("oled_dither_algo", getattr(self.display, "dither_algo", "bayer8"))
        self._gcal_orig_ramp = self.db.get_setting("oled_dither_ramp", "1" if getattr(self.display, "dither_ramp", True) else "0") != "0"
        self._gcal_lo = max(4, min(96, self._gcal_orig_lo))
        self._gcal_hi = max(self._gcal_lo, min(96, self._gcal_orig_hi))
        self._gcal_algo = self._gcal_orig_algo
        self._gcal_ramp = self._gcal_orig_ramp
        self._gcal_sel = 0  # 0=Lo(33%), 1=Hi(67%)
        self.current_menu = "gray_cal"
        self.is_tuning = False
        self.ui.request_render()

    def _open_tone_calibrator(self):
        """Launches the live Gradient Mapping (T0/T1/T2 + Grad/Snap) tuner with PWM & 16-scale dither preview."""
        self._tcal_orig_t0 = int(self.db.get_setting("oled_pwm_t0", str(getattr(self.display, "pwm_t0", 48))))
        self._tcal_orig_t1 = int(self.db.get_setting("oled_pwm_t1", str(getattr(self.display, "pwm_t1", 128))))
        self._tcal_orig_t2 = int(self.db.get_setting("oled_pwm_t2", str(getattr(self.display, "pwm_t2", 208))))
        self._tcal_orig_ramp = self.db.get_setting("oled_dither_ramp", "1" if getattr(self.display, "dither_ramp", True) else "0") != "0"
        self._tcal_t0 = max(1, min(250, self._tcal_orig_t0))
        self._tcal_t1 = max(self._tcal_t0 + 1, min(252, self._tcal_orig_t1))
        self._tcal_t2 = max(self._tcal_t1 + 1, min(254, self._tcal_orig_t2))
        self._tcal_ramp = self._tcal_orig_ramp
        self._tcal_sel = 0  # 0=T0, 1=T1, 2=T2
        self.current_menu = "tone_cal"
        self.is_tuning = False
        self.ui.request_render()

    def _open_disp_calibrator(self):
        """Launches live Brightness / Pixel Drive / Contrast demo & tuning screen."""
        self._dcal_orig_brt = int(self.db.get_setting("brightness", "128"))
        self._dcal_orig_drv = self.db.get_setting("oled_drive_mode", "normal")
        self._dcal_orig_inv = self.db.get_setting("invert_display", "0") == "1"
        self._dcal_brt = self._dcal_orig_brt
        self._dcal_drv = self._dcal_orig_drv
        self._dcal_inv = self._dcal_orig_inv
        self._dcal_sel = 0  # 0=Brightness, 1=Drive, 2=Contrast(Invert)
        self.current_menu = "disp_cal"
        self.is_tuning = False
        self.ui.request_render()

    def _open_gray_mode_calibrator(self):
        """Launches live Gray Mode (Hybrid/Pure PWM/Dithering/1-Bit) 3D + 4-shade demo & tuning screen."""
        cur_gm = self.db.get_setting("oled_gray_mode", getattr(self.display, "gray_mode", "hybrid"))
        if cur_gm not in ("hybrid", "pwm", "dithering", "off"):
            cur_gm = "hybrid"
        self._gmcal_orig_gm = cur_gm
        self._gmcal_orig_dm = self.db.get_setting("oled_dither_mode", getattr(self.display, "dither_mode", "pwm"))
        self._gmcal_gm = cur_gm
        self.current_menu = "gray_mode_cal"
        self.is_tuning = False
        self.ui.request_render()

    async def handle_key(self, action: str, char: str):
        if self.current_menu == "pwm_cal":
            if action in ("CLICK", "HOLD") and char in ("4", "6"):
                d_key = "L" if char == "4" else "R"
                if self._cal_last_dir is not None and d_key != self._cal_last_dir:
                    self._cal_step_dt = max(1e-9, self._cal_step_dt / 2.0)
                    self._cal_rev += 1
                self._cal_last_dir = d_key
                self._cal_cur_dt += (self._cal_step_dt if d_key == "R" else -self._cal_step_dt)
                if hasattr(self.display, "set_pwm_base_dt"):
                    self.display.set_pwm_base_dt(self._cal_cur_dt)
                self.ui.request_render()
            elif action in ("CLICK", "HOLD") and char in ("2", "8"):
                self._cal_step_dt = min(1e-4, self._cal_step_dt * 2.0) if char == "2" else max(1e-9, self._cal_step_dt / 2.0)
                self._cal_last_dir = None
                self.ui.request_render()
            elif action == "CLICK" and char == "C":
                self.db.set_setting("oled_pwm_dt", f"{self._cal_cur_dt:.9f}")
                if hasattr(self.display, "set_pwm_base_dt"):
                    self.display.set_pwm_base_dt(self._cal_cur_dt)
                self.current_menu = "gray_cfg"
                self._rebuild_menu(preserve_cursor=True)
                self.show_info_toast(f"Saved {self._cal_cur_dt*1e3:.4f}ms")
                self.ui.request_render()
            elif action == "CLICK" and char == "D":
                if hasattr(self.display, "set_pwm_base_dt"):
                    self.display.set_pwm_base_dt(getattr(self, "_cal_orig_dt", 0.006443948))
                self.current_menu = "gray_cfg"
                self._rebuild_menu(preserve_cursor=True)
                self.show_info_toast("Calib Canceled")
                self.ui.request_render()
            return
        if self.current_menu == "disp_cal":
            if action == "CLICK" and char in ("2", "8"):
                self._dcal_sel = (getattr(self, "_dcal_sel", 0) + (-1 if char == "2" else 1)) % 3
                self.ui.request_render()
            elif action in ("CLICK", "HOLD") and char in ("4", "6"):
                step = -1 if char == "4" else 1
                sel = getattr(self, "_dcal_sel", 0)
                if sel == 0:
                    pct = round((self._dcal_brt / 255) * 100)
                    pct = max(0, min(100, pct + step * 5))
                    self._dcal_brt = round((pct / 100) * 255)
                    self.display.set_brightness(self._dcal_brt)
                elif sel == 1:
                    opts = ["night", "soft", "normal", "crisp"]
                    idx = (opts.index(self._dcal_drv) + step) % len(opts) if self._dcal_drv in opts else 2
                    self._dcal_drv = opts[idx]
                    if hasattr(self.display, "apply_oled_drive_profile"):
                        self.display.apply_oled_drive_profile("max", self._dcal_drv)
                else:
                    self._dcal_inv = not self._dcal_inv
                    if hasattr(self.display, "set_invert"):
                        self.display.set_invert(self._dcal_inv)
                self.ui.request_render()
            elif action == "CLICK" and char == "C":
                self.db.set_setting("brightness", str(self._dcal_brt))
                self.db.set_setting("oled_drive_mode", self._dcal_drv)
                self.db.set_setting("invert_display", "1" if self._dcal_inv else "0")
                self.current_menu = "panel_cfg"
                self._rebuild_menu(preserve_cursor=True)
                self.show_info_toast(f"Saved {round((self._dcal_brt/255)*100)}% {self._dcal_drv}")
                self.ui.request_render()
            elif action == "CLICK" and char == "D":
                self.display.set_brightness(getattr(self, "_dcal_orig_brt", 128))
                if hasattr(self.display, "apply_oled_drive_profile"):
                    self.display.apply_oled_drive_profile("max", getattr(self, "_dcal_orig_drv", "normal"))
                if hasattr(self.display, "set_invert"):
                    self.display.set_invert(getattr(self, "_dcal_orig_inv", False))
                self.current_menu = "panel_cfg"
                self._rebuild_menu(preserve_cursor=True)
                self.show_info_toast("Display Canceled")
                self.ui.request_render()
            return
        if self.current_menu == "gray_mode_cal":
            if action in ("CLICK", "HOLD") and char in ("4", "6"):
                opts = ["hybrid", "pwm", "dithering", "off"]
                idx = (opts.index(self._gmcal_gm) + (-1 if char == "4" else 1)) % len(opts) if self._gmcal_gm in opts else 0
                self._gmcal_gm = opts[idx]
                self.display.gray_mode = self._gmcal_gm
                self.display.dither_mode = "off" if self._gmcal_gm == "off" else ("spatial" if self._gmcal_gm == "dithering" else "pwm")
                self.ui.request_render()
            elif action == "CLICK" and char == "C":
                # Comment (decision): Sync legacy oled_dither_mode with oled_gray_mode so scenes checking dither_mode (e.g. ambient_cube3d) stay consistent with the single Gray Mode setting.
                dm = "off" if self._gmcal_gm == "off" else ("spatial" if self._gmcal_gm == "dithering" else "pwm")
                self.db.set_setting("oled_gray_mode", self._gmcal_gm)
                self.db.set_setting("oled_dither_mode", dm)
                self.display.gray_mode = self._gmcal_gm
                self.display.dither_mode = dm
                self.current_menu = "gray_cfg"
                self._rebuild_menu(preserve_cursor=True)
                self.show_info_toast(f"Gray: {self._gmcal_gm}")
                self.ui.request_render()
            elif action == "CLICK" and char == "D":
                self.display.gray_mode = getattr(self, "_gmcal_orig_gm", "hybrid")
                self.display.dither_mode = getattr(self, "_gmcal_orig_dm", "pwm")
                self.current_menu = "gray_cfg"
                self._rebuild_menu(preserve_cursor=True)
                self.show_info_toast("Gray Canceled")
                self.ui.request_render()
            return
        if self.current_menu == "gray_cal":
            if action == "CLICK" and char in ("2", "8"):
                self._gcal_sel = 1 - getattr(self, "_gcal_sel", 0)
                self.ui.request_render()
            elif action in ("CLICK", "HOLD") and char in ("4", "6"):
                step = -2 if char == "4" else 2
                if getattr(self, "_gcal_sel", 0) == 0:
                    self._gcal_lo = max(4, min(self._gcal_hi, self._gcal_lo + step))
                    self.display.dither_lo = self._gcal_lo
                else:
                    self._gcal_hi = max(self._gcal_lo, min(96, self._gcal_hi + step))
                    self.display.dither_hi = self._gcal_hi
                self.ui.request_render()
            elif action == "CLICK" and char == "0":
                algos = ["bayer8", "bayer4", "bayer2", "cluster", "ign", "floyd", "atkinson", "sierra", "temporal", "bluenoise"]
                idx = (algos.index(self._gcal_algo) + 1) % len(algos) if self._gcal_algo in algos else 0
                self._gcal_algo = algos[idx]
                self.display.dither_algo = self._gcal_algo
                self.ui.request_render()
            elif action == "CLICK" and char == "#":
                self._gcal_ramp = not self._gcal_ramp
                self.display.dither_ramp = self._gcal_ramp
                self.ui.request_render()
            elif action == "CLICK" and char == "C":
                self.db.set_setting("oled_dither_lo", str(self._gcal_lo))
                self.db.set_setting("oled_dither_hi", str(self._gcal_hi))
                self.db.set_setting("oled_dither_algo", self._gcal_algo)
                self.db.set_setting("oled_dither_ramp", "1" if self._gcal_ramp else "0")
                self.display.dither_lo = self._gcal_lo
                self.display.dither_hi = self._gcal_hi
                self.display.dither_algo = self._gcal_algo
                self.display.dither_ramp = self._gcal_ramp
                self.current_menu = "gray_cfg"
                self._rebuild_menu(preserve_cursor=True)
                self.show_info_toast(f"Saved Lo:{self._gcal_lo}% Hi:{self._gcal_hi}%")
                self.ui.request_render()
            elif action == "CLICK" and char == "D":
                self.display.dither_lo = getattr(self, "_gcal_orig_lo", 42)
                self.display.dither_hi = getattr(self, "_gcal_orig_hi", 70)
                self.display.dither_algo = getattr(self, "_gcal_orig_algo", "bayer8")
                self.display.dither_ramp = getattr(self, "_gcal_orig_ramp", True)
                self.current_menu = "gray_cfg"
                self._rebuild_menu(preserve_cursor=True)
                self.show_info_toast("Calib Canceled")
                self.ui.request_render()
            return
        if self.current_menu == "tone_cal":
            if action == "CLICK" and char in ("2", "8"):
                delta = -1 if char == "2" else 1
                self._tcal_sel = (getattr(self, "_tcal_sel", 0) + delta) % 3
                self.ui.request_render()
            elif action in ("CLICK", "HOLD") and char in ("4", "6"):
                step = -4 if char == "4" else 4
                sel = getattr(self, "_tcal_sel", 0)
                if sel == 0:
                    self._tcal_t0 = max(1, min(self._tcal_t1 - 4, self._tcal_t0 + step))
                elif sel == 1:
                    self._tcal_t1 = max(self._tcal_t0 + 4, min(self._tcal_t2 - 4, self._tcal_t1 + step))
                else:
                    self._tcal_t2 = max(self._tcal_t1 + 4, min(254, self._tcal_t2 + step))
                self.display.set_gray_engine_config(pwm_t0=self._tcal_t0, pwm_t1=self._tcal_t1, pwm_t2=self._tcal_t2)
                self.ui.request_render()
            elif action == "CLICK" and char == "0":
                self._tcal_t0, self._tcal_t1, self._tcal_t2 = 48, 128, 208
                self.display.set_gray_engine_config(pwm_t0=48, pwm_t1=128, pwm_t2=208)
                self.ui.request_render()
            elif action == "CLICK" and char == "#":
                self._tcal_ramp = not getattr(self, "_tcal_ramp", True)
                self.display.set_gray_engine_config(dither_ramp=self._tcal_ramp)
                self.ui.request_render()
            elif action == "CLICK" and char == "C":
                self.db.set_setting("oled_pwm_t0", str(self._tcal_t0))
                self.db.set_setting("oled_pwm_t1", str(self._tcal_t1))
                self.db.set_setting("oled_pwm_t2", str(self._tcal_t2))
                self.db.set_setting("oled_dither_ramp", "1" if getattr(self, "_tcal_ramp", True) else "0")
                self.display.set_gray_engine_config(pwm_t0=self._tcal_t0, pwm_t1=self._tcal_t1, pwm_t2=self._tcal_t2, dither_ramp=getattr(self, "_tcal_ramp", True))
                self.current_menu = "gray_cfg"
                self._rebuild_menu(preserve_cursor=True)
                self.show_info_toast(f"Saved {'Grad' if getattr(self, '_tcal_ramp', True) else 'Snap'} {self._tcal_t0}/{self._tcal_t1}/{self._tcal_t2}")
                self.ui.request_render()
            elif action == "CLICK" and char == "D":
                self.display.set_gray_engine_config(
                    pwm_t0=getattr(self, "_tcal_orig_t0", 48),
                    pwm_t1=getattr(self, "_tcal_orig_t1", 128),
                    pwm_t2=getattr(self, "_tcal_orig_t2", 208),
                    dither_ramp=getattr(self, "_tcal_orig_ramp", True),
                )
                self.current_menu = "gray_cfg"
                self._rebuild_menu(preserve_cursor=True)
                self.show_info_toast("Gradient Canceled")
                self.ui.request_render()
            return
        if not getattr(self, 'is_tuning', False) and self.current_menu != "root":
            if char == 'D' and action == 'CLICK':
                # Stage: Hierarchical Back Navigation Map (Maps every nested Lock & Scenes view to its parent menu)
                prev_m = self.current_menu
                parent_map = {
                    "gray_cfg":       "display_ui",
                    "ui_ux_cfg":      "display_ui",
                    "panel_cfg":      "display_ui",
                    "lock_slots":     "lock_cfg",
                    "lock_slot_item": "lock_slots",
                    "lock_cat":        "lock_slots",
                    "lock_cat_scenes": "lock_cat",
                    "lock_viz_cfg":   "lock_cfg",
                    "lock_viz_phys":  "lock_viz_cfg",
                    "lock_mv_cfg":    "lock_cfg",
                    "lock_enables":   "lock_cfg",
                    "lock_timers":    "lock_cfg",
                    "lock_hud_fx":    "lock_cfg",
                    "lock_scn_list":  "lock_cfg",
                    "lock_scn_item":  "lock_scn_list",
                    "lock_scn_viz":   "lock_scn_item",
                }
                self.current_menu = parent_map.get(prev_m, "root")
                restore_cur = None
                if prev_m == "lock_cat":
                    restore_cur = getattr(self, "_pick_slot_cursor", 0)
                elif prev_m == "lock_cat_scenes":
                    restore_cur = getattr(self, "_pick_cat_cursor", 0)
                elif prev_m == "lock_scn_item":
                    restore_cur = getattr(self, "_scn_list_cursor", 0)
                if restore_cur is not None:
                    self.cursor_index = restore_cur
                    self._rebuild_menu(preserve_cursor=True)
                else:
                    self._rebuild_menu(preserve_cursor=False)
                self.ui.request_render()
                return
        await super().handle_key(action, char)

    def _tune_brightness(self, delta: int):
        """Callback attached to 'on_tune' for dynamic 1% level adjustments."""
        current = int(self.db.get_setting("brightness", "128"))
        
        # Convert hardware 0-255 scale to 0-100 percentage, apply 1% step, and map back
        current_pct = round((current / 255) * 100)
        new_pct = max(0, min(100, current_pct + delta))
        new_val = round((new_pct / 100) * 255)
        
        self.db.set_setting("brightness", str(new_val))
        self.display.set_brightness(new_val)
        self._rebuild_menu()
        self.ui.request_render()

    def _toggle_invert_display(self):
        """Toggles high-contrast inverted pixel rendering mode."""
        current = self.db.get_setting("invert_display", "0")
        new_val = "1" if current == "0" else "0"
        self.db.set_setting("invert_display", new_val)
        if hasattr(self.display, 'set_invert'):
            self.display.set_invert(new_val == "1")
        self._rebuild_menu()
        self.ui.request_render()

    def _toggle_clock_format(self):
        """Toggles Lock Screen clock format between 24-Hour and 12-Hour."""
        current = self.db.get_setting("clock_format", "24h")
        new_val = "12h" if current == "24h" else "24h"
        self.db.set_setting("clock_format", new_val)
        self._rebuild_menu()
        self.ui.request_render()

    def _tune_marquee_speed(self, direction: int = 1):
        """Slider callback stepping global marquee scroll speed across Slow (2.0), Normal (3.0), and Fast (4.5)."""
        from src.ui import components as ui_components
        options = ["2.0", "3.0", "4.5"]
        current = str(self.db.get_setting("marquee_speed", "3.0"))
        idx = options.index(current) if current in options else 1
        next_val = options[max(0, min(len(options) - 1, idx + (1 if direction > 0 else -1)))]
        self.db.set_setting("marquee_speed", next_val)
        ui_components.DEFAULT_MARQUEE_SPEED = float(next_val)
        self.ui.marquee_speed = float(next_val)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_oled_fps(self, direction: int = 1):
        """Slider callback stepping OLED compositor refresh rate from 10Hz up to 120Hz."""
        options = [10, 15, 20, 25, 30, 40, 45, 50, 60, 75, 90, 120]
        cur = int(self.db.get_setting("oled_fps", "20"))
        if cur not in options:
            cur = min(options, key=lambda x: abs(x - cur))
        idx = (options.index(cur) + direction) % len(options)
        new_fps = options[idx]
        self.db.set_setting("oled_fps", str(new_fps))
        self.ui.frame_interval = 1.0 / new_fps
        if hasattr(self.display, "apply_i2c_clock_profile"):
            self.display.apply_i2c_clock_profile(new_fps)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_oled_drive(self, direction: int = 1):
        """Slider callback stepping SH1106 0xD9 pre-charge drive (night=Sub-1%, soft=Warm, normal=Std, crisp=Punch)."""
        options = ["night", "soft", "normal", "crisp"]
        cur = self.db.get_setting("oled_drive_mode", "normal")
        idx = (options.index(cur) + direction) % len(options) if cur in options else 2
        nxt = options[idx]
        self.db.set_setting("oled_drive_mode", nxt)
        if hasattr(self.display, "apply_oled_drive_profile"):
            self.display.apply_oled_drive_profile("max", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_oled_dither(self, direction: int = 1):
        """Slider callback stepping system-wide Gray Shades mode (pwm=125Hz Pure PWM Gray ★, spatial=4x4 Bayer, temporal=2x2, off=Wireframe)."""
        opts = ["pwm", "spatial", "temporal", "off"]
        cur = self.db.get_setting("oled_dither_mode", "pwm")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        nxt = opts[idx]
        self.db.set_setting("oled_dither_mode", nxt)
        self.display.dither_mode = nxt
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_gray_mode(self, direction: int = 1):
        """Steps Universal Gray Engine mode (hybrid ★, pwm, dithering) live without touching oled_pwm_dt."""
        opts = ["hybrid", "pwm", "dithering"]
        cur = self.db.get_setting("oled_gray_mode", getattr(self.display, "gray_mode", "hybrid"))
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        nxt = opts[idx]
        self.db.set_setting("oled_gray_mode", nxt)
        self.display.gray_mode = nxt
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_dither_algo(self, direction: int = 1):
        """Steps Universal Gray Engine dither algorithm across all dither kernels live."""
        opts = ["bayer8", "bayer4", "bayer2", "cluster", "ign", "floyd", "atkinson", "sierra", "temporal", "bluenoise"]
        cur = self.db.get_setting("oled_dither_algo", getattr(self.display, "dither_algo", "bayer8"))
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        nxt = opts[idx]
        self.db.set_setting("oled_dither_algo", nxt)
        self.display.dither_algo = nxt
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _toggle_dither_ramp(self, _direction: int = 1):
        """Toggles between Smooth continuous ramp and 4-Band discrete luminance mapping in _apply_dither_img()."""
        cur = self.db.get_setting("oled_dither_ramp", "1") != "0"
        nxt = not cur
        self.db.set_setting("oled_dither_ramp", "1" if nxt else "0")
        self.display.dither_ramp = nxt
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _toggle_3d_demo(self):
        """Toggles a live 60-120FPS 3D cube & grayscale swatch overlay inside Display & UI."""
        self._show_3d_cube = not getattr(self, "_show_3d_cube", False)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _draw_state(self):
        """Renders the standard list menu, PWM Sync Calibrator, or 3D cube overlay in Display & UI."""
        if self.current_menu == "pwm_cal":
            dt_v = getattr(self, "_cal_cur_dt", 0.006443948)
            st_v = getattr(self, "_cal_step_dt", 0.0000040)
            rv_v = getattr(self, "_cal_rev", 0)
            dr_v = getattr(self, "_cal_last_dir", None) or "-"
            self.display.draw_text(2, 0, f"{dt_v*1e3:.5f}ms {1.0/dt_v:.2f}Hz", font_type="small")
            self.display.draw_text(2, 12, f"d:{st_v*1e6:.3f}u R:{rv_v}({dr_v})", font_type="small")
            self.display.draw_text(2, 24, "4/6:Dir 2/8:Stp C:Ok D:X", font_type="small")
            for i in range(16):
                bx = 4 + i * 7
                self.display.draw.rectangle((bx, 48, bx + 4, 56), fill=1)
                self.display.draw_shaded_rect(bx, 43, bx + 4, 43, shade=0.50, mode_override="pwm", phase_id=i)
            return
        if self.current_menu == "gray_cal":
            from PIL import Image
            lo = getattr(self, "_gcal_lo", 42)
            hi = getattr(self, "_gcal_hi", 70)
            sel = getattr(self, "_gcal_sel", 0)
            algo = getattr(self, "_gcal_algo", "bayer8")
            ramp = getattr(self, "_gcal_ramp", True)
            lo_tag = f">{lo}%<" if sel == 0 else f"{lo}%"
            hi_tag = f">{hi}%<" if sel == 1 else f"{hi}%"
            self.display.draw_text(2, 0, f"Lo:{lo_tag} Hi:{hi_tag}", font_type="small")
            self.display.draw_text(2, 12, f"{algo} {'Ramp' if ramp else '4Bnd'} 0:Alg", font_type="small")
            self.display.draw_text(2, 24, "2/8:Sel 4/6:+- C:Ok D:X", font_type="small")
            # Top strip (y=37..48): live dithered 4-band strip (0, 85, 170, 255)
            row = bytes([0] * 32 + [85] * 32 + [170] * 32 + [255] * 32)
            g_img = Image.frombytes("L", (128, 12), row * 12)
            if hasattr(self.display, "_apply_dither_img"):
                d_img = self.display._apply_dither_img(g_img, algo=algo, d_lo=lo, d_hi=hi, ramp=ramp)
                self.display.image.paste(d_img, (0, 37))
            # Bottom strip (y=51..62): solid 155Hz Pure PWM reference (0%, 33%, 67%, 100%)
            self.display.draw_shaded_rect(32, 51, 63, 62, shade=0.33, mode_override="pwm")
            self.display.draw_shaded_rect(64, 51, 95, 62, shade=0.67, mode_override="pwm")
            self.display.draw.rectangle((96, 51, 127, 62), fill=1)
            return
        if self.current_menu == "tone_cal":
            from PIL import Image
            t0 = getattr(self, "_tcal_t0", 48)
            t1 = getattr(self, "_tcal_t1", 128)
            t2 = getattr(self, "_tcal_t2", 208)
            ramp = getattr(self, "_tcal_ramp", True)
            sel = getattr(self, "_tcal_sel", 0)
            s0 = f">{t0}<" if sel == 0 else str(t0)
            s1 = f">{t1}<" if sel == 1 else str(t1)
            s2 = f">{t2}<" if sel == 2 else str(t2)
            self.display.draw_text(2, 0, f"T0:{s0} T1:{s1}", font_type="small")
            self.display.draw_text(2, 12, f"T2:{s2} {'Grad' if ramp else 'Snap'} #:M", font_type="small")
            self.display.draw_text(2, 24, "2/8:Sel 4/6:+- 0:Def C/D", font_type="small")
            for tx in (t0, t1, t2):
                px = max(0, min(127, int(tx * 127 / 255)))
                self.display.draw.line((px, 36, px, 38), fill=1)
            # Comment (order): Paste the 16-scale dither bar onto self.image BEFORE draw_gray_frame() so ph1/lo snapshot it at full 3/3 (100% white) duty instead of 1/3 (33% dark gray).
            scale_row = bytes(int((x // 8) * 255 / 15) for x in range(128))
            if hasattr(self.display, "_apply_dither_img"):
                d_img = self.display._apply_dither_img(Image.frombytes("L", (128, 11), scale_row * 11), ramp=ramp)
                self.display.image.paste(d_img, (0, 51))
            grad_row = bytes(int(x * 255 / 127) for x in range(128))
            self.display.draw_gray_frame(0, 39, 128, 11, grad_row * 11)
            return
        if self.current_menu == "disp_cal":
            sel = getattr(self, "_dcal_sel", 0)
            pct = round((getattr(self, "_dcal_brt", 128) / 255) * 100)
            drv = getattr(self, "_dcal_drv", "normal")
            inv = getattr(self, "_dcal_inv", False)
            b_s = f">{pct}%<" if sel == 0 else f"{pct}%"
            d_s = f">{drv}<" if sel == 1 else drv
            i_s = f">{'Inv' if inv else 'Std'}<" if sel == 2 else ("Inv" if inv else "Std")
            self.display.draw_text(2, 0, f"Brt:{b_s} Drv:{d_s}", font_type="small")
            self.display.draw_text(2, 12, f"Mode:{i_s}  2/8:Row", font_type="small")
            self.display.draw_text(2, 24, "4/6:Tune  C:Ok  D:X", font_type="small")
            # Live test pattern: 4-shade PWM strip + solid white + fine grid
            self.display.draw.rectangle((2, 38, 125, 61), outline=1, fill=0)
            self.display.draw_shaded_rect(4, 40, 33, 59, shade=0.33, mode_override="pwm")
            self.display.draw_shaded_rect(34, 40, 63, 59, shade=0.67, mode_override="pwm")
            self.display.draw.rectangle((64, 40, 93, 59), fill=1)
            for y in range(40, 60, 2):
                for x in range(95 + (y & 2), 124, 4):
                    self.display.draw.point((x, y), fill=1)
            return
        if self.current_menu == "gray_mode_cal":
            gm = getattr(self, "_gmcal_gm", "hybrid")
            lbl = {"hybrid": "Hybrid ★", "pwm": "Pure PWM", "dithering": "Dithering", "off": "Off (1-Bit)"}.get(gm, gm)
            self.display.draw_text(2, 0, f"Mode: {lbl}", font_type="small")
            self.display.draw_text(2, 12, "4/6:Mode C:Ok D:X", font_type="small")
            # Comment (order): Draw solid white box outline before render_wireframe_3d() so it is captured in ph0/ph1/lo; then composite the 4-shade bar via draw_gray_frame() so Hybrid/PWM/Dithering/Off all render true 4-shade planes.
            self.display.draw.rectangle((2, 32, 65, 59), outline=1, fill=0)
            if hasattr(self.display, "render_wireframe_3d"):
                verts = [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]
                edges = [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)]
                ang = getattr(self.display, "_frame_seq", 0) * 0.06
                self.display.render_wireframe_3d(verts, edges, ang * 0.6, ang, cx=98, cy=43, scale=17.0, dist=3.2)
            if hasattr(self.display, "draw_gray_frame"):
                row = bytes([0] * 15 + [85] * 15 + [170] * 15 + [255] * 15)
                self.display.draw_gray_frame(4, 34, 60, 24, row * 24, mode_override=gm)
            self.display.has_active_marquee = True
            return
        super()._draw_state()

    def _cycle_timeout(self, key_name: str):
        """Cycles through standard timeout options: 15s, 30s, 60s, 120s, 0 (Never)."""
        options = [15, 30, 60, 120, 0]
        current = int(self.db.get_setting(key_name, "30"))
        try:
            next_idx = (options.index(current) + 1) % len(options)
        except ValueError:
            next_idx = 0
        self.db.set_setting(key_name, str(options[next_idx]))
        self._rebuild_menu()
        self.ui.request_render()

    def _tune_lock_scene(self, db_key: str, default_id: str, direction: int = 1):
        """Slider callback assigning any Scene Cartridge to Clock, Music, or Ambient slots."""
        from src.ui.scene_manager import SCENE_CATALOG
        ids = [cid for cid, _ in SCENE_CATALOG]
        cur = self.db.get_setting(db_key, default_id)
        idx = (ids.index(cur) + direction) % len(ids) if cur in ids else 0
        self.db.set_setting(db_key, ids[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _toggle_lock_flag(self, db_key: str, default_val: str = "1"):
        """Toggles a boolean Lock Screen slot setting ('1' vs '0') in SQLite."""
        cur = self.db.get_setting(db_key, default_val)
        self.db.set_setting(db_key, "0" if cur == "1" else "1")
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_lock_ab_policy(self, direction: int = 1):
        """Slider callback stepping Manual A/B Override Timer Policy (reset, pin, strict)."""
        opts = ["reset", "pin", "strict"]
        cur = self.db.get_setting("lock_ab_policy", "reset")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_ab_policy", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_lock_saver_policy(self, direction: int = 1):
        """Slider callback stepping Screensaver Trigger Policy (idle_only, pause_only, rigid)."""
        opts = ["idle_only", "pause_only", "rigid"]
        cur = self.db.get_setting("lock_saver_policy", "idle_only")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_saver_policy", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_lock_timer(self, db_key: str, default_s: str = "30"):
        """Steps Ambient Idle Delay or Slot Cycle Loop timer across 10s, 20s, 30s, 60s, 0 (Off)."""
        opts = [10, 20, 30, 60, 0]
        cur = int(self.db.get_setting(db_key, default_s))
        nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else 30
        self.db.set_setting(db_key, str(nxt))
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _open_slot_detail(self, slot_id: str, slot_label: str, scene_db_key: str, default_scene: str, enable_db_key: str = None):
        """Opens the individual configuration menu for a specific Lock Screen slot."""
        self._pick_slot_id = slot_id
        self._pick_slot_label = slot_label
        self._pick_db_key = scene_db_key
        self._pick_default_scene = default_scene
        self._pick_enable_key = enable_db_key
        self._open_submenu("lock_slot_item")

    def _open_slot_categories(self, slot_label: str, db_key: str, default_scene: str):
        """Opens the 5-category scene selector (All Scenes + 4 Categories) for a specific Lock Screen slot."""
        from src.ui.scene_manager import SCENE_CATEGORIES, SceneManager
        if not SCENE_CATEGORIES:
            SceneManager.discover()
        self._pick_slot_cursor = self.cursor_index
        self._pick_slot_label = slot_label
        self._pick_db_key = db_key
        self._pick_default_scene = default_scene
        cur_sc = self.db.get_setting(db_key, default_scene)
        cat_idx = 0
        for idx_c, (cid, _ct, csids) in enumerate(SCENE_CATEGORIES):
            if cid != "cat_all" and cur_sc in csids:
                cat_idx = idx_c
                break
        self.current_menu = "lock_cat"
        self.is_tuning = False
        self.cursor_index = cat_idx
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _open_category_scenes(self, cat_title: str, cat_scenes: list):
        """Opens the list of scenes belonging to the selected category before launching live preview."""
        self._pick_cat_cursor = self.cursor_index
        self._pick_cat_title = cat_title
        self._pick_cat_scenes = list(cat_scenes)
        db_key = getattr(self, "_pick_db_key", "lock_slot_clock_scene")
        dflt_sc = getattr(self, "_pick_default_scene", "clock_digital")
        cur_sc = self.db.get_setting(db_key, dflt_sc)
        sc_idx = cat_scenes.index(cur_sc) if cur_sc in cat_scenes else 0
        self.current_menu = "lock_cat_scenes"
        self.is_tuning = False
        self.cursor_index = sc_idx
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_slot_timeout_override(self, slot_id: str):
        """Cycles individual slot timeout override across Use Global ★, 10s, 20s, 30s, 60s, and 0 (Pin/Never)."""
        opts = ["global", "10", "20", "30", "60", "0"]
        cur = self.db.get_setting(f"lock_timeout_{slot_id}", "global")
        nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"lock_timeout_{slot_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _open_scene_tuning_item(self, scene_id: str, scene_label: str):
        """Opens individual per-scene HUD & parameter settings for a specific Scene Cartridge."""
        self._scn_list_cursor = self.cursor_index
        self._tune_scene_id = scene_id
        self._tune_scene_label = scene_label
        self._open_submenu("lock_scn_item")

    def _cycle_scene_hud_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene '#' HUD mode across Use Global ★, Guide+Title, Title Only, and Clean Canvas."""
        opts = ["global", "0", "1", "2"]
        cur = self.db.get_setting(f"scene_hud_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_hud_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_intro_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene Intro banner duration across Use Global ★, Fade 3.0s, Fade 1.5s, Always On, and Hidden."""
        opts = ["global", "3.0", "1.5", "always", "0"]
        cur = self.db.get_setting(f"scene_intro_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_intro_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_db_opts(self, db_key: str, opts: list, default_val: str, direction: int = 1):
        """Generic bidirectional cycler for individual per-scene parameter settings."""
        cur = self.db.get_setting(db_key, default_val)
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else opts[0]
        self.db.set_setting(db_key, nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    # Stage: Per-Scene Default Map & One-Click Reset Helper
    _SCENE_PARAM_DEFAULTS = {
        "clock_digital": [("scn_clk_fmt_clock_digital", "global"), ("scn_clk_sec_clock_digital", "1"), ("scn_dig_date", "1")],
        "clock_analog": [("scn_clk_fmt_clock_analog", "global"), ("scn_ana_sec", "1"), ("scn_clk_sec_clock_analog", "1")],
        "music_classic": [("scn_mcls_prog", "1"), ("scn_mcls_foot", "marquee")],
        "ambient_cube3d": [("scn_3d_mesh", "0"), ("scn_3d_auto", "1"), ("scn_3d_scale", "20")],
        "ambient_pond": [("scn_pond_drops", "4"), ("scn_pond_spd", "1.15")],
        "ambient_stars": [("scn_star_spd", "1.5")],
        "ambient_bounce": [("scn_bnc_shape", "0"), ("scn_bnc_spd", "1.8")],
        "ambient_grass": [("scn_grass_wind", "0.8"), ("scn_grass_ht", "22")],
        "bad_apple": [("mv_autoplay_enabled", "1"), ("mv_match_mode", "smart")],
        "power_scope": [("scn_pwr_view", "0"), ("scn_pwr_stride", "1")],
        "mic_ultrasonic": [("scn_us_view", "0"), ("scn_us_lo_hz", "19500"), ("scn_us_gain", "2")],
        "ir_scope": [("scn_ir_zoom_us", "45000")],
        "ble_radar": [("scn_ble_view", "0"), ("scn_ble_geiger", "0")],
    }

    def _is_scene_customized(self, scene_id: str) -> bool:
        for p in ("scene_hud_beh", "scene_seq", "scene_t_sec", "scene_g_sec", "scene_anchor", "scene_stereo", "scene_bands", "scene_curve", "scene_bar", "scene_cap", "scene_decay"):
            if self.db.get_setting(f"{p}_{scene_id}", "global") != "global":
                return True
        for k_name, d_val in self._SCENE_PARAM_DEFAULTS.get(scene_id, []):
            if self.db.get_setting(k_name, d_val) != d_val:
                return True
        return False

    def _reset_scene_customization(self, scene_id: str):
        for p in ("scene_hud_beh", "scene_seq", "scene_t_sec", "scene_g_sec", "scene_anchor", "scene_stereo", "scene_bands", "scene_curve", "scene_bar", "scene_cap", "scene_decay"):
            self.db.set_setting(f"{p}_{scene_id}", "global")
        for k_name, d_val in self._SCENE_PARAM_DEFAULTS.get(scene_id, []):
            self.db.set_setting(k_name, d_val)
        self._rebuild_menu(preserve_cursor=True)
        self.show_info_toast("Scene Reset ★", duration=1.5)
        self.ui.request_render()

    async def _launch_category_picker(self, cat_scenes: list, db_key: str = None, default_scene: str = None, start_scene: str = None):
        # Live Visual Scene Picker Launcher (Supports both direct Per-Scene preview and Slot assignment modes)
        from src.apps.lock_app import LockScreenApp
        eff_key = db_key if db_key is not None else getattr(self, "_pick_db_key", None)
        eff_def = default_scene if default_scene is not None else getattr(self, "_pick_default_scene", cat_scenes[0])
        if start_scene and start_scene in cat_scenes:
            start_sc = start_scene
        else:
            cur_sc = self.db.get_setting(eff_key, eff_def) if eff_key else eff_def
            start_sc = cur_sc if cur_sc in cat_scenes else cat_scenes[0]
        viewer = LockScreenApp(
            self.display, self.ui,
            standalone_scene=start_sc,
            parent_app_id="settings",
            pick_db_key=eff_key,
            pick_slot_label=getattr(self, "_pick_slot_label", None),
            category_scenes=cat_scenes
        )
        await self.ui.switch_app("scene_preview", fallback_app=viewer)

    def _tune_hud_seq(self, direction: int = 1):
        """Steps Global Launch Intro sequence across Title Only ★, Title->Guide, Guide->Title, Title+Guide, Guide Only, and Off."""
        opts = ["title", "title_guide", "guide_title", "both", "guide", "off"]
        cur = self.db.get_setting("lock_hud_seq", "title")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_hud_seq", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_title_hold(self, direction: int = 1):
        """Steps Global Scene Title hold time across 1.5s, 3.0s ★, 5.0s, and Always."""
        opts = ["1.5", "3.0", "5.0", "always"]
        cur = self.db.get_setting("lock_title_sec", "3.0")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 1
        self.db.set_setting("lock_title_sec", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_guide_hold(self, direction: int = 1):
        """Steps Global Keypad Guide hold time across 3.0s, 4.5s ★, 8.0s, and Always."""
        opts = ["3.0", "4.5", "8.0", "always"]
        cur = self.db.get_setting("lock_guide_sec", "4.5")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 1
        self.db.set_setting("lock_guide_sec", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_hud_fx(self, direction: int = 1):
        """Steps HUD Banner Transition Effect across Bayer Dissolve ★, Curtain Slide, and Laser Wipe."""
        opts = ["bayer", "curtain", "laser"]
        cur = self.db.get_setting("lock_hud_fx", "bayer")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_hud_fx", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_hud_anchor(self, direction: int = 1):
        """Steps Global Scene Label Anchor across Left ★, Center, and Right."""
        opts = ["left", "center", "right"]
        cur = self.db.get_setting("lock_hud_anchor", "left")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_hud_anchor", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    # Stage: Global & Per-Scene Visualizer Mode & Band Count Controls (Mono / Stereo / Sine Wave / Stereo Wave)
    def _tune_viz_stereo_mode(self, direction: int = 1):
        """Steps Global Visualizer Mode across Mono RTA ★, Stereo L|R, Sine Wave, and Stereo Wave."""
        opts = ["mono", "stereo", "sine_wave", "stereo_wave"]
        cur = self.db.get_setting("lock_viz_stereo", "mono")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_viz_stereo", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_viz_bands(self, direction: int = 1):
        """Steps Global Visualizer Band / Harmonic Count across 20 ★, 16, 12, 10, 8, and 5."""
        opts = ["20", "16", "12", "10", "8", "5"]
        cur = self.db.get_setting("lock_viz_bands", "20")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_viz_bands", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_viz_curve(self, direction: int = 1):
        """Steps Global 20kHz Spectrum Weighting Curve between Pink Flat +3dB ★ ('pink') and Raw RTA 0dB ('raw')."""
        opts = ["pink", "raw"]
        cur = self.db.get_setting("lock_viz_curve", "pink")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_viz_curve", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_viz_bar_style(self, direction: int = 1):
        """Steps Global Visualizer Bar Style across Solid ★, Segmented LED, PWM Shaded, and Hollow Outline."""
        opts = ["solid", "led", "shaded", "hollow"]
        cur = self.db.get_setting("lock_viz_bar_style", "solid")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_viz_bar_style", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_viz_cap_style(self, direction: int = 1):
        """Steps Global Floating Peak Cap Style across PWM Dim ★ ('pwm'), Bright Solid ('solid'), 2px Dot ('dot'), and Off ('off')."""
        opts = ["pwm", "faint", "trail", "solid", "dot", "off"]
        cur = self.db.get_setting("lock_viz_cap_style", "pwm")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_viz_cap_style", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_viz_cap_decay(self, direction: int = 1):
        """Steps Master Particle Physics Preset across Zero-G Float ★ ('float'), Studio ('studio'), Feather ('feather'), Heavy ('heavy'), and Custom ('custom')."""
        opts = ["float", "studio", "feather", "heavy", "custom"]
        cur = self.db.get_setting("lock_viz_preset", "float")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_viz_preset", opts[idx])
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_phys_hold(self, direction: int = 1):
        """Steps Particle Apex Hold duration and automatically switches Physics Preset to 'custom'."""
        opts = ["150", "300", "450", "700", "1000"]
        cur = self.db.get_setting("lock_viz_hold_ms", "450")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 2
        self.db.set_setting("lock_viz_hold_ms", opts[idx])
        self.db.set_setting("lock_viz_preset", "custom")
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_phys_curve(self, direction: int = 1):
        """Steps Particle Downward Acceleration Curve across Quadratic t² ★ ('quad'), Cubic t³ ('cubic'), and Linear ('linear')."""
        opts = ["quad", "cubic", "linear"]
        cur = self.db.get_setting("lock_viz_accel_curve", "quad")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_viz_accel_curve", opts[idx])
        self.db.set_setting("lock_viz_preset", "custom")
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _tune_phys_speed(self, direction: int = 1):
        """Steps Particle Terminal Fall Speed across Slow ★ ('slow'), Medium ('med'), and Fast ('fast')."""
        opts = ["slow", "med", "fast"]
        cur = self.db.get_setting("lock_viz_term_spd", "slow")
        idx = (opts.index(cur) + direction) % len(opts) if cur in opts else 0
        self.db.set_setting("lock_viz_term_spd", opts[idx])
        self.db.set_setting("lock_viz_preset", "custom")
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_stereo_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene Visualizer Mode across Use Global ★, Mono RTA, Stereo L|R, Sine Wave, and Stereo Wave."""
        opts = ["global", "mono", "stereo", "sine_wave", "stereo_wave"]
        cur = self.db.get_setting(f"scene_stereo_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_stereo_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_bands_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene Band / Harmonic Count across Use Global ★, 20, 16, 12, 10, 8, and 5."""
        opts = ["global", "20", "16", "12", "10", "8", "5"]
        cur = self.db.get_setting(f"scene_bands_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_bands_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_curve_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene Spectrum Curve across Use Global ★, Pink Flat +3dB, and Raw RTA 0dB."""
        opts = ["global", "pink", "raw"]
        cur = self.db.get_setting(f"scene_curve_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_curve_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_bar_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene Bar Style across Use Global ★, Solid, Segmented LED, PWM Shaded, and Hollow."""
        opts = ["global", "solid", "led", "shaded", "hollow"]
        cur = self.db.get_setting(f"scene_bar_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_bar_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_cap_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene Peak Cap Style across Use Global ★, PWM Dim, Bright Solid, 2px Dot, and Off."""
        opts = ["global", "pwm", "faint", "trail", "solid", "dot", "off"]
        cur = self.db.get_setting(f"scene_cap_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_cap_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_decay_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene Particle Physics Preset across Use Global ★, Zero-G Float, Studio, Feather, and Heavy."""
        opts = ["global", "float", "studio", "feather", "heavy"]
        cur = self.db.get_setting(f"scene_decay_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_decay_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_hud_behavior(self, scene_id: str, direction: int = 1):
        """Cycles simplified per-scene HUD Behavior across Use Global ★, Title Intro, Pin Guide, and Clean."""
        opts = ["global", "title", "pin_guide", "clean"]
        cur = self.db.get_setting(f"scene_hud_beh_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_hud_beh_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_anchor_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene Label Anchor across Use Global ★, Left, Center, and Right."""
        opts = ["global", "left", "center", "right"]
        cur = self.db.get_setting(f"scene_anchor_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_anchor_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_seq_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene Launch Intro sequence across Use Global ★, Title Only, Title->Guide, Guide->Title, Title+Guide, Guide Only, Off."""
        opts = ["global", "title", "title_guide", "guide_title", "both", "guide", "off"]
        cur = self.db.get_setting(f"scene_seq_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_seq_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_title_sec_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene Title Hold duration across Use Global ★, 1.5s, 3.0s, 5.0s, Always."""
        opts = ["global", "1.5", "3.0", "5.0", "always"]
        cur = self.db.get_setting(f"scene_t_sec_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_t_sec_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    def _cycle_scene_guide_sec_override(self, scene_id: str, direction: int = 1):
        """Cycles per-scene Guide Hold duration across Use Global ★, 3.0s, 4.5s, 8.0s, Always."""
        opts = ["global", "3.0", "4.5", "8.0", "always"]
        cur = self.db.get_setting(f"scene_g_sec_{scene_id}", "global")
        nxt = opts[(opts.index(cur) + direction) % len(opts)] if cur in opts else "global"
        self.db.set_setting(f"scene_g_sec_{scene_id}", nxt)
        self._rebuild_menu(preserve_cursor=True)
        self.ui.request_render()

    async def _launch_scene_viewer(self, scene_id: str):
        """Launches the standalone Scene Cartridge viewer in-app from Settings."""
        from src.apps.lock_app import LockScreenApp
        viewer = LockScreenApp(self.display, self.ui, standalone_scene=scene_id, parent_app_id="settings")
        await self.ui.switch_app("scene_preview", fallback_app=viewer)

    def _clear_mv_caches(self):
        # MV Cache Purge Action (Removes all pre-cached .bin and .meta.json files from the MV cache directory)
        import os
        import glob
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scenes", "music", "bad_apple"))
        removed = 0
        for pat in (os.path.join(base_dir, "cache", "*"), os.path.join(base_dir, "*.bin"), os.path.join(base_dir, "frames_meta.json")):
            for fp in glob.glob(pat):
                if os.path.isfile(fp):
                    try:
                        os.remove(fp)
                        removed += 1
                    except Exception:
                        pass
        self.show_info_toast(f"Purged {removed} cache files")

    async def _refresh_hw_diag(self, toast: bool = True):
        """Queries live I2C0 PCM5122 DAC registers and hardware subsystems."""
        self._hw_dac_status = await self.ui.read_pcm5122_status()
        if self.current_menu == "hw_diag":
            self._rebuild_menu(preserve_cursor=True)
            if toast:
                self.show_info_toast(f"DAC: {self._hw_dac_status}", duration=1.8)
            self.ui.request_render()

    async def _purge_lightdm_and_claim_audio(self):
        """Masks LightDM systemd audio units, kills UID 109 ghosts, disables Xorg greeter, and restarts User 1000 PipeWire."""
        self.show_info_toast("Purging LightDM...", duration=2.0)
        cmd = (
            "sudo mkdir -p /var/lib/lightdm/.config/systemd/user && "
            "sudo ln -sf /dev/null /var/lib/lightdm/.config/systemd/user/pipewire.service && "
            "sudo ln -sf /dev/null /var/lib/lightdm/.config/systemd/user/pipewire.socket && "
            "sudo ln -sf /dev/null /var/lib/lightdm/.config/systemd/user/wireplumber.service && "
            "sudo ln -sf /dev/null /var/lib/lightdm/.config/systemd/user/pipewire-pulse.service && "
            "sudo ln -sf /dev/null /var/lib/lightdm/.config/systemd/user/pipewire-pulse.socket && "
            "sudo pkill -9 -u 109 2>/dev/null || true; "
            "sudo pkill -9 -u lightdm 2>/dev/null || true; "
            "sudo loginctl terminate-user lightdm 2>/dev/null || true; "
            "sudo systemctl disable --now lightdm 2>/dev/null || true; "
            "sudo systemctl set-default multi-user.target 2>/dev/null || true; "
            "sudo modprobe -r pcspkr snd_pcsp 2>/dev/null || true; "
            "sudo setterm -blength 0 2>/dev/null || true; "
            "sudo -u vipxpert env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart pipewire wireplumber pipewire-pulse"
        )
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        kernel = getattr(self.ui, 'os_kernel', None)
        if kernel and hasattr(kernel, '_enforce_audio_routing'):
            await asyncio.sleep(1.0)
            await kernel._enforce_audio_routing()
        self.show_info_toast("LightDM Purged!", duration=2.0)
        self.ui.request_render()

    async def _purge_dev_ram(self):
        """Kills lingering VS Code Remote Server / Copilot processes and clears temporary WAV files from /tmp RAM-disk."""
        self.show_info_toast("Freeing Dev RAM...", duration=1.8)
        cmd = "pkill -9 -f '\\.vscode-server|copilot-runtime' 2>/dev/null || true; rm -f /tmp/*.wav 2>/dev/null || true"
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        self.show_info_toast("Dev RAM Freed!", duration=2.0)
        self.ui.request_render()

    def _get_desktop_state(self):
        """Returns (session_active: bool, boot_graphical: bool, profile_str: str, gpu_ok: bool)."""
        import os, subprocess
        res_act = subprocess.run(["systemctl", "is-active", "lightdm"], stdout=subprocess.PIPE, text=True, check=False)
        session_on = (res_act.stdout.strip() == "active")
        res_def = subprocess.run(["systemctl", "get-default"], stdout=subprocess.PIPE, text=True, check=False)
        boot_gui = ("graphical" in res_def.stdout.strip())
        prof = "1280x720_30.00 65"
        if os.path.exists("/home/vipxpert/.last_display_profile"):
            try:
                with open("/home/vipxpert/.last_display_profile", "r") as f:
                    prof = f.read().strip() or prof
            except Exception:
                pass
        gpu_ok = os.path.exists("/etc/X11/xorg.conf")
        return session_on, boot_gui, prof, gpu_ok

    async def _toggle_desktop_session(self):
        """Starts or stops Desktop GUI (lightdm) + x11vnc on-demand while keeping LightDM UID 109 audio masked."""
        session_on, _, _, _ = self._get_desktop_state()
        if session_on:
            self.show_info_toast("Stopping Desktop...", duration=2.0)
            cmd = (
                "sudo systemctl isolate multi-user.target 2>/dev/null || true; "
                "sudo systemctl stop lightdm 2>/dev/null || true; "
                "sudo killall x11vnc 2>/dev/null || true; "
                "sudo loginctl terminate-user lightdm 2>/dev/null || true"
            )
            toast_done = "CLI Mode Active"
        else:
            self.show_info_toast("Starting Desktop...", duration=2.5)
            cmd = (
                "sudo mkdir -p /var/lib/lightdm/.config/systemd/user && "
                "for s in pipewire.service pipewire.socket wireplumber.service pipewire-pulse.service pipewire-pulse.socket; do "
                "sudo ln -sf /dev/null /var/lib/lightdm/.config/systemd/user/$s; done; "
                "sudo systemctl isolate graphical.target 2>/dev/null || true; "
                "sudo systemctl start lightdm 2>/dev/null || true; "
                "sleep 1.5; "
                "if [ -x /usr/bin/x11vnc ]; then "
                "AUTH_ARG=''; [ -f /home/vipxpert/.vnc/passwd ] && AUTH_ARG='-rfbauth /home/vipxpert/.vnc/passwd'; "
                "sudo x11vnc -display :0 -auth guess -forever -loop -repeat -xdamage $AUTH_ARG -rfbport 5900 -shared -bg 2>/dev/null || true; "
                "fi"
            )
            toast_done = "Desktop + VNC On!"
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        self._rebuild_menu(preserve_cursor=True)
        self.show_info_toast(toast_done, duration=2.0)
        self.ui.request_render()

    async def _toggle_desktop_boot_target(self):
        """Toggles default systemd boot target between multi-user.target (CLI) and graphical.target (Desktop)."""
        _, boot_gui, _, _ = self._get_desktop_state()
        target = "multi-user.target" if boot_gui else "graphical.target"
        label = "Boot: CLI ★" if boot_gui else "Boot: Desktop"
        proc = await asyncio.create_subprocess_shell(f"sudo systemctl set-default {target}", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        self._rebuild_menu(preserve_cursor=True)
        self.show_info_toast(label, duration=1.8)
        self.ui.request_render()

    def _parse_display_profile(self):
        """Parses ~/.last_display_profile into (res, fps, dpi) strings."""
        _, _, prof, _ = self._get_desktop_state()
        parts = prof.split()
        mode_part = parts[0] if parts else "1280x720_30.00"
        dpi_str = parts[1] if len(parts) > 1 else "65"
        if "_" in mode_part:
            res_str, fps_raw = mode_part.split("_", 1)
            fps_str = fps_raw.split(".")[0]
        else:
            res_str, fps_str = "1280x720", "30"
        return res_str, fps_str, dpi_str

    async def _apply_display_components(self, res_str: str, fps_str: str, dpi_str: str):
        """Applies global X11/VNC resolution, refresh rate, and DPI via ~/set_display.sh."""
        arg = f"{res_str}_{fps_str}.00 {dpi_str}"
        proc = await asyncio.create_subprocess_shell(
            f"sudo -u vipxpert /home/vipxpert/set_display.sh {arg}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
        self._rebuild_menu(preserve_cursor=True)
        self.show_info_toast(f"{res_str} @{fps_str}Hz ({dpi_str}dpi)", duration=1.8)
        self.ui.request_render()

    def _tune_display_resolution(self, direction: int = 1):
        """Slider callback stepping global X11/VNC resolution while preserving active refresh rate and DPI."""
        res_list = ["854x480", "1024x768", "1280x720", "1280x960", "1600x1200", "1920x1080", "2560x1440"]
        res_s, fps_s, dpi_s = self._parse_display_profile()
        idx = res_list.index(res_s) if res_s in res_list else 2
        nxt_res = res_list[(idx + direction) % len(res_list)]
        asyncio.create_task(self._apply_display_components(nxt_res, fps_s, dpi_s))

    def _tune_display_refresh_rate(self, direction: int = 1):
        """Slider callback stepping global X11/VNC refresh rate (30/60/90/120Hz) while preserving resolution and DPI."""
        fps_list = ["30", "60", "90", "120"]
        res_s, fps_s, dpi_s = self._parse_display_profile()
        idx = fps_list.index(fps_s) if fps_s in fps_list else 0
        nxt_fps = fps_list[(idx + direction) % len(fps_list)]
        asyncio.create_task(self._apply_display_components(res_s, nxt_fps, dpi_s))

    def _tune_display_dpi(self, direction: int = 1):
        """Slider callback stepping global X11/VNC Xft DPI scaling while preserving active resolution and refresh rate."""
        dpi_list = ["60", "65", "75", "85", "96", "120"]
        res_s, fps_s, dpi_s = self._parse_display_profile()
        idx = dpi_list.index(dpi_s) if dpi_s in dpi_list else 1
        nxt_dpi = dpi_list[(idx + direction) % len(dpi_list)]
        asyncio.create_task(self._apply_display_components(res_s, fps_s, nxt_dpi))

    def _rebuild_menu(self, preserve_cursor: bool = True):
        t_lock = int(self.db.get_setting("timeout_lock", "30"))
        t_disp = int(self.db.get_setting("timeout_display", "60"))
        bright = int(self.db.get_setting("brightness", "128"))
        invert = self.db.get_setting("invert_display", "0") == "1"
        clk_fmt = self.db.get_setting("clock_format", "24h")
        spd = str(self.db.get_setting("marquee_speed", "3.0"))
        
        lock_str = f"{t_lock}s ★" if t_lock == 30 else (f"{t_lock}s" if t_lock > 0 else "Never")
        disp_str = f"{t_disp}s ★" if t_disp == 60 else (f"{t_disp}s" if t_disp > 0 else "Never")
        bright_pct = f"{int((bright / 255) * 100)}%"
        invert_str = "Inverted" if invert else "Normal ★"
        clk_str = "12-Hour" if clk_fmt == "12h" else "24-Hour ★"
        spd_map = {"2.0": "Slow", "3.0": "Normal ★", "4.5": "Fast", "6.0": "Turbo", "10.0": "60px/s"}
        spd_str = spd_map.get(spd, "Normal (3x) ★")

        self.layout.header_right_cb = self.ui.draw_top_right_battery

        if self.current_menu == "display_ui":
            self.title = "Display & UI"
            min_on = self.db.get_setting("ui_minimal_mode", "0") == "1"
            min_lbl = "On (Min)" if min_on else "Off (Mod) ★"
            items = [
                {
                    "name": "Minimal Mode",
                    "suffix": min_lbl,
                    "action": lambda: self._cycle_db_opts("ui_minimal_mode", ["0", "1"], "0", 1),
                    "on_tune": lambda d: self._cycle_db_opts("ui_minimal_mode", ["0", "1"], "0", d)
                },
                {
                    "name": "Animations & UX",
                    "end_indicator": ">",
                    "action": lambda: self._open_submenu("ui_ux_cfg")
                },
                {
                    "name": "Panel & Drive",
                    "end_indicator": ">",
                    "action": lambda: self._open_submenu("panel_cfg")
                },
                {
                    "name": "Gray & PWM",
                    "end_indicator": ">",
                    "action": lambda: self._open_submenu("gray_cfg")
                },
            ]
        elif self.current_menu == "ui_ux_cfg":
            self.title = "Animations & UX"
            ln_style = self.db.get_setting("ui_launcher_style", "modern")
            ln_lbl = "Minimal List" if ln_style == "minimal" else "Modern Dock ★"
            rc_style = self.db.get_setting("ui_recent_style", "modern")
            rc_lbl = "Classic List" if rc_style == "minimal" else "Live Cards ★"
            tr_fx = self.db.get_setting("ui_transition_fx", "iris")
            tr_lbl = {"iris": "Portal ★", "bayer": "Bayer", "curtain": "Curtain", "off": "Off"}.get(tr_fx, "Portal ★")
            scn_scr = self.db.get_setting("ui_scene_scroll", "1") != "0"
            scn_lbl = "Slide ★" if scn_scr else "Instant"
            sm_scr = self.db.get_setting("ui_smooth_scroll", "1") != "0"
            sm_lbl = "Smooth ★" if sm_scr else "Instant"
            items = [
                {
                    "name": "App Launcher",
                    "suffix": ln_lbl,
                    "action": lambda: self._cycle_db_opts("ui_launcher_style", ["modern", "minimal"], "modern", 1),
                    "on_tune": lambda d: self._cycle_db_opts("ui_launcher_style", ["modern", "minimal"], "modern", d)
                },
                {
                    "name": "Recent Style",
                    "suffix": rc_lbl,
                    "action": lambda: self._cycle_db_opts("ui_recent_style", ["modern", "minimal"], "modern", 1),
                    "on_tune": lambda d: self._cycle_db_opts("ui_recent_style", ["modern", "minimal"], "modern", d)
                },
                {
                    "name": "Screen Trans",
                    "suffix": tr_lbl,
                    "action": lambda: self._cycle_db_opts("ui_transition_fx", ["iris", "bayer", "curtain", "off"], "iris", 1),
                    "on_tune": lambda d: self._cycle_db_opts("ui_transition_fx", ["iris", "bayer", "curtain", "off"], "iris", d)
                },
                {
                    "name": "Scene Scroll",
                    "suffix": scn_lbl,
                    "action": lambda: self._cycle_db_opts("ui_scene_scroll", ["1", "0"], "1", 1),
                    "on_tune": lambda d: self._cycle_db_opts("ui_scene_scroll", ["1", "0"], "1", d)
                },
                {
                    "name": "Menu Scroll",
                    "suffix": sm_lbl,
                    "action": lambda: self._cycle_db_opts("ui_smooth_scroll", ["1", "0"], "1", 1),
                    "on_tune": lambda d: self._cycle_db_opts("ui_smooth_scroll", ["1", "0"], "1", d)
                },
                {
                    "name": "Scroll Speed",
                    "suffix": spd_str,
                    "on_tune": self._tune_marquee_speed
                }
            ]
        elif self.current_menu == "panel_cfg":
            self.title = "Panel & Drive"
            items = [
                {
                    "name": "Brightness & Drive",
                    "end_indicator": ">",
                    "action": self._open_disp_calibrator
                },
                {
                    "name": "OLED Refresh",
                    "suffix": f"{int(self.db.get_setting('oled_fps', '20'))}Hz ★" if int(self.db.get_setting('oled_fps', '20')) == 20 else f"{int(self.db.get_setting('oled_fps', '20'))}Hz",
                    "on_tune": self._tune_oled_fps
                },
                {
                    "name": "Clock Format",
                    "suffix": clk_str,
                    "action": self._toggle_clock_format
                },
            ]
        elif self.current_menu == "gray_cfg":
            self.title = "Gray & PWM"
            gm = self.db.get_setting("oled_gray_mode", getattr(self.display, "gray_mode", "hybrid"))
            gm_lbl = {"hybrid": "Hybrid ★", "pwm": "Pure PWM", "dithering": "Dither", "off": "1-Bit"}.get(gm, "Hybrid ★")
            alg = self.db.get_setting("oled_dither_algo", getattr(self.display, "dither_algo", "bayer8"))
            lo_v = int(self.db.get_setting("oled_dither_lo", str(getattr(self.display, "dither_lo", 42))))
            hi_v = int(self.db.get_setting("oled_dither_hi", str(getattr(self.display, "dither_hi", 70))))
            pwm_ms = float(self.db.get_setting("oled_pwm_dt", str(getattr(self.display, "_pwm_base_dt", 0.006451948)))) * 1e3
            t0 = int(self.db.get_setting("oled_pwm_t0", str(getattr(self.display, "pwm_t0", 48))))
            t1 = int(self.db.get_setting("oled_pwm_t1", str(getattr(self.display, "pwm_t1", 128))))
            t2 = int(self.db.get_setting("oled_pwm_t2", str(getattr(self.display, "pwm_t2", 208))))
            items = [
                {"name": "Gray Mode", "suffix": gm_lbl, "end_indicator": ">", "action": self._open_gray_mode_calibrator},
                {"name": "Dither & Calib", "suffix": alg, "end_indicator": ">", "action": self._open_gray_calibrator},
                {"name": "Gradient Mapping", "suffix": f"{t0}/{t1}/{t2}", "end_indicator": ">", "action": self._open_tone_calibrator},
                {"name": "PWM Sync Cal", "suffix": f"{pwm_ms:.2f}ms", "end_indicator": ">", "action": self._open_pwm_calibrator},
            ]
        elif self.current_menu == "hw_diag":
            self.title = "Hardware Info"
            oled_bus = getattr(self.display, 'port', 0)
            oled_ok = f"I2C{oled_bus} 0x3C OK" if getattr(self.display, 'device', None) else "Headless"
            pwr = getattr(self.ui, 'power_state', {})
            ups_str = f"{pwr.get('voltage', 0.0):.2f}V {pwr.get('current', 0.0):.0f}mA" if not pwr.get('error', True) else "Offline"
            drv_lvl = int(self.db.get_setting("i2s_drive_level", "2"))
            drv_ma = f"{(drv_lvl + 1) * 10}mA (L{drv_lvl})"
            pmic_mode = str(self.db.get_setting("pmic_power_mode", "2"))
            pmic_lbl = {"0": "Stock PFM", "1": "DVM Smooth", "2": "Fixed PWM", "3": "Spread PWM"}.get(pmic_mode, "Fixed PWM")
            kernel = getattr(self.ui, 'os_kernel', None)
            ir_hw = getattr(kernel, 'ir_hardware', None) if kernel else None
            ir_ok = "TX+RX Ready" if (ir_hw and ir_hw.is_ready()) else "Probing/Off"
            hwp = getattr(self.ui, 'hw_profile', {}) or {}
            tier_s = f"{str(hwp.get('tier', 'full')).capitalize()} ({hwp.get('cores', 4)}C)"

            items = [
                {"name": "Board Profile", "suffix": tier_s, "action": lambda: self.show_info_toast(f"DSP:{hwp.get('dsp_us', 0)}us Pack:{hwp.get('pack_us', 0)}us")},
                {"name": "USB Power Scope", "suffix": "Open >", "action": lambda: asyncio.create_task(self._launch_scene_viewer("power_scope"))},
                {"name": "Ultrasonic Scope", "suffix": "Open >", "action": lambda: asyncio.create_task(self._launch_scene_viewer("mic_ultrasonic"))},
                {"name": "IR Logic Scope", "suffix": "Open >", "action": lambda: asyncio.create_task(self._launch_scene_viewer("ir_scope"))},
                {"name": "BLE Tracker Radar", "suffix": "Open >", "action": lambda: asyncio.create_task(self._launch_scene_viewer("ble_radar"))},
                {"name": "OLED (Pin3/5)", "suffix": oled_ok, "action": lambda: None},
                {"name": "DAC (0x4C)", "suffix": self._hw_dac_status, "action": lambda: asyncio.create_task(self._refresh_hw_diag(toast=True))},
                {"name": "PMIC (0x36)", "suffix": pmic_lbl, "action": lambda: None},
                {"name": "I2S BCK/WS", "suffix": f"Pin12/35 {drv_ma}", "action": lambda: None},
                {"name": "I2S DIN/DAT", "suffix": "Pin40/38 10mA", "action": lambda: None},
                {"name": "Amp MAX97220", "suffix": "Pin4 5V/Pin25*", "action": lambda: None},
                {"name": "IR Pin31/33", "suffix": ir_ok, "action": lambda: None},
                {"name": "Purge LightDM", "suffix": "Run >", "action": lambda: asyncio.create_task(self._purge_lightdm_and_claim_audio())},
                {"name": "Free Dev RAM", "suffix": "Run >", "action": lambda: asyncio.create_task(self._purge_dev_ram())},
                {"name": "Refresh Probe", "suffix": "Run >", "action": lambda: asyncio.create_task(self._refresh_hw_diag(toast=True))}
            ]
        elif self.current_menu == "lock_cfg":
            from src.ui.scene_manager import SCENE_CATALOG, SceneManager
            if not SCENE_CATALOG:
                SceneManager.discover()
            self.title = "Lock & Scenes"
            pr_c = self.db.get_setting("lock_viz_preset", "float")
            pr_tag = {"float": "Zero-G ★", "studio": "Studio", "feather": "Feather", "heavy": "Heavy", "custom": "Custom"}.get(pr_c, "Zero-G ★")
            fx_c = self.db.get_setting("lock_hud_fx", "bayer")
            fx_s = {"bayer": "Bayer ★", "curtain": "Curtain", "laser": "Laser"}.get(fx_c, "Bayer ★")
            sav_s = int(self.db.get_setting("lock_ambient_idle_sec", "30"))
            items = [
                {"name": "Scene Slots", "suffix": "5 Slots >", "action": lambda: self._enter_submenu("lock_slots")},
                {"name": "Visualizer Style", "suffix": f"{pr_tag} >", "action": lambda: self._enter_submenu("lock_viz_cfg")},
                {"name": "Music Video (MV)", "suffix": "Config >", "action": lambda: self._enter_submenu("lock_mv_cfg")},
                {"name": "Per-Scene Setup", "suffix": f"{len(SCENE_CATALOG)} Scenes >", "action": lambda: self._enter_submenu("lock_scn_list")},
                {"name": "Global HUD & FX", "suffix": f"{fx_s} >", "action": lambda: self._enter_submenu("lock_hud_fx")},
                {"name": "Timers & Rules", "suffix": f"{sav_s}s / Idle >", "action": lambda: self._enter_submenu("lock_timers")},
                {"name": "Enable Slots", "suffix": "Configure >", "action": lambda: self._enter_submenu("lock_enables")},
            ]
        elif self.current_menu == "lock_mv_cfg":
            self.title = "Music Video (MV)"
            mv_on = self.db.get_setting("mv_autoplay_enabled", "1") == "1"
            match_m = self.db.get_setting("mv_match_mode", "smart")
            match_lbl = {"smart": "Smart Match ★", "exact": "Exact Title"}.get(match_m, "Smart Match ★")
            pre_on = self.db.get_setting("mv_boot_precache", "1") == "1"
            items = [
                {"name": "Auto-Play MV", "suffix": "Enabled ★" if mv_on else "Disabled", "action": lambda: self._toggle_lock_flag("mv_autoplay_enabled", "1")},
                {"name": "Match Mode", "suffix": match_lbl, "action": lambda: self._cycle_db_opts("mv_match_mode", ["smart", "exact"], "smart")},
                {"name": "Boot Pre-Cache", "suffix": "Enabled ★" if pre_on else "Disabled", "action": lambda: self._toggle_lock_flag("mv_boot_precache", "1")},
                {"name": "Clear MV Caches", "suffix": "Run >", "action": lambda: self._clear_mv_caches()},
            ]
        elif self.current_menu == "lock_viz_cfg":
            self.title = "Visualizer Style"
            st_c = self.db.get_setting("lock_viz_stereo", "mono")
            st_lbl = {
                "mono": "Mono RTA ★",
                "stereo": "Stereo L|R",
                "sine_wave": "Sine Wave",
                "stereo_wave": "Stereo Wave",
            }.get(st_c, "Mono RTA ★")
            bd_c = self.db.get_setting("lock_viz_bands", "20")
            bd_lbl = f"{bd_c} Bands ★" if bd_c == "20" else f"{bd_c} Bands"
            cv_c = self.db.get_setting("lock_viz_curve", "pink")
            cv_lbl = {"pink": "Pink +3dB ★", "raw": "Raw RTA 0dB"}.get(cv_c, "Pink +3dB ★")
            bar_c = self.db.get_setting("lock_viz_bar_style", "solid")
            bar_lbl = {"solid": "Solid ★", "led": "Segmented LED", "shaded": "Bayer Shaded", "hollow": "Hollow"}.get(bar_c, "Solid ★")
            cap_c = self.db.get_setting("lock_viz_cap_style", "pwm")
            cap_lbl = {"pwm": "Faded Gray ★", "faint": "Faint Gray", "trail": "Ember Trail", "solid": "Bright Solid", "dot": "2px Dot", "off": "Off"}.get(cap_c, "Faded Gray ★")
            pr_c = self.db.get_setting("lock_viz_preset", "float")
            pr_lbl = {"float": "Zero-G Float ★", "studio": "Studio RTA", "feather": "Feather Drift", "heavy": "Heavy Gravity", "custom": "Custom"}.get(pr_c, "Zero-G Float ★")
            items = [
                {"name": "Live Preview", "suffix": "Test Audio >", "action": lambda: asyncio.create_task(self._launch_category_picker(["music_split", "music_viz", "ambient_hud", "amb_music_hybrid"], None, "music_split"))},
                {"name": "Viz Mode (0)", "suffix": st_lbl, "action": lambda: self._tune_viz_stereo_mode(1), "on_tune": self._tune_viz_stereo_mode},
                {"name": "Band Count (3)", "suffix": bd_lbl, "action": lambda: self._tune_viz_bands(1), "on_tune": self._tune_viz_bands},
                {"name": "Physics Preset", "suffix": pr_lbl, "action": lambda: self._tune_viz_cap_decay(1), "on_tune": self._tune_viz_cap_decay},
                {"name": "Particle Physics", "suffix": "Fine-Tune >", "action": lambda: self._open_submenu("lock_viz_phys")},
                {"name": "Spectrum Curve", "suffix": cv_lbl, "action": lambda: self._tune_viz_curve(1), "on_tune": self._tune_viz_curve},
                {"name": "Bar Style", "suffix": bar_lbl, "action": lambda: self._tune_viz_bar_style(1), "on_tune": self._tune_viz_bar_style},
                {"name": "Peak Caps", "suffix": cap_lbl, "action": lambda: self._tune_viz_cap_style(1), "on_tune": self._tune_viz_cap_style},
            ]
        elif self.current_menu == "lock_viz_phys":
            self.title = "Particle Physics"
            pr_c = self.db.get_setting("lock_viz_preset", "float")
            pr_lbl = {"float": "Zero-G Float ★", "studio": "Studio RTA", "feather": "Feather Drift", "heavy": "Heavy Gravity", "custom": "Custom"}.get(pr_c, "Zero-G Float ★")
            hm_c = self.db.get_setting("lock_viz_hold_ms", "450")
            hm_lbl = f"{hm_c}ms ★" if hm_c == "450" else f"{hm_c}ms"
            ac_c = self.db.get_setting("lock_viz_accel_curve", "quad")
            ac_lbl = {"quad": "Quad t² ★", "cubic": "Cubic t³", "linear": "Linear"}.get(ac_c, "Quad t² ★")
            sp_c = self.db.get_setting("lock_viz_term_spd", "slow")
            sp_lbl = {"slow": "Slow ★", "med": "Medium", "fast": "Fast"}.get(sp_c, "Slow ★")
            items = [
                {"name": "Active Preset", "suffix": pr_lbl, "action": lambda: self._tune_viz_cap_decay(1), "on_tune": self._tune_viz_cap_decay},
                {"name": "Apex Hold", "suffix": hm_lbl, "action": lambda: self._tune_phys_hold(1), "on_tune": self._tune_phys_hold},
                {"name": "Fall Curve", "suffix": ac_lbl, "action": lambda: self._tune_phys_curve(1), "on_tune": self._tune_phys_curve},
                {"name": "Fall Speed", "suffix": sp_lbl, "action": lambda: self._tune_phys_speed(1), "on_tune": self._tune_phys_speed},
            ]
        elif self.current_menu == "lock_slots":
            from src.ui.scene_manager import SCENE_LABELS
            self.title = "Scene Slots"
            clk_sc = self.db.get_setting("lock_slot_clock_scene", "clock_digital")
            mus_sc = self.db.get_setting("lock_slot_music_scene", "music_split")
            amb_sc = self.db.get_setting("lock_slot_ambient_scene", "ambient_hud")
            hyb_sc = self.db.get_setting("lock_slot_amb_music_scene", "amb_music_hybrid")
            sav_sc = self.db.get_setting("lock_slot_saver_scene", "ambient_cube3d")
            # Stage: Slot -> Category List -> Category Scene List -> Live Preview Hierarchy
            items = [
                {"name": "Clock Slot", "suffix": f"{SCENE_LABELS.get(clk_sc, clk_sc)} >", "action": lambda: self._open_slot_categories("Clock Slot", "lock_slot_clock_scene", "clock_digital")},
                {"name": "Music Slot", "suffix": f"{SCENE_LABELS.get(mus_sc, mus_sc)} >", "action": lambda: self._open_slot_categories("Music Slot", "lock_slot_music_scene", "music_split")},
                {"name": "Ambient Slot", "suffix": f"{SCENE_LABELS.get(amb_sc, amb_sc)} >", "action": lambda: self._open_slot_categories("Ambient Slot", "lock_slot_ambient_scene", "ambient_hud")},
                {"name": "Amb+Music Slot", "suffix": f"{SCENE_LABELS.get(hyb_sc, hyb_sc)} >", "action": lambda: self._open_slot_categories("Amb+Music Slot", "lock_slot_amb_music_scene", "amb_music_hybrid")},
                {"name": "Screensaver", "suffix": f"{SCENE_LABELS.get(sav_sc, sav_sc)} >", "action": lambda: self._open_slot_categories("Screensaver", "lock_slot_saver_scene", "ambient_cube3d")},
            ]
        elif self.current_menu == "lock_cat":
            from src.ui.scene_manager import SCENE_CATEGORIES, SCENE_LABELS, SceneManager
            if not SCENE_CATEGORIES:
                SceneManager.discover()
            slot_lbl = getattr(self, "_pick_slot_label", "Select Category")
            db_key = getattr(self, "_pick_db_key", "lock_slot_clock_scene")
            dflt_sc = getattr(self, "_pick_default_scene", "clock_digital")
            cur_sc = self.db.get_setting(db_key, dflt_sc)
            self.title = f"{slot_lbl}: Cat"
            items = []
            for _cat_id, cat_title, cat_sids in SCENE_CATEGORIES:
                has_cur = cur_sc in cat_sids and _cat_id != "cat_all"
                sfx = f"{len(cat_sids)} ★ >" if has_cur else f"{len(cat_sids)} >"
                items.append({
                    "name": cat_title,
                    "suffix": sfx,
                    "action": lambda ct=cat_title, cs=list(cat_sids): self._open_category_scenes(ct, cs)
                })
        elif self.current_menu == "lock_cat_scenes":
            from src.ui.scene_manager import SCENE_LABELS, SceneManager
            if not SCENE_LABELS:
                SceneManager.discover()
            cat_title = getattr(self, "_pick_cat_title", "Scenes")
            cat_scenes = getattr(self, "_pick_cat_scenes", ["clock_digital"])
            db_key = getattr(self, "_pick_db_key", "lock_slot_clock_scene")
            dflt_sc = getattr(self, "_pick_default_scene", "clock_digital")
            cur_sc = self.db.get_setting(db_key, dflt_sc)
            self.title = cat_title[:16]
            items = []
            for sid in cat_scenes:
                sc_lbl = SCENE_LABELS.get(sid, sid)
                is_active = (sid == cur_sc)
                items.append({
                    "name": sc_lbl,
                    "suffix": "Active ★ >" if is_active else "Preview >",
                    "action": lambda s=sid, cs=list(cat_scenes), dk=db_key, df=dflt_sc: asyncio.create_task(self._launch_category_picker(cs, dk, df, start_scene=s))
                })
        elif self.current_menu == "lock_scn_list":
            from src.ui.scene_manager import SCENE_CATALOG, SceneManager
            if not SCENE_CATALOG:
                SceneManager.discover()
            self.title = "Per-Scene Setup"
            items = []
            for sc_id, sc_lbl in SCENE_CATALOG:
                tag = "Custom" if self._is_scene_customized(sc_id) else "Global ★"
                items.append({
                    "name": sc_lbl,
                    "suffix": f"{tag} >",
                    "action": lambda cid=sc_id, clbl=sc_lbl: self._open_scene_tuning_item(cid, clbl)
                })
        elif self.current_menu == "lock_scn_item":
            sc_id = getattr(self, "_tune_scene_id", "ambient_cube3d")
            sc_lbl = getattr(self, "_tune_scene_label", "Interactive 3D")
            self.title = sc_lbl
            beh_ov = self.db.get_setting(f"scene_hud_beh_{sc_id}", "global")
            beh_lbl = {"global": "Use Global ★", "title": "Title Intro", "pin_guide": "Pin Guide", "clean": "Clean Canvas"}.get(beh_ov, "Use Global ★")
            seq_ov = self.db.get_setting(f"scene_seq_{sc_id}", "global")
            seq_lbl = {"global": "Use Global ★", "title": "Title Only", "title_guide": "Title->Guide", "guide_title": "Guide->Title", "both": "Title+Guide", "guide": "Guide Only", "off": "Off"}.get(seq_ov, "Use Global ★")
            t_ov = self.db.get_setting(f"scene_t_sec_{sc_id}", "global")
            t_lbl = {"global": "Use Global ★", "1.5": "1.5s", "3.0": "3.0s", "5.0": "5.0s", "always": "Always On"}.get(t_ov, "Use Global ★")
            g_ov = self.db.get_setting(f"scene_g_sec_{sc_id}", "global")
            g_lbl = {"global": "Use Global ★", "3.0": "3.0s", "4.5": "4.5s", "8.0": "8.0s", "always": "Always On"}.get(g_ov, "Use Global ★")
            anc_ov = self.db.get_setting(f"scene_anchor_{sc_id}", "global")
            anc_lbl = {"global": "Use Global ★", "left": "Left", "center": "Center", "right": "Right"}.get(anc_ov, "Use Global ★")
            items = [
                {"name": "Live Preview", "suffix": "Open >", "action": lambda cid=sc_id: asyncio.create_task(self._launch_category_picker([cid], db_key=None, default_scene=cid, start_scene=cid))},
                {"name": "HUD Behavior", "suffix": beh_lbl, "action": lambda cid=sc_id: self._cycle_scene_hud_behavior(cid, 1), "on_tune": lambda d, cid=sc_id: self._cycle_scene_hud_behavior(cid, d)},
                {"name": "Launch Intro", "suffix": seq_lbl, "action": lambda cid=sc_id: self._cycle_scene_seq_override(cid, 1), "on_tune": lambda d, cid=sc_id: self._cycle_scene_seq_override(cid, d)},
                {"name": "Title Hold", "suffix": t_lbl, "action": lambda cid=sc_id: self._cycle_scene_title_sec_override(cid, 1), "on_tune": lambda d, cid=sc_id: self._cycle_scene_title_sec_override(cid, d)},
                {"name": "Guide Hold", "suffix": g_lbl, "action": lambda cid=sc_id: self._cycle_scene_guide_sec_override(cid, 1), "on_tune": lambda d, cid=sc_id: self._cycle_scene_guide_sec_override(cid, d)},
                {"name": "Label Anchor", "suffix": anc_lbl, "action": lambda cid=sc_id: self._cycle_scene_anchor_override(cid, 1), "on_tune": lambda d, cid=sc_id: self._cycle_scene_anchor_override(cid, d)},
            ]
            if sc_id in ("music_split", "music_viz", "ambient_hud", "amb_music_hybrid"):
                v_cust = any(
                    self.db.get_setting(f"{p}_{sc_id}", "global") != "global"
                    for p in ("scene_stereo", "scene_curve", "scene_bar", "scene_cap", "scene_decay")
                )
                v_tag = "Custom >" if v_cust else "Global ★ >"
                items.append({"name": "Visualizer", "suffix": v_tag, "action": lambda: self._enter_submenu("lock_scn_viz")})
            if sc_id in ("clock_digital", "clock_analog"):
                cf_key = f"scn_clk_fmt_{sc_id}"
                cf_val = self.db.get_setting(cf_key, "global")
                cf_lbl = {"global": "Use Global ★", "24h": "24-Hour", "12h": "12-Hour"}.get(cf_val, "Use Global ★")
                cs_key = f"scn_clk_sec_{sc_id}"
                cs_on = self.db.get_setting(cs_key, "1") == "1"
                items.append({"name": "Time Format", "suffix": cf_lbl, "action": lambda k=cf_key: self._cycle_db_opts(k, ["global", "24h", "12h"], "global", 1), "on_tune": lambda d, k=cf_key: self._cycle_db_opts(k, ["global", "24h", "12h"], "global", d)})
                items.append({"name": "Digital Secs", "suffix": "On ★" if cs_on else "Off", "action": lambda k=cs_key: self._cycle_db_opts(k, ["1", "0"], "1", 1), "on_tune": lambda d, k=cs_key: self._cycle_db_opts(k, ["1", "0"], "1", d)})
                if sc_id == "clock_digital":
                    dt_on = self.db.get_setting("scn_dig_date", "1") == "1"
                    items.append({"name": "Show Date", "suffix": "On ★" if dt_on else "Off", "action": lambda: self._cycle_db_opts("scn_dig_date", ["1", "0"], "1", 1), "on_tune": lambda d: self._cycle_db_opts("scn_dig_date", ["1", "0"], "1", d)})
                else:
                    sh_on = self.db.get_setting("scn_ana_sec", "1") == "1"
                    items.append({"name": "Second Hand", "suffix": "On ★" if sh_on else "Off", "action": lambda: self._cycle_db_opts("scn_ana_sec", ["1", "0"], "1", 1), "on_tune": lambda d: self._cycle_db_opts("scn_ana_sec", ["1", "0"], "1", d)})
            elif sc_id == "music_classic":
                mp_on = self.db.get_setting("scn_mcls_prog", "1") == "1"
                mf_val = self.db.get_setting("scn_mcls_foot", "marquee")
                mf_lbl = {"marquee": "Scroll Full ★", "dac_bat": "DAC + Bat", "clean": "Hidden"}.get(mf_val, "Scroll Full ★")
                items.append({"name": "Progress Bar", "suffix": "On ★" if mp_on else "Off", "action": lambda: self._cycle_db_opts("scn_mcls_prog", ["1", "0"], "1", 1), "on_tune": lambda d: self._cycle_db_opts("scn_mcls_prog", ["1", "0"], "1", d)})
                items.append({"name": "Footer Line", "suffix": mf_lbl, "action": lambda: self._cycle_db_opts("scn_mcls_foot", ["marquee", "dac_bat", "clean"], "marquee", 1), "on_tune": lambda d: self._cycle_db_opts("scn_mcls_foot", ["marquee", "dac_bat", "clean"], "marquee", d)})
            elif sc_id == "ambient_cube3d":
                m_val = self.db.get_setting("scn_3d_mesh", "0")
                a_val = self.db.get_setting("scn_3d_auto", "1")
                s_val = self.db.get_setting("scn_3d_scale", "20")
                items.append({"name": "Default Mesh", "suffix": "Octahedron" if m_val == "1" else "Cube ★", "action": lambda: self._cycle_db_opts("scn_3d_mesh", ["0", "1"], "0", 1), "on_tune": lambda d: self._cycle_db_opts("scn_3d_mesh", ["0", "1"], "0", d)})
                items.append({"name": "Auto-Rotate", "suffix": "On ★" if a_val == "1" else "Manual", "action": lambda: self._cycle_db_opts("scn_3d_auto", ["1", "0"], "1", 1), "on_tune": lambda d: self._cycle_db_opts("scn_3d_auto", ["1", "0"], "1", d)})
                items.append({"name": "Default Zoom", "suffix": f"{s_val}x ★" if s_val == "20" else f"{s_val}x", "action": lambda: self._cycle_db_opts("scn_3d_scale", ["16", "20", "24", "28"], "20", 1), "on_tune": lambda d: self._cycle_db_opts("scn_3d_scale", ["16", "20", "24", "28"], "20", d)})
            elif sc_id == "ambient_pond":
                pd_val = self.db.get_setting("scn_pond_drops", "5")
                pd_lbl = {"2": "Light (2)", "5": "Normal (5) ★", "8": "Heavy (8)"}.get(pd_val, f"{pd_val} Drops")
                ps_val = self.db.get_setting("scn_pond_spd", "1.85")
                ps_lbl = {"1.15": "Calm 1.15x", "1.85": "Lively ★", "2.5": "Rapid 2.5x"}.get(ps_val, f"{ps_val}x")
                items.append({"name": "Rain Density", "suffix": pd_lbl, "action": lambda: self._cycle_db_opts("scn_pond_drops", ["2", "5", "8"], "5", 1), "on_tune": lambda d: self._cycle_db_opts("scn_pond_drops", ["2", "5", "8"], "5", d)})
                items.append({"name": "Ripple Speed", "suffix": ps_lbl, "action": lambda: self._cycle_db_opts("scn_pond_spd", ["1.15", "1.85", "2.5"], "1.85", 1), "on_tune": lambda d: self._cycle_db_opts("scn_pond_spd", ["1.15", "1.85", "2.5"], "1.85", d)})
            elif sc_id == "ambient_stars":
                sp_val = self.db.get_setting("scn_star_spd", "0.85")
                items.append({"name": "Warp Speed", "suffix": f"{sp_val}x ★" if sp_val == "0.85" else f"{sp_val}x", "action": lambda: self._cycle_db_opts("scn_star_spd", ["0.5", "0.85", "1.5", "2.5"], "0.85", 1), "on_tune": lambda d: self._cycle_db_opts("scn_star_spd", ["0.5", "0.85", "1.5", "2.5"], "0.85", d)})
            elif sc_id == "ambient_grass":
                w_val = self.db.get_setting("scn_grass_wind", "1.35")
                h_val = self.db.get_setting("scn_grass_ht", "14")
                items.append({"name": "Breeze Strength", "suffix": f"{w_val}x ★" if w_val == "1.35" else f"{w_val}x", "action": lambda: self._cycle_db_opts("scn_grass_wind", ["0.6", "1.35", "2.0", "2.8"], "1.35", 1), "on_tune": lambda d: self._cycle_db_opts("scn_grass_wind", ["0.6", "1.35", "2.0", "2.8"], "1.35", d)})
                items.append({"name": "Blade Height", "suffix": f"{h_val}px ★" if h_val == "14" else f"{h_val}px", "action": lambda: self._cycle_db_opts("scn_grass_ht", ["10", "14", "20", "26"], "14", 1), "on_tune": lambda d: self._cycle_db_opts("scn_grass_ht", ["10", "14", "20", "26"], "14", d)})
            elif sc_id == "ambient_bounce":
                b_val = self.db.get_setting("scn_bnc_shape", "0")
                b_lbl = {"0": "VIP Badge ★", "1": "Diamond", "2": "Orb"}.get(b_val, "VIP Badge ★")
                bs_val = self.db.get_setting("scn_bnc_spd", "1.8")
                bs_lbl = {"1.2": "Chill 1.2x", "1.8": "Normal ★", "2.6": "Turbo 2.6x"}.get(bs_val, "Normal ★")
                items.append({"name": "Bounce Shape", "suffix": b_lbl, "action": lambda: self._cycle_db_opts("scn_bnc_shape", ["0", "1", "2"], "0", 1), "on_tune": lambda d: self._cycle_db_opts("scn_bnc_shape", ["0", "1", "2"], "0", d)})
                items.append({"name": "Bounce Speed", "suffix": bs_lbl, "action": lambda: self._cycle_db_opts("scn_bnc_spd", ["1.2", "1.8", "2.6"], "1.8", 1), "on_tune": lambda d: self._cycle_db_opts("scn_bnc_spd", ["1.2", "1.8", "2.6"], "1.8", d)})
            elif sc_id == "bad_apple":
                mv_on = self.db.get_setting("mv_autoplay_enabled", "1") == "1"
                match_m = self.db.get_setting("mv_match_mode", "smart")
                items.append({"name": "Auto-Play MV", "suffix": "Enabled ★" if mv_on else "Disabled", "action": lambda: self._toggle_lock_flag("mv_autoplay_enabled", "1"), "on_tune": lambda _d: self._toggle_lock_flag("mv_autoplay_enabled", "1")})
                items.append({"name": "Match Mode", "suffix": "Exact Title" if match_m == "exact" else "Smart Match ★", "action": lambda: self._cycle_db_opts("mv_match_mode", ["smart", "exact"], "smart", 1), "on_tune": lambda d: self._cycle_db_opts("mv_match_mode", ["smart", "exact"], "smart", d)})
            elif sc_id == "power_scope":
                pv_val = self.db.get_setting("scn_pwr_view", "0")
                pv_lbl = {"0": "Waveform ★", "1": "Cable Resistance", "2": "USB Delta Audit"}.get(pv_val, "Waveform ★")
                ps_st = self.db.get_setting("scn_pwr_stride", "1")
                items.append({"name": "Default View", "suffix": pv_lbl, "action": lambda: self._cycle_db_opts("scn_pwr_view", ["0", "1", "2"], "0", 1), "on_tune": lambda d: self._cycle_db_opts("scn_pwr_view", ["0", "1", "2"], "0", d)})
                items.append({"name": "Timebase", "suffix": f"{ps_st}x ★" if ps_st == "1" else f"{ps_st}x", "action": lambda: self._cycle_db_opts("scn_pwr_stride", ["1", "2", "4"], "1", 1), "on_tune": lambda d: self._cycle_db_opts("scn_pwr_stride", ["1", "2", "4"], "1", d)})
            elif sc_id == "mic_ultrasonic":
                lo_val = self.db.get_setting("scn_us_lo_hz", "19500")
                uv_val = self.db.get_setting("scn_us_view", "0")
                ug_val = self.db.get_setting("scn_us_gain", "2")
                uv_lbl = {"0": "Full FFT ★", "1": "18-24kHz Band", "2": "Heterodyne"}.get(uv_val, "Full FFT ★")
                items.append({"name": "Default View", "suffix": uv_lbl, "action": lambda: self._cycle_db_opts("scn_us_view", ["0", "1", "2"], "0", 1), "on_tune": lambda d: self._cycle_db_opts("scn_us_view", ["0", "1", "2"], "0", d)})
                items.append({"name": "Default LO Freq", "suffix": f"{int(lo_val)/1000:.1f}kHz ★" if lo_val == "19500" else f"{int(lo_val)/1000:.1f}kHz", "action": lambda: self._cycle_db_opts("scn_us_lo_hz", ["18000", "19500", "20500", "21500"], "19500", 1), "on_tune": lambda d: self._cycle_db_opts("scn_us_lo_hz", ["18000", "19500", "20500", "21500"], "19500", d)})
                items.append({"name": "Default Gain", "suffix": f"x{1<<int(ug_val)} ★" if ug_val == "2" else f"x{1<<int(ug_val)}", "action": lambda: self._cycle_db_opts("scn_us_gain", ["0", "1", "2", "3", "4", "5"], "2", 1), "on_tune": lambda d: self._cycle_db_opts("scn_us_gain", ["0", "1", "2", "3", "4", "5"], "2", d)})
            elif sc_id == "ir_scope":
                iz_val = self.db.get_setting("scn_ir_zoom_us", "45000")
                items.append({"name": "Default Zoom", "suffix": f"{int(iz_val)//1000}ms ★" if iz_val == "45000" else f"{int(iz_val)//1000}ms", "action": lambda: self._cycle_db_opts("scn_ir_zoom_us", ["15000", "30000", "45000", "90000"], "45000", 1), "on_tune": lambda d: self._cycle_db_opts("scn_ir_zoom_us", ["15000", "30000", "45000", "90000"], "45000", d)})
            elif sc_id == "ble_radar":
                bv_val = self.db.get_setting("scn_ble_view", "0")
                bg_on = self.db.get_setting("scn_ble_geiger", "0") == "1"
                items.append({"name": "Default View", "suffix": "Tracker List" if bv_val == "1" else "Radar Sweep ★", "action": lambda: self._cycle_db_opts("scn_ble_view", ["0", "1"], "0", 1), "on_tune": lambda d: self._cycle_db_opts("scn_ble_view", ["0", "1"], "0", d)})
                items.append({"name": "Geiger Audio", "suffix": "On" if bg_on else "Off ★", "action": lambda: self._cycle_db_opts("scn_ble_geiger", ["0", "1"], "0", 1), "on_tune": lambda d: self._cycle_db_opts("scn_ble_geiger", ["0", "1"], "0", d)})
            items.append({"name": "Reset Scene", "suffix": "Defaults >", "action": lambda cid=sc_id: self._reset_scene_customization(cid)})
        elif self.current_menu == "lock_scn_viz":
            sc_id = getattr(self, "_tune_scene_id", "music_split")
            sc_lbl = getattr(self, "_tune_scene_label", "Music + Spectrum")
            self.title = f"Viz: {sc_lbl[:12]}"
            st_ov = self.db.get_setting(f"scene_stereo_{sc_id}", "global")
            st_lbl = {
                "global": "Use Global ★",
                "mono": "Mono RTA",
                "stereo": "Stereo L|R",
                "sine_wave": "Sine Wave",
                "stereo_wave": "Stereo Wave",
            }.get(st_ov, "Use Global ★")
            bd_ov = self.db.get_setting(f"scene_bands_{sc_id}", "global")
            bd_lbl = "Use Global ★" if bd_ov == "global" else f"{bd_ov} Bands"
            c_ov = self.db.get_setting(f"scene_curve_{sc_id}", "global")
            c_lbl = {"global": "Use Global ★", "pink": "Pink +3dB", "raw": "Raw RTA 0dB"}.get(c_ov, "Use Global ★")
            b_ov = self.db.get_setting(f"scene_bar_{sc_id}", "global")
            b_lbl = {"global": "Use Global ★", "solid": "Solid", "led": "Segmented LED", "shaded": "Bayer Shaded", "hollow": "Hollow"}.get(b_ov, "Use Global ★")
            p_ov = self.db.get_setting(f"scene_cap_{sc_id}", "global")
            p_lbl = {"global": "Use Global ★", "pwm": "Faded Gray", "faint": "Faint Gray", "trail": "Ember Trail", "solid": "Bright Solid", "dot": "2px Dot", "off": "Off"}.get(p_ov, "Use Global ★")
            d_ov = self.db.get_setting(f"scene_decay_{sc_id}", "global")
            d_lbl = {"global": "Use Global ★", "float": "Zero-G Float", "studio": "Studio RTA", "feather": "Feather Drift", "heavy": "Heavy Gravity"}.get(d_ov, "Use Global ★")
            items = [
                {"name": "Viz Mode", "suffix": st_lbl, "action": lambda cid=sc_id: self._cycle_scene_stereo_override(cid, 1), "on_tune": lambda d, cid=sc_id: self._cycle_scene_stereo_override(cid, d)},
                {"name": "Band Count", "suffix": bd_lbl, "action": lambda cid=sc_id: self._cycle_scene_bands_override(cid, 1), "on_tune": lambda d, cid=sc_id: self._cycle_scene_bands_override(cid, d)},
                {"name": "Physics Preset", "suffix": d_lbl, "action": lambda cid=sc_id: self._cycle_scene_decay_override(cid, 1), "on_tune": lambda d, cid=sc_id: self._cycle_scene_decay_override(cid, d)},
                {"name": "Spectrum Curve", "suffix": c_lbl, "action": lambda cid=sc_id: self._cycle_scene_curve_override(cid, 1), "on_tune": lambda d, cid=sc_id: self._cycle_scene_curve_override(cid, d)},
                {"name": "Bar Style", "suffix": b_lbl, "action": lambda cid=sc_id: self._cycle_scene_bar_override(cid, 1), "on_tune": lambda d, cid=sc_id: self._cycle_scene_bar_override(cid, d)},
                {"name": "Peak Caps", "suffix": p_lbl, "action": lambda cid=sc_id: self._cycle_scene_cap_override(cid, 1), "on_tune": lambda d, cid=sc_id: self._cycle_scene_cap_override(cid, d)},
            ]
        elif self.current_menu == "lock_enables":
            self.title = "Enable Slots"
            mus_on = self.db.get_setting("lock_music_enabled", "1") == "1"
            amb_on = self.db.get_setting("lock_ambient_enabled", "1") == "1"
            hyb_on = self.db.get_setting("lock_amb_music_enabled", "1") == "1"
            sav_on = self.db.get_setting("lock_saver_enabled", "1") == "1"
            items = [
                {"name": "Clock Slot", "suffix": "Always On ★", "action": lambda: self.show_info_toast("Clock is safety anchor")},
                {"name": "Music Slot", "suffix": "Enabled ★" if mus_on else "Disabled", "action": lambda: self._toggle_lock_flag("lock_music_enabled", "1")},
                {"name": "Ambient Slot", "suffix": "Enabled ★" if amb_on else "Disabled", "action": lambda: self._toggle_lock_flag("lock_ambient_enabled", "1")},
                {"name": "Amb+Music Slot", "suffix": "Enabled ★" if hyb_on else "Disabled", "action": lambda: self._toggle_lock_flag("lock_amb_music_enabled", "1")},
                {"name": "Screensaver", "suffix": "Enabled ★" if sav_on else "Disabled", "action": lambda: self._toggle_lock_flag("lock_saver_enabled", "1")},
            ]
        elif self.current_menu == "lock_timers":
            self.title = "Timers & Rules"
            sav_s = int(self.db.get_setting("lock_ambient_idle_sec", "30"))
            sav_pol = self.db.get_setting("lock_saver_policy", "idle_only")
            sav_pol_lbl = {"idle_only": "Idle Only ★", "pause_only": "Pause Only", "rigid": "Rigid Timer"}.get(sav_pol, "Idle Only ★")
            cyc_s = int(self.db.get_setting("lock_cycle_sec", "0"))
            ab_pol = self.db.get_setting("lock_ab_policy", "reset")
            pol_lbl = {"reset": "Reset Timer ★", "pin": "Pin Scene", "strict": "Strict Timer"}.get(ab_pol, "Reset Timer ★")
            t_lock = int(self.db.get_setting("timeout_lock", "30"))
            t_disp = int(self.db.get_setting("timeout_display", "60"))
            lk_lbl = f"{t_lock}s ★" if t_lock == 30 else (f"{t_lock}s" if t_lock > 0 else "Never")
            dp_lbl = f"{t_disp}s ★" if t_disp == 60 else (f"{t_disp}s" if t_disp > 0 else "Never")
            # Stage: Dual Click + Tune Bindings (Allows both Key C click and 4/6 tuning on Timers and HUD FX items)
            items = [
                {"name": "Auto-Lock", "suffix": lk_lbl, "action": lambda: self._cycle_timeout("timeout_lock"), "on_tune": lambda _d: self._cycle_timeout("timeout_lock")},
                {"name": "Screen Off", "suffix": dp_lbl, "action": lambda: self._cycle_timeout("timeout_display"), "on_tune": lambda _d: self._cycle_timeout("timeout_display")},
                {"name": "Saver Trigger", "suffix": sav_pol_lbl, "action": lambda: self._tune_lock_saver_policy(1), "on_tune": self._tune_lock_saver_policy},
                {"name": "Saver Delay", "suffix": f"{sav_s}s ★" if sav_s == 30 else (f"{sav_s}s" if sav_s > 0 else "Off"), "action": lambda: self._cycle_lock_timer("lock_ambient_idle_sec", "30"), "on_tune": lambda _d: self._cycle_lock_timer("lock_ambient_idle_sec", "30")},
                {"name": "Cycle Loop", "suffix": f"Every {cyc_s}s" if cyc_s > 0 else "Off ★", "action": lambda: self._cycle_lock_timer("lock_cycle_sec", "0"), "on_tune": lambda _d: self._cycle_lock_timer("lock_cycle_sec", "0")},
                {"name": "A/B Override", "suffix": pol_lbl, "action": lambda: self._tune_lock_ab_policy(1), "on_tune": self._tune_lock_ab_policy},
            ]
        elif self.current_menu == "lock_hud_fx":
            self.title = "Global HUD & FX"
            seq_c = self.db.get_setting("lock_hud_seq", "title")
            seq_lbl = {
                "title": "Title Only ★",
                "title_guide": "Title -> Guide",
                "guide_title": "Guide -> Title",
                "both": "Title + Guide",
                "guide": "Guide Only",
                "off": "Off (Manual #)"
            }.get(seq_c, "Title Only ★")
            ts_c = self.db.get_setting("lock_title_sec", "3.0")
            ts_lbl = {"1.5": "1.5s", "3.0": "3.0s ★", "5.0": "5.0s", "always": "Always On"}.get(ts_c, "3.0s ★")
            gs_c = self.db.get_setting("lock_guide_sec", "4.5")
            gs_lbl = {"3.0": "3.0s", "4.5": "4.5s ★", "8.0": "8.0s", "always": "Always On"}.get(gs_c, "4.5s ★")
            anc_c = self.db.get_setting("lock_hud_anchor", "left")
            anc_lbl = {"left": "Left ★", "center": "Center", "right": "Right"}.get(anc_c, "Left ★")
            fx_c = self.db.get_setting("lock_hud_fx", "bayer")
            fx_lbl = {"bayer": "Bayer Dissolve ★", "curtain": "Curtain Slide", "laser": "Laser Wipe"}.get(fx_c, "Bayer Dissolve ★")
            items = [
                {"name": "Launch Intro", "suffix": seq_lbl, "action": lambda: self._tune_hud_seq(1), "on_tune": self._tune_hud_seq},
                {"name": "Title Hold", "suffix": ts_lbl, "action": lambda: self._tune_title_hold(1), "on_tune": self._tune_title_hold},
                {"name": "Guide Hold", "suffix": gs_lbl, "action": lambda: self._tune_guide_hold(1), "on_tune": self._tune_guide_hold},
                {"name": "Label Anchor", "suffix": anc_lbl, "action": lambda: self._tune_hud_anchor(1), "on_tune": self._tune_hud_anchor},
                {"name": "Banner Effect", "suffix": fx_lbl, "action": lambda: self._tune_hud_fx(1), "on_tune": self._tune_hud_fx},
            ]
        elif self.current_menu == "desktop_vnc":
            self.title = "Desktop & VNC"
            sess_on, boot_gui, _, gpu_ok = self._get_desktop_state()
            res_s, fps_s, dpi_s = self._parse_display_profile()
            sess_str = "Running (VNC)" if sess_on else "CLI Only ★"
            boot_str = "Desktop GUI" if boot_gui else "CLI Forever ★"
            res_lbl = f"{res_s} ★" if res_s == "1280x720" else res_s
            fps_lbl = f"{fps_s}Hz ★" if fps_s == "30" else f"{fps_s}Hz"
            dpi_lbl = f"{dpi_s} DPI ★" if dpi_s == "65" else f"{dpi_s} DPI"
            gpu_str = "Glamor+DTB OK" if gpu_ok else "Unconfigured"
            items = [
                {"name": "Session Now", "suffix": sess_str, "action": lambda: asyncio.create_task(self._toggle_desktop_session())},
                {"name": "Boot Default", "suffix": boot_str, "action": lambda: asyncio.create_task(self._toggle_desktop_boot_target())},
                {"name": "Resolution", "suffix": res_lbl, "on_tune": self._tune_display_resolution},
                {"name": "Refresh Rate", "suffix": fps_lbl, "on_tune": self._tune_display_refresh_rate},
                {"name": "DPI Scale", "suffix": dpi_lbl, "on_tune": self._tune_display_dpi},
                {"name": "Mali-G31 GPU", "suffix": gpu_str, "action": lambda: None},
                {"name": "Purge LightDM", "suffix": "Run >", "action": lambda: asyncio.create_task(self._purge_lightdm_and_claim_audio())}
            ]
        else:
            self.title = "Settings"
            items = [
                {
                    "name": "Audio Hub",
                    "suffix": "->",
                    "action": "audio_settings"
                },
                {
                    "name": "Bluetooth Hub",
                    "suffix": "->",
                    "action": "bluetooth_manager"
                },
                {
                    "name": "Wi-Fi Hub",
                    "suffix": "->",
                    "action": "wifi_manager"
                },
                {
                    "name": "Hotspot Hub",
                    "suffix": "->",
                    "action": "hotspot_manager"
                },
                {
                    "name": "Display & UI",
                    "suffix": ">",
                    "action": lambda: self._open_submenu("display_ui")
                },
                {
                    "name": "Lock & Scenes",
                    "suffix": ">",
                    "action": lambda: self._open_submenu("lock_cfg")
                },
                {
                    "name": "Desktop & VNC",
                    "suffix": ">",
                    "action": lambda: self._open_submenu("desktop_vnc")
                },
                {
                    "name": "Hardware Info",
                    "suffix": ">",
                    "action": lambda: self._open_submenu("hw_diag")
                }
            ]

        self.update_items(items, preserve_cursor=preserve_cursor)
        
