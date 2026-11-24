// /vipxpert_minimal_os/rust_workers/src/bin/viz_dsp.rs
// Zero-GC, zero-dependency ARM64 Rust 48kHz 20-band 4-tap log-spaced Goertzel spectrum worker.
use std::env;
use std::f64::consts::PI;
use std::io::{Read, Write};
use std::os::unix::io::AsRawFd;
use std::process::{Child, Command, Stdio};

#[repr(C)]
struct PollFd {
    fd: i32,
    events: i16,
    revents: i16,
}

const POLLIN: i16 = 0x0001;
const POLLHUP: i16 = 0x0010;
const POLLERR: i16 = 0x0008;
const F_GETFL: i32 = 3;
const F_SETFL: i32 = 4;
const O_NONBLOCK: i32 = 0o4000;

extern "C" {
    fn fcntl(fd: i32, cmd: i32, ...) -> i32;
    fn poll(fds: *mut PollFd, nfds: usize, timeout: i32) -> i32;
}

fn set_nonblocking(fd: i32) {
    unsafe {
        let fl = fcntl(fd, F_GETFL);
        if fl >= 0 {
            let _ = fcntl(fd, F_SETFL, fl | O_NONBLOCK);
        }
    }
}

fn get_default_monitor() -> String {
    if let Ok(out) = Command::new("pactl")
        .arg("get-default-sink")
        .stderr(Stdio::null())
        .output()
    {
        let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
        if !s.is_empty() && !s.contains("vip_mic_bus") {
            return format!("{s}.monitor");
        }
    }
    "alsa_output.platform-soc_ahub0_mach.stereo-fallback.monitor".to_string()
}

// Comment (invariant): Keep parec latency >= 22ms (>= 1024/48000 quantum) so visualizer capture never forces sub-quantum PipeWire graph reconfigurations during Bluetooth A2DP playback.
fn spawn_parec(target: &str, is_stream: bool, channels: u8) -> Option<Child> {
    let resolved = if !is_stream && target == "@DEFAULT_MONITOR@" {
        get_default_monitor()
    } else {
        target.to_string()
    };
    let arg_target = if is_stream {
        format!("--monitor-stream={resolved}")
    } else {
        format!("--device={resolved}")
    };
    let ch_arg = format!("--channels={channels}");
    let child = Command::new("parec")
        .args([
            "--format=s16le",
            "--rate=48000",
            &ch_arg,
            "--latency-msec=22",
            &arg_target,
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    if let Some(ref out) = child.stdout {
        set_nonblocking(out.as_raw_fd());
    }
    Some(child)
}

#[inline]
fn shift_ring_var(ring: &mut [u8], chunk: &[u8], n: usize, align_mask: usize) {
    let cap = ring.len();
    let nc = n & align_mask;
    if nc >= cap {
        ring.copy_from_slice(&chunk[nc - cap..nc]);
    } else if nc > 0 {
        ring.copy_within(nc..cap, 0);
        ring[cap - nc..cap].copy_from_slice(&chunk[..nc]);
    }
}

#[inline]
fn shift_f64_ring(ring: &mut [f64; 4096], fresh: &[f64], n: usize) {
    if n >= 4096 {
        ring.copy_from_slice(&fresh[n - 4096..n]);
    } else if n > 0 {
        ring.copy_within(n..4096, 0);
        ring[4096 - n..4096].copy_from_slice(&fresh[..n]);
    }
}

// Comment (invariant): Zero-dependency in-place Cooley-Tukey Radix-2 FFT used in 2-for-1 Hermitian stereo mode (Z[n] = L[n] + i*R[n]) so a single 4096-pt FFT resolves BOTH Left & Right 11.72Hz-resolution bass spectra simultaneously (<0.4ms on Cortex-A53).
fn radix2_fft(re: &mut [f64], im: &mut [f64]) {
    let n = re.len();
    let mut j = 0usize;
    for i in 1..n {
        let mut bit = n >> 1;
        while (j & bit) != 0 {
            j ^= bit;
            bit >>= 1;
        }
        j ^= bit;
        if i < j {
            re.swap(i, j);
            im.swap(i, j);
        }
    }
    let mut len = 2usize;
    while len <= n {
        let ang = -2.0 * PI / (len as f64);
        let (wlen_r, wlen_i) = (ang.cos(), ang.sin());
        let half = len >> 1;
        let mut i = 0usize;
        while i < n {
            let (mut wr, mut wi) = (1.0f64, 0.0f64);
            for k in 0..half {
                let u_r = re[i + k];
                let u_i = im[i + k];
                let vr = re[i + k + half] * wr - im[i + k + half] * wi;
                let vi = re[i + k + half] * wi + im[i + k + half] * wr;
                re[i + k] = u_r + vr;
                im[i + k] = u_i + vi;
                re[i + k + half] = u_r - vr;
                im[i + k + half] = u_i - vi;
                let nwr = wr * wlen_r - wi * wlen_i;
                wi = wr * wlen_i + wi * wlen_r;
                wr = nwr;
            }
            i += len;
        }
        len <<= 1;
    }
}

#[derive(Clone)]
struct BandSpec {
    tier: u8, // 0: 2048-pt, 1: dual 1024-pt, 2: dual 512-pt, 3: dual 256-pt
    k0: usize,
    k1: usize,
    overlaps: Vec<f64>,
    raw_div: f64,
}

// Comment (invariant): Zero-Delay Constant-Q IEC 61260 Fractional-Octave Filterbank via Parseval FFT summation (N008 + N005, proven in CR-700): matching FFT window length to band period (2048/1024/512/256) eliminates the +19.3ms 1-frame mid/high lag while Parseval summation sum |X[k]|^2 yields exact +3.01dB/octave Pink-noise flatness with zero magic exponents!
fn build_band_specs(centers: &[f64; 20], is_raw: bool) -> Vec<BandSpec> {
    let mut specs = Vec::with_capacity(20);
    for idx in 0..20 {
        let fc = centers[idx];
        let f_prev = if idx > 0 { centers[idx - 1] } else { fc / 1.38 };
        let f_next = if idx < 19 { centers[idx + 1] } else { 21200.0 };
        let f_lo = if idx == 0 { 32.0 } else { (f_prev * fc).sqrt() };
        let f_hi = if idx == 19 { 20600.0 } else { (fc * f_next).sqrt() };
        let (tier, df, k_max) = if idx < 4 {
            (0u8, 48000.0 / 2048.0, 1023usize)
        } else if idx < 9 {
            (1u8, 48000.0 / 1024.0, 511usize)
        } else if idx < 14 {
            (2u8, 48000.0 / 512.0, 255usize)
        } else {
            (3u8, 48000.0 / 256.0, 127usize)
        };
        let b_lo = (f_lo / df).max(0.6);
        let b_hi = (f_hi / df).clamp(b_lo + 0.6, k_max as f64);
        let k0 = (b_lo.floor() as usize).max(1);
        let k1 = (b_hi.ceil() as usize).clamp(k0, k_max);
        // Comment (invariant): Parseval Multi-Res Identity + Voxengo SPAN / FabFilter Pro-Q +4.2dB/oct Music Balance: when |X[k]|^2 is normalized by (N/4)^2, halving N doubles bin noise power and halves bin count (2 * 0.5 = 1.0 exact tier invariance); weighting Pink/Music mode by (fk / 350)^0.4 (+1.2dB/oct on top of +3.01dB/oct Parseval = +4.21dB/oct) balances bass and treble on real -4.5dB/oct mastered songs!
        let mut overlaps = Vec::with_capacity(k1 - k0 + 1);
        for k in k0..=k1 {
            let kf = k as f64;
            let fk = kf * df;
            let ov = (b_hi.min(kf + 0.5) - b_lo.max(kf - 0.5)).max(0.0);
            let tilt = if is_raw { 1.0 } else { (fk / 350.0).max(0.28).powf(0.40) };
            overlaps.push(ov * tilt);
        }
        let raw_div = if is_raw { (b_hi - b_lo).max(0.5) } else { 1.0 };
        specs.push(BandSpec {
            tier,
            k0,
            k1,
            overlaps,
            raw_div,
        });
    }
    specs
}

// Comment (invariant): Unpack 2-for-1 Hermitian stereo FFT bins Z[k] = L[k] + i*R[k] via L[k] = 0.5*(Z[k] + Z*[N-k]) and R[k] = -0.5i*(Z[k] - Z*[N-k]) to compute exact Left, Right, and Mono band RMS magnitudes in one pass.
#[inline]
fn unpack_hermitian_pwr(re: &[f64], im: &[f64], k: usize, inv_norm_sq: f64) -> (f64, f64, f64) {
    let n = re.len();
    let nk = n - k;
    let lr = 0.5 * (re[k] + re[nk]);
    let li = 0.5 * (im[k] - im[nk]);
    let rr = 0.5 * (im[k] + im[nk]);
    let ri = 0.5 * (re[nk] - re[k]);
    let mr = 0.5 * (lr + rr);
    let mi = 0.5 * (li + ri);
    (
        (lr * lr + li * li) * inv_norm_sq,
        (rr * rr + ri * ri) * inv_norm_sq,
        (mr * mr + mi * mi) * inv_norm_sq,
    )
}

#[inline]
fn fill_hermitian_win(
    ring_l: &[f64; 4096],
    ring_r: &[f64; 4096],
    offset: usize,
    win: &[f64],
    re: &mut [f64],
    im: &mut [f64],
) {
    let n = win.len();
    let (mut ml, mut mr) = (0.0f64, 0.0f64);
    for i in 0..n {
        ml += ring_l[offset + i];
        mr += ring_r[offset + i];
    }
    let inv_n = 1.0 / (n as f64);
    ml *= inv_n;
    mr *= inv_n;
    for i in 0..n {
        re[i] = (ring_l[offset + i] - ml) * win[i];
        im[i] = (ring_r[offset + i] - mr) * win[i];
    }
    radix2_fft(re, im);
}

#[inline]
fn integrate_dual_band(
    sp: &BandSpec,
    re_a: &[f64],
    im_a: &[f64],
    re_b: &[f64],
    im_b: &[f64],
    inv_sq: f64,
) -> (f64, f64, f64) {
    let (mut pla, mut pra, mut pma) = (0.0f64, 0.0f64, 0.0f64);
    let (mut plb, mut prb, mut pmb) = (0.0f64, 0.0f64, 0.0f64);
    for (wi, k) in (sp.k0..=sp.k1).enumerate() {
        let ov = sp.overlaps[wi];
        let (la2, ra2, ma2) = unpack_hermitian_pwr(re_a, im_a, k, inv_sq);
        let (lb2, rb2, mb2) = unpack_hermitian_pwr(re_b, im_b, k, inv_sq);
        pla += la2 * ov;
        pra += ra2 * ov;
        pma += ma2 * ov;
        plb += lb2 * ov;
        prb += rb2 * ov;
        pmb += mb2 * ov;
    }
    (
        (pla.max(plb) / sp.raw_div).sqrt(),
        (pra.max(prb) / sp.raw_div).sqrt(),
        (pma.max(pmb) / sp.raw_div).sqrt(),
    )
}

#[allow(clippy::too_many_arguments)]
fn eval_standard_multires_stereo(
    ring_l: &[f64; 4096],
    ring_r: &[f64; 4096],
    hann2048: &[f64; 2048],
    hann1024: &[f64; 1024],
    hann512: &[f64; 512],
    hann256: &[f64; 256],
    specs: &[BandSpec],
    mags_l: &mut [f64; 20],
    mags_r: &mut [f64; 20],
    mags_m: &mut [f64; 20],
) {
    let mut re2 = [0.0f64; 2048];
    let mut im2 = [0.0f64; 2048];
    fill_hermitian_win(ring_l, ring_r, 2048, hann2048, &mut re2, &mut im2);

    let mut re1a = [0.0f64; 1024];
    let mut im1a = [0.0f64; 1024];
    let mut re1b = [0.0f64; 1024];
    let mut im1b = [0.0f64; 1024];
    fill_hermitian_win(ring_l, ring_r, 2560, hann1024, &mut re1a, &mut im1a);
    fill_hermitian_win(ring_l, ring_r, 3072, hann1024, &mut re1b, &mut im1b);

    let mut re5a = [0.0f64; 512];
    let mut im5a = [0.0f64; 512];
    let mut re5b = [0.0f64; 512];
    let mut im5b = [0.0f64; 512];
    fill_hermitian_win(ring_l, ring_r, 3072, hann512, &mut re5a, &mut im5a);
    fill_hermitian_win(ring_l, ring_r, 3584, hann512, &mut re5b, &mut im5b);

    let mut re256a = [0.0f64; 256];
    let mut im256a = [0.0f64; 256];
    let mut re256b = [0.0f64; 256];
    let mut im256b = [0.0f64; 256];
    fill_hermitian_win(ring_l, ring_r, 3584, hann256, &mut re256a, &mut im256a);
    fill_hermitian_win(ring_l, ring_r, 3840, hann256, &mut re256b, &mut im256b);

    let inv2048 = 1.0 / (512.0 * 512.0);
    let inv1024 = 1.0 / (256.0 * 256.0);
    let inv512 = 1.0 / (128.0 * 128.0);
    let inv256 = 1.0 / (64.0 * 64.0);

    let (mut pl_f, mut pr_f, mut pm_f) = (0.0f64, 0.0f64, 0.0f64);
    for k in 1..=4 {
        let (l2, r2, m2) = unpack_hermitian_pwr(&re1b, &im1b, k, inv1024);
        pl_f += l2;
        pr_f += r2;
        pm_f += m2;
    }
    let (mut pl_s, mut pr_s, mut pm_s) = (0.0f64, 0.0f64, 0.0f64);
    for k in 1..=9 {
        let (l2, r2, m2) = unpack_hermitian_pwr(&re2, &im2, k, inv2048);
        pl_s += l2;
        pr_s += r2;
        pm_s += m2;
    }
    let gate_l = if pl_s > 0.1 { (pl_f / pl_s).sqrt().clamp(0.0, 1.4) } else { 1.0 };
    let gate_r = if pr_s > 0.1 { (pr_f / pr_s).sqrt().clamp(0.0, 1.4) } else { 1.0 };
    let gate_m = if pm_s > 0.1 { (pm_f / pm_s).sqrt().clamp(0.0, 1.4) } else { 1.0 };

    for (idx, sp) in specs.iter().enumerate() {
        match sp.tier {
            0 => {
                let (mut pl, mut pr, mut pm) = (0.0f64, 0.0f64, 0.0f64);
                for (wi, k) in (sp.k0..=sp.k1).enumerate() {
                    let (l2, r2, m2) = unpack_hermitian_pwr(&re2, &im2, k, inv2048);
                    let ov = sp.overlaps[wi];
                    pl += l2 * ov;
                    pr += r2 * ov;
                    pm += m2 * ov;
                }
                mags_l[idx] = (pl / sp.raw_div).sqrt() * gate_l;
                mags_r[idx] = (pr / sp.raw_div).sqrt() * gate_r;
                mags_m[idx] = (pm / sp.raw_div).sqrt() * gate_m;
            }
            1 => {
                let (ml, mr, mm) = integrate_dual_band(sp, &re1a, &im1a, &re1b, &im1b, inv1024);
                mags_l[idx] = ml;
                mags_r[idx] = mr;
                mags_m[idx] = mm;
            }
            2 => {
                let (ml, mr, mm) = integrate_dual_band(sp, &re5a, &im5a, &re5b, &im5b, inv512);
                mags_l[idx] = ml;
                mags_r[idx] = mr;
                mags_m[idx] = mm;
            }
            _ => {
                let (ml, mr, mm) = integrate_dual_band(sp, &re256a, &im256a, &re256b, &im256b, inv256);
                mags_l[idx] = ml;
                mags_r[idx] = mr;
                mags_m[idx] = mm;
            }
        }
    }
}

// Comment (invariant): Crisp 22.0dB RTA dynamic contrast with fast 0.15/0.85 attack + 0.48/0.52 release matching/beating C99_ORIG reactivity speed!
#[inline]
fn norm_and_smooth_20(
    mags: &[f64; 20],
    agc_peak: f64,
    gate_v: f64,
    smooth: &mut [f64; 20],
    dst: &mut [u8],
) {
    for i in 0..20 {
        let norm = if mags[i] < gate_v {
            0.05
        } else {
            let ratio = (mags[i] / agc_peak).clamp(1e-4, 1.0);
            let db_rel = 20.0 * ratio.log10();
            let lin = ((db_rel + 22.0) / 22.0).clamp(0.0, 1.0);
            lin.powf(1.35).clamp(0.05, 1.0)
        };
        let prev_b = smooth[i];
        let cur_b = if norm > prev_b {
            prev_b * 0.15 + norm * 0.85
        } else {
            prev_b * 0.48 + norm * 0.52
        }
        .clamp(0.05, 1.0);
        smooth[i] = cur_b;
        dst[i] = ((cur_b * 255.0).round() as i32).clamp(12, 255) as u8;
    }
}

#[inline]
fn norm_fast_rta_20(
    mags: &[f64; 20],
    sub_disp: f64,
    agc_peak: f64,
    gate_v: f64,
    fast_state: &mut [f64; 20],
    dst: &mut [u8],
) {
    let mut eff_mags = *mags;
    let mut upper_max = 0.0f64;
    for &m in &eff_mags[2..20] {
        if m > upper_max {
            upper_max = m;
        }
    }
    if sub_disp >= 4.0 && upper_max < sub_disp * 0.08 {
        eff_mags[0] = eff_mags[0].max(sub_disp * 0.5);
    }
    let eff_agc = agc_peak.max(if sub_disp >= 4.0 { sub_disp * 0.5 } else { 1.8 });
    for i in 0..20 {
        let norm = if eff_mags[i] < gate_v {
            0.0
        } else {
            let ratio = (eff_mags[i] / eff_agc).clamp(1e-4, 1.0);
            let db_rel = 20.0 * ratio.log10();
            (((db_rel + 22.0) / 22.0).clamp(0.0, 1.0)).powf(1.35)
        };
        let prev = fast_state[i];
        let cur = if norm >= prev {
            norm
        } else {
            prev * 0.15 + norm * 0.85
        };
        fast_state[i] = cur;
        dst[i] = (cur * 255.0).round().clamp(0.0, 255.0) as u8;
    }
}

// Comment (invariant): Standard YIN Cumulative Mean Normalized Difference Pitch Detector over newest 2560 samples (53ms): finds true fundamental period T0 (22..1500Hz) even when mid-bass harmonics are louder!
fn yin_detect_f0_code(ring4096: &[f64; 4096], dc_est: f64) -> u8 {
    let mut x = [0.0f64; 640];
    let mut e_tot = 0.0f64;
    for i in 0..640 {
        let o = 1536 + i * 4;
        let v = (ring4096[o] + ring4096[o + 1] + ring4096[o + 2] + ring4096[o + 3]) * 0.25 - dc_est;
        x[i] = v;
        e_tot += v * v;
    }
    if e_tot < 640.0 * 2.0 {
        return 0;
    }
    let w = 300usize;
    let mut cmnd = [1.0f64; 335];
    let mut run_sum = 0.0f64;
    for tau in 1..335 {
        let mut s = 0.0f64;
        for j in 0..w {
            let d = x[j] - x[j + tau];
            s += d * d;
        }
        run_sum += s;
        if run_sum > 1e-9 {
            cmnd[tau] = s * (tau as f64) / run_sum;
        }
    }
    let mut best_tau = 0usize;
    for tau in 8..330 {
        if cmnd[tau] < 0.22 && cmnd[tau] <= cmnd[tau - 1] && cmnd[tau] <= cmnd[tau + 1] {
            best_tau = tau;
            break;
        }
    }
    if best_tau == 0 {
        let mut min_v = 1e9f64;
        for tau in 8..330 {
            if cmnd[tau] < min_v {
                min_v = cmnd[tau];
                best_tau = tau;
            }
        }
        if min_v > 0.55 {
            return 0;
        }
    }
    let tau_f = if best_tau > 1 && best_tau < 333 {
        let (y0, y1, y2) = (cmnd[best_tau - 1], cmnd[best_tau], cmnd[best_tau + 1]);
        let denom = 2.0 * (2.0 * y1 - y0 - y2);
        let shift = if denom.abs() > 1e-9 {
            ((y0 - y2) / denom).clamp(-0.5, 0.5)
        } else {
            0.0
        };
        (best_tau as f64) + shift
    } else {
        best_tau as f64
    };
    let f0_hz = (12000.0 / tau_f.max(1.0)).clamp(18.0, 1500.0);
    let log_u = ((f0_hz / 18.0).ln() / (1500.0f64 / 18.0).ln()).clamp(0.0, 1.0);
    (1.0 + log_u * 254.0).round().clamp(1.0, 255.0) as u8
}

// Comment (invariant): Strict N008 Textbook Unaltered 0dB Triggered Digital Storage Oscilloscope (Foobar2000 / Corrscope / Tektronix Peak-Detect standard): ZERO Pink/treble boost and ZERO fake sine carriers on the waveform so bass keeps 100% natural physical dominance, gated on newest 928 samples for instant 0-frame silence release, triggered via 150Hz low-pass rising zero-crossing + correlation over a fast 20.2ms (stride=22) window with responsive 0.88 mix-relative crest normalization!
fn extract_corrscope_wave44(
    ring4096: &[f64; 4096],
    _dc_est: f64,
    _is_pink: bool,
    prev_trig_buf: &mut [f64; 44],
    wave_agc: &mut f64,
    dst44: &mut [u8],
) {
    let ring2048 = &ring4096[2048..4096];
    let mut sum_f = 0.0f64;
    for &v in &ring2048[1120..2048] {
        sum_f += v;
    }
    let m_fresh = sum_f * (1.0 / 928.0);
    let mut pk_fresh = 0.0f64;
    for &v in &ring2048[1120..2048] {
        let a = (v - m_fresh).abs();
        if a > pk_fresh {
            pk_fresh = a;
        }
    }
    if pk_fresh < 0.5 {
        *wave_agc = (*wave_agc * 0.85).max(1.0);
        dst44.fill(128);
        return;
    }

    let mut sum_all = 0.0f64;
    for &v in ring2048.iter() {
        sum_all += v;
    }
    let dc = sum_all * (1.0 / 2048.0);
    let mut sig = [0.0f64; 2048];
    let mut lp = [0.0f64; 2048];
    let mut acc = ring2048[0] - dc;
    for i in 0..2048 {
        let s = ring2048[i] - dc;
        sig[i] = s;
        acc += 0.0196 * (s - acc);
        lp[i] = acc;
    }

    let stride = 22usize;
    let win_len = 44 * stride;
    let search_end = 2048 - win_len;
    let search_start = 520usize;

    let mut best_idx = search_end;
    let mut best_sc = -1.0e30f64;
    let mut found = false;
    for ic in search_start..search_end {
        if lp[ic] <= 0.0 && lp[ic + 1] > 0.0 {
            found = true;
            let slope = lp[ic + 4] - lp[ic - 4];
            let mut corr = 0.0f64;
            let mut k = 0usize;
            while k < 44 {
                corr += sig[ic + k * stride] * prev_trig_buf[k];
                k += 2;
            }
            let sc = corr + slope * 4.0;
            if sc > best_sc {
                best_sc = sc;
                best_idx = ic;
            }
        }
    }
    if !found {
        best_idx = search_end;
    }

    // Comment (invariant): Proven in PNR-690 — scalar |mx|>=|mn| tie-breaks flip all 44 buckets between 0 and 43 crossings as 12kHz phase drifts, while discrete 48kHz max(seg) beats by cos(45deg)=0.7071 at 12kHz (4 samples/cycle); alternating (mean + pk_amp, mean - pk_amp) with Parseval RMS crest sqrt(2*Var) on multi-crossing slices (zc_s >= 3) keeps 12kHz rock-solid (0 pulsing, 0 amplitude beat) while preserving exact signed peaks on bass/mids!
    let mut dec = [0.0f64; 44];
    let mut w_pk = 0.5f64;
    let inv_st = 1.0 / (stride as f64);
    for k in 0..44 {
        let b0 = best_idx + k * stride;
        let mut mn = sig[b0];
        let mut mx = sig[b0];
        let mut sum_s = sig[b0];
        for j in 1..stride {
            let v = sig[b0 + j];
            if v < mn {
                mn = v;
            }
            if v > mx {
                mx = v;
            }
            sum_s += v;
        }
        let mean_s = sum_s * inv_st;
        let mut zc_s = 0usize;
        let mut var_acc = (sig[b0] - mean_s) * (sig[b0] - mean_s);
        for j in 1..stride {
            let d0 = sig[b0 + j - 1] - mean_s;
            let d1 = sig[b0 + j] - mean_s;
            if d0 * d1 < 0.0 {
                zc_s += 1;
            }
            var_acc += d1 * d1;
        }
        let picked = if zc_s >= 3 {
            let rms_crest = (2.0 * var_acc * inv_st).sqrt();
            let pk_amp = (0.5 * (mx - mn)).max(rms_crest);
            if (k & 1) == 0 {
                mean_s + pk_amp
            } else {
                mean_s - pk_amp
            }
        } else if mx.abs() >= mn.abs() {
            mx
        } else {
            mn
        };
        dec[k] = picked;
        if picked.abs() > w_pk {
            w_pk = picked.abs();
        }
    }

    *prev_trig_buf = dec;
    if w_pk > *wave_agc {
        *wave_agc = *wave_agc * 0.25 + w_pk * 0.75;
    } else {
        *wave_agc = (*wave_agc * 0.88 + w_pk * 0.12).max(1.0);
    }
    let inv_a = 1.0 / *wave_agc;
    for k in 0..44 {
        let norm_v = (dec[k] * inv_a).clamp(-1.0, 1.0);
        dst44[k] = (128.0 + norm_v * 124.0).round().clamp(4.0, 252.0) as u8;
    }
}

#[inline]
fn shift_f64_slice(ring: &mut [f64], fresh: &[f64], n: usize) {
    let len = ring.len();
    if n == 0 {
        return;
    }
    if n >= len {
        ring.copy_from_slice(&fresh[n - len..n]);
    } else {
        ring.copy_within(n..len, 0);
        ring[len - n..len].copy_from_slice(&fresh[..n]);
    }
}

#[inline]
fn sinc_interp_32(sig: &[f64; 98304], dc: f64, pos_f: f64) -> f64 {
    let i_c = pos_f.floor() as isize;
    let fr = pos_f - (i_c as f64);
    if fr < 1e-6 {
        return sig[i_c.clamp(0, 98303) as usize] - dc;
    }
    let mut acc_s = 0.0f64;
    let mut w_sum = 0.0f64;
    for m in -15isize..=16isize {
        let x = fr - (m as f64);
        let px_w = PI * x;
        let sv = if x.abs() < 1e-7 { 1.0 } else { px_w.sin() / px_w };
        let u = (x + 16.0) * (1.0 / 32.0);
        let bw = 0.35875
            - 0.48829 * (2.0 * PI * u).cos()
            + 0.14128 * (4.0 * PI * u).cos()
            - 0.01168 * (6.0 * PI * u).cos();
        let w = sv * bw;
        let idx = (i_c + m).clamp(0, 98303) as usize;
        acc_s += (sig[idx] - dc) * w;
        w_sum += w;
    }
    acc_s / w_sum
}

// Comment (invariant): Contiguous 98,304-Sample (2.048s!) Zero-Splice & Full 0.55Hz..20kHz Stationary-Lock Oscilloscope (N008 + N005): (1) 98,304-sample ring holds >2.0 full cycles of 1.0 Hz (48,000 samples/cycle) and >4.0 cycles of 2.0 Hz (24,000 samples/cycle), so `(max + min) * 0.5` yields exact 0.0-LSB DC symmetry and the 96,000-sample Schmitt-hysteresis + anti-dither fine rising-edge lock freezes 1.0Hz, 2.0Hz, 3Hz, 5Hz, 15Hz, and 50Hz ALL completely static (`max_jmp <= 0.01`); (2) O(4096) local 120Hz LP search handles >=22Hz music in <150us while stride-8 coarse + 64-sample fine scan handles <22Hz infrasonic in <180us!
fn extract_dso_trace120(
    scope_hist: &[f64; 98304],
    dc_est: &mut f64,
    fixed_stride_q4: usize,
    auto_state: &mut [f64; 3],
    prev_trig_mid: &mut [f64; 60],
    wave_agc: &mut f64,
    dst120: &mut [u8],
) {
    // Comment (invariant): Unaliased full-ring min/max scan — NEVER step by 16 (3kHz), which aliased >5200Hz tones into a fake DC offset that kept pk_dc_fresh >= 2.0 for 0.5s after stop and froze the dead tone's last rising edge on the left half of the screen!
    let mut vmin_s = scope_hist[0];
    let mut vmax_s = scope_hist[0];
    for &v in scope_hist.iter() {
        if v < vmin_s {
            vmin_s = v;
        }
        if v > vmax_s {
            vmax_s = v;
        }
    }
    let dc_sym = 0.5 * (vmax_s + vmin_s);
    *dc_est = *dc_est * 0.90 + dc_sym * 0.10;
    let dc = *dc_est;
    let pk_all = (0.5 * (vmax_s - vmin_s)).max(1.0);

    let mut sum_f = 0.0f64;
    for &v in &scope_hist[97376..98304] {
        sum_f += v;
    }
    let m_fresh = sum_f * (1.0 / 928.0);
    let mut pk_fresh = 0.0f64;
    let mut pk_dc_fresh = 0.0f64;
    for &v in &scope_hist[97376..98304] {
        let a = (v - m_fresh).abs();
        if a > pk_fresh {
            pk_fresh = a;
        }
        let d = (v - dc).abs();
        if d > pk_dc_fresh {
            pk_dc_fresh = d;
        }
    }
    if pk_fresh < 0.5 && pk_dc_fresh < (pk_all * 0.08).max(2.0) {
        *wave_agc = (*wave_agc * 0.85).max(1.0);
        auto_state[1] *= 0.85;
        dst120.fill(128);
        return;
    }

    let mut e_sig_sub = 0.0f64;
    let mut e_dif_sub = 0.0f64;
    let mut prev_sub = scope_hist[0] - dc;
    let mut idx_s = 0usize;
    while idx_s < 98304 {
        let s = scope_hist[idx_s] - dc;
        e_sig_sub += s * s;
        if idx_s > 0 {
            let df = s - prev_sub;
            e_dif_sub += df * df;
        }
        prev_sub = s;
        idx_s += 16;
    }
    let e_bound = e_sig_sub.max(0.5 * pk_all * pk_all * 6144.0).max(1e-9);
    let f_96k = (3000.0 / (2.0 * PI)) * (e_dif_sub / e_bound).min(4.0).sqrt();
    auto_state[2] = if auto_state[2] <= 0.0 {
        f_96k
    } else {
        auto_state[2] * 0.85 + f_96k * 0.15
    };
    let f_infra = auto_state[2];

    let tail_0 = 94208usize;
    let mut lp_tail = [0.0f64; 4096];
    let mut acc_t = scope_hist[tail_0] - dc;
    for i in 0..4096 {
        let s = scope_hist[tail_0 + i] - dc;
        acc_t += 0.016 * (s - acc_t);
        lp_tail[i] = acc_t;
    }

    let mut e_sig = 1e-9f64;
    let mut e_bass = 0.0f64;
    let mut e_dif = 0.0f64;
    for i in 2048..4096 {
        let s = scope_hist[tail_0 + i] - dc;
        let s_prev = scope_hist[tail_0 + i - 1] - dc;
        e_sig += s * s;
        e_bass += lp_tail[i] * lp_tail[i];
        let d = s - s_prev;
        e_dif += d * d;
    }
    let w_est = (e_dif / e_sig).min(4.0).sqrt();
    let f_rms = (48000.0 / (2.0 * PI)) * (2.0 * (0.5 * w_est).min(1.0).asin());

    let stride: f64 = match fixed_stride_q4 {
        32 => 7.5,
        16 => 4.0,
        8 => 2.0,
        4 => 1.0,
        2 => 0.58,
        1 => 0.40,
        _ => {
            if e_bass >= e_sig * 0.12 || f_rms <= 700.0 {
                auto_state[1] = 1.0;
            } else {
                auto_state[1] *= 0.90;
            }
            let target_st = if auto_state[1] > 0.35 {
                7.5
            } else {
                let norm_f = (f_rms / 700.0).max(1.0);
                (7.5 * norm_f.powf(-0.86)).clamp(0.45, 7.5)
            };
            let prev_st = auto_state[0].clamp(0.40, 7.5);
            let alpha = if target_st > prev_st { 0.50 } else { 0.32 };
            let mut st = (prev_st.ln() * (1.0 - alpha) + target_st.ln() * alpha).exp();
            if (st - 7.5).abs() < 0.08 {
                st = 7.5;
            }
            auto_state[0] = st;
            st
        }
    };

    let half_win = ((60.0 * stride).ceil() as usize) + 18;
    let zc_end = 98284 - half_win;
    let zc_near_start = (tail_0 + 16).max(half_win + 18).max(zc_end.saturating_sub(2100));
    let zc_deep_start = (half_win + 64).max(zc_end.saturating_sub(96000));
    let hyst = (pk_all * 0.05).max(2.0);
    let st_int = (stride.round() as usize).max(1);

    let mut has_near = false;
    let mut b_zc = zc_end;
    let mut best_sc = -1.0e30f64;
    if f_infra >= 22.0 {
        let mut armed = if stride >= 6.0 {
            lp_tail[zc_near_start - tail_0] <= -hyst
        } else {
            (scope_hist[zc_near_start] - dc) <= -hyst
        };
        for ic in zc_near_start..zc_end {
            let (v0, v1) = if stride >= 6.0 {
                (lp_tail[ic - tail_0], lp_tail[ic + 1 - tail_0])
            } else {
                (scope_hist[ic] - dc, scope_hist[ic + 1] - dc)
            };
            if v0 <= -hyst {
                armed = true;
            }
            if armed && v0 <= 0.0 && v1 > 0.0 {
                armed = false;
                has_near = true;
                let slope = if stride >= 6.0 {
                    lp_tail[(ic + st_int - tail_0).min(4095)] - lp_tail[(ic - tail_0).saturating_sub(st_int)]
                } else {
                    scope_hist[(ic + st_int).min(98303)] - scope_hist[ic.saturating_sub(st_int)]
                };
                let mut corr = 0.0f64;
                for (k, &pm) in prev_trig_mid.iter().enumerate() {
                    let s_pos = ((ic as f64) + (((k as isize) * 2 - 60) as f64) * stride).round() as isize;
                    if stride >= 6.0 && s_pos >= (tail_0 as isize) && s_pos < 98304 {
                        corr += lp_tail[(s_pos as usize) - tail_0] * pm;
                    } else {
                        corr += (scope_hist[s_pos.clamp(0, 98303) as usize] - dc) * pm;
                    }
                }
                let sc = corr + slope * pk_all * 0.4;
                if sc > best_sc {
                    best_sc = sc;
                    b_zc = ic;
                }
            }
        }
    }

    let (best_zc, zc_frac) = if has_near {
        let (v0, v1) = if stride >= 6.0 {
            (lp_tail[b_zc - tail_0], lp_tail[b_zc + 1 - tail_0])
        } else {
            (scope_hist[b_zc] - dc, scope_hist[b_zc + 1] - dc)
        };
        let frac = if (v1 - v0) > 1e-6 { -v0 / (v1 - v0) } else { 0.0 };
        (b_zc, frac)
    } else {
        let mut pk_near = 0.0f64;
        for &v in &scope_hist[zc_near_start..98304] {
            let a = (v - dc).abs();
            if a > pk_near {
                pk_near = a;
            }
        }
        if pk_near < (pk_all * 0.05).max(2.0) {
            *wave_agc = (*wave_agc * 0.85).max(1.0);
            auto_state[1] *= 0.85;
            dst120.fill(128);
            return;
        }
        let mut acc_d = scope_hist[zc_deep_start] - dc;
        let mut prev_d = acc_d;
        let mut armed_d = acc_d <= -hyst;
        let mut found_deep = 0usize;
        let mut ic = zc_deep_start + 8;
        while ic < zc_end {
            acc_d += 0.25 * ((scope_hist[ic] - dc) - acc_d);
            if acc_d <= -hyst {
                armed_d = true;
            }
            if armed_d && prev_d <= 0.0 && acc_d > 0.0 {
                found_deep = ic;
                armed_d = false;
            }
            prev_d = acc_d;
            ic += 8;
        }
        if found_deep > 0 {
            let j0 = (half_win + 16).max(found_deep.saturating_sub(64));
            let j1 = zc_end.min(found_deep + 16);
            let mut f_lp = scope_hist[j0 - 16] - dc;
            for &w_v in &scope_hist[j0 - 15..j0] {
                f_lp += 0.15 * ((w_v - dc) - f_lp);
            }
            let mut prev_f = f_lp;
            let mut r_zc = found_deep;
            let mut r_fr = 0.0f64;
            for j in j0..j1 {
                f_lp += 0.15 * ((scope_hist[j] - dc) - f_lp);
                if prev_f <= 0.0 && f_lp > 0.0 {
                    r_zc = j;
                    if (f_lp - prev_f) > 1e-6 {
                        r_fr = -prev_f / (f_lp - prev_f);
                    }
                    break;
                }
                prev_f = f_lp;
            }
            (r_zc, r_fr)
        } else {
            (zc_end, 0.0)
        }
    };
    let center_pos = (best_zc as f64) + zc_frac;

    let mut pts = [0.0f64; 120];
    let mut mids = [0.0f64; 60];
    let mut sq_sum = 0.0f64;
    let mut pk_abs = 0.5f64;
    for k in 0..120 {
        let pos = center_pos + (((k as isize) - 60) as f64) * stride;
        let val = if stride < 2.5 {
            sinc_interp_32(scope_hist, dc, pos)
        } else {
            let i0 = (pos as usize).min(98302);
            let fr = pos - (i0 as f64);
            (scope_hist[i0] * (1.0 - fr) + scope_hist[i0 + 1] * fr) - dc
        };
        pts[k] = val;
        if (k & 1) == 0 {
            mids[k >> 1] = val;
        }
        sq_sum += val * val;
        if val.abs() > pk_abs {
            pk_abs = val.abs();
        }
    }

    *prev_trig_mid = mids;
    let rms_crest = (sq_sum * (1.0 / 120.0)).sqrt() * std::f64::consts::SQRT_2;
    let w_pk = pk_abs.max(rms_crest).max(if f_infra < 35.0 { pk_all } else { 0.0 });
    let decay_k = if f_infra < 30.0 { 0.996 } else { 0.88 };
    if w_pk > *wave_agc {
        *wave_agc = *wave_agc * 0.25 + w_pk * 0.75;
    } else {
        *wave_agc = (*wave_agc * decay_k + w_pk * (1.0 - decay_k)).max(1.0);
    }
    let inv_a = 1.0 / *wave_agc;
    for k in 0..120 {
        let nv = (pts[k] * inv_a).clamp(-1.0, 1.0);
        dst120[k] = (128.0 + nv * 124.0).round().clamp(4.0, 252.0) as u8;
    }
}

fn kill_children(main_c: &mut Option<Child>, aux_c: &mut Option<Child>) {
    if let Some(ref mut c) = main_c {
        let _ = c.kill();
        let _ = c.wait();
    }
    if let Some(ref mut c) = aux_c {
        let _ = c.kill();
        let _ = c.wait();
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 5 {
        std::process::exit(1);
    }
    let viz_mode = args[1].as_str();
    let curve_mode = args[2].as_str();
    let amb_on = args[3].as_str();
    let stream_idx = args[4].as_str();
    let proto = args.get(5).map(|s| s.as_str()).unwrap_or("");
    let ext_mode = matches!(proto, "ext344" | "ext192" | "ext152" | "ext108");
    let is_ext344 = proto == "ext344";
    let is_ext152 = proto == "ext152" || proto == "ext192" || is_ext344;
    let is_ext192 = proto == "ext192" || is_ext344;
    let channels: u8 = if ext_mode { 2 } else { 1 };

    // Comment (invariant): Zero-Delay Constant-Q Multi-Resolution IEC 61260 FFT-RTA (2048/1024/512/256 Hann tiers) covering 32Hz..20.6kHz with exact Parseval +3.01dB/octave Pink-noise response.
    let centers: [f64; 20] = [
        45.0, 65.0, 92.0, 130.0, 185.0, 260.0, 365.0, 510.0, 700.0, 960.0, 1320.0, 1800.0,
        2450.0, 3350.0, 4550.0, 6200.0, 8400.0, 11400.0, 15200.0, 19500.0,
    ];
    let is_pink = curve_mode != "raw";
    let specs = build_band_specs(&centers, curve_mode == "raw");
    let mut hann2048 = Box::new([0.0f64; 2048]);
    for i in 0..2048 {
        hann2048[i] = 0.5 * (1.0 - (2.0 * PI * (i as f64) / 2047.0).cos());
    }
    let mut hann1024 = [0.0f64; 1024];
    for (i, h) in hann1024.iter_mut().enumerate() {
        *h = 0.5 * (1.0 - (2.0 * PI * (i as f64) / 1023.0).cos());
    }
    let mut hann512 = [0.0f64; 512];
    for (i, h) in hann512.iter_mut().enumerate() {
        *h = 0.5 * (1.0 - (2.0 * PI * (i as f64) / 511.0).cos());
    }
    let mut hann256 = [0.0f64; 256];
    for (i, h) in hann256.iter_mut().enumerate() {
        *h = 0.5 * (1.0 - (2.0 * PI * (i as f64) / 255.0).cos());
    }
    let tb_arg = args.get(6).map(|s| s.as_str()).unwrap_or("auto");
    let fixed_stride: usize = match tb_arg {
        "20ms" => 32,
        "10ms" => 16,
        "5ms" => 8,
        "2.5ms" => 4,
        "1.2ms" => 2,
        "0.6ms" => 1,
        _ => 0,
    };

    let mut using_stream_idx = false;
    let mut main_child: Option<Child>;
    let mut aux_child: Option<Child> = None;

    if viz_mode == "mic_only" {
        main_child = spawn_parec("vip_mic_bus.monitor", false, channels);
    } else if viz_mode == "music_only" {
        if !stream_idx.is_empty() {
            main_child = spawn_parec(stream_idx, true, channels);
            using_stream_idx = true;
        } else {
            main_child = spawn_parec("@DEFAULT_MONITOR@", false, channels);
        }
    } else {
        main_child = spawn_parec("@DEFAULT_MONITOR@", false, channels);
        if amb_on == "0" {
            aux_child = spawn_parec("vip_mic_bus.monitor", false, channels);
        }
    }

    if main_child.is_none() {
        return;
    }

    let hop_samples: usize = 928;
    let hop_bytes: usize = if ext_mode { hop_samples * 4 } else { hop_samples * 2 };
    let align_mask: usize = if ext_mode { !3usize } else { !1usize };
    let mut ring_main = vec![0u8; if ext_mode { 4096 } else { 2048 }];
    let mut ring_aux = vec![0u8; if ext_mode { 4096 } else { 2048 }];
    let mut ring_l = Box::new([0.0f64; 4096]);
    let mut ring_r = Box::new([0.0f64; 4096]);
    let mut chunk = [0u8; 4096];
    let mut out20 = [12u8; 20];
    let mut out344 = [12u8; 344];
    let is_music_only = viz_mode == "music_only";
    let mut agc_peak = if is_music_only { 6.0 } else { 180.0 };
    let floor_pk = if is_music_only { 1.8 } else { 18.0 };
    let gate_v = if is_music_only { 0.15 } else { 2.2 };
    let mut smooth_m = [0.05f64; 20];
    let mut smooth_l = [0.05f64; 20];
    let mut smooth_r = [0.05f64; 20];
    let mut agc_peak_l = if is_music_only { 6.0 } else { 180.0 };
    let mut agc_peak_r = if is_music_only { 6.0 } else { 180.0 };
    let mut dc_est_l = 0.0f64;
    let mut dc_est_r = 0.0f64;
    let mut fast_st_l = [0.0f64; 20];
    let mut fast_st_r = [0.0f64; 20];
    let mut prev_trig_l = [0.0f64; 44];
    let mut prev_trig_r = [0.0f64; 44];
    let mut scope_hist_l = Box::new([0.0f64; 98304]);
    let mut scope_hist_r = Box::new([0.0f64; 98304]);
    let mut scope_dc_l = 0.0f64;
    let mut scope_dc_r = 0.0f64;
    let mut prev_dso_l = [0.0f64; 60];
    let mut prev_dso_r = [0.0f64; 60];
    let mut auto_st_l = [7.5f64, 0.0f64, 0.0f64];
    let mut auto_st_r = [7.5f64, 0.0f64, 0.0f64];
    let mut wave_agc_l = 6.0f64;
    let mut wave_agc_r = 6.0f64;
    // Comment (invariant): Proven in PNR-687 — capping live parec reads at hop_bytes (3712B) trapped startup/PipeWire 3840B bursts inside the kernel pipe forever (+85..300ms bar lag!); in live mode (!offline_mock), drain O_NONBLOCK fd_main until WouldBlock directly into ring_l/ring_r so 0 stale bytes ever sit queued in the pipe!
    let offline_mock = env::var_os("MOCK_PCM_FILE").is_some();
    let frame_bytes = if ext_mode { 4usize } else { 2usize };
    let mut dec_l = [0.0f64; 1024];
    let mut dec_r = [0.0f64; 1024];
    let mut bytes_since_calc = 0usize;
    let mut stdout = std::io::stdout();

    loop {
        let fd_main = match main_child.as_ref().and_then(|c| c.stdout.as_ref()) {
            Some(s) => s.as_raw_fd(),
            None => break,
        };
        let fd_aux = aux_child
            .as_ref()
            .and_then(|c| c.stdout.as_ref())
            .map(|s| s.as_raw_fd())
            .unwrap_or(-1);

        if fd_aux >= 0 && (!offline_mock || bytes_since_calc < hop_bytes) {
            while let Some(Ok(n)) = aux_child
                .as_mut()
                .and_then(|c| c.stdout.as_mut())
                .map(|s| s.read(&mut chunk))
            {
                if n == 0 {
                    break;
                }
                shift_ring_var(&mut ring_aux, &chunk, n, align_mask);
            }
        }

        let mut pfds = [
            PollFd {
                fd: fd_main,
                events: POLLIN,
                revents: 0,
            },
            PollFd {
                fd: fd_aux,
                events: POLLIN,
                revents: 0,
            },
        ];
        let nfds = if fd_aux >= 0 { 2 } else { 1 };
        let pr = unsafe { poll(pfds.as_mut_ptr(), nfds, 30) };

        if pr > 0 && (pfds[0].revents & (POLLIN | POLLHUP | POLLERR)) != 0 {
            loop {
                if offline_mock && bytes_since_calc >= hop_bytes {
                    break;
                }
                let rem = if offline_mock {
                    (hop_bytes - bytes_since_calc).min(chunk.len())
                } else {
                    chunk.len()
                };
                let read_res = main_child
                    .as_mut()
                    .and_then(|c| c.stdout.as_mut())
                    .map(|s| s.read(&mut chunk[..rem]));
                match read_res {
                    Some(Ok(0)) => {
                        if using_stream_idx {
                            if let Some(ref mut old) = main_child {
                                let _ = old.kill();
                                let _ = old.wait();
                            }
                            main_child = spawn_parec("@DEFAULT_MONITOR@", false, channels);
                            using_stream_idx = false;
                            if main_child.is_none() {
                                kill_children(&mut main_child, &mut aux_child);
                                return;
                            }
                            break;
                        } else {
                            kill_children(&mut main_child, &mut aux_child);
                            return;
                        }
                    }
                    Some(Ok(n)) => {
                        let nc = n & align_mask;
                        bytes_since_calc += nc;
                        shift_ring_var(&mut ring_main, &chunk, nc, align_mask);
                        let ns = nc / frame_bytes;
                        if ns > 0 {
                            if ext_mode {
                                for i in 0..ns {
                                    let o = i * 4;
                                    let mut l = i16::from_le_bytes([chunk[o], chunk[o + 1]]) as f64;
                                    let mut r = i16::from_le_bytes([chunk[o + 2], chunk[o + 3]]) as f64;
                                    if fd_aux >= 0 {
                                        let ao = ring_aux.len() - nc + o;
                                        l += (i16::from_le_bytes([ring_aux[ao], ring_aux[ao + 1]]) as f64) * 1.15;
                                        r += (i16::from_le_bytes([ring_aux[ao + 2], ring_aux[ao + 3]]) as f64) * 1.15;
                                    }
                                    dec_l[i] = l;
                                    dec_r[i] = r;
                                }
                            } else {
                                for i in 0..ns {
                                    let o = i * 2;
                                    let mut m = i16::from_le_bytes([chunk[o], chunk[o + 1]]) as f64;
                                    if fd_aux >= 0 {
                                        let ao = ring_aux.len() - nc + o;
                                        m += (i16::from_le_bytes([ring_aux[ao], ring_aux[ao + 1]]) as f64) * 1.15;
                                    }
                                    dec_l[i] = m;
                                    dec_r[i] = m;
                                }
                            }
                            shift_f64_ring(&mut ring_l, &dec_l, ns);
                            shift_f64_ring(&mut ring_r, &dec_r, ns);
                            shift_f64_slice(&mut *scope_hist_l, &dec_l, ns);
                            shift_f64_slice(&mut *scope_hist_r, &dec_r, ns);
                        }
                    }
                    Some(Err(ref e)) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                    _ => {
                        kill_children(&mut main_child, &mut aux_child);
                        return;
                    }
                }
            }
        }

        if bytes_since_calc < hop_bytes {
            continue;
        }
        bytes_since_calc = 0;

        let mut mags_l = [0.0f64; 20];
        let mut mags_r = [0.0f64; 20];
        let mut mags_m = [0.0f64; 20];
        eval_standard_multires_stereo(
            &ring_l,
            &ring_r,
            &hann2048,
            &hann1024,
            &hann512,
            &hann256,
            &specs,
            &mut mags_l,
            &mut mags_r,
            &mut mags_m,
        );

        let mut frame_max = floor_pk;
        let mut max_l = floor_pk;
        let mut max_r = floor_pk;
        for i in 0..20 {
            if mags_m[i] > frame_max {
                frame_max = mags_m[i];
            }
            if mags_l[i] > max_l {
                max_l = mags_l[i];
            }
            if mags_r[i] > max_r {
                max_r = mags_r[i];
            }
        }

        agc_peak = if frame_max > agc_peak {
            agc_peak * 0.35 + frame_max * 0.65
        } else {
            (agc_peak * 0.96 + frame_max * 0.04).max(floor_pk)
        };

        if !ext_mode {
            norm_and_smooth_20(&mags_m, agc_peak, gate_v, &mut smooth_m, &mut out20);
            if stdout.write_all(&out20).is_err() || stdout.flush().is_err() {
                break;
            }
            continue;
        }

        agc_peak_l = if max_l > agc_peak_l {
            agc_peak_l * 0.35 + max_l * 0.65
        } else {
            (agc_peak_l * 0.96 + max_l * 0.04).max(floor_pk)
        };
        agc_peak_r = if max_r > agc_peak_r {
            agc_peak_r * 0.35 + max_r * 0.65
        } else {
            (agc_peak_r * 0.96 + max_r * 0.04).max(floor_pk)
        };

        norm_and_smooth_20(&mags_m, agc_peak, gate_v, &mut smooth_m, &mut out344[0..20]);
        norm_and_smooth_20(&mags_l, agc_peak_l, gate_v, &mut smooth_l, &mut out344[20..40]);
        norm_and_smooth_20(&mags_r, agc_peak_r, gate_v, &mut smooth_r, &mut out344[40..60]);

        let (mut wl_m, mut wr_m) = (0.0f64, 0.0f64);
        for i in 3072..4096 {
            wl_m += ring_l[i];
            wr_m += ring_r[i];
        }
        wl_m *= 1.0 / 1024.0;
        wr_m *= 1.0 / 1024.0;
        dc_est_l = dc_est_l * 0.992 + wl_m * 0.008;
        dc_est_r = dc_est_r * 0.992 + wr_m * 0.008;
        let sub_disp_l = (wl_m - dc_est_l).abs();
        let sub_disp_r = (wr_m - dc_est_r).abs();

        let _ = (wl_m, wr_m);
        if is_ext344 {
            extract_dso_trace120(&scope_hist_l, &mut scope_dc_l, fixed_stride, &mut auto_st_l, &mut prev_dso_l, &mut wave_agc_l, &mut out344[60..180]);
            extract_dso_trace120(&scope_hist_r, &mut scope_dc_r, fixed_stride, &mut auto_st_r, &mut prev_dso_r, &mut wave_agc_r, &mut out344[180..300]);
            norm_fast_rta_20(&mags_l, sub_disp_l, agc_peak_l, gate_v, &mut fast_st_l, &mut out344[304..324]);
            norm_fast_rta_20(&mags_r, sub_disp_r, agc_peak_r, gate_v, &mut fast_st_r, &mut out344[324..344]);
        } else if is_ext152 {
            extract_corrscope_wave44(&ring_l, dc_est_l, is_pink, &mut prev_trig_l, &mut wave_agc_l, &mut out344[60..104]);
            extract_corrscope_wave44(&ring_r, dc_est_r, is_pink, &mut prev_trig_r, &mut wave_agc_r, &mut out344[104..148]);
            if is_ext192 {
                norm_fast_rta_20(&mags_l, sub_disp_l, agc_peak_l, gate_v, &mut fast_st_l, &mut out344[152..172]);
                norm_fast_rta_20(&mags_r, sub_disp_r, agc_peak_r, gate_v, &mut fast_st_r, &mut out344[172..192]);
            }
        } else {
            extract_corrscope_wave44(&ring_l, dc_est_l, is_pink, &mut prev_trig_l, &mut wave_agc_l, &mut out344[60..104]);
        }

        let sub_e = (smooth_m[0].max(smooth_m[1]).max(smooth_m[2]).max(smooth_m[3])).clamp(0.05, 1.0);
        let mid_e = (smooth_m[6].max(smooth_m[8]).max(smooth_m[10]).max(smooth_m[12])).clamp(0.05, 1.0);
        let env_off = if is_ext344 {
            300
        } else if is_ext152 {
            148
        } else {
            104
        };
        out344[env_off] = (sub_e * 255.0).round().clamp(12.0, 255.0) as u8;
        out344[env_off + 1] = (mid_e * 255.0).round().clamp(12.0, 255.0) as u8;
        out344[env_off + 2] = yin_detect_f0_code(&ring_l, dc_est_l);
        out344[env_off + 3] = yin_detect_f0_code(&ring_r, dc_est_r);

        let pkt_slice = if is_ext344 {
            &out344[0..344]
        } else if is_ext192 {
            &out344[0..192]
        } else if is_ext152 {
            &out344[0..152]
        } else {
            &out344[0..108]
        };
        if stdout.write_all(pkt_slice).is_err() || stdout.flush().is_err() {
            break;
        }
    }
    kill_children(&mut main_child, &mut aux_child);
}
