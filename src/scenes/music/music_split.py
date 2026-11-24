# /vipxpert_minimal_os/src/scenes/music/music_split.py
import time
from src.ui.scene_manager import ScenePlugin
from src.ui.components import scroll_text


class MusicSplitScene(ScenePlugin):
    # Split music and spectrum scene (Delegates modes, band count, fullscreen canvas, and hotkeys to single-source LockScreenApp engine)
    SCENE_ID = "music_split"
    TITLE = "Music + Spectrum"
    CATEGORY = "music"
    ORDER = 20
    GUIDE_TEXT = "0:Mode 3:Band C:Full|Hold0:Style Hold3:Pk|Hold1:Pink/Raw D:MV|5:Play 4/6:Trk 28:Vl"

    def handle_key(self, action: str, key: str):
        return self.app.handle_visualizer_hotkeys(self.SCENE_ID, action, key)

    def draw(self):
        status_str, title_str, artist_str, pos, dur, _, bt_str = self.app._extract_media_info()
        playing = (status_str == "playing")
        tm1 = time.monotonic()
        is_fs = self.app.render_visualizer_viewport(4, 25, 124, 50, playing, max_bars=20)
        tm2 = time.monotonic()
        if is_fs:
            self.app._prof_txt_ms = 0.0
            self.app._prof_bars_ms = round((tm2 - tm1) * 1000.0, 2)
            return
        icon = "►" if playing else "||"
        m, s = divmod(int(pos), 60)
        dm, ds = divmod(int(dur), 60)
        time_str = f"{m:02d}:{s:02d}/{dm:02d}:{ds:02d}" if dur > 0.0 else f"{m:02d}:{s:02d}"
        full_t = f"{title_str} - {artist_str}" if (artist_str and title_str and title_str != "No Media") else title_str
        d_full = scroll_text(full_t, max_chars=18) if full_t and full_t != "No Media" else full_t
        tm0 = time.monotonic()
        self.display.draw_text(2, 0, f"♫ {d_full}")
        self.app._draw_music_top_right_battery()
        self.display.draw_text(2, 12, f"{icon} {time_str}")
        vol_str = getattr(self.ui, "pi_volume_str", "N/A")
        self.display.draw_text(2, 52, f"Pi:{vol_str}")
        self.app._draw_right_aligned_text(126, 52, f"BT:{bt_str}")
        tm3 = time.monotonic()
        self.app._prof_txt_ms = round((tm3 - tm0) * 1000.0, 2)
        self.app._prof_bars_ms = round((tm2 - tm1) * 1000.0, 2)
