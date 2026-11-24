# /vipxpert_minimal_os/src/scenes/music/ambient_hud.py
from src.ui.scene_manager import ScenePlugin


class AmbientHudScene(ScenePlugin):
    # Ambient microphone HUD scene (Delegates modes, band count, fullscreen canvas, and hotkeys to single-source LockScreenApp engine)
    SCENE_ID = "ambient_hud"
    TITLE = "Ambient Mic HUD"
    CATEGORY = "music"
    ORDER = 40
    GUIDE_TEXT = "0:Mode 3:Band C:Full|Hold0:Style Hold3:Pk|Hold1:Pink/Raw 1:DAC|2/8:PiVol A/B:Slot"

    def handle_key(self, action: str, key: str):
        return self.app.handle_visualizer_hotkeys(self.SCENE_ID, action, key)

    def draw(self):
        db = self.ui.db
        amb_on = bool(getattr(self.ui, "transparency_enabled", False))
        if self.app.render_visualizer_viewport(4, 39, 124, 50, amb_on, max_bars=18):
            return
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
        vol_str = getattr(self.ui, "pi_volume_str", "N/A")
        hint = "[A/B] Scn" if getattr(self.app, "standalone_scene", None) else "[A/B] Slot"
        self.display.draw_text(2, 52, f"Pi:{vol_str}  {hint}")
