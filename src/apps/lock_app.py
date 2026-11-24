# /vipxpert_minimal_os/src/apps/lock_app.py
import time
import math
import random
import asyncio
import threading
from src.ui.base_app import BaseApp
from src.ui.components import scroll_text

from src.ui.scene_manager import (
    SceneManager,
    SCENE_CATALOG,
    SCENE_LABELS,
    SCENE_CATEGORIES,
    SCENE_GUIDES,
    SLOT_CATEGORIES,
    AMBIENT_PHYSICS_SCENES,
    INTERACTIVE_SCENES,
)

SceneManager.discover()

_BAYER_4X4 = (
    (0,  8,  2, 10),
    (12, 4, 14,  6),
    (3, 11,  1,  9),
    (15, 7, 13,  5),
)


class LockScreenApp(BaseApp):
    """5-Slot Priority Lock Screen & Visual Scene Picker supporting animated HUD overlays and 16 Scene Cartridges."""
    def __init__(self, display, ui_manager, standalone_scene=None, parent_app_id=None, pick_db_key=None, pick_slot_label=None, category_scenes=None):
        super().__init__(display)
        self.ui = ui_manager
        self.standalone_scene = standalone_scene
        self.parent_app_id = parent_app_id
        self.pick_db_key = pick_db_key
        self.pick_slot_label = pick_slot_label
        self.category_scenes = list(category_scenes) if category_scenes else None
        self.hud_mode = int(self.ui.db.get_setting("lock_hud_mode", "0")) % 3
        now = time.monotonic()
        self.banner_birth = now
        self._last_banner_scene = None
        self._last_banner_slot = None
        self._manual_hud_peek = False
        self.active_slot = "clock"
        self.manual_override = False
        self._prev_playing = False
        self._prev_amb = False
        self.last_slot_input = now
        self.last_cycle_tick = now

        # Interactive 3D state
        self.cube_pitch = 0.4
        self.cube_yaw = 0.6
        self.cube_auto = True
        self.cube_scale = 20.0
        self.cube_mesh = 0

        # USB Power Scope state
        self.scope_view = 0
        self.scope_stride = 1

        # Real-Time 48kHz Rust Audio Visualizer state (True-Stereo + Phase-Locked Waveform + Sine-Pulse Envelopes)
        self._viz_running = False
        self._viz_thread = None
        self._viz_proc = None
        self._viz_source = None
        self.viz_bins = [0.05] * 20
        self.viz_bins_l = [0.05] * 20
        self.viz_bins_r = [0.05] * 20
        self.viz_wave = [0.0] * 44
        self.viz_wave_l = [0.0] * 44
        self.viz_wave_r = [0.0] * 44
        self.viz_fast_m = [0.0] * 20
        self.viz_fast_l = [0.0] * 20
        self.viz_fast_r = [0.0] * 20
        self.viz_env = (0.05, 0.05, 0.05, 0.0)
        self.viz_fullscreen = False

        # Dual-Mic & Ultrasonic state
        self.usonic_view = 0
        self.usonic_listen = False
        self.usonic_carrier_hz = 19500
        self.usonic_gain_shift = 2
        self._usonic_running = False
        self._usonic_thread = None
        self.usonic_data = {
            "full_bins": [2] * 18,
            "us_bins": [2] * 18,
            "vu_l": 0.0,
            "vu_r": 0.0,
            "peak_hz": 20000,
            "peak_db": -90.0,
            "alert": False,
        }

        # IR Logic Scope state
        self._ir_scope_running = False
        self._ir_scope_task = None
        self.ir_pulses = []
        self.ir_raw_str = ""
        self.ir_decoded = "WAITING PH16..."
        self.ir_zoom_us = 45000
        self.ir_pan_us = 0
        self.ir_flashing = False

        # BLE Tracker Radar state
        self._ble_running = False
        self._ble_thread = None
        self.ble_view = 0
        self.ble_geiger = False
        self.ble_sel_idx = 0
        self.ble_devices = {}

        # Ambient Physics state & Wall-Clock Frame-Rate Normalizer
        self._phys_ready = False
        self._scene_last_ts = {}
        self._scene_phase = {}

    def get_scene_dt(self, scene_id: str, ref_fps: float = 30.0) -> float:
        """
        Returns wall-clock delta-time normalized to `ref_fps` ticks (default 30.0 = 1.25x smooth pace).
        Comment (invariant): Saver scenes must scale motion by wall-clock dt instead of raw _frame_seq increments; otherwise when oled_fps=120 and the heavy 2-line Bayer/TTF Guide Card fades out, composite_frame jumps from ~45 FPS to ~110 FPS and doubles scene speed!
        """
        now = time.monotonic()
        prev = self._scene_last_ts.get(scene_id)
        self._scene_last_ts[scene_id] = now
        if prev is None:
            return 1.0
        dt_sec = now - prev
        if dt_sec <= 0.0 or dt_sec > 0.25:
            return 1.0
        return max(0.05, min(3.2, dt_sec * ref_fps))

    def advance_scene_phase(self, scene_id: str, ref_fps: float = 30.0) -> tuple:
        """Advances and returns `(phase_ticks, dt_ticks)` in steady `ref_fps` units."""
        dt = self.get_scene_dt(scene_id, ref_fps=ref_fps)
        ph = self._scene_phase.get(scene_id, 0.0) + dt
        self._scene_phase[scene_id] = ph
        return ph, dt

    def _ensure_ambient_physics(self):
        if getattr(self, "_phys_ready", False):
            return
        self._phys_ready = True
        drop_n = max(1, min(10, int(self.ui.db.get_setting("scn_pond_drops", "4"))))
        self.pond_ripples = [[64.0, 36.0, 4.0, 28.0], [32.0, 44.0, 12.0, 24.0]]
        self.pond_drops = [[random.randint(8, 120), random.randint(0, 24), random.randint(24, 48)] for _ in range(drop_n)]
        self.pond_ripple_spd = float(self.ui.db.get_setting("scn_pond_spd", "1.15"))
        self.pond_storm = 0
        self.star_cx, self.star_cy = 64.0, 32.0
        self.star_speed = float(self.ui.db.get_setting("scn_star_spd", "1.5"))
        self.stars_3d = [[random.uniform(-60, 60), random.uniform(-32, 32), random.uniform(4, 64)] for _ in range(36)]
        self.meteors = []
        b_spd = float(self.ui.db.get_setting("scn_bnc_spd", "1.8"))
        self.bnc_x, self.bnc_y = 42.0, 22.0
        self.bnc_vx, self.bnc_vy = b_spd, round(b_spd * 0.72, 2)
        self.bnc_w, self.bnc_h = 26, 13
        self.bnc_shape = int(self.ui.db.get_setting("scn_bnc_shape", "0")) % 3
        self.bnc_walls = 0
        self.bnc_corners = 0
        self.bnc_flash = 0
        self.grass_wind = float(self.ui.db.get_setting("scn_grass_wind", "0.8"))
        self.grass_target_wind = self.grass_wind
        self.grass_height = int(self.ui.db.get_setting("scn_grass_ht", "22"))
        self.fireflies = [[random.uniform(6, 122), random.uniform(8, 42), random.uniform(0, 6.28)] for _ in range(6)]

    def _get_enabled_slots(self):
        db = self.ui.db
        slots = ["clock"]
        if db.get_setting("lock_music_enabled", "1") == "1":
            slots.append("music")
        if db.get_setting("lock_ambient_enabled", "1") == "1":
            slots.append("ambient")
        if db.get_setting("lock_amb_music_enabled", "1") == "1":
            slots.append("amb_music")
        if db.get_setting("lock_saver_enabled", "1") == "1":
            slots.append("screensaver")
        return slots

    def _resolve_priority_slot(self, is_playing: bool, is_amb: bool, slots: list) -> str:
        if is_playing and is_amb:
            if "amb_music" in slots:
                return "amb_music"
            if "music" in slots:
                return "music"
            if "ambient" in slots:
                return "ambient"
        elif is_playing:
            if "music" in slots:
                return "music"
        elif is_amb:
            if "ambient" in slots:
                return "ambient"
        return "clock"

    def _get_active_scene_id(self) -> str:
        # Active Scene Resolver (Evaluates standalone preview, conditional plugin overrides, and slot DB mappings)
        if self.standalone_scene:
            return self.standalone_scene
        if not self.manual_override and self.active_slot in ("music", "amb_music"):
            auto_sc = SceneManager.resolve_auto_scene(self)
            if auto_sc:
                return auto_sc
        now_s = time.monotonic()
        if now_s - getattr(self, "_sc_id_ts", 0.0) < 0.25 and getattr(self, "_sc_id_slot", None) == self.active_slot:
            return self._sc_id_val
        slot_defaults = {
            "clock":       ("lock_slot_clock_scene",     "clock_digital"),
            "music":       ("lock_slot_music_scene",     "music_split"),
            "ambient":     ("lock_slot_ambient_scene",   "ambient_hud"),
            "amb_music":   ("lock_slot_amb_music_scene", "amb_music_hybrid"),
            "screensaver": ("lock_slot_saver_scene",     "ambient_cube3d"),
        }
        key, dflt = slot_defaults.get(self.active_slot, ("lock_slot_clock_scene", "clock_digital"))
        val = self.ui.db.get_setting(key, dflt)
        self._sc_id_ts = now_s
        self._sc_id_slot = self.active_slot
        self._sc_id_val = val
        return val

    def _get_slot_idle_limit(self, slot_id: str) -> int:
        ov = self.ui.db.get_setting(f"lock_timeout_{slot_id}", "global")
        if ov != "global":
            try:
                return int(ov)
            except ValueError:
                pass
        return int(self.ui.db.get_setting("lock_ambient_idle_sec", "30"))

    # Stage: Single-Source Visualizer Configuration, Hotkey Controller & Viewport Engine (Zero duplicated scene code)
    VIZ_MODES = ("mono", "stereo", "sine_wave", "stereo_wave")
    VIZ_MODE_LABELS = {
        "mono": "Mono RTA",
        "stereo": "Stereo L|R",
        "sine_wave": "Sine Wave",
        "stereo_wave": "Stereo Wave",
    }
    VIZ_BAND_OPTS = (20, 16, 12, 10, 8, 5)
    VIZ_TIMEBASES = ("auto", "20ms", "10ms", "5ms", "2.5ms", "1.2ms", "0.6ms")
    VIZ_TIMEBASE_LABELS = {
        "auto": "Auto ★",
        "20ms": "20ms (Bass)",
        "10ms": "10ms (Mid)",
        "5ms": "5ms (2-4k)",
        "2.5ms": "2.5ms (3-6k)",
        "1.2ms": "1.2ms (12k)",
        "0.6ms": "0.6ms (20k)",
    }

    def get_effective_viz_timebase(self) -> str:
        tb = self.ui.db.get_setting("lock_viz_timebase", "auto")
        return tb if tb in self.VIZ_TIMEBASES else "auto"

    def get_effective_viz_mode(self, scene_id: str) -> str:
        ov = self.ui.db.get_setting(f"scene_stereo_{scene_id}", "global")
        if ov in self.VIZ_MODES:
            return ov
        g_mode = self.ui.db.get_setting("lock_viz_stereo", "mono")
        return g_mode if g_mode in self.VIZ_MODES else "mono"

    def get_effective_viz_bands(self, scene_id: str) -> int:
        ov = self.ui.db.get_setting(f"scene_bands_{scene_id}", "global")
        raw = ov if ov != "global" else self.ui.db.get_setting("lock_viz_bands", "20")
        try:
            val = int(raw)
        except ValueError:
            val = 20
        return val if val in self.VIZ_BAND_OPTS else 20

    def handle_visualizer_hotkeys(self, scene_id: str, action: str, key: str, allow_fullscreen: bool = True) -> bool:
        """
        Centralized live visualizer customization hotkeys shared by all music/visualizer scenes:
- CLICK '0': Cycle Mode (Mono RTA -> Stereo L|R -> Sine Wave -> Stereo Wave)
        - CLICK 'C': Toggle 100% Fullscreen 128x64 Visualizer Canvas (hides text/HUD edge-to-edge)
        - CLICK '3': Cycle Band / Harmonic Count (20 -> 16 -> 12 -> 10 -> 8 -> 5)
        - HOLD  '0': Cycle Bar / Wave Style (Solid -> LED -> PWM Shaded -> Hollow)
        - HOLD  '3': Cycle Peak / Pulse Physics (Zero-G Float -> Feather -> Studio -> Heavy -> Caps Off)
        - HOLD  '1': Toggle Spectrum Curve (Pink +3dB <-> Raw 0dB RTA)
        """
        db = self.ui.db
        if action == "CLICK":
            if key == "0":
                cur = self.get_effective_viz_mode(scene_id)
                nxt = self.VIZ_MODES[(self.VIZ_MODES.index(cur) + 1) % len(self.VIZ_MODES)]
                if db.get_setting(f"scene_stereo_{scene_id}", "global") in self.VIZ_MODES:
                    db.set_setting(f"scene_stereo_{scene_id}", nxt)
                else:
                    db.set_setting("lock_viz_stereo", nxt)
                self._viz_cfg_ts = 0.0
                self.ui.show_toast(f"Viz: {self.VIZ_MODE_LABELS[nxt]}", duration=1.25)
                return True
            if key == "C" and allow_fullscreen and self.standalone_scene is None:
                self.viz_fullscreen = not self.viz_fullscreen
                self._latched_geom_key = None
                self.ui.show_toast("Viz: Fullscreen" if self.viz_fullscreen else "Viz: HUD View", duration=1.15)
                return True
            if key == "3":
                mode_now = self.get_effective_viz_mode(scene_id)
                if "wave" in mode_now:
                    cur_tb = self.get_effective_viz_timebase()
                    nxt_tb = self.VIZ_TIMEBASES[(self.VIZ_TIMEBASES.index(cur_tb) + 1) % len(self.VIZ_TIMEBASES)]
                    db.set_setting("lock_viz_timebase", nxt_tb)
                    self._viz_cfg_ts = 0.0
                    self.ui.show_toast(f"Wave: {self.VIZ_TIMEBASE_LABELS[nxt_tb]}", duration=1.25)
                    return True
                cur_b = self.get_effective_viz_bands(scene_id)
                nxt_b = self.VIZ_BAND_OPTS[(self.VIZ_BAND_OPTS.index(cur_b) + 1) % len(self.VIZ_BAND_OPTS)]
                if db.get_setting(f"scene_bands_{scene_id}", "global") != "global":
                    db.set_setting(f"scene_bands_{scene_id}", str(nxt_b))
                else:
                    db.set_setting("lock_viz_bands", str(nxt_b))
                self._viz_cfg_ts = 0.0
                self._latched_geom_key = None
                self.ui.show_toast(f"Bands: {nxt_b}", duration=1.15)
                return True
        elif action == "HOLD":
            if key == "0":
                styles = ("solid", "led", "shaded", "hollow")
                s_lbls = {"solid": "Solid", "led": "LED Seg", "shaded": "PWM Gray", "hollow": "Hollow/Dual"}
                cur_s = self._get_effective_bar_style(scene_id)
                nxt_s = styles[(styles.index(cur_s) + 1) % len(styles)] if cur_s in styles else "solid"
                if db.get_setting(f"scene_bar_{scene_id}", "global") in styles:
                    db.set_setting(f"scene_bar_{scene_id}", nxt_s)
                else:
                    db.set_setting("lock_viz_bar_style", nxt_s)
                self._viz_cfg_ts = 0.0
                self.ui.show_toast(f"Style: {s_lbls[nxt_s]}", duration=1.25)
                return True
            if key == "3":
                mode_now = self.get_effective_viz_mode(scene_id)
                if "wave" in mode_now:
                    cur_sc = db.get_setting("lock_viz_scope_algo", "triggered_pcm")
                    nxt_sc = "pitch_silk" if cur_sc == "triggered_pcm" else "triggered_pcm"
                    db.set_setting("lock_viz_scope_algo", nxt_sc)
                    self._viz_cfg_ts = 0.0
                    lbl_sc = "Triggered PCM" if nxt_sc == "triggered_pcm" else "Pitch Silk"
                    self.ui.show_toast(f"Scope: {lbl_sc}", duration=1.35)
                    return True
                presets = ("float", "feather", "studio", "heavy", "off")
                p_lbls = {"float": "Zero-G", "feather": "Feather", "studio": "Studio", "heavy": "Heavy", "off": "Caps Off"}
                cur_cap = self._get_effective_cap_style(scene_id)
                cur_pr = db.get_setting("lock_viz_preset", "float")
                cur_key = "off" if cur_cap == "off" else (cur_pr if cur_pr in presets else "float")
                nxt_p = presets[(presets.index(cur_key) + 1) % len(presets)]
                if nxt_p == "off":
                    db.set_setting("lock_viz_cap_style", "off")
                else:
                    if cur_cap == "off":
                        db.set_setting("lock_viz_cap_style", "pwm")
                    db.set_setting("lock_viz_preset", nxt_p)
                self._viz_cfg_ts = 0.0
                self.ui.show_toast(f"Peak: {p_lbls[nxt_p]}", duration=1.25)
                return True
            if key == "1":
                cur_c = self._get_effective_viz_curve(scene_id)
                nxt_c = "raw" if cur_c == "pink" else "pink"
                if db.get_setting(f"scene_curve_{scene_id}", "global") in ("pink", "raw"):
                    db.set_setting(f"scene_curve_{scene_id}", nxt_c)
                else:
                    db.set_setting("lock_viz_curve", nxt_c)
                self._viz_cfg_ts = 0.0
                self.ui.show_toast("EQ: Pink +3dB" if nxt_c == "pink" else "EQ: Raw 0dB", duration=1.25)
                return True
        return False

    def _get_effective_viz_curve(self, scene_id: str) -> str:
        """Resolves per-scene or global spectrum weighting curve: 'pink' (+3dB/oct Pink-Noise flat ★) or 'raw' (0dB linear RTA)."""
        ov = self.ui.db.get_setting(f"scene_curve_{scene_id}", "global")
        if ov in ("pink", "raw"):
            return ov
        g_curve = self.ui.db.get_setting("lock_viz_curve", "pink")
        return g_curve if g_curve in ("pink", "raw") else "pink"

    def _get_effective_bar_style(self, scene_id: str) -> str:
        """Resolves per-scene or global Bar Style: 'solid' ★, 'led' (Segmented), 'shaded' (PWM Gradient), or 'hollow' (Outline)."""
        ov = self.ui.db.get_setting(f"scene_bar_{scene_id}", "global")
        valid = ("solid", "led", "shaded", "hollow")
        if ov in valid:
            return ov
        g_val = self.ui.db.get_setting("lock_viz_bar_style", "solid")
        return g_val if g_val in valid else "solid"

    def _get_effective_cap_style(self, scene_id: str) -> str:
        ov = self.ui.db.get_setting(f"scene_cap_{scene_id}", "global")
        valid = ("pwm", "faint", "trail", "solid", "dot", "off")
        if ov in valid:
            return ov
        g_val = self.ui.db.get_setting("lock_viz_cap_style", "pwm")
        return g_val if g_val in valid else "pwm"

    def _get_effective_particle_physics(self, scene_id: str):
        """
        Resolves (hold_frames, curve_exp, v0_drift, accel_g) for floating peak particles.
        Supports 1-click presets ('float' ★, 'studio', 'feather', 'heavy') OR 'custom' fine-tuned variables!
        """
        db = self.ui.db
        sc_pres = db.get_setting(f"scene_decay_{scene_id}", "global")
        preset = sc_pres if sc_pres in ("float", "studio", "feather", "heavy", "custom") else db.get_setting("lock_viz_preset", "float")
        # Map legacy values ('slow', 'med', 'fast', 'hold') gracefully
        if preset == "slow":
            preset = "float"
        elif preset in ("med", "hold"):
            preset = "studio"
        elif preset == "fast":
            preset = "heavy"

        presets_map = {
            # preset:    (hold_ms, curve_type, speed_tier)
            "float":   (450, "quad",  "slow"),   # ★ Default: Hangs 0.45s in air, starts near-zero drift, accelerates smoothly (t^2)
            "studio":  (650, "quad",  "med"),    # Classic studio RTA peak hold (0.65s hang, then brisk accelerated drop)
            "feather": (350, "cubic", "slow"),   # Ultra-gentle upper suspension (t^2.6), speeds up only near the bottom
            "heavy":   (180, "quad",  "fast"),   # Brief 0.18s hang, strong gravity pull
        }
        if preset in presets_map:
            hold_ms, curve_t, spd_t = presets_map[preset]
        else:
            try:
                hold_ms = int(db.get_setting("lock_viz_hold_ms", "450"))
            except ValueError:
                hold_ms = 450
            curve_t = db.get_setting("lock_viz_accel_curve", "quad")
            spd_t = db.get_setting("lock_viz_term_spd", "slow")

        # Convert hold_ms to ~60Hz V-Sync frames (approx 16.6ms per frame)
        hold_frames = max(2, int(round(hold_ms / 16.6)))
        # Acceleration exponent p in v(t) = v0 + g * (t^p):
        #   - 'quad' ★ (p=1.85): Starts floating almost stationary after hold, then gradually accelerates downward
        #   - 'cubic'  (p=2.60): Lingers even longer in the upper air before swooping down
        #   - 'linear' (p=0.0):  Constant-velocity fall
        if curve_t == "cubic":
            exp_p = 2.60
        elif curve_t == "linear":
            exp_p = 0.0
        else:
            exp_p = 1.85

        if spd_t == "fast":
            v0, g_coeff = (0.0018, 0.0220) if exp_p > 0.0 else (0.0140, 0.0)
        elif spd_t == "med":
            v0, g_coeff = (0.0010, 0.0115) if exp_p > 0.0 else (0.0075, 0.0)
        else:
            # 'slow' ★ default: tiny 0.0005/frame initial creep after apex hold, smoothly accelerating up to ~0.0065/frame
            v0, g_coeff = (0.0005, 0.0055) if exp_p > 0.0 else (0.0032, 0.0)

        return hold_frames, exp_p, v0, g_coeff

    def _get_scene_hud_behavior(self, scene_id: str) -> str:
        """
        Returns the simplified per-scene HUD behavior override:
        - 'global':    Follow Global HUD & FX settings ★
        - 'title':     Show 3.0s Title Intro on launch, then clean
        - 'pin_guide': Keep 2-Line Keypad Guide pinned on screen
        - 'clean':     No automatic overlays on launch (100% Clean Canvas until '#' pressed)
        """
        ov = self.ui.db.get_setting(f"scene_hud_beh_{scene_id}", "global")
        return ov if ov in ("global", "title", "pin_guide", "clean") else "global"

    def _get_effective_seq_cfg(self, scene_id: str) -> str:
        valid = ("title", "title_guide", "guide_title", "both", "guide", "off")
        ov_seq = self.ui.db.get_setting(f"scene_seq_{scene_id}", "global")
        if ov_seq in valid:
            return ov_seq
        beh = self._get_scene_hud_behavior(scene_id)
        if beh == "title":
            return "title"
        if beh == "pin_guide":
            return "guide"
        if beh == "clean":
            return "off"
        g_seq = self.ui.db.get_setting("lock_hud_seq", "title")
        return g_seq if g_seq in valid else "title"

    def _get_effective_title_sec(self, scene_id: str) -> float:
        ov = self.ui.db.get_setting(f"scene_t_sec_{scene_id}", "global")
        val = ov if ov != "global" else self.ui.db.get_setting("lock_title_sec", "3.0")
        if val == "always":
            return 999999.0
        try:
            return float(val)
        except ValueError:
            return 3.0

    def _get_effective_guide_sec(self, scene_id: str) -> float:
        if self._get_scene_hud_behavior(scene_id) == "pin_guide":
            return 999999.0
        ov = self.ui.db.get_setting(f"scene_g_sec_{scene_id}", "global")
        val = ov if ov != "global" else self.ui.db.get_setting("lock_guide_sec", "4.5")
        if val == "always":
            return 999999.0
        try:
            return float(val)
        except ValueError:
            return 4.5

    def _get_effective_anchor_cfg(self, scene_id: str) -> str:
        ov = self.ui.db.get_setting(f"scene_anchor_{scene_id}", "global")
        if ov in ("left", "center", "right"):
            return ov
        g_anc = self.ui.db.get_setting("lock_hud_anchor", "left")
        return g_anc if g_anc in ("left", "center", "right") else "left"

    def _get_effective_hud_mode(self, scene_id: str) -> int:
        return 0

    def _load_scene_individual_params(self):
        db = self.ui.db
        self.cube_mesh = int(db.get_setting("scn_3d_mesh", "0")) % 2
        self.cube_auto = db.get_setting("scn_3d_auto", "1") == "1"
        self.cube_scale = float(db.get_setting("scn_3d_scale", "20"))
        self.scope_view = int(db.get_setting("scn_pwr_view", "0")) % 3
        self.usonic_view = int(db.get_setting("scn_us_view", "0")) % 3
        self.usonic_carrier_hz = int(db.get_setting("scn_us_lo_hz", "19500"))
        self.ir_zoom_us = int(db.get_setting("scn_ir_zoom_us", "45000"))
        self.ble_view = int(db.get_setting("scn_ble_view", "0")) % 2
        if getattr(self, "_phys_ready", False):
            self.star_speed = float(db.get_setting("scn_star_spd", "1.5"))
            self.grass_target_wind = float(db.get_setting("scn_grass_wind", "0.8"))
            self.grass_height = int(db.get_setting("scn_grass_ht", "22"))
            self.bnc_shape = int(db.get_setting("scn_bnc_shape", "0")) % 3

    def _get_media_playing(self) -> bool:
        media = self.ui.media_arbiter.get_now_playing()
        raw_status = media.get("status", "stopped")
        status_str = str(raw_status.value if hasattr(raw_status, "value") else raw_status)
        if status_str != "playing":
            return False
        if media.get("source") == "bt":
            raw_tr = media.get("transport_state")
            tr_str = str(raw_tr.value if hasattr(raw_tr, "value") else raw_tr)
            if tr_str not in ("active", "pending"):
                return False
        return True

    def _has_music_session(self) -> bool:
        """Returns True when Bluetooth is connected or local audio is playing/paused so Pause stays on Music and Disconnect switches to Clock."""
        bt_d = getattr(self.ui, "bt_daemon", None)
        bt_ms = getattr(bt_d, "media_state", {}) if bt_d else {}
        bt_conn = bool(bt_ms.get("connected_mac") or bt_ms.get("player_path") or getattr(self.ui, "bt_session_connected", False)) and not getattr(self.ui, "bt_user_disconnected", False)
        if bt_conn:
            return True
        media = self.ui.media_arbiter.get_now_playing()
        raw_status = media.get("status", "stopped")
        status_str = str(raw_status.value if hasattr(raw_status, "value") else raw_status)
        return status_str in ("playing", "paused")

    def _trigger_banner_intro(self, hash_step: int = 0):
        """
        Resets the banner transition clock.
        hash_step = 0: Auto Launch Intro (follows Launch Intro setting, default 'title')
        hash_step = 1: Manual '#' Step 1 -> Pinned 2-Line Keypad Guide Card (with transition)
        hash_step = 2: Manual '#' Step 2 -> Pinned Scene Title Pill (with transition)
        hash_step = 3: Manual '#' Step 3 -> Smoothly dissolve out to Clean Canvas
        """
        self._hash_step = hash_step
        self.banner_birth = time.monotonic()
        self.display.has_active_marquee = True
        self.ui.request_render()

    def _stop_viz_worker(self):
        """Cleanly terminates the isolated 48kHz visualizer process group (both Python helper and child parec streams)."""
        import os, signal
        self._viz_running = False
        proc = getattr(self, "_viz_proc", None)
        self._viz_proc = None
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

    def _resolve_pure_music_sink_input(self):
        """
        Finds the PipeWire/Pulse sink-input index of the active music player (MPV or BlueZ A2DP)
        so 'music_split' and 'music_viz' react to 100% Pure Music even if AMB mic loopback is active!
        """
        import subprocess
        try:
            res = subprocess.run(
                ["runuser", "-u", "vipxpert", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000", "pactl", "list", "sink-inputs"],
                capture_output=True, text=True, timeout=0.4
            )
            cur_id = None
            for raw_ln in res.stdout.splitlines():
                ln = raw_ln.strip()
                if ln.startswith("Sink Input #"):
                    cur_id = ln.split("#", 1)[1].strip()
                elif cur_id and ("application.name" in ln or "node.name" in ln or "media.name" in ln):
                    low = ln.lower()
                    if "loopback" in low or "vip_mic" in low or "parec" in low or "pacat" in low:
                        continue
                    if "mpv" in low or "bluez" in low or "a2dp" in low or "vip_usb_out" in low:
                        return cur_id
        except Exception:
            pass
        return None

    def _sync_scope_burst(self):
        awake = not getattr(self.ui, "is_screen_off", False)
        scene_id = self._get_active_scene_id()
        pm = getattr(self.ui, "power_monitor", None)
        if pm:
            pm.set_scope_mode(awake and (scene_id == "power_scope"))

        # 1. GIL-Free 48kHz (45Hz-20kHz) Audio Visualizer Subprocess:
        #    Includes live amb_flag in target_mode so toggling AMB immediately updates stream isolation!
        viz_scenes = {"music_split", "music_viz", "ambient_hud", "amb_music_hybrid"}
        if self.viz_fullscreen and scene_id == "music_classic":
            viz_scenes.add("music_classic")
        if awake and scene_id in viz_scenes:
            is_play = self._get_media_playing()
            is_amb = "1" if getattr(self.ui, "transparency_enabled", False) else "0"
            curve_m = self._get_effective_viz_curve(scene_id)
            tb_m = self.get_effective_viz_timebase()
            if scene_id in ("music_split", "music_viz", "music_classic"):
                want_viz = is_play
                target_mode = f"music_only:{curve_m}:{is_amb}:{tb_m}"
            elif scene_id == "ambient_hud":
                want_viz = True
                target_mode = f"mic_only:{curve_m}:{is_amb}:{tb_m}"
            else:
                want_viz = True
                target_mode = f"hybrid:{curve_m}:{is_amb}:{tb_m}"
        else:
            want_viz = False
            target_mode = None

        now_t = time.monotonic()
        if want_viz:
            dead_thread = (self._viz_thread is not None) and (not self._viz_thread.is_alive())
            if (not self._viz_running) or (self._viz_source != target_mode) or (dead_thread and (now_t - getattr(self, "_last_viz_spawn", 0.0)) >= 1.0):
                self._stop_viz_worker()
                self._viz_source = target_mode
                self._viz_running = True
                self._last_viz_spawn = now_t
                self._viz_thread = threading.Thread(
                    target=self._viz_scope_worker,
                    args=(target_mode,),
                    daemon=True,
                    name="AudioVizIPC"
                )
                self._viz_thread.start()
        elif self._viz_running:
            self._stop_viz_worker()

        # 2. 48kHz Dual-Mic & Ultrasonic Scope Worker
        want_mic = awake and (scene_id == "mic_ultrasonic")
        if want_mic and not self._usonic_running:
            self._usonic_running = True
            self._usonic_thread = threading.Thread(target=self._mic_scope_worker, daemon=True, name="MicUltrasonicScope")
            self._usonic_thread.start()
        elif not want_mic and self._usonic_running:
            self._usonic_running = False
            self.usonic_listen = False

        # 3. Mode2 IR Logic Analyzer Worker
        want_ir = awake and (scene_id == "ir_scope")
        if want_ir and not self._ir_scope_running:
            try:
                loop = asyncio.get_running_loop()
                self._ir_scope_running = True
                self._ir_scope_task = loop.create_task(self._ir_scope_worker())
            except RuntimeError:
                pass
        elif not want_ir and self._ir_scope_running:
            self._ir_scope_running = False
            if self._ir_scope_task:
                self._ir_scope_task.cancel()
                self._ir_scope_task = None

        # 4. BLE Tracker Proximity Radar Worker
        want_ble = awake and (scene_id == "ble_radar")
        if want_ble and not self._ble_running:
            self._ble_running = True
            self._ble_thread = threading.Thread(target=self._ble_radar_worker, daemon=True, name="BLERadarScope")
            self._ble_thread.start()
        elif not want_ble and self._ble_running:
            self._ble_running = False
            self.ble_geiger = False

    def _viz_scope_worker(self, mode_curve_amb: str):
        """
        Zero-GIL-Contention 48kHz (45Hz..20kHz) Visualizer IPC Reader:
        - Spawns an isolated Python DSP subprocess in its own session (start_new_session=True) on Cores 0-1.
        - Applies CIC Boxcar Low-Pass anti-aliasing filters with exact inverse-sinc droop compensation.
        - Uses Dual-Subframe Transient Peak Catch on BOTH Mids (365..2450Hz) and Highs (3.35..19.5kHz) so fast snare
          and hi-hat hits anywhere in the 21.3ms frame are captured at 100% amplitude.
        - Automatically falls back to @DEFAULT_MONITOR@ if a specific --monitor-stream closes on track change!
        """
        import os, signal, subprocess, sys
        parts = mode_curve_amb.split(":")
        viz_mode = parts[0] if parts else "music_only"
        curve_mode = parts[1] if len(parts) > 1 else "pink"
        amb_on = parts[2] if len(parts) > 2 else ("1" if getattr(self.ui, "transparency_enabled", False) else "0")
        tb_mode = parts[3] if len(parts) > 3 else "auto"

        stream_idx = ""
        if viz_mode == "music_only" and amb_on == "1":
            stream_idx = self._resolve_pure_music_sink_input() or ""

        dsp_code = r"""
import os, sys, math, fcntl, select, signal, subprocess
from array import array
try:
    os.sched_setaffinity(0, {0})
except Exception:
    pass

viz_mode, curve_mode, amb_on, stream_idx = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

p_main, p_aux = None, None
def _clean_exit(signum, frame):
    for p in (p_main, p_aux):
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
    os._exit(0)

signal.signal(signal.SIGTERM, _clean_exit)
signal.signal(signal.SIGINT, _clean_exit)

# Stage: Contiguous 4-Tap Log-Band Goertzel Filterbank (Covers 40Hz..20.4kHz with zero inter-bar frequency gaps)
# Comment (invariant): Adjacent bar centers are spaced by ~1.38x (+-18%); sampling 4 log-spaced taps across the full geometric band [sqrt(f_prev*f_c)..sqrt(f_c*f_next)] up to 20.4kHz (0.425*fs) eliminates inter-bar notches and restores 17..20kHz sensitivity.
centers = [45, 65, 92, 130, 185, 260, 365, 510, 700, 960, 1320, 1800, 2450, 3350, 4550, 6200, 8400, 11400, 15200, 19500]
specs = []
for idx, f_c in enumerate(centers):
    if idx < 6:
        stride, fs_eff = 8, 6000.0
    elif idx < 13:
        stride, fs_eff = 2, 24000.0
    else:
        stride, fs_eff = 1, 48000.0
    f_prev = centers[idx - 1] if idx > 0 else (f_c / 1.38)
    f_next = centers[idx + 1] if idx + 1 < len(centers) else 21400.0
    f_lo = max(25.0, math.sqrt(f_prev * f_c))
    f_hi = min(fs_eff * 0.435, math.sqrt(f_c * f_next))
    if idx == 19:
        f_lo, f_hi = 16600.0, 20400.0
    ratio = (f_hi / f_lo) if f_hi > f_lo else 1.05
    taps = [
        2.0 * math.cos(2.0 * math.pi * (f_lo * (ratio ** (k / 3.0))) / fs_eff)
        for k in range(4)
    ]
    # Inverse-sinc compensation for the CIC Boxcar anti-aliasing filter droop: 1 / sinc(pi * f_c / fs_eff)
    x_sinc = math.pi * f_c / fs_eff
    sinc_comp = (x_sinc / math.sin(x_sinc)) if (stride > 1 and x_sinc > 0.01) else 1.0
    hf_boost = 1.35 if idx >= 18 else (1.15 if idx == 17 else 1.0)
    w = (((f_c / 60.0) ** 0.58) if curve_mode == "pink" else 1.0) * sinc_comp * hf_boost
    specs.append((stride, taps[0], taps[1], taps[2], taps[3], w))

hann128 = [0.5 * (1.0 - math.cos(2.0 * math.pi * i / 127.0)) for i in range(128)]

def get_music_monitor():
    try:
        s = subprocess.check_output(["pactl", "get-default-sink"], text=True, timeout=0.3).strip()
        if s and "vip_mic_bus" not in s:
            return f"{s}.monitor"
    except Exception:
        pass
    return "alsa_output.platform-soc_ahub0_mach.stereo-fallback.monitor"

def spawn_parec(target, is_stream=False):
    if not is_stream and target == "@DEFAULT_MONITOR@":
        target = get_music_monitor()
    # Comment (invariant): Keep parec latency >= 22ms (>= 1024/48000 quantum) and avoid spawning pw-metadata on every visualizer mount so parec never forces sub-quantum PipeWire graph reconfigurations during Bluetooth A2DP playback.
    cmd = ["parec", "--format=s16le", "--rate=48000", "--channels=1", "--latency-msec=22"]
    cmd.append(f"--monitor-stream={target}" if is_stream else f"--device={target}")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    fd = p.stdout.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    return p, fd

def goertzel_pair(seq_w, c0, c1, c2, c3):
    s1a = s2a = s1b = s2b = s1c = s2c = s1d = s2d = 0.0
    for x in seq_w:
        s0a = x + c0 * s1a - s2a
        s2a, s1a = s1a, s0a
        s0b = x + c1 * s1b - s2b
        s2b, s1b = s1b, s0b
        s0c = x + c2 * s1c - s2c
        s2c, s1c = s1c, s0c
        s0d = x + c3 * s1d - s2d
        s2d, s1d = s1d, s0d
    p_a = max(0.0, s1a * s1a + s2a * s2a - c0 * s1a * s2a)
    p_b = max(0.0, s1b * s1b + s2b * s2b - c1 * s1b * s2b)
    p_c = max(0.0, s1c * s1c + s2c * s2c - c2 * s1c * s2c)
    p_d = max(0.0, s1d * s1d + s2d * s2d - c3 * s1d * s2d)
    p_max = max(p_a, p_b, p_c, p_d)
    p_avg = (p_a + p_b + p_c + p_d) * 0.25
    return math.sqrt(p_max * 0.75 + p_avg * 0.25) * 0.015625

try:
    using_stream_idx = False
    if viz_mode == "mic_only":
        p_main, fd_main = spawn_parec("vip_mic_bus.monitor", False)
        fd_aux = None
    elif viz_mode == "music_only":
        if stream_idx:
            p_main, fd_main = spawn_parec(stream_idx, True)
            using_stream_idx = True
        else:
            p_main, fd_main = spawn_parec("@DEFAULT_MONITOR@", False)
        fd_aux = None
    else:
        p_main, fd_main = spawn_parec("@DEFAULT_MONITOR@", False)
        if amb_on == "0":
            p_aux, fd_aux = spawn_parec("vip_mic_bus.monitor", False)
        else:
            fd_aux = None

    ring_main = bytearray(2048)
    ring_aux = bytearray(2048)
    agc_peak = 6.0 if viz_mode == "music_only" else 180.0
    smooth = [0.05] * 20
    out_fd = sys.stdout.fileno()
    bytes_since_calc = 0

    while True:
        r_fds = [fd_main] + ([fd_aux] if fd_aux is not None else [])
        ready, _, _ = select.select(r_fds, [], [], 0.030)
        # Comment (optimization): Core-1 OLED_Compositor latches Visualizer frames at 51.73Hz (19.33ms = 928 samples = 1856 s16le bytes); hopping every 1856 bytes instead of 768 bytes (125Hz) cuts Goertzel CPU from 70.6% to ~25% on Core 0 without dropping a single displayed spectrum update (DP-498).
        if fd_main in ready:
            while bytes_since_calc < 1856:
                try:
                    chunk = os.read(fd_main, 2048)
                    if not chunk:
                        if using_stream_idx:
                            try:
                                p_main.terminate()
                            except Exception:
                                pass
                            p_main, fd_main = spawn_parec("@DEFAULT_MONITOR@", False)
                            using_stream_idx = False
                            break
                        _clean_exit(0, None)
                    bytes_since_calc += len(chunk)
                    n_c = min(2048, len(chunk) & ~1)
                    if n_c >= 2048:
                        ring_main[:] = chunk[-2048:]
                    elif n_c > 0:
                        ring_main[:-n_c] = ring_main[n_c:]
                        ring_main[-n_c:] = chunk[:n_c]
                except BlockingIOError:
                    break
        if fd_aux is not None and fd_aux in ready:
            while True:
                try:
                    chunk = os.read(fd_aux, 1024)
                    if not chunk:
                        break
                    bytes_since_calc = max(bytes_since_calc, len(chunk))
                    n_c = min(2048, len(chunk) & ~1)
                    if n_c >= 2048:
                        ring_aux[:] = chunk[-2048:]
                    elif n_c > 0:
                        ring_aux[:-n_c] = ring_aux[n_c:]
                        ring_aux[-n_c:] = chunk[:n_c]
                except BlockingIOError:
                    break

        if bytes_since_calc < 1856:
            continue
        bytes_since_calc = 0

        arr_m = array("h")
        arr_m.frombytes(ring_main)
        if fd_aux is not None:
            arr_a = array("h")
            arr_a.frombytes(ring_aux)
            pcm = [arr_m[i] + int(arr_a[i] * 1.15) for i in range(1024)]
        else:
            pcm = arr_m

        # 1. CIC Boxcar 8-sample Low-Pass Anti-Aliasing Filter for Sub-Bass/Bass (45..260Hz)
        s8_raw = [sum(pcm[i * 8 : i * 8 + 8]) * 0.125 for i in range(128)]
        m8 = sum(s8_raw) * 0.0078125
        w8 = [(s8_raw[i] - m8) * hann128[i] for i in range(128)]

        # 2. Dual-Subframe CIC Boxcar 2-sample Low-Pass Filter for Mids (365..2450Hz across [256:512] and [768:1024])
        s2_early = [(pcm[256 + i * 2] + pcm[257 + i * 2]) * 0.5 for i in range(128)]
        m2_e = sum(s2_early) * 0.0078125
        w2_early = [(s2_early[i] - m2_e) * hann128[i] for i in range(128)]

        s2_late = [(pcm[768 + i * 2] + pcm[769 + i * 2]) * 0.5 for i in range(128)]
        m2_l = sum(s2_late) * 0.0078125
        w2_late = [(s2_late[i] - m2_l) * hann128[i] for i in range(128)]

        # 3. Dual-Subframe High-Frequency Windows for Treble (3.35kHz..19.5kHz across [384:512] and [896:1024])
        s1_early = pcm[384:512]
        m1_e = sum(s1_early) * 0.0078125
        w1_early = [(s1_early[i] - m1_e) * hann128[i] for i in range(128)]

        s1_late = pcm[896:1024]
        m1_l = sum(s1_late) * 0.0078125
        w1_late = [(s1_late[i] - m1_l) * hann128[i] for i in range(128)]

        mags = [0.0] * 20
        frame_max = 1.0

        for idx, (stride, c0, c1, c2, c3, weight) in enumerate(specs):
            if stride == 8:
                raw_mag = goertzel_pair(w8, c0, c1, c2, c3)
            elif stride == 2:
                raw_mag = max(goertzel_pair(w2_early, c0, c1, c2, c3), goertzel_pair(w2_late, c0, c1, c2, c3))
            else:
                raw_mag = max(goertzel_pair(w1_early, c0, c1, c2, c3), goertzel_pair(w1_late, c0, c1, c2, c3))
            val = raw_mag * weight
            mags[idx] = val
            if val > frame_max:
                frame_max = val

        floor_pk = 1.8 if viz_mode == "music_only" else 18.0
        gate_v = 0.12 if viz_mode == "music_only" else 2.2
        if frame_max > agc_peak:
            agc_peak = agc_peak * 0.35 + frame_max * 0.65
        else:
            agc_peak = max(floor_pk, agc_peak * 0.96 + frame_max * 0.04)

        out_bytes = bytearray(20)
        for i in range(20):
            v = mags[i]
            if v < gate_v:
                norm = 0.05
            else:
                db_rel = 20.0 * math.log10(max(1e-4, v / agc_peak))
                lin = max(0.0, min(1.0, (db_rel + 22.0) / 22.0))
                norm = max(0.05, min(1.0, lin ** 1.35))
            prev_b = smooth[i]
            cur_b = (prev_b * 0.20 + norm * 0.80) if norm > prev_b else (prev_b * 0.54 + norm * 0.46)
            smooth[i] = max(0.05, min(1.0, cur_b))
            out_bytes[i] = max(12, min(255, int(round(smooth[i] * 255.0))))

        os.write(out_fd, out_bytes)
finally:
    _clean_exit(0, None)
"""
        # Comment (invariant): Never pass `0,1` (Core 1 is isolated for nice(-20) OLED_Compositor, DP-495) and never cram onto single Core `0` alone on 4-core H618 (HR-503); read dsp_cores from hw_profile (`0,2` on 4-core boards, `0` only on 1/2-core boards) and prefer native Rust `bin/viz_dsp` (<0.5% CPU, zero GC).
        dsp_cores = str(getattr(self.ui, "hw_profile", {}).get("dsp_cores", "0,2" if (os.cpu_count() or 1) >= 4 else "0"))
        native_bin = "/home/vipxpert/vipxpert_minimal_os/bin/viz_dsp"
        use_ext344 = os.path.isfile(native_bin) and os.access(native_bin, os.X_OK)
        if use_ext344:
            cmd = [
                "taskset", "-c", dsp_cores,
                "runuser", "-u", "vipxpert", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000",
                native_bin, viz_mode, curve_mode, amb_on, stream_idx, "ext344", tb_mode
            ]
            pkt_len = 344
        else:
            cmd = [
                "taskset", "-c", dsp_cores,
                "runuser", "-u", "vipxpert", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000",
                sys.executable, "-u", "-c", dsp_code, viz_mode, curve_mode, amb_on, stream_idx
            ]
            pkt_len = 20
        proc = None
        try:
            import select as _sel
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
            self._viz_proc = proc
            p_fd = proc.stdout.fileno()

            def _read_exact(n_req: int) -> bytes:
                buf = bytearray()
                while len(buf) < n_req:
                    ch = os.read(p_fd, n_req - len(buf))
                    if not ch:
                        return b""
                    buf.extend(ch)
                return bytes(buf)

            while self._viz_running and self._viz_source == mode_curve_amb:
                if getattr(self.ui, "is_screen_off", False):
                    break
                pkt = _read_exact(pkt_len)
                if not pkt:
                    break
                # Drain any queued complete packets so UI always latches the freshest 0.0ms real-time audio frame!
                while _sel.select([p_fd], [], [], 0.0)[0]:
                    newer = _read_exact(pkt_len)
                    if not newer:
                        break
                    pkt = newer
                self.viz_bins = [b / 255.0 for b in pkt[0:20]]
                if pkt_len == 344:
                    self.viz_bins_l = [b / 255.0 for b in pkt[20:40]]
                    self.viz_bins_r = [b / 255.0 for b in pkt[40:60]]
                    wl = [(b - 128) / 124.0 for b in pkt[60:180]]
                    wr = [(b - 128) / 124.0 for b in pkt[180:300]]
                    wm = [(wl[k] + wr[k]) * 0.5 for k in range(120)]
                    fl = [b / 255.0 for b in pkt[304:324]]
                    fr = [b / 255.0 for b in pkt[324:344]]
                    self.viz_wave_l = wl
                    self.viz_wave_r = wr
                    self.viz_wave = wm
                    self.viz_fast_l = fl
                    self.viz_fast_r = fr
                    self.viz_fast_m = [max(fl[i], fr[i]) for i in range(20)]
                    f0_l = (18.0 * ((1500.0 / 18.0) ** ((pkt[302] - 1) / 254.0))) if pkt[302] > 0 else 0.0
                    f0_r = (18.0 * ((1500.0 / 18.0) ** ((pkt[303] - 1) / 254.0))) if pkt[303] > 0 else 0.0
                    self.viz_f0_l = f0_l
                    self.viz_f0_r = f0_r
                    self.viz_f0_m = f0_l if (f0_l > 0.0 and (f0_r <= 0.0 or max(fl) >= max(fr))) else f0_r
                    self.viz_env = (pkt[300] / 255.0, pkt[301] / 255.0, pkt[302] / 255.0, pkt[303] / 255.0)
                else:
                    self.viz_bins_l = self.viz_bins
                    self.viz_bins_r = self.viz_bins
                    self.viz_fast_m = [max(0.0, b - 0.05) for b in self.viz_bins]
                    self.viz_fast_l = self.viz_fast_m
                    self.viz_fast_r = self.viz_fast_m
        except Exception:
            pass
        finally:
            if proc is not None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            if self._viz_proc is proc:
                self._viz_proc = None

    async def on_start(self):
        now = time.monotonic()
        self.last_slot_input = now
        self.last_cycle_tick = now
        self.banner_birth = now
        self.manual_override = False
        self._manual_hud_peek = False
        self._hash_step = 0
        self._load_scene_individual_params()
        is_playing = self._get_media_playing()
        has_music = self._has_music_session()
        is_amb = bool(getattr(self.ui, "transparency_enabled", False))
        self._prev_playing = is_playing
        self._prev_music_session = has_music
        self._prev_amb = is_amb
        self.active_slot = self._resolve_priority_slot(has_music, is_amb, self._get_enabled_slots())
        sc_id = self._get_active_scene_id()
        self._last_banner_scene = sc_id
        self._last_banner_slot = self.active_slot
        asyncio.create_task(self.ui.adjust_pi_volume(0))
        await self.update()

    async def on_resume(self, previous_app_id=None):
        """Ensures cached lock_screen or preview instances refresh timers cleanly when resumed."""
        now = time.monotonic()
        self.last_slot_input = now
        self.banner_birth = now
        self._hash_step = 0
        await self.update()

    async def on_screen_sleep(self):
        self._stop_viz_worker()
        self._usonic_running = False
        self.usonic_listen = False
        self._ir_scope_running = False
        self._ble_running = False
        self.ble_geiger = False
        if self._ir_scope_task:
            self._ir_scope_task.cancel()
            self._ir_scope_task = None
        pm = getattr(self.ui, "power_monitor", None)
        if pm:
            pm.set_scope_mode(False)
        has_music = self._has_music_session()
        is_amb = bool(getattr(self.ui, "transparency_enabled", False))
        self.active_slot = self._resolve_priority_slot(has_music, is_amb, self._get_enabled_slots())

    async def on_screen_wake(self):
        now = time.monotonic()
        self.last_slot_input = now
        self.last_cycle_tick = now
        self.banner_birth = now
        is_playing = self._get_media_playing()
        has_music = self._has_music_session()
        is_amb = bool(getattr(self.ui, "transparency_enabled", False))
        self._prev_playing = is_playing
        self._prev_music_session = has_music
        self._prev_amb = is_amb
        if not (self.manual_override and self.ui.db.get_setting("lock_ab_policy", "reset") == "pin"):
            self.active_slot = self._resolve_priority_slot(has_music, is_amb, self._get_enabled_slots())
        await self.update()

    async def on_stop(self):
        # Lock Screen Teardown (Stops background scope workers and resets session-scoped MV suppression)
        self._mv_suppressed = False
        self._stop_viz_worker()
        self._usonic_running = False
        self.usonic_listen = False
        self._ir_scope_running = False
        self._ble_running = False
        self.ble_geiger = False
        if self._ir_scope_task:
            self._ir_scope_task.cancel()
            self._ir_scope_task = None
        pm = getattr(self.ui, "power_monitor", None)
        if pm:
            pm.set_scope_mode(False)

    def _mic_scope_worker(self):
        import os, signal, subprocess, struct
        us_freqs = [17600 + i * 350 for i in range(18)]
        rust_bin = "/home/vipxpert/vipxpert_minimal_os/bin/mic_scope_dsp"
        # Comment (invariant): Prefer native Rust bin/mic_scope_dsp pinned to Cores 0,2 (<0.3% CPU, zero GIL) so 48kHz ultrasonic Goertzel + heterodyne mixing never contends with Core-1 OLED PWM or Core-2 UI loop.
        if os.path.isfile(rust_bin) and os.access(rust_bin, os.X_OK):
            ctl_path = "/tmp/vip_usonic_ctl"
            proc = None
            last_ctl = None
            try:
                cmd = [
                    "taskset", "-c", "0,2",
                    "runuser", "-u", "vipxpert", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000",
                    rust_bin,
                ]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=88, start_new_session=True)
                while self._usonic_running:
                    listen_flag = "1" if (self.usonic_listen and not getattr(self.ui, "is_screen_off", False)) else "0"
                    ctl_str = f"{listen_flag}|{int(self.usonic_carrier_hz)}|{int(self.usonic_gain_shift)}\n"
                    if ctl_str != last_ctl:
                        try:
                            with open(ctl_path + ".tmp", "w") as cf:
                                cf.write(ctl_str)
                            os.replace(ctl_path + ".tmp", ctl_path)
                            last_ctl = ctl_str
                        except Exception:
                            pass
                    pkt = proc.stdout.read(44)
                    if not pkt or len(pkt) < 44 or pkt[0] != 0x55 or pkt[1] != 0x53:
                        break
                    pk_idx = min(17, pkt[4])
                    pk_db = struct.unpack_from("<h", pkt, 6)[0] / 10.0
                    self.usonic_data.update({
                        "full_bins": list(pkt[8:26]),
                        "us_bins": list(pkt[26:44]),
                        "vu_l": pkt[2] / 255.0,
                        "vu_r": pkt[3] / 255.0,
                        "peak_hz": us_freqs[pk_idx],
                        "peak_db": max(-90.0, min(0.0, pk_db)),
                        "alert": bool(pkt[5]),
                    })
                return
            except Exception:
                pass
            finally:
                if proc is not None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except Exception:
                        try:
                            proc.terminate()
                        except Exception:
                            pass

        rec_cmd = [
            "runuser", "-u", "vipxpert", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000",
            "parec", "--device=vip_mic_bus.monitor", "--format=s16le", "--rate=48000", "--channels=2", "--latency-msec=40"
        ]
        play_cmd = [
            "runuser", "-u", "vipxpert", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000",
            "pacat", "--playback", "--format=s16le", "--rate=48000", "--channels=1", "--latency-msec=50"
        ]
        rec_p, play_p = None, None
        phase, lp_z1, lp_z2 = 0.0, 0.0, 0.0
        N = 256
        full_freqs = [250, 500, 800, 1200, 1800, 2500, 3500, 4800, 6400, 8000, 10000, 12000, 14000, 16000, 18000, 20000, 21500, 23000]
        us_freqs = [17600 + i * 350 for i in range(18)]
        full_c = [2.0 * math.cos(2.0 * math.pi * f / 48000.0) for f in full_freqs]
        us_c = [2.0 * math.cos(2.0 * math.pi * (17600 + i * 350) / 48000.0) for i in range(18)]
        try:
            rec_p = subprocess.Popen(rec_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=4096)
            chunk_bytes = 1024 * 4
            while self._usonic_running:
                raw = rec_p.stdout.read(chunk_bytes)
                if not raw or len(raw) < chunk_bytes:
                    break
                samples = struct.unpack("<2048h", raw)
                left = samples[0::2]
                right = samples[1::2]
                pk_l = max(abs(x) for x in left[:512]) / 32768.0
                pk_r = max(abs(x) for x in right[:512]) / 32768.0
                win = [(left[i] + right[i]) * 0.5 for i in range(N)]
                f_bins = []
                for c in full_c:
                    s1, s2 = 0.0, 0.0
                    for x in win:
                        s0 = x + c * s1 - s2
                        s2, s1 = s1, s0
                    mag = math.sqrt(max(1.0, s1 * s1 + s2 * s2 - c * s1 * s2)) / (N * 512.0)
                    f_bins.append(max(2, min(34, int(math.log10(1.0 + mag * 18.0) * 26.0))))
                u_bins = []
                best_idx, best_mag = 0, 1.0
                for idx, c in enumerate(us_c):
                    s1, s2 = 0.0, 0.0
                    for x in win:
                        s0 = x + c * s1 - s2
                        s2, s1 = s1, s0
                    mag = math.sqrt(max(1.0, s1 * s1 + s2 * s2 - c * s1 * s2))
                    if mag > best_mag:
                        best_mag, best_idx = mag, idx
                    norm_u = mag / (N * 256.0)
                    u_bins.append(max(2, min(34, int(math.log10(1.0 + norm_u * 28.0) * 28.0))))
                pk_db = 20.0 * math.log10(max(1e-5, best_mag / (N * 16384.0)))
                self.usonic_data.update({
                    "full_bins": f_bins,
                    "us_bins": u_bins,
                    "vu_l": min(1.0, pk_l * 2.5),
                    "vu_r": min(1.0, pk_r * 2.5),
                    "peak_hz": us_freqs[best_idx],
                    "peak_db": max(-90.0, min(0.0, pk_db)),
                    "alert": pk_db > -48.0,
                })
                if self.usonic_listen and not getattr(self.ui, "is_screen_off", False):
                    if play_p is None or play_p.poll() is not None:
                        play_p = subprocess.Popen(play_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    w_step = 2.0 * math.pi * float(self.usonic_carrier_hz) / 48000.0
                    gain = float(1 << self.usonic_gain_shift)
                    out_buf = bytearray(2048)
                    prev_x = 0.0
                    for i in range(1024):
                        cur_x = (left[i] + right[i]) * 0.5
                        hp = cur_x - prev_x
                        prev_x = cur_x
                        phase = (phase + w_step) % 6.2831853
                        mixed = hp * math.cos(phase) * gain
                        lp_z1 += 0.32 * (mixed - lp_z1)
                        lp_z2 += 0.32 * (lp_z1 - lp_z2)
                        struct.pack_into("<h", out_buf, i * 2, max(-32000, min(32000, int(lp_z2))))
                    try:
                        play_p.stdin.write(out_buf)
                        play_p.stdin.flush()
                    except Exception:
                        pass
                elif play_p is not None:
                    try:
                        play_p.terminate()
                    except Exception:
                        pass
                    play_p = None
        except Exception:
            pass
        finally:
            for p in (rec_p, play_p):
                if p is not None:
                    try:
                        p.terminate()
                    except Exception:
                        pass

    async def _ir_scope_worker(self):
        kernel = getattr(self.ui, "os_kernel", None)
        ir_hw = getattr(kernel, "ir_hardware", None) if kernel else None
        if not ir_hw:
            from src.hardware.ir_transceiver import IRTransceiver
            ir_hw = IRTransceiver()
        tokens = []
        try:
            async for data in ir_hw.stream_raw_continuous():
                if not self._ir_scope_running:
                    break
                if data == "SILENCE":
                    if tokens:
                        raw_b = " ".join(tokens)
                        pulses = ir_hw.parse_pulses(raw_b)
                        tokens.clear()
                        self.ir_flashing = False
                        if len(pulses) >= 6:
                            self.ir_pulses = pulses
                            self.ir_raw_str = " ".join(f"+{p}" if p > 0 else f"{p}" for p in pulses)
                            self.ir_pan_us = 0
                            self.ir_decoded = ir_hw.decode_protocol(pulses)
                        else:
                            self.ir_decoded = f"NOISE ({len(pulses)}e)"
                        self.ui.request_render()
                elif data.startswith("ERROR"):
                    self.ir_decoded = "IR RX OFFLINE"
                    self.ui.request_render()
                else:
                    tokens.append(data.replace("pulse ", "+").replace("space ", "-"))
                    self.ir_flashing = True
                    self.ui.request_render()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _ble_radar_worker(self):
        import subprocess, struct
        btmon_p, scan_p, play_p = None, None, None
        play_cmd = [
            "runuser", "-u", "vipxpert", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000",
            "pacat", "--playback", "--format=s16le", "--rate=48000", "--channels=1", "--latency-msec=40"
        ]
        cur_mac, cur_name, cur_tag = None, "", "BLE"
        last_click_t = 0.0
        try:
            scan_p = subprocess.Popen(["bluetoothctl", "--", "scan", "le"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            btmon_p = subprocess.Popen(["sudo", "stdbuf", "-oL", "btmon"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            while self._ble_running:
                line = btmon_p.stdout.readline()
                if not line:
                    break
                s = line.strip()
                now = time.monotonic()
                if s.startswith("Address:"):
                    parts = s.split()
                    if len(parts) >= 2:
                        cur_mac = parts[1].upper()
                        cur_name = ""
                        cur_tag = "RND" if "Random" in s else "BLE"
                elif s.startswith("Name ("):
                    cur_name = s.split(":", 1)[-1].strip()
                elif "Company:" in s:
                    if "Apple" in s or "0x004c" in s.lower():
                        cur_tag = "AIRTAG/APL"
                    elif "Samsung" in s or "0x0075" in s.lower():
                        cur_tag = "SMARTTAG"
                    elif "Tile" in s or "0x00c7" in s.lower() or "0xfeed" in s.lower():
                        cur_tag = "TILE"
                elif s.startswith("RSSI:") and cur_mac:
                    try:
                        rssi_val = int(s.split(":")[1].strip().split()[0])
                    except Exception:
                        rssi_val = -85
                    entry = self.ble_devices.get(cur_mac)
                    if not entry:
                        self.ble_devices[cur_mac] = {
                            "mac": cur_mac, "name": cur_name or cur_mac[-8:], "rssi": rssi_val,
                            "first_ts": now, "last_ts": now, "tag": cur_tag, "hist": [rssi_val]
                        }
                    else:
                        entry["rssi"] = int(entry["rssi"] * 0.4 + rssi_val * 0.6)
                        entry["last_ts"] = now
                        if cur_name:
                            entry["name"] = cur_name
                        if cur_tag not in ("BLE", "RND"):
                            entry["tag"] = cur_tag
                        entry["hist"].append(entry["rssi"])
                        if len(entry["hist"]) > 40:
                            entry["hist"].pop(0)
                stale = [k for k, v in self.ble_devices.items() if (now - v["last_ts"]) > 25.0]
                for k in stale:
                    self.ble_devices.pop(k, None)
                if self.ble_geiger and self.ble_devices and not getattr(self.ui, "is_screen_off", False):
                    ranked = sorted(self.ble_devices.values(), key=lambda x: x["rssi"], reverse=True)
                    idx = min(self.ble_sel_idx, len(ranked) - 1)
                    t_rssi = ranked[idx]["rssi"]
                    norm = max(0.0, min(1.0, (t_rssi + 95.0) / 60.0))
                    if (now - last_click_t) >= (1.0 - norm * 0.90):
                        last_click_t = now
                        if play_p is None or play_p.poll() is not None:
                            play_p = subprocess.Popen(play_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                        tick = bytearray(960 * 2)
                        for i in range(144):
                            amp = int(22000 * (1.0 - i / 144.0) * math.sin(2.0 * math.pi * 2400.0 * i / 48000.0))
                            struct.pack_into("<h", tick, i * 2, amp)
                        try:
                            play_p.stdin.write(tick)
                            play_p.stdin.flush()
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            subprocess.run(["bluetoothctl", "--", "scan", "off"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for p in (btmon_p, scan_p, play_p):
                if p is not None:
                    try:
                        p.terminate()
                    except Exception:
                        pass

    async def _handle_media_keys(self, action: str, key: str):
        if action in ("DOWN", "REPEAT"):
            if key == "2":
                await self.ui.adjust_pi_volume(1)
            elif key == "8":
                await self.ui.adjust_pi_volume(-1)
            elif key == "9":
                await self.ui.bt_daemon.adjust_bt_volume(4)
            elif key == "7":
                await self.ui.bt_daemon.adjust_bt_volume(-4)
            return
        if action != "CLICK":
            return
        if key == "1":
            await self.ui.media_arbiter.restart_dac()
        elif key == "5":
            state = self.ui.media_arbiter.get_now_playing()
            st = state.get("status", "stopped")
            if hasattr(st, "value"):
                st = st.value
            await self.ui.media_arbiter.send_action("Pause" if st == "playing" else "Play")
        elif key == "6":
            await self.ui.media_arbiter.send_action("Next")
        elif key == "4":
            await self.ui.media_arbiter.send_action("Previous")

    def _handle_interactive_3d_keys(self, action: str, key: str):
        if action not in ("DOWN", "REPEAT", "CLICK"):
            return
        if key == "4":
            self.cube_yaw -= 0.18
            self.cube_auto = False
        elif key == "6":
            self.cube_yaw += 0.18
            self.cube_auto = False
        elif key == "2":
            self.cube_pitch -= 0.18
            self.cube_auto = False
        elif key == "8":
            self.cube_pitch += 0.18
            self.cube_auto = False
        elif key == "5" and action == "CLICK":
            self.cube_auto = not self.cube_auto
        elif key == "1" and action == "CLICK":
            self.cube_mesh = (self.cube_mesh + 1) % 2
            self.ui.show_toast("Mesh: Octahedron" if self.cube_mesh == 1 else "Mesh: Cube", duration=1.1)
        elif key == "3" and action == "CLICK":
            modes = ["off", "pwm", "spatial", "temporal"]
            cur = getattr(self.display, "dither_mode", "off")
            nxt_d = modes[(modes.index(cur) + 1) % len(modes)] if cur in modes else "pwm"
            self.display.dither_mode = nxt_d
            self.ui.show_toast(f"Shade: {nxt_d.upper()}", duration=1.1)
        elif key == "7":
            self.cube_scale = max(10.0, self.cube_scale - 2.0)
        elif key == "9":
            self.cube_scale = min(30.0, self.cube_scale + 2.0)
        elif key == "0" and action == "CLICK":
            self.cube_pitch, self.cube_yaw, self.cube_scale, self.cube_auto = 0.4, 0.6, 20.0, True
        self.ui.request_render()

    def _handle_power_scope_keys(self, action: str, key: str):
        if action != "CLICK":
            return
        pm = getattr(self.ui, "power_monitor", None)
        if key in ("1", "2", "3"):
            self.scope_view = int(key) - 1
        elif key in ("4", "6"):
            self.scope_view = (self.scope_view + (1 if key == "6" else -1)) % 3
        elif key == "5" and pm:
            pm.scope_paused = not pm.scope_paused
        elif key == "0" and pm:
            pm.reset_scope_stats()
        elif key in ("7", "9"):
            strides = [1, 2, 4]
            idx = strides.index(self.scope_stride) if self.scope_stride in strides else 0
            self.scope_stride = strides[(idx + (1 if key == "9" else -1)) % len(strides)]
        self.ui.request_render()

    def _handle_mic_scope_keys(self, action: str, key: str):
        if action not in ("CLICK", "REPEAT"):
            return
        if key in ("1", "2", "3") and action == "CLICK":
            self.usonic_view = int(key) - 1
        elif key == "5" and action == "CLICK":
            self.usonic_listen = not self.usonic_listen
        elif key == "4":
            self.usonic_carrier_hz = max(17000, self.usonic_carrier_hz - 500)
        elif key == "6":
            self.usonic_carrier_hz = min(22500, self.usonic_carrier_hz + 500)
        elif key == "7" and action == "CLICK":
            self.usonic_gain_shift = max(0, self.usonic_gain_shift - 1)
        elif key == "9" and action == "CLICK":
            self.usonic_gain_shift = min(5, self.usonic_gain_shift + 1)
        elif key == "0" and action == "CLICK":
            self.usonic_carrier_hz, self.usonic_gain_shift, self.usonic_listen = 19500, 2, False
        self.ui.request_render()

    async def _handle_ir_scope_keys(self, action: str, key: str):
        if action not in ("CLICK", "REPEAT"):
            return
        if key == "4":
            self.ir_pan_us = max(0, self.ir_pan_us - (self.ir_zoom_us // 4))
        elif key == "6":
            self.ir_pan_us = min(200000, self.ir_pan_us + (self.ir_zoom_us // 4))
        elif key == "7" and action == "CLICK":
            opts = [15000, 30000, 45000, 90000]
            idx = opts.index(self.ir_zoom_us) if self.ir_zoom_us in opts else 2
            self.ir_zoom_us = opts[max(0, idx - 1)]
        elif key == "9" and action == "CLICK":
            opts = [15000, 30000, 45000, 90000]
            idx = opts.index(self.ir_zoom_us) if self.ir_zoom_us in opts else 2
            self.ir_zoom_us = opts[min(len(opts) - 1, idx + 1)]
        elif key == "0" and action == "CLICK":
            self.ir_pulses.clear()
            self.ir_raw_str, self.ir_pan_us, self.ir_decoded = "", 0, "CLEARED"
        elif key == "5" and action == "CLICK" and self.ir_raw_str:
            kernel = getattr(self.ui, "os_kernel", None)
            ir_hw = getattr(kernel, "ir_hardware", None) if kernel else None
            if ir_hw:
                self.ir_decoded = "TX BLASTING..."
                self.ui.request_render()
                try:
                    await ir_hw.send_raw(self.ir_raw_str)
                    self.ir_decoded = "TX BLAST OK!"
                except Exception:
                    self.ir_decoded = "TX ERROR"
        self.ui.request_render()

    def _handle_ble_radar_keys(self, action: str, key: str):
        if action != "CLICK":
            return
        if key in ("1", "2"):
            self.ble_view = int(key) - 1
        elif key == "5":
            self.ble_geiger = not self.ble_geiger
        elif key in ("2", "4"):
            self.ble_sel_idx = max(0, self.ble_sel_idx - 1)
        elif key in ("8", "6"):
            self.ble_sel_idx = min(max(0, len(self.ble_devices) - 1), self.ble_sel_idx + 1)
        elif key == "0":
            self.ble_devices.clear()
            self.ble_sel_idx = 0
        self.ui.request_render()

    def _handle_ambient_physics_keys(self, scene_id: str, action: str, key: str):
        self._ensure_ambient_physics()
        if action not in ("DOWN", "REPEAT", "CLICK"):
            return
        if scene_id == "ambient_pond":
            if action == "CLICK" and key in "123456789":
                idx = int(key) - 1
                gx = 22 + (idx % 3) * 42 + random.randint(-6, 6)
                gy = 18 + (idx // 3) * 18 + random.randint(-4, 4)
                self.pond_ripples.append([float(gx), float(gy), 1.0, 32.0])
            elif action == "CLICK" and key == "0":
                self.pond_storm = 8
                for _ in range(5):
                    self.pond_ripples.append([float(random.randint(12, 116)), float(random.randint(16, 56)), 1.0, 34.0])
        elif scene_id == "ambient_stars":
            if key == "4":
                self.star_cx = max(16.0, self.star_cx - 6.0)
            elif key == "6":
                self.star_cx = min(112.0, self.star_cx + 6.0)
            elif key == "2":
                self.star_cy = max(10.0, self.star_cy - 5.0)
            elif key == "8":
                self.star_cy = min(54.0, self.star_cy + 5.0)
            elif key == "7":
                self.star_speed = max(0.4, self.star_speed - 0.4)
            elif key == "9":
                self.star_speed = min(4.5, self.star_speed + 0.4)
            elif key == "5" and action == "CLICK":
                for _ in range(3):
                    self.meteors.append([float(random.randint(20, 124)), float(random.randint(2, 24)), -4.5, 2.6, 12])
            elif key == "0" and action == "CLICK":
                self.star_cx, self.star_cy, self.star_speed = 64.0, 32.0, 1.5
        elif scene_id == "ambient_bounce":
            if key == "4":
                self.bnc_vx = -abs(self.bnc_vx)
            elif key == "6":
                self.bnc_vx = abs(self.bnc_vx)
            elif key == "2":
                self.bnc_vy = -abs(self.bnc_vy)
            elif key == "8":
                self.bnc_vy = abs(self.bnc_vy)
            elif key == "5" and action == "CLICK":
                self.bnc_vx = random.choice([-2.4, -1.8, 1.8, 2.4])
                self.bnc_vy = random.choice([-1.8, -1.3, 1.3, 1.8])
            elif key == "1" and action == "CLICK":
                self.bnc_shape = (self.bnc_shape + 1) % 3
            elif key == "0" and action == "CLICK":
                self.bnc_x = float(127 - self.bnc_w - 18)
                self.bnc_y = float(63 - self.bnc_h - 18)
                self.bnc_vx, self.bnc_vy = 1.5, 1.5
        elif scene_id == "ambient_grass":
            if key == "4":
                self.grass_target_wind = max(-2.6, self.grass_target_wind - 0.5)
            elif key == "6":
                self.grass_target_wind = min(2.6, self.grass_target_wind + 0.5)
            elif key == "2":
                self.grass_height = min(36, self.grass_height + 3)
            elif key == "8":
                self.grass_height = max(10, self.grass_height - 3)
            elif key == "5" and action == "CLICK":
                for _ in range(3):
                    self.fireflies.append([float(random.randint(8, 120)), float(random.randint(6, 40)), random.uniform(0, 6.28)])
                self.fireflies = self.fireflies[-14:]
            elif key == "0" and action == "CLICK":
                self.grass_target_wind, self.grass_height = 0.5, 22
        self.ui.request_render()

    async def handle_key(self, action: str, key: str):
        if getattr(self.ui, "is_screen_off", False):
            await self._handle_media_keys(action, key)
            return

        now = time.monotonic()
        if self.active_slot != "screensaver":
            self.last_slot_input = now

        # Universal '#' Key: Deterministic 3-State Cycler:
        #   Step 1: Pinned 2-Line Keypad Guide Card (with transition)
        #   Step 2: Pinned Scene Title Pill (with transition)
        #   Step 3: Smooth Dissolve Out to 100% Clean Canvas
        if key == "#" and action in ("CLICK", "DOWN"):
            if action == "DOWN":
                return
            self._manual_hud_peek = True
            self._guide_tuck_deadline = 0.0
            self._last_banner_slot = self.active_slot
            self._last_banner_scene = self._get_active_scene_id()
            cur_step = getattr(self, "_hash_step", 0)
            vis_now = getattr(self, "_pill_currently_visible", False)
            if not vis_now and cur_step not in (1, 2):
                nxt_step = 1
            elif cur_step == 0:
                nxt_step = 1
            elif cur_step == 1:
                nxt_step = 2
            elif cur_step == 2:
                nxt_step = 3
            else:
                nxt_step = 1
            self._last_pill_before_fade = "title" if cur_step == 2 else "guide"
            self._trigger_banner_intro(hash_step=nxt_step)
            return

        if self.standalone_scene is not None:
            # Stage: Preview Navigation & A/B/C/D Scene-Override Guard (Locks CLICK C=Save/HUD, CLICK D=Cancel/Back, CLICK A/B=Switch; allows HOLD C Fullscreen test)
            # Comment (invariant): Never pass CLICK A, B, C, or D down to plugin.handle_key() while standalone_scene preview is active, so scenes using C (Fullscreen) or D (MV toggle) never conflict with C=Confirm/Save or D=Cancel/Back.
            if key in ("A", "B", "C", "D"):
                if key == "C" and action == "HOLD" and self.standalone_scene in ("music_split", "music_viz", "ambient_hud", "amb_music_hybrid", "music_classic"):
                    self.viz_fullscreen = not self.viz_fullscreen
                    self._latched_geom_key_M = None
                    self.ui.show_toast("Preview: FullScrn" if self.viz_fullscreen else "Preview: HUD View", duration=1.15)
                    return
                if action != "CLICK":
                    return
                self.viz_fullscreen = False
                parent_app = self.ui.active_apps.get(self.parent_app_id) if self.parent_app_id else None
                if key == "D" and self.parent_app_id:
                    if parent_app and getattr(parent_app, "current_menu", "") == "lock_cat_scenes" and self.category_scenes:
                        if self.standalone_scene in self.category_scenes:
                            parent_app.cursor_index = self.category_scenes.index(self.standalone_scene)
                    await self.ui.switch_app(self.parent_app_id)
                    if parent_app and hasattr(parent_app, "_rebuild_menu"):
                        parent_app._rebuild_menu(preserve_cursor=True)
                    return
                if key == "C":
                    if self.pick_db_key:
                        self.ui.db.set_setting(self.pick_db_key, self.standalone_scene)
                        if parent_app and getattr(parent_app, "current_menu", "") == "lock_cat_scenes":
                            parent_app.current_menu = "lock_slots"
                            slot_cur = getattr(parent_app, "_pick_slot_cursor", None)
                            if slot_cur is not None:
                                parent_app.cursor_index = slot_cur
                        await self.ui.switch_app(self.parent_app_id)
                        if parent_app and hasattr(parent_app, "_rebuild_menu"):
                            parent_app._rebuild_menu(preserve_cursor=True)
                        if parent_app and hasattr(parent_app, "show_info_toast"):
                            sc_lbl = SCENE_LABELS.get(self.standalone_scene, self.standalone_scene)
                            slot_tag = f"{self.pick_slot_label}: " if self.pick_slot_label else "Set: "
                            parent_app.show_info_toast(f"{slot_tag}{sc_lbl} ★", duration=1.8)
                        return
                    else:
                        # In Per-Scene Setup -> Live Preview, 'C' cycles HUD Behavior live on screen!
                        opts = ["global", "title", "pin_guide", "clean"]
                        cur_b = self.ui.db.get_setting(f"scene_hud_beh_{self.standalone_scene}", "global")
                        nxt_b = opts[(opts.index(cur_b) + 1) % len(opts)] if cur_b in opts else "global"
                        self.ui.db.set_setting(f"scene_hud_beh_{self.standalone_scene}", nxt_b)
                        if parent_app and hasattr(parent_app, "_rebuild_menu"):
                            parent_app._rebuild_menu(preserve_cursor=True)
                        self._hud_cfg_ts = 0.0
                        self._guide_tuck_deadline = 0.0
                        self._trigger_banner_intro(hash_step=1 if nxt_b == "pin_guide" else (3 if nxt_b == "clean" else 0))
                        return
                if key in ("A", "B"):
                    ids = self.category_scenes if self.category_scenes else [cid for cid, _ in SCENE_CATALOG]
                    if not ids:
                        return
                    idx = ids.index(self.standalone_scene) if self.standalone_scene in ids else 0
                    step = 1 if key == "B" else -1
                    self.standalone_scene = ids[(idx + step) % len(ids)]
                    if parent_app and getattr(parent_app, "current_menu", "") == "lock_cat_scenes" and self.standalone_scene in ids:
                        parent_app.cursor_index = ids.index(self.standalone_scene)
                    self._manual_hud_peek = False
                    self._guide_tuck_deadline = 0.0
                    self._hud_cfg_ts = 0.0
                    self._last_banner_scene = self.standalone_scene
                    self._load_scene_individual_params()
                    self._trigger_banner_intro(hash_step=0)
                    await self.update()
                    return
                return
        else:
            if action == "CLICK" and key in ("A", "B"):
                self.viz_fullscreen = False
                self.last_slot_input = now
                slots = self._get_enabled_slots()
                if len(slots) > 1:
                    idx = slots.index(self.active_slot) if self.active_slot in slots else 0
                    step = 1 if key == "B" else -1
                    self.active_slot = slots[(idx + step) % len(slots)]
                self.manual_override = True
                self.last_cycle_tick = now
                self._manual_hud_peek = False
                self._guide_tuck_deadline = 0.0
                self._last_banner_slot = self.active_slot
                self._last_banner_scene = self._get_active_scene_id()
                self._trigger_banner_intro(hash_step=0)
                await self.update()
                return
            # Stage: Key D Exit-Fullscreen / MV-HUD Toggle (Exits Fullscreen Visualizer first if active; otherwise toggles MV/HUD)
            if action == "CLICK" and key == "D":
                if self.viz_fullscreen:
                    self.viz_fullscreen = False
                    self.ui.show_toast("Viz: HUD View", duration=1.1)
                    await self.update()
                    return
                if self.active_slot in ("music", "amb_music"):
                    self._mv_suppressed = not getattr(self, "_mv_suppressed", False)
                    self._sc_id_ts = 0.0
                    self._manual_hud_peek = False
                    self._guide_tuck_deadline = 0.0
                    await self.update()
                    return

        scene_id = self._get_active_scene_id()
        # Smart 2.5s Post-Interaction Guide Auto-Tuck:
        # When user presses an interactive 0-9 key while the '#' Guide (Step 1) is up, keep the Guide visible
        # while they are actively tapping keys, then auto-dissolve it 2.5 seconds after their last tap!
        if action == "CLICK" and key in "0123456789" and getattr(self, "_hash_step", 0) == 1:
            if self._get_scene_hud_behavior(scene_id) != "pin_guide":
                self._guide_tuck_deadline = now + 2.5
                self.display.has_active_marquee = True

        # Scene Plugin Key Dispatch (Routes input to active ScenePlugin and falls back to media controls)
        plugin = SceneManager.get_instance(scene_id, self.display, self.ui, self)
        handled = False
        if plugin:
            res = plugin.handle_key(action, key)
            handled = (await res) if asyncio.iscoroutine(res) else bool(res)
        if not handled:
            await self._handle_media_keys(action, key)

    async def update(self):
        if getattr(self.ui, "is_screen_off", False):
            return
        if self.standalone_scene is None:
            is_playing = self._get_media_playing()
            has_music = self._has_music_session()
            is_amb = bool(getattr(self.ui, "transparency_enabled", False))
            slots = self._get_enabled_slots()
            if self.active_slot not in slots:
                self.active_slot = slots[0]

            ab_policy = self.ui.db.get_setting("lock_ab_policy", "reset")
            saver_policy = self.ui.db.get_setting("lock_saver_policy", "idle_only")
            allow_auto = not (self.manual_override and ab_policy == "pin")

            now = time.monotonic()
            prev_sess = getattr(self, "_prev_music_session", has_music)
            sess_changed = (has_music != prev_sess) or (is_amb != self._prev_amb)
            play_changed = (is_playing != self._prev_playing)
            rising_wake = (is_playing and not self._prev_playing) or (has_music and not prev_sess) or (is_amb and not self._prev_amb)
            self._prev_playing = is_playing
            self._prev_music_session = has_music
            self._prev_amb = is_amb

            if play_changed:
                self.last_slot_input = now

            if allow_auto:
                target_pri = self._resolve_priority_slot(has_music, is_amb, slots)
                if rising_wake:
                    self.manual_override = False
                    self.last_slot_input = now
                    self.active_slot = target_pri
                elif sess_changed:
                    self.manual_override = False
                    self.last_slot_input = now
                    self.active_slot = target_pri

                saver_blocked = False
                if saver_policy == "idle_only" and (is_playing or is_amb):
                    saver_blocked = True
                elif saver_policy == "pause_only" and is_playing:
                    saver_blocked = True

                if saver_blocked:
                    self.last_slot_input = now
                    if self.active_slot == "screensaver" and not self.manual_override:
                        self.active_slot = target_pri
                else:
                    idle_ref = self.last_slot_input if ab_policy != "strict" else self.last_cycle_tick
                    idle_lim = self._get_slot_idle_limit(self.active_slot)
                    cycle_sec = int(self.ui.db.get_setting("lock_cycle_sec", "0"))
                    if cycle_sec > 0 and (now - self.last_cycle_tick) >= cycle_sec and len(slots) > 1:
                        idx = slots.index(self.active_slot) if self.active_slot in slots else 0
                        self.active_slot = slots[(idx + 1) % len(slots)]
                        self.last_cycle_tick = now
                    elif "screensaver" in slots and idle_lim > 0 and self.active_slot != "screensaver":
                        if (now - idle_ref) >= idle_lim:
                            self.active_slot = "screensaver"

        # Stage: MV Suppression Reset (Clears session suppression once the user exits the music slot)
        if self.standalone_scene is None and self.active_slot not in ("music", "amb_music"):
            self._mv_suppressed = False

        self._sync_scope_burst()
        self.layout.hide_header = True
        scene_id = self._get_active_scene_id()
        if self._last_banner_slot is None:
            self._last_banner_slot = self.active_slot
        if self._last_banner_scene is None:
            self._last_banner_scene = scene_id
        if scene_id != self._last_banner_scene or self.active_slot != self._last_banner_slot:
            self._last_banner_scene = scene_id
            self._last_banner_slot = self.active_slot
            self._manual_hud_peek = False
            self._load_scene_individual_params()
            self._trigger_banner_intro(hash_step=0)
        self.layout.footer_text = ""
        self.layout.body_lines = [("", "", "")]
        self.ui.request_render()

    def _draw_right_aligned_text(self, right_x: int, y: int, text_s: str):
        """Draws crisp right-aligned small text ending cleanly at right_x so right-column HUD items align vertically."""
        if not text_s:
            return
        font = self.display.font_small
        tw = int(round(font.getlength(text_s))) if hasattr(font, "getlength") else len(text_s) * 6
        tx = max(0, right_x - tw)
        self.display.draw_text(tx, y, text_s)

    def _draw_music_top_right_battery(self):
        """Delegates fixed top-right battery percentage badge to single-source UIManager.draw_top_right_battery()."""
        if self.ui.db.get_setting("music_top_bat_overlay", "1") == "0":
            return
        self.ui.draw_top_right_battery()

    def _extract_media_info(self):
        now_m = time.monotonic()
        if now_m - getattr(self, "_media_info_ts", 0.0) < 0.025 and hasattr(self, "_cached_media_info"):
            return self._cached_media_info
        self._media_info_ts = now_m
        media = self.ui.media_arbiter.get_now_playing()
        raw_status = media.get("status", "stopped")
        status_str = str(raw_status.value if hasattr(raw_status, "value") else raw_status)
        raw_title = media.get("title", "No Media")
        title_str = str(raw_title.value if hasattr(raw_title, "value") else raw_title)
        raw_artist = media.get("artist", "")
        artist_val = raw_artist.value if hasattr(raw_artist, "value") else raw_artist
        artist_str = ", ".join(str(a) for a in artist_val) if isinstance(artist_val, list) else str(artist_val)
        raw_pos = media.get("position", 0.0)
        pos = float(raw_pos.value if hasattr(raw_pos, "value") else raw_pos)
        raw_dur = media.get("duration", 0.0)
        dur = float(raw_dur.value if hasattr(raw_dur, "value") else (raw_dur or 0.0))
        raw_tr = media.get("transport_state")
        tr_str = str(raw_tr.value if hasattr(raw_tr, "value") else raw_tr)
        is_active_tr = (tr_str == "active") if media.get("source") == "bt" else True
        if status_str == "playing" and is_active_tr:
            raw_upd = media.get("updated_at", time.time())
            pos += max(0.0, time.time() - float(raw_upd.value if hasattr(raw_upd, "value") else raw_upd))
        # Comment (invariant): Never clamp pos to dur here; busctl introspection proved Android players like YMusic often report a previous track's shorter Duration (e.g. 141.86s on 219s Bad Apple), and clamping pos to dur freezes MusicVideoScene at that exact timestamp until the song ends.
        pos = max(0.0, pos)
        raw_codec = media.get("codec", "N/A")
        codec_str = str(raw_codec.value if hasattr(raw_codec, "value") else raw_codec)
        bt_vol = media.get("bt_volume")
        bt_str = f"{int((bt_vol / 127) * 100)}%" if bt_vol is not None else "N/A"
        self._cached_media_info = (status_str, title_str, artist_str, pos, dur, codec_str, bt_str)
        return self._cached_media_info

    def render_visualizer_viewport(self, x1: int, y_top: int, x2: int, y_bot: int, active: bool, max_bars: int = 20) -> bool:
        """
        Single-source DRY visualizer viewport renderer for all scenes.
        Expands to full 128x64 canvas (2..125, 2..61) whenever `self.viz_fullscreen` is active,
        and promotes Display.show() to 8-page `shm_pwm=2` so particles in Pages 0,1,6,7 (left/right edges) never vanish.
        """
        sc_id = self._get_active_scene_id()
        is_fs = bool(self.viz_fullscreen)
        if is_fs:
            x1, y_top, x2, y_bot = 2, 2, 125, 61
            max_bars = 20

        mode = self.get_effective_viz_mode(sc_id)
        cfg_bands = min(max_bars, self.get_effective_viz_bands(sc_id))

        if mode == "stereo_wave":
            mid_x = (x1 + x2) // 2
            half_harm = max(5, cfg_bands // 2)
            self._draw_sine_visualizer(x1, y_top, mid_x - 2, y_bot, active, harmonics=half_harm, bins_src=self.viz_fast_l, wave_src=self.viz_wave_l, phase_tag="L")
            self.display.draw.line((mid_x, y_top, mid_x, y_bot), fill=1)
            self._draw_sine_visualizer(mid_x + 3, y_top, x2, y_bot, active, harmonics=half_harm, bins_src=self.viz_fast_r, wave_src=self.viz_wave_r, phase_tag="R")
        elif mode == "sine_wave":
            self._draw_sine_visualizer(x1, y_top, x2, y_bot, active, harmonics=cfg_bands, bins_src=self.viz_fast_m, wave_src=self.viz_wave, phase_tag="M")
        elif mode == "stereo":
            half_bars = max(3, cfg_bands // 2)
            mid_x = (x1 + x2) // 2
            self._draw_spectrum_bars(x1, y_top, mid_x - 2, y_bot, active, bars=half_bars, bins_override=self.viz_bins_l, chan_key="L")
            self.display.draw.line((mid_x, y_top, mid_x, y_bot), fill=1)
            self._draw_spectrum_bars(mid_x + 3, y_top, x2, y_bot, active, bars=half_bars, bins_override=self.viz_bins_r, chan_key="R")
        else:
            self._draw_spectrum_bars(x1, y_top, x2, y_bot, active, bars=cfg_bands)

        # Comment (invariant): Display._flush_precompiled_pages skips OLED Pages 0, 1, 6, 7 during ph0/ph1 when shm_pwm==1 (optimized for y=16..47 HUD viewports), which erases PWM gray particles on quiet outer left/right bars (y=48..61) and tall apexes (y=2..15) in Fullscreen; seeding _pwm_gray_planes promotes Display.show() to 8-page shm_pwm=2 while still applying _pwm_rects shades onto ph1/lo.
        if is_fs and getattr(self.display, "_pwm_rects", None) and getattr(self.display, "_pwm_gray_planes", None) is None:
            base_copy = self.display.image.copy()
            self.display._pwm_gray_planes = (base_copy, base_copy.copy(), ("fs_viz", getattr(self.display, "_frame_seq", 0), tuple(self.display._pwm_rects)))
        return is_fs

    def _draw_sine_visualizer(self, x1: int, y_top: int, x2: int, y_bot: int, active: bool, harmonics: int = 16, bins_src=None, wave_src=None, phase_tag: str = "M"):
        """
        Renders live HOLD-'3' A/B-selectable standard oscilloscope modes (proven in CR-668):
          - 'triggered_pcm' ★: Corrscope low-pass correlation-triggered real-PCM waveform (44 pts over 36.7ms/73.3ms)
          - 'pitch_silk':      YIN autocorrelation fundamental pitch f0 silky wave + real HF shimmer
        """
        sc_id = self._get_active_scene_id()
        show_live = (active or sc_id in ("ambient_hud", "amb_music_hybrid")) and self._viz_running
        ph, _ = self.advance_scene_phase(f"viz_sine_wave_{phase_tag}", ref_fps=30.0)
        bins20 = list(bins_src if bins_src is not None else getattr(self, "viz_fast_m", self.viz_bins))
        if len(bins20) < 20:
            bins20 = [0.0] * 20
        w_raw = list(wave_src if wave_src is not None else getattr(self, "viz_wave", [0.0] * 120))
        pts120 = [(float(p[1]) if isinstance(p, tuple) else float(p)) for p in w_raw]
        if len(pts120) < 120:
            pts120 = [0.0] * 120
        bar_style = self._get_effective_bar_style(sc_id)
        scope_algo = self.ui.db.get_setting("lock_viz_scope_algo", "triggered_pcm")

        # Comment (invariant): Unstretched 1:1 Pixel-Pitch Oscilloscope Viewport (N008 + user Stereo invariant): NEVER squeeze 120 points into 58px Stereo viewports! Both Mono (120px) and Stereo L|R (58px) map 1.0 point per pixel centered around pts120[60] (the Rust rising-edge trigger anchor), while Key '3' switches the actual Rust 48kHz capture timebase (Auto / 20ms / 10ms / 5ms / 2.5ms)!
        mid_y = (y_top + y_bot) * 0.5
        half_h = max(3.0, (y_bot - y_top) * 0.48)
        w_cols = max(12, int(x2) - int(x1))
        pk_all = max(bins20) if show_live else 0.0
        w_span = (max(pts120) - min(pts120)) if show_live else 0.0
        if show_live or pk_all > 0.02 or w_span > 0.04:
            self.display.has_active_marquee = True

        if scope_algo == "triggered_pcm":
            mid_yi = int(round(mid_y))
            if not hasattr(self, "_wave_disp_state"):
                self._wave_disp_state = {}
                self._wave_disp_ts = {}
            if not show_live or w_span <= 0.02:
                self._wave_disp_state[phase_tag] = [0.0] * 120
                if bar_style == "led":
                    for col_i in range(0, w_cols + 1, 2):
                        self.display.draw.point((x1 + col_i, mid_yi), fill=1)
                else:
                    self.display.draw.line((x1, mid_yi, x2, mid_yi), fill=1)
                return
            # Asymmetric Analog Phosphor Interpolation: tau_attack = 3.2ms (~99% instant punch on outward crests -> ZERO
            # transient damping!) + tau_release = 8.5ms (silky 60..120FPS sub-frame glide between 46.9Hz DSP packets!):
            now_w = time.monotonic()
            dt_w = max(0.002, min(0.05, now_w - self._wave_disp_ts.get(phase_tag, now_w - 0.016)))
            self._wave_disp_ts[phase_tag] = now_w
            a_atk = 1.0 - math.exp(-dt_w / 0.0032)
            a_rel = 1.0 - math.exp(-dt_w / 0.0085)
            prev_disp = self._wave_disp_state.get(phase_tag)
            if prev_disp is None or len(prev_disp) != 120:
                disp120 = list(pts120)
            else:
                disp120 = [
                    prev_disp[i] + (pts120[i] - prev_disp[i]) * (a_atk if abs(pts120[i]) > abs(prev_disp[i]) else a_rel)
                    for i in range(120)
                ]
            self._wave_disp_state[phase_tag] = disp120

            span_pts = float(min(119, w_cols))
            start_pt = (119.0 - span_pts) * 0.5
            prev_y = None
            for col_i in range(w_cols + 1):
                pos = start_pt + (col_i / float(w_cols)) * span_pts
                idx = min(118, max(0, int(pos)))
                frac = pos - idx
                sig = disp120[idx] * (1.0 - frac) + disp120[idx + 1] * frac
                px = x1 + col_i
                y_cur = int(round(max(y_top, min(y_bot, mid_y - sig * half_h))))
                if bar_style == "shaded" and (col_i & 1) == 0:
                    ya, yb = (y_cur, mid_yi) if y_cur <= mid_yi else (mid_yi, y_cur)
                    if yb - ya >= 2:
                        self.display.draw_shaded_rect(px, ya, min(x2, px + 1), yb, shade=0.50, phase_id=col_i)
                if bar_style == "led":
                    if (col_i & 1) == 0:
                        self.display.draw.point((px, y_cur), fill=1)
                else:
                    if prev_y is not None:
                        if abs(y_cur - prev_y) <= 6:
                            self.display.draw.line((px - 1, prev_y, px, y_cur), fill=1)
                        else:
                            ym = (prev_y + y_cur) // 2
                            self.display.draw.line((px - 1, prev_y, px - 1, ym), fill=1)
                            self.display.draw.line((px, ym, px, y_cur), fill=1)
                    else:
                        self.display.draw.point((px, y_cur), fill=1)
                prev_y = y_cur
            return

        raw_vals = []
        scale = 1.0
        if not show_live or pk_all <= 0.008:
            raw_vals = [0.0] * (w_cols + 1)
        else:
            f0_attr = "viz_f0_l" if phase_tag == "L" else ("viz_f0_r" if phase_tag == "R" else "viz_f0_m")
            f0_hz = float(getattr(self, f0_attr, 0.0))
            h_scale = max(0.75, min(1.15, float(harmonics) / 12.0))
            max_cyc = max(3.5, min(25.0, (w_cols / 5.2) * h_scale))
            if f0_hz > 15.0:
                cyc_f0 = max(0.30, min(max_cyc, f0_hz * 0.0367 * h_scale))
            elif bins20[0] > 0.10 and max(bins20[2:20]) < 0.05:
                cyc_f0 = 0.24
            else:
                pk_i = bins20.index(pk_all)
                cyc_f0 = 0.85 + (max_cyc - 0.85) * ((pk_i / 19.0) ** 1.35)
            hi_e = max(bins20[13:20])
            amp_env = min(1.0, pk_all * 1.05)
            two_pi = 2.0 * math.pi
            for col_i in range(w_cols + 1):
                u = col_i / float(w_cols)
                v = math.sin(two_pi * cyc_f0 * u - ph * 0.65)
                if hi_e > 0.15 and cyc_f0 < max_cyc * 0.65:
                    v = v * 0.72 + 0.35 * hi_e * math.sin(two_pi * max_cyc * u + ph * 1.1)
                raw_vals.append(v * amp_env)

        prev_hi = None
        prev_lo = None
        mid_yi = int(round(mid_y))

        for col_i, rv in enumerate(raw_vals):
            px = x1 + col_i
            sig = max(-1.0, min(1.0, rv * scale))
            y_up = int(round(max(y_top, min(y_bot, mid_y - sig * half_h))))
            y_dn = int(round(max(y_top, min(y_bot, mid_y + sig * half_h * 0.62))))

            if bar_style == "shaded" and (col_i & 1) == 0:
                ya, yb = (y_up, mid_yi) if y_up <= mid_yi else (mid_yi, y_up)
                if yb - ya >= 2:
                    self.display.draw_shaded_rect(px, ya, min(x2, px + 1), yb, shade=0.50, phase_id=col_i)

            if bar_style == "led":
                if (col_i & 1) == 0:
                    self.display.draw.point((px, y_up), fill=1)
            else:
                if prev_hi is not None:
                    self.display.draw.line((px - 1, prev_hi, px, y_up), fill=1)
                else:
                    self.display.draw.point((px, y_up), fill=1)
                if bar_style in ("hollow", "shaded"):
                    if prev_lo is not None:
                        self.display.draw.line((px - 1, prev_lo, px, y_dn), fill=1)
                    else:
                        self.display.draw.point((px, y_dn), fill=1)
            prev_hi = y_up
            prev_lo = y_dn

    def _draw_spectrum_bars(self, x1: int, y_top: int, x2: int, y_bot: int, active: bool, bars: int = 16, bins_override=None, chan_key: str = "M"):
        sc_id = self._get_active_scene_id()
        show_live = (active or sc_id in ("ambient_hud", "amb_music_hybrid")) and self._viz_running
        live_bins = (bins_override if bins_override is not None else getattr(self, "viz_bins", None)) or [0.05] * 20
        pk_attr = f"_viz_peaks_{chan_key}"
        hd_attr = f"_viz_holds_{chan_key}"
        fl_attr = f"_viz_falls_{chan_key}"
        peaks = getattr(self, pk_attr, None)
        peak_holds = getattr(self, hd_attr, None)
        fall_ticks = getattr(self, fl_attr, None)
        if not peaks or len(peaks) != 20:
            peaks = [0.05] * 20
            setattr(self, pk_attr, peaks)
            self.viz_peaks = peaks
        if not peak_holds or len(peak_holds) != 20:
            peak_holds = [0.0] * 20
            setattr(self, hd_attr, peak_holds)
        if not fall_ticks or len(fall_ticks) != 20:
            fall_ticks = [0.0] * 20
            setattr(self, fl_attr, fall_ticks)

        now_v = time.monotonic()
        # Cache SQLite visualizer style/physics lookups for 0.5s instead of querying SQLite 13 times per frame!
        if now_v - getattr(self, "_viz_cfg_ts", 0.0) > 0.5 or getattr(self, "_viz_cfg_sc", None) != sc_id:
            self._viz_cfg_ts = now_v
            self._viz_cfg_sc = sc_id
            self._viz_cached_bar = self._get_effective_bar_style(sc_id)
            self._viz_cached_cap = self._get_effective_cap_style(sc_id)
            self._viz_cached_phys = self._get_effective_particle_physics(sc_id)
        bar_style = self._viz_cached_bar
        cap_style = self._viz_cached_cap
        hold_frames, exp_p, v0_drift, g_coeff = self._viz_cached_phys

        raw_dt = now_v - getattr(self, "_last_viz_frame_t", now_v - 0.0083)
        self._last_viz_frame_t = now_v
        phys_dt = now_v - getattr(self, "_last_viz_phys_t", 0.0)
        shm_v = getattr(self.display, "_shm", None)
        if shm_v is not None:
            import struct as _stv
            pwm_m_v = _stv.unpack("<I", shm_v[8:12])[0]
            hw_tk_v = _stv.unpack("<I", shm_v[20:24])[0]
        else:
            pwm_m_v, hw_tk_v = 0, getattr(self.display, "_frame_seq", 0)
        # Comment (invariant): When 3-phase UI PWM is active (pwm_m_v == 1), Core-1 latches shm_ver strictly at 51.73Hz (pwm_phase == 2, every 19.33ms); stepping bar geometry once per hw_tk_v eliminates 120Hz vs 51.73Hz beat-aliasing on falling bars/caps and skips redundant intermediate 3-plane compilations.
        tk_attr = f"_last_viz_hw_tk_{chan_key}"
        lb_attr = f"_latched_viz_bins_{chan_key}"
        ph_attr = f"_last_viz_phys_t_{chan_key}"
        phys_dt_ch = now_v - getattr(self, ph_attr, 0.0)
        if pwm_m_v == 1:
            step_bars_now = (hw_tk_v != getattr(self, tk_attr, -1)) or not hasattr(self, lb_attr)
            setattr(self, tk_attr, hw_tk_v)
        else:
            step_bars_now = (phys_dt_ch >= 0.015) or not hasattr(self, lb_attr)
        if step_bars_now:
            dt_sec = max(0.005, min(0.15, phys_dt_ch if phys_dt_ch < 1.0 else 0.0193))
            setattr(self, ph_attr, now_v)
            dt_ticks = dt_sec * 30.0
        else:
            dt_ticks = 0.0
            live_bins = getattr(self, lb_attr)
        import os as _os
        if not hasattr(self, "_viz_diag") or _os.path.exists("/tmp/oled_diag_reset_viz"):
            self._viz_diag = []
            try:
                _os.remove("/tmp/oled_diag_reset_viz")
            except Exception:
                pass
        self._viz_diag.append((
            round(raw_dt * 1000.0, 2),
            int(getattr(self.display, "_frame_seq", 0)),
            int(getattr(self.display, "_last_p0_lit", -1)),
            str(sc_id),
            str(cap_style),
            round(float(live_bins[0]), 3) if live_bins else 0.0,
            round(float(peaks[0]), 3) if peaks else 0.0,
            round(float(getattr(self.ui, "frame_interval", 0.0)) * 1000.0, 2),
            float(getattr(self.ui, "_last_comp_draw_ms", 0.0)),
            float(getattr(self.ui, "_last_comp_total_ms", 0.0)),
            float(getattr(self, "_prof_super_ms", 0.0)),
            float(getattr(self, "_prof_txt_ms", 0.0)),
            float(getattr(self, "_prof_bars_ms", 0.0)),
            float(getattr(self, "_prof_hud_ms", 0.0)),
        ))
        if len(self._viz_diag) > 300:
            self._viz_diag = self._viz_diag[-300:]
        if _os.path.exists("/tmp/oled_diag_dump_viz"):
            try:
                _os.remove("/tmp/oled_diag_dump_viz")
                import json as _j
                with open("/tmp/oled_viz_diag.json.tmp", "w") as vf:
                    _j.dump(self._viz_diag[:-4] if len(self._viz_diag) > 8 else self._viz_diag, vf)
                _os.replace("/tmp/oled_viz_diag.json.tmp", "/tmp/oled_viz_diag.json")
            except Exception:
                pass

        span_w = max(1, x2 - x1)
        step_x = max(2, span_w // bars)
        bar_w = max(1, step_x - 2)
        used_w = (bars - 1) * step_x + (bar_w + 1)
        start_x = x1 + max(0, (span_w - used_w) // 2)
        max_h = max(4, y_bot - y_top)
        gk_attr = f"_latched_geom_key_{chan_key}"
        bg_attr = f"_latched_bar_geom_{chan_key}"
        geom_key = (x1, y_top, x2, y_bot, bars, show_live, chan_key)

        if step_bars_now or getattr(self, gk_attr, None) != geom_key or not hasattr(self, bg_attr):
            if not show_live:
                any_high = False
                decayed = []
                for idx, v in enumerate(live_bins):
                    nv = max(0.05, v * (0.72 ** dt_ticks))
                    fall_ticks[idx] += dt_ticks
                    t_norm = min(2.5, fall_ticks[idx] / 25.0)
                    step_d = (v0_drift + (g_coeff * (t_norm ** exp_p) if exp_p > 0.0 else 0.0)) * dt_ticks
                    peaks[idx] = max(nv, peaks[idx] - step_d)
                    peak_holds[idx] = 0.0
                    if nv > 0.06 or peaks[idx] > 0.065:
                        any_high = True
                    decayed.append(nv)
                if chan_key == "M":
                    self.viz_bins = decayed
                live_bins = decayed
                self._latched_any_high = any_high
            else:
                self._latched_any_high = True
                be_attr = f"_viz_bar_env_{chan_key}"
                bar_env = getattr(self, be_attr, None)
                if not bar_env or len(bar_env) != 20:
                    bar_env = list(live_bins)
                    setattr(self, be_attr, bar_env)
                # Wall-Clock dt-Normalized Asymmetric Bar-Body Ballistics: 0.0ms instant upward snap (zero transient
                # damping!) + smooth hardware-tick-locked exponential release (0.58^dt_ticks) eliminating 46.9Hz PipeWire burst step-jitter!
                rel_k = 0.58 ** dt_ticks
                smoothed_bars = []
                for idx, v in enumerate(live_bins):
                    bv = v if v >= bar_env[idx] else max(0.05, v + (bar_env[idx] - v) * rel_k)
                    bar_env[idx] = bv
                    smoothed_bars.append(bv)
                    if bv >= peaks[idx]:
                        peaks[idx] = bv
                        peak_holds[idx] = float(hold_frames) * 0.5
                        fall_ticks[idx] = 0.0
                    else:
                        if peak_holds[idx] > 0.0:
                            peak_holds[idx] = max(0.0, peak_holds[idx] - dt_ticks)
                            fall_ticks[idx] = 0.0
                        else:
                            fall_ticks[idx] += dt_ticks
                            t_norm = min(2.8, fall_ticks[idx] / 25.0)
                            step_d = (v0_drift + (g_coeff * (t_norm ** exp_p) if exp_p > 0.0 else 0.0)) * dt_ticks
                            peaks[idx] = max(bv, peaks[idx] - step_d)
                live_bins = smoothed_bars
            setattr(self, lb_attr, list(live_bins))
            setattr(self, gk_attr, geom_key)
            n_bins = len(live_bins)
            g_list = []
            # Comment (invariant): Pool the peak across the contiguous bin slice [b0..b1] for each rendered bar so 5/8/9/10/12/16/18/20-bar layouts never drop bin 19 (16.6..20.4kHz) or skip inter-bar bins.
            for i in range(bars):
                bx = start_x + i * step_x
                if bx + bar_w > x2:
                    break
                b0 = min(n_bins - 1, int((i * n_bins) // max(1, bars)))
                b1 = min(n_bins, max(b0 + 1, int(round(((i + 1) * n_bins) / max(1, bars)))))
                if i == bars - 1:
                    b1 = n_bins
                lvl = max(live_bins[b0:b1])
                pk_lvl = max(peaks[b0:b1])
                bh = max(2, int(round(lvl * max_h))) if (show_live or lvl > 0.06) else 2
                pk_h = max(bh, int(round(pk_lvl * max_h)))
                by0 = max(y_top, y_bot - bh)
                g_list.append((i, bx, by0, bh, pk_h))
            setattr(self, bg_attr, g_list)

        if getattr(self, "_latched_any_high", True):
            self.display.has_active_marquee = True

        for i, bx, by0, bh, pk_h in getattr(self, bg_attr):

            if bar_style == "led":
                self.display.draw.rectangle((bx, by0, bx + bar_w, y_bot), fill=1)
                for gy in range(y_bot - 2, by0, -3):
                    self.display.draw.line((bx, gy, bx + bar_w, gy), fill=0)
            elif bar_style == "shaded":
                if bh > 2:
                    self.display.draw_shaded_rect(bx, by0 + 2, bx + bar_w, y_bot, shade=0.65, phase_id=i)
                self.display.draw.rectangle((bx, by0, bx + bar_w, min(y_bot, by0 + 1)), fill=1)
            elif bar_style == "hollow":
                if bh <= 3 or bar_w < 2:
                    self.display.draw.rectangle((bx, by0, bx + bar_w, y_bot), fill=1)
                else:
                    self.display.draw.rectangle((bx, by0, bx + bar_w, y_bot), fill=0, outline=1)
            else:
                self.display.draw.rectangle((bx, by0, bx + bar_w, y_bot), fill=1)

            if cap_style != "off":
                eff_pk_h = pk_h if pk_h >= bh + 2 else (bh + 4)
                py = max(y_top, y_bot - eff_pk_h - 1)
                if py < by0 - 1:
                    if cap_style == "pwm":
                        self.display.draw_shaded_rect(bx, py, bx + bar_w, py, shade=0.50, phase_id=i)
                    elif cap_style == "faint":
                        self.display.draw_shaded_rect(bx, py, bx + bar_w, py, shade=0.35, phase_id=i)
                    elif cap_style == "trail":
                        self.display.draw_shaded_rect(bx, py, bx + bar_w, py, shade=0.65, phase_id=i)
                        if py - 2 >= y_top:
                            self.display.draw_shaded_rect(bx, py - 2, bx + bar_w, py - 2, shade=0.35, phase_id=i)
                    elif cap_style == "dot":
                        mx = bx + max(0, (bar_w - 1) // 2)
                        self.display.draw_shaded_rect(mx, max(y_top, py - 1), min(bx + bar_w, mx + 1), py, shade=0.45, phase_id=i)
                    else:
                        self.display.draw.line((bx, py, bx + bar_w, py), fill=1)

    def _draw_state(self):
        # Modular Scene Frame Dispatcher (Renders active ScenePlugin and overlays HUD banner effects)
        t0 = time.monotonic()
        super()._draw_state()
        t1 = time.monotonic()
        scene_id = self._get_active_scene_id()
        plugin = SceneManager.get_instance(scene_id, self.display, self.ui, self)
        if plugin:
            plugin.draw()
        t2 = time.monotonic()
        self._apply_hud_and_banner_fx(scene_id)
        t3 = time.monotonic()
        self._prof_super_ms = round((t1 - t0) * 1000.0, 2)
        self._prof_scene_ms = round((t2 - t1) * 1000.0, 2)
        self._prof_hud_ms = round((t3 - t2) * 1000.0, 2)

    def _draw_pill_with_fx(self, text_s: str, pos: str, alpha: float, anchor_cfg: str, fx_mode: str, is_guide: bool = False):
        """
        Renders a tight-fitted pill at pos ('top' or 'bottom') anchored at Left ★ / Center / Right
        with per-line text alignment, a 2px left HUD accent bar on 2-line Guide Cards,
        and 'bayer' 4x4 dissolve / 'curtain' slide / 'laser' wipe transitions.
        """
        if not text_s or alpha <= 0.02:
            return
        raw_lines = [ln.strip()[:20] for ln in text_s.replace("\n", "|").split("|") if ln.strip()]
        if not raw_lines:
            return
        lines = raw_lines[:5] if is_guide else raw_lines[:1]
        n_lines = len(lines)
        pad = 4 if is_guide else 6

        line_widths = []
        max_w_px = 12
        for ln in lines:
            try:
                bb = self.display.draw.textbbox((0, 0), ln, font=self.display.font_small)
                w_px = max(12, bb[2] - bb[0])
            except Exception:
                w_px = len(ln) * 6
            line_widths.append(w_px)
            if w_px > max_w_px:
                max_w_px = w_px

        box_w = min(126, max_w_px + pad * 2)
        if anchor_cfg == "center":
            x0 = max(1, (128 - box_w) // 2)
        elif anchor_cfg == "right":
            x0 = max(1, 126 - box_w)
        else:
            x0 = 1 if is_guide else 2
        x1 = min(126, x0 + box_w)

        def _line_x(idx_l: int) -> int:
            lw = line_widths[idx_l]
            if anchor_cfg == "center":
                return max(x0 + 3, x0 + ((x1 - x0) - lw) // 2)
            if anchor_cfg == "right":
                return max(x0 + 3, x1 - pad - lw)
            return x0 + pad

        box_h = 12 if n_lines == 1 else min(62, 3 + n_lines * 10)
        if pos == "top":
            y_base0, y_base1 = 0, box_h - 1
        else:
            y_base0, y_base1 = max(0, 64 - box_h), 63

        if fx_mode == "curtain":
            y_off = int((1.0 - alpha) * float(box_h))
            y0 = (y_base0 - y_off) if pos == "top" else (y_base0 + y_off)
            y1 = (y_base1 - y_off) if pos == "top" else (y_base1 + y_off)
            if y1 < 0 or y0 >= 64:
                return
            yc0, yc1 = max(0, y0), min(63, y1)
            if is_guide:
                self.display.draw.rectangle((x0, yc0, x1, yc1), fill=0, outline=1)
                self.display.draw.rectangle((x0, yc0, min(x1, x0 + 2), yc1), fill=1)
                for idx, ln in enumerate(lines):
                    ty = y0 + 2 + idx * 10
                    if 0 <= ty <= 55:
                        self.display.draw.text((_line_x(idx), ty), ln, font=self.display.font_small, fill=1)
            else:
                self.display.draw.rectangle((x0, yc0, x1, yc1), fill=1)
                self.display.draw.text((_line_x(0), y0 + 1), lines[0], font=self.display.font_small, fill=0)
            return

        if fx_mode == "laser":
            mid_x = (x0 + x1) // 2
            half_w = int(alpha * ((x1 - x0) * 0.5))
            if half_w <= 2:
                return
            lx0, lx1 = max(0, mid_x - half_w), min(127, mid_x + half_w)
            if is_guide:
                self.display.draw.rectangle((lx0, y_base0, lx1, y_base1), fill=0, outline=1)
                if alpha > 0.40:
                    for idx, ln in enumerate(lines):
                        self.display.draw.text((max(lx0 + 2, _line_x(idx)), y_base0 + 2 + idx * 10), ln, font=self.display.font_small, fill=1)
            else:
                self.display.draw.rectangle((lx0, y_base0, lx1, y_base1), fill=1)
                if alpha > 0.40:
                    self.display.draw.text((max(lx0 + 2, _line_x(0)), y_base0 + 1), lines[0], font=self.display.font_small, fill=0)
            return

        # Default 'bayer': Draw 1-line Title pill or 2-line Guide card (with 2px left accent bar), then erode via 4x4 Bayer matrix
        self.display.draw.rectangle((max(0, x0 - 1), max(0, y_base0 - 1), min(127, x1 + 1), min(63, y_base1 + 1)), fill=0)
        if is_guide:
            self.display.draw.rectangle((x0, y_base0, x1, y_base1), fill=0, outline=1)
            self.display.draw.rectangle((x0, y_base0, min(x1, x0 + 2), y_base1), fill=1)
            for idx, ln in enumerate(lines):
                self.display.draw.text((_line_x(idx), y_base0 + 2 + idx * 10), ln, font=self.display.font_small, fill=1)
        else:
            self.display.draw.rectangle((x0, y_base0, x1, y_base1), fill=1)
            self.display.draw.text((_line_x(0), y_base0 + 1), lines[0], font=self.display.font_small, fill=0)

        if alpha < 0.99:
            thresh = int(round(alpha * 16.0))
            px = self.display.image.load()
            for y in range(y_base0, y_base1 + 1):
                b_row = _BAYER_4X4[y & 3]
                for x in range(x0, x1 + 1):
                    if b_row[x & 3] >= thresh:
                        px[x, y] = 0

    def _apply_hud_and_banner_fx(self, scene_id: str):
        # HUD Banner Overlay Compositor (Applies intro/guide pills and Bayer fade transitions over active scene)
        p_cls = SceneManager._plugins_classes.get(scene_id)
        is_saver = bool(getattr(p_cls, "IS_SAVER", False)) or (scene_id in ({"ambient_cube3d"} | AMBIENT_PHYSICS_SCENES))
        now_h = time.monotonic()
        if now_h - getattr(self, "_hud_cfg_ts", 0.0) > 0.5 or getattr(self, "_hud_cfg_sc", None) != scene_id:
            self._hud_cfg_ts = now_h
            self._hud_cfg_sc = scene_id
            self._hud_cached_vals = (
                self._get_scene_hud_behavior(scene_id),
                self._get_effective_anchor_cfg(scene_id),
                self.ui.db.get_setting("lock_hud_fx", "bayer"),
                self._get_effective_seq_cfg(scene_id),
                self._get_effective_title_sec(scene_id),
                self._get_effective_guide_sec(scene_id),
            )
        beh_ov, anchor_cfg, fx_mode, seq_cfg, t_hold, g_hold = self._hud_cached_vals
        hash_step = getattr(self, "_hash_step", 0)
        use_banner_fx = (
            self.active_slot == "screensaver"
            or self.standalone_scene is not None
            or beh_ov != "global"
            or getattr(self, "_manual_hud_peek", False)
            or hash_step in (1, 2, 3)
        )
        if not use_banner_fx:
            self._pill_currently_visible = False
            if scene_id in ("clock_digital", "clock_analog"):
                self.display.draw_text(4, 52, "[*] Unlock   [A/B] Slot")
            return

        title_s, guide_s = SCENE_GUIDES.get(scene_id, (SCENE_LABELS.get(scene_id, scene_id), "A/B:Switch|#:Guide"))
        if self.standalone_scene is not None:
            ids_p = self.category_scenes if self.category_scenes else [cid for cid, _ in SCENE_CATALOG]
            if len(ids_p) > 1 and scene_id in ids_p:
                title_s = f"[{ids_p.index(scene_id)+1}/{len(ids_p)}] {title_s}"
            if self.pick_db_key:
                guide_s = "A/B:Switch Scene  C:Select|D:Back (No Save)     #:HUD"
                if hash_step == 0:
                    g_hold = 3.5
                    t_hold = max(t_hold, 2.5)
            else:
                sw_hint = "A/B:Switch  " if len(ids_p) > 1 else ""
                guide_s = f"{sw_hint}C:HUD  D:Back|0-9:Interact         #:HUD"

        elapsed = time.monotonic() - self.banner_birth
        in_s = 0.28
        out_s = 0.85

        def _env_alpha(t_now: float, t_start: float, hold_d: float) -> float:
            if t_now < t_start:
                return 0.0
            dt = t_now - t_start
            tot = in_s + hold_d + out_s
            if dt >= tot:
                return 0.0
            if dt < in_s:
                return max(0.08, dt / in_s)
            if dt <= in_s + hold_d:
                return 1.0
            return max(0.0, 1.0 - ((dt - (in_s + hold_d)) / out_s))

        t_alpha = 0.0
        g_alpha = 0.0

        # Check if the 2.5s post-interaction Smart Auto-Tuck deadline has expired on Step 1 (Guide):
        now_fx = time.monotonic()
        tuck_dl = getattr(self, "_guide_tuck_deadline", 0.0)
        if hash_step == 1 and tuck_dl > 0.0:
            if now_fx >= tuck_dl:
                self._guide_tuck_deadline = 0.0
                self._last_pill_before_fade = "guide"
                self._trigger_banner_intro(hash_step=3)
                hash_step = 3
            else:
                self.display.has_active_marquee = True

        # When user manually presses '#', PIN the Guide (Step 1) or Title (Step 2) so they can read all 2 lines at their own pace!
        # Pressing '#' a 3rd time (Step 3) smoothly dissolves it out to a 100% Clean Canvas.
        if hash_step == 1:
            g_alpha = _env_alpha(elapsed, 0.0, 999999.0)
        elif hash_step == 2:
            t_alpha = _env_alpha(elapsed, 0.0, 999999.0)
        elif hash_step == 3:
            fade_out_a = max(0.0, 1.0 - (elapsed / 0.60)) if elapsed < 0.60 else 0.0
            if getattr(self, "_last_pill_before_fade", "title") == "title":
                t_alpha = fade_out_a
            else:
                g_alpha = fade_out_a
        else:
            if seq_cfg == "title":
                t_alpha = _env_alpha(elapsed, 0.0, t_hold)
                if self.pick_db_key:
                    g_alpha = _env_alpha(elapsed, 0.0, g_hold)
            elif seq_cfg == "guide":
                g_alpha = _env_alpha(elapsed, 0.0, g_hold)
            elif seq_cfg == "both":
                t_alpha = _env_alpha(elapsed, 0.0, t_hold)
                g_alpha = _env_alpha(elapsed, 0.0, g_hold)
            elif seq_cfg == "title_guide":
                p1_end = in_s + t_hold + out_s
                t_alpha = _env_alpha(elapsed, 0.0, t_hold)
                g_alpha = _env_alpha(elapsed, p1_end, g_hold)
            elif seq_cfg == "guide_title":
                p1_end = in_s + g_hold + out_s
                g_alpha = _env_alpha(elapsed, 0.0, g_hold)
                t_alpha = _env_alpha(elapsed, p1_end, t_hold)
            else:
                if self.pick_db_key:
                    g_alpha = _env_alpha(elapsed, 0.0, g_hold)

        self._pill_currently_visible = (t_alpha > 0.02) or (g_alpha > 0.02)
        if not self._pill_currently_visible:
            if scene_id in ("clock_digital", "clock_analog") and self.standalone_scene is None:
                self.display.draw_text(4, 52, "[*] Unlock   [A/B] Slot")
            return
        if g_alpha <= 0.02 and scene_id in ("clock_digital", "clock_analog") and self.standalone_scene is None:
            self.display.draw_text(4, 52, "[*] Unlock   [A/B] Slot")

        if (0.0 < t_alpha < 1.0) or (0.0 < g_alpha < 1.0) or (hash_step == 0 and ((t_alpha > 0 and t_hold < 9000.0) or (g_alpha > 0 and g_hold < 9000.0))):
            self.display.has_active_marquee = True

        # Comment (invariant): Never wipe rows 0..11 or 52..63 on self.display.image when pills are invisible (causes Pages 0,1,6,7 to black out on ph0 while staying lit on ph1/lo in _pwm_gray_planes!); when a pill IS visible on a saver/MV scene, clear the strip across ph1/lo as well before drawing the pill.
        gp = getattr(self.display, "_pwm_gray_planes", None)
        if is_saver and t_alpha > 0.02:
            self.display.draw.rectangle((0, 0, 127, 11), fill=0)
            if gp is not None:
                from PIL import ImageDraw as _ID
                _ID.Draw(gp[0]).rectangle((0, 0, 127, 11), fill=0)
                _ID.Draw(gp[1]).rectangle((0, 0, 127, 11), fill=0)
        g_lines_cnt = min(5, max(1, len([s for s in guide_s.replace("\n", "|").split("|") if s.strip()])))
        g_top_y = max(0, 64 - (12 if g_lines_cnt == 1 else min(62, 3 + g_lines_cnt * 10)))
        if is_saver:
            if g_alpha > 0.02:
                self.display.draw.rectangle((0, g_top_y, 127, 63), fill=0)
                if gp is not None:
                    from PIL import ImageDraw as _ID
                    _ID.Draw(gp[0]).rectangle((0, g_top_y, 127, 63), fill=0)
                    _ID.Draw(gp[1]).rectangle((0, g_top_y, 127, 63), fill=0)

        if t_alpha > 0.02:
            self._draw_pill_with_fx(title_s, "top", t_alpha, anchor_cfg, fx_mode, is_guide=False)
        if g_alpha > 0.02:
            self._draw_pill_with_fx(guide_s, "bottom", g_alpha, anchor_cfg, fx_mode, is_guide=True)
        if gp is not None and (t_alpha > 0.02 or g_alpha > 0.02):
            if t_alpha > 0.02:
                band_t = self.display.image.crop((0, 0, 128, 12))
                gp[0].paste(band_t, (0, 0))
                gp[1].paste(band_t, (0, 0))
            if g_alpha > 0.02:
                band_b = self.display.image.crop((0, g_top_y, 128, 64))
                gp[0].paste(band_b, (0, g_top_y))
                gp[1].paste(band_b, (0, g_top_y))


    def _draw_power_scope(self):
        self._sync_scope_burst()
        pm = getattr(self.ui, "power_monitor", None)
        pwr = getattr(self.ui, "power_state", {})
        v_now = float(pwr.get("voltage", 0.0))
        c_now = float(pwr.get("current", 0.0))
        pct = float(pwr.get("percent", 0.0))
        mw_now = int(round(v_now * c_now))
        paused = getattr(pm, "scope_paused", False) if pm else False
        st = getattr(pm, "scope_stats", {}) if pm else {}
        hist = list(getattr(pm, "scope_history", [])) if pm else []
        if c_now < -15.0:
            hrs = (2000.0 * (pct / 100.0)) / abs(c_now)
            rt_str = f"{min(99, int(hrs)):02d}h{int((hrs % 1.0) * 60):02d}m"
        elif c_now > 15.0:
            rt_str = "CHG+"
        else:
            rt_str = "IDLE"

        if self.scope_view == 1:
            self.display.draw.rectangle((0, 0, 127, 12), fill=1)
            self.display.draw.text((2, 0), f"2:CABLE TEST {'[HOLD]' if paused else '[0:TARE]'}", font=self.display.font_small, fill=0)
            dv = abs(st.get("v_max", v_now) - st.get("v_min", v_now))
            di = abs(st.get("c_max", c_now) - st.get("c_min", c_now))
            r_m = st.get("r_mohm", 0)
            grade = "WAIT dI" if r_m == 0 else ("GOOD" if r_m < 250 else ("FAIR" if r_m < 500 else "SAG!"))
            self.display.draw_text(2, 14, f"Now: {v_now:.2f}V {c_now:+.0f}mA")
            self.display.draw_text(2, 26, f"Sag: {dv*1000:.0f}mV  dI:{di:.0f}mA")
            self.display.draw_text(2, 38, f"Est R: {r_m}mOhm [{grade}]")
            self.display.draw_text(2, 51, "[1-3]Mode [5]Hold [0]Tare")
            return

        if self.scope_view == 2:
            self.display.draw.rectangle((0, 0, 127, 12), fill=1)
            self.display.draw.text((2, 0), f"3:USB AUDIT  Est:{rt_str}", font=self.display.font_small, fill=0)
            t_ma = st.get("tare_ma", c_now)
            d_ma = c_now - t_ma
            d_mw = int(round(d_ma * v_now))
            self.display.draw_text(2, 14, f"Base: {t_ma:+.0f}mA ({st.get('tare_v', v_now):.2f}V)")
            self.display.draw_text(2, 26, f"Live: {c_now:+.0f}mA ({mw_now:+d}mW)")
            self.display.draw_text(2, 38, f"Delta:{d_ma:+.0f}mA ({d_mw:+d}mW)")
            self.display.draw_text(2, 51, "[0]Tare Base  [1-3]Mode")
            return

        self.display.draw.rectangle((0, 0, 127, 11), fill=1)
        self.display.draw.text((2, 0), f"{v_now:.2f}V {c_now:+.0f}mA {rt_str}", font=self.display.font_small, fill=0)
        gx1, gy1, gx2, gy2 = 2, 13, 125, 50
        self.display.draw.rectangle((gx1, gy1, gx2, gy2), outline=1, fill=0)
        if len(hist) >= 2:
            sampled = hist[::self.scope_stride][-60:]
            c_vals = [pt[1] for pt in sampled]
            c_lo, c_hi = min(c_vals), max(c_vals)
            span = max(40.0, c_hi - c_lo)
            mid = (c_hi + c_lo) * 0.5
            lo_b, hi_b = mid - span * 0.55, mid + span * 0.55
            w_px, h_px = gx2 - gx1 - 2, gy2 - gy1 - 2
            pts = []
            for idx, (_, c_val) in enumerate(sampled):
                px = gx1 + 1 + int((idx / max(1, len(sampled) - 1)) * w_px)
                norm = max(0.0, min(1.0, (c_val - lo_b) / max(1.0, hi_b - lo_b)))
                py = gy2 - 1 - int(norm * h_px)
                pts.append((px, py))
            for i in range(len(pts) - 1):
                self.display.draw.line((pts[i], pts[i + 1]), fill=1)
        st_tag = "HOLD" if paused else f"{self.scope_stride}x"
        self.display.draw_text(2, 51, f"{mw_now:+d}mW [{st_tag}] 1-3:Vw 0:Tr")

    def _draw_mic_ultrasonic(self):
        self._sync_scope_burst()
        self.display.has_active_marquee = True
        d = self.usonic_data
        pk_khz, pk_db = d["peak_hz"] / 1000.0, d["peak_db"]
        het_tag = "HET:ON" if self.usonic_listen else "5:Listen"
        if self.usonic_view == 1:
            self.display.draw.rectangle((0, 0, 127, 11), fill=1)
            self.display.draw.text((2, 0), f"2:USONIC {pk_khz:.1f}kHz {pk_db:.0f}dB", font=self.display.font_small, fill=0)
            for i, bh in enumerate(d["us_bins"]):
                bx = 3 + i * 6
                self.display.draw.rectangle((bx, 49 - bh, bx + 4, 49), fill=1)
            self.display.draw_text(2, 51, f"17.6k..23.6k [{het_tag}]")
            return
        if self.usonic_view == 2:
            lo_khz = self.usonic_carrier_hz / 1000.0
            diff_hz = abs(d["peak_hz"] - self.usonic_carrier_hz)
            self.display.draw.rectangle((0, 0, 127, 11), fill=1)
            self.display.draw.text((2, 0), f"3:HETERODYNE [{'LIVE' if self.usonic_listen else 'MUTE'}]", font=self.display.font_small, fill=0)
            self.display.draw_text(2, 13, f"LO Carrier: {lo_khz:.1f}kHz (4/6)")
            self.display.draw_text(2, 25, f"Peak: {pk_khz:.1f}k -> {diff_hz}Hz")
            self.display.draw_text(2, 37, f"Boost: {1 << self.usonic_gain_shift}x (7/9)  {pk_db:.0f}dB")
            self.display.draw_text(2, 51, "[5]Listen [4/6]LO [1-3]Vw")
            return
        us_badge = "USONIC!" if d["alert"] else f"{pk_khz:.1f}kHz"
        self.display.draw.rectangle((0, 0, 127, 11), fill=1)
        self.display.draw.text((2, 0), f"1:MIC FFT+VU  [{us_badge}]", font=self.display.font_small, fill=0)
        lw, rw = int(max(1, d["vu_l"] * 54)), int(max(1, d["vu_r"] * 54))
        self.display.draw.rectangle((2, 13, 2 + lw, 15), fill=1)
        self.display.draw.rectangle((66, 13, 66 + rw, 15), fill=1)
        for i, bh in enumerate(d["full_bins"]):
            bx = 3 + i * 6
            self.display.draw.rectangle((bx, 49 - min(30, bh), bx + 4, 49), fill=1)
        self.display.draw_text(2, 51, f"[{het_tag}] 1:FFT 2:US 3:Het")

    def _draw_ir_scope(self):
        self._sync_scope_burst()
        self.display.draw.rectangle((0, 0, 127, 11), fill=1)
        rx_tag = " [RX!]" if self.ir_flashing else ""
        self.display.draw.text((2, 0), f"{self.ir_decoded}{rx_tag}", font=self.display.font_small, fill=0)
        x1, y_hi, x2, y_lo = 2, 16, 125, 44
        self.display.draw.rectangle((x1 - 1, y_hi - 3, x2 + 1, y_lo + 4), outline=1, fill=0)
        pulses = self.ir_pulses
        if pulses:
            w_px, t_cur, t_start = x2 - x1, 0, self.ir_pan_us
            t_end = t_start + max(1000, self.ir_zoom_us)
            prev_y = y_lo
            for p in pulses:
                dur = min(abs(p), 15000)
                seg_s, seg_e = t_cur, t_cur + dur
                t_cur = seg_e
                y_lvl = y_hi if p > 0 else y_lo
                if seg_e <= t_start:
                    prev_y = y_lvl
                    continue
                if seg_s >= t_end:
                    break
                px0 = x1 + int(max(0.0, min(1.0, (seg_s - t_start) / float(t_end - t_start))) * w_px)
                px1 = x1 + int(max(0.0, min(1.0, (seg_e - t_start) / float(t_end - t_start))) * w_px)
                if y_lvl != prev_y:
                    self.display.draw.line((px0, prev_y, px0, y_lvl), fill=1)
                self.display.draw.line((px0, y_lvl, max(px0, px1), y_lvl), fill=1)
                prev_y = y_lvl
        else:
            self.display.draw_text(16, 24, "Point IR Remote")
        zm_ms = self.ir_zoom_us // 1000
        if pulses and len(pulses) >= 2:
            self.display.draw_text(2, 51, f"+{abs(pulses[0])}/-{abs(pulses[1])}us [{zm_ms}m] 5:TX")
        else:
            self.display.draw_text(2, 51, f"[{zm_ms}ms] 46:Pan 79:Zm 5:TX")

    def _draw_ble_radar(self):
        self._sync_scope_burst()
        self.display.has_active_marquee = True
        now = time.monotonic()
        ranked = sorted(self.ble_devices.values(), key=lambda x: x["rssi"], reverse=True)
        stalkers = [d for d in ranked if d["tag"] in ("AIRTAG/APL", "SMARTTAG", "TILE") and (now - d["first_ts"]) >= 45.0]
        g_tag = "GEIGER:ON" if self.ble_geiger else "5:Geiger"
        if self.ble_view == 1:
            hdr = f"2:TRACKERS ({len(ranked)}) {'[ALERT!]' if stalkers else ''}"
            self.display.draw.rectangle((0, 0, 127, 11), fill=1)
            self.display.draw.text((2, 0), hdr, font=self.display.font_small, fill=0)
            if not ranked:
                self.display.draw_text(8, 24, "Scanning hci0 BLE...")
            else:
                for row_i, dev in enumerate(ranked[:3]):
                    age = int(now - dev["first_ts"])
                    self.display.draw_text(2, 13 + row_i * 12, f"{dev['tag'][:6]} {dev['mac'][-5:]} {dev['rssi']}dB {age}s")
            self.display.draw_text(2, 51, f"[{g_tag}] 1:Radar 0:Clr")
            return
        self.display.draw.rectangle((0, 0, 127, 11), fill=1)
        if ranked:
            idx = min(self.ble_sel_idx, len(ranked) - 1)
            tgt = ranked[idx]
            rssi = tgt["rssi"]
            dist_m = 10.0 ** ((-56.0 - float(rssi)) / 22.0)
            self.display.draw.text((2, 0), f"1:RADAR {tgt['tag'][:7]} {rssi}dBm", font=self.display.font_small, fill=0)
            cx, cy = 24, 31
            for r in (6, 12, 17):
                self.display.draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=1)
            blip_r = int(max(1.0, min(16.0, (abs(rssi) - 38.0) * 0.35)))
            ph_ble, _ = self.advance_scene_phase("ble_radar", ref_fps=30.0)
            ang = (ph_ble * 0.12) % 6.283
            bx, by = int(cx + math.cos(ang) * blip_r), int(cy + math.sin(ang) * blip_r)
            self.display.draw.rectangle((bx - 1, by - 1, bx + 1, by + 1), fill=1)
            self.display.draw_text(46, 13, f"{tgt['name'][:13]}")
            self.display.draw_text(46, 25, f"Dist: ~{dist_m:.1f}m ({idx+1}/{len(ranked)})")
            hist = tgt.get("hist", [])
            if len(hist) >= 2:
                for hi in range(len(hist) - 1):
                    x0 = 46 + hi * 2
                    if x0 + 2 > 125:
                        break
                    y0 = 48 - int(max(0.0, min(1.0, (hist[hi] + 95.0) / 55.0)) * 11)
                    y1 = 48 - int(max(0.0, min(1.0, (hist[hi + 1] + 95.0) / 55.0)) * 11)
                    self.display.draw.line((x0, y0, x0 + 2, y1), fill=1)
        else:
            self.display.draw.text((2, 0), "1:BLE PROXIMITY RADAR", font=self.display.font_small, fill=0)
            self.display.draw_text(10, 26, "Scanning BLE Packets...")
        self.display.draw_text(2, 51, f"[{g_tag}] 4/6:Tgt 2:List")
