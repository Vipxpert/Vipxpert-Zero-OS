# /vipxpert_minimal_os/src/scenes/music/music_viz.py
from src.ui.scene_manager import ScenePlugin
from src.ui.components import scroll_text


class MusicVizScene(ScenePlugin):
    # Full-screen visualizer scene (Delegates modes, band count, fullscreen canvas, and hotkeys to single-source LockScreenApp engine)
    SCENE_ID = "music_viz"
    TITLE = "Full Visualizer"
    CATEGORY = "music"
    ORDER = 30
    GUIDE_TEXT = "0:Mode 3:Band C:Full|Hold0:Style Hold3:Pk|Hold1:Pink/Raw D:HUD|5:Play 4/6:Trk 28:Vl"

    def handle_key(self, action: str, key: str):
        return self.app.handle_visualizer_hotkeys(self.SCENE_ID, action, key)

    def draw(self):
        status_str, title_str, artist_str, _, _, _, bt_str = self.app._extract_media_info()
        playing = (status_str == "playing")
        if self.app.render_visualizer_viewport(4, 14, 124, 52, playing, max_bars=20):
            return
        icon = "►" if playing else "||"
        full_t = f"{title_str} - {artist_str}" if (artist_str and title_str and title_str != "No Media") else title_str
        d_full = scroll_text(full_t, max_chars=18) if full_t and full_t != "No Media" else full_t
        self.display.draw_text(2, 0, f"{icon} {d_full}")
        self.app._draw_music_top_right_battery()
        vol_str = getattr(self.ui, "pi_volume_str", "N/A")
        self.display.draw_text(2, 53, f"Pi:{vol_str}")
        self.app._draw_right_aligned_text(126, 53, f"BT:{bt_str}")
