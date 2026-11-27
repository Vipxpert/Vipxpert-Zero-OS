// /vipxpert_minimal_os/rust_workers/src/bin/cart_libretro.rs
// Zero-dependency Universal Libretro SHM+IPC Adapter (NES/GB/GBC/GBA/Arduboy) for Vipxpert Zero OS.
// Usage: cart_libretro <core_libretro.so> <rom_path> <shm_path>

use std::env;
use std::ffi::{c_char, c_void, CStr, CString};
use std::fs::OpenOptions;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

const SHM_HEADER_SIZE: usize = 64;
const OLED_W: usize = 128;
const OLED_H: usize = 64;
const SHM_TOTAL_SIZE: usize = SHM_HEADER_SIZE + OLED_W * OLED_H;

const RTLD_NOW: i32 = 2;
const RTLD_LOCAL: i32 = 0;

const RETRO_ENVIRONMENT_GET_CAN_DUPE: u32 = 3;
const RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY: u32 = 9;
const RETRO_ENVIRONMENT_SET_PIXEL_FORMAT: u32 = 10;
const RETRO_ENVIRONMENT_GET_VARIABLE: u32 = 15;
const RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE: u32 = 16;
const RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY: u32 = 31;
const RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO: u32 = 32;
const RETRO_ENVIRONMENT_SET_GEOMETRY: u32 = 37;

const RETRO_PIXEL_FORMAT_0RGB1555: u32 = 0;
const RETRO_PIXEL_FORMAT_XRGB8888: u32 = 1;
const RETRO_PIXEL_FORMAT_RGB565: u32 = 2;

const RETRO_DEVICE_JOYPAD: u32 = 1;

extern "C" {
    fn dlopen(filename: *const c_char, flags: i32) -> *mut c_void;
    fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
    fn dlclose(handle: *mut c_void) -> i32;
    fn dlerror() -> *const c_char;
}

#[repr(C)]
struct RetroGameInfo {
    path: *const c_char,
    data: *const c_void,
    size: usize,
    meta: *const c_char,
}

#[repr(C)]
#[derive(Default, Copy, Clone)]
struct RetroGameGeometry {
    base_width: u32,
    base_height: u32,
    max_width: u32,
    max_height: u32,
    aspect_ratio: f32,
}

#[repr(C)]
#[derive(Default, Copy, Clone)]
struct RetroSystemTiming {
    fps: f64,
    sample_rate: f64,
}

#[repr(C)]
#[derive(Default, Copy, Clone)]
struct RetroSystemAvInfo {
    geometry: RetroGameGeometry,
    timing: RetroSystemTiming,
}

struct CoreRuntime {
    pixel_format: AtomicU32,
    scale_mode: AtomicU32,
    zoom_pct: AtomicU32,
    pan_x: std::sync::atomic::AtomicI32,
    pan_y: std::sync::atomic::AtomicI32,
    buttons_down: AtomicU32,
    audio_muted: AtomicBool,
    sys_dir: CString,
    save_dir: CString,
    oled_fb: Mutex<[u8; OLED_W * OLED_H]>,
    audio_buf: Mutex<Vec<u8>>,
}

static RT: OnceLock<CoreRuntime> = OnceLock::new();

fn rt() -> &'static CoreRuntime {
    RT.get().expect("CoreRuntime initialized")
}

#[inline]
fn read_u32_le(buf: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([buf[off], buf[off + 1], buf[off + 2], buf[off + 3]])
}

#[inline]
fn write_u32_le(buf: &mut [u8], off: usize, v: u32) {
    buf[off..off + 4].copy_from_slice(&v.to_le_bytes());
}

#[inline]
fn sample_luma(data: *const u8, pitch: usize, fmt: u32, sx: usize, sy: usize) -> u8 {
    unsafe {
        let row = data.add(sy * pitch);
        let (r, g, b) = match fmt {
            RETRO_PIXEL_FORMAT_XRGB8888 => {
                let p = (row as *const u32).add(sx).read_unaligned();
                (((p >> 16) & 0xFF) as u32, ((p >> 8) & 0xFF) as u32, (p & 0xFF) as u32)
            }
            RETRO_PIXEL_FORMAT_RGB565 => {
                let p = (row as *const u16).add(sx).read_unaligned() as u32;
                let r5 = (p >> 11) & 0x1F;
                let g6 = (p >> 5) & 0x3F;
                let b5 = p & 0x1F;
                ((r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2))
            }
            _ => {
                // 0RGB1555
                let p = (row as *const u16).add(sx).read_unaligned() as u32;
                let r5 = (p >> 10) & 0x1F;
                let g5 = (p >> 5) & 0x1F;
                let b5 = p & 0x1F;
                ((r5 << 3) | (r5 >> 2), (g5 << 3) | (g5 >> 2), (b5 << 3) | (b5 >> 2))
            }
        };
        ((r * 77 + g * 150 + b * 29) >> 8) as u8
    }
}

// Comment (decision): Universal RGB->8-bit-luma zero-stretch aspect-preserving scaler for any Libretro core (NES/GB/GBC/GBA/Arduboy).
// scale_mode 0 = Fill/Cover (uniform width=128, center vertical crop), 1 = Fit (uniform height=64, centered letterbox), 2 = 1:1 Native Crop.
unsafe extern "C" fn cb_video_refresh(data: *const c_void, width: u32, height: u32, pitch: usize) {
    if data.is_null() || width == 0 || height == 0 {
        return;
    }
    let state = rt();
    let fmt = state.pixel_format.load(Ordering::Relaxed);
    let scale_mode = state.scale_mode.load(Ordering::Relaxed);
    let w = width as usize;
    let h = height as usize;
    let ptr = data as *const u8;

    let mut fb = state.oled_fb.lock().unwrap();
    match scale_mode {
        1 => {
            // Fit: preserve aspect inside 128x64 with black pillar/letterbox
            fb.fill(0);
            let (dw, dh) = if w * OLED_H >= h * OLED_W {
                (OLED_W, ((h * OLED_W) / w).clamp(1, OLED_H))
            } else {
                (((w * OLED_H) / h).clamp(1, OLED_W), OLED_H)
            };
            let ox = (OLED_W - dw) / 2;
            let oy = (OLED_H - dh) / 2;
            for y in 0..dh {
                let sy = (y * h) / dh;
                for x in 0..dw {
                    let sx = (x * w) / dw;
                    fb[(oy + y) * OLED_W + (ox + x)] = sample_luma(ptr, pitch, fmt, sx, sy);
                }
            }
        }
        2 => {
            // 1:1 Native Crop centered
            fb.fill(0);
            let cw = w.min(OLED_W);
            let ch = h.min(OLED_H);
            let sx0 = (w - cw) / 2;
            let sy0 = (h - ch) / 2;
            let dx0 = (OLED_W - cw) / 2;
            let dy0 = (OLED_H - ch) / 2;
            for y in 0..ch {
                for x in 0..cw {
                    fb[(dy0 + y) * OLED_W + (dx0 + x)] = sample_luma(ptr, pitch, fmt, sx0 + x, sy0 + y);
                }
            }
        }
        3 => {
            // Custom Zoom & Pan: zoom_pct (50..300, 100=Fill width), pan_x/pan_y (-1000..1000, 0=Center, +/-1000=Edges)
            let zoom = state.zoom_pct.load(Ordering::Relaxed).clamp(50, 300) as f32;
            let pan_x = state.pan_x.load(Ordering::Relaxed).clamp(-1000, 1000) as f32 / 1000.0;
            let pan_y = state.pan_y.load(Ordering::Relaxed).clamp(-1000, 1000) as f32 / 1000.0;
            let wf = w as f32;
            let hf = h as f32;
            let vw = wf * 100.0 / zoom;
            let vh = vw * (OLED_H as f32) / (OLED_W as f32);
            let max_ox = ((wf - vw) * 0.5).max(0.0);
            let max_oy = ((hf - vh) * 0.5).max(0.0);
            let cx = wf * 0.5 + pan_x * max_ox;
            let cy = hf * 0.5 + pan_y * max_oy;
            let x0 = cx - vw * 0.5;
            let y0 = cy - vh * 0.5;
            // Comment (quality): Use exact fractional area-weighted box resampling when downscaling (step > 1.0) so 1px/2px NES checkerboard/dither tiles average into steady gray instead of aliasing into scrolling black/white moire bands; use bilinear only when magnifying.
            let step_x = vw / OLED_W as f32;
            let step_y = vh / OLED_H as f32;
            let use_area = step_x > 1.0 || step_y > 1.0;
            for y in 0..OLED_H {
                let fy0 = y0 + (y as f32) * step_y;
                let fy1 = fy0 + step_y;
                if fy1 <= 0.0 || fy0 >= hf {
                    fb[y * OLED_W..(y + 1) * OLED_W].fill(0);
                    continue;
                }
                if use_area {
                    let cy0 = fy0.max(0.0);
                    let cy1 = fy1.min(hf);
                    let iy0 = (cy0.floor() as usize).min(h - 1);
                    let iy1 = (cy1.ceil() as usize).clamp(iy0 + 1, h);
                    for x in 0..OLED_W {
                        let fx0 = x0 + (x as f32) * step_x;
                        let fx1 = fx0 + step_x;
                        if fx1 <= 0.0 || fx0 >= wf {
                            fb[y * OLED_W + x] = 0;
                            continue;
                        }
                        let cx0 = fx0.max(0.0);
                        let cx1 = fx1.min(wf);
                        let ix0 = (cx0.floor() as usize).min(w - 1);
                        let ix1 = (cx1.ceil() as usize).clamp(ix0 + 1, w);
                        let mut sum = 0.0f32;
                        let mut wsum = 0.0f32;
                        for sy in iy0..iy1 {
                            let wy = (cy1.min((sy + 1) as f32) - cy0.max(sy as f32)).max(0.0);
                            if wy <= 0.0 {
                                continue;
                            }
                            for sx in ix0..ix1 {
                                let wx = (cx1.min((sx + 1) as f32) - cx0.max(sx as f32)).max(0.0);
                                let wt = wx * wy;
                                sum += sample_luma(ptr, pitch, fmt, sx, sy) as f32 * wt;
                                wsum += wt;
                            }
                        }
                        fb[y * OLED_W + x] = if wsum > 0.0 { (sum / wsum).round().clamp(0.0, 255.0) as u8 } else { 0 };
                    }
                } else {
                    let yc = (fy0 + 0.5 * step_y - 0.5).clamp(0.0, (h - 1) as f32);
                    let y_a = yc.floor() as usize;
                    let y_b = (y_a + 1).min(h - 1);
                    let ty = yc - y_a as f32;
                    for x in 0..OLED_W {
                        let fx0 = x0 + (x as f32) * step_x;
                        let fx1 = fx0 + step_x;
                        if fx1 <= 0.0 || fx0 >= wf {
                            fb[y * OLED_W + x] = 0;
                            continue;
                        }
                        let xc = (fx0 + 0.5 * step_x - 0.5).clamp(0.0, (w - 1) as f32);
                        let x_a = xc.floor() as usize;
                        let x_b = (x_a + 1).min(w - 1);
                        let tx = xc - x_a as f32;
                        let p00 = sample_luma(ptr, pitch, fmt, x_a, y_a) as f32;
                        let p10 = sample_luma(ptr, pitch, fmt, x_b, y_a) as f32;
                        let p01 = sample_luma(ptr, pitch, fmt, x_a, y_b) as f32;
                        let p11 = sample_luma(ptr, pitch, fmt, x_b, y_b) as f32;
                        let top = p00 + (p10 - p00) * tx;
                        let bot = p01 + (p11 - p01) * tx;
                        fb[y * OLED_W + x] = (top + (bot - top) * ty).round().clamp(0.0, 255.0) as u8;
                    }
                }
            }
        }
        _ => {
            // Fill (Cover): uniform aspect-preserving scale filling 128x64, cropping excess axis
            if w * OLED_H <= h * OLED_W {
                // Taller than 2:1 (NES 256x240, GB 160x144, GBA 240x160): fit width=128, crop height uniformly
                let crop_h = ((w * OLED_H) / OLED_W).clamp(1, h);
                let sy0 = (h - crop_h) / 2;
                for y in 0..OLED_H {
                    let sy = sy0 + (y * crop_h) / OLED_H;
                    for x in 0..OLED_W {
                        let sx = (x * w) / OLED_W;
                        fb[y * OLED_W + x] = sample_luma(ptr, pitch, fmt, sx, sy);
                    }
                }
            } else {
                // Wider than 2:1: fit height=64, crop width uniformly
                let crop_w = ((h * OLED_W) / OLED_H).clamp(1, w);
                let sx0 = (w - crop_w) / 2;
                for y in 0..OLED_H {
                    let sy = (y * h) / OLED_H;
                    for x in 0..OLED_W {
                        let sx = sx0 + (x * crop_w) / OLED_W;
                        fb[y * OLED_W + x] = sample_luma(ptr, pitch, fmt, sx, sy);
                    }
                }
            }
        }
    }
}

extern "C" fn cb_audio_sample(left: i16, right: i16) {
    let state = rt();
    if state.audio_muted.load(Ordering::Relaxed) {
        return;
    }
    if let Ok(mut buf) = state.audio_buf.lock() {
        buf.extend_from_slice(&left.to_le_bytes());
        buf.extend_from_slice(&right.to_le_bytes());
    }
}

unsafe extern "C" fn cb_audio_sample_batch(data: *const i16, frames: usize) -> usize {
    let state = rt();
    if !data.is_null() && frames > 0 && !state.audio_muted.load(Ordering::Relaxed) {
        let samples = std::slice::from_raw_parts(data, frames * 2);
        let mut has_l = false;
        let mut has_r = false;
        for f in 0..frames {
            if samples[f * 2] != 0 { has_l = true; }
            if samples[f * 2 + 1] != 0 { has_r = true; }
            if has_l && has_r { break; }
        }
        if let Ok(mut buf) = state.audio_buf.lock() {
            if has_l && !has_r {
                for f in 0..frames {
                    let b = samples[f * 2].to_le_bytes();
                    buf.extend_from_slice(&b);
                    buf.extend_from_slice(&b);
                }
            } else {
                let bytes = std::slice::from_raw_parts(data as *const u8, frames * 4);
                buf.extend_from_slice(bytes);
            }
        }
    }
    frames
}

extern "C" fn cb_input_poll() {}

// 16-key bitmask (KEY_TO_BIT in cartridge_app.py):
// 0:0, 1:1(SELECT), 2:2(UP), 3:3(START), 4:4(LEFT), 5:5(A/JUMP), 6:6(RIGHT),
// 7:7(L), 8:8(DOWN), 9:9(R), 10:A(B), 11:B(B), 12:C(A/JUMP)
extern "C" fn cb_input_state(port: u32, device: u32, _index: u32, id: u32) -> i16 {
    if port != 0 || (device & 0xFF) != RETRO_DEVICE_JOYPAD {
        return 0;
    }
    let mask = rt().buttons_down.load(Ordering::Relaxed);
    // Comment (compatibility): Support both RETRO_DEVICE_ID_JOYPAD_MASK (256) and per-button id queries for Nestopia/libretro cores.
    let mut joy: u16 = 0;
    if (mask & ((1 << 10) | (1 << 11))) != 0 { joy |= 1 << 0; }  // B <- keys A/B
    if (mask & (1 << 1)) != 0 { joy |= 1 << 2; }                 // SELECT <- key 1
    if (mask & (1 << 3)) != 0 { joy |= 1 << 3; }                 // START <- key 3
    if (mask & (1 << 2)) != 0 { joy |= 1 << 4; }                 // UP <- key 2
    if (mask & (1 << 8)) != 0 { joy |= 1 << 5; }                 // DOWN <- key 8
    if (mask & (1 << 4)) != 0 { joy |= 1 << 6; }                 // LEFT <- key 4
    if (mask & (1 << 6)) != 0 { joy |= 1 << 7; }                 // RIGHT <- key 6
    if (mask & ((1 << 5) | (1 << 12))) != 0 { joy |= 1 << 8; }   // A (Jump) <- keys 5/C
    if (mask & (1 << 7)) != 0 { joy |= 1 << 10; }                // L <- key 7
    if (mask & (1 << 9)) != 0 { joy |= 1 << 11; }                // R <- key 9
    if id == 256 {
        return joy as i16;
    }
    if id < 16 && (joy & (1u16 << id)) != 0 { 1 } else { 0 }
}

unsafe extern "C" fn cb_environment(cmd: u32, data: *mut c_void) -> bool {
    let state = rt();
    match cmd {
        RETRO_ENVIRONMENT_GET_CAN_DUPE => {
            if !data.is_null() {
                *(data as *mut bool) = true;
            }
            true
        }
        RETRO_ENVIRONMENT_SET_PIXEL_FORMAT => {
            if !data.is_null() {
                let fmt = *(data as *const u32);
                if fmt <= RETRO_PIXEL_FORMAT_RGB565 {
                    state.pixel_format.store(fmt, Ordering::Relaxed);
                    return true;
                }
            }
            false
        }
        RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY => {
            if !data.is_null() {
                *(data as *mut *const c_char) = state.sys_dir.as_ptr();
            }
            true
        }
        RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY => {
            if !data.is_null() {
                *(data as *mut *const c_char) = state.save_dir.as_ptr();
            }
            true
        }
        RETRO_ENVIRONMENT_GET_VARIABLE => false,
        RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE => {
            if !data.is_null() {
                *(data as *mut bool) = false;
            }
            true
        }
        RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO | RETRO_ENVIRONMENT_SET_GEOMETRY => true,
        _ => false,
    }
}

unsafe fn load_sym<T>(handle: *mut c_void, name: &str) -> T {
    let c_name = CString::new(name).unwrap();
    let ptr = dlsym(handle, c_name.as_ptr());
    if ptr.is_null() {
        eprintln!("[cart_libretro] Missing symbol: {}", name);
        std::process::exit(1);
    }
    std::mem::transmute_copy(&ptr)
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 4 {
        eprintln!("Usage: cart_libretro <core_libretro.so> <rom_path> <shm_path>");
        std::process::exit(2);
    }
    let core_path = PathBuf::from(&args[1]);
    let rom_path = PathBuf::from(&args[2]);
    let shm_path = PathBuf::from(&args[3]);

    let rom_bytes = match std::fs::read(&rom_path) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("[cart_libretro] Failed to read ROM {}: {}", rom_path.display(), e);
            std::process::exit(1);
        }
    };

    let mut shm_file = match OpenOptions::new().read(true).write(true).open(&shm_path) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("[cart_libretro] Failed to open SHM {}: {}", shm_path.display(), e);
            std::process::exit(1);
        }
    };

    let parent_dir = rom_path.parent().unwrap_or_else(|| std::path::Path::new("/tmp"));
    let dir_c = CString::new(parent_dir.to_string_lossy().as_ref()).unwrap();
    let _ = RT.set(CoreRuntime {
        pixel_format: AtomicU32::new(RETRO_PIXEL_FORMAT_0RGB1555),
        scale_mode: AtomicU32::new(0),
        zoom_pct: AtomicU32::new(100),
        pan_x: std::sync::atomic::AtomicI32::new(0),
        pan_y: std::sync::atomic::AtomicI32::new(0),
        buttons_down: AtomicU32::new(0),
        audio_muted: AtomicBool::new(false),
        sys_dir: dir_c.clone(),
        save_dir: dir_c,
        oled_fb: Mutex::new([0u8; OLED_W * OLED_H]),
        audio_buf: Mutex::new(Vec::with_capacity(8192)),
    });

    // Parent-death guard: exit cleanly if host closes stdin pipe.
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

    let core_c = CString::new(core_path.to_string_lossy().as_ref()).unwrap();
    let handle = unsafe { dlopen(core_c.as_ptr(), RTLD_NOW | RTLD_LOCAL) };
    if handle.is_null() {
        let err = unsafe {
            let e = dlerror();
            if e.is_null() { "unknown".to_string() } else { CStr::from_ptr(e).to_string_lossy().into_owned() }
        };
        eprintln!("[cart_libretro] dlopen failed for {}: {}", core_path.display(), err);
        std::process::exit(1);
    }

    unsafe {
        let retro_set_environment: extern "C" fn(unsafe extern "C" fn(u32, *mut c_void) -> bool) = load_sym(handle, "retro_set_environment");
        let retro_set_video_refresh: extern "C" fn(unsafe extern "C" fn(*const c_void, u32, u32, usize)) = load_sym(handle, "retro_set_video_refresh");
        let retro_set_audio_sample: extern "C" fn(extern "C" fn(i16, i16)) = load_sym(handle, "retro_set_audio_sample");
        let retro_set_audio_sample_batch: extern "C" fn(unsafe extern "C" fn(*const i16, usize) -> usize) = load_sym(handle, "retro_set_audio_sample_batch");
        let retro_set_input_poll: extern "C" fn(extern "C" fn()) = load_sym(handle, "retro_set_input_poll");
        let retro_set_input_state: extern "C" fn(extern "C" fn(u32, u32, u32, u32) -> i16) = load_sym(handle, "retro_set_input_state");
        let retro_init: extern "C" fn() = load_sym(handle, "retro_init");
        let retro_deinit: extern "C" fn() = load_sym(handle, "retro_deinit");
        let retro_load_game: extern "C" fn(*const RetroGameInfo) -> bool = load_sym(handle, "retro_load_game");
        let retro_unload_game: extern "C" fn() = load_sym(handle, "retro_unload_game");
        let retro_get_system_av_info: extern "C" fn(*mut RetroSystemAvInfo) = load_sym(handle, "retro_get_system_av_info");
        let retro_run: extern "C" fn() = load_sym(handle, "retro_run");
        let retro_set_controller_port_device: extern "C" fn(u32, u32) = load_sym(handle, "retro_set_controller_port_device");

        retro_set_environment(cb_environment);
        retro_set_video_refresh(cb_video_refresh);
        retro_set_audio_sample(cb_audio_sample);
        retro_set_audio_sample_batch(cb_audio_sample_batch);
        retro_set_input_poll(cb_input_poll);
        retro_set_input_state(cb_input_state);
        retro_init();

        let rom_c = CString::new(rom_path.to_string_lossy().as_ref()).unwrap();
        let game_info = RetroGameInfo {
            path: rom_c.as_ptr(),
            data: rom_bytes.as_ptr() as *const c_void,
            size: rom_bytes.len(),
            meta: std::ptr::null(),
        };
        if !retro_load_game(&game_info) {
            eprintln!("[cart_libretro] retro_load_game rejected {}", rom_path.display());
            retro_deinit();
            dlclose(handle);
            std::process::exit(1);
        }

        // Comment (invariant): Nestopia/libretro cores require explicit Port 0 joypad attachment after retro_load_game, otherwise controller input is ignored.
        retro_set_controller_port_device(0, RETRO_DEVICE_JOYPAD);

        let mut av = RetroSystemAvInfo::default();
        retro_get_system_av_info(&mut av);
        let core_fps = if av.timing.fps >= 20.0 && av.timing.fps <= 120.0 { av.timing.fps } else { 60.0 };
        let sample_rate = if av.timing.sample_rate >= 8000.0 && av.timing.sample_rate <= 192000.0 {
            av.timing.sample_rate.round() as u32
        } else {
            44100
        };
        let sr_str = sample_rate.to_string();

        let mut pw_child: Option<Child> = None;
        let mut hdr = [0u8; SHM_HEADER_SIZE];
        let mut frame_seq: u32 = 0;
        let period = Duration::from_secs_f64(1.0 / core_fps);
        let mut next_frame = Instant::now();

        while !quit_flag.load(Ordering::Relaxed) {
            if shm_file.seek(SeekFrom::Start(0)).is_err() || shm_file.read_exact(&mut hdr).is_err() {
                break;
            }
            let flags = read_u32_le(&hdr, 12);
            if (flags & 1) != 0 {
                break;
            }
            let muted = (flags & 2) != 0;
            let scale_mode = (flags >> 2) & 0x3;
            let buttons = read_u32_le(&hdr, 16);
            // Comment (invariant): Offset 28 is worker status (1=running, 2=exited); host zoom/pan lives at offsets 32, 36, 40.
            let zoom_pct = read_u32_le(&hdr, 32);
            let pan_x = i32::from_le_bytes([hdr[36], hdr[37], hdr[38], hdr[39]]);
            let pan_y = i32::from_le_bytes([hdr[40], hdr[41], hdr[42], hdr[43]]);

            let state = rt();
            state.audio_muted.store(muted, Ordering::Relaxed);
            state.scale_mode.store(scale_mode, Ordering::Relaxed);
            state.zoom_pct.store(if zoom_pct == 0 { 100 } else { zoom_pct }, Ordering::Relaxed);
            state.pan_x.store(pan_x, Ordering::Relaxed);
            state.pan_y.store(pan_y, Ordering::Relaxed);
            state.buttons_down.store(buttons, Ordering::Relaxed);

            retro_run();

            // Flush PipeWire audio chunk
            let pcm_chunk = {
                let mut ab = state.audio_buf.lock().unwrap();
                let out = ab.clone();
                ab.clear();
                out
            };
            if !muted && !pcm_chunk.is_empty() {
                if pw_child.is_none() {
                    // Comment (audio): Use sudo -u vipxpert env XDG_RUNTIME_DIR=/run/user/1000 pw-cat matching working OS audio playback patterns in audio_settings_app.py and os_kernel.py.
                    let pw_bin = if std::path::Path::new("/usr/local/bin/pw-cat").exists() {
                        "/usr/local/bin/pw-cat"
                    } else {
                        "pw-cat"
                    };
                    if let Ok(c) = Command::new("sudo")
                        .args([
                            "-u", "vipxpert", "env", "XDG_RUNTIME_DIR=/run/user/1000",
                            pw_bin, "--playback", "--raw", "--rate", &sr_str, "--channels", "2", "--channel-map", "stereo",
                            "--format", "s16", "--latency", "80ms", "--media-role", "Game", "-",
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
                        if stdin.write_all(&pcm_chunk).is_err() {
                            let _ = c.kill();
                            let _ = c.wait();
                            pw_child = None;
                        }
                    }
                }
            } else if muted {
                if let Some(mut c) = pw_child.take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
            }

            frame_seq = frame_seq.wrapping_add(1);
            // Comment (invariant): Never write back host-owned control bytes (12..28, 32..44) or concurrent keypad/zoom updates will be clobbered.
            let fb_copy = {
                let fb = state.oled_fb.lock().unwrap();
                *fb
            };
            if shm_file.seek(SeekFrom::Start(SHM_HEADER_SIZE as u64)).is_err()
                || shm_file.write_all(&fb_copy).is_err()
                || shm_file.seek(SeekFrom::Start(28)).is_err()
                || shm_file.write_all(&1u32.to_le_bytes()).is_err()
                || shm_file.seek(SeekFrom::Start(8)).is_err()
                || shm_file.write_all(&frame_seq.to_le_bytes()).is_err()
            {
                break;
            }

            next_frame += period;
            let now = Instant::now();
            if next_frame > now {
                thread::sleep(next_frame - now);
            } else if now.duration_since(next_frame) > period * 3 {
                next_frame = now;
            }
        }

        if let Some(mut c) = pw_child.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
        retro_unload_game();
        retro_deinit();
        dlclose(handle);
    }

    if shm_file.seek(SeekFrom::Start(28)).is_ok() {
        let _ = shm_file.write_all(&2u32.to_le_bytes());
    }
}
