// /vipxpert_minimal_os/rust_workers/src/bin/mic_scope_dsp.rs
// Zero-GC ARM64 Rust 48kHz Dual-Mic 36-Bin Goertzel + 17.6..23.6kHz Ultrasonic Heterodyne Scope Worker.
// Reads live control state (listen|carrier_hz|gain_shift) from /tmp/vip_usonic_ctl if present and emits
// deterministic 44-byte binary frames on stdout (<0.3% CPU vs ~35% in Python).
use std::f64::consts::PI;
use std::fs;
use std::io::{Read, Write};
use std::process::{Child, Command, Stdio};

fn parse_ctl() -> (bool, f64, u32) {
    if let Ok(s) = fs::read_to_string("/tmp/vip_usonic_ctl") {
        let parts: Vec<&str> = s.trim().split('|').collect();
        if parts.len() >= 3 {
            let listen = parts[0] == "1";
            let carrier = parts[1].parse::<f64>().unwrap_or(19500.0).clamp(15000.0, 23500.0);
            let shift = parts[2].parse::<u32>().unwrap_or(2).min(6);
            return (listen, carrier, shift);
        }
    }
    (false, 19500.0, 2)
}

fn main() {
    let mut rec_p = match Command::new("parec")
        .args([
            "--device=vip_mic_bus.monitor",
            "--format=s16le",
            "--rate=48000",
            "--channels=2",
            "--latency-msec=40",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(c) => c,
        Err(_) => return,
    };

    let full_freqs: [f64; 18] = [
        250.0, 500.0, 800.0, 1200.0, 1800.0, 2500.0, 3500.0, 4800.0, 6400.0, 8000.0, 10000.0,
        12000.0, 14000.0, 16000.0, 18000.0, 20000.0, 21500.0, 23000.0,
    ];
    let mut full_c = [0.0f64; 18];
    let mut us_c = [0.0f64; 18];
    for i in 0..18 {
        full_c[i] = 2.0 * (2.0 * PI * full_freqs[i] / 48000.0).cos();
        let uf = 17600.0 + (i as f64) * 350.0;
        us_c[i] = 2.0 * (2.0 * PI * uf / 48000.0).cos();
    }

    let mut play_p: Option<Child> = None;
    let mut phase = 0.0f64;
    let mut lp_z1 = 0.0f64;
    let mut lp_z2 = 0.0f64;
    let mut raw = [0u8; 4096];
    let mut left = [0i16; 1024];
    let mut right = [0i16; 1024];
    let mut win = [0.0f64; 256];
    let mut out_pcm = [0u8; 2048];
    let mut stdout = std::io::stdout();

    loop {
        let ok = match rec_p.stdout.as_mut() {
            Some(r) => r.read_exact(&mut raw).is_ok(),
            None => false,
        };
        if !ok {
            break;
        }

        let mut pk_l = 0i32;
        let mut pk_r = 0i32;
        for i in 0..1024 {
            let l = i16::from_le_bytes([raw[i * 4], raw[i * 4 + 1]]);
            let r = i16::from_le_bytes([raw[i * 4 + 2], raw[i * 4 + 3]]);
            left[i] = l;
            right[i] = r;
            if i < 512 {
                let al = (l as i32).abs();
                let ar = (r as i32).abs();
                if al > pk_l {
                    pk_l = al;
                }
                if ar > pk_r {
                    pk_r = ar;
                }
            }
        }
        for i in 0..256 {
            win[i] = ((left[i] as f64) + (right[i] as f64)) * 0.5;
        }

        let mut pkt = [0u8; 44];
        pkt[0] = 0x55;
        pkt[1] = 0x53;
        let vu_l = (((pk_l as f64) / 32768.0 * 2.5).min(1.0) * 255.0).round() as u8;
        let vu_r = (((pk_r as f64) / 32768.0 * 2.5).min(1.0) * 255.0).round() as u8;
        pkt[2] = vu_l;
        pkt[3] = vu_r;

        for (bi, &c) in full_c.iter().enumerate() {
            let (mut s1, mut s2) = (0.0, 0.0);
            for &x in win.iter() {
                let s0 = x + c * s1 - s2;
                s2 = s1;
                s1 = s0;
            }
            let mag = (s1 * s1 + s2 * s2 - c * s1 * s2).max(1.0).sqrt() / (256.0 * 512.0);
            pkt[8 + bi] = (((1.0 + mag * 18.0).log10() * 26.0) as i32).clamp(2, 34) as u8;
        }

        let mut best_idx = 0usize;
        let mut best_mag = 1.0f64;
        for (idx, &c) in us_c.iter().enumerate() {
            let (mut s1, mut s2) = (0.0, 0.0);
            for &x in win.iter() {
                let s0 = x + c * s1 - s2;
                s2 = s1;
                s1 = s0;
            }
            let mag = (s1 * s1 + s2 * s2 - c * s1 * s2).max(1.0).sqrt();
            if mag > best_mag {
                best_mag = mag;
                best_idx = idx;
            }
            let norm_u = mag / (256.0 * 256.0);
            pkt[26 + idx] = (((1.0 + norm_u * 28.0).log10() * 28.0) as i32).clamp(2, 34) as u8;
        }

        let pk_db = (20.0 * (best_mag / (256.0 * 16384.0)).max(1e-5).log10()).clamp(-90.0, 0.0);
        let pk_db_x10 = (pk_db * 10.0).round() as i16;
        pkt[4] = best_idx as u8;
        pkt[5] = if pk_db > -48.0 { 1 } else { 0 };
        let db_le = pk_db_x10.to_le_bytes();
        pkt[6] = db_le[0];
        pkt[7] = db_le[1];

        let (listen, carrier_hz, gain_shift) = parse_ctl();
        if listen {
            let need_spawn = match play_p.as_mut() {
                Some(c) => c.try_wait().ok().flatten().is_some(),
                None => true,
            };
            if need_spawn {
                play_p = Command::new("pacat")
                    .args([
                        "--playback",
                        "--format=s16le",
                        "--rate=48000",
                        "--channels=1",
                        "--latency-msec=50",
                    ])
                    .stdin(Stdio::piped())
                    .stderr(Stdio::null())
                    .spawn()
                    .ok();
            }
            let w_step = 2.0 * PI * carrier_hz / 48000.0;
            let gain = (1u32 << gain_shift) as f64;
            let mut prev_x = 0.0f64;
            for i in 0..1024 {
                let cur_x = ((left[i] as f64) + (right[i] as f64)) * 0.5;
                let hp = cur_x - prev_x;
                prev_x = cur_x;
                phase = (phase + w_step) % (2.0 * PI);
                let mixed = hp * phase.cos() * gain;
                lp_z1 += 0.32 * (mixed - lp_z1);
                lp_z2 += 0.32 * (lp_z1 - lp_z2);
                let s = (lp_z2 as i32).clamp(-32000, 32000) as i16;
                let sb = s.to_le_bytes();
                out_pcm[i * 2] = sb[0];
                out_pcm[i * 2 + 1] = sb[1];
            }
            if let Some(ref mut p) = play_p {
                if let Some(ref mut sin) = p.stdin {
                    let _ = sin.write_all(&out_pcm);
                    let _ = sin.flush();
                }
            }
        } else if let Some(mut p) = play_p.take() {
            let _ = p.kill();
            let _ = p.wait();
        }

        if stdout.write_all(&pkt).is_err() || stdout.flush().is_err() {
            break;
        }
    }

    if let Some(mut p) = play_p {
        let _ = p.kill();
        let _ = p.wait();
    }
    let _ = rec_p.kill();
    let _ = rec_p.wait();
}
