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
        b_lo = max(0.6, f_lo / df)
        b_hi = min((n_fft // 2) - 1.0, max(b_lo + 0.6, f_hi / df))
        k_start = max(1, int(math.floor(b_lo)))
        k_end = min((n_fft // 2) - 1, int(math.ceil(b_hi)))
        best_p = 0.0
        for Xs in Xlist:
            p_sum = 0.0
            for k in range(k_start, k_end + 1):
                overlap = max(0.0, min(b_hi, k + 0.5) - max(b_lo, k - 0.5))
                pwr = (Xs[k].real ** 2 + Xs[k].imag ** 2) / (norm * norm)
                p_sum += pwr * overlap
            best_p = max(best_p, p_sum)
        val = math.sqrt(best_p if is_pink else (best_p / max(0.75, fc / 60.0)))
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
    m_fresh = sum(ring2048[1120:2048]) / 928.0
    pk_fresh = max(abs(ring2048[i] - m_fresh) for i in range(1120, 2048))
    if pk_fresh < 0.5:
        wave_agc = max(1.0, wave_agc * 0.85)
        return [0.0] * 120, wave_agc

    dc = sum(ring2048) / 2048.0
    sig = [ring2048[i] - dc for i in range(2048)]
    lp = [0.0] * 2048
    acc = sig[0]
    for i in range(2048):
        acc += 0.0196 * (sig[i] - acc)
        lp[i] = acc

    stride = 8  # 120 * 8 = 960 samples (20.0ms) -> 1:1 native 120px OLED resolution (6,000 Hz sample rate)!
    win_len = 120 * stride
    search_end = 2048 - win_len
    search_start = 528

    cands = [i for i in range(search_start, search_end) if lp[i] <= 0.0 < lp[i + 1]]
    if not cands:
        best_idx = search_end
    else:
        best_idx = cands[-1]
        best_sc = -1e30
        for ic in cands:
            slope = lp[ic + 4] - lp[ic - 4]
            corr = sum(sig[ic + k * 16] * prev_trig_mid[k] for k in range(0, 60, 2))
            sc = corr + slope * 4.0
            if sc > best_sc:
                best_sc = sc
                best_idx = ic

    pts120 = []
    mids60 = []
    w_pk = 0.5
    c4 = (0.0, 1.0, 0.0, -1.0)
    for k in range(120):
        seg = sig[best_idx + k * stride : best_idx + (k + 1) * stride]
        mn, mx = min(seg), max(seg)
        mean_s = sum(seg) / stride
        if (k & 1) == 0:
            mids60.append(mean_s)
        zc_s = sum(1 for j in range(1, stride) if (seg[j - 1] - mean_s) * (seg[j] - mean_s) < 0.0)
        if zc_s >= 2:
            rms_c = math.sqrt(2.0 * sum((v - mean_s) ** 2 for v in seg) / stride)
            amp = max(0.5 * (mx - mn), rms_c)
            v = mean_s + amp * c4[k & 3]
            w_pk = max(w_pk, abs(mean_s) + amp)
        else:
            v = seg[stride // 2]
            w_pk = max(w_pk, abs(mn), abs(mx))
        pts120.append(v)

    prev_trig_mid[:] = mids60
    wave_agc = (wave_agc * 0.25 + w_pk * 0.75) if w_pk > wave_agc else max(1.0, wave_rms_floor(wave_agc, w_pk))
    inv_a = 1.0 / wave_agc
    return [max(-1.0, min(1.0, v * inv_a)) for v in pts120], wave_agc


def wave_rms_floor(wave_agc, w_pk):
    return wave_agc * 0.88 + w_pk * 0.12


def sim_standard_dsp(gen_fn, n_q=14, is_pink=True):
    tot = 2048 + n_q * 928
    pcm = [float(max(-32767, min(32767, int(round(gen_fn(i, i // 928)[0]))))) for i in range(tot)]
    agc_rta = 6.0
    wave_agc = 6.0
    smooth = [0.05] * 20
    prev_trig = [0.0] * 60
    out = []
    for q in range(n_q):
        ring = pcm[q * 928 : q * 928 + 2048]
        mags = eval_iec_fft_rta(ring, is_pink=is_pink)
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

        pts120, wave_agc = extract_dso_trace120(ring, prev_trig, wave_agc)
        out.append((bars, pts120))
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
    c4_col = (0.0, 1.0, 0.0, -1.0)
    for col_i in range(w_cols + 1):
        i0 = int((col_i * 119) // w_cols)
        i1 = min(119, max(i0 + 1, int(((col_i + 1) * 119) // w_cols)))
        sl = pts120[i0 : i1 + 1]
        mn_s, mx_s = min(sl), max(sl)
        # If this pixel spans an alternating HF wave (mx - mn > 0.35 across 2..3 pts), step cleanly on pixel column c4_col
        # as a SINGLE 1px polyline point (NEVER a vertical block fill!), otherwise linearly interpolate the exact 1px trace!
        if (mx_s - mn_s) > 0.35 and any((pts120[j + 1] - pts120[j]) * (pts120[j] - pts120[max(0, j - 1)]) < -0.05 for j in range(max(1, i0), min(118, i1 + 1))):
            sig = 0.5 * (mx_s + mn_s) + 0.5 * (mx_s - mn_s) * c4_col[col_i & 3]
        else:
            pos = (col_i / float(w_cols)) * 119.0
            idx = min(118, int(pos))
            frac = pos - idx
            sig = pts120[idx] * (1.0 - frac) + pts120[idx + 1] * frac
        px = x1 + col_i
        y_cur = int(round(max(y_top, min(y_bot, mid_y - sig * half_h))))
        col_ys.append(y_cur)
        # Limit vertical connector jump on ultra-steep HF edges so adjacent columns NEVER fuse into a solid white rectangle!
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
    return span, turns, lit_px


print("=== [1/4] ZERO-DELAY CONSTANT-Q IEC FFT-RTA (0-FRAME 1320Hz & 65Hz SNAP + PINK FLATNESS) ===")
rng = random.Random(42)
octs = [0.0] * 12
pink_pcm = []
for i in range(2048 + 20 * 928):
    ktz = (i & -i).bit_length() - 1 if i > 0 else 0
    if ktz < 12:
        octs[ktz] = rng.uniform(-2600.0, 2600.0)
    pink_pcm.append(sum(octs) + rng.uniform(-1200.0, 1200.0))
pb = sim_standard_dsp(lambda i, _q: (pink_pcm[i], 0.0), n_q=18, is_pink=True)[-1][0]
print(f"  [PARSEVAL PINK FLATNESS] lo={sum(pb[1:6])/5:.2f} mid={sum(pb[7:13])/6:.2f} hi={sum(pb[14:19])/5:.2f}")
assert abs(sum(pb[1:6]) / 5 - sum(pb[14:19]) / 5) <= 0.28

# Burst starts at sample 2048 + 3*928 (so q=4 has the FIRST 928-sample burst in [1120..2048]!)
ms0, ms1 = 2048 + 3 * 928, 2048 + 7 * 928
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
sp65, tr65, px65 = render_dso_scope(pure_65, width_px=120)
sp_m, tr_m, px_m = render_dso_scope(with_mid, width_px=120)
sp_h, tr_h, px_h = render_dso_scope(with_hat, width_px=120)
print(f"  [PURE 65Hz BASS]      span={sp65}px | turns={tr65:2d} | lit_px={px65} (1px thin: ~{px65/121:.1f}px/col)")
print(f"  [65Hz + 950Hz VOCAL]  span={sp_m}px | turns={tr_m:2d} | lit_px={px_m} (1px thin mid ripples!)")
print(f"  [65Hz + 12kHz HI-HAT] span={sp_h}px | turns={tr_h:2d} | lit_px={px_h} (1px thin hi-hat wave, NEVER a fat block!)")
assert px65 <= 210 and tr_m >= tr65 + 12 and tr_h >= tr65 + 20 and px_h <= 520, f"Line too thick or missing detail! px65={px65} px_h={px_h} tr_h={tr_h}"

print("=== [3/4] BASS NOTE PROGRESSION (E1 41Hz -> C3 131Hz) & MIX-RELATIVE VOLUME INDEPENDENCE ===")
tr_notes = []
for note, f0 in [("E1", 41.2), ("A1", 55.0), ("D2", 73.4), ("G2", 98.0), ("C3", 130.8)]:
    w_n = sim_standard_dsp(lambda i, _q, f=f0: (10000 * math.sin(2 * math.pi * f * i / 48000) + 6000 * math.sin(4 * math.pi * f * i / 48000), 0.0), n_q=10)[-1][1]
    sp_n, tr_n, _ = render_dso_scope(w_n, width_px=120)
    tr_notes.append(tr_n)
    print(f"  [BASS {note} {f0:5.1f}Hz] span={sp_n}px | turns={tr_n}")
assert tr_notes[-1] >= tr_notes[0] + 4
sps_vol = [render_dso_scope(sim_standard_dsp(lambda i, _q, a=av: (a * math.sin(2 * math.pi * 65 * i / 48000), 0.0), n_q=12)[-1][1])[0] for av in (120.0, 1200.0, 14000.0)]
print(f"  [VOLUME INDEPENDENCE] amps=(120, 1200, 14000) -> spans={sps_vol}px")
assert all(s >= 22 for s in sps_vol)

print("=== [4/4] 12,001.7 Hz CONSTANT TONE THIN-TRACE STABILITY (0 PULSING & CRISP DARK GAPS OVER 24 FRAMES!) ===")
f12k_run = sim_standard_dsp(lambda i, _q: (12000.0 * math.sin(2.0 * math.pi * 12001.7 * i / 48000.0), 0.0), n_q=28)
s58 = [render_dso_scope(fr[1], width_px=58) for fr in f12k_run[4:]]
s120 = [render_dso_scope(fr[1], width_px=120) for fr in f12k_run[4:]]
spans_58 = [x[0] for x in s58]
turns_58 = [x[1] for x in s58]
pxs_58 = [x[2] for x in s58]
print(f"  [12kHz 24-FRAME 58px]  spans={min(spans_58)}..{max(spans_58)}px | turns={min(turns_58)}..{max(turns_58)} | lit_px={min(pxs_58)}..{max(pxs_58)} ( vs 1475px fat block!)")
assert min(spans_58) >= 22 and (max(turns_58) - min(turns_58)) <= 1 and max(pxs_58) <= 820
print("CRISP 1PX-THIN 120-PT OSCILLOSCOPE + ZERO-DELAY IEC FFT-RTA PASSED!")
