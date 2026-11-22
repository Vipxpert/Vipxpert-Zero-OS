# /vipxpert_minimal_os/src/scenes/music/ambient_hud.py
from src.ui.scene_manager import ScenePlugin


class AmbientHudScene(ScenePlugin):
    # Ambient microphone HUD scene (Renders mic gain, range mode, wind HPF, and live ambient spectrum)
    SCENE_ID = "ambient_hud"
    TITLE = "Ambient Mic HUD"
    CATEGORY = "music"
    ORDER = 40
    GUIDE_TEXT = "2/8:PiVol 0:St/Mono 1:DAC|A/B:Switch Slot    #:HUD"

    # Stage: Key '0' Live Stereo / Mono Visualizer Shortcut
    def _is_stereo(self) -> bool:
        ov = self.ui.db.get_setting(f"scene_stereo_{self.SCENE_ID}", "global")
        mode = ov if ov in ("mono", "stereo") else self.ui.db.get_setting("lock_viz_stereo", "mono")
        return mode == "stereo"

    def handle_key(self, action: str, key: str):
        if action == "CLICK" and key == "0":
            nxt = "mono" if self._is_stereo() else "stereo"
            if self.ui.db.get_setting(f"scene_stereo_{self.SCENE_ID}", "global") in ("mono", "stereo"):
                self.ui.db.set_setting(f"scene_stereo_{self.SCENE_ID}", nxt)
            else:
                self.ui.db.set_setting("lock_viz_stereo", nxt)
            self.ui.request_render()
            return True
        return False

    def draw(self):
        # Ambient HUD Frame Render (Draws inverted status bar, mic parameters, and 18-bar spectrum)
        db = self.ui.db
        amb_on = bool(getattr(self.ui, "transparency_enabled", False))
        gain = db.get_setting("mic_gain_db", "46")
        rng = {"near": "Near", "far": "Far", "far_hpf": "Far+HPF", "far_agc": "Far+AGC"}.get(db.get_setting("mic_range_mode", "near"), "Near")
        wind = int(db.get_setting("mic_wind_hpf_hz", "0"))
        t_vol = db.get_setting("transparency_vol_pct", "100")
        dac_badge = "I2S" if self.ui._is_i2s_effectively_active() else "USB"
        pwr = getattr(self.ui, "power_state", {})
        bat_s = f"{pwr.get('percent', 0.0):.0f}%" if (not pwr.get("error", True) or pwr.get("voltage", 0.0) > 0.5) else ""
        self.display.draw.rectangle((0, 0, 127, 12), fill=1)
        self.display.draw.text((2, 0), f"AMB {'ON' if amb_on else 'STBY'} | {dac_badge} {bat_s}", font=self.display.font_small, fill=0)
        self.display.draw_text(2, 14, f"Mic: +{gain}dB ({rng})")
        self.display.draw_text(2, 26, f"Mix: {t_vol}%  Wind:{wind}Hz")
        if self._is_stereo():
            self.app._draw_spectrum_bars(3, 39, 61, 50, amb_on, bars=9)
            self.display.draw.line((63, 39, 63, 50), fill=1)
            self.app._draw_spectrum_bars(66, 39, 124, 50, amb_on, bars=9)
        else:
            self.app._draw_spectrum_bars(4, 39, 124, 50, amb_on, bars=18)
        vol_str = getattr(self.ui, "pi_volume_str", "N/A")
        hint = "[A/B] Scn" if getattr(self.app, "standalone_scene", None) else "[A/B] Slot"
        self.display.draw_text(2, 52, f"Pi:{vol_str}  {hint}")
