# /vipxpert_minimal_os/src/scenes/music/music_split.py
import time
from src.ui.scene_manager import ScenePlugin
from src.ui.components import scroll_text


class MusicSplitScene(ScenePlugin):
    # Split music and spectrum scene (Renders scrolling track header, timer, 18-bar Mono or 9L|9R Stereo spectrum, and Key '0' toggle)
    SCENE_ID = "music_split"
    TITLE = "Music + Spectrum"
    CATEGORY = "music"
    ORDER = 20
    GUIDE_TEXT = "5:Play 4/6:Trk 0:St/Mono|2/8:PiVol 7/9:BTVol A/B:Slot"

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
        # Split Spectrum Frame Render (Draws header, 18-bar Mono or 9L|9R Stereo RTA, and volume footer)
        status_str, title_str, artist_str, pos, dur, _, bt_str = self.app._extract_media_info()
        playing = (status_str == "playing")
        icon = "►" if playing else "||"
        m, s = divmod(int(pos), 60)
        dm, ds = divmod(int(dur), 60)
        time_str = f"{m:02d}:{s:02d}/{dm:02d}:{ds:02d}" if dur > 0.0 else f"{m:02d}:{s:02d}"
        full_t = f"{title_str} - {artist_str}" if (artist_str and title_str and title_str != "No Media") else title_str
        d_full = scroll_text(full_t, max_chars=18) if full_t and full_t != "No Media" else full_t
        st_mode = self._is_stereo()
        tm0 = time.monotonic()
        self.display.draw_text(2, 0, f"♫ {d_full}")
        self.app._draw_music_top_right_battery()
        self.display.draw_text(2, 12, f"{icon} {time_str}")
        tm1 = time.monotonic()
        if st_mode:
            self.app._draw_spectrum_bars(3, 25, 61, 50, playing, bars=9)
            self.display.draw.line((63, 25, 63, 50), fill=1)
            self.app._draw_spectrum_bars(66, 25, 124, 50, playing, bars=9)
        else:
            self.app._draw_spectrum_bars(4, 25, 124, 50, playing, bars=20)
        tm2 = time.monotonic()
        vol_str = getattr(self.ui, "pi_volume_str", "N/A")
        self.display.draw_text(2, 52, f"Pi:{vol_str}")
        self.app._draw_right_aligned_text(126, 52, f"BT:{bt_str}")
        tm3 = time.monotonic()
        self.app._prof_txt_ms = round(((tm1 - tm0) + (tm3 - tm2)) * 1000.0, 2)
        self.app._prof_bars_ms = round((tm2 - tm1) * 1000.0, 2)
