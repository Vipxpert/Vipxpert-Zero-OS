<!-- /vipxpert_minimal_os/docs/settings_guide.md -->
# VipXpert Minimal OS — Hardware, UI & Field Scopes Settings Guide

Complete reference guide for display compositor tuning, SH1106/SSD1306 silicon registers, grayscale dithering, V-Sync marquee behavior, the 5-Slot Priority Lock Screen & Scene Cartridge Engine, `0Hz` Screen-Off Deep-Freeze, and the 4 Offline Hardware Field Scopes.

---

## 1. Display & Compositor Refresh (`OLED Refresh`)
Controls the V-Sync compositor target frame rate (`oled_fps`) inside **Settings -> Display & UI** and dynamically manages the Allwinner H618 `TWI0_CCR` (`0x05002014`) I2C0 clock profile:

| Setting | Frame Interval | I2C0 Bus Clock | Best Use Case |
| :--- | :--- | :--- | :--- |
| **10Hz – 30Hz** (`20Hz ★`) | `33.3ms – 100ms` | `400 kHz` (`0x28` Fast Mode) | Maximum power efficiency; zero overdrive needed. |
| **40Hz – 60Hz** | `16.67ms – 25ms` | `800 kHz` (`0x90` DUTY=1 Burst) | Locked 1:1 `1px/frame` smooth text scrolling and 3D wireframes. |
| **75Hz – 120Hz** | `8.33ms – 13.3ms` | `800 kHz` (`0x90` DUTY=1 Burst) | Ultra-low motion blur on partial-page updates (1–3 dirty 8px pages). |

* **Adaptive VRR (Variable Refresh Rate):** When no marquee text, 3D animation, or live scope is active on screen, the compositor automatically drops from `60–120Hz` to `0 I2C bytes/sec` (`_last_frame_bytes` deduplication), waking instantly on keypad input or telemetry changes.
* **Scoped I2C0 Overdrive:** During `>30Hz` operation, the `800 kHz` (`0x90`) clock burst is strictly scoped inside `I2C_BUS_LOCK` for the duration of `_flush_dirty_pages()`, immediately restoring `400 kHz` (`0x28`) afterward so the `INA219` UPS and `PCM5122` DAC never experience bus timing violations.

---

## 2. 3-Phase Hardware PWM Sync Calibrator (`PWM Sync Cal`)
The SH1106/SSD1306 internal RC oscillator is permanently locked to `0xD5 = 0xF0` (`~155.18 Hz` scan rate, `0xD9 = 0x11` `52 DCLKs/row`) inside the Core-1 `OLED_Compositor` process. Because 4-pin I2C OLED modules have no hardware `VSYNC`/`FR` pin and their on-glass RC oscillator drifts slightly with temperature, **Settings -> Display & UI -> PWM Sync Cal** provides a live, in-service binary bisection calibrator (`oled_pwm_dt`, default `6.443948ms` / `155.18Hz`):

- **Live Visualizer Demo & HUD:** Renders the exact 16-bar Page-5 (`y=43` and `y=48..56`) resting visualizer geometry at the bottom of the screen while displaying real-time period (`ms`), frequency (`Hz`), step size (`d:...u`), reversal count (`R`), and last direction at the top.
- **Keypad Controls:**
  * **`4` (`Left`) / `6` (`Right`):** Nudges the live sub-frame period in `/dev/shm/vipxpert_oled_shm` (`shm[24:32]`) by `-Step` (`4`) or `+Step` (`6`). Reversing direction (`4 <-> 6`) **automatically halves `Step` (`Step /= 2`)** for rapid sub-microsecond binary bisection.
  * **`2` (`Up`) / `8` (`Down`):** Manually doubles (`2`, `Step *= 2`) or halves (`8`, `Step /= 2`) the step size without altering the current period.
  * **`C` (`Save`):** Persists the locked period to SQLite (`oled_pwm_dt`) and shared memory, returning to `Display & UI`.
  * **`D` (`Cancel`):** Restores the previous period in shared memory and exits without saving.

---

## 3. Pre-Charge & Contrast Drive (`Pixel Drive` & `Brightness`)
Configures the SH1106/SSD1306 Phase 1 / Phase 2 pre-charge period (`0xD9`) alongside the master segment current register (`0x81` Contrast) via `oled_drive_mode`:

* **`Sub-1% Dim` (`night` — `0xD9 = 0x11, 0x81 = 0x01`):** Shortens the capacitive pre-charge pulse to 1 DCLK per phase while locking contrast to `1`. Achieves pitch-black room visibility roughly **5x dimmer** than standard `1%` brightness without collapsing the SH1106 internal charge pump.
* **`Soft Warm` (`soft` — `0xD9 = 0xF1`):** Extended Phase 2 pre-charge (`15 DCLKs`) with a 1-DCLK discharge for softer pixel edges and zero dark-room glare.
* **`Normal ★` (`normal` — `0xD9 = 0x22`):** Balanced factory pre-charge timing (`2 DCLKs` / `2 DCLKs`) honoring the user's `0%–100%` Brightness slider (`0x81`).
* **`Max Punch` (`crisp` — `0xD9 = 0xF2`):** Fast pre-charge overdrive for maximum daylight contrast on high-speed scrolling text and 3D lines.

---

## 4. V-Sync Slot Marquee Engine (`Scroll Speed`)
Controls the deterministic integer frame-step table (`marquee_speed`) and C-level 1-bit sprite blitter:

* **`Slow` (`2.0`):** Advances `0.5 px/frame` (`1 px` every `2` V-Sync frames = `30 px/s` at 60Hz).
* **`Normal ★` (`3.0`):** Advances `1.0 px/frame` (`60 px/s` at 60Hz — true 1:1 frame-locked motion with zero beat jitter).
* **`Fast` (`4.5`):** Advances `1.5 px/frame` (`90 px/s` at 60Hz; `1.0 px/frame` at 90Hz/120Hz).
* **Lifecycle & Telemetry Immunity:** Each overflowing row pauses for `1.2s` at pixel `0` on initial mount and after completing each full lap. Live numeric updates (`Vol: Pi:... BT:...`, `Bat: ...`, `DAC: ...`) and vertical viewport scrolling (`A`/`B`) update the cached sprite in-place without resetting the scroll phase.

---

## 5. Grayscale Shading (`Gray Shades`) & Live 3D Preview
* **`Gray Shades` (`oled_dither_mode`):** Selects how `Display.draw_shaded_rect()` and `Display.render_wireframe_3d()` render intermediate gray levels on the 1-bit OLED:
  * **`Off (Wire) ★` (`off`):** Pure 1-bit crisp wireframe and solid fills with zero dithering pattern or temporal shimmer.
  * **`Pure PWM Gray` (`pwm`):** Solid, dot-free darker-white gray shading across both Cube and Octahedron faces using a 1st-order Delta-Sigma sub-frame PWM duty cycle (`((seq * duty) & 7) < duty`) with crisp 100% white wireframe borders.
  * **`Spatial 4x4` (`spatial`):** Static 17-level Ordered 4x4 Bayer Matrix dithering. Renders shaded 3D polygon faces and grayscale gradients with **0Hz flicker** and **0 I2C traffic when static** (100% VRR-compatible).
  * **`Temporal PWM` (`temporal`):** Frame-alternating 2x2 Spatiotemporal Bayer PWM. Rotates the 2x2 threshold matrix on every V-Sync frame (`60Hz–120Hz`) so the eye integrates spatial dots into smoother continuous gray shades.
* **`3D Cube Test` (`[OFF]` / `[ON]`):** Renders a real-time 3D perspective cube inside `Settings -> Display & UI` (wireframe when `Gray Shades` is `Off`, or Lambertian-lit solid shaded faces + a 5-shade calibration bar when `Spatial 4x4` or `Temporal PWM` is selected).
* **`Contrast Mode`:** Toggles hardware-accelerated 1-bit frame inversion (`Normal ★` white-on-black vs `Inverted` black-on-white).
* **`Lock Timeout` & `Screen Sleep`:** Independently step inactivity timers across `15s`, `30s ★`, `60s ★`, `120s`, and `Never (0)`.

---

## 6. 5-Slot Lock Screen & Scene Engine (`Settings -> Lock & Scenes`)
Configures the 5 priority Lock Screen slots, their assigned Scene Cartridges, and Screen-Off deep-freeze behavior:

* **Launch & Overwrite Priority Hierarchy:**
  1. **Manual `A/B` Selection:** Governed by **`A/B Override`** (`Reset Timer ★`, `Pin Scene`, or `Strict Timer`).
  2. **`Screensaver` Slot (`screensaver`):** Idle animation/3D/scope slot triggered after **`Saver Delay`** (`10s..60s`, default `30s ★`) when permitted by **`Saver Trigger`**.
  3. **`Amb+Music` Hybrid Slot (`amb_music`):** Active when both music is playing **and** Microphone Transparency (`AMB`) is enabled.
  4. **`Music` Slot (`music`):** Active when music is playing (`AMB` off).
  5. **`Ambient` Slot (`ambient`):** Active when Microphone Transparency (`AMB`) is enabled (no music playing).
  6. **`Clock` Slot (`clock`):** Baseline fallback slot (`Digital Clock` or `Analog Clock`).
* **Available Scene Cartridges (`SCENE_CATALOG`):**
  * `Digital Clock` (`clock_digital`), `Analog Clock` (`clock_analog`), `Music Classic` (`music_classic`), `Music + Spectrum` (`music_split`), `Full Visualizer` (`music_viz`), `Ambient Mic HUD` (`ambient_hud`), `Amb+Music Hybrid` (`amb_music_hybrid`), `Interactive 3D` (`ambient_cube3d`), `USB Power Scope` (`power_scope`), `Mic & Ultrasonic` (`mic_ultrasonic`), `IR Logic Scope` (`ir_scope`), and `BLE Tracker Radar` (`ble_radar`).
  * Any Scene Cartridge can be assigned to any Lock Screen Slot or launched directly in-app via **Music -> Visualizer / Info**, **Settings -> Lock & Scenes -> Preview Scene**, **Settings -> Audio Hub -> Microphone -> Ultrasonic Scope**, or **Settings -> Hardware Info**.
* **`Saver Trigger` Policy (`lock_saver_policy`):**
  * **`Idle Only ★` (`idle_only`):** Keeps `Music`, `Ambient`, and `Amb+Music` pinned while active; only transitions to `Screensaver` when music is paused/stopped and `AMB` is off (or from `Clock`).
  * **`Pause Only` (`pause_only`):** Keeps `Music` / `Amb+Music` pinned while a track is actively playing, but allows `Ambient` or paused `Music` to enter `Screensaver` after `Saver Delay`.
  * **`Rigid Timer` (`rigid`):** Strictly transitions any slot to `Screensaver` after `Saver Delay` seconds of no keypad input.
* **Screen-Off `0Hz` Deep-Freeze & Media Key Reclaim:**
  * When the OLED powers down (`is_screen_off == True`), `OSKernel._main_loop()`, `PowerMonitor` (`INA219` at `0x43`), and all interactive 3D/visualizer/scope workers drop to **0Hz (zero I2C0 traffic and zero background subprocesses)**, and keys `0–9` automatically revert to **Music & Volume transport controls** (`2/8` Pi Vol, `7/9` BT Vol, `5` Play/Pause, `4/6` Prev/Next, `1` DAC Reset).
  * Pressing `*` (`DOWN`) to wake the OLED triggers `PowerMonitor.notify_screen_wake()` (flushing stale pre-sleep averaging history for an immediate fresh battery read) and `LockScreenApp.on_screen_wake()` (mounting the highest-priority active slot).

---

* **GIL-Free 48 kHz (`45 Hz – 20 kHz`) Subprocess Audio Visualizer & 3-Slot Source Separation (`_viz_scope_worker`):**
  * **Zero-GIL-Contention IPC Architecture (`AudioVizIPC`):** When `Music + Spectrum`, `Full Visualizer`, `Ambient Mic HUD`, or `Amb+Music Hybrid` is actively displayed on an awake OLED, `LockScreenApp` spawns a standalone Python DSP child process pinned to housekeeping **Cores 0–1** (`taskset -c 0,1`). Because all `48,000 Hz` PCM capture, `O_NONBLOCK` pipe draining, and CIC Boxcar Low-Pass Anti-Aliasing filters (8-sample block-average for `45–260 Hz` sub-bass and 2-sample block-average for `365–2450 Hz` mids, achieving >25 dB high-frequency alias rejection) feeding a clock-locked ~46.8 FPS Multi-Rate Goertzel bank in its own process group (`start_new_session=True` + `os.killpg`) execute inside the child process's independent GIL, the main `vipxpert_os` UI/OLED/keypad loop experiences **zero GIL contention**—reading only a compact 20-byte frame (`1 byte per bar`) at ~46.8 FPS and rendering **1px Floating Peak-Hold RTA Caps** (`viz_peaks`) above each bar.
  * **Real-Time Non-Blocking Pipe Drain (`O_NONBLOCK` + `select`):** Drains all queued OS pipe chunks on every frame and analyzes only the freshest 1,024 samples (`21.3ms`), eliminating pipe-buffer backlog latency. Automatically terminates the instant the screen sleeps (`is_screen_off == True`) or switches to a non-visualizer scene.
  * **Distinct 3-Slot Default Audio Routing:**
    * **`Music Slot` -> `Music + Spectrum` (`music_split` ★ default):** Reacts to **100% Pure Music Only**. When `AMB` mic transparency is enabled, `_resolve_pure_music_sink_input()` isolates the active `mpv` or `bluez` sink-input (`parec --monitor-stream=<idx>`) so room mic audio never bleeds into the music visualizer.
    * **`Ambient Slot` -> `Ambient Mic HUD` (`ambient_hud` ★ default):** Reacts to **100% Pure Room Mic Only** (`vip_mic_bus.monitor`).
    * **`Amb+Music Slot` -> `Amb+Music Hybrid` (`amb_music_hybrid` ★ default):** **Always Dual-Source (Music + Room Mic Combined)**. Visually sums the Music monitor and `vip_mic_bus.monitor` in software even when `AMB` IEM feedback is turned **OFF** (keeping IEM playback 100% mic-free and avoiding any clash with phone Bluetooth recorders).
  * **Dedicated `Visualizer Style` Menu (`Settings -> Lock & Scenes -> Visualizer Style`) & Nested Per-Scene `Visualizer >` Overrides:**
    * **`Spectrum Curve` (`lock_viz_curve` / `scene_curve_<id>`):** **`Pink +3dB ★` (`pink` — default)** vs. **`Raw RTA 0dB` (`raw`)**.
    * **`Bar Style` (`lock_viz_bar_style` / `scene_bar_<id>`):** **`Solid ★` (`solid` — default)**, **`Segmented LED` (`led`)**, **`PWM Shaded` (`shaded`)**, or **`Hollow` (`hollow`)**.
    * **`Peak Caps` (`lock_viz_cap_style` / `scene_cap_<id>`):** Defaults to **`PWM Dim ★` (`pwm`)** (renders floating peak particles with a soft `45%` OLED PWM gray fade so they contrast gently against the bars), or **`Bright Solid` (`solid`)**, **`2px Dot` (`dot`)**, or **`Off` (`off`)**.
    * **`Peak Gravity` (`lock_viz_cap_decay` / `scene_decay_<id>`):** Defaults to **`Slow Float ★` (`slow` — `-0.0055/frame`)** so peak particles drift down gracefully like embers, plus **`Medium` (`med`)**, **`Fast Drop` (`fast`)**, and **`Hold+Drop` (`hold`)**.
* **Scene Label Anchor, Launch Intro Sequences & 2-Line `#` Guide Cards (`Global HUD & FX` & `Per-Scene Setup`):**
  * **Default Screensaver Scope:** By default on automatic slot changes, only the **`Screensaver`** slot (and in-app live previews) auto-plays the intro banner transition; `Clock`, `Music`, `Ambient`, and `Amb+Music` slots render immediately without banner overlays unless customized in `Per-Scene Setup` or manually summoned via **`#`**.
  * **`Launch Intro` (`lock_hud_seq` / `scene_seq_<id>`):** Controls what animates onto the canvas when a scene mounts: **`Title Only ★` (`title` — default)**, **`Title -> Guide` (`title_guide`)**, **`Guide -> Title` (`guide_title`)**, **`Title + Guide` (`both`)**, **`Guide Only` (`guide`)**, or **`Off (Manual #)` (`off`)**.
  * **`Title Hold` (`1.5s`, `3.0s ★`, `5.0s`, `Always`) & `Guide Hold` (`3.0s`, `4.5s ★`, `8.0s`, `Always`):** Independently configure how long the 1-line Scene Title pill (`y=0..11`) and 2-line Keypad Guide card (`y=41..63`) stay visible before dissolving out via **`Bayer Dissolve ★`**, **`Curtain Slide`**, or **`Laser Wipe`**.
  * **`Label Anchor` (`lock_hud_anchor` / `scene_anchor_<id>`):** Positions the tight-fitted, padded pill/card at **`Left ★` (`left` — default)**, **`Center` (`center`)**, or **`Right` (`right`)**.
  * **Deterministic `#` Key 3-State Cycler (`1:Guide -> 2:Title -> 3:Clean`):** Pressing **`#`** in any scene transitions in **Step 1: 2-Line Keypad Guide Card (`y=41..63`)** displaying all `0–9` number controls (`|`-separated in `SCENE_GUIDES`), pressing **`#`** a second time transitions to **Step 2: Scene Title Pill (`y=0..11`)**, and pressing **`#`** a third time smoothly dissolves out to **Step 3: 100% Clean Canvas**.
---

## 7. Real-Time USB Power Oscilloscope (`USB Power Scope`)
Accessible from **Settings -> Hardware Info -> USB Power Scope** or assignable to any Lock Screen Slot in **Settings -> Lock & Scenes**:

* **On-Demand 15Hz Burst Mode:** Switches the Waveshare UPS HAT `INA219` (`0x43`) from its quiet 15s sleep cycle into `15Hz` (`ADC_16SAMP`) continuous sampling strictly while the scope is visible on an awake screen, automatically reverting to `15s` / `0Hz` sleep on exit or screen-off.
* **Exclusive Keypad Controls (`0–9`):**
  * **`[1]` Live Waveform Graph:** Scrolling 128x64 current (`mA`) oscilloscope trace with live `V`, `mA`, `mW`, and remaining battery runtime (`HH:MM`).
  * **`[2]` Charger & Cable Tester:** Measures voltage sag (`mV`), current delta (`dI`), and estimated cable/brick path resistance (`mOhm` graded `GOOD` / `FAIR` / `SAG!`).
  * **`[3]` USB Peripheral Spike Auditor:** Press **`[0]` (Tare)** before plugging in a USB DAC, hub, or dongle to measure the exact `Delta mA` and `Delta mW` power draw spike.
  * **`[5]` Hold / Freeze Trace** & **`[7] / [9]` Timebase Zoom (`1x`, `2x`, `4x`)**.

---

## 8. Dual-Mic FFT Spectrum & 18–24 kHz Ultrasonic Down-Converter (`Mic & Ultrasonic`)
Accessible from **Settings -> Audio Hub -> Microphone -> Ultrasonic Scope**, **Settings -> Hardware Info -> Ultrasonic Scope**, or assignable to any Lock Screen Slot:

* **On-Demand 48 kHz Capture:** Taps `vip_mic_bus.monitor` at `48,000 Hz` (`Nyquist = 24.0 kHz`) strictly while the scope is visible on an awake screen, automatically terminating capture and audio playback on exit or screen-off.
* **Exclusive Keypad Controls (`0–9`):**
  * **`[1]` Full-Band 0–24 kHz Spectrum + Stereo L/R VU:** Displays 18 Goertzel frequency bins alongside dual L/R peak VU bars and an automatic `[USONIC!]` beacon alert.
  * **`[2]` 18–24 kHz Ultrasonic Zoom Scanner:** Isolates `17.6 kHz – 23.6 kHz` in `350 Hz` narrowband steps to pinpoint ultrasonic store beacons, motion sensors, or SMPS charger coil whine.
  * **`[3]` Heterodyne Down-Converter HUD:** Multiplies ultrasonic frequencies by a tunable Local Oscillator (`17.0–22.5 kHz` via **`[4] / [6]`**, default `19.5 kHz`) to shift inaudible `20 kHz` tones down into audible headphone frequencies.
  * **`[5]` Toggle Live Heterodyne Audio Monitor** & **`[7] / [9]` Heterodyne Gain Boost (`1x..32x`)**.

---

## 9. Mode2 IR Logic Analyzer (`IR Logic Scope`) & BLE Proximity Radar (`BLE Tracker Radar`)
Accessible from **Settings -> Hardware Info** (and **IR Remotes -> IR Scope**) or assignable to any Lock Screen Slot:

* **`IR Logic Scope` (PH16 Pin 33 RX / PI9 Pin 31 TX):**
  * Captures microsecond pulse/space trains and draws a scaled 128px digital square-wave logic trace while automatically decoding **NEC, NEC Repeat, Samsung, Sony SIRC (12/15/20-bit), Philips RC5, and RC6** hex addresses.
  * **Controls:** **`[4] / [6]`** Pan timeline, **`[7] / [9]`** Zoom window (`15ms / 30ms / 45ms / 90ms`), **`[5]`** Re-transmit captured frame out of Pin 31 IR Blaster, **`[0]`** Clear.
* **`BLE Tracker Radar` (`hci0` Passive LE Scanner):**
  * Passively tracks nearby BLE advertisement packets and Company IDs (**Apple `0x004C` AirTag/FindMy, Samsung `0x0075` SmartTag, Tile `0x00C7`/`0xFEED`**) with live RSSI distance estimation and a radial bullseye radar.
  * **Controls:** **`[1]`** Radar Bullseye + RSSI Sparkline, **`[2]`** Tracker Watchlist, **`[4] / [6]`** Cycle tracked target, **`[5]`** Toggle Headphone Geiger Clicks (click rate accelerates as RSSI approaches `-35 dBm`), **`[0]`** Clear list.

---

## 10. Interactive Ambient Physics Cartridges (`Rain Pond`, `Star Shower`, `Corner Bounce`, `Windy Grass`)
Assignable to any Lock Screen Slot (such as `Screensaver`) in **Settings -> Lock & Scenes** or viewable directly via **Preview Scene -> Open >** (`A/B` cycles scenes):

* **`Rain Pond` (`ambient_pond`):**
  * Falling raindrops spawn concentric 3D-perspective elliptical ripples across the pond surface.
  * **Controls:** Keys **`[1]–[9]`** drop pebbles into the corresponding 3x3 spatial sector of the pond; **`[0]`** triggers a lightning flash and downpour burst.
* **`Star Shower` (`ambient_stars`):**
  * 3D warp starfield with streaking diagonal meteors.
  * **Controls:** **`[2] / [8] / [4] / [6]`** steer the 3D vanishing point, **`[7] / [9]`** throttle warp speed (`0.4x..4.5x`), **`[5]`** launches a 3-meteor salvo, **`[0]`** re-centers.
* **`Corner Bounce` (`ambient_bounce`):**
  * Bouncing badge/diamond/orb that tracks **Wall Hits (`W:`)** and **Exact Corner Strikes (`C:`)** with an inverted `HIT!` celebration flash.
  * **Controls:** **`[2] / [8] / [4] / [6]`** redirect velocity vectors, **`[1]`** cycles shape, **`[5]`** randomizes speed, **`[0]`** lines up a guaranteed corner strike.
* **`Windy Grass` (`ambient_grass`):**
  * 32 multi-segment grass blades swaying in sinusoidal wind waves with drifting fireflies.
  * **Controls:** **`[4] / [6]`** whip wind gusts left/right, **`[2] / [8]`** grow/trim grass height, **`[5]`** releases extra fireflies, **`[0]`** calms the breeze.
