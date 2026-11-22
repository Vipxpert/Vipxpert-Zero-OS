# Vipxpert Zero OS

A lightweight, fully customizable, machine-accurate headless audio & 3-phase PWM OLED operating system built for the **Banana Pi M4 Zero** (Allwinner H618).

## Key Highlights
- **Audiophile Signal Chain:** Custom PipeWire + LDAC/AAC/aptX/mSBC Bluetooth receiver, internal I2S **PCM5122** hardware register tuning (`0.5 dB` volume/balance, PLL/OSR/Charge-Pump dividers, ROM oversampling filters), external USB DAC autosuspend/routing, dual **ICS-43434** 24-bit I2S MEMS microphone array with live ambient transparency & wind-cut DSP, and **USB-C Hub / Android ADB 48 kHz PCM Last-Active Takeover**.
- **Deterministic 4-Core Split Architecture:**
  - **Core 1:** Isolated `OLED_Compositor` driving an **SH1106 128x64 OLED** over `800 kHz` chunked `I2C0` with 3-phase grayscale PWM sub-frames and V-Sync slot marquees.
  - **Core 3:** Isolated `SCHED_FIFO` PipeWire real-time audio `data-loop` & I2S DMA IRQs.
  - **Cores 0 & 2:** Asyncio Python OS UI, 16 customizable Lock/Visualizer/Ambient/MV scenes, **90 Hz Open-Drain 3-pass batch-ioctl 4x4 matrix keypad** (`<11 ms` cycle, zero-lag event queue), INA219 battery/power scope, IR transceiver, BLE tracker radar, and **zero-GC ARM64 Rust DSP workers** (`rust_workers/`: `viz_dsp` 20-band Constant-Q Goertzel RTA & `mic_scope_dsp` ultrasonic heterodyne scope).

## Quick Install (Armbian / Banana Pi M4 Zero)
1. Clone into `/home/<user>/vipxpert_minimal_os` so systemd & PipeWire paths match out of the box:
   ```bash
   git clone https://github.com/Vipxpert/Vipxpert-Zero-OS.git ~/vipxpert_minimal_os
   cd ~/vipxpert_minimal_os
   sudo python3 install.py
   ```
2. Reboot after `install.py` injects the Allwinner H618 Device Tree overlays, compiles PipeWire/LDAC & `rust_workers/`, and enables `vipxpert_os.service`.

## Repository Layout
- `main.py` / `vipxpert_os.service` — OS entrypoint & systemd unit
- `install.py` — Idempotent provisioner (APT, udev, H618 DT overlays, PipeWire/LDAC, ADB audio bridge, Rust workers)
- `src/core/` — Kernel loop, SQLite state, BlueZ AVRCP/HFP daemon, MediaArbiter (BT/Local/USB Last-Active Takeover), system chimes
- `src/hardware/` — SH1106 3-phase PWM display engine, Open-Drain 4x4 keypad scanner, INA219 power monitor, IR transceiver
- `src/ui/` & `src/apps/` — Compositor, hierarchical menus, Audio/Display/Bluetooth/Wi-Fi/Hotspot/IR apps, 5-slot Lock Screen host
- `src/scenes/` — Modular Clock, Music/RTA Visualizer, Ambient Saver, Hybrid, and Universal Music Video plugins
- `rust_workers/` — Zero-dependency Rust real-time workers (`viz_dsp`, `mic_scope_dsp`)
- `tools/` — Virtual Device Control RPC CLI (`device_control.py`), Android ADB PCM bridge, & deterministic benchmarks
- `docs/` — Architectural trees (`docs/trees/`) & hardware/settings guides
