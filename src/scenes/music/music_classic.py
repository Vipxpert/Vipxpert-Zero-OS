# /vipxpert_minimal_os/src/scenes/music/music_classic.py
from src.ui.scene_manager import ScenePlugin
from src.ui.components import scroll_text


class MusicClassicScene(ScenePlugin):
    # Classic music transport scene (Supports Key 'C' Fullscreen Visualizer toggle via single-source LockScreenApp engine)
    SCENE_ID = "music_classic"
    TITLE = "Music Classic"
    CATEGORY = "music"
    ORDER = 10
    GUIDE_TEXT = "5:Play 4/6:Trk 1:DAC|2/8:PiVol 7/9:BTVol|C:FullViz D:MV/Back|A/B:Slot  *:Sleep/Unlk"

    def handle_key(self, action: str, key: str):
        if getattr(self.app, "viz_fullscreen", False) or key == "C":
            return self.app.handle_visualizer_hotkeys(self.SCENE_ID, action, key)
        return False

    def draw(self):
        # Classic Transport Layout Render (Or pure 128x64 Fullscreen Visualizer when toggled via Key 'C')
        status_str, title_str, artist_str, pos, dur, codec_str, bt_str = self.app._extract_media_info()
        playing = (status_str == "playing")
        if getattr(self.app, "viz_fullscreen", False) and self.app.render_visualizer_viewport(2, 2, 125, 61, playing, max_bars=20):
            return
        icon = "►" if playing else "||"
        m, s = divmod(int(pos), 60)
        dm, ds = divmod(int(dur), 60)
        time_str = f"{m:02d}:{s:02d}/{dm:02d}:{ds:02d}" if dur > 0.0 else f"{m:02d}:{s:02d}"
        d_title = scroll_text(title_str, max_chars=18) if title_str and title_str != "No Media" else title_str
        d_artist = scroll_text(artist_str, max_chars=18) if artist_str else ""
        self.display.draw_text(2, 0, f"♫ {d_title}")
        self.app._draw_music_top_right_battery()
        self.display.draw_text(14, 12, d_artist)
        self.display.draw_text(2, 24, f"{icon} {time_str}")
        db = self.ui.db
        show_prog = db.get_setting("scn_mcls_prog", "1") == "1"
        foot_mode = db.get_setting("scn_mcls_foot", "marquee")
        if show_prog and dur > 0.0:
            pct_done = max(0.0, min(1.0, pos / dur))
            self.display.draw.line((2, 37, 125, 37), fill=1)
            fill_x = 2 + int(123 * pct_done)
            if fill_x > 2:
                self.display.draw.rectangle((2, 36, fill_x, 38), fill=1)
        vol_str = getattr(self.ui, "pi_volume_str", "N/A")
        self.display.draw_text(2, 40, f"Pi:{vol_str}")
        self.app._draw_right_aligned_text(126, 40, f"BT:{bt_str}")
        if foot_mode != "clean":
            dac_name = ("I2S (Off)" if getattr(self.ui, "i2s_manually_killed", False) else "PCM5122 I2S") if self.ui._is_i2s_effectively_active() else (self.ui.active_hardware_dac or "PCM5122 I2S")
            pwr = getattr(self.ui, "power_state", {})
            bat_str = f"{pwr.get('icon', '[-]')} {pwr.get('percent', 0.0):.1f}%" if (not pwr.get("error", True) or pwr.get("voltage", 0.0) > 0.5) else ""
            if foot_mode == "dac_bat":
                short_dac = "I2S" if "PCM5122" in dac_name else dac_name[:7]
                self.display.draw_text(4, 51, f"{short_dac} {codec_str[:5]} {bat_str}")
            else:
                full_dap = f"DAC: {dac_name}   Codec: {codec_str}   Bat: {bat_str}"
                self.display.draw_text(4, 51, scroll_text(full_dap, max_chars=18))
