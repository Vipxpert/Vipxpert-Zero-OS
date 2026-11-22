# /vipxpert_minimal_os/src/core/sound_manager.py
"""
Non-blocking System Sound Effects Manager (/sounds/ folder backend).
Automatically provisions clean 48kHz 16-bit default WAV chimes into /sounds/ if missing
(never overwriting user-customized .wav/.ogg/.mp3/.flac files) and dispatches event playback
on Cores 0,2 via PipeWire (pw-play / paplay) without touching Core 1 (OLED) or Core 3 (RT Audio).
"""
import math
import os
import struct
import subprocess
import threading
import time
import wave

BASE_DIR = "/home/vipxpert/vipxpert_minimal_os"
SOUNDS_DIR = os.path.join(BASE_DIR, "sounds")

# Default synthesized chime specs: event_name -> list of (freq_hz, duration_s, gain)
DEFAULT_CHIME_SPECS = {
    "boot": [(523.25, 0.075, 0.22), (659.25, 0.085, 0.24), (783.99, 0.130, 0.26)],
    "bt_connect": [(587.33, 0.070, 0.25), (880.00, 0.120, 0.28)],
    "bt_disconnect": [(783.99, 0.075, 0.24), (523.25, 0.125, 0.22)],
    "battery_low": [(440.00, 0.090, 0.30), (0.0, 0.045, 0.0), (370.00, 0.130, 0.30)],
    "charge_start": [(659.25, 0.060, 0.22), (987.77, 0.105, 0.25)],
    "usb_connect": [(698.46, 0.055, 0.22), (1046.50, 0.095, 0.24)],
}


def _synthesize_wav(filepath: str, notes: list, sample_rate: int = 48000):
    frames = bytearray()
    for freq, dur, amp in notes:
        n_samp = max(1, int(sample_rate * dur))
        fade = max(1, min(int(sample_rate * 0.012), n_samp // 3))
        for i in range(n_samp):
            if freq <= 0.0 or amp <= 0.0:
                val = 0
            else:
                env = 1.0
                if i < fade:
                    env = 0.5 * (1.0 - math.cos(math.pi * i / fade))
                elif i >= n_samp - fade:
                    env = 0.5 * (1.0 - math.cos(math.pi * (n_samp - 1 - i) / fade))
                # Exponential natural bell decay + soft 2nd harmonic warmth
                decay = math.exp(-3.2 * (i / n_samp))
                t = i / sample_rate
                sig = math.sin(2.0 * math.pi * freq * t) + 0.18 * math.sin(4.0 * math.pi * freq * t)
                val = int(max(-32767, min(32767, sig * amp * env * decay * 28000.0)))
            frames += struct.pack("<h", val)
    tmp_path = filepath + ".tmp"
    with wave.open(tmp_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    os.replace(tmp_path, filepath)


class SystemSoundManager:
    SUPPORTED_EXTS = (".wav", ".ogg", ".flac", ".mp3")

    def __init__(self, db, sounds_dir: str = SOUNDS_DIR):
        self.db = db
        self.sounds_dir = sounds_dir
        self._last_play_ts = {}
        self._lock = threading.Lock()
        self.ensure_default_sounds()

    def ensure_default_sounds(self):
        """Creates /sounds/ and writes default 48kHz WAV chimes if no custom file exists for an event."""
        os.makedirs(self.sounds_dir, exist_ok=True)
        readme_p = os.path.join(self.sounds_dir, "README.txt")
        if not os.path.exists(readme_p):
            try:
                with open(readme_p, "w", encoding="utf-8") as f:
                    f.write(
                        "VIPXpert OS System Sounds Directory (/sounds/)\n"
                        "Drop custom .wav, .ogg, .flac, or .mp3 files named after any event:\n"
                        "  boot, bt_connect, bt_disconnect, battery_low, charge_start, usb_connect\n"
                        "Custom extensions take priority in order: .wav -> .ogg -> .flac -> .mp3.\n"
                    )
            except Exception:
                pass
        for event_name, spec in DEFAULT_CHIME_SPECS.items():
            if self.resolve_sound_file(event_name) is None:
                try:
                    _synthesize_wav(os.path.join(self.sounds_dir, f"{event_name}.wav"), spec)
                except Exception as e:
                    print(f"[SoundManager] Failed to synthesize {event_name}.wav: {e}", flush=True)

    def resolve_sound_file(self, event_name: str):
        for ext in self.SUPPORTED_EXTS:
            cand = os.path.join(self.sounds_dir, f"{event_name}{ext}")
            if os.path.isfile(cand) and os.path.getsize(cand) > 44:
                return cand
        return None

    def play(self, event_name: str, cooldown_s: float = 0.8) -> bool:
        """Fires non-blocking system sound playback on Cores 0,2 if enabled in settings."""
        try:
            if self.db and str(self.db.get_setting("system_sounds_enabled", "1")) == "0":
                return False
            vol_pct = int(float(self.db.get_setting("system_sounds_vol", "75"))) if self.db else 75
        except Exception:
            vol_pct = 75
        if vol_pct <= 0:
            return False

        path = self.resolve_sound_file(event_name)
        if not path:
            return False

        now = time.monotonic()
        with self._lock:
            if (now - self._last_play_ts.get(event_name, 0.0)) < cooldown_s:
                return False
            self._last_play_ts[event_name] = now

        vol_lin = max(0.05, min(1.0, vol_pct / 100.0))
        pw_play = "/usr/local/bin/pw-play" if os.path.exists("/usr/local/bin/pw-play") else "pw-play"
        if path.lower().endswith((".mp3", ".flac", ".ogg")) and not os.path.exists("/usr/local/bin/pw-play"):
            player_args = ["mpv", "--no-video", "--really-quiet", f"--volume={vol_pct}", path]
        else:
            player_args = [pw_play, f"--volume={vol_lin:.2f}", path]

        # Comment (invariant): Pin system sound helper strictly to Cores 0,2 so short chime playback never preempts Core-1 OLED_Compositor or Core-3 SCHED_FIFO 88 data-loop.0.
        if os.geteuid() == 0:
            cmd = ["taskset", "-c", "0,2", "runuser", "-u", "vipxpert", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000"] + player_args
        else:
            cmd = ["taskset", "-c", "0,2", "env", f"XDG_RUNTIME_DIR=/run/user/{os.getuid()}"] + player_args

        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except Exception:
            return False
