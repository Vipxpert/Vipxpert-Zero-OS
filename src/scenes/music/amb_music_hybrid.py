# /vipxpert_minimal_os/src/scenes/music/amb_music_hybrid.py
from src.ui.scene_manager import ScenePlugin
from src.ui.components import scroll_text


class AmbMusicHybridScene(ScenePlugin):
    # Hybrid ambient + music scene (Renders concurrent media playback and microphone transparency telemetry)
    SCENE_ID = "amb_music_hybrid"
    TITLE = "Amb+Music Hybrid"
    CATEGORY = "music"
    ORDER = 50
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
        # Hybrid Amb+Music Frame Render (Draws track marquee, timer + AMB mix, spectrum, and gain footer)
        status_str, title_str, artist_str, pos, dur, _, bt_str = self.app._extract_media_info()
        playing = (status_str == "playing")
        amb_on = bool(getattr(self.ui, "transparency_enabled", False))
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
        if self._is_stereo():
            self.app._draw_spectrum_bars(3, 26, 61, 49, playing or amb_on, bars=9)
            self.display.draw.line((63, 26, 63, 49), fill=1)
            self.app._draw_spectrum_bars(66, 26, 124, 49, playing or amb_on, bars=9)
        else:
            self.app._draw_spectrum_bars(4, 26, 124, 49, playing or amb_on, bars=20)
        vol_str = getattr(self.ui, "pi_volume_str", "N/A")
        self.display.draw_text(2, 52, f"Pi:{vol_str} BT:{bt_str}")
        self.app._draw_right_aligned_text(126, 52, f"+{gain}dB")
