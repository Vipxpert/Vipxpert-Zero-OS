# /vipxpert_minimal_os/tools/diag_keypad_row1.py
"""
Row 1 (Keys 1, 2, 3, A on GPIO 226 / PH2) vs Rows 2..4 Electrical & Timing Diagnostic.
Modes:
  1) `--check-config` (non-intrusive): Verifies Keypad class scan cadence, Row-1 settle, and bounce window.
  2) `--interactive` (stops vipxpert_os.service temporarily, restores on exit): Samples GPIO 226..230 x Cols
     every 120us after driving each row LOW to measure exact RC fall-time (us), contact bounce %, and low duty!
"""
import argparse
import subprocess
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.hardware.keypad import Keypad


def check_config():
    kp = Keypad()
    row_scan_ms = (2 * kp.row0_settle_time + 2 * kp.settle_time + 4 * (2 * kp.sample_delay + 0.00018)) * 1000.0
    worst_sweep_ms = row_scan_ms + kp.master_poll_interval * 1000.0
    print(
        f"[Keypad Config] drive=OPEN_DRAIN+PULL_UP sweep={row_scan_ms:.2f}ms rest={kp.master_poll_interval*1000:.1f}ms "
        f"total_cycle={worst_sweep_ms:.2f}ms ({1000.0/worst_sweep_ms:.1f}Hz) "
        f"r12_settle={kp.row0_settle_time*1000:.2f}ms r34_settle={kp.settle_time*1000:.2f}ms "
        f"batch_vote={kp.min_valid_samples}/{kp.target_samples}@{kp.sample_delay*1000:.2f}ms "
        f"release_hyst={kp.release_debounce_sweeps}sw queue=call_soon_threadsafe"
    )
    assert worst_sweep_ms <= 12.0, f"full scan cycle too slow ({worst_sweep_ms:.2f}ms > 12ms)"
    return 0


def run_interactive(duration_s: float):
    import gpiod
    from gpiod.line import Direction, Value, Bias

    print(f"[Row1 Diag] Stopping vipxpert_os.service for {duration_s:.0f}s to claim /dev/gpiochip1 directly...")
    subprocess.run(["sudo", "systemctl", "stop", "vipxpert_os.service"], check=False)
    time.sleep(0.4)
    try:
        cfg = {}
        for r in Keypad.ROWS:
            cfg[r] = gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE)
        for c in Keypad.COLS:
            cfg[c] = gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP)
        req = gpiod.request_lines("/dev/gpiochip1", consumer="row1-diag", config=cfg)
        print(f"[Row1 Diag] Tap Row 1 keys (1, 2, 3, A) and Row 2 keys (4, 5, 6, B) now for {duration_s:.0f}s!")
        t_end = time.monotonic() + duration_s
        hits = {}
        while time.monotonic() < t_end:
            for r_i, r_pin in enumerate(Keypad.ROWS):
                t0 = time.perf_counter()
                req.set_value(r_pin, Value.INACTIVE)
                first_low_us = [None, None, None, None]
                low_counts = [0, 0, 0, 0]
                for _ in range(25):  # 25 x 120us = 3.0ms high-speed waveform capture
                    time.sleep(0.00012)
                    dt_us = int((time.perf_counter() - t0) * 1_000_000)
                    for c_i, c_pin in enumerate(Keypad.COLS):
                        if req.get_value(c_pin) == Value.INACTIVE:
                            low_counts[c_i] += 1
                            if first_low_us[c_i] is None:
                                first_low_us[c_i] = dt_us
                req.set_value(r_pin, Value.ACTIVE)
                time.sleep(0.0002)
                for c_i, cnt in enumerate(low_counts):
                    if cnt >= 2:
                        k = Keypad.KEYMAP[r_i][c_i]
                        bounce_pct = round((1.0 - cnt / 25.0) * 100.0, 1)
                        prev_t = hits.get(k, 0.0)
                        if time.monotonic() - prev_t > 0.18:
                            hits[k] = time.monotonic()
                            print(f"  KEY '{k}' (Row {r_i+1} GPIO {r_pin} x Col {c_i+1} GPIO {Keypad.COLS[c_i]}): fall_time={first_low_us[c_i]}us low_duty={cnt}/25 bounce={bounce_pct}%", flush=True)
            time.sleep(0.006)
        req.release()
    finally:
        print("[Row1 Diag] Restarting vipxpert_os.service...")
        subprocess.run(["sudo", "systemctl", "start", "vipxpert_os.service"], check=False)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-config", action="store_true")
    ap.add_argument("--interactive", type=float, default=0.0, help="Seconds to capture sub-ms Row 1 waveforms")
    args = ap.parse_args()
    if args.interactive > 0:
        sys.exit(run_interactive(args.interactive))
    sys.exit(check_config())
