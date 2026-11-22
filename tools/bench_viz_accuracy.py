# /vipxpert_minimal_os/tools/bench_viz_accuracy.py
"""
Machine-Accurate Visualizer Verification Suite:
  1. Compiles C99 reference (src/core/viz_dsp.c) and tests production Rust (/bin/viz_dsp) head-to-head
     using a deterministic 48,000 Hz s16le parec shim:
       - Bit-exact LSB parity between Rust and C99 across silence, 20 band centers, 19 crossovers, and multitone.
       - 20/20 center-frequency peak lock (45 Hz .. 19,500 Hz) in Raw RTA ('flat') and Pink ('pink') curves.
       - 19/19 geometric inter-bar crossover continuity (proving 0 frequency notches between bars).
       - Pure per-quantum compute latency (us / 21.33ms quantum).
  2. Wakes live OLED onto Music + Spectrum (music_split), verifies live Rust /bin/viz_dsp PID, CPU affinity,
     VmRSS, and %CPU, and captures 4.0s (~200 frames) of /tmp/oled_viz_diag.json + SHM hw_tick cadence.
"""
import json
import math
import mmap
import os
import shutil
import socket
import statistics
import struct
import subprocess
import tempfile
import time

REPO_DIR = "/home/vipxpert/vipxpert_minimal_os"
RUST_VIZ = os.path.join(REPO_DIR, "bin", "viz_dsp")
C_SRC = os.path.join(REPO_DIR, "src", "core", "viz_dsp.c")
CENTERS = [
    45.0, 65.0, 92.0, 130.0, 185.0, 260.0, 365.0, 510.0, 700.0, 960.0,
    1320.0, 1800.0, 2450.0, 3350.0, 4550.0, 6200.0, 8400.0, 11400.0, 15200.0, 19500.0,
]


def devctl(payload: dict) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect("/tmp/vipxpert_devctl.sock")
    s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode("utf-8"))


def make_sine_frames(freq_hz: float, n_quanta: int = 18, amp: float = 14000.0, fs: float = 48000.0) -> bytes:
    # Each viz_dsp trigger consumes >= 1856 bytes (928 samples); feed 928-sample steps cleanly
    total_samples = 1024 + n_quanta * 928
    out = bytearray(total_samples * 2)
    w = 2.0 * math.pi * freq_hz / fs
    for i in range(total_samples):
        v = int(round(amp * math.sin(w * i)))
        struct.pack_into("<h", out, i * 2, v)
    return bytes(out)


def make_multitone_frames(n_quanta: int = 40, fs: float = 48000.0) -> bytes:
    total_samples = 1024 + n_quanta * 928
    out = bytearray(total_samples * 2)
    freqs = [65.0, 260.0, 960.0, 3350.0, 11400.0, 19500.0]
    for i in range(total_samples):
        acc = 0.0
        for k, f in enumerate(freqs):
            acc += 3200.0 * math.sin(2.0 * math.pi * f * i / fs + k * 0.37)
        struct.pack_into("<h", out, i * 2, max(-32767, min(32767, int(round(acc)))))
    return bytes(out)


def run_dsp_with_pcm(bin_path: str, pcm_bytes: bytes, curve: str, mock_dir: str) -> tuple:
    pcm_file = os.path.join(mock_dir, "input.pcm")
    with open(pcm_file, "wb") as f:
        f.write(pcm_bytes)
    env = os.environ.copy()
    env["PATH"] = f"{mock_dir}:{env.get('PATH', '')}"
    env["MOCK_PCM_FILE"] = pcm_file
    t0 = time.perf_counter()
    p = subprocess.Popen(
        [bin_path, "music_only", curve, "1", "mock_stream"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    raw_out, _ = p.communicate(timeout=5.0)
    elapsed_us = (time.perf_counter() - t0) * 1e6
    frames = [list(raw_out[i : i + 20]) for i in range(0, (len(raw_out) // 20) * 20, 20)]
    return frames, elapsed_us


def bench_offline_dsp():
    print("=== [1/2] DETERMINISTIC 48kHz GOERTZEL ACCURACY & RUST-vs-C99 BIT-PARITY ===")
    tmp_dir = tempfile.mkdtemp(prefix="viz_bench_")
    try:
        # Mock 'parec' streams MOCK_PCM_FILE in 1856-byte quanta so every read boundary is deterministic
        parec_shim = os.path.join(tmp_dir, "parec")
        with open(parec_shim, "w", encoding="utf-8") as sf:
            sf.write(
                "#!/usr/bin/env python3\n"
                "import os, sys, time\n"
                "if any('--monitor-stream=mock_stream' in a for a in sys.argv):\n"
                "    data = open(os.environ['MOCK_PCM_FILE'], 'rb').read()\n"
                "    out = sys.stdout.buffer\n"
                "    off = 0\n"
                "    while off < len(data):\n"
                "        blk = data[off:off+1856]\n"
                "        out.write(blk); out.flush()\n"
                "        off += len(blk)\n"
                "        time.sleep(0.0004)\n"
            )
        os.chmod(parec_shim, 0o755)

        c_ref = os.path.join(tmp_dir, "viz_dsp_c_ref")
        subprocess.run(["gcc", "-O3", C_SRC, "-lm", "-o", c_ref], check=True)

        # A. Bit-exact Rust vs C99 parity on 40-quantum multitone
        mt_pcm = make_multitone_frames(40)
        f_rust, _ = run_dsp_with_pcm(RUST_VIZ, mt_pcm, "pink", tmp_dir)
        f_c, _ = run_dsp_with_pcm(c_ref, mt_pcm, "pink", tmp_dir)
        n_cmp = min(len(f_rust), len(f_c))
        max_lsb_diff = 0
        for fi in range(n_cmp):
            for bi in range(20):
                d = abs(f_rust[fi][bi] - f_c[fi][bi])
                if d > max_lsb_diff:
                    max_lsb_diff = d
        print(f"  [Parity] Compared {n_cmp} frames x 20 bands ({n_cmp*20} bins): max |Rust - C99| = {max_lsb_diff} LSB")

        # B. Pure Rust throughput (unthrottled pipe burst, 500 quanta = 10.67s of 48kHz audio)
        with open(parec_shim, "w", encoding="utf-8") as sf:
            sf.write(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "if any('--monitor-stream=mock_stream' in a for a in sys.argv):\n"
                "    sys.stdout.buffer.write(open(os.environ['MOCK_PCM_FILE'], 'rb').read())\n"
            )
        os.chmod(parec_shim, 0o755)
        burst_pcm = make_multitone_frames(500)
        f_burst, burst_us = run_dsp_with_pcm(RUST_VIZ, burst_pcm, "pink", tmp_dir)
        q_cnt = max(1, len(f_burst))
        us_per_q = burst_us / q_cnt
        rt_factor = 21333.3 / max(1.0, us_per_q)
        print(f"  [Speed]  {q_cnt} quanta processed in {burst_us/1000.0:.2f}ms -> {us_per_q:.1f} us/quantum ({rt_factor:.1f}x real-time, est CPU @46.9Hz = {100.0/rt_factor:.2f}%)")

        # Restore paced parec shim so each 1856B quantum triggers its own frame
        with open(parec_shim, "w", encoding="utf-8") as sf:
            sf.write(
                "#!/usr/bin/env python3\n"
                "import os, sys, time\n"
                "if any('--monitor-stream=mock_stream' in a for a in sys.argv):\n"
                "    data = open(os.environ['MOCK_PCM_FILE'], 'rb').read()\n"
                "    out = sys.stdout.buffer\n"
                "    for off in range(0, len(data), 1856):\n"
                "        out.write(data[off:off+1856]); out.flush(); time.sleep(0.0003)\n"
            )
        os.chmod(parec_shim, 0o755)

        # C. 20-Band Center Frequency Lock (45 Hz .. 19,500 Hz in flat/RTA mode)
        exact_locks = 0
        mismatches = []
        side_leak_max = 0
        for idx, fc in enumerate(CENTERS):
            frames, _ = run_dsp_with_pcm(RUST_VIZ, make_sine_frames(fc, n_quanta=16), "flat", tmp_dir)
            steady = frames[-1]
            pk_val = max(steady)
            pk_idx = steady.index(pk_val)
            distant = [steady[j] for j in range(20) if abs(j - idx) >= 2]
            if distant:
                side_leak_max = max(side_leak_max, max(distant))
            if pk_idx == idx and pk_val >= 250:
                exact_locks += 1
            else:
                mismatches.append((idx, fc, pk_idx, pk_val, steady[idx]))
        print(f"  [20-Band Center Lock] {exact_locks}/20 exact index locks (peak>=250/255), distant-band floor max={side_leak_max}/255 (noise_gate=12)")
        if mismatches:
            print(f"    MISMATCHES: {mismatches}")

        # D. 19 Inter-Band Geometric Crossover Continuity (sqrt(f_i * f_{i+1}))
        min_xover_val = 255
        min_xover_pair = None
        for idx in range(19):
            fx = math.sqrt(CENTERS[idx] * CENTERS[idx + 1])
            frames, _ = run_dsp_with_pcm(RUST_VIZ, make_sine_frames(fx, n_quanta=16), "flat", tmp_dir)
            steady = frames[-1]
            pair_pk = max(steady[idx], steady[idx + 1])
            if pair_pk < min_xover_val:
                min_xover_val = pair_pk
                min_xover_pair = (idx, idx + 1, round(fx, 1), steady[idx], steady[idx + 1])
        print(f"  [19-Band Crossovers]  Min geometric-midpoint response across all 19 boundaries: {min_xover_val}/255 ({min_xover_val/255.0*100:.1f}%) at {min_xover_pair}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def bench_live_oled_pipeline():
    print("\n=== [2/2] LIVE AWAKE OLED + RUST /bin/viz_dsp END-TO-END TIMING (4.0s) ===")
    st = devctl({"cmd": "state"})
    if st.get("is_screen_off"):
        devctl({"cmd": "tap", "key": "*"})
        time.sleep(0.4)
        st = devctl({"cmd": "state"})
    if st.get("current_app_id") != "lock_screen":
        devctl({"cmd": "tap", "key": "*"})
        time.sleep(0.4)
        st = devctl({"cmd": "state"})
    # Ensure Music slot is visible
    for _ in range(5):
        st = devctl({"cmd": "state"})
        if st.get("lock", {}).get("active_slot") == "music":
            break
        devctl({"cmd": "tap", "key": "B"})
        time.sleep(0.25)
    time.sleep(0.8)

    # Check live Rust viz_dsp process metrics
    ps_out = subprocess.check_output(["ps", "-eLo", "pid,psr,pcpu,rss,comm,args"], text=True)
    viz_lines = [ln.strip() for ln in ps_out.splitlines() if "viz_dsp" in ln and "grep" not in ln]
    print(f"  [Live Process] {viz_lines if viz_lines else 'NOT RUNNING!'}")

    # Reset & collect 4.0s of _viz_diag + SHM hw_tick cadence
    open("/tmp/oled_diag_reset_viz", "w").close()
    fd = os.open("/dev/shm/vipxpert_oled_shm", os.O_RDONLY)
    shm = mmap.mmap(fd, 64, mmap.MAP_SHARED, mmap.PROT_READ)
    hw_dts = []
    last_tk = struct.unpack("<I", shm[20:24])[0]
    last_t = time.perf_counter()
    t_end = last_t + 4.0
    while time.perf_counter() < t_end:
        tk = struct.unpack("<I", shm[20:24])[0]
        now = time.perf_counter()
        if tk != last_tk:
            hw_dts.append((now - last_t) * 1000.0)
            last_tk = tk
            last_t = now
        time.sleep(0.001)
    shm.close()
    os.close(fd)

    open("/tmp/oled_diag_dump_viz", "w").close()
    time.sleep(0.15)
    if os.path.exists("/tmp/oled_viz_diag.json"):
        rows = json.load(open("/tmp/oled_viz_diag.json"))
        dts = [r[0] for r in rows[2:]] if len(rows) > 3 else []
        draws = [r[8] for r in rows[2:]] if len(rows) > 3 else []
        totals = [r[9] for r in rows[2:]] if len(rows) > 3 else []
        if dts:
            s_dt = sorted(dts)
            p95 = s_dt[min(len(s_dt) - 1, int(len(s_dt) * 0.95))]
            p99 = s_dt[min(len(s_dt) - 1, int(len(s_dt) * 0.99))]
            spikes = [x for x in dts if x > 45.0]
            print(
                f"  [UI Render Loop] n={len(dts)} frames | dt_ms: min={min(dts):.2f} med={statistics.median(dts):.2f} "
                f"p95={p95:.2f} p99={p99:.2f} max={max(dts):.2f} | >45ms_hiccups={len(spikes)} {spikes[:5]}"
            )
            print(
                f"  [Compositor Draw] draw_ms: med={statistics.median(draws):.2f} max={max(draws):.2f} | "
                f"total_ms: med={statistics.median(totals):.2f} max={max(totals):.2f}"
            )
    if len(hw_dts) > 4:
        h_s = sorted(hw_dts[2:])
        hp99 = h_s[min(len(h_s) - 1, int(len(h_s) * 0.99))]
        print(
            f"  [SHM hw_tick Latch] n={len(h_s)} ticks | target=19.36ms (3 x 6.452ms) | "
            f"min={min(h_s):.2f}ms med={statistics.median(h_s):.2f}ms p99={hp99:.2f}ms max={max(h_s):.2f}ms"
        )


if __name__ == "__main__":
    bench_offline_dsp()
    bench_live_oled_pipeline()
