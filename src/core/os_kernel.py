# /vipxpert_minimal_os/src/core/os_kernel.py
import asyncio
import subprocess
import pyudev
import time
from src.core.database import DatabaseManager
from src.hardware.display import Display
from src.hardware.keypad import Keypad
from src.hardware.ir_transceiver import IRTransceiver
from src.hardware.power_monitor import PowerMonitor
from src.ui.ui_manager import UIManager
from src.core.bluetooth_daemon import BluetoothDaemon
from src.apps.dashboard import DashboardApp
from src.apps.ir_app import IRControllerApp
from src.ui.components import ListMenuApp
from src.apps.recent_app import RecentApp
from src.apps.wifi_app import WiFiManagerApp
from src.apps.lock_app import LockScreenApp
from src.apps.bluetooth_app import BluetoothManagerApp, BluetoothDevicesApp, BluetoothBlockedApp, BluetoothCodecApp, BluetoothQualityApp
from src.apps.hotspot_app import HotspotManagerApp
from src.apps.settings_app import SettingsApp
from src.apps.audio_settings_app import AudioSettingsApp
from src.core.media_arbiter import MediaArbiter

class OSKernel:
    def __init__(self):
        self.db = DatabaseManager()
        self.hw_profile = self._run_boot_hw_profile()
        self.recover_i2c0_bus(force=False)
        self.display = Display()
        self.keypad = Keypad()
        self.ir_hardware = IRTransceiver()
        self.ui_manager = UIManager(self.display, self.db)
        self.ui_manager.hw_profile = self.hw_profile
        self.ui_manager.os_kernel = self  # INJECT: Expose kernel to UI for hardware callbacks
        self.keypad.ui_manager = self.ui_manager  # INJECT: Allow keypad to throttle polling during OLED sleep
        self.bt_daemon = BluetoothDaemon(self.ui_manager, self.db)
        self.power_monitor = PowerMonitor(self.ui_manager)
        
        from src.core.sound_manager import SystemSoundManager
        self.sound_manager = SystemSoundManager(self.db)
        self.ui_manager.sound_manager = self.sound_manager

        # Instantiate and Mount the Arbiter (Passing ui_manager for post-wake volume callbacks)
        self.media_arbiter = MediaArbiter(self.bt_daemon, self.db, self.ui_manager)
        self.ui_manager.media_arbiter = self.media_arbiter
        
        # Comment (constraint): Banana Pi M4 Zero only routes 1 physical USB data port (5311000.usb Host); binding usb_f_uac2 to unwired musb-hdrc.4.auto (5100000.usb) hangs the H618 SoC in kernel bus wait, so USB audio capture must stay on Host Bus 003 (alsa_input.usb-*).
        self._scan_existing_hardware()
        
        # Udev Hardware Monitor
        self.udev_context = pyudev.Context()
        self.udev_monitor = pyudev.Monitor.from_netlink(self.udev_context)
        self.udev_monitor.filter_by(subsystem='sound')
        self.udev_monitor.filter_by(subsystem='bluetooth')
        self._bt_restart_task = None
        
        # Global App Registry (The OS Manifest)
        # Using lambda functions allows apps to be lazy-loaded into memory only when opened
        self.dashboard = DashboardApp(self.display, self.ui_manager)
        
        self.ui_manager.app_registry = {
            'home': lambda: self.dashboard,
            'lock_screen': lambda: LockScreenApp(self.display, self.ui_manager),
            'recent_tab': lambda: RecentApp(self.display, self.ui_manager),
            'ir_remote': lambda: IRControllerApp(self.display, self.ui_manager, self.db, self.ir_hardware),
            'app_selector': lambda: __import__('src.apps.launcher_app', fromlist=['']).AppLauncherApp(self.display, self.ui_manager, self.db),
            'cartridges': lambda: __import__('src.apps.cartridge_app', fromlist=['']).CartridgeLauncherApp(self.display, self.ui_manager, self.db),
            'music_app': lambda: __import__('src.apps.music_app', fromlist=['']).OfflineMusicApp(self.display, self.ui_manager, self.db),
            'settings': lambda: SettingsApp(self.display, self.ui_manager, self.db),
            'audio_settings': lambda: AudioSettingsApp(self.display, self.ui_manager, self.db),
            'wifi_manager': lambda: WiFiManagerApp(self.display, self.ui_manager, self.db),
            'hotspot_manager': lambda: HotspotManagerApp(self.display, self.ui_manager, self.db),
            'bluetooth_manager': lambda: BluetoothManagerApp(self.display, self.ui_manager, self.db),
            'bt_devices_app': lambda: BluetoothDevicesApp(self.display, self.ui_manager),
            'bt_blocked_app': lambda: BluetoothBlockedApp(self.display, self.ui_manager),
            'bt_codec_app': lambda: BluetoothCodecApp(self.display, self.ui_manager),
            'bt_quality_app': lambda: BluetoothQualityApp(self.display, self.ui_manager)
        }
        
        self.is_running = False
        self.tasks = []

    # Stage: Rust Boot Hardware Auto-Profiler (Adapts weak single/dual-core boards to Minimal/Balanced defaults without overwriting user settings)
    def _run_boot_hw_profile(self) -> dict:
        import json, os
        cores = os.cpu_count() or 1
        prof = {
            "tier": "full" if cores >= 4 else ("balanced" if cores >= 2 else "minimal"),
            "cores": cores,
            "mem_mb": 512,
            "dsp_us": 0,
            "pack_us": 0,
            "rec_fps": 60 if cores >= 4 else (30 if cores >= 2 else 20),
            "rec_pwm": "max" if cores >= 4 else ("normal" if cores >= 2 else "off"),
            "rec_dither": "pwm" if cores >= 4 else ("spatial" if cores >= 2 else "off"),
            "rec_bands": 20 if cores >= 4 else (16 if cores >= 2 else 10),
            "rec_caps": "pwm" if cores >= 4 else ("solid" if cores >= 2 else "off"),
            "dsp_cores": "0,2" if cores >= 4 else "0",
            "bg_cores": "0",
        }
        bench_bin = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "bin", "hw_bench"))
        if os.path.isfile(bench_bin) and os.access(bench_bin, os.X_OK):
            try:
                res = subprocess.run([bench_bin], capture_output=True, text=True, timeout=0.5)
                if res.returncode == 0 and res.stdout.strip():
                    prof.update(json.loads(res.stdout.strip()))
            except Exception as e:
                print(f"[OSKernel] hw_bench fallback ({e})", flush=True)
        # Comment (invariant): Seed hardware-appropriate defaults strictly when keys are absent from SQLite; never overwrite explicit user settings or calibrated oled_pwm_dt.
        sentinel = "__UNSET__"
        defaults_if_unset = (
            ("oled_fps", str(prof["rec_fps"])),
            ("oled_pwm_mode", str(prof["rec_pwm"])),
            ("oled_dither_mode", str(prof["rec_dither"])),
            ("lock_viz_bands", str(prof["rec_bands"])),
            ("lock_viz_cap_style", str(prof["rec_caps"])),
        )
        for k, v in defaults_if_unset:
            if self.db.get_setting(k, sentinel) == sentinel:
                self.db.set_setting(k, v)
        self.db.set_setting("hw_perf_tier", str(prof["tier"]))
        print(
            f"[OSKernel] Hardware Profile: tier={prof['tier']} cores={prof['cores']} mem={prof['mem_mb']}MB "
            f"dsp={prof['dsp_us']}us pack={prof['pack_us']}us dsp_cores={prof['dsp_cores']}",
            flush=True,
        )
        return prof

    def _handle_udev_event(self):
        """Fires instantly on zero-cost hardware interrupts."""
        device = self.udev_monitor.poll(timeout=0)
        if not device or device.action not in ['add', 'remove']: 
            return
        
        # 1. Intercept Bluetooth Hotplug Events (Debounced; ignore ACL link child nodes like hci1:1)
        if device.subsystem == 'bluetooth':
            sys_n = str(device.sys_name or '')
            if sys_n.startswith('hci') and ':' not in sys_n and getattr(device, 'device_type', 'host') != 'link':
                self._debounce_bt_restart()

        # 2. Filter strictly for external USB sound card declarations (ignore internal platform ahub rebinds)
        if device.subsystem == 'sound' and device.get('ID_BUS') == 'usb' and 'ID_MODEL' in device:
            name = device.get('ID_MODEL').replace('_', ' ')
            
            if device.action == 'add':
                self.ui_manager.active_hardware_dac = name
                print(f"[Kernel] Hardware Audio Connected: {name}")
                if hasattr(self, 'sound_manager') and self.sound_manager:
                    self.sound_manager.play("usb_connect")
                asyncio.create_task(self._enforce_audio_routing())
                async def _reapply_wp():
                    await asyncio.sleep(0.8)
                    try:
                        from src.hardware.walkplay_hid import reapply_walkplay_profile
                        if reapply_walkplay_profile(self.db):
                            print(f"[Kernel] Re-applied saved Walkplay Filter & PEQ for {name} (Gain untouched)", flush=True)
                    except Exception as e:
                        print(f"[Kernel] Walkplay reapply error: {e}", flush=True)
                asyncio.create_task(_reapply_wp())
                
                # Execute Auto-Connect trigger on USB DAC plug (Mode 2: On Plug)
                mode = int(self.db.get_setting('bt_autoconnect', '0'))
                target_mac = self.db.get_setting('bt_primary_mac', '')
                if mode == 2 and target_mac:
                    self.ui_manager.bt_user_disconnected = False
                    print(f"[Kernel] Auto-Connect (On Plug) Triggered for {target_mac}", flush=True)
                    subprocess.Popen(["bluetoothctl", "connect", target_mac], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
            elif device.action == 'remove':
                print(f"[Kernel] Hardware Audio Disconnected: {name}")
                self.ui_manager.active_hardware_dac = None
                asyncio.create_task(self._enforce_audio_routing())

    async def _main_loop(self):
        while self.is_running:
            # Single-Clock V-Sync Compositor Gate (drives active marquees at frame_interval & deep-freezes when screen is off)
            if getattr(self.ui_manager, 'is_screen_off', False):
                self.ui_manager.needs_render = False
                self.display.has_active_marquee = False
                self.display.next_marquee_wake = 0.0
                await asyncio.sleep(0.25)
                continue

            current_time = time.monotonic()
            next_wake = getattr(self.display, 'next_marquee_wake', 0.0)
            if next_wake > 0.0 and current_time >= next_wake:
                self.display.next_marquee_wake = 0.0
                self.ui_manager.needs_render = True

            has_marquee = getattr(self.display, 'has_active_marquee', False)
            if self.ui_manager.needs_render or has_marquee:
                import struct as _stm
                shm_m = getattr(self.display, '_shm', None)
                pwm_m = _stm.unpack("<I", shm_m[8:12])[0] if shm_m is not None else 0
                if pwm_m == 1 and shm_m is not None:
                    # Comment (invariant): When 3-phase UI PWM is active (pwm_m==1), Core-1 latches shm_ver strictly on pwm_phase==2 (incrementing shm[20:24] every 19.33ms); triggering composite_frame (~5.3ms) immediately after shm[20:24] increments guarantees shm_ver lands ~13ms BEFORE the next 19.33ms latch, eliminating the 8.33ms free-running timer race that missed pwm_phase==2 and caused 38.7ms double-frame holds (DHP-513).
                    hw_tk = _stm.unpack("<I", shm_m[20:24])[0]
                    last_tk = getattr(self, '_last_vsync_hw_tk', -1)
                    if hw_tk != last_tk or (current_time - self.ui_manager.last_render_time) >= 0.025:
                        self._last_vsync_hw_tk = hw_tk
                        self.ui_manager.last_render_time = current_time
                        await self.ui_manager.composite_frame()
                        elapsed_c = time.monotonic() - current_time
                        await asyncio.sleep(max(0.0008, min(0.013, 0.0165 - elapsed_c)))
                    else:
                        await asyncio.sleep(0.0007)
                else:
                    frame_interval = getattr(self.ui_manager, 'frame_interval', 0.05)
                    elapsed_f = current_time - self.ui_manager.last_render_time
                    if elapsed_f >= frame_interval and self.display.frame_queue.empty():
                        if elapsed_f > frame_interval * 2.0:
                            self.ui_manager.last_render_time = current_time
                        else:
                            self.ui_manager.last_render_time += frame_interval
                        await self.ui_manager.composite_frame()
                    rem = frame_interval - (time.monotonic() - self.ui_manager.last_render_time)
                    await asyncio.sleep(max(0.0008, min(0.015, rem * 0.85)))
            else:
                await asyncio.sleep(0.015)

    async def _enforce_audio_routing(self):
        """Centralized preference-aware audio routing (Handles hotplug, boot, and UI)."""
        try:
            preference = self.db.get_setting("active_dac", "usb")
            usb_present = bool(self.ui_manager.active_hardware_dac)
            
            # Determine physical reality based on preference and hardware presence
            if preference == "usb" and not usb_present:
                search_term = "alsa_output.platform-soc_ahub0"
                print("[OSKernel] USB preferred but disconnected. Falling back to I2S.", flush=True)
            else:
                search_term = "alsa_output.usb" if preference == "usb" else "alsa_output.platform-soc_ahub0"

            # Guarantee ahub0 ASoC DMA link is healthy before polling PipeWire for the I2S sink/source
            await self._heal_ahub0_alsa_driver()

            # Smart loop: PipeWire takes 1-2 seconds to mount a newly inserted USB DAC
            sink_name = ""
            for attempt in range(8):  # Poll every 0.5s for up to 4 seconds
                bash_cmd = f"sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 pactl list short sinks | grep '{search_term}' | awk '{{print $2}}' | head -n 1"
                proc = await asyncio.create_subprocess_shell(
                    bash_cmd, 
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                stdout, _ = await proc.communicate()
                sink_name = stdout.decode().strip()
                
                if sink_name:
                    break
                await asyncio.sleep(0.5)
            
            if sink_name:
                await asyncio.create_subprocess_shell(
                    f"sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 pactl set-default-sink {sink_name}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                # Migrate active playback sink-inputs (MPV, BlueZ, Transparency) to the new sink,
                # skipping internal vip_feed_* streams bound to vip_mic_bus.
                move_cmd = (
                    "sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 python3 - << 'EOF'\n"
                    "import subprocess\n"
                    "out = subprocess.check_output(['pactl', 'list', 'sink-inputs']).decode('utf-8', errors='ignore')\n"
                    "for block in out.split('Sink Input #')[1:]:\n"
                    "    si_id = block.splitlines()[0].strip()\n"
                    "    if 'vip_feed_' in block or 'vip_mic_bus' in block:\n"
                    "        continue\n"
                    f"    subprocess.run(['pactl', 'move-sink-output' if False else 'pactl', 'move-sink-input', si_id, '{sink_name}'][1:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                    "EOF"
                )
                await asyncio.create_subprocess_shell(
                    move_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                print(f"[OSKernel] Enforced audio routing & stream migration to: {sink_name}", flush=True)

                # If routing to internal I2S and not manually killed, re-latch PCM5122 BCK PLL, filter, & gain
                if "ahub0" in sink_name and not getattr(self.ui_manager, 'i2s_manually_killed', False):
                    await self.ui_manager._apply_pcm5122_i2c_gain()

                # Flush cached sink volume and re-apply hybrid I2S/USB gain ceiling on the newly routed sink
                self.ui_manager.last_synced_volumes.pop("@DEFAULT_AUDIO_SINK@", None)
                await self.ui_manager.sync_exclusive_volumes()
            else:
                print(f"[OSKernel] Routing failed: Timeout waiting for {search_term} sink.", flush=True)

            # Enforce I2S-specific DAC L/R swap, persistent vip_mic_bus Mic Feed routing, and 24-bit AHUB Mic DSP state
            await self.ui_manager.sync_i2s_dac_channels()
            await self.ui_manager.sync_transparency_and_mic_feed()
                
            # Request UI render to update the fallback label instantly if menu is open
            active_app = getattr(self.ui_manager, 'current_app', None)
            if active_app and hasattr(active_app, '_rebuild_menu'):
                active_app._rebuild_menu()
                
            self.ui_manager.request_render()
        except Exception as e:
            print(f"[OSKernel] Failed to enforce audio routing: {e}", flush=True)

    def apply_h618_i2s_drive(self, level: int = None) -> bool:
        """
        Live-updates Allwinner H618 Port I drive strength (PI_DRV0 @ 0x0300B134):
        - PI1 (BCLK) & PI2 (WSEL): Set to `level` (default Level 2 / 30mA) to cleanly drive the 3-way split (DAC + 2x Mics).
        - PI3 (DIN)  & PI4 (DOUT): Set to Level 0 (10mA) soft-slew to eliminate point-to-point data overshoot & RF ringing.
        - PI5 (SCL0) & PI6 (SDA0): Set to Level 0 (10mA) soft-slew to stop 400kHz OLED frame-flush spikes from bleeding into the DAC.
        """
        import os, mmap, struct
        if level is None:
            level = int(self.db.get_setting("i2s_drive_level", "2"))
        lvl = max(0, min(3, int(level))) & 0x3
        # PI1=[3:2]=lvl, PI2=[5:4]=lvl, PI3=[7:6]=0, PI4=[9:8]=0, PI5=[11:10]=0, PI6=[13:12]=0
        mask = (lvl << 2) | (lvl << 4)
        try:
            fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
            try:
                with mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x0300B000) as m:
                    p134 = struct.unpack("<I", m[0x134:0x138])[0]
                    m[0x134:0x138] = struct.pack("<I", (p134 & ~0x3FFC) | mask)
                print(f"[OSKernel] Allwinner H618 PI_DRV0 tuned: BCK/WSEL=L{lvl} ({(lvl + 1) * 10}mA), DIN/DOUT/I2C0=L0 (10mA low-EMI slew).", flush=True)
                return True
            finally:
                os.close(fd)
        except Exception as e:
            print(f"[OSKernel] WARNING: Failed to set PI_DRV0 drive strength: {e}", flush=True)
            return False

    def apply_h618_usb_phy_boost(self, mode=None) -> bool:
        """
        Tunes Allwinner H618 USB PHY calibration registers (pmu0..3 +0x18 @ 0x818) from Stock to more aggressive:
        - '0' (Stock):     0x023438e4 (TXVREF=3, TXRISE=4, PREEMP=7)
        - '1' (Gentle):    0x023228e4 (TXVREF=3, TXRISE=2, PREEMP=2, Default)
        - '2' (Low-EMI):   0x021108e4 (TXVREF=1, TXRISE=1, PREEMP=1)
        - '3' (Max-Boost): 0x027778e4 (TXVREF=7, TXRISE=7, PREEMP=7)
        Sanitizes phy_ctrl +0x18 (0x05100418) and pmu0..3 +0x10 (0x810) to 0x00000000 so EHCI SET_ADDRESS never fails with -71,
        and automatically reclaims Bus 003 (480M EHCI) if the hub was ever handed off to Bus 006 (12M OHCI).
        """
        import os, glob, mmap, struct, subprocess, time
        is_live_toggle = (mode is not None)
        if mode is None:
            mode = str(self.db.get_setting("usb_phy_boost", "1"))
        elif isinstance(mode, bool):
            mode = "3" if mode else "0"
        else:
            mode = str(mode)

        profile_map = {
            "0": (0x023438e4, "Stock"),
            "1": (0x023228e4, "Gentle-Slew"),
            "2": (0x021108e4, "Low-EMI"),
            "3": (0x027778e4, "Max-Boost")
        }
        phy_word, label = profile_map.get(mode, (0x023228e4, "Gentle-Slew"))

        # Standard Allwinner H618 USB PMU page bases (pmu0=0x05101800, pmu1=0x05200800, pmu2=0x05310800, pmu3=0x05311800)
        pmu_pages = [0x05101000, 0x05200000, 0x05310000, 0x05311000]
        try:
            if os.path.exists("/proc/iomem"):
                parsed = []
                with open("/proc/iomem", "r") as f:
                    for line in f:
                        if ".phy pmu" in line:
                            start_hex = line.strip().split("-")[0]
                            addr = int(start_hex, 16)
                            if addr > 0:
                                parsed.append(addr & ~0xFFF)
                if parsed:
                    pmu_pages = parsed
        except Exception:
            pass

        def _write_phy_registers(fd_mem):
            # 1. Keep shared phy_ctrl +0x18 (0x05100418) at clean hardware boot 0x00000000
            with mmap.mmap(fd_mem, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x05100000) as m:
                if struct.unpack_from("<I", m, 0x418)[0] != 0:
                    struct.pack_into("<I", m, 0x418, 0x00000000)

            # 2. Keep pmu0..3 +0x10 (0x810) at clean 0x00000000 and write phy_word to the true PHY_TUNE offset +0x18 (0x818)
            for page_base in pmu_pages:
                with mmap.mmap(fd_mem, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=page_base) as m:
                    if struct.unpack_from("<I", m, 0x810)[0] != 0:
                        struct.pack_into("<I", m, 0x810, 0x00000000)
                    struct.pack_into("<I", m, 0x818, phy_word)

        need_ohci_recovery = False
        try:
            fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
            try:
                _write_phy_registers(fd)
                # Inspect EHCI3 PORTSC (0x05311054) Bit 13 (PORT_OWNER = 0x2000) or stranded devices on Bus 006 (OHCI)
                with mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ, offset=0x05311000) as m:
                    ehci3_portsc = struct.unpack_from("<I", m, 0x054)[0]
                if (ehci3_portsc & 0x2000) or bool(glob.glob("/sys/bus/usb/devices/6-[1-9]*")):
                    need_ohci_recovery = True
                print(f"[OSKernel] Allwinner H618 USB PHY (0x818) tuned to 0x{phy_word:08x} ({label}) across {len(pmu_pages)} PMU(s).", flush=True)
            finally:
                os.close(fd)
        except Exception as e:
            print(f"[OSKernel] WARNING: Failed to tune USB PHY: {e}", flush=True)
            return False

        # Reclaim Bus 003 (480Mbps EHCI) if the hub fell onto Bus 006 (12Mbps OHCI), or pulse usbreset on live UI change
        try:
            if need_ohci_recovery:
                print("[OSKernel] Detected USB hub on Bus 006 (OHCI 12M). Reclaiming Bus 003 (EHCI 480M)...", flush=True)
                for pwr in ("/sys/bus/usb/devices/usb3/power/control", "/sys/bus/usb/devices/usb6/power/control"):
                    if os.path.exists(pwr):
                        with open(pwr, "w") as pf:
                            pf.write("on")
                subprocess.run("echo 5311400.usb > /sys/bus/platform/drivers/ohci-platform/unbind 2>/dev/null || true", shell=True)
                subprocess.run("echo 5311000.usb > /sys/bus/platform/drivers/ehci-platform/unbind 2>/dev/null || true", shell=True)
                time.sleep(0.3)
                subprocess.run("echo 5311000.usb > /sys/bus/platform/drivers/ehci-platform/bind 2>/dev/null || true", shell=True)
                fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
                try:
                    _write_phy_registers(fd)
                finally:
                    os.close(fd)
                time.sleep(1.8)
                subprocess.run("echo 5311400.usb > /sys/bus/platform/drivers/ohci-platform/bind 2>/dev/null || true", shell=True)
            elif is_live_toggle:
                for hub_id in ("1a40:0101", "214b:7260"):
                    res = subprocess.run(["sudo", "/usr/bin/usbreset", hub_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if res.returncode == 0:
                        print(f"[OSKernel] Pulsed live USB re-handshake on hub {hub_id} for {label}.", flush=True)
                        break
        except Exception as e:
            print(f"[OSKernel] USB re-handshake warning: {e}", flush=True)

        # Adaptive USB Power Management: keep active trees/hubs/BT awake ('on'), let empty root hubs clock-gate ('auto')
        try:
            for dev in glob.glob("/sys/bus/usb/devices/*"):
                base_name = os.path.basename(dev)
                if ":" in base_name:
                    continue
                pwr_p = os.path.join(dev, "power/control")
                if not os.path.exists(pwr_p):
                    continue
                cls_p = os.path.join(dev, "bDeviceClass")
                cls = open(cls_p).read().strip() if os.path.exists(cls_p) else ""

                if base_name.startswith("usb"):
                    bus_num = base_name[3:]
                    has_children = bool(glob.glob(f"/sys/bus/usb/devices/{bus_num}-[1-9]*"))
                    with open(pwr_p, "w") as pf:
                        pf.write("on" if has_children else "auto")
                else:
                    is_bt = bool(glob.glob(os.path.join(dev, "*:*/bluetooth")))
                    if cls in ("09", "e0") or is_bt:
                        with open(pwr_p, "w") as pf:
                            pf.write("on")
        except Exception:
            pass
        return True

    def apply_axp313a_pmic_mode(self, mode=None) -> bool:
        """
        Dynamically locates the onboard X-Powers AXP313A PMIC (0x36, Chip ID Reg 0x03 == 0x4b)
        and configures DCDC1-3 PWM/Spread-Spectrum (Reg 0x12) and DVM Voltage Ramp Slew (Reg 0x1a):
        - '0' (Stock PFM):  Reg 0x12=0x00, Reg 0x1a=0x20 (Auto PFM burst, instant voltage step)
        - '1' (DVM Smooth): Reg 0x12=0x00, Reg 0x1a=0x23 (Auto PFM + 15.6mV/us DCDC1/2 soft voltage ramp)
        - '2' (Fixed PWM):  Reg 0x12=0x07, Reg 0x1a=0x23 (Continuous 1.5MHz PWM + DVM soft ramp, Default)
        - '3' (Spread PWM): Reg 0x12=0x87, Reg 0x1a=0x23 (1.5MHz Spread-Spectrum PWM + DVM soft ramp)
        """
        import os, glob, subprocess
        if mode is None:
            mode = str(self.db.get_setting("pmic_power_mode", "2"))
        else:
            mode = str(mode)

        profile_map = {
            "0": (0x00, 0x20, "Stock-PFM"),
            "1": (0x00, 0x23, "DVM-Smooth"),
            "2": (0x07, 0x23, "Fixed-PWM+DVM"),
            "3": (0x87, 0x23, "Spread-PWM+DVM")
        }
        reg12, reg1a, label = profile_map.get(mode, (0x07, 0x23, "Fixed-PWM+DVM"))

        # Dynamically discover which I2C bus hosts 0x0036 (typically i2c-6 on H618)
        bus_num = None
        for dev in glob.glob("/sys/bus/i2c/devices/*-0036"):
            base = os.path.basename(dev)
            if "-" in base:
                bus_num = base.split("-")[0]
                break
        if bus_num is None:
            return False

        try:
            chip_id = subprocess.check_output(
                ["sudo", "i2cget", "-f", "-y", str(bus_num), "0x36", "0x03"],
                stderr=subprocess.DEVNULL
            ).decode().strip().lower()
            if chip_id != "0x4b":
                return False

            subprocess.run(["sudo", "i2cset", "-f", "-y", str(bus_num), "0x36", "0x12", f"0x{reg12:02x}"], check=True, stderr=subprocess.DEVNULL)
            subprocess.run(["sudo", "i2cset", "-f", "-y", str(bus_num), "0x36", "0x1a", f"0x{reg1a:02x}"], check=True, stderr=subprocess.DEVNULL)
            print(f"[OSKernel] AXP313A PMIC (i2c-{bus_num} @ 0x36) tuned to {label} (0x12=0x{reg12:02x}, 0x1a=0x{reg1a:02x}).", flush=True)
            return True
        except Exception as e:
            print(f"[OSKernel] WARNING: Failed to tune AXP313A PMIC: {e}", flush=True)
            return False

    def recover_i2c0_bus(self, force: bool = False) -> bool:
        """
        Recovers Allwinner H618 I2C0 (TWI0 @ 0x05002000, PI5=SCL0, PI6=SDA0) when a slave holds SDA low
        or mv64xxx reports 'I2C bus locked, block: 1, time_left: 0' (Errno 110):
        1. Enforces internal Pull-Up (0b01) on PI5 (bits 11:10) and PI6 (bits 13:12) in PI_PULL0 (0x0300B13C).
        2. Bit-bangs 9 SCL clock recovery pulses + STOP condition on PI5/PI6 if either line is stuck low or force=True.
        3. Rebinds 5002000.i2c in mv64xxx_i2c to clear any latched controller timeout state.
        """
        import os, mmap, struct, time, subprocess
        from src.hardware.display import I2C_BUS_LOCK
        with I2C_BUS_LOCK:
            try:
                fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
                try:
                    with mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x0300B000) as m:
                        # Ensure PI5 & PI6 have internal Pull-Up enabled (bits 13:10 = 0x1400)
                        p13c = struct.unpack("<I", m[0x13C:0x140])[0]
                        if (p13c & 0x00003C00) != 0x00001400:
                            m[0x13C:0x140] = struct.pack("<I", (p13c & ~0x00003C00) | 0x00001400)
                            time.sleep(0.001)

                        dat = struct.unpack("<I", m[0x130:0x134])[0]
                        lines_low = ((dat >> 5) & 0x3) != 0x3
                        if not (force or lines_low):
                            return True

                        cfg0_orig = struct.unpack("<I", m[0x120:0x124])[0]
                        # Set PI5 (bits 23:20) = 1 (Output) and PI6 (bits 27:24) = 0 (Input)
                        cfg_rec = (cfg0_orig & ~0x0FF00000) | 0x00100000
                        m[0x120:0x124] = struct.pack("<I", cfg_rec)

                        # Clock 9 SCL recovery pulses on PI5 (Bit 5) at ~50kHz
                        for _ in range(9):
                            d = struct.unpack("<I", m[0x130:0x134])[0]
                            m[0x130:0x134] = struct.pack("<I", d & ~(1 << 5))
                            time.sleep(0.00002)
                            d = struct.unpack("<I", m[0x130:0x134])[0]
                            m[0x130:0x134] = struct.pack("<I", d | (1 << 5))
                            time.sleep(0.00002)

                        # Generate clean STOP condition (SCL=0, SDA=0 -> SCL=1, SDA=0 -> SCL=1, SDA=1)
                        cfg_stop = (cfg0_orig & ~0x0FF00000) | 0x01100000
                        d = struct.unpack("<I", m[0x130:0x134])[0]
                        m[0x130:0x134] = struct.pack("<I", d & ~((1 << 5) | (1 << 6)))
                        m[0x120:0x124] = struct.pack("<I", cfg_stop)
                        time.sleep(0.00005)
                        m[0x130:0x134] = struct.pack("<I", (d & ~(1 << 6)) | (1 << 5))
                        time.sleep(0.00005)
                        m[0x130:0x134] = struct.pack("<I", d | (1 << 5) | (1 << 6))
                        time.sleep(0.00005)

                        # Restore PI5 & PI6 to Alt5 (TWI0 = 0x55 in bits 27:20)
                        cfg_twi = (cfg0_orig & ~0x0FF00000) | 0x05500000
                        m[0x120:0x124] = struct.pack("<I", cfg_twi)
                finally:
                    os.close(fd)

                fd_twi = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
                try:
                    with mmap.mmap(fd_twi, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x05002000) as twi:
                        twi[0x1C:0x20] = struct.pack("<I", 0x01)
                        time.sleep(0.005)
                        twi[0x14:0x18] = struct.pack("<I", 0x28)
                finally:
                    os.close(fd_twi)
                print("[OSKernel] Recovered I2C0 bus (9-clock SCL unwind + PI5/PI6 Pull-Up + TWI0 SRST).", flush=True)
                if hasattr(self, 'display') and self.display:
                    self.display.reset_pins_and_recover()
                # Stage: Post-Recovery PCM5122 DAC Re-Latch (Restores 3-wire BCK PLL & unmutes 0x4C after I2C0 bus unwind)
                if hasattr(self, 'ui_manager') and self.ui_manager and self.ui_manager._is_i2s_effectively_active() and not getattr(self.ui_manager, 'i2s_manually_killed', False):
                    try:
                        asyncio.get_running_loop().create_task(self.ui_manager._apply_pcm5122_i2c_gain())
                    except Exception:
                        pass
                return True
            except Exception as e:
                print(f"[OSKernel] WARNING: I2C0 recovery failed: {e}", flush=True)
                return False

    def _enforce_h618_i2s_pulldowns(self):
        """
        Enforces Allwinner H618 internal silicon pad drive strength, pull resistors, and USB3 PHY tune via /dev/mem:
        - PI_DRV0  (0x0300B134): Saved DB drive level (default Level 3 / 40mA) on PI1-PI2, Level 0 (10mA) on PI3-PI6.
        - PI_PULL0 (0x0300B13C): Pull-Down (0b10) on PI1-PI4 (I2S0 audio) and PI9 (IR TX), Pull-Up (0b01) on PI5-PI6 (I2C0).
        - PH_PULL1 (0x0300B11C): Pull-Up (0b01) on PH16 (Pin 33 active-low IR Receiver RX).
        - USB3_PHY (0x05310410): High-Speed TX Eye-Diagram pre-emphasis & hub autosuspend lock.
        """
        import os, mmap, struct
        self.apply_h618_i2s_drive()
        self.apply_h618_usb_phy_boost()
        self.apply_axp313a_pmic_mode()
        try:
            fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
            try:
                with mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x0300B000) as m:
                    # 1. Port I Pulls (PI_PULL0 @ 0x13C): PI1..PI4 & PI9 Pull-Down (0x000802A8) + PI5..PI6 I2C0 Pull-Up (0x00001400)
                    p13c = struct.unpack("<I", m[0x13C:0x140])[0]
                    m[0x13C:0x140] = struct.pack("<I", (p13c & ~0x000C3FFC) | 0x000816A8)

                    # 2. Port H Pulls (PH_PULL1 @ 0x11C): PH16 (bits 1:0 = 0x1) -> Pull-Up
                    p11c = struct.unpack("<I", m[0x11C:0x120])[0]
                    m[0x11C:0x120] = struct.pack("<I", (p11c & ~0x3) | 0x1)
                print("[OSKernel] Allwinner H618 pulls enforced: PI1-PI4 & PI9 Pull-Down, PI5-PI6 I2C0 Pull-Up, PH16 Pull-Up.", flush=True)
            finally:
                os.close(fd)
            self.recover_i2c0_bus(force=False)
        except Exception as e:
            print(f"[OSKernel] WARNING: Failed to enforce GPIO hardware pulls: {e}", flush=True)

    def enforce_ahub0_24bit_i2s(self, framing_mode: str = None, bclk_inv: bool = None):
        """
        Configures Allwinner H618 AHUB I2S0 slot/sample resolution (I2S0_FMT0 +0x204), BCLK polarity,
        and hardware TX/RX slot maps (I2S0_TX0CHMAP0 +0x234, I2S0_RXCHMAP0 +0x274):
        - '24bit' (ICS-43434 ★): SR=5 (24-bit), SW=7 (32-bit) -> low byte 0x57 (or 0xD7 if BCLK inverted)
        - '20bit' (SPH0645):     SR=4 (20-bit), SW=7 (32-bit) -> low byte 0x47 (or 0xC7 if BCLK inverted)
        - '32bit' (Raw ALSA ⚙):  SR=7 (32-bit), SW=7 (32-bit) -> low byte 0x77 (or 0xF7 if BCLK inverted)
        Locks RXCHMAP0 (+0x274 = 0x03020100) so Mic Slot 0/1 never jumpscare-swaps, and maps TX0CHMAP0 (+0x234)
        according to i2s_dac_swap XOR _tx_fifo_slipped while clearing APBIF_TX0/RX0 FIFO underrun status flags.
        """
        import os, mmap, struct
        if framing_mode is None:
            framing_mode = self.db.get_setting("mic_i2s_framing", "24bit")
        if bclk_inv is None:
            bclk_inv = self.db.get_setting("mic_bclk_edge", "normal") == "invert"

        sr_map = {"24bit": 0x57, "20bit": 0x47, "32bit": 0x77}
        low_byte = sr_map.get(str(framing_mode), 0x57)
        if bclk_inv:
            low_byte |= 0x80  # Bit 7: BCLK_POL = 1 (Inverted BCLK edge for SPH0645)

        dac_swap = self.db.get_setting("i2s_dac_swap", "0") == "1"

        try:
            fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
            try:
                with mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x05097000) as m:
                    # 1. Check & clear APBIF_TX0_STS (+0x110) and APBIF_RX0_STS (+0x130) FIFO underrun/overrun flags
                    tx_sts = struct.unpack("<I", m[0x110:0x114])[0]
                    rx_sts = struct.unpack("<I", m[0x130:0x134])[0]
                    if tx_sts & 0xFF:
                        m[0x110:0x114] = struct.pack("<I", tx_sts)
                    if rx_sts & 0xFF:
                        m[0x130:0x134] = struct.pack("<I", rx_sts)

                    # 2. Enforce 24/20/32-bit sample resolution (I2S0_FMT0 +0x204), zero LSB pad (+0x208), 1-BCLK offset (+0x270)
                    fmt0 = struct.unpack("<I", m[0x204:0x208])[0]
                    if (fmt0 & 0xFF) != low_byte:
                        m[0x204:0x208] = struct.pack("<I", (fmt0 & ~0xFF) | low_byte)
                    if struct.unpack("<I", m[0x208:0x20C])[0] != 0x00000000:
                        m[0x208:0x20C] = struct.pack("<I", 0x00000000)
                    if struct.unpack("<I", m[0x270:0x274])[0] != 0x00110000:
                        m[0x270:0x274] = struct.pack("<I", 0x00110000)

                    # 3. Lock Mic RXCHMAP0 (+0x274) strictly to 0x03020100 (Slot 0->Ch0, Slot 1->Ch1) without mid-stream glitching
                    if struct.unpack("<I", m[0x274:0x278])[0] != 0x03020100:
                        m[0x274:0x278] = struct.pack("<I", 0x03020100)

                    # 4. Enforce DAC TX0CHMAP0 (+0x234): 0x76543201 when swapped/slipped, 0x76543210 when normal
                    tx_slipped = getattr(self, '_tx_fifo_slipped', False)
                    want_tx_swap = dac_swap ^ tx_slipped
                    tx_map = 0x76543201 if want_tx_swap else 0x76543210
                    if struct.unpack("<I", m[0x234:0x238])[0] != tx_map:
                        m[0x234:0x238] = struct.pack("<I", tx_map)
            finally:
                os.close(fd)
        except Exception as e:
            print(f"[OSKernel] WARNING: Failed to enforce AHUB I2S framing: {e}", flush=True)

    def toggle_ahub0_tx_slip_compensation(self) -> bool:
        """Flips the hardware I2S0_TX0CHMAP0 (+0x234) slot order in 1us to un-invert an odd-word APBIF_TX0 FIFO slip."""
        self._tx_fifo_slipped = not getattr(self, '_tx_fifo_slipped', False)
        self.enforce_ahub0_24bit_i2s()
        return self._tx_fifo_slipped

    async def _apply_boot_audio_state(self):
        """Restores the Real-Time CPU, hardware governor settings, and strictly enforces the global exclusive volume baseline before audio plays"""
        import subprocess
        import asyncio
        import os

        # 0a. Enforce Allwinner H618 PIO Pulls, Drive Strength, USB PHY, and PMIC mode
        self._enforce_h618_i2s_pulldowns()
        from src.core.bluetooth_daemon import BluetoothDaemon
        sco_val = "60:12" if BluetoothDaemon.read_mic_mode() == "msbc" else "48:12"
        subprocess.run(['sudo', 'hciconfig', 'hci0', 'scomtu', sco_val], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 0b. Smart Defibrillator: Wake ONLY the USB DAC (supports multi-interface & deconfigured DACs).
        try:
            dac_path = None
            if hasattr(self, 'media_arbiter') and hasattr(self.media_arbiter, '_get_usb_audio_path'):
                dac_path = await self.media_arbiter._get_usb_audio_path()
            if dac_path:
                subprocess.run(['sudo', '/usr/local/bin/toggle_dac.sh', dac_path, '1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
            
        # Give systemd a moment to ensure PipeWire is initialized and the DAC has time to mount
        await asyncio.sleep(2.0)
        
        # 0c. Heal 99-master-tuning.conf (min-quantum=1024, NEON enabled) and 53-no-hearing-protection.conf (4096/48000 on bluez_input)
        pw_cfg = "/home/vipxpert/.config/pipewire/pipewire.conf.d/99-master-tuning.conf"
        wp_cfg = "/home/vipxpert/.config/wireplumber/wireplumber.conf.d/53-no-hearing-protection.conf"
        need_pw_restart = False
        try:
            if os.path.exists(pw_cfg):
                with open(pw_cfg, "r", encoding="utf-8") as f:
                    pt = f.read()
                if "min-quantum = 32" in pt or "disable-neon = true" in pt or "resample.quality = 14" in pt:
                    pt = pt.replace("default.clock.min-quantum = 32", "default.clock.min-quantum = 1024")
                    pt = pt.replace("resample.quality = 14", "resample.quality = 4")
                    pt = pt.replace("resample.disable-neon = true", "resample.disable-neon = false")
                    with open(pw_cfg, "w", encoding="utf-8", newline="\n") as f:
                        f.write(pt)
                    need_pw_restart = True
            if os.path.exists(wp_cfg):
                with open(wp_cfg, "r", encoding="utf-8") as f:
                    wt = f.read()
                bad_rule = '{\n                node.name = "~bluez_input.*"\n            }\n            {\n                node.name = "~bluez_output.*"\n            }'
                if bad_rule in wt:
                    wt = wt.replace(bad_rule, '{\n                node.name = "~bluez_output.*"\n            }')
                    with open(wp_cfg, "w", encoding="utf-8", newline="\n") as f:
                        f.write(wt)
                    need_pw_restart = True
            if need_pw_restart:
                print("[OSKernel] Healed PipeWire/WirePlumber quantum & 4096/48000 A2DP latency configs. Restarting user audio...", flush=True)
                subprocess.run(
                    "sudo -u vipxpert env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
                    "systemctl --user restart pipewire wireplumber pipewire-pulse",
                    shell=True, check=False
                )
                await asyncio.sleep(2.0)
        except Exception as e:
            print(f"[OSKernel] Config heal warning: {e}", flush=True)

        # 1. Enforce Active Audio Output (Preference-Aware) & lock PipeWire 1024-sample quantum floor
        await self._enforce_audio_routing()
        subprocess.run(
            "sudo -u vipxpert env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-metadata -n settings 0 clock.min-quantum 1024 >/dev/null 2>&1; "
            "sudo -u vipxpert env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-metadata -n settings 0 clock.quantum 1024 >/dev/null 2>&1",
            shell=True, check=False
        )

        rt_state = self.db.get_setting("rt_cpu_audio", "0")
        self.apply_rt_cpu_state(rt_state == "1")

        # 3. Force absolute volume math enforcement before any audio is allowed to play
        try:
            await self.ui_manager.sync_exclusive_volumes()
            print("[OSKernel] Enforced initial DSP exclusive volume math.", flush=True)
        except Exception as e:
            print(f"[OSKernel] Failed to enforce initial volume math: {e}", flush=True)

        # Stage: Boot-Time Bluetooth Auto-Connect Guard (Skips redundant connect call when phone is already connected)
        bt_mode = int(self.db.get_setting('bt_autoconnect', '0'))
        target_mac = self.db.get_setting('bt_primary_mac', '')
        if target_mac and (bt_mode == 1 or (bt_mode == 2 and self.ui_manager.active_hardware_dac)):
            if not getattr(self, '_boot_bt_autoconnect_done', False):
                self._boot_bt_autoconnect_done = True
                already_conn = getattr(self.ui_manager, 'bt_session_connected', False) or bool(getattr(self.bt_daemon, 'media_state', {}).get('connected_mac'))
                if not already_conn:
                    print(f"[OSKernel] Boot Auto-Connect (mode={bt_mode}) triggered for {target_mac}...", flush=True)
                    await asyncio.create_subprocess_shell(
                        f"bluetoothctl connect {target_mac}",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )

    async def _heal_ahub0_alsa_driver(self) -> bool:
        """
        Detects and self-heals the Allwinner H618 Linux 6.12 ASoC boot race where `soc:ahub0_mach`
        probes before `soc:ahub0_plat` finishes `devm_snd_dmaengine_pcm_register()`, leaving `ahubi2s0`
        without its `dmaengine_pcm` component (`rate mask 0x80000000` -> `-22 EINVAL` on `pcm_open`).
        Safely ignores `Errno 16 (EBUSY)` when PipeWire is already actively streaming to the DAC.
        """
        import os, errno, asyncio

        def _probe_pcm_ok() -> bool:
            if not os.path.exists("/dev/snd/pcmC0D0p"):
                return False
            try:
                fd = os.open("/dev/snd/pcmC0D0p", os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
                return True
            except OSError as e:
                # EBUSY (16) means PipeWire already holds the substream open and healthy
                if e.errno == errno.EBUSY:
                    return True
                return False

        if _probe_pcm_ok():
            return False

        print("[OSKernel] WARNING: Allwinner ahub0 failed pcm_open (-22 EINVAL). Rebinding ASoC machine...", flush=True)
        try:
            # Stage 1: Rebind soc:ahub0_mach alone first so it attaches to the already-registered dmaengine_pcm
            with open("/sys/bus/platform/drivers/sunxi-snd-mach/unbind", "w") as f:
                f.write("soc:ahub0_mach")
            await asyncio.sleep(0.1)
            with open("/sys/bus/platform/drivers/sunxi-snd-mach/bind", "w") as f:
                f.write("soc:ahub0_mach")
            await asyncio.sleep(0.2)

            # Stage 2: If still broken, cycle both soc:ahub0_plat and soc:ahub0_mach sequentially
            if not _probe_pcm_ok():
                with open("/sys/bus/platform/drivers/sunxi-snd-mach/unbind", "w") as f:
                    f.write("soc:ahub0_mach")
                with open("/sys/bus/platform/drivers/sunxi-snd-plat-ahub/unbind", "w") as f:
                    f.write("soc:ahub0_plat")
                await asyncio.sleep(0.15)
                with open("/sys/bus/platform/drivers/sunxi-snd-plat-ahub/bind", "w") as f:
                    f.write("soc:ahub0_plat")
                await asyncio.sleep(0.15)
                with open("/sys/bus/platform/drivers/sunxi-snd-mach/bind", "w") as f:
                    f.write("soc:ahub0_mach")
                await asyncio.sleep(0.25)

            # Flush stale PipeWire node IDs so UIManager re-discovers the newly bound card0 sink/source
            self.ui_manager.i2s_sink_id = None
            self.ui_manager.i2s_source_id = None
            print(f"[OSKernel] Allwinner ahub0 ASoC rebind complete (healthy={_probe_pcm_ok()}).", flush=True)
            await asyncio.sleep(1.0)
            return True
        except Exception as e:
            print(f"[OSKernel] ERROR: Failed to self-heal ahub0 ASoC driver: {e}", flush=True)
            return False

    def apply_rt_cpu_state(self, enable: bool):
        """
        Configures split-core Real-Time Audio & USB IRQ isolation:
        - Core 3 (Hex 8): PipeWire `data-loop*` threads ONLY (SCHED_FIFO 88) + I2S DMA (`dma-controller`/`ahub`).
        - Core 2 (Hex 4): USB Controllers (`ehci_hcd`/`ohci_hcd`/`musb-hdrc`) so Bluetooth L2CAP & USB hotplugs never starve or deadlock Core 3.
        - Cores 0-2: PipeWire `main-loop` (`pipewire` control/socket thread) kept at SCHED_OTHER to prevent `futex_lock_pi` hotplug deadlocks.
        """
        import os, glob, subprocess
        usb_irq_cmd = "grep -iE 'ehci_hcd|ohci_hcd|musb-hdrc' /proc/interrupts | awk '{print $1}' | tr -d ':'"
        dma_irq_cmd = "grep -iE 'dma-controller|ahub' /proc/interrupts | awk '{print $1}' | tr -d ':'"
        try:
            # Always keep the kernel 95% RT safety valve (950000) so an ALSA hotplug hiccup can never wedge the CPU
            subprocess.run("echo 950000 | sudo tee /proc/sys/kernel/sched_rt_runtime_us > /dev/null 2>&1 || true", shell=True, check=False)

            if enable:
                subprocess.run(["sudo", "cpufreq-set", "-c", "0", "-g", "performance"], check=False)
                subprocess.run(["sudo", "cpufreq-set", "-c", "2", "-g", "performance"], check=False)
                subprocess.run(["sudo", "cpufreq-set", "-c", "3", "-g", "performance"], check=False)
                subprocess.run("for f in /sys/devices/system/cpu/cpu3/cpuidle/state*/disable; do [ -f \"$f\" ] && echo 1 | sudo tee \"$f\" > /dev/null; done", shell=True, check=False)

                # Pin USB Controller IRQs (BT L2CAP + USB DAC) to Core 2 (Hex 4)
                try:
                    usb_irqs = subprocess.check_output(usb_irq_cmd, shell=True).decode('utf-8').strip()
                    for irq in usb_irqs.split('\n'):
                        if irq.strip():
                            subprocess.run(f"echo '4' | sudo tee /proc/irq/{irq.strip()}/smp_affinity > /dev/null", shell=True, check=False)
                except Exception:
                    pass

                # Pin Allwinner I2S DMA IRQs strictly to Core 3 (Hex 8)
                try:
                    dma_irqs = subprocess.check_output(dma_irq_cmd, shell=True).decode('utf-8').strip()
                    for irq in dma_irqs.split('\n'):
                        if irq.strip():
                            subprocess.run(f"echo '8' | sudo tee /proc/irq/{irq.strip()}/smp_affinity > /dev/null", shell=True, check=False)
                except Exception:
                    pass
            else:
                subprocess.run(["sudo", "cpufreq-set", "-c", "2", "-g", "schedutil"], check=False)
                subprocess.run(["sudo", "cpufreq-set", "-c", "3", "-g", "schedutil"], check=False)
                subprocess.run("for f in /sys/devices/system/cpu/cpu3/cpuidle/state*/disable; do [ -f \"$f\" ] && echo 0 | sudo tee \"$f\" > /dev/null; done", shell=True, check=False)

                for cmd in (usb_irq_cmd, dma_irq_cmd):
                    try:
                        irqs = subprocess.check_output(cmd, shell=True).decode('utf-8').strip()
                        for irq in irqs.split('\n'):
                            if irq.strip():
                                subprocess.run(f"echo 'f' | sudo tee /proc/irq/{irq.strip()}/smp_affinity > /dev/null", shell=True, check=False)
                    except Exception:
                        pass

            # Apply thread-specific scheduling to PipeWire (ONLY `data-loop*` gets SCHED_FIFO 88 on Core 3)
            pids = subprocess.check_output(["pgrep", "-x", "pipewire"]).decode('utf-8').strip().split()
            for pid in pids:
                if enable:
                    subprocess.run(f"sudo prlimit --pid {pid} --rtprio=99:99 --memlock=unlimited:unlimited", shell=True, check=False, stderr=subprocess.DEVNULL)
                    subprocess.run(f"echo {pid} | sudo tee /sys/fs/cgroup/cgroup.procs > /dev/null 2>&1 || true", shell=True, check=False)
                    for comm_path in glob.glob(f"/proc/{pid}/task/*/comm"):
                        try:
                            tid = comm_path.split('/')[-2]
                            t_name = open(comm_path).read().strip()
                            if t_name.startswith("data-loop"):
                                subprocess.run(["sudo", "chrt", "-f", "-p", "88", tid], check=False, stderr=subprocess.DEVNULL)
                                subprocess.run(["sudo", "taskset", "-p", "-c", "3", tid], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            else:
                                # Comment (invariant): Never include Core 1 in PipeWire's main-loop affinity (`0,2`, never `0-2`); OLED_Compositor spins on Core 1 at nice(-20) and starves PipeWire's socket/A2DP timer while PipeWire wakeups delay 51.73Hz PWM latches (DP-495).
                                subprocess.run(["sudo", "chrt", "-o", "-p", "0", tid], check=False, stderr=subprocess.DEVNULL)
                                subprocess.run(["sudo", "taskset", "-p", "-c", "0,2", tid], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            continue
                else:
                    subprocess.run(["sudo", "chrt", "-a", "-o", "-p", "0", pid], check=False, stderr=subprocess.DEVNULL)
                    subprocess.run(["sudo", "taskset", "-a", "-p", "-c", "0,2,3", pid], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # Comment (invariant): Never cram main.py + pipewire + vscode onto single Core {0} (saturated Core 0 >100% in HR-503); give main.py + audio control daemons Cores {0,2} while keeping Core {1} strictly for OLED_Compositor and Core {3} strictly for SCHED_FIFO 88 data-loop.0.
            my_pid = os.getpid()
            disp_proc = getattr(getattr(self, "display", None), "_render_proc", None)
            oled_pid = getattr(disp_proc, "pid", -1) if disp_proc else -1
            for comm_p in glob.glob("/proc/[0-9]*/comm"):
                try:
                    p_int = int(comm_p.split("/")[2])
                    if p_int == oled_pid:
                        continue
                    c_name = open(comm_p).read().strip()
                    target_cpus = {0, 2} if (p_int == my_pid or c_name in ("bluetoothd", "wireplumber", "pipewire-pulse")) else None
                    if target_cpus is not None:
                        for te in os.listdir(f"/proc/{p_int}/task"):
                            if te.isdigit() and int(te) != oled_pid:
                                try:
                                    os.sched_setaffinity(int(te), target_cpus)
                                except Exception:
                                    pass
                except Exception:
                    pass
        except Exception as e:
            print(f"[OSKernel] Non-fatal error applying RT Audio state: {e}", flush=True)

    async def run(self):
        self.is_running = True
        print("VipXpert Minimal OS starting...")
        self.keypad.setup()
        
        # Apply initial OS brightness and display inversion settings to hardware
        initial_brightness = int(self.db.get_setting("brightness", "128"))
        self.display.set_brightness(initial_brightness)
        invert_disp = self.db.get_setting("invert_display", "0") == "1"
        if hasattr(self.display, 'set_invert'):
            self.display.set_invert(invert_disp)
        from src.ui import components as ui_components
        ui_components.DEFAULT_MARQUEE_SPEED = float(self.db.get_setting("marquee_speed", "3.0"))
        
        # Dispatch the asynchronous boot state sync for Real-Time audio
        self.tasks.append(asyncio.create_task(self._apply_boot_audio_state()))
        
        # Mount the udev monitor to the active asyncio loop
        self.udev_monitor.start()
        asyncio.get_running_loop().add_reader(self.udev_monitor.fileno(), self._handle_udev_event)
        
        # Mount the primary view stack using the new router
        await self.ui_manager.switch_app('home', fallback_app=self.dashboard)
        
        ## Deploy structural background engine tasks
        self.tasks.append(asyncio.create_task(self.ui_manager.telemetry_ticker_loop()))
        self.tasks.append(asyncio.create_task(self.ui_manager.activity_timeout_loop()))
        self.tasks.append(asyncio.create_task(self.keypad.poll_keys(self.ui_manager.inject_key)))
        self.tasks.append(asyncio.create_task(self.bt_daemon.start()))
        self.tasks.append(asyncio.create_task(self.media_arbiter.start()))
        self.tasks.append(asyncio.create_task(self._wifi_policy_daemon()))
        self.tasks.append(asyncio.create_task(self.power_monitor.start()))
        
        try:
            await self._main_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self):
        self.is_running = False
        print("\nShutting down OS gracefully...")
        
        # 1. Brutally kill the headless MPV process and any Transparency/Mix loopbacks to release the systemd cgroup
        try:
            if hasattr(self, 'media_arbiter'):
                if self.media_arbiter.local_audio.process:
                    self.media_arbiter.local_audio.process.kill()
                for _attr in ('_adb_bridge_proc', '_usb_loop_proc'):
                    _p = getattr(self.media_arbiter, _attr, None)
                    if _p and _p.returncode is None:
                        try:
                            _p.kill()
                        except Exception:
                            pass
            if hasattr(self, 'ui_manager') and hasattr(self.ui_manager, 'stop_all_mix_streams'):
                await self.ui_manager.stop_all_mix_streams()
        except Exception:
            pass
            
        # 2. Release DBus cleanly
        try:
            if hasattr(self, 'bt_daemon'):
                await self.bt_daemon.cleanup()
        except Exception:
            pass

        # 3. Stop hardware interrupt monitors
        try:
            asyncio.get_running_loop().remove_reader(self.udev_monitor.fileno())
            if hasattr(self.udev_monitor, 'stop'):
                self.udev_monitor.stop()
        except Exception:
            pass

        # 4. Cancel all background loops
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
            
        # 5. Release hardware
        self.keypad.cleanup()
        self.display.clear()
        await self.display.show()
        
        # 6. Nuclear exit to instantly release the systemd cgroup (bypassing hung I2C/DBus threads)
        import os
        os._exit(0)

    async def _wifi_policy_daemon(self):
        """Continuously monitors Wi-Fi state and enforces power-saving policies."""
        mode = self.db.get_setting("wifi_boot_policy", "Smart")
        if mode == "Smart":
            print("[Kernel] Smart Policy: Forcing Wi-Fi ON at boot for initial scan.")
            try:
                proc = await asyncio.create_subprocess_exec('nmcli', 'radio', 'wifi', 'on', stderr=asyncio.subprocess.PIPE)
                _, err = await proc.communicate()
                if proc.returncode != 0:
                    print(f"[Kernel] Failed to enable Wi-Fi on boot (nmcli): {err.decode('utf-8').strip()}")
                await asyncio.sleep(3) # Give the hardware radio time to initialize
            except Exception as e:
                print(f"[Kernel] Boot Radio Error: {e}")
                
        offline_start_time = None
        ps_locked_off = False
        ssh_idle_since = None
        vsc_reaped = False
        offline_start_time = None

        def _has_active_ssh():
            for tcp_path in ("/proc/net/tcp", "/proc/net/tcp6"):
                try:
                    with open(tcp_path, "r") as f:
                        for line in f.readlines()[1:]:
                            parts = line.split()
                            if len(parts) >= 4 and parts[1].endswith(":0016") and parts[3] == "01":
                                return True
                except Exception:
                    pass
            return False

        while self.is_running:
            try:
                mode = self.db.get_setting("wifi_boot_policy", "Smart")
                try:
                    with open("/sys/class/net/wlan0/operstate", "r") as f:
                        wlan_up = (f.read().strip() == "up")
                except Exception:
                    wlan_up = False

                # Comment (invariant): Never pin sshd/.vscode-server/copilot to Core 3! Core 3 runs PipeWire's SCHED_FIFO 88 data-loop.0 + I2S DMA IRQs, and 80% CPU bursts from VSCode/SSH on Core 3 caused Bluetooth A2DP audio hiccups (P-500). Confine dev/SSH background trees + late-spawned main.py threads strictly to Core 0.
                if wlan_up and _has_active_ssh():
                    ssh_idle_since = None
                    vsc_reaped = False
                    import os as _os
                    _my_pid_s = str(_os.getpid())
                    for _pid_s in _os.listdir("/proc"):
                        if not _pid_s.isdigit():
                            continue
                        try:
                            if _pid_s == _my_pid_s:
                                _ui_aff = {0, 2} if (_os.cpu_count() or 1) >= 4 else {0}
                                for _tid_s in _os.listdir(f"/proc/{_pid_s}/task"):
                                    if _tid_s.isdigit() and _os.sched_getaffinity(int(_tid_s)) != _ui_aff:
                                        _os.sched_setaffinity(int(_tid_s), _ui_aff)
                                continue
                            with open(f"/proc/{_pid_s}/cmdline", "rb") as _cf:
                                _cmd = _cf.read().decode("utf-8", errors="ignore")
                            if any(_k in _cmd for _k in (".vscode-server", "copilot", "ripgrep", "/rg\x00", "sshd:")):
                                _tdir = f"/proc/{_pid_s}/task"
                                for _tid_s in (_os.listdir(_tdir) if _os.path.isdir(_tdir) else (_pid_s,)):
                                    if _tid_s.isdigit():
                                        _tid = int(_tid_s)
                                        try:
                                            if _os.sched_getaffinity(_tid) != {0}:
                                                _os.sched_setaffinity(_tid, {0})
                                            _os.setpriority(_os.PRIO_PROCESS, _tid, 15)
                                        except Exception:
                                            pass
                        except Exception:
                            pass
                elif not vsc_reaped:
                    if ssh_idle_since is None:
                        ssh_idle_since = time.time()
                    elif time.time() - ssh_idle_since >= (15 if not wlan_up else 30):
                        proc_reap = await asyncio.create_subprocess_shell(
                            "pkill -9 -f '\\.vscode-server|copilot-runtime' 2>/dev/null || true",
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                        )
                        await proc_reap.wait()
                        vsc_reaped = True

                # Only run iw power_save off once per connection session instead of forking every 5s
                if wlan_up and not ps_locked_off:
                    proc_iw = await asyncio.create_subprocess_exec(
                        '/usr/sbin/iw', 'dev', 'wlan0', 'set', 'power_save', 'off',
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                    )
                    await proc_iw.wait()
                    ps_locked_off = (proc_iw.returncode == 0)
                elif not wlan_up:
                    ps_locked_off = False

                if mode == "Keep Alive":
                    offline_start_time = None
                    await asyncio.sleep(5)
                    continue
                    
                if mode == "Offline":
                    offline_start_time = None
                    if wlan_up:
                        print("[Kernel] Wi-Fi Policy: Offline. Disabling radio.")
                        proc_off = await asyncio.create_subprocess_exec('nmcli', 'radio', 'wifi', 'off', stderr=asyncio.subprocess.PIPE)
                        _, err = await proc_off.communicate()
                        if proc_off.returncode != 0:
                            print(f"[Kernel] Failed to disable Wi-Fi (Offline Policy): {err.decode('utf-8').strip()}")
                    await asyncio.sleep(5)
                    continue
                    
                # Smart Mode: Fast-path via sysfs operstate=='up' (0 subprocesses while connected!)
                if wlan_up:
                    offline_start_time = None # Reset fuse, we are connected
                else:
                    # Check if the radio is actually powered on before counting down
                    proc_radio = await asyncio.create_subprocess_exec(
                        'nmcli', '-t', 'radio', 'wifi', stdout=asyncio.subprocess.PIPE
                    )
                    out_radio, _ = await proc_radio.communicate()
                    
                    if b'enabled' in out_radio:
                        if offline_start_time is None:
                            offline_start_time = time.time()
                        elif time.time() - offline_start_time >= 45:
                            print("[Kernel] Smart Fuse Blown: Wi-Fi disconnected for 45s. Killing radio.")
                            proc_smart_off = await asyncio.create_subprocess_exec('nmcli', 'radio', 'wifi', 'off', stderr=asyncio.subprocess.PIPE)
                            _, err = await proc_smart_off.communicate()
                            if proc_smart_off.returncode != 0:
                                print(f"[Kernel] Failed to disable Wi-Fi (Smart Policy): {err.decode('utf-8').strip()}")
                            offline_start_time = None
                    else:
                        offline_start_time = None # Radio is already off, reset timer
                
                await asyncio.sleep(5)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Kernel] Wi-Fi Policy Daemon error: {e}")
                await asyncio.sleep(5)

    def _scan_existing_hardware(self):
        """Performs a one-time sweep on boot to catch already-plugged-in DACs."""
        import pyudev
        context = pyudev.Context()
        
        # Iterate through all currently connected sound devices
        for device in context.list_devices(subsystem='sound'):
            # Filter explicitly for USB DACs to ignore internal board audio
            if device.get('ID_BUS') == 'usb' and 'ID_MODEL' in device:
                name = device.get('ID_MODEL').replace('_', ' ')
                self.ui_manager.active_hardware_dac = name
                print(f"[Kernel] Boot-time USB DAC Found: {name}")
                try:
                    from src.hardware.walkplay_hid import reapply_walkplay_profile
                    if reapply_walkplay_profile(self.db):
                        print(f"[Kernel] Boot-time Walkplay Filter & PEQ restored for {name} (Gain untouched)", flush=True)
                except Exception as e:
                    print(f"[Kernel] Boot-time Walkplay reapply error: {e}", flush=True)
                break

    def _debounce_bt_restart(self):
        # Stage: Immediate Hotplug Recovery Latch (Flags _bt_recovering before 1s settling fuse to protect session reconnect state)
        if getattr(self, '_bt_recovering_active', False):
            return
        self._bt_recovering = True
        if self._bt_restart_task and not self._bt_restart_task.done():
            self._bt_restart_task.cancel()
        self._bt_restart_task = asyncio.create_task(self._restart_bt_daemon())

    async def _restart_bt_daemon(self):
        # Stage: Sequenced Bluetooth Hotplug & Daemon Re-Initialization
        if getattr(self, '_bt_recovering_active', False):
            return
        try:
            self._bt_recovering = True
            await asyncio.sleep(1.0)  # 1-second fuse for hardware settling
            self._bt_recovering_active = True
            print("[Kernel] Triggering graceful Bluetooth hotplug restart...", flush=True)

            # 1. Safely teardown old DBus connections FIRST with a 2.0s timeout so a hung bluetoothd never freezes for 30s
            if self.bt_daemon:
                try:
                    await asyncio.wait_for(self.bt_daemon.cleanup(), timeout=2.0)
                except Exception:
                    pass

            # 2. Check if this hotplug event was caused by a crash, and heal it before proceeding
            await self._heal_bluetooth_hardware()

            # 3. Let the kernel fully process the new hci0 adapter
            await asyncio.sleep(0.5)

            # 4. Instantiate a fresh daemon instance and mount it
            self.bt_daemon = BluetoothDaemon(self.ui_manager, self.db)

            # Re-link the arbiter to the new daemon instance
            if hasattr(self, 'media_arbiter') and self.media_arbiter:
                self.media_arbiter.bt_daemon = self.bt_daemon

            # 5. Start the DBus loops anew
            self.tasks.append(asyncio.create_task(self.bt_daemon.start()))

            # 6. If Transparency or Mic Feed was active, ensure its loopback graph & 24-bit AHUB registers stay alive
            if getattr(self.ui_manager, 'transparency_enabled', False) or getattr(self.ui_manager, 'mic_feed_mode', 'mic') != 'mic':
                await self.ui_manager.sync_transparency_and_mic_feed()

            # 7. Restore connection after hardware reset ONLY if not intentionally disconnected by user
            target_mac = self.db.get_setting('bt_primary_mac', '')
            bt_mode = int(self.db.get_setting('bt_autoconnect', '0'))
            user_disc = getattr(self.ui_manager, 'bt_user_disconnected', False)
            was_conn = getattr(self.ui_manager, 'bt_session_connected', False)
            should_reconnect = bool(target_mac) and (not user_disc) and (was_conn or bt_mode in (1, 2))
            if should_reconnect:
                await asyncio.sleep(1.5)
                print(f"[Kernel] Restoring Bluetooth session to {target_mac} after hardware reset...", flush=True)
                await asyncio.create_subprocess_shell(
                    f"bluetoothctl connect {target_mac}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
            else:
                print(f"[Kernel] Skipping auto-reconnect after reset (user_disconnected={user_disc}, was_connected={was_conn}, mode={bt_mode}).", flush=True)

            print("[Kernel] Bluetooth hotplug recovery complete.", flush=True)
        except asyncio.CancelledError:
            pass  # Fuse was blown by a newer interrupt event, wait for the next timeout
        except Exception as e:
            print(f"[Kernel] Failed to recover Bluetooth after hotplug: {e}", flush=True)
        finally:
            self._bt_recovering_active = False
            self._bt_recovering = False

    def _find_usb_hci_state(self, hciconfig_text: str):
        """Parses `hciconfig -a` output to locate the physical `Bus: USB` adapter, ignoring dummy `Bus: UART` ghosts."""
        import re
        blocks = re.split(r'(?=^hci\d+:)', hciconfig_text, flags=re.MULTILINE)
        for block in blocks:
            if "Bus: USB" in block:
                iface = block.split(':', 1)[0].strip()
                is_up = "UP RUNNING" in block
                return iface, is_up
        return None, False

    async def _heal_bluetooth_hardware(self, force_check: bool = False):
        """Checks for firmware panics, missing Bus: USB adapters, or soft-blocks, executing an auto-defibrillator sequence if needed."""
        try:
            proc_rf = await asyncio.create_subprocess_shell(
                "rfkill list bluetooth",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            out_rf, _ = await proc_rf.communicate()
            soft_blocked = b"Soft blocked: yes" in out_rf

            proc_hci = await asyncio.create_subprocess_shell(
                "hciconfig -a",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            out_hci, _ = await proc_hci.communicate()
            usb_iface, usb_is_up = self._find_usb_hci_state(out_hci.decode('utf-8', errors='ignore'))

            if soft_blocked or not usb_iface or (force_check and not usb_is_up):
                print(f"[Kernel] Bluetooth USB adapter unhealthy (iface={usb_iface}, up={usb_is_up}, blocked={soft_blocked}). Initiating Auto-Defibrillator...", flush=True)

                await asyncio.create_subprocess_shell("sudo rfkill unblock bluetooth", stdout=asyncio.subprocess.DEVNULL)
                await asyncio.create_subprocess_shell("sudo modprobe -r btusb", stdout=asyncio.subprocess.DEVNULL)
                await asyncio.sleep(1.0)
                await asyncio.create_subprocess_shell("sudo modprobe btusb", stdout=asyncio.subprocess.DEVNULL)

                # Poll for up to 4.5s specifically for the `Bus: USB` HCI interface to finish loading rtl8761bu_fw.bin
                hci_iface = None
                for _ in range(15):
                    await asyncio.sleep(0.3)
                    proc = await asyncio.create_subprocess_shell("hciconfig -a", stdout=asyncio.subprocess.PIPE)
                    out, _ = await proc.communicate()
                    found_iface, _ = self._find_usb_hci_state(out.decode('utf-8', errors='ignore'))
                    if found_iface:
                        hci_iface = found_iface
                        break

                if not hci_iface:
                    print("[Kernel] Auto-Defibrillator failed: No Bus: USB HCI interface found after btusb reload.", flush=True)
                    return

                await asyncio.create_subprocess_shell(f"sudo hciconfig {hci_iface} up", stdout=asyncio.subprocess.DEVNULL)
                await asyncio.create_subprocess_shell("sudo systemctl restart bluetooth", stdout=asyncio.subprocess.DEVNULL)
                await asyncio.sleep(1.2)
                await asyncio.create_subprocess_shell("bluetoothctl power on", stdout=asyncio.subprocess.DEVNULL)

                print(f"[Kernel] Auto-Defibrillator complete. {hci_iface} (Bus: USB) revived and powered on.", flush=True)

                target_mac = self.db.get_setting('bt_primary_mac', '')
                bt_mode = int(self.db.get_setting('bt_autoconnect', '0'))
                user_disc = getattr(self.ui_manager, 'bt_user_disconnected', False)
                was_conn = getattr(self.ui_manager, 'bt_session_connected', False)
                if target_mac and (not user_disc) and (was_conn or bt_mode in (1, 2)):
                    print(f"[Kernel] Restoring Bluetooth connection to {target_mac}...", flush=True)
                    await asyncio.create_subprocess_shell(f"bluetoothctl connect {target_mac}", stdout=asyncio.subprocess.DEVNULL)
        except Exception as e:
            print(f"[Kernel] Failed to execute Bluetooth auto-defibrillator: {e}", flush=True)
