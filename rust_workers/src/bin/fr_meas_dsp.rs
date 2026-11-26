// /vipxpert_minimal_os/rust_workers/src/bin/fr_meas_dsp.rs
use std::env;
use std::f64::consts::PI;
use std::fs::File;
use std::io::{Read, Write};

fn fft_in_place(re: &mut [f64], im: &mut [f64], inverse: bool) {
    let n = re.len();
    let mut j = 0usize;
    for i in 1..n {
        let mut bit = n >> 1;
        while j & bit != 0 {
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
        let ang = 2.0 * PI / (len as f64) * if inverse { 1.0 } else { -1.0 };
        let wlen_re = ang.cos();
        let wlen_im = ang.sin();
        let half = len >> 1;
        let mut i = 0usize;
        while i < n {
            let mut w_re = 1.0;
            let mut w_im = 0.0;
            for k in 0..half {
                let u_re = re[i + k];
                let u_im = im[i + k];
                let v_re = re[i + k + half] * w_re - im[i + k + half] * w_im;
                let v_im = re[i + k + half] * w_im + im[i + k + half] * w_re;
                re[i + k] = u_re + v_re;
                im[i + k] = u_im + v_im;
                re[i + k + half] = u_re - v_re;
                im[i + k + half] = u_im - v_im;
                let nw_re = w_re * wlen_re - w_im * wlen_im;
                w_im = w_re * wlen_im + w_im * wlen_re;
                w_re = nw_re;
            }
            i += len;
        }
        len <<= 1;
    }

    if inverse {
        let inv_n = 1.0 / (n as f64);
        for i in 0..n {
            re[i] *= inv_n;
            im[i] *= inv_n;
        }
    }
}

fn generate_sweep_samples(rate: usize, f0: f64, f1: f64, dur: f64, amp: f64) -> Vec<f64> {
    let n = (rate as f64 * dur).round() as usize;
    let l = dur / (f1 / f0).ln();
    let fade = (rate as f64 * 0.015).round() as usize;
    let mut out = vec![0.0; n];
    for i in 0..n {
        let t = i as f64 / rate as f64;
        let phase = 2.0 * PI * f0 * l * ((t / l).exp() - 1.0);
        let mut env_w = 1.0;
        if i < fade {
            env_w = 0.5 * (1.0 - (PI * i as f64 / fade as f64).cos());
        } else if i + fade >= n {
            let rem = n - 1 - i;
            env_w = 0.5 * (1.0 - (PI * rem as f64 / fade as f64).cos());
        }
        out[i] = amp * env_w * phase.sin();
    }
    out
}

fn write_wav_s32_stereo(path: &str, rate: u32, samples: &[f64]) -> Result<(), String> {
    let n = samples.len();
    let data_bytes = (n * 2 * 4) as u32;
    let riff_size = 36 + data_bytes;
    let mut f = File::create(path).map_err(|e| format!("create {}: {}", path, e))?;
    f.write_all(b"RIFF").map_err(|e| e.to_string())?;
    f.write_all(&riff_size.to_le_bytes()).map_err(|e| e.to_string())?;
    f.write_all(b"WAVE").map_err(|e| e.to_string())?;
    f.write_all(b"fmt ").map_err(|e| e.to_string())?;
    f.write_all(&16u32.to_le_bytes()).map_err(|e| e.to_string())?;
    f.write_all(&1u16.to_le_bytes()).map_err(|e| e.to_string())?; // PCM
    f.write_all(&2u16.to_le_bytes()).map_err(|e| e.to_string())?; // Stereo
    f.write_all(&rate.to_le_bytes()).map_err(|e| e.to_string())?;
    f.write_all(&(rate * 8).to_le_bytes()).map_err(|e| e.to_string())?; // byte rate
    f.write_all(&8u16.to_le_bytes()).map_err(|e| e.to_string())?; // block align
    f.write_all(&32u16.to_le_bytes()).map_err(|e| e.to_string())?; // bits per sample
    f.write_all(b"data").map_err(|e| e.to_string())?;
    f.write_all(&data_bytes.to_le_bytes()).map_err(|e| e.to_string())?;

    let mut buf = Vec::with_capacity(n * 8);
    for &s in samples {
        let clamped = s.max(-0.999999).min(0.999999);
        let v = (clamped * 2147483647.0).round() as i32;
        let b = v.to_le_bytes();
        buf.extend_from_slice(&b);
        buf.extend_from_slice(&b);
    }
    f.write_all(&buf).map_err(|e| e.to_string())?;
    Ok(())
}

fn read_wav_mono(path: &str, channel: &str) -> Result<(u32, Vec<f64>), String> {
    let mut f = File::open(path).map_err(|e| format!("open {}: {}", path, e))?;
    let mut raw = Vec::new();
    f.read_to_end(&mut raw).map_err(|e| e.to_string())?;
    if raw.len() < 44 || &raw[0..4] != b"RIFF" || &raw[8..12] != b"WAVE" {
        return Err("Invalid WAV header".to_string());
    }
    let mut pos = 12usize;
    let mut rate = 48000u32;
    let mut channels = 2u16;
    let mut bits = 32u16;
    let mut data_off = 0usize;
    let mut data_len = 0usize;

    while pos + 8 <= raw.len() {
        let id = &raw[pos..pos + 4];
        let sz = u32::from_le_bytes([raw[pos + 4], raw[pos + 5], raw[pos + 6], raw[pos + 7]]) as usize;
        let body = pos + 8;
        if id == b"fmt " && body + 16 <= raw.len() {
            channels = u16::from_le_bytes([raw[body + 2], raw[body + 3]]);
            rate = u32::from_le_bytes([raw[body + 4], raw[body + 5], raw[body + 6], raw[body + 7]]);
            bits = u16::from_le_bytes([raw[body + 14], raw[body + 15]]);
        } else if id == b"data" {
            data_off = body;
            data_len = sz.min(raw.len().saturating_sub(body));
            break;
        }
        pos = body + sz + (sz & 1);
    }
    if data_len == 0 {
        return Err("No WAV data chunk".to_string());
    }

    let ch_n = channels.max(1) as usize;
    let bytes_per_sample = (bits / 8) as usize;
    let frame_bytes = ch_n * bytes_per_sample;
    let n_frames = data_len / frame_bytes;
    let mut out = Vec::with_capacity(n_frames);

    for i in 0..n_frames {
        let base = data_off + i * frame_bytes;
        let read_ch = |c: usize| -> f64 {
            let off = base + (c.min(ch_n - 1)) * bytes_per_sample;
            if bits == 32 {
                let v = i32::from_le_bytes([raw[off], raw[off + 1], raw[off + 2], raw[off + 3]]);
                (v as f64) / 2147483648.0
            } else if bits == 16 {
                let v = i16::from_le_bytes([raw[off], raw[off + 1]]);
                (v as f64) / 32768.0
            } else {
                0.0
            }
        };
        let val = match channel {
            "L" | "l" | "left" => read_ch(1.min(ch_n - 1)),
            "R" | "r" | "right" => read_ch(0),
            _ => {
                if ch_n >= 2 {
                    0.5 * (read_ch(0) + read_ch(1))
                } else {
                    read_ch(0)
                }
            }
        };
        out.push(val);
    }
    Ok((rate, out))
}

fn iec711_comp_db(freq: f64) -> f64 {
    // Approximate IEC-711 occluded-ear coupler resonance model (8kHz half-wave peak + 3kHz canal)
    let p1 = 2.5 * (-((freq.log2() - 3000.0_f64.log2()) / 0.55).powi(2)).exp();
    let p2 = 6.5 * (-((freq.log2() - 8000.0_f64.log2()) / 0.22).powi(2)).exp();
    -(p1 + p2)
}

fn cmd_gen(args: &[String]) -> Result<(), String> {
    if args.is_empty() {
        return Err("Usage: fr_meas_dsp gen <out_wav> [options]".to_string());
    }
    let out_wav = &args[0];
    let mut rate = 48000usize;
    // Comment (invariant): Sweep 15Hz..22kHz so 15ms cosine fades sit completely outside the 20Hz..20kHz measurement band.
    let mut f0 = 15.0f64;
    let mut f1 = 22000.0f64;
    let mut dur = 3.0f64;
    let mut pre = 0.6f64;
    let mut post = 0.3f64;
    let mut amp = 0.35f64;

    let mut i = 1;
    while i + 1 < args.len() {
        match args[i].as_str() {
            "--rate" => rate = args[i + 1].parse().unwrap_or(rate),
            "--f0" => f0 = args[i + 1].parse().unwrap_or(f0),
            "--f1" => f1 = args[i + 1].parse().unwrap_or(f1),
            "--dur" => dur = args[i + 1].parse().unwrap_or(dur),
            "--pre" => pre = args[i + 1].parse().unwrap_or(pre),
            "--post" => post = args[i + 1].parse().unwrap_or(post),
            "--amp" => amp = args[i + 1].parse().unwrap_or(amp),
            _ => {}
        }
        i += 2;
    }

    let n_pre = (rate as f64 * pre).round() as usize;
    let n_post = (rate as f64 * post).round() as usize;
    let sweep = generate_sweep_samples(rate, f0, f1, dur, amp);
    let mut full = Vec::with_capacity(n_pre + sweep.len() + n_post);
    full.resize(n_pre, 0.0);
    full.extend_from_slice(&sweep);
    full.resize(full.len() + n_post, 0.0);

    write_wav_s32_stereo(out_wav, rate as u32, &full)?;
    println!("{{\"ok\":true,\"samples\":{},\"rate\":{}}}", full.len(), rate);
    Ok(())
}

fn cmd_analyze(args: &[String]) -> Result<(), String> {
    if args.is_empty() {
        return Err("Usage: fr_meas_dsp analyze <rec_wav> [options]".to_string());
    }
    let rec_wav = &args[0];
    let mut channel = "avg".to_string();
    let mut f0 = 15.0f64;
    let mut f1 = 22000.0f64;
    let mut dur = 3.0f64;
    let mut pre = 0.6f64;
    let mut gate_db = -45.0f64;
    let mut smooth_oct = 12.0f64;
    let mut comp = "none".to_string();
    let mut out_txt = String::new();

    let mut i = 1;
    while i + 1 < args.len() {
        match args[i].as_str() {
            "--channel" => channel = args[i + 1].clone(),
            "--f0" => f0 = args[i + 1].parse().unwrap_or(f0),
            "--f1" => f1 = args[i + 1].parse().unwrap_or(f1),
            "--dur" => dur = args[i + 1].parse().unwrap_or(dur),
            "--pre" => pre = args[i + 1].parse().unwrap_or(pre),
            "--gate-db" => gate_db = args[i + 1].parse().unwrap_or(gate_db),
            "--smooth" => smooth_oct = args[i + 1].parse().unwrap_or(smooth_oct),
            "--comp" => comp = args[i + 1].clone(),
            "--out-txt" => out_txt = args[i + 1].clone(),
            _ => {}
        }
        i += 2;
    }

    let (rate_u32, mut rec) = read_wav_mono(rec_wav, &channel)?;
    let rate = rate_u32 as usize;
    if rec.len() < rate {
        return Err("Recording too short".to_string());
    }

    // Remove DC offset
    let mean = rec.iter().sum::<f64>() / rec.len() as f64;
    for x in &mut rec {
        *x -= mean;
    }

    // 1. Quiet-Room Noise Gate on pre-sweep window (0.15s .. pre - 0.05s)
    let g_start = ((rate as f64 * 0.15) as usize).min(rec.len());
    let g_end = ((rate as f64 * (pre - 0.05).max(0.25)) as usize).min(rec.len());
    let (noise_rms_db, noise_var_db) = if g_end > g_start + 4800 {
        let seg = &rec[g_start..g_end];
        let rms = (seg.iter().map(|x| x * x).sum::<f64>() / seg.len() as f64).sqrt();
        let db_rms = if rms > 1e-12 { 20.0 * rms.log10() } else { -120.0 };
        let win = (rate / 10).max(1);
        let mut w_min = 0.0f64;
        let mut w_max = -120.0f64;
        let mut first = true;
        let mut pos = 0;
        while pos + win <= seg.len() {
            let wr = (seg[pos..pos + win].iter().map(|x| x * x).sum::<f64>() / win as f64).sqrt();
            let wd = if wr > 1e-12 { 20.0 * wr.log10() } else { -120.0 };
            if first {
                w_min = wd;
                w_max = wd;
                first = false;
            } else {
                w_min = w_min.min(wd);
                w_max = w_max.max(wd);
            }
            pos += win;
        }
        (db_rms, (w_max - w_min).max(0.0))
    } else {
        (-120.0, 0.0)
    };
    let gate_passed = noise_rms_db <= gate_db && noise_var_db <= 4.5;

    // 2. Farina Inverse Filter FFT Deconvolution
    let sweep = generate_sweep_samples(rate, f0, f1, dur, 1.0);
    let n_sw = sweep.len();
    let ln_ratio = (f1 / f0).ln();
    let mut inv = vec![0.0; n_sw];
    for k in 0..n_sw {
        let t_norm = k as f64 / (n_sw - 1).max(1) as f64;
        inv[k] = sweep[n_sw - 1 - k] * (-t_norm * ln_ratio).exp();
    }

    let n_fft = (rec.len() + n_sw).next_power_of_two();
    let mut r_re = vec![0.0; n_fft];
    let mut r_im = vec![0.0; n_fft];
    r_re[..rec.len()].copy_from_slice(&rec);

    let mut i_re = vec![0.0; n_fft];
    let mut i_im = vec![0.0; n_fft];
    i_re[..n_sw].copy_from_slice(&inv);

    fft_in_place(&mut r_re, &mut r_im, false);
    fft_in_place(&mut i_re, &mut i_im, false);

    for k in 0..n_fft {
        let a = r_re[k];
        let b = r_im[k];
        let c = i_re[k];
        let d = i_im[k];
        r_re[k] = a * c - b * d;
        r_im[k] = a * d + b * c;
    }
    fft_in_place(&mut r_re, &mut r_im, true);

    // 3. Locate primary impulse peak & extract reflection-free windowed IR
    let mut pk_idx = 0usize;
    let mut pk_val = 0.0f64;
    for (idx, &v) in r_re.iter().enumerate() {
        let av = v.abs();
        if av > pk_val {
            pk_val = av;
            pk_idx = idx;
        }
    }

    let pre_w = (rate as f64 * 0.010).round() as usize; // 10 ms pre-peak
    let post_w = (rate as f64 * 0.120).round() as usize; // 120 ms post-peak
    let ir_fft_n = 16384usize;
    let mut ir_re = vec![0.0; ir_fft_n];
    let mut ir_im = vec![0.0; ir_fft_n];

    let start_ir = pk_idx.saturating_sub(pre_w);
    let win_len = (pre_w + post_w).min(ir_fft_n);
    for k in 0..win_len {
        let src = (start_ir + k) % n_fft;
        // Asymmetric raised-cosine taper at edges
        let mut w = 1.0;
        let edge_pre = (pre_w / 2).max(1);
        let edge_post = (post_w / 3).max(1);
        if k < edge_pre {
            w = 0.5 * (1.0 - (PI * k as f64 / edge_pre as f64).cos());
        } else if k + edge_post >= win_len {
            let rem = win_len - 1 - k;
            w = 0.5 * (1.0 - (PI * rem as f64 / edge_post as f64).cos());
        }
        ir_re[k] = r_re[src] * w;
    }

    fft_in_place(&mut ir_re, &mut ir_im, false);

    let half = ir_fft_n / 2;
    let df = rate as f64 / ir_fft_n as f64;
    let mut mag2 = vec![0.0; half + 1];
    let mut phase_deg = vec![0.0; half + 1];
    for k in 0..=half {
        mag2[k] = ir_re[k] * ir_re[k] + ir_im[k] * ir_im[k];
        phase_deg[k] = ir_im[k].atan2(ir_re[k]) * 180.0 / PI;
    }

    // Helper: fractional-octave smoothed dB at frequency `f`
    let sample_db = |f: f64| -> (f64, f64) {
        let factor = 2.0_f64.powf(0.5 / smooth_oct.max(1.0));
        let k_lo = ((f / factor) / df).floor().max(1.0) as usize;
        let k_hi = ((f * factor) / df).ceil().min(half as f64) as usize;
        let mut sum_p = 0.0;
        let mut cnt = 0usize;
        for k in k_lo..=k_hi {
            sum_p += mag2[k];
            cnt += 1;
        }
        let avg_p = if cnt > 0 { sum_p / cnt as f64 } else { 1e-24 };
        let mut db = 10.0 * avg_p.max(1e-24).log10();
        if comp == "iec711" {
            db += iec711_comp_db(f);
        }
        let k_c = (f / df).round().max(1.0).min(half as f64) as usize;
        (db, phase_deg[k_c])
    };

    let (ref_1k_db, _) = sample_db(1000.0);

    // 4. Write REW/Squiglink .txt (240 log-spaced points)
    let eval_f0 = 20.0f64;
    let eval_f1 = 20000.0f64;
    if !out_txt.is_empty() {
        if let Ok(mut f) = File::create(&out_txt) {
            let _ = writeln!(f, "* VipXpert OS IEC-711 FR Measurement (comp={}, smooth=1/{}oct)", comp, smooth_oct as i32);
            let _ = writeln!(f, "* Freq(Hz)\tSPL(dB)\tPhase(deg)");
            let n_pts = 240usize;
            for p in 0..n_pts {
                let frac = p as f64 / (n_pts - 1) as f64;
                let freq = eval_f0 * (eval_f1 / eval_f0).powf(frac);
                let (db, ph) = sample_db(freq);
                let _ = writeln!(f, "{:.2}\t{:.2}\t{:.1}", freq, db - ref_1k_db, ph);
            }
        }
    }

    // 5. Build 120-point OLED array + summary stats
    let mut pts_120 = Vec::with_capacity(120);
    let mut min_db = 999.0f64;
    let mut max_db = -999.0f64;
    for p in 0..120 {
        let frac = p as f64 / 119.0;
        let freq = eval_f0 * (eval_f1 / eval_f0).powf(frac);
        let (db, _) = sample_db(freq);
        let norm = db - ref_1k_db;
        min_db = min_db.min(norm);
        max_db = max_db.max(norm);
        pts_120.push(format!("{:.2}", norm));
    }

    let sig_rms = (rec.iter().map(|x| x * x).sum::<f64>() / rec.len() as f64).sqrt();
    let sig_db = if sig_rms > 1e-12 { 20.0 * sig_rms.log10() } else { -120.0 };
    let snr_db = (sig_db - noise_rms_db).max(0.0);
    let peak_ms = (pk_idx as f64 / rate as f64) * 1000.0;

    println!(
        "{{\"ok\":true,\"gate_passed\":{},\"noise_rms_db\":{:.2},\"noise_var_db\":{:.2},\"snr_db\":{:.2},\"peak_ms\":{:.2},\"min_db\":{:.2},\"max_db\":{:.2},\"points_120\":[{}]}}",
        gate_passed,
        noise_rms_db,
        noise_var_db,
        snr_db,
        peak_ms,
        min_db,
        max_db,
        pts_120.join(",")
    );
    Ok(())
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("Usage: fr_meas_dsp <gen|analyze> ...");
        std::process::exit(1);
    }
    let res = match args[1].as_str() {
        "gen" => cmd_gen(&args[2..]),
        "analyze" => cmd_analyze(&args[2..]),
        _ => Err(format!("Unknown subcommand: {}", args[1])),
    };
    if let Err(e) = res {
        eprintln!("{{\"ok\":false,\"error\":\"{}\"}}", e.replace('"', "'"));
        std::process::exit(1);
    }
}
