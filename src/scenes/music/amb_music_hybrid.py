# /vipxpert_minimal_os/src/scenes/music/amb_music_hybrid.py
from src.ui.scene_manager import ScenePlugin
from src.ui.components import scroll_text


class AmbMusicHybridScene(ScenePlugin):
    # Hybrid ambient + music scene (Delegates modes, band count, fullscreen canvas, and hotkeys to single-source LockScreenApp engine)
    SCENE_ID = "amb_music_hybrid"
    TITLE = "Amb+Music Hybrid"
    CATEGORY = "music"
    ORDER = 50
    GUIDE_TEXT = "0:Mode 3:Band C:Full|Hold0:Style Hold3:Pk|Hold1:Pink/Raw D:MV|5:Play 4/6:Trk 28:Vl"

    def handle_key(self, action: str, key: str):
        return self.app.handle_visualizer_hotkeys(self.SCENE_ID, action, key)

    def draw(self):
        status_str, title_str, artist_str, pos, dur, _, bt_str = self.app._extract_media_info()
        playing = (status_str == "playing")
        amb_on = bool(getattr(self.ui, "transparency_enabled", False))
        if self.app.render_visualizer_viewport(4, 26, 124, 49, playing or amb_on, max_bars=20):
            return
        icon = "►" if playing else "||"
        m, s = divmod(int(pos), 60)
        dm, ds = divmod(int(dur), 60)
        time_str = f"{m:02d}:{s:02d}/{dm:02d}:{ds:02d}" if dur > 0.0 else f"{m:02d}:{s:02d}"
        full_t = f"{title_str} - {artist_str}" if (artist_str and title_str and title_str != "No Media") else title_str
        d_title = scroll_text(full_t, max_chars=18) if full_t and full_t != "No Media" else full_t
        gain = self.ui.db.get_setting("mic_gain_db", "46")
        t_vol = self.ui.db.get_setting("transparency_vol_pct", "100")
        self.display.draw_text(2, 0, f"♫ {d_title}")
        self.app._draw_music_top_right_battery()
        self.display.draw_text(2, 12, f"{icon} {time_str}")
        self.app._draw_right_aligned_text(126, 12, f"AMB:{t_vol}%")
        vol_str = getattr(self.ui, "pi_volume_str", "N/A")
        self.display.draw_text(2, 52, f"Pi:{vol_str} BT:{bt_str}")
        self.app._draw_right_aligned_text(126, 52, f"+{gain}dB")
