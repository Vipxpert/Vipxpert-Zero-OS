# /vipxpert_minimal_os/src/scenes/music/bad_apple/scene.py
import os
import re
import json
import time
import mmap
import threading
import subprocess
from typing import Optional
from src.ui.scene_manager import ScenePlugin, SceneManager
from src.ui.components import render_progress_alert

# Default live-reloadable video alias rules
DEFAULT_RULES = {
    "rules": [
        {"pattern": "bad\\s*apple", "video": "bad_apple.mp4"}
    ]
}


class MusicVideoScene(ScenePlugin):
    # Universal Music Video (MV) Scene Plugin (Supports smart substring matching, video_rules.json aliases, 128x64 fit-padding, background pre-caching, and Key D toggle)
    SCENE_ID = "bad_apple"
    TITLE = "Music Video (MV)"
    CATEGORY = "music"
    ORDER = 60
    GUIDE_TEXT = "D:Toggle MV/HUD 5:Play 4/6:Prev 0:ReCache"
    IS_SAVER = True
    IS_INTERACTIVE = True

    VID_W = 128
    VID_H = 64
    RAW_FRAME_BYTES = VID_W * VID_H

    def __init__(self, display, ui_manager, app_ref):
        # Universal MV State Initializer
        super().__init__(display, ui_manager, app_ref)
        self.bundle_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_dir = os.path.join(self.bundle_dir, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.rules_json = os.path.join(self.bundle_dir, "video_rules.json")
        self._ensure_default_rules()

        self.fps = 30.0
        self._mmap_fd = None
        self._mmap_buf = None
        self._total_frames = 0
        self._converting = False
        self._convert_err = None
        self._convert_progress = 0.0
        self._current_video_path = None
        self._smooth_pos = 0.0
        self._smooth_ts = 0.0
        self._last_rules_mtime = 0.0
        self._loaded_rules = DEFAULT_RULES

        # Trigger background boot pre-cacher if enabled in DB
        if self.ui.db.get_setting("mv_boot_precache", "1") == "1":
            threading.Thread(target=self._background_precache_daemon, daemon=True, name="MVPrecacheDaemon").start()

    def _ensure_default_rules(self):
        # Creates default video_rules.json if missing
        if not os.path.exists(self.rules_json):
            try:
                with open(self.rules_json, "w") as f:
                    json.dump(DEFAULT_RULES, f, indent=2)
            except Exception:
                pass

    def _get_rules(self) -> dict:
        # Live-reloads video_rules.json if modified on disk
        try:
            if os.path.exists(self.rules_json):
                st = os.stat(self.rules_json)
                if st.st_mtime > self._last_rules_mtime:
                    self._last_rules_mtime = st.st_mtime
                    with open(self.rules_json, "r") as f:
                        self._loaded_rules = json.load(f)
        except Exception:
            pass
        return self._loaded_rules

    @classmethod
    def should_auto_activate(cls, app_ref) -> bool:
        # Universal MV Matcher (Returns True ONLY if autoplay is enabled, MV is not suppressed, and a matching video file exists)
        if getattr(app_ref, "_mv_suppressed", False):
            return False
        if app_ref.ui.db.get_setting("mv_autoplay_enabled", "1") != "1":
            return False
        status_str, title_str, artist_str, _, _, _, _ = app_ref._extract_media_info()
        if status_str != "playing" or not title_str or title_str == "No Media":
            return False

        plugin = SceneManager.get_instance("bad_apple", app_ref.display, app_ref.ui, app_ref)
        if plugin and isinstance(plugin, MusicVideoScene):
            return plugin._find_matching_video(title_str, artist_str) is not None
        return False

    def _find_matching_video(self, title: str, artist: str) -> Optional[str]:
        # Searches bundle folder and /music for video matching song via rules or smart substring stem match
        exts = (".mp4", ".mkv", ".webm", ".avi", ".mov")
        parent_dir = os.path.dirname(self.bundle_dir)
        search_dirs = [self.bundle_dir, parent_dir, "/music", "/home/vipxpert/music", "/home/vipxpert/vipxpert_minimal_os/music"]
        
        norm_title = re.sub(r'[\(\[].*?[\)\]]', '', title).lower().strip()
        norm_artist = re.sub(r'[\(\[].*?[\)\]]', '', artist).lower().strip()
        full_query = f"{norm_artist} {norm_title}" if norm_artist else norm_title

        rules = self._get_rules().get("rules", [])
        all_videos = []
        for d in search_dirs:
            if not os.path.isdir(d):
                continue
            for root, _, files in os.walk(d):
                for f in files:
                    if f.lower().endswith(exts):
                        all_videos.append((f.lower(), os.path.join(root, f)))

        # 1. Check custom JSON rules first
        match_mode = self.ui.db.get_setting("mv_match_mode", "smart")
        for r in rules:
            pat = r.get("pattern", "")
            vname = r.get("video", "").lower()
            if pat and re.search(pat, full_query, re.IGNORECASE):
                for fname, fpath in all_videos:
                    if vname in fname:
                        return fpath

        # 2. Smart Substring / Exact match against video filename stems
        for fname, fpath in all_videos:
            stem = os.path.splitext(fname)[0].replace("_", " ").replace("-", " ").strip()
            if match_mode == "exact":
                if stem == norm_title:
                    return fpath
            else:
                # Smart substring match (e.g. video stem inside song title or vice versa)
                if stem in norm_title or norm_title in stem or stem in full_query:
                    return fpath
        return None

    def handle_key(self, action: str, key: str) -> bool:
        # Key '0' manually clears cache and forces re-transcode
        if action == "CLICK" and key == "0":
            self._close_mmap()
            if self._current_video_path:
                stem = os.path.splitext(os.path.basename(self._current_video_path))[0]
                for ext in (".bin", ".meta.json"):
                    cp = os.path.join(self.cache_dir, stem + ext)
                    if os.path.exists(cp):
                        try:
                            os.remove(cp)
                        except Exception:
                            pass
            self._convert_err = None
            self.ui.request_render()
            return True
        return False

    def _close_mmap(self):
        # Safely releases mmap handle
        if self._mmap_buf is not None:
            try:
                self._mmap_buf.close()
            except Exception:
                pass
            self._mmap_buf = None
        if self._mmap_fd is not None:
            try:
                os.close(self._mmap_fd)
            except Exception:
                pass
            self._mmap_fd = None
        self._total_frames = 0

    @staticmethod
    def _probe_native_fps(src_path: str) -> float:
        # Probes exact video framerate via ffprobe
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate,r_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1", src_path]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8)
            for line in res.stdout.decode("utf-8", errors="ignore").splitlines():
                line = line.strip()
                if "/" in line:
                    num, den = line.split("/", 1)
                    if float(den) > 0:
                        val = float(num) / float(den)
                        if 5.0 <= val <= 120.0:
                            return round(val, 3)
                elif line:
                    val = float(line)
                    if 5.0 <= val <= 120.0:
                        return round(val, 3)
        except Exception:
            pass
        return 30.0

    def _transcode_worker(self, src_path: str, cache_bin: str, meta_json: str, sig: dict):
        # Background transcoder worker with aspect-ratio fitting and live progress reporting
        native_fps = self._probe_native_fps(src_path)
        self.fps = native_fps
        _, _, _, _, dur, _, _ = self.app._extract_media_info()
        est_frames = max(1.0, (dur * native_fps) if dur > 0.0 else (219.0 * native_fps))
        tmp_path = cache_bin + ".tmp"
        
        vf_str = (
            f"scale={self.VID_W}:{self.VID_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={self.VID_W}:{self.VID_H}:(ow-iw)/2:(oh-ih)/2:black"
        )
        cmd = ["ffmpeg", "-y", "-i", src_path, "-vf", vf_str, "-f", "rawvideo", "-pix_fmt", "gray", "-progress", "pipe:1", "-nostats", tmp_path]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if proc.stdout:
                for line in proc.stdout:
                    if line.startswith("frame="):
                        try:
                            cur_f = float(line.split("=", 1)[1].strip())
                            self._convert_progress = min(0.99, cur_f / est_frames)
                            self.ui.request_render()
                        except Exception:
                            pass
            _, err_out = proc.communicate(timeout=240)
            if proc.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) >= self.RAW_FRAME_BYTES:
                self._convert_progress = 1.0
                os.replace(tmp_path, cache_bin)
                meta = dict(sig)
                meta["fps"] = native_fps
                with open(meta_json, "w") as mf:
                    json.dump(meta, mf)
            else:
                with open("/tmp/bad_apple_err.txt", "w") as f:
                    f.write(err_out or "Unknown FFmpeg error")
                self._convert_err = "FFmpeg Failed"
        except Exception as e:
            with open("/tmp/bad_apple_err.txt", "w") as f:
                f.write(str(e))
            self._convert_err = "Subprocess Err"
        finally:
            self._converting = False
            self.ui.request_render()

    def _background_precache_daemon(self):
        # Background boot pre-cacher scanning all video sources
        exts = (".mp4", ".mkv", ".webm", ".avi", ".mov")
        parent_dir = os.path.dirname(self.bundle_dir)
        search_dirs = [self.bundle_dir, parent_dir, "/music", "/home/vipxpert/music"]
        for d in search_dirs:
            if not os.path.isdir(d):
                continue
            for root, _, files in os.walk(d):
                for f in files:
                    if f.lower().endswith(exts):
                        fpath = os.path.join(root, f)
                        try:
                            st = os.stat(fpath)
                            stem = os.path.splitext(f)[0]
                            cb = os.path.join(self.cache_dir, stem + ".bin")
                            mj = os.path.join(self.cache_dir, stem + ".meta.json")
                            sig = {"path": fpath, "mtime": round(st.st_mtime, 3), "size": st.st_size, "mode": "fit_128x64"}
                            if not os.path.exists(cb) or not os.path.exists(mj):
                                self._transcode_worker(fpath, cb, mj, sig)
                        except Exception:
                            pass

    def _ensure_mmap(self) -> bool:
        # Resolves active video source and validates per-video cache mmap
        status_str, title_str, artist_str, _, _, _, _ = self.app._extract_media_info()
        # Comment (workaround): Fast-return before _find_matching_video() when (playing, title, artist) is unchanged and _mmap_buf is already open so draw() never runs os.walk() across 5 directories at 30Hz.
        query_key = (status_str == "playing", title_str, artist_str)
        if getattr(self, "_cached_query_key", None) == query_key and self._current_video_path and self._mmap_buf is not None:
            return True
        src = self._find_matching_video(title_str, artist_str) if (status_str == "playing") else None
        if not src:
            # Fallback to default or any video if available
            exts = (".mp4", ".mkv", ".webm")
            for root, _, files in os.walk(self.bundle_dir):
                for f in files:
                    if f.lower().endswith(exts):
                        src = os.path.join(root, f)
                        break
                if src: break

        if not src:
            return False

        self._cached_query_key = query_key
        # Comment: Fast-return when the active track's .bin mmap is already open so draw() never performs disk stat() or meta.json reads on the 30 FPS UI render path.
        if self._current_video_path == src and self._mmap_buf is not None:
            return True
        if self._current_video_path != src:
            self._close_mmap()
            self._current_video_path = src
            self._smooth_ts = 0.0

        stem = os.path.splitext(os.path.basename(src))[0]
        cb = os.path.join(self.cache_dir, stem + ".bin")
        mj = os.path.join(self.cache_dir, stem + ".meta.json")

        try:
            st = os.stat(src)
            cur_sig = {"path": src, "mtime": round(st.st_mtime, 3), "size": st.st_size, "mode": "fit_128x64"}
        except Exception:
            cur_sig = None

        valid_cache = False
        if os.path.exists(cb) and os.path.exists(mj):
            try:
                with open(mj, "r") as mf:
                    meta = json.load(mf)
                if cur_sig and meta.get("path") == cur_sig["path"] and meta.get("mtime") == cur_sig["mtime"] and meta.get("size") == cur_sig["size"]:
                    self.fps = float(meta.get("fps", 30.0))
                    valid_cache = True
            except Exception:
                pass

        if not valid_cache and not self._converting:
            self._converting = True
            self._convert_err = None
            self._convert_progress = 0.0
            threading.Thread(target=self._transcode_worker, args=(src, cb, mj, cur_sig), daemon=True, name="MVTranscode").start()
            return False

        if self._mmap_buf is not None:
            return True

        if valid_cache and os.path.exists(cb):
            sz = os.path.getsize(cb)
            if sz >= self.RAW_FRAME_BYTES:
                try:
                    self._mmap_fd = os.open(cb, os.O_RDONLY)
                    self._mmap_buf = mmap.mmap(self._mmap_fd, sz, mmap.MAP_SHARED, mmap.PROT_READ)
                    try:
                        self._mmap_buf.madvise(mmap.MADV_SEQUENTIAL)
                    except Exception:
                        pass
                    self._total_frames = sz // self.RAW_FRAME_BYTES
                    return True
                except Exception:
                    pass
        return False

    def draw(self):
        # Monotonic time-interpolated video frame renderer with smooth PLL drift correction
        self.display.has_active_marquee = True
        status_str, _, _, pos, _, _, _ = self.app._extract_media_info()
        playing = (status_str == "playing")
        now = time.monotonic()

        if self._ensure_mmap() and self._total_frames > 0:
            is_stale = (self._smooth_ts == 0.0 or (now - self._smooth_ts) > 1.0)
            if playing:
                # Comment: MPV/Bluetooth reports pos=0.0 briefly during pipeline init before emitting its first real timestamp; lock immediately on the 0.0->pos transition and use proportional 15% drift slewing for normal jitter (0.04s..1.50s) so video never hard-snaps backward/forward half a second after startup.
                dt_step = 0.0 if is_stale else max(0.0, min(0.10, now - self._smooth_ts))
                if is_stale or (pos > 0.0 and getattr(self, "_last_raw_pos", 0.0) <= 0.0):
                    self._smooth_pos = pos
                else:
                    self._smooth_pos += dt_step
                    if pos > 0.0:
                        err = pos - self._smooth_pos
                        if abs(err) > 1.50:
                            self._smooth_pos = pos
                        elif abs(err) > 0.04:
                            self._smooth_pos += err * 0.15
                self._last_raw_pos = pos
                f_idx = int(max(0.0, self._smooth_pos) * self.fps) % self._total_frames
            elif pos > 0.0:
                self._smooth_pos = pos
                f_idx = int(pos * self.fps) % self._total_frames
            else:
                if not is_stale:
                    self._smooth_pos += max(0.0, min(0.25, now - self._smooth_ts))
                f_idx = int(self._smooth_pos * self.fps) % self._total_frames
            self._smooth_ts = now
            # Comment (invariant): Display.clear() wipes self.display.image to black and resets _pwm_gray_planes=None at the start of every composite_frame(); cache the quantized (b_ph0, ph1_full, lo_full, sig) tuple on MusicVideoScene so duplicate f_idx ticks restore the canvas in O(1) without re-running 3x PIL .point() LUTs.
            if getattr(self, "_last_drawn_vid_frame", None) == (self._current_video_path, f_idx) and getattr(self, "_cached_vid_planes", None) is not None:
                c_ph0, c_ph1, c_lo, c_sig = self._cached_vid_planes
                self.display.image.paste(c_ph0, (0, 0))
                self.display._pwm_gray_planes = (c_ph1.copy(), c_lo.copy(), c_sig)
                self.display.has_active_marquee = True
                return
            self._last_drawn_vid_frame = (self._current_video_path, f_idx)
            off = f_idx * self.RAW_FRAME_BYTES
            frame_bytes = self._mmap_buf[off : off + self.RAW_FRAME_BYTES]
            self.display.draw_gray_frame(0, 0, self.VID_W, self.VID_H, frame_bytes)
            gp = getattr(self.display, "_pwm_gray_planes", None)
            if gp is not None:
                self._cached_vid_planes = (self.display.image.copy(), gp[0].copy(), gp[1].copy(), gp[2])
            return

        if self._converting:
            render_progress_alert(self.display, "Caching MV", f"@ {self.fps:g}fps Grayscale", getattr(self, "_convert_progress", 0.0))
        elif self._convert_err:
            render_progress_alert(self.display, "Transcode Err", f"{self._convert_err} | 0: Retry | Log: /tmp/bad_apple_err.txt")
        else:
            render_progress_alert(self.display, "No Video File", "Drop matching .mp4 in /music or scene folder")
