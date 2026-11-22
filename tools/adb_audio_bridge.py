# /vipxpert_minimal_os/tools/adb_audio_bridge.py
"""
Standalone ADB-to-PipeWire 48kHz Stereo Audio Bridge (scrcpy-server v2.7 raw PCM).
Streams Android internal playback audio over USB ADB into PipeWire ('vip_usb_out')
and publishes live signal-activity telemetry to /tmp/vipxpert_adb_audio.json so
MediaArbiter can enforce Last-Active Takeover across Bluetooth, Local, and USB.
"""
import argparse
import array
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

BASE_DIR = "/home/vipxpert/vipxpert_minimal_os"
ADB_BIN = os.path.join(BASE_DIR, "bin", "adb")
SCRCPY_JAR = os.path.join(BASE_DIR, "bin", "scrcpy-server.jar")
STATE_FILE = "/tmp/vipxpert_adb_audio.json"
SCID = "12345678"
LOCAL_PORT = 27183
SILENCE_PEAK_THRESHOLD = 28      # ~ -61 dBFS on 16-bit signed PCM
HOLD_PLAYING_SECONDS = 1.6       # Keep playing=True across brief track gaps


def write_state(payload: dict):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _has_usb_adb_hardware() -> bool:
    """Zero-fork sysfs check: returns True only when a physical USB interface exposes Android ADB (Class ff, SubClass 42, Protocol 01)."""
    import glob
    for if_dir in glob.glob("/sys/bus/usb/devices/*:*"):
        try:
            cls = open(os.path.join(if_dir, "bInterfaceClass")).read().strip().lower()
            sub = open(os.path.join(if_dir, "bInterfaceSubClass")).read().strip().lower()
            pro = open(os.path.join(if_dir, "bInterfaceProtocol")).read().strip().lower()
            if cls == "ff" and sub == "42" and pro == "01":
                return True
        except Exception:
            continue
    return False


def get_authorized_phone():
    """Returns (serial, pretty_model) for the first authorized ADB device, or (None, None)."""
    # Comment (invariant): Never fork `adb devices -l` every 1.5s when no Android ADB interface exists in /sys/bus/usb/devices; background adb forks collided with Bluetooth/OLED scheduling (DP-495).
    if not os.path.exists(ADB_BIN) or not _has_usb_adb_hardware():
        return None, None
    try:
        out = subprocess.check_output([ADB_BIN, "devices", "-l"], stderr=subprocess.DEVNULL, timeout=3.0).decode("utf-8", errors="ignore")
    except Exception:
        return None, None
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serial = parts[0]
            m = re.search(r"model:([^\s]+)", line)
            raw_model = m.group(1) if m else "Android USB"
            pretty = raw_model.replace("_", " ").title()
            return serial, pretty
    return None, None


def _build_pacat_cmd():
    base = [
        "/usr/local/bin/pacat" if os.path.exists("/usr/local/bin/pacat") else "pacat",
        "--playback",
        "--client-name=vip_usb_out",
        "--stream-name=vip_usb_out",
        "--rate=48000",
        "--channels=2",
        "--format=s16le",
        "--volume=65536",
        "--latency-msec=45",
    ]
    if os.geteuid() == 0:
        return ["runuser", "-u", "vipxpert", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000"] + base
    return base


class AdbAudioBridge:
    def __init__(self, self_test_sec: float = 0.0):
        self.self_test_sec = self_test_sec
        self.running = True
        self.server_proc = None
        self.pacat_proc = None
        self.sock = None
        self.server_logs = []

    def stop(self, *_):
        self.running = False
        for p in (self.pacat_proc, self.server_proc):
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def _cleanup_remote(self, serial: str):
        subprocess.run([ADB_BIN, "-s", serial, "forward", "--remove", f"tcp:{LOCAL_PORT}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([ADB_BIN, "-s", serial, "shell", "pkill -f com.genymobile.scrcpy.Server 2>/dev/null || true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _drain_logs(self, pipe):
        try:
            for line in iter(pipe.readline, b""):
                txt = line.decode("utf-8", errors="ignore").strip()
                if txt:
                    self.server_logs.append(txt)
                    if len(self.server_logs) > 20:
                        self.server_logs.pop(0)
        except Exception:
            pass

    def stream_session(self, serial: str, model: str) -> bool:
        self._cleanup_remote(serial)
        push = subprocess.run([ADB_BIN, "-s", serial, "push", SCRCPY_JAR, "/data/local/tmp/scrcpy-server.jar"], capture_output=True, text=True, timeout=8.0)
        if push.returncode != 0:
            print(f"[ADB-Audio] Push failed: {push.stderr.strip()}", flush=True)
            return False

        fwd = subprocess.run([ADB_BIN, "-s", serial, "forward", f"tcp:{LOCAL_PORT}", f"localabstract:scrcpy_{SCID}"], capture_output=True, text=True, timeout=4.0)
        if fwd.returncode != 0:
            print(f"[ADB-Audio] Forward failed: {fwd.stderr.strip()}", flush=True)
            return False

        srv_cmd = [
            ADB_BIN, "-s", serial, "shell",
            f"CLASSPATH=/data/local/tmp/scrcpy-server.jar app_process / com.genymobile.scrcpy.Server 2.7 "
            f"scid={SCID} tunnel_forward=true video=false audio=true control=false "
            f"audio_codec=raw send_dummy_byte=false send_device_meta=false send_codec_meta=false send_frame_meta=false"
        ]
        self.server_proc = subprocess.Popen(srv_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=self._drain_logs, args=(self.server_proc.stdout,), daemon=True).start()

        # Comment (order): adb forward binds localhost immediately and returns EOF until Android app_process finishes binding localabstract:scrcpy_<scid> (~0.85s), so wait and verify non-empty readiness before entering the audio loop.
        time.sleep(0.85)
        sock = None
        first_chunk = b""
        deadline = time.time() + 3.5
        while self.running and time.time() < deadline:
            if self.server_proc.poll() is not None:
                break
            try:
                s = socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=0.8)
                s.settimeout(0.6)
                probe = s.recv(4096)
                if probe:
                    sock = s
                    first_chunk = probe
                    break
                s.close()
            except socket.timeout:
                sock = s
                break
            except Exception:
                pass
            time.sleep(0.2)

        if not sock:
            print(f"[ADB-Audio] Could not connect to scrcpy audio socket. Logs: {self.server_logs}", flush=True)
            self._cleanup_remote(serial)
            return False

        self.sock = sock
        env = os.environ.copy()
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
        self.pacat_proc = subprocess.Popen(_build_pacat_cmd(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

        last_sound_ts = 0.0
        last_state_write = 0.0
        total_bytes = 0
        max_peak_seen = 0
        t_start = time.time()
        print(f"[ADB-Audio] Connected 48kHz stereo PCM stream from {model} ({serial}) -> PipeWire vip_usb_out", flush=True)

        try:
            pending = first_chunk
            while self.running:
                if self.self_test_sec > 0 and (time.time() - t_start) >= self.self_test_sec:
                    break
                if pending:
                    chunk = pending
                    pending = b""
                else:
                    try:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                    except socket.timeout:
                        chunk = b""

                now = time.time()
                peak = 0
                if chunk:
                    total_bytes += len(chunk)
                    even_len = len(chunk) & ~1
                    if even_len > 0:
                        samples = array.array("h")
                        samples.frombytes(chunk[:even_len])
                        peak = max((abs(x) for x in samples), default=0)
                        if peak > max_peak_seen:
                            max_peak_seen = peak
                    if peak >= SILENCE_PEAK_THRESHOLD:
                        last_sound_ts = now

                    if self.pacat_proc and self.pacat_proc.stdin:
                        try:
                            self.pacat_proc.stdin.write(chunk)
                            self.pacat_proc.stdin.flush()
                        except Exception:
                            break

                is_playing = (now - last_sound_ts) <= HOLD_PLAYING_SECONDS if last_sound_ts > 0 else False
                if (now - last_state_write) >= 0.25:
                    write_state({
                        "connected": True,
                        "serial": serial,
                        "model": model,
                        "playing": is_playing,
                        "peak": peak,
                        "updated_at": now,
                    })
                    last_state_write = now
        finally:
            if self.pacat_proc:
                try:
                    self.pacat_proc.stdin.close()
                    self.pacat_proc.terminate()
                except Exception:
                    pass
            try:
                sock.close()
            except Exception:
                pass
            if self.server_proc and self.server_proc.poll() is None:
                try:
                    self.server_proc.terminate()
                except Exception:
                    pass
            self._cleanup_remote(serial)
            write_state({"connected": False, "serial": "", "model": "", "playing": False, "peak": 0, "updated_at": time.time()})

        if self.self_test_sec > 0:
            print(f"[ADB-Audio] Self-test complete: bytes_rx={total_bytes}, max_peak={max_peak_seen}, logs={self.server_logs}", flush=True)
        return True

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        if os.path.exists(ADB_BIN):
            subprocess.run([ADB_BIN, "start-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5.0)
        write_state({"connected": False, "serial": "", "model": "", "playing": False, "peak": 0, "updated_at": time.time()})
        while self.running:
            serial, model = get_authorized_phone()
            if serial:
                ok = self.stream_session(serial, model)
                if self.self_test_sec > 0:
                    return 0 if ok else 1
                time.sleep(1.0)
            else:
                write_state({"connected": False, "serial": "", "model": "", "playing": False, "peak": 0, "updated_at": time.time()})
                if self.self_test_sec > 0:
                    print("[ADB-Audio] No authorized ADB phone found.", flush=True)
                    return 1
                time.sleep(1.5)
        return 0


def main():
    parser = argparse.ArgumentParser(description="ADB USB Audio Bridge for VIPXpert OS")
    parser.add_argument("--self-test", type=float, default=0.0, help="Run a timed self-test in seconds and exit")
    args = parser.parse_args()
    bridge = AdbAudioBridge(self_test_sec=args.self_test)
    return bridge.run()


if __name__ == "__main__":
    sys.exit(main())
