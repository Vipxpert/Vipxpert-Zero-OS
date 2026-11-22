# /vipxpert_minimal_os/src/scenes/music/music_viz.py
from src.ui.scene_manager import ScenePlugin
from src.ui.components import scroll_text


class MusicVizScene(ScenePlugin):
    # Full-screen visualizer scene (Renders 20-bar Mono or dual 10-bar Stereo L|R spectrum analyzer with Key '0' toggle)
    SCENE_ID = "music_viz"
    TITLE = "Full Visualizer"
    CATEGORY = "music"
    ORDER = 30
    GUIDE_TEXT = "5:Play 4/6:Trk 0:St/Mono|2/8:PiVol 7/9:BTVol #:HUD"

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
        # Full Visualizer Frame Render (Draws 20 mono bars or split 10L|10R stereo bars across y=14..52)
        status_str, title_str, artist_str, _, _, _, bt_str = self.app._extract_media_info()
        playing = (status_str == "playing")
        icon = "►" if playing else "||"
        full_t = f"{title_str} - {artist_str}" if (artist_str and title_str and title_str != "No Media") else title_str
        d_full = scroll_text(full_t, max_chars=18) if full_t and full_t != "No Media" else full_t
        self.display.draw_text(2, 0, f"{icon} {d_full}")
        self.app._draw_music_top_right_battery()
        st_mode = self._is_stereo()
        if st_mode:
            self.app._draw_spectrum_bars(3, 14, 61, 52, playing, bars=10)
            self.display.draw.line((63, 14, 63, 52), fill=1)
            self.app._draw_spectrum_bars(66, 14, 124, 52, playing, bars=10)
        else:
            self.app._draw_spectrum_bars(4, 14, 124, 52, playing, bars=20)
        vol_str = getattr(self.ui, "pi_volume_str", "N/A")
        self.display.draw_text(2, 53, f"Pi:{vol_str}")
        self.app._draw_right_aligned_text(126, 53, f"BT:{bt_str}")
