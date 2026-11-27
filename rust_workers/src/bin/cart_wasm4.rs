// /vipxpert_minimal_os/rust_workers/src/bin/cart_wasm4.rs
// Sandboxed WASM-4 runtime adapter communicating over Vipxpert Zero OS SHM+IPC ABI (8256 bytes).
// Usage: cart_wasm4 <cart.wasm> <shm_path>

use std::env;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use wasmi::{Caller, Engine, Linker, Memory, MemoryType, Module, Store};

const SHM_HEADER_SIZE: usize = 64;
const OLED_W: usize = 128;
const OLED_H: usize = 64;
const SHM_TOTAL_SIZE: usize = SHM_HEADER_SIZE + OLED_W * OLED_H;

const W4_W: i32 = 160;
const W4_H: i32 = 160;
const ADDR_PALETTE: usize = 0x04;
const ADDR_DRAW_COLORS: usize = 0x14;
const ADDR_GAMEPAD1: usize = 0x16;
const ADDR_SYSTEM_FLAGS: usize = 0x1F;
const ADDR_FRAMEBUFFER: usize = 0xA0;
const FB_BYTES: usize = 6400; // 160 * 160 / 4

fn glyph_row(ch: u8, row: usize) -> u8 {
    if row >= 8 {
        return 0;
    }
    let c = if ch >= b'a' && ch <= b'z' { ch - 32 } else { ch };
    let rows: [u8; 8] = match c {
        b' ' => [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
        b'!' => [0x18, 0x18, 0x18, 0x18, 0x18, 0x00, 0x18, 0x00],
        b'"' => [0x66, 0x66, 0x24, 0x00, 0x00, 0x00, 0x00, 0x00],
        b'#' => [0x24, 0x7E, 0x24, 0x24, 0x7E, 0x24, 0x00, 0x00],
        b'$' => [0x18, 0x3E, 0x60, 0x3C, 0x06, 0x7C, 0x18, 0x00],
        b'%' => [0x62, 0x64, 0x08, 0x10, 0x26, 0x46, 0x00, 0x00],
        b'+' => [0x00, 0x18, 0x18, 0x7E, 0x18, 0x18, 0x00, 0x00],
        b',' => [0x00, 0x00, 0x00, 0x00, 0x00, 0x18, 0x18, 0x30],
        b'-' => [0x00, 0x00, 0x00, 0x7E, 0x00, 0x00, 0x00, 0x00],
        b'.' => [0x00, 0x00, 0x00, 0x00, 0x00, 0x18, 0x18, 0x00],
        b'/' => [0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x00, 0x00],
        b':' => [0x00, 0x18, 0x18, 0x00, 0x18, 0x18, 0x00, 0x00],
        b'<' => [0x06, 0x18, 0x60, 0x18, 0x06, 0x00, 0x00, 0x00],
        b'=' => [0x00, 0x00, 0x7E, 0x00, 0x7E, 0x00, 0x00, 0x00],
        b'>' => [0x60, 0x18, 0x06, 0x18, 0x60, 0x00, 0x00, 0x00],
        b'?' => [0x3C, 0x66, 0x06, 0x1C, 0x18, 0x00, 0x18, 0x00],
        b'0' => [0x3C, 0x66, 0x6E, 0x76, 0x66, 0x66, 0x3C, 0x00],
        b'1' => [0x18, 0x38, 0x18, 0x18, 0x18, 0x18, 0x7E, 0x00],
        b'2' => [0x3C, 0x66, 0x06, 0x1C, 0x30, 0x60, 0x7E, 0x00],
        b'3' => [0x3C, 0x66, 0x06, 0x1C, 0x06, 0x66, 0x3C, 0x00],
        b'4' => [0x0C, 0x1C, 0x3C, 0x6C, 0x7E, 0x0C, 0x0C, 0x00],
        b'5' => [0x7E, 0x60, 0x7C, 0x06, 0x06, 0x66, 0x3C, 0x00],
        b'6' => [0x3C, 0x60, 0x7C, 0x66, 0x66, 0x66, 0x3C, 0x00],
        b'7' => [0x7E, 0x06, 0x0C, 0x18, 0x30, 0x30, 0x30, 0x00],
        b'8' => [0x3C, 0x66, 0x66, 0x3C, 0x66, 0x66, 0x3C, 0x00],
        b'9' => [0x3C, 0x66, 0x66, 0x3E, 0x06, 0x0C, 0x38, 0x00],
        b'A' => [0x18, 0x3C, 0x66, 0x66, 0x7E, 0x66, 0x66, 0x00],
        b'B' => [0x7C, 0x66, 0x66, 0x7C, 0x66, 0x66, 0x7C, 0x00],
        b'C' => [0x3C, 0x66, 0x60, 0x60, 0x60, 0x66, 0x3C, 0x00],
        b'D' => [0x78, 0x6C, 0x66, 0x66, 0x66, 0x6C, 0x78, 0x00],
        b'E' => [0x7E, 0x60, 0x60, 0x78, 0x60, 0x60, 0x7E, 0x00],
        b'F' => [0x7E, 0x60, 0x60, 0x78, 0x60, 0x60, 0x60, 0x00],
        b'G' => [0x3C, 0x66, 0x60, 0x6E, 0x66, 0x66, 0x3E, 0x00],
        b'H' => [0x66, 0x66, 0x66, 0x7E, 0x66, 0x66, 0x66, 0x00],
        b'I' => [0x3C, 0x18, 0x18, 0x18, 0x18, 0x18, 0x3C, 0x00],
        b'J' => [0x1E, 0x0C, 0x0C, 0x0C, 0x6C, 0x6C, 0x38, 0x00],
        b'K' => [0x66, 0x6C, 0x78, 0x70, 0x78, 0x6C, 0x66, 0x00],
        b'L' => [0x60, 0x60, 0x60, 0x60, 0x60, 0x60, 0x7E, 0x00],
        b'M' => [0x63, 0x77, 0x7F, 0x6B, 0x63, 0x63, 0x63, 0x00],
        b'N' => [0x66, 0x76, 0x7E, 0x7E, 0x6E, 0x66, 0x66, 0x00],
        b'O' => [0x3C, 0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x00],
        b'P' => [0x7C, 0x66, 0x66, 0x7C, 0x60, 0x60, 0x60, 0x00],
        b'Q' => [0x3C, 0x66, 0x66, 0x66, 0x6A, 0x6C, 0x36, 0x00],
        b'R' => [0x7C, 0x66, 0x66, 0x7C, 0x6C, 0x66, 0x66, 0x00],
        b'S' => [0x3C, 0x66, 0x60, 0x3C, 0x06, 0x66, 0x3C, 0x00],
        b'T' => [0x7E, 0x18, 0x18, 0x18, 0x18, 0x18, 0x18, 0x00],
        b'U' => [0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x00],
        b'V' => [0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x18, 0x00],
        b'W' => [0x63, 0x63, 0x63, 0x6B, 0x7F, 0x77, 0x63, 0x00],
        b'X' => [0x66, 0x66, 0x3C, 0x18, 0x3C, 0x66, 0x66, 0x00],
        b'Y' => [0x66, 0x66, 0x66, 0x3C, 0x18, 0x18, 0x18, 0x00],
        b'Z' => [0x7E, 0x06, 0x0C, 0x18, 0x30, 0x60, 0x7E, 0x00],
        b'[' => [0x3C, 0x30, 0x30, 0x30, 0x30, 0x30, 0x3C, 0x00],
        b']' => [0x3C, 0x0C, 0x0C, 0x0C, 0x0C, 0x0C, 0x3C, 0x00],
        b'_' => [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x7E, 0x00],
        _ => [0x00, 0x3C, 0x42, 0x5A, 0x42, 0x3C, 0x00, 0x00],
    };
    rows[row]
}

#[derive(Clone, Default)]
struct ApuChannel {
    active: bool,
    f1: f32,
    f2: f32,
    phase: f32,
    t_samp: u32,
    attack: u32,
    decay: u32,
    sustain: u32,
    release: u32,
    peak_vol: f32,
    sustain_vol: f32,
    duty: f32,
    pan_l: f32,
    pan_r: f32,
    lfsr: u16,
}

impl ApuChannel {
    fn trigger(&mut self, frequency: u32, duration: u32, volume: u32, flags: u32) {
        let f1 = (frequency & 0xFFFF) as f32;
        let f2_raw = ((frequency >> 16) & 0xFFFF) as f32;
        self.f1 = f1.max(20.0);
        self.f2 = if f2_raw > 0.0 { f2_raw.max(20.0) } else { self.f1 };
        self.sustain = (duration & 0xFF) * 800;
        self.release = ((duration >> 8) & 0xFF) * 800;
        self.decay = ((duration >> 16) & 0xFF) * 800;
        self.attack = ((duration >> 24) & 0xFF) * 800;
        let sus_v = (volume & 0xFF).min(100) as f32 / 100.0;
        let pk_v_raw = (volume >> 8) & 0xFF;
        let pk_v = if pk_v_raw > 0 { pk_v_raw.min(100) as f32 / 100.0 } else { sus_v };
        self.sustain_vol = sus_v;
        self.peak_vol = pk_v;
        let mode = (flags >> 2) & 0x3;
        self.duty = match mode {
            0 => 0.125,
            1 => 0.25,
            2 => 0.5,
            _ => 0.75,
        };
        let pan = (flags >> 4) & 0x3;
        self.pan_l = if pan == 2 { 0.0 } else { 1.0 };
        self.pan_r = if pan == 1 { 0.0 } else { 1.0 };
        self.t_samp = 0;
        if self.lfsr == 0 {
            self.lfsr = 0xACE1;
        }
        self.active = (self.attack + self.decay + self.sustain + self.release) > 0;
    }

    fn next_sample(&mut self, ch_idx: usize) -> (f32, f32) {
        if !self.active {
            return (0.0, 0.0);
        }
        let total = self.attack + self.decay + self.sustain + self.release;
        if self.t_samp >= total {
            self.active = false;
            return (0.0, 0.0);
        }
        let env = if self.t_samp < self.attack && self.attack > 0 {
            self.peak_vol * (self.t_samp as f32 / self.attack as f32)
        } else if self.t_samp < self.attack + self.decay && self.decay > 0 {
            let u = (self.t_samp - self.attack) as f32 / self.decay as f32;
            self.peak_vol + (self.sustain_vol - self.peak_vol) * u
        } else if self.t_samp < self.attack + self.decay + self.sustain {
            self.sustain_vol
        } else if self.release > 0 {
            let u = (self.t_samp - self.attack - self.decay - self.sustain) as f32 / self.release as f32;
            self.sustain_vol * (1.0 - u)
        } else {
            0.0
        };

        let prog = self.t_samp as f32 / total.max(1) as f32;
        let freq = self.f1 + (self.f2 - self.f1) * prog;
        let prev_phase = self.phase;
        self.phase = (self.phase + freq / 48000.0) % 1.0;
        let raw = match ch_idx {
            0 | 1 => if self.phase < self.duty { 1.0 } else { -1.0 },
            2 => 1.0 - 4.0 * (self.phase - 0.5).abs(),
            _ => {
                if self.phase < prev_phase {
                    let bit = ((self.lfsr >> 0) ^ (self.lfsr >> 1)) & 1;
                    self.lfsr = (self.lfsr >> 1) | (bit << 14);
                }
                if (self.lfsr & 1) != 0 { 1.0 } else { -1.0 }
            }
        };
        self.t_samp += 1;
        let s = raw * env * 0.22;
        (s * self.pan_l, s * self.pan_r)
    }
}

struct ApuShared {
    ch: [ApuChannel; 4],
    any_triggered: bool,
}

fn spawn_apu_thread(apu: Arc<Mutex<ApuShared>>, muted: Arc<AtomicBool>, quit: Arc<AtomicBool>) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut pw_child: Option<Child> = None;
        let mut buf = [0u8; 800 * 4];
        let frame_dur = Duration::from_micros(16_667);
        while !quit.load(Ordering::Relaxed) {
            let t0 = Instant::now();
            let is_muted = muted.load(Ordering::Relaxed);
            let mut active = false;
            {
                let mut guard = apu.lock().unwrap();
                if guard.any_triggered {
                    for i in 0..800 {
                        let mut l = 0.0f32;
                        let mut r = 0.0f32;
                        for ci in 0..4 {
                            if guard.ch[ci].active {
                                active = true;
                                let (cl, cr) = guard.ch[ci].next_sample(ci);
                                l += cl;
                                r += cr;
                            }
                        }
                        if is_muted {
                            l = 0.0;
                            r = 0.0;
                        }
                        let ls = (l.clamp(-1.0, 1.0) * 28000.0) as i16;
                        let rs = (r.clamp(-1.0, 1.0) * 28000.0) as i16;
                        let off = i * 4;
                        buf[off..off + 2].copy_from_slice(&ls.to_le_bytes());
                        buf[off + 2..off + 4].copy_from_slice(&rs.to_le_bytes());
                    }
                }
            }
            if active && !is_muted {
                if pw_child.is_none() {
                    if let Ok(c) = Command::new("pw-cat")
                        .args([
                            "--playback", "--raw", "--rate", "48000", "--channels", "2",
                            "--format", "s16", "--latency", "30ms", "--media-role", "Game",
                        ])
                        .stdin(Stdio::piped())
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .spawn()
                    {
                        pw_child = Some(c);
                    }
                }
                if let Some(ref mut c) = pw_child {
                    if let Some(ref mut stdin) = c.stdin {
                        if stdin.write_all(&buf).is_err() {
                            let _ = c.kill();
                            let _ = c.wait();
                            pw_child = None;
                        }
                    }
                }
            } else if is_muted {
                if let Some(mut c) = pw_child.take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
            }
            let elapsed = t0.elapsed();
            if elapsed < frame_dur {
                thread::sleep(frame_dur - elapsed);
            }
        }
        if let Some(mut c) = pw_child.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
    })
}

struct W4State {
    memory: Memory,
    sav_path: PathBuf,
    apu: Arc<Mutex<ApuShared>>,
}

#[inline]
fn set_pixel(mem: &mut [u8], x: i32, y: i32, color_idx: u8) {
    if x < 0 || x >= W4_W || y < 0 || y >= W4_H || color_idx == 0 {
        return;
    }
    let idx = (y as usize) * (W4_W as usize) + (x as usize);
    let byte_off = ADDR_FRAMEBUFFER + (idx >> 2);
    let shift = (idx & 3) * 2;
    let c = (color_idx - 1) & 0x3;
    mem[byte_off] = (mem[byte_off] & !(0x3 << shift)) | (c << shift);
}

#[inline]
fn draw_colors(mem: &[u8]) -> u16 {
    u16::from_le_bytes([mem[ADDR_DRAW_COLORS], mem[ADDR_DRAW_COLORS + 1]])
}

fn w4_hline(mem: &mut [u8], x: i32, y: i32, len: u32, col: u8) {
    if y < 0 || y >= W4_H || col == 0 || len == 0 {
        return;
    }
    let x0 = x.max(0);
    let x1 = (x + len as i32).min(W4_W);
    for px in x0..x1 {
        set_pixel(mem, px, y, col);
    }
}

fn w4_vline(mem: &mut [u8], x: i32, y: i32, len: u32, col: u8) {
    if x < 0 || x >= W4_W || col == 0 || len == 0 {
        return;
    }
    let y0 = y.max(0);
    let y1 = (y + len as i32).min(W4_H);
    for py in y0..y1 {
        set_pixel(mem, x, py, col);
    }
}

fn w4_line(mem: &mut [u8], mut x0: i32, mut y0: i32, x1: i32, y1: i32, col: u8) {
    if col == 0 {
        return;
    }
    let dx = (x1 - x0).abs();
    let sx = if x0 < x1 { 1 } else { -1 };
    let dy = -(y1 - y0).abs();
    let sy = if y0 < y1 { 1 } else { -1 };
    let mut err = dx + dy;
    loop {
        set_pixel(mem, x0, y0, col);
        if x0 == x1 && y0 == y1 {
            break;
        }
        let e2 = 2 * err;
        if e2 >= dy {
            err += dy;
            x0 += sx;
        }
        if e2 <= dx {
            err += dx;
            y0 += sy;
        }
    }
}

fn w4_rect(mem: &mut [u8], x: i32, y: i32, w: u32, h: u32) {
    let dc = draw_colors(mem);
    let fill = (dc & 0xF) as u8;
    let stroke = ((dc >> 4) & 0xF) as u8;
    if fill != 0 {
        for dy in 0..h as i32 {
            w4_hline(mem, x, y + dy, w, fill);
        }
    }
    if stroke != 0 && w > 0 && h > 0 {
        w4_hline(mem, x, y, w, stroke);
        w4_hline(mem, x, y + h as i32 - 1, w, stroke);
        w4_vline(mem, x, y, h, stroke);
        w4_vline(mem, x + w as i32 - 1, y, h, stroke);
    }
}

fn w4_oval(mem: &mut [u8], x: i32, y: i32, w: u32, h: u32) {
    if w == 0 || h == 0 {
        return;
    }
    let dc = draw_colors(mem);
    let fill = (dc & 0xF) as u8;
    let stroke = ((dc >> 4) & 0xF) as u8;
    let rx = w as f32 * 0.5;
    let ry = h as f32 * 0.5;
    let cx = x as f32 + rx;
    let cy = y as f32 + ry;
    let y0 = y.max(0);
    let y1 = (y + h as i32).min(W4_H);
    let x0 = x.max(0);
    let x1 = (x + w as i32).min(W4_W);
    for py in y0..y1 {
        let ny = (py as f32 + 0.5 - cy) / ry;
        for px in x0..x1 {
            let nx = (px as f32 + 0.5 - cx) / rx;
            let d = nx * nx + ny * ny;
            if d <= 1.0 {
                let is_edge = stroke != 0 && {
                    let nx_in = ((px as f32 + 0.5 - cx).abs() + 1.0) / rx;
                    let ny_in = ((py as f32 + 0.5 - cy).abs() + 1.0) / ry;
                    nx_in * nx_in + ny * ny > 1.0 || nx * nx + ny_in * ny_in > 1.0
                };
                if is_edge {
                    set_pixel(mem, px, py, stroke);
                } else if fill != 0 {
                    set_pixel(mem, px, py, fill);
                }
            }
        }
    }
}

fn w4_blit_sub(
    mem: &mut [u8],
    sprite_ptr: u32,
    dst_x: i32,
    dst_y: i32,
    w: u32,
    h: u32,
    src_x: u32,
    src_y: u32,
    stride: u32,
    flags: u32,
) {
    let bpp2 = (flags & 1) != 0;
    let flip_x = (flags & 2) != 0;
    let flip_y = (flags & 4) != 0;
    let rotate = (flags & 8) != 0;
    let dc = draw_colors(mem);
    let sp = sprite_ptr as usize;
    let mem_len = mem.len();
    for dy in 0..h {
        for dx in 0..w {
            let sx = src_x + if flip_x { w - 1 - dx } else { dx };
            let sy = src_y + if flip_y { h - 1 - dy } else { dy };
            let bit_idx = (sy as usize) * (stride as usize) + (sx as usize);
            let px_val = if bpp2 {
                let off = sp + (bit_idx >> 2);
                if off >= mem_len {
                    continue;
                }
                (mem[off] >> (6 - (bit_idx & 3) * 2)) & 0x3
            } else {
                let off = sp + (bit_idx >> 3);
                if off >= mem_len {
                    continue;
                }
                (mem[off] >> (7 - (bit_idx & 7))) & 0x1
            };
            let col = ((dc >> (px_val * 4)) & 0xF) as u8;
            if col != 0 {
                let (ox, oy) = if rotate {
                    (dst_x + dy as i32, dst_y + (w - 1 - dx) as i32)
                } else {
                    (dst_x + dx as i32, dst_y + dy as i32)
                };
                set_pixel(mem, ox, oy, col);
            }
        }
    }
}

fn w4_text_bytes(mem: &mut [u8], bytes: &[u8], x: i32, y: i32) {
    let dc = draw_colors(mem);
    let fg = (dc & 0xF) as u8;
    let bg = ((dc >> 4) & 0xF) as u8;
    let mut cx = x;
    let mut cy = y;
    for &b in bytes {
        if b == b'\n' {
            cx = x;
            cy += 8;
            continue;
        }
        for row in 0..8 {
            let bits = glyph_row(b, row);
            for col in 0..8 {
                let on = (bits & (0x80 >> col)) != 0;
                let c = if on { fg } else { bg };
                if c != 0 {
                    set_pixel(mem, cx + col, cy + row as i32, c);
                }
            }
        }
        cx += 8;
    }
}

fn build_palette_gray_lut(mem: &[u8]) -> [u8; 4] {
    let mut lums = [(0u32, 0usize); 4];
    for i in 0..4 {
        let off = ADDR_PALETTE + i * 4;
        let rgb = u32::from_le_bytes([mem[off], mem[off + 1], mem[off + 2], mem[off + 3]]);
        let r = (rgb >> 16) & 0xFF;
        let g = (rgb >> 8) & 0xFF;
        let b = rgb & 0xFF;
        let lum = (r * 77 + g * 150 + b * 29) >> 8;
        lums[i] = (lum, i);
    }
    lums.sort_by_key(|k| k.0);
    let shades = [0u8, 85u8, 170u8, 255u8];
    let mut lut = [0u8; 4];
    for (rank, &(_, idx)) in lums.iter().enumerate() {
        lut[idx] = shades[rank];
    }
    lut
}

// Comment (decision): Zero-stretch aspect-preserving downscaler from 160x160 WASM-4 canvas to 128x64 OLED.
// scale_mode 0 = Fill/Cover (0.8x uniform scale, center-cropped y=40..120), 1 = Fit (64x64 centered full playfield), 2 = 1:1 Native Crop (128x64 center).
fn project_to_oled(mem: &[u8], out: &mut [u8; OLED_W * OLED_H], scale_mode: u32) {
    let lut = build_palette_gray_lut(mem);
    let px = |sx: usize, sy: usize| -> u8 {
        let idx = sy * 160 + sx;
        let b = mem[ADDR_FRAMEBUFFER + (idx >> 2)];
        let c = (b >> ((idx & 3) * 2)) & 0x3;
        lut[c as usize]
    };
    match scale_mode {
        1 => {
            out.fill(0);
            for y in 0..OLED_H {
                let sy = (y * 160) / 64;
                for dx in 0..64 {
                    let sx = (dx * 160) / 64;
                    out[y * OLED_W + (32 + dx)] = px(sx, sy);
                }
            }
        }
        2 => {
            for y in 0..OLED_H {
                let sy = y + 48;
                for x in 0..OLED_W {
                    out[y * OLED_W + x] = px(x + 16, sy);
                }
            }
        }
        _ => {
            for y in 0..OLED_H {
                let sy = 40 + (y * 80) / 64;
                for x in 0..OLED_W {
                    let sx = (x * 160) / 128;
                    out[y * OLED_W + x] = px(sx, sy);
                }
            }
        }
    }
}

fn read_u32_le(buf: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([buf[off], buf[off + 1], buf[off + 2], buf[off + 3]])
}

fn write_u32_le(buf: &mut [u8], off: usize, v: u32) {
    buf[off..off + 4].copy_from_slice(&v.to_le_bytes());
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!("Usage: cart_wasm4 <cart.wasm> <shm_path>");
        std::process::exit(2);
    }
    let cart_path = PathBuf::from(&args[1]);
    let shm_path = PathBuf::from(&args[2]);

    let wasm_bytes = match std::fs::read(&cart_path) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("[cart_wasm4] Failed to read {}: {}", cart_path.display(), e);
            std::process::exit(1);
        }
    };

    let mut shm_file = match OpenOptions::new().read(true).write(true).open(&shm_path) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("[cart_wasm4] Failed to open SHM {}: {}", shm_path.display(), e);
            std::process::exit(1);
        }
    };

    let quit_flag = Arc::new(AtomicBool::new(false));
    let quit_stdin = Arc::clone(&quit_flag);
    thread::spawn(move || {
        let mut tmp = [0u8; 16];
        let mut stdin = std::io::stdin();
        loop {
            match stdin.read(&mut tmp) {
                Ok(0) | Err(_) => {
                    quit_stdin.store(true, Ordering::Relaxed);
                    break;
                }
                Ok(_) => {}
            }
        }
    });

    let apu_shared = Arc::new(Mutex::new(ApuShared {
        ch: Default::default(),
        any_triggered: false,
    }));
    let audio_muted = Arc::new(AtomicBool::new(false));
    let apu_handle = spawn_apu_thread(Arc::clone(&apu_shared), Arc::clone(&audio_muted), Arc::clone(&quit_flag));

    let engine = Engine::default();
    let module = match Module::new(&engine, &wasm_bytes[..]) {
        Ok(m) => m,
        Err(e) => {
            eprintln!("[cart_wasm4] Invalid WASM module: {}", e);
            std::process::exit(1);
        }
    };

    let mem_ty = MemoryType::new(1, Some(1));
    let mut tmp_store = Store::new(&engine, ());
    let dummy_mem = Memory::new(&mut tmp_store, mem_ty).expect("alloc 64KB W4 RAM");
    drop(tmp_store);

    let sav_path = cart_path.with_extension("sav");
    let mut store = Store::new(
        &engine,
        W4State {
            memory: dummy_mem,
            sav_path,
            apu: Arc::clone(&apu_shared),
        },
    );
    let memory = Memory::new(&mut store, mem_ty).expect("alloc 64KB W4 RAM");
    store.data_mut().memory = memory;

    {
        let mem = memory.data_mut(&mut store);
        let def_pal: [u32; 4] = [0x00e0f8cf, 0x0086c06c, 0x00306850, 0x00071821];
        for (i, c) in def_pal.iter().enumerate() {
            write_u32_le(mem, ADDR_PALETTE + i * 4, *c);
        }
        mem[ADDR_DRAW_COLORS] = 0x03;
        mem[ADDR_DRAW_COLORS + 1] = 0x12;
    }

    let mut linker: Linker<W4State> = Linker::new(&engine);
    linker.define("env", "memory", memory).expect("define memory");

    linker.func_wrap("env", "rect", |mut c: Caller<'_, W4State>, x: i32, y: i32, w: u32, h: u32| {
        let m = c.data().memory;
        w4_rect(m.data_mut(&mut c), x, y, w, h);
    }).unwrap();
    linker.func_wrap("env", "oval", |mut c: Caller<'_, W4State>, x: i32, y: i32, w: u32, h: u32| {
        let m = c.data().memory;
        w4_oval(m.data_mut(&mut c), x, y, w, h);
    }).unwrap();
    linker.func_wrap("env", "line", |mut c: Caller<'_, W4State>, x1: i32, y1: i32, x2: i32, y2: i32| {
        let m = c.data().memory;
        let mem = m.data_mut(&mut c);
        let col = (draw_colors(mem) & 0xF) as u8;
        w4_line(mem, x1, y1, x2, y2, col);
    }).unwrap();
    linker.func_wrap("env", "hline", |mut c: Caller<'_, W4State>, x: i32, y: i32, len: u32| {
        let m = c.data().memory;
        let mem = m.data_mut(&mut c);
        let col = (draw_colors(mem) & 0xF) as u8;
        w4_hline(mem, x, y, len, col);
    }).unwrap();
    linker.func_wrap("env", "vline", |mut c: Caller<'_, W4State>, x: i32, y: i32, len: u32| {
        let m = c.data().memory;
        let mem = m.data_mut(&mut c);
        let col = (draw_colors(mem) & 0xF) as u8;
        w4_vline(mem, x, y, len, col);
    }).unwrap();
    linker.func_wrap("env", "blit", |mut c: Caller<'_, W4State>, sp: u32, x: i32, y: i32, w: u32, h: u32, flags: u32| {
        let m = c.data().memory;
        w4_blit_sub(m.data_mut(&mut c), sp, x, y, w, h, 0, 0, w, flags);
    }).unwrap();
    linker.func_wrap("env", "blitSub", |mut c: Caller<'_, W4State>, sp: u32, x: i32, y: i32, w: u32, h: u32, sx: u32, sy: u32, stride: u32, flags: u32| {
        let m = c.data().memory;
        w4_blit_sub(m.data_mut(&mut c), sp, x, y, w, h, sx, sy, stride, flags);
    }).unwrap();
    linker.func_wrap("env", "text", |mut c: Caller<'_, W4State>, ptr: u32, x: i32, y: i32| {
        let m = c.data().memory;
        let mem = m.data_mut(&mut c);
        let start = (ptr as usize).min(mem.len());
        let mut end = start;
        while end < mem.len() && mem[end] != 0 && end - start < 256 {
            end += 1;
        }
        let s = mem[start..end].to_vec();
        w4_text_bytes(mem, &s, x, y);
    }).unwrap();
    linker.func_wrap("env", "textUtf8", |mut c: Caller<'_, W4State>, ptr: u32, len: u32, x: i32, y: i32| {
        let m = c.data().memory;
        let mem = m.data_mut(&mut c);
        let start = (ptr as usize).min(mem.len());
        let end = (start + (len as usize).min(256)).min(mem.len());
        let s = mem[start..end].to_vec();
        w4_text_bytes(mem, &s, x, y);
    }).unwrap();
    linker.func_wrap("env", "textUtf16", |mut c: Caller<'_, W4State>, ptr: u32, len: u32, x: i32, y: i32| {
        let m = c.data().memory;
        let mem = m.data_mut(&mut c);
        let start = (ptr as usize).min(mem.len());
        let mut s = Vec::new();
        for i in 0..(len as usize / 2).min(256) {
            let off = start + i * 2;
            if off + 1 >= mem.len() { break; }
            s.push(mem[off]);
        }
        w4_text_bytes(mem, &s, x, y);
    }).unwrap();
    linker.func_wrap("env", "tone", |c: Caller<'_, W4State>, freq: u32, dur: u32, vol: u32, flags: u32| {
        let ch_idx = (flags & 0x3) as usize;
        if let Ok(mut guard) = c.data().apu.lock() {
            guard.ch[ch_idx].trigger(freq, dur, vol, flags);
            guard.any_triggered = true;
        }
    }).unwrap();
    linker.func_wrap("env", "diskr", |mut c: Caller<'_, W4State>, dest: u32, size: u32| -> u32 {
        let sav = c.data().sav_path.clone();
        let m = c.data().memory;
        let max_sz = (size as usize).min(1024);
        let mut data = vec![0u8; max_sz];
        let n = File::open(&sav).and_then(|mut f| f.read(&mut data)).unwrap_or(0);
        let mem = m.data_mut(&mut c);
        let d = dest as usize;
        if d + n <= mem.len() {
            mem[d..d + n].copy_from_slice(&data[..n]);
        }
        n as u32
    }).unwrap();
    linker.func_wrap("env", "diskw", |c: Caller<'_, W4State>, src: u32, size: u32| -> u32 {
        let sav = c.data().sav_path.clone();
        let m = c.data().memory;
        let max_sz = (size as usize).min(1024);
        let mem = m.data(&c);
        let s = src as usize;
        let end = (s + max_sz).min(mem.len());
        let written = File::create(&sav)
            .and_then(|mut f| f.write_all(&mem[s..end]).map(|_| end - s))
            .unwrap_or(0);
        written as u32
    }).unwrap();
    linker.func_wrap("env", "trace", |_c: Caller<'_, W4State>, _ptr: u32| {}).unwrap();
    linker.func_wrap("env", "traceUtf8", |_c: Caller<'_, W4State>, _ptr: u32, _len: u32| {}).unwrap();
    linker.func_wrap("env", "traceUtf16", |_c: Caller<'_, W4State>, _ptr: u32, _len: u32| {}).unwrap();
    linker.func_wrap("env", "tracef", |_c: Caller<'_, W4State>, _ptr: u32, _stack: u32| {}).unwrap();

    let instance = match linker.instantiate_and_start(&mut store, &module) {
        Ok(inst) => inst,
        Err(e) => {
            eprintln!("[cart_wasm4] Instantiation error: {}", e);
            std::process::exit(1);
        }
    };

    if let Ok(start_fn) = instance.get_typed_func::<(), ()>(&store, "start") {
        let _ = start_fn.call(&mut store, ());
    }
    let update_fn = instance.get_typed_func::<(), ()>(&store, "update").ok();

    let mut hdr = [0u8; SHM_HEADER_SIZE];
    let mut oled_fb = [0u8; OLED_W * OLED_H];
    let mut frame_seq: u32 = 0;

    while !quit_flag.load(Ordering::Relaxed) {
        let t0 = Instant::now();
        if shm_file.seek(SeekFrom::Start(0)).is_err() || shm_file.read_exact(&mut hdr).is_err() {
            break;
        }
        let flags = read_u32_le(&hdr, 12);
        if (flags & 1) != 0 {
            break;
        }
        audio_muted.store((flags & 2) != 0, Ordering::Relaxed);
        let scale_mode = (flags >> 2) & 0x3;
        let gp1 = read_u32_le(&hdr, 20) as u8;
        let target_fps = read_u32_le(&hdr, 24).clamp(15, 60);

        {
            let mem = memory.data_mut(&mut store);
            mem[ADDR_GAMEPAD1] = gp1;
            let preserve_fb = (mem[ADDR_SYSTEM_FLAGS] & 1) != 0;
            if !preserve_fb {
                mem[ADDR_FRAMEBUFFER..ADDR_FRAMEBUFFER + FB_BYTES].fill(0);
            }
        }

        if let Some(ref f) = update_fn {
            if f.call(&mut store, ()).is_err() {
                break;
            }
        }

        {
            let mem = memory.data(&store);
            project_to_oled(mem, &mut oled_fb, scale_mode);
        }

        frame_seq = frame_seq.wrapping_add(1);
        let mut full = [0u8; SHM_TOTAL_SIZE];
        full[..SHM_HEADER_SIZE].copy_from_slice(&hdr);
        write_u32_le(&mut full, 8, frame_seq);
        write_u32_le(&mut full, 28, 1);
        full[SHM_HEADER_SIZE..].copy_from_slice(&oled_fb);
        if shm_file.seek(SeekFrom::Start(0)).is_err() || shm_file.write_all(&full).is_err() {
            break;
        }

        let period = Duration::from_micros(1_000_000 / target_fps as u64);
        let dt = t0.elapsed();
        if dt < period {
            thread::sleep(period - dt);
        }
    }

    quit_flag.store(true, Ordering::Relaxed);
    let _ = apu_handle.join();
    if shm_file.seek(SeekFrom::Start(28)).is_ok() {
        let _ = shm_file.write_all(&2u32.to_le_bytes());
    }
}
