#!/usr/bin/env python3
import os
import sys
import time
import math
import struct
import subprocess

RATE = 48000
CHANNELS = 2
CHUNK_FRAMES = 256
CHUNK_BYTES = CHUNK_FRAMES * CHANNELS * 2

def get_pw_quantum_info(env):
    try:
        cmd = ["runuser", "-u", "vipxpert", "--", "pw-cli", "info", "all"] if os.geteuid() == 0 else ["pw-cli", "info", "all"]
        out = subprocess.check_output(cmd, env=env, text=True, stderr=subprocess.DEVNULL, timeout=1.5)
        lat = "unknown"
        in_bz = False
        for line in out.splitlines():
            if "bluez_input." in line:
                in_bz = True
            elif in_bz and "node.latency" in line:
                lat = line.split("=", 1)[1].strip().strip('"')
                break
        return lat
    except Exception:
        return "unknown"

def main():
    env = os.environ.copy()
    if os.geteuid() == 0:
        env["XDG_RUNTIME_DIR"] = "/run/user/1000"
        env["PULSE_RUNTIME_PATH"] = "/run/user/1000/pulse"

    mon = "alsa_output.platform-soc_ahub0_mach.stereo-fallback.monitor"
    try:
        cmd_s = ["runuser", "-u", "vipxpert", "--", "pactl", "get-default-sink"] if os.geteuid() == 0 else ["pactl", "get-default-sink"]
        out = subprocess.check_output(cmd_s, env=env, text=True, timeout=1.0).strip()
        if out:
            mon = f"{out}.monitor"
    except Exception:
        pass

    bz_lat = get_pw_quantum_info(env)
    cmd = ["parec", "--format=s16le", f"--rate={RATE}", f"--channels={CHANNELS}", f"--device={mon}", "--latency-msec=10"]
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", "vipxpert", "--"] + cmd

    print(f"[catch_zeros] SinkMonitor={mon} | bluez_input.latency={bz_lat}")
    print("[catch_zeros] Waiting for stutters/zero-runs/splices (Ctrl+C to stop)...")
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    try:
        pre = int(RATE * 0.35) * CHANNELS * 2
        while pre > 0:
            d = p.stdout.read(min(4096, pre))
            if not d:
                break
            pre -= len(d)

        total_frames = 0
        in_zero_run = 0
        zero_start_frame = 0
        last_nonzero_l = 0
        last_nonzero_r = 0
        last_stutter_t = None
        stutter_num = 0
        signal_active = False
        recent_peak = 0

        while True:
            buf = p.stdout.read(CHUNK_BYTES)
            if not buf or len(buf) < CHUNK_BYTES:
                break
            samples = struct.unpack(f"<{CHUNK_FRAMES * CHANNELS}h", buf)

            chunk_peak = 0
            for i in range(0, len(samples), 2):
                l, r = samples[i], samples[i + 1]
                a = max(abs(l), abs(r))
                if a > chunk_peak:
                    chunk_peak = a
                if a > 50:
                    signal_active = True

                if signal_active and l == 0 and r == 0:
                    if in_zero_run == 0:
                        zero_start_frame = total_frames
                    in_zero_run += 1
                else:
                    if in_zero_run >= 8 and recent_peak >= 400 and in_zero_run <= 8192:
                        stutter_num += 1
                        t_s = zero_start_frame / RATE
                        dur_ms = (in_zero_run / RATE) * 1000.0
                        q_est = in_zero_run / 1024.0
                        dt_str = f"dt={t_s - last_stutter_t:6.2f}s" if last_stutter_t is not None else "dt=  FIRST"
                        last_stutter_t = t_s
                        print(
                            f"[STUTTER #{stutter_num:02d}] t={t_s:7.2f}s ({dt_str}) | "
                            f"zeros={in_zero_run:4d} ({dur_ms:5.2f}ms, ~{q_est:.2f}x1024q) | "
                            f"edge_before=({last_nonzero_l:+6d},{last_nonzero_r:+6d}) -> edge_after=({l:+6d},{r:+6d}) | "
                            f"peak_ref={recent_peak}",
                            flush=True
                        )
                    in_zero_run = 0
                    last_nonzero_l = l
                    last_nonzero_r = r

                total_frames += 1

            if chunk_peak > 0:
                recent_peak = int(recent_peak * 0.9 + chunk_peak * 0.1) if recent_peak else chunk_peak

    except KeyboardInterrupt:
        print(f"\n[catch_zeros] Stopped at t={(total_frames / RATE):.1f}s. Total stutters caught: {stutter_num}")
    finally:
        p.terminate()

if __name__ == "__main__":
    main()
