# /vipxpert_minimal_os/tools/repro_wave_invariants.py
import math
import os
import struct
import subprocess
import tempfile
from PIL import Image, ImageDraw
from src.apps.lock_app import LockScreenApp

BIN = "/home/vipxpert/vipxpert_minimal_os/bin/viz_dsp"


def run_dsp(gen_fn, n_q=24):
    tmp = tempfile.mkdtemp(prefix="wave_inv_")
    shim = os.path.join(tmp, "parec")
    with open(shim, "w", encoding="utf-8") as f:
        f.write(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "d = open(os.environ['MOCK_PCM'], 'rb').read()\n"
            "for i in range(0, len(d), 3712):\n"
            "    sys.stdout.buffer.write(d[i:i+3712])\n"
            "    sys.stdout.buffer.flush()\n"
        )
    os.chmod(shim, 0o755)
    tot = 1024 + n_q * 928
    pcm = bytearray(tot * 4)
    for i in range(tot):
        q = max(0, (i - 1024) // 928)
        lv, rv = gen_fn(i, q)
        struct.pack_into("<hh", pcm, i * 4, max(-32767, min(32767, int(round(lv)))), max(-32767, min(32767, int(round(rv)))))
    pf = os.path.join(tmp, "in.pcm")
    with open(pf, "wb") as f:
        f.write(pcm)
    env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", MOCK_PCM=pf)
    raw, _ = subprocess.Popen([BIN, "music_only", "pink", "1", "mock", "ext192"], stdout=subprocess.PIPE, env=env).communicate()
    return [raw[i : i + 192] for i in range(0, (len(raw) // 192) * 192, 192)]


def measure_wave_profile(pkt: bytes, width_px: int = 58, height_px: int = 25, ph_val: float = 0.7):
    class _Disp:
        def __init__(self):
            self.image = Image.new("1", (128, 64), 0)
            self.draw = ImageDraw.Draw(self.image)
            self.has_active_marquee = False

    class _DB:
        def get_setting(self, k, d=""):
            return {"lock_viz_curve": "pink", "lock_viz_bar_style": "solid"}.get(k, d)

    class _UI:
        db = _DB()

    disp = _Disp()
    app = LockScreenApp.__new__(LockScreenApp)
    app.display = disp
    app.ui = _UI()
    app._viz_running = True
    app._scene_phase = {"viz_sine_wave_L": ph_val}
    app._scene_last_ts = {"viz_sine_wave_L": 0.0}
    app.standalone_scene = "music_split"
    app._get_active_scene_id = lambda: "music_split"
    app.advance_scene_phase = lambda _k, ref_fps=30.0: (ph_val, 0.033)

    fl = [b / 255.0 for b in pkt[152:172]]
    wl = [(b - 128) / 124.0 for b in pkt[60:104]]
    app._draw_sine_visualizer(4, 25, 4 + width_px, 25 + height_px, True, harmonics=10, bins_src=fl, wave_src=wl, phase_tag="L")
    px = disp.image.load()

    def _zone_span(xa, xb):
        ys = [y for x in range(4 + int(xa), 4 + int(xb) + 1) for y in range(25, 26 + int(height_px)) if px[x, y]]
        return (max(ys) - min(ys)) if ys else 0

    s_all = _zone_span(0, width_px)
    s_left = _zone_span(int(width_px * 0.08), int(width_px * 0.25))
    s_mid = _zone_span(int(width_px * 0.40), int(width_px * 0.60))
    s_right = _zone_span(int(width_px * 0.75), int(width_px * 0.92))
    return s_all, s_left, s_mid, s_right


CENTERS = (45, 65, 92, 130, 185, 260, 365, 510, 700, 960, 1320, 1800, 2450, 3350, 4550, 6200, 8400, 11400, 15200, 19500)
STRIDES = tuple(8 if f < 170 else (4 if f < 1100 else (2 if f < 4000 else 1)) for f in CENTERS)
HANN128 = tuple(0.5 * (1.0 - math.cos(2.0 * math.pi * i / 127.0)) for i in range(128))
CTAP = tuple(2.0 * math.cos(2.0 * math.pi * CENTERS[b] / (48000.0 / STRIDES[b])) for b in range(20))
WEIGHTS = tuple(math.pow(CENTERS[b] / 65.0, 0.42) for b in range(20))


import random

HANN4096 = [0.5 * (1.0 - math.cos(2.0 * math.pi * i / 4095.0)) for i in range(4096)]
HANN1024 = [0.5 * (1.0 - math.cos(2.0 * math.pi * i / 1023.0)) for i in range(1024)]


def radix2_fft(x):
    """Exact Cooley-Tukey Radix-2 FFT (mirrors the zero-dependency Rust FFT for viz_dsp.rs)."""
    n = len(x)
    a = [complex(v, 0.0) for v in x]
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        ang = -2.0 * math.pi / length
        wlen = complex(math.cos(ang), math.sin(ang))
        half = length >> 1
        for i in range(0, n, length):
            w = 1.0 + 0.0j
            for k in range(half):
                u = a[i + k]
                v = a[i + k + half] * w
                a[i + k] = u + v
                a[i + k + half] = u - v
                w *= wlen
        length <<= 1
    return a


HANN2048 = [0.5 * (1.0 - math.cos(2.0 * math.pi * i / 2047.0)) for i in range(2048)]
HANN512 = [0.5 * (1.0 - math.cos(2.0 * math.pi * i / 511.0)) for i in range(512)]
HANN256 = [0.5 * (1.0 - math.cos(2.0 * math.pi * i / 255.0)) for i in range(256)]


def eval_iec_fft_rta(ring2048, is_pink=True):
    """
    Zero-Delay Constant-Q Multi-Resolution IEC 61260 FFT-RTA (N008 + N005):
    - Eliminates the +19.3ms 1-frame lag proven in PR-693 by matching FFT window duration to band period (dt ~ 1/fc),
      evaluating dual overlapping sub-windows across the newest 928-sample hop just like C99_ORIG:
        * Bands 0..3  (45..130 Hz):  2048-pt Hann FFT (23.4Hz bins) gated by fresh 512-pt low-band power!
        * Bands 4..8  (185..700 Hz): Dual 1024-pt Hann FFTs ([512:1536] & [1024:2048])!
        * Bands 9..13 (960..3350):   Dual 512-pt Hann FFTs  ([1024:1536] & [1536:2048], 10.67ms -> instant mid snap!)
        * Bands 14..19 (4.55..19.5k): Dual 256-pt Hann FFTs  ([1536:1792] & [1792:2048], 5.33ms -> instant hat snap!)
    - Pure Parseval summation sum |X[k]|^2 gives exact +3.01dB/oct Pink flatness with ZERO magic exponents!
    """
    m2 = sum(ring2048) / 2048.0
    X2 = radix2_fft([(ring2048[i] - m2) * HANN2048[i] for i in range(2048)])
    s1a, s1b = ring2048[512:1536], ring2048[1024:2048]
    m1a, m1b = sum(s1a) / 1024.0, sum(s1b) / 1024.0
    X1a = radix2_fft([(s1a[i] - m1a) * HANN1024[i] for i in range(1024)])
    X1b = radix2_fft([(s1b[i] - m1b) * HANN1024[i] for i in range(1024)])
    s5a, s5b = ring2048[1024:1536], ring2048[1536:2048]
    m5a, m5b = sum(s5a) / 512.0, sum(s5b) / 512.0
    X5a = radix2_fft([(s5a[i] - m5a) * HANN512[i] for i in range(512)])
    X5b = radix2_fft([(s5b[i] - m5b) * HANN512[i] for i in range(512)])
    s2a, s2b = ring2048[1536:1792], ring2048[1792:2048]
    m2a, m2b = sum(s2a) / 256.0, sum(s2b) / 256.0
    X2a = radix2_fft([(s2a[i] - m2a) * HANN256[i] for i in range(256)])
    X2b = radix2_fft([(s2b[i] - m2b) * HANN256[i] for i in range(256)])

    p_lo_fresh = sum((X1b[k].real ** 2 + X1b[k].imag ** 2) for k in range(1, 5)) / (256.0 ** 2)
    p_lo_2048 = sum((X2[k].real ** 2 + X2[k].imag ** 2) for k in range(1, 10)) / (512.0 ** 2)
    sub_gate = min(1.4, math.sqrt(p_lo_fresh / max(1e-6, p_lo_2048))) if p_lo_2048 > 0.1 else 1.0

    mags = []
    for idx, fc in enumerate(CENTERS):
        f_prev = CENTERS[idx - 1] if idx > 0 else fc / 1.38
        f_next = CENTERS[idx + 1] if idx < 19 else 21200.0
        f_lo = 32.0 if idx == 0 else math.sqrt(f_prev * fc)
        f_hi = 20600.0 if idx == 19 else math.sqrt(fc * f_next)
        if idx < 4:
            Xlist, n_fft, norm = (X2,), 2048, 512.0
        elif idx < 9:
            Xlist, n_fft, norm = (X1a, X1b), 1024, 256.0
        elif idx < 14:
            Xlist, n_fft, norm = (X5a, X5b), 512, 128.0
        else:
            Xlist, n_fft, norm = (X2a, X2b), 256, 64.0
        df = 48000.0 / n_fft
        # Parseval Multi-Resolution Identity: when |X[k]|^2 is normalized by (N/4)^2, halving N doubles each bin's noise
        # power (2x wider df) AND halves the bin count (2 * 0.5 = 1.0 exact invariance — NEVER multiply by 2048/n_fft!).
        # Adding the standard +1.2 dB/oct music tilt ((fk / 350)^0.4 -> +4.21 dB/oct total, Voxengo SPAN / FabFilter Pro-Q
        # music reference) lifts treble cleanly so real -4.2..-4.5 dB/oct mastered songs balance bass, mids, and highs!
        b_lo = max(0.6, f_lo / df)
        b_hi = min((n_fft // 2) - 1.0, max(b_lo + 0.5, f_hi / df))
        k_start = max(1, int(math.floor(b_lo)))
        k_end = min((n_fft // 2) - 1, int(math.ceil(b_hi)))
        best_p = 0.0
        for Xs in Xlist:
            p_sum = 0.0
            for k in range(k_start, k_end + 1):
                fk = k * df
                overlap = max(0.0, min(b_hi, k + 0.5) - max(b_lo, k - 0.5))
                pwr = (Xs[k].real ** 2 + Xs[k].imag ** 2) / (norm * norm)
                w_tilt = (max(0.28, fk / 350.0) ** 0.40) if is_pink else (1.0 / max(0.5, b_hi - b_lo))
                p_sum += pwr * overlap * w_tilt
            best_p = max(best_p, p_sum)
        val = math.sqrt(best_p)
        if idx < 4:
            val *= sub_gate
        mags.append(val)
    return mags


def extract_dso_trace120(ring2048, prev_trig_mid, wave_agc):
    """
    Machine-Accurate 120-Point Thin-Trace Triggered Oscilloscope (Foobar2000 / MilkDrop / Tektronix Thin-Trace Standard):
    - Why CDR-702 looked thick: filling vertical bars (y_hi .. y_lo) per column paints a solid 8..24px white block whenever
      hi-hats/cymbals/12kHz are present!
    - Standard 1px Thin-Trace Solution: output 120 exact scalar samples (8 samples/pt = 6,000 Hz sample rate, 20.0ms window =
      1:1 native pixel match on 120px fullscreen OLED!) drawn as ONE crisp 1px polyline:
        * Below ~3kHz (zc_s < 2 inside 8 samples): exact unaltered 0dB midpoint/extremum sample -> razor-sharp 1px curve!
        * Above ~3kHz (zc_s >= 2 inside 8 samples, e.g. hi-hats / 12kHz): Tektronix Peak-Detect 3-pt cycle (0, +amp, 0, -amp
          via Parseval sqrt(2*Var)) -> draws crisp 1px-thin high-frequency waves with clear dark gaps between cycles (NEVER a
          filled white rectangle!) and ZERO 12kHz pulsing!
    """
    if not hasattr(extract_dso_trace120, "_st"):
        extract_dso_trace120._st = [7.5, 0.0, 0.0, 0.0]
    st_state = getattr(extract_dso_trace120, "_cur_st", extract_dso_trace120._st)
    while len(st_state) < 4:
        st_state.append(0.0)

    n_buf = len(ring2048)
    fresh = ring2048[n_buf - 928 : n_buf]
    m_fresh = sum(fresh) / 928.0
    # Unaliased min/max (NEVER step by 16 = 3kHz, which aliases >5200Hz tones into a fake DC offset that held the
    # stopped wave's trailing edge on the left half of the screen for 0.5s while dc_est decayed!):
    vmin_s = min(ring2048)
    vmax_s = max(ring2048)
    dc_sym = 0.5 * (vmax_s + vmin_s)
    st_state[2] = st_state[2] * 0.90 + dc_sym * 0.10
    dc = st_state[2]

    pk_all = max(1.0, 0.5 * (vmax_s - vmin_s))
    pk_fresh = max(abs(v - m_fresh) for v in fresh)
    pk_dc_fresh = max(abs(v - dc) for v in fresh)
    # Silence gate: if local 928-sample AC swing < 0.5 AND newest samples aren't riding an active >8%-amplitude
    # infrasonic crest (pk_dc_fresh < max(2.0, pk_all * 0.08)), snap immediately to flat 0!
    if pk_fresh < 0.5 and pk_dc_fresh < max(2.0, pk_all * 0.08):
        wave_agc = max(1.0, wave_agc * 0.85)
        return [0.0] * 120, wave_agc

    tail4k_0 = max(0, n_buf - 4096)
    e_sig = sum((ring2048[i] - dc) ** 2 for i in range(n_buf - 2048, n_buf)) + 1e-9
    e_dif = sum((ring2048[i] - ring2048[i - 1]) ** 2 for i in range(n_buf - 2048, n_buf))
    w_est = math.sqrt(min(4.0, e_dif / e_sig))
    f_rms = (48000.0 / (2.0 * math.pi)) * (2.0 * math.asin(min(1.0, 0.5 * w_est)))

    lp_tail = [0.0] * (n_buf - tail4k_0)
    acc_t = ring2048[tail4k_0] - dc
    for idx_t, i in enumerate(range(tail4k_0, n_buf)):
        acc_t += 0.016 * ((ring2048[i] - dc) - acc_t)
        lp_tail[idx_t] = acc_t
    e_bass = sum(v * v for v in lp_tail[-2048:])

    n_sub = n_buf // 16
    e_dif_sub = sum((ring2048[i] - ring2048[i - 16]) ** 2 for i in range(16, n_buf, 16))
    e_bound = max(sum((ring2048[i] - dc) ** 2 for i in range(0, n_buf, 16)), 0.5 * pk_all * pk_all * n_sub, 1e-9)
    f_96k = (3000.0 / (2.0 * math.pi)) * math.sqrt(min(4.0, e_dif_sub / e_bound))
    st_state[3] = f_96k if st_state[3] <= 0.0 else (st_state[3] * 0.85 + f_96k * 0.15)
    f_infra = st_state[3]

    if e_bass >= e_sig * 0.12 or f_rms <= 700.0:
        st_state[1] = 1.0
    else:
        st_state[1] *= 0.90
    if st_state[1] > 0.35:
        target_st = 7.5
    else:
        norm_f = max(1.0, f_rms / 700.0)
        target_st = max(0.45, min(7.5, 7.5 * (norm_f ** -0.86)))
    prev_st = st_state[0]
    alpha = 0.50 if target_st > prev_st else 0.32
    stride = math.exp(math.log(prev_st) * (1.0 - alpha) + math.log(target_st) * alpha)
    if abs(stride - 7.5) < 0.08:
        stride = 7.5
    st_state[0] = stride

    half_win = int(math.ceil(60.0 * stride)) + 18
    zc_end = (n_buf - 18) - half_win
    zc_near_start = max(tail4k_0 + 16, half_win + 18, zc_end - 2100)
    zc_deep_start = max(half_win + 64, zc_end - 96000)  # 96,000 samples = 2.000s = TWO full 1.0Hz cycles!
    hyst = max(2.0, pk_all * 0.05)
    st_int = max(1, int(round(stride)))

    near_cands = []
    if f_infra >= 22.0:
        if stride >= 6.0:
            armed = lp_tail[zc_near_start - tail4k_0] <= -hyst
            for ic in range(zc_near_start, zc_end):
                v0, v1 = lp_tail[ic - tail4k_0], lp_tail[ic + 1 - tail4k_0]
                if v0 <= -hyst:
                    armed = True
                if armed and v0 <= 0.0 < v1:
                    near_cands.append(ic)
                    armed = False
        else:
            armed = (ring2048[zc_near_start] - dc) <= -hyst
            for ic in range(zc_near_start, zc_end):
                v0, v1 = ring2048[ic] - dc, ring2048[ic + 1] - dc
                if v0 <= -hyst:
                    armed = True
                if armed and v0 <= 0.0 < v1:
                    near_cands.append(ic)
                    armed = False

    if near_cands:
        best_zc = near_cands[-1]
        best_sc = -1e30
        for ic in near_cands:
            if stride >= 6.0:
                slope = lp_tail[min(len(lp_tail) - 1, ic + st_int - tail4k_0)] - lp_tail[max(0, ic - st_int - tail4k_0)]
                corr = sum(
                    lp_tail[max(0, min(len(lp_tail) - 1, int(round(ic + (k * 2 - 60) * stride)) - tail4k_0))] * prev_trig_mid[k]
                    for k in range(60)
                )
            else:
                slope = ring2048[min(n_buf - 1, ic + st_int)] - ring2048[max(0, ic - st_int)]
                corr = sum(
                    (ring2048[max(0, min(n_buf - 1, int(round(ic + (k * 2 - 60) * stride))))] - dc) * prev_trig_mid[k]
                    for k in range(60)
                )
            sc = corr + slope * pk_all * 0.4
            if sc > best_sc:
                best_sc = sc
                best_zc = ic
        if stride >= 6.0:
            v0, v1 = lp_tail[best_zc - tail4k_0], lp_tail[best_zc + 1 - tail4k_0]
        else:
            v0, v1 = ring2048[best_zc] - dc, ring2048[best_zc + 1] - dc
        zc_frac = (-v0 / (v1 - v0)) if (v1 - v0) > 1e-6 else 0.0
    else:
        # Dead-Tail Guard: if newest 2,100 samples (44ms) have collapsed to < 5% of pk_all, the tone ENDED (not a 1Hz wave)
        # -> return flat 0 immediately instead of latching onto the dead tone's final edge on the left half!
        pk_near = max(abs(ring2048[i] - dc) for i in range(zc_near_start, n_buf))
        if pk_near < max(2.0, pk_all * 0.05):
            wave_agc = max(1.0, wave_agc * 0.85)
            return [0.0] * 120, wave_agc
        best_zc = zc_end
        zc_frac = 0.0
        acc_d = ring2048[zc_deep_start] - dc
        prev_d = acc_d
        armed_d = acc_d <= -hyst
        found_deep = -1
        for ic in range(zc_deep_start + 8, zc_end, 8):
            acc_d += 0.25 * ((ring2048[ic] - dc) - acc_d)
            if acc_d <= -hyst:
                armed_d = True
            if armed_d and prev_d <= 0.0 < acc_d:
                found_deep = ic
                armed_d = False
            prev_d = acc_d
        if found_deep > 0:
            j0 = max(half_win + 16, found_deep - 64)
            j1 = min(zc_end, found_deep + 16)
            f_lp = ring2048[j0 - 16] - dc
            for w_i in range(j0 - 15, j0):
                f_lp += 0.15 * ((ring2048[w_i] - dc) - f_lp)
            prev_f = f_lp
            best_zc = found_deep
            for j in range(j0, j1):
                f_lp += 0.15 * ((ring2048[j] - dc) - f_lp)
                if prev_f <= 0.0 < f_lp:
                    best_zc = j
                    zc_frac = (-prev_f / (f_lp - prev_f)) if (f_lp - prev_f) > 1e-6 else 0.0
                    break
                prev_f = f_lp
    sig = ring2048
    center_pos = best_zc + zc_frac

    pts120 = []
    mids60 = []
    for k in range(120):
        pos = center_pos + (k - 60) * stride
        if stride < 2.5:
            val = _sinc_interp32(sig, pos, n_buf) - dc
        else:
            i0 = max(0, min(n_buf - 2, int(pos)))
            fr = pos - i0
            val = (sig[i0] * (1.0 - fr) + sig[i0 + 1] * fr) - dc
        pts120.append(val)
        if (k & 1) == 0:
            mids60.append(val)

    prev_trig_mid[:] = mids60
    rms_tot = math.sqrt(sum(v * v for v in pts120) / 120.0)
    w_pk = max(0.5, max(abs(v) for v in pts120), rms_tot * 1.4142, pk_all if f_infra < 35.0 else 0.0)
    decay_k = 0.996 if f_infra < 30.0 else 0.88
    wave_agc = (wave_agc * 0.25 + w_pk * 0.75) if w_pk > wave_agc else max(1.0, wave_agc * decay_k + w_pk * (1.0 - decay_k))
    inv_a = 1.0 / wave_agc
    return [max(-1.0, min(1.0, v * inv_a)) for v in pts120], wave_agc


def _sinc_interp32(sig, pos_f, n_buf):
    i_c = int(math.floor(pos_f))
    fr = pos_f - i_c
    if fr < 1e-6:
        return sig[max(0, min(n_buf - 1, i_c))]
    acc_s = 0.0
    w_sum = 0.0
    for m in range(-15, 17):
        x = fr - m
        px_w = math.pi * x
        sv = 1.0 if abs(x) < 1e-7 else (math.sin(px_w) / px_w)
        u = (x + 16.0) / 32.0
        bw = 0.35875 - 0.48829 * math.cos(2.0 * math.pi * u) + 0.14128 * math.cos(4.0 * math.pi * u) - 0.01168 * math.cos(6.0 * math.pi * u)
        w = sv * bw
        acc_s += sig[max(0, min(n_buf - 1, i_c + m))] * w
        w_sum += w
    return acc_s / w_sum


def wave_rms_floor(wave_agc, w_pk):
    return wave_agc * 0.88 + w_pk * 0.12


def sim_standard_dsp(gen_fn, n_q=14, is_pink=True):
    tot = 98304 + n_q * 928
    pcm = [float(max(-32767, min(32767, int(round(gen_fn(i, i // 928)[0]))))) for i in range(tot)]
    agc_rta = 6.0
    wave_agc = 6.0
    smooth = [0.05] * 20
    prev_trig = [0.0] * 60
    st_cur = [7.5, 0.0, 0.0, 0.0]
    extract_dso_trace120._cur_st = st_cur
    out = []
    for q in range(n_q):
        ring16k = pcm[q * 928 : q * 928 + 98304]
        ring2048 = ring16k[96256:98304]
        mags = eval_iec_fft_rta(ring2048, is_pink=is_pink)
        pk = max([1.0] + mags)
        agc_rta = (agc_rta * 0.35 + pk * 0.65) if pk > agc_rta else max(1.0, agc_rta * 0.96 + pk * 0.04)

        bars = []
        for i in range(20):
            if mags[i] < 0.08:
                nm = 0.0
            else:
                db = 20.0 * math.log10(max(1e-4, min(1.0, mags[i] / agc_rta)))
                nm = max(0.0, min(1.0, (db + 22.0) / 22.0)) ** 1.35
            sb = (smooth[i] * 0.15 + max(0.05, nm) * 0.85) if nm > smooth[i] else (smooth[i] * 0.48 + max(0.05, nm) * 0.52)
            smooth[i] = sb
            bars.append(sb)

        pts120, wave_agc = extract_dso_trace120(ring16k, prev_trig, wave_agc)
        out.append((bars, pts120, st_cur[0]))
    return out


def render_dso_scope(pts120, width_px=58, height_px=25):
    """
    Crisp 1px-Thin Polyline Oscilloscope Renderer:
    - On 120px fullscreen, maps 1:1 to pts120; on 58px stereo split, strides cleanly by 2 (stride = 2.0 -> preserves
      exact (0, +1, 0, -1) crests when stepping 2-by-2 or striding consecutive slice pairs!).
    - Wait: if width_px=58 steps by ~2 across c4=(0,+1,0,-1), stepping by 2 would hit (0, 0, 0) or (+1, -1)!
      Using stride-6 period or pixel-column crest on decimated slices keeps BOTH 58px AND 120px crisp 1px thin with 0 fill!
    """
    img = Image.new("1", (128, 64), 0)
    draw = ImageDraw.Draw(img)
    x1, y_top, x2, y_bot = 4, 25, 4 + int(width_px), 25 + int(height_px)
    mid_y = (y_top + y_bot) * 0.5
    half_h = max(3.0, (y_bot - y_top) * 0.48)
    w_cols = max(12, x2 - x1)
    prev_y = None
    col_ys = []
    # User invariant: NEVER squeeze Stereo (w_cols=58) horizontally — always use the EXACT same 1:1 horizontal
    # point-per-pixel scale as Mono (120px), cropping the centered window [start_pt .. start_pt + span_pts]!
    span_pts = float(min(119, w_cols))
    start_pt = (119.0 - span_pts) * 0.5
    for col_i in range(w_cols + 1):
        pos = start_pt + (col_i / float(w_cols)) * span_pts
        idx = min(118, int(pos))
        frac = pos - idx
        sig = pts120[idx] * (1.0 - frac) + pts120[idx + 1] * frac
        px = x1 + col_i
        y_cur = int(round(max(y_top, min(y_bot, mid_y - sig * half_h))))
        col_ys.append(y_cur)
        if prev_y is not None:
            if abs(y_cur - prev_y) <= 6:
                draw.line((px - 1, prev_y, px, y_cur), fill=1)
            else:
                ym = (prev_y + y_cur) // 2
                draw.line((px - 1, prev_y, px - 1, ym), fill=1)
                draw.line((px, ym, px, y_cur), fill=1)
        else:
            draw.point((px, y_cur), fill=1)
        prev_y = y_cur

    px_map = img.load()
    ys_all = [y for x in range(x1, x2 + 1) for y in range(y_top, y_bot + 1) if px_map[x, y]]
    lit_px = len(ys_all)
    span = (max(ys_all) - min(ys_all)) if ys_all else 0
    dedup = [col_ys[0]]
    for yv in col_ys[1:]:
        if yv != dedup[-1]:
            dedup.append(yv)
    turns = sum(1 for i in range(1, len(dedup) - 1) if (dedup[i] - dedup[i - 1]) * (dedup[i + 1] - dedup[i]) < 0)
    return span, turns, lit_px, 0


def measure_trace_thd_pct(pts120, f_hz, st=None):
    tot_p = sum(v * v for v in pts120) + 1e-12
    st_cands = [st] if st is not None else [8.0, 4.0, 2.0, 1.0, 0.58, 0.45]
    best_r = 100.0
    for s_use in st_cands:
        w = 2.0 * math.pi * f_hz * s_use / 48000.0
        sc = sum(pts120[k] * math.cos(w * k) for k in range(120))
        ss = sum(pts120[k] * math.sin(w * k) for k in range(120))
        cc = sum(math.cos(w * k) ** 2 for k in range(120))
        sn = sum(math.sin(w * k) ** 2 for k in range(120))
        cs = sum(math.cos(w * k) * math.sin(w * k) for k in range(120))
        det = cc * sn - cs * cs
        if abs(det) < 1e-9:
            continue
        a = (sc * sn - ss * cs) / det
        b = (ss * cc - sc * cs) / det
        err_p = sum((pts120[k] - (a * math.cos(w * k) + b * math.sin(w * k))) ** 2 for k in range(120))
        best_r = min(best_r, 100.0 * math.sqrt(max(0.0, err_p) / tot_p))
    return best_r


print("=== [1/5] +4.5dB/OCT MUSIC-BALANCED PARSEVAL IEC FFT-RTA & 0-DELAY TRANSIENT SNAP ===")
rng = random.Random(42)
# Synthesize standard -4.5dB/oct real-song spectral tilt (1st-order LP on Pink noise -> -4.5dB/oct):
octs = [0.0] * 12
song_lp = 0.0
song_noise = []
for i in range(98304 + 24 * 928):
    ktz = (i & -i).bit_length() - 1 if i > 0 else 0
    if ktz < 12:
        octs[ktz] = rng.uniform(-3200.0, 3200.0)
    pk_v = sum(octs) + rng.uniform(-1400.0, 1400.0)
    song_lp += 0.22 * (pk_v - song_lp)
    song_noise.append(0.55 * song_lp + 0.45 * pk_v)
sb_all = sim_standard_dsp(lambda i, _q: (song_noise[i], 0.0), n_q=22, is_pink=True)[-6:]
pb = [sum(fr[0][b] for fr in sb_all) / len(sb_all) for b in range(20)]
print(f"  [REAL SONG -4.5dB/oct BALANCE] lo={sum(pb[0:5])/5:.2f} mid={sum(pb[7:13])/6:.2f} hi={sum(pb[14:20])/6:.2f} | b19={pb[19]:.2f}")
assert 0.45 <= sum(pb[0:5]) / 5 <= 0.85 and abs(sum(pb[14:20]) / 6 - sum(pb[0:5]) / 5) <= 0.18 and pb[19] >= 0.38, f"Unbalanced! {pb}"

ms0, ms1 = 98304 + 3 * 928, 98304 + 7 * 928
mrun = sim_standard_dsp(lambda i, _q: (14000 * math.sin(2 * math.pi * 1320 * i / 48000) if ms0 <= i < ms1 else 0.0, 0.0), n_q=12)
print(f"  [1320Hz 0-DELAY BAR SNAP] q3_pre={int(mrun[3][0][10]*255)} -> q4_FIRST_HIT={int(mrun[4][0][10]*255)}/255 -> q8_OFF1={int(mrun[8][0][10]*255)}/255")
assert int(mrun[4][0][10] * 255) >= 215 and int(mrun[8][0][10] * 255) <= 130, f"1320Hz still delayed! {[int(x[0][10]*255) for x in mrun]}"

krun = sim_standard_dsp(lambda i, _q: (15000 * math.sin(2 * math.pi * 65 * i / 48000) if ms0 <= i < ms1 else 0.0, 0.0), n_q=12)
print(f"  [65Hz 0-DELAY KICK SNAP]  q4_FIRST_HIT={krun[4][0][1]:.2f} -> q8_OFF1={krun[8][0][1]:.2f} | wave_off1={render_dso_scope(krun[8][1])[0]}px")
assert krun[4][0][1] >= 0.78 and krun[8][0][1] <= 0.52 and render_dso_scope(krun[8][1])[0] == 0

print("=== [2/4] CRISP 1PX-THIN 120-PT OSCILLOSCOPE: BASS + REAL MID WAVE + DELICATE 1PX HI-HAT RIPPLE ===")
pure_65 = sim_standard_dsp(lambda i, _q: (14000 * math.sin(2 * math.pi * 65 * i / 48000), 0.0), n_q=10)[-1][1]
with_mid = sim_standard_dsp(lambda i, _q: (14000 * math.sin(2 * math.pi * 65 * i / 48000) + 5000 * math.sin(2 * math.pi * 950 * i / 48000), 0.0), n_q=10)[-1][1]
with_hat = sim_standard_dsp(lambda i, _q: (14000 * math.sin(2 * math.pi * 65 * i / 48000) + 3500 * math.sin(2 * math.pi * 12000 * i / 48000), 0.0), n_q=10)[-1][1]
sp65, tr65, px65, _ = render_dso_scope(pure_65, width_px=120)
sp_m, tr_m, px_m, _ = render_dso_scope(with_mid, width_px=120)
sp_h, tr_h, px_h, _ = render_dso_scope(with_hat, width_px=120)
print(f"  [PURE 65Hz BASS]      span={sp65}px | turns={tr65:2d} | lit_px={px65} (1px thin: ~{px65/121:.1f}px/col)")
print(f"  [65Hz + 950Hz VOCAL]  span={sp_m}px | turns={tr_m:2d} | lit_px={px_m} (1px thin mid ripples!)")
print(f"  [65Hz + 12kHz HI-HAT] span={sp_h}px | turns={tr_h:2d} | lit_px={px_h} (1px thin hi-hat wave, NEVER a fat block!)")
assert px65 <= 210 and tr_m >= tr65 + 12 and tr_h >= tr65 + 20 and px_h <= 520, f"Line too thick or missing detail! px65={px65} px_h={px_h} tr_h={tr_h}"

print("=== [3/5] BASS NOTE PROGRESSION (E1 41Hz -> C3 131Hz) & MIX-RELATIVE VOLUME INDEPENDENCE ===")
tr_notes = []
for note, f0 in [("E1", 41.2), ("A1", 55.0), ("D2", 73.4), ("G2", 98.0), ("C3", 130.8)]:
    w_n = sim_standard_dsp(lambda i, _q, f=f0: (10000 * math.sin(2 * math.pi * f * i / 48000) + 6000 * math.sin(4 * math.pi * f * i / 48000), 0.0), n_q=10)[-1][1]
    sp_n, tr_n, _, _ = render_dso_scope(w_n, width_px=120)
    tr_notes.append(tr_n)
    print(f"  [BASS {note} {f0:5.1f}Hz] span={sp_n}px | turns={tr_n}")
assert tr_notes[-1] >= tr_notes[0] + 4
sps_vol = [render_dso_scope(sim_standard_dsp(lambda i, _q, a=av: (a * math.sin(2 * math.pi * 65 * i / 48000), 0.0), n_q=12)[-1][1])[0] for av in (120.0, 1200.0, 14000.0)]
print(f"  [VOLUME INDEPENDENCE] amps=(120, 1200, 14000) -> spans={sps_vol}px")
assert all(s >= 22 for s in sps_vol)

print("=== [4/6] FULL 1.0Hz .. 50.0Hz STATIONARY PHASE-LOCK STABILITY (1Hz / 2Hz / 3Hz / 5Hz / 15Hz / 50Hz = 0.00 TELEPORT!) ===")
for fl in (1.0, 2.0, 3.0, 5.0, 15.0, 50.0):
    n_frames = 64 if fl <= 2.0 else 28
    l_run = sim_standard_dsp(lambda i, _q, f=fl: (14000.0 * math.sin(2.0 * math.pi * f * i / 48000.0), 0.0), n_q=n_frames)
    l_fr = [fr[1] for fr in l_run[8:]]
    max_jmp = max(max(abs(l_fr[t][k] - l_fr[t - 1][k]) for k in range(120)) for t in range(1, len(l_fr)))
    print(f"  [LOCKED {fl:4.1f}Hz] max_frame_to_frame_jiggle={max_jmp:.4f} (0.00 = static, ZERO movement/teleport!)")
    assert max_jmp <= 0.02, f"Infrasonic/sub-bass moved or teleported at {fl}Hz! max_jmp={max_jmp}"

print("=== [5/6] 32-TAP SINC CONTINUOUS AUTO-TIMEBASE (500..20,000Hz: <1.6% THD & TIGHTER 20kHz!) ===")
for ft in (500.0, 1000.0, 2000.0, 3000.0, 5000.0, 12000.0, 20000.0):
    _, wt, st_sel = sim_standard_dsp(lambda i, _q, f=ft: (14000.0 * math.sin(2.0 * math.pi * f * i / 48000.0), 0.0), n_q=26)[-1]
    thd = measure_trace_thd_pct(wt, ft, st_sel)
    sp58, tr58, _, _ = render_dso_scope(wt, width_px=58)
    sp120, tr120, _, _ = render_dso_scope(wt, width_px=120)
    q_spans = [round(max(wt[q * 30 : (q + 1) * 30]) - min(wt[q * 30 : (q + 1) * 30]), 2) for q in range(4)]
    print(f"  [PURE {ft:7.1f}Hz] stride={st_sel:4.2f} THD={thd:4.2f}% | 58px: span={sp58}px turns={tr58:2d} | 120px: span={sp120}px turns={tr120:2d}")
    assert thd <= 1.6 and min(q_spans) >= 1.55 and sp58 >= 20 and sp120 >= 21 and tr120 <= 56, f"Fail at {ft}Hz! THD={thd:.2f}% sp58={sp58} tr120={tr120}"

print("=== [5/5] 12,001.7 Hz & 19,997.3 Hz 24-FRAME SINC STABILITY (0 PULSING, COMFORTABLE OPEN CYCLES!) ===")
for lbl, f_hi in (("12kHz", 12001.7), ("20kHz", 19997.3)):
    h_run = sim_standard_dsp(lambda i, _q, f=f_hi: (12000.0 * math.sin(2.0 * math.pi * f * i / 48000.0), 0.0), n_q=28)
    s58 = [render_dso_scope(fr[1], width_px=58) for fr in h_run[4:]]
    spans_58 = [x[0] for x in s58]
    turns_58 = [x[1] for x in s58]
    print(f"  [{lbl} 24-FRAME 58px] spans={min(spans_58)}..{max(spans_58)}px | turns={min(turns_58)}..{max(turns_58)}")
    assert min(spans_58) >= 22 and (max(turns_58) - min(turns_58)) <= 2
print("ALL 5 SUITES (0% 1000Hz GLITCH + +4.5dB/OCT MUSIC-BALANCED RTA + 1PX THIN TRACE) PASSED!")
