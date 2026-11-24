# /vipxpert_minimal_os/src/apps/dashboard.py
import os
import socket
import asyncio
from src.ui.base_app import BaseApp

class DashboardApp(BaseApp):
    """
    Default Home Screen application. Fetches and displays core hardware 
    metrics (CPU temperature, network IP) utilizing the declarative LayoutEngine.
    """
    def __init__(self, display, ui_manager):
        super().__init__(display)
        self.ui = ui_manager
        self.cpu_temp = "0.0°C"
        self.ram_usage = "N/A"
        self.ip4_address = "Loading..."
        self.ip6_address = None
        self.last_net_check = 0.0

    async def on_start(self):
        self.scroll_offset = 0
        if hasattr(self, 'layout_engine'):
            self.layout_engine.selected_index = 0
        await self.update()

    async def on_stop(self):
        pass

    async def handle_key(self, action: str, key: str):
        # Filter for universal continuous actions only
        if action in ['DOWN', 'REPEAT']:
            if key == '2':
                await self.ui.adjust_pi_volume(1)
                return
            elif key == '8':
                await self.ui.adjust_pi_volume(-1)
                return
            elif key == 'B': # Scroll Down continuously
                self.scroll_offset = getattr(self, 'scroll_offset', 0) + 1
                await self.update()
                return
            elif key == 'A': # Scroll Up continuously
                self.scroll_offset = max(0, getattr(self, 'scroll_offset', 0) - 1)
                await self.update()
                return
            elif key == '9':
                await self.ui.bt_daemon.adjust_bt_volume(4)
                return
            elif key == '7':
                await self.ui.bt_daemon.adjust_bt_volume(-4)
                return

        if action != 'CLICK':
            return
            
        if key == '1':
            await self.ui.media_arbiter.restart_dac()
            return
        elif key == '5':
            state = self.ui.media_arbiter.get_now_playing()
            current_status = state.get('status', 'stopped')
            if hasattr(current_status, 'value'): current_status = current_status.value
            
            cmd = 'Pause' if current_status == 'playing' else 'Play'
            await self.ui.media_arbiter.send_action(cmd)
            return
        elif key == '6':
            await self.ui.media_arbiter.send_action('Next')
            return
        elif key == '4':
            await self.ui.media_arbiter.send_action('Previous')
            return
            
        if key == 'C':
            await self.ui.switch_app('app_selector')

    def _refresh_metrics(self):
        """Parses local system endpoints natively without spawning kernel subprocesses."""
        import time
        current_time = time.time()
        
        # 1. CPU Temp & RAM Usage: Pure file reads (Execute in < 1ms, 0 kernel forks)
        try:
            if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                    raw_temp = int(f.read().strip())
                    self.cpu_temp = f"{raw_temp / 1000.0:.1f}°C"
            else:
                self.cpu_temp = "N/A"
        except Exception:
            self.cpu_temp = "Err"

        try:
            if os.path.exists("/proc/meminfo"):
                mem_total = 0
                mem_avail = 0
                with open("/proc/meminfo", "r") as mf:
                    for line in mf:
                        if line.startswith("MemTotal:"):
                            mem_total = int(line.split()[1])
                        elif line.startswith("MemAvailable:"):
                            mem_avail = int(line.split()[1])
                        if mem_total and mem_avail:
                            break
                if mem_total > 0:
                    used_mb = (mem_total - mem_avail) // 1024
                    pct_ram = int(((mem_total - mem_avail) / mem_total) * 100)
                    self.ram_usage = f"{used_mb}M ({pct_ram}%)"
        except Exception:
            self.ram_usage = "N/A"

        # 2. Network Check: Throttled to every 10 seconds to save CPU
        if current_time - self.last_net_check >= 10.0:
            self.last_net_check = current_time
            
            # Native IPv4 fetch via dummy UDP socket (0 kernel forks)
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    # Target doesn't need to be reachable, it just forces OS to resolve active routing interface
                    s.connect(("10.255.255.255", 1))
                    self.ip4_address = s.getsockname()[0]
            except Exception:
                self.ip4_address = "Offline"

            # Native IPv6 fetch via procfs (0 kernel forks)
            try:
                self.ip6_address = None
                if os.path.exists("/proc/net/if_inet6"):
                    with open("/proc/net/if_inet6", "r") as f:
                        for line in f:
                            parts = line.strip().split()
                            if len(parts) >= 6 and parts[5] != "lo":  # Skip loopback
                                raw_ipv6 = parts[0]
                                self.ip6_address = ":".join([raw_ipv6[i:i+4] for i in range(0, 32, 4)])
                                break
            except Exception:
                self.ip6_address = None

    async def update(self):
        """Invoked by the UI Manager telemetry loop every 1 second."""
        # Decouple synchronous metric gathering (IP/Temp) from the UI thread to prevent 25ms lag spikes
        if not hasattr(self, '_metric_task_running') or not self._metric_task_running:
            self._metric_task_running = True
            
            async def background_fetch():
                try:
                    await asyncio.to_thread(self._refresh_metrics)
                except Exception:
                    pass
                finally:
                    self._metric_task_running = False
            asyncio.create_task(background_fetch())
            
        # Ensure fallback attributes exist during the very first boot frame 
        # before the background thread finishes its first sweep.
        if getattr(self, 'ip4_address', None) is None:
            self.ip4_address = "Loading..."
            self.cpu_temp = "--.-°C"
        if getattr(self.ui, 'pi_volume_str', None) is None and not getattr(self, '_vol_init_sent', False):
            self._vol_init_sent = True
            asyncio.create_task(self.ui.adjust_pi_volume(0))
        self.ui.request_render()

    def _build_layout(self):
        """Compiles dashboard rows and time-driven marquee slices on every compositor frame."""
        if getattr(self, 'ip4_address', None) is None:
            self.ip4_address = "Loading..."
            self.cpu_temp = "--.-°C"
        media = self.ui.media_arbiter.get_now_playing()
        if not hasattr(self, 'scroll_offset'):
            self.scroll_offset = 0

        self.layout.header_text = "VipXpert OS"
        self.layout.header_right_cb = self.ui.draw_top_right_battery
        full_body = []
        from src.ui.components import scroll_text

        # Helper to safely unpack dbus_next.signature.Variant objects
        raw_title = media.get('title', 'No Media')
        title_str = str(raw_title.value if hasattr(raw_title, 'value') else raw_title)
        
        raw_artist = media.get('artist', '')
        artist_val = raw_artist.value if hasattr(raw_artist, 'value') else raw_artist
        artist_str = ", ".join(str(a) for a in artist_val) if isinstance(artist_val, list) else str(artist_val)
        
        raw_status = media.get('status', 'stopped')
        status_str = str(raw_status.value if hasattr(raw_status, 'value') else raw_status)
        
        if title_str and title_str != 'No Media':
            icon = "►" if status_str == 'playing' else "||"
            
            # Row 1: Title
            display_title = scroll_text(title_str, max_chars=14)
            full_body.append((f"{icon} ", display_title, ""))
            
            # Row 2: Artist (Indented to group visually under the title)
            if artist_str and artist_str not in ['None', '']:
                display_artist = scroll_text(artist_str, max_chars=14)
                full_body.append(("  ", display_artist, ""))
                
        # Fetch volume silently once on boot if not yet populated
        if getattr(self.ui, 'pi_volume_str', None) is None and not getattr(self, '_vol_init_sent', False):
            self._vol_init_sent = True
            asyncio.create_task(self.ui.adjust_pi_volume(0))

        from src.ui.components import scroll_text
        
        bt_vol_raw = self.ui.bt_daemon.media_state.get('bt_volume')
        bt_str = f"{int((bt_vol_raw / 127) * 100)}%" if bt_vol_raw is not None else "N/A"
        pi_str = getattr(self.ui, 'pi_volume_str', 'N/A')
        
        # Combine and scroll the volume string (11 chars in 12pt fusion-pixel = ~84px, fitting cleanly inside 94px column)
        vol_combined = f"Pi:{pi_str} BT:{bt_str}"
        display_vol = scroll_text(vol_combined, max_chars=11)
        
        # Safely extract the active codec
        raw_codec = self.ui.bt_daemon.media_state.get('codec', 'N/A')
        codec_str = str(raw_codec.value if hasattr(raw_codec, 'value') else raw_codec)

        # Stage: Core Hardware Telemetry Rows (Battery % is pinned permanently at top-right of header via UIManager.draw_top_right_battery)
        full_body.append(("Vol: ", display_vol, ""))
        full_body.append(("CPU: ", f"{self.cpu_temp}", ""))
        if getattr(self, 'ram_usage', 'N/A') != 'N/A':
            full_body.append(("RAM: ", f"{self.ram_usage}", ""))
        
        # Display IPv4 and IPv6 in two separate scrollable rows
        display_ip4 = scroll_text(self.ip4_address, max_chars=12)
        full_body.append(("IP4: ", display_ip4, ""))
        
        if getattr(self, 'ip6_address', None):
            display_ip6 = scroll_text(self.ip6_address, max_chars=12)
            full_body.append(("IP6: ", display_ip6, ""))
            
        if self.ui._is_i2s_effectively_active():
            if getattr(self.ui, 'i2s_manually_killed', False):
                dac_label = "I2S (Killed)"
            elif self.ui.db.get_setting("active_dac", "usb") == "usb":
                dac_label = "PCM5122 (Auto)"
            else:
                dac_label = "PCM5122 I2S"
            full_body.append(("DAC: ", dac_label, ""))
        elif self.ui.active_hardware_dac:
            full_body.append(("DAC: ", f"{self.ui.active_hardware_dac[:14]}", ""))
        else:
            full_body.append(("DAC: ", "PCM5122 I2S", ""))
            
        full_body.append(("Codec: ", codec_str, ""))
            
        # --- UNIFIED VIEWPORT SLICING ---
        capacity = self.layout.get_body_capacity()
        max_scroll = max(0, len(full_body) - capacity)
        
        # Auto-correct bounds (e.g. if Bluetooth disconnects and the list suddenly shrinks)
        self.scroll_offset = min(self.scroll_offset, max_scroll)
        
        # Dynamically update the footer to indicate scrollability
        if max_scroll > 0:
            self.layout.footer_text = "[*] Lock   [A/B] v"
        else:
            self.layout.footer_text = "[*] Lock   [C] Apps"

        import time as _tm
        font_h = self.layout.font_h
        target_px = float(self.scroll_offset * font_h)
        now_t = _tm.monotonic()
        dt = max(0.004, min(0.08, now_t - getattr(self, "_last_scroll_t", now_t - 0.016)))
        self._last_scroll_t = now_t
        smooth_on = self.ui.db.get_setting("ui_smooth_scroll", "1") != "0"
        cur_px = float(getattr(self, "_anim_scroll_px", target_px))
        cur_px = max(0.0, min(float(max_scroll * font_h), cur_px))
        if smooth_on and abs(target_px - cur_px) > 0.35:
            cur_px += (target_px - cur_px) * (1.0 - (2.718281828 ** (-24.0 * dt)))
            if abs(target_px - cur_px) <= 0.35:
                cur_px = target_px
            else:
                self.display.has_active_marquee = True
        else:
            cur_px = target_px
        self._anim_scroll_px = cur_px
        if abs(cur_px - target_px) > 0.01:
            self.layout.full_scroll_lines = full_body
            self.layout.scroll_px = cur_px
            self.layout.scrollbar = (len(full_body), capacity, cur_px / float(font_h))
        else:
            self.layout.full_scroll_lines = None
            self.layout.scroll_px = None
            self.layout.body_lines = full_body[self.scroll_offset : self.scroll_offset + capacity]
            self.layout.scrollbar = (len(full_body), capacity, float(self.scroll_offset))

    def _draw_state(self):
        """Compositor hook to compile time-driven marquee slices and paint the dashboard layout to RAM."""
        self._build_layout()
        self.layout.render()
