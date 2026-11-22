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
fn spawn_parec(target: &str, is_stream: bool) -> Option<Child> {
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
    let child = Command::new("parec")
        .args([
            "--format=s16le",
            "--rate=48000",
            "--channels=1",
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
fn goertzel_quad(w: &[f64; 128], c: &[f64; 4]) -> f64 {
    let (mut s1a, mut s2a) = (0.0, 0.0);
    let (mut s1b, mut s2b) = (0.0, 0.0);
    let (mut s1c, mut s2c) = (0.0, 0.0);
    let (mut s1d, mut s2d) = (0.0, 0.0);
    let (c0, c1, c2, c3) = (c[0], c[1], c[2], c[3]);
    for &x in w.iter() {
        let s0a = x + c0 * s1a - s2a;
        s2a = s1a;
        s1a = s0a;
        let s0b = x + c1 * s1b - s2b;
        s2b = s1b;
        s1b = s0b;
        let s0c = x + c2 * s1c - s2c;
        s2c = s1c;
        s1c = s0c;
        let s0d = x + c3 * s1d - s2d;
        s2d = s1d;
        s1d = s0d;
    }
    let pa = (s1a * s1a + s2a * s2a - c0 * s1a * s2a).max(0.0);
    let pb = (s1b * s1b + s2b * s2b - c1 * s1b * s2b).max(0.0);
    let pc = (s1c * s1c + s2c * s2c - c2 * s1c * s2c).max(0.0);
    let pd = (s1d * s1d + s2d * s2d - c3 * s1d * s2d).max(0.0);
    let pmax = pa.max(pb).max(pc).max(pd);
    let pavg = 0.25 * (pa + pb + pc + pd);
    (pmax * 0.75 + pavg * 0.25).sqrt() * 0.015625
}

#[inline]
fn shift_ring(ring: &mut [u8; 2048], chunk: &[u8], n: usize) {
    let nc = n & !1usize;
    if nc >= 2048 {
        ring.copy_from_slice(&chunk[nc - 2048..nc]);
    } else if nc > 0 {
        ring.copy_within(nc..2048, 0);
        ring[2048 - nc..2048].copy_from_slice(&chunk[..nc]);
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

    // Comment (invariant): Contiguous 4-tap log-band Goertzel filterbank covering 40Hz..20.4kHz with inverse-sinc CIC droop compensation so high-frequency 17..20kHz air bands never notch out.
    let centers: [f64; 20] = [
        45.0, 65.0, 92.0, 130.0, 185.0, 260.0, 365.0, 510.0, 700.0, 960.0, 1320.0, 1800.0,
        2450.0, 3350.0, 4550.0, 6200.0, 8400.0, 11400.0, 15200.0, 19500.0,
    ];
    let mut strides = [1usize; 20];
    let mut c_tap = [[0.0f64; 4]; 20];
    let mut weights = [1.0f64; 20];
    let mut hann128 = [0.0f64; 128];
    for (i, h) in hann128.iter_mut().enumerate() {
        *h = 0.5 * (1.0 - (2.0 * PI * (i as f64) / 127.0).cos());
    }

    // Comment (invariant): Use 4-tier Constant-Q decimation (stride 8 @6kHz for 45..260Hz, stride 4 @12kHz for 365..1320Hz, stride 2 @24kHz for 1800..6200Hz, stride 1 @48kHz for 8400..19500Hz); stride=2 at 365Hz had a 375Hz Hann main-lobe half-width that bled 260Hz into Band 6 (CDHR-540).
    let is_pink = curve_mode == "pink";
    for idx in 0..20 {
        let fc = centers[idx];
        let (stride, fs_eff) = if idx < 6 {
            (8usize, 6000.0)
        } else if idx < 11 {
            (4usize, 12000.0)
        } else if idx < 16 {
            (2usize, 24000.0)
        } else {
            (1usize, 48000.0)
        };
        strides[idx] = stride;
        let f_prev = if idx > 0 { centers[idx - 1] } else { fc / 1.38 };
        let f_next = if idx + 1 < 20 { centers[idx + 1] } else { 21400.0 };
        let mut f_lo = (fc * (f_prev / fc).powf(0.38)).max(25.0);
        let mut f_hi = (fc * (f_next / fc).powf(0.38)).min(fs_eff * 0.435);
        if idx == 19 {
            f_lo = 17100.0;
            f_hi = 20400.0;
        }
        let ratio = if f_hi > f_lo { f_hi / f_lo } else { 1.05 };
        for k in 0..4 {
            let fk = f_lo * ratio.powf((k as f64) / 3.0);
            c_tap[idx][k] = 2.0 * (2.0 * PI * fk / fs_eff).cos();
        }
        let xs = PI * fc / fs_eff;
        let sinc_comp = if stride > 1 && xs > 0.01 {
            xs / xs.sin()
        } else {
            1.0
        };
        let hf_boost = if idx >= 18 {
            1.35
        } else if idx == 17 {
            1.15
        } else {
            1.0
        };
        let curve_w = if is_pink { (fc / 60.0).powf(0.58) } else { 1.0 };
        weights[idx] = curve_w * sinc_comp * hf_boost;
    }

    let mut using_stream_idx = false;
    let mut main_child: Option<Child>;
    let mut aux_child: Option<Child> = None;

    if viz_mode == "mic_only" {
        main_child = spawn_parec("vip_mic_bus.monitor", false);
    } else if viz_mode == "music_only" {
        if !stream_idx.is_empty() {
            main_child = spawn_parec(stream_idx, true);
            using_stream_idx = true;
        } else {
            main_child = spawn_parec("@DEFAULT_MONITOR@", false);
        }
    } else {
        main_child = spawn_parec("@DEFAULT_MONITOR@", false);
        if amb_on == "0" {
            aux_child = spawn_parec("vip_mic_bus.monitor", false);
        }
    }

    if main_child.is_none() {
        return;
    }

    let mut ring_main = [0u8; 2048];
    let mut ring_aux = [0u8; 2048];
    let mut chunk = [0u8; 2048];
    let mut out_bytes = [12u8; 20];
    let is_music_only = viz_mode == "music_only";
    let mut agc_peak = if is_music_only { 6.0 } else { 180.0 };
    let floor_pk = if is_music_only { 1.8 } else { 18.0 };
    let gate_v = if is_music_only { 0.12 } else { 2.2 };
    let mut smooth = [0.05f64; 20];
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
            while bytes_since_calc < 1856 {
                let read_res = main_child
                    .as_mut()
                    .and_then(|c| c.stdout.as_mut())
                    .map(|s| s.read(&mut chunk));
                match read_res {
                    Some(Ok(0)) => {
                        if using_stream_idx {
                            if let Some(ref mut old) = main_child {
                                let _ = old.kill();
                                let _ = old.wait();
                            }
                            main_child = spawn_parec("@DEFAULT_MONITOR@", false);
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
                        bytes_since_calc += n;
                        shift_ring(&mut ring_main, &chunk, n);
                    }
                    Some(Err(ref e)) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                    _ => {
                        kill_children(&mut main_child, &mut aux_child);
                        return;
                    }
                }
            }
        }

        if fd_aux >= 0 && pr > 0 && (pfds[1].revents & POLLIN) != 0 {
            while let Some(Ok(n)) = aux_child
                .as_mut()
                .and_then(|c| c.stdout.as_mut())
                .map(|s| s.read(&mut chunk))
            {
                if n == 0 {
                    break;
                }
                if n > bytes_since_calc {
                    bytes_since_calc = n;
                }
                shift_ring(&mut ring_aux, &chunk, n);
            }
        }

        if bytes_since_calc < 1856 {
            continue;
        }
        bytes_since_calc = 0;

        let mut pcm = [0.0f64; 1024];
        if fd_aux >= 0 {
            for i in 0..1024 {
                let am = i16::from_le_bytes([ring_main[i * 2], ring_main[i * 2 + 1]]) as f64;
                let aa = i16::from_le_bytes([ring_aux[i * 2], ring_aux[i * 2 + 1]]) as f64;
                pcm[i] = am + aa * 1.15;
            }
        } else {
            for i in 0..1024 {
                pcm[i] = i16::from_le_bytes([ring_main[i * 2], ring_main[i * 2 + 1]]) as f64;
            }
        }

        let mut s8 = [0.0f64; 128];
        let mut s4e = [0.0f64; 128];
        let mut s4l = [0.0f64; 128];
        let mut s2e = [0.0f64; 128];
        let mut s2l = [0.0f64; 128];
        let mut w8 = [0.0f64; 128];
        let mut w4e = [0.0f64; 128];
        let mut w4l = [0.0f64; 128];
        let mut w2e = [0.0f64; 128];
        let mut w2l = [0.0f64; 128];
        let mut w1e = [0.0f64; 128];
        let mut w1l = [0.0f64; 128];
        let (mut m8, mut m4e, mut m4l, mut m2e, mut m2l, mut m1e, mut m1l) =
            (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0);

        for i in 0..128 {
            let mut sum8 = 0.0;
            for k in 0..8 {
                sum8 += pcm[i * 8 + k];
            }
            s8[i] = sum8 * 0.125;
            m8 += s8[i];
            s4e[i] = (pcm[i * 4] + pcm[i * 4 + 1] + pcm[i * 4 + 2] + pcm[i * 4 + 3]) * 0.25;
            m4e += s4e[i];
            let o4 = 512 + i * 4;
            s4l[i] = (pcm[o4] + pcm[o4 + 1] + pcm[o4 + 2] + pcm[o4 + 3]) * 0.25;
            m4l += s4l[i];
            s2e[i] = (pcm[256 + i * 2] + pcm[257 + i * 2]) * 0.5;
            m2e += s2e[i];
            s2l[i] = (pcm[768 + i * 2] + pcm[769 + i * 2]) * 0.5;
            m2l += s2l[i];
            m1e += pcm[384 + i];
            m1l += pcm[896 + i];
        }
        m8 *= 0.0078125;
        m4e *= 0.0078125;
        m4l *= 0.0078125;
        m2e *= 0.0078125;
        m2l *= 0.0078125;
        m1e *= 0.0078125;
        m1l *= 0.0078125;

        for i in 0..128 {
            w8[i] = (s8[i] - m8) * hann128[i];
            w4e[i] = (s4e[i] - m4e) * hann128[i];
            w4l[i] = (s4l[i] - m4l) * hann128[i];
            w2e[i] = (s2e[i] - m2e) * hann128[i];
            w2l[i] = (s2l[i] - m2l) * hann128[i];
            w1e[i] = (pcm[384 + i] - m1e) * hann128[i];
            w1l[i] = (pcm[896 + i] - m1l) * hann128[i];
        }

        let mut mags = [0.0f64; 20];
        let mut frame_max = 1.0f64;
        for idx in 0..20 {
            let raw_mag = if strides[idx] == 8 {
                goertzel_quad(&w8, &c_tap[idx])
            } else if strides[idx] == 4 {
                goertzel_quad(&w4e, &c_tap[idx]).max(goertzel_quad(&w4l, &c_tap[idx]))
            } else if strides[idx] == 2 {
                goertzel_quad(&w2e, &c_tap[idx]).max(goertzel_quad(&w2l, &c_tap[idx]))
            } else {
                goertzel_quad(&w1e, &c_tap[idx]).max(goertzel_quad(&w1l, &c_tap[idx]))
            };
            let val = raw_mag * weights[idx];
            mags[idx] = val;
            if val > frame_max {
                frame_max = val;
            }
        }

        if frame_max > agc_peak {
            agc_peak = agc_peak * 0.35 + frame_max * 0.65;
        } else {
            agc_peak = (agc_peak * 0.96 + frame_max * 0.04).max(floor_pk);
        }

        for i in 0..20 {
            let norm = if mags[i] < gate_v {
                0.05
            } else {
                let ratio = (mags[i] / agc_peak).max(1e-4);
                let db_rel = 20.0 * ratio.log10();
                let lin = ((db_rel + 22.0) / 22.0).clamp(0.0, 1.0);
                lin.powf(1.35).clamp(0.05, 1.0)
            };
            let prev_b = smooth[i];
            let cur_b = if norm > prev_b {
                prev_b * 0.20 + norm * 0.80
            } else {
                prev_b * 0.54 + norm * 0.46
            }
            .clamp(0.05, 1.0);
            smooth[i] = cur_b;
            out_bytes[i] = ((cur_b * 255.0).round() as i32).clamp(12, 255) as u8;
        }

        if stdout.write_all(&out_bytes).is_err() || stdout.flush().is_err() {
            break;
        }
    }
    kill_children(&mut main_child, &mut aux_child);
}
