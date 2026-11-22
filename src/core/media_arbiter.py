# /vipxpert_minimal_os/src/core/media_arbiter.py
import asyncio
import time
import json
import os

class LocalAudioDaemon:
    """Headless MPV wrapper controlled via IPC socket."""
    def __init__(self, ui_manager):
        self.ui = ui_manager
        self.socket_path = '/tmp/mpv_ipc.sock'
        self.process = None
        self.state = {
            'status': 'stopped',
            'title': 'No Media',
            'artist': '',
            'position': 0.0,
            'time-pos': 0.0,
            'duration': 0.0,
            'playlist-pos': 0,
            'path': '',
            'source': 'local',
            'updated_at': time.time()
        }

    async def start(self):
        """Spawns MPV in idle mode listening to an IPC socket."""
        print("[LocalAudio] Booting MPV daemon...", flush=True)
        try:
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)
        except Exception as e:
            print(f"[LocalAudio] WARNING: Could not delete socket: {e}", flush=True)
            
        # Rebuild environment variables so systemd doesn't blind MPV to the audio server
        env = os.environ.copy()
        if 'XDG_RUNTIME_DIR' not in env:
            env['XDG_RUNTIME_DIR'] = f"/run/user/{os.getuid()}"
            
        try:
            # Added --loop-playlist=force so the tape naturally rewinds without shutting MPV down
            self.process = await asyncio.create_subprocess_exec(
                'mpv', '--idle', '--no-video', '--vo=null', '--ao=pulse', '--loop-playlist=force', f'--input-ipc-server={self.socket_path}',
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            self._listener_task = asyncio.create_task(self._listen_loop())
            
        except Exception as e:
            print(f"[LocalAudio] FATAL: MPV subprocess failed to start! {e}", flush=True)

    async def _listen_loop(self):
        """Connects to MPV natively, subscribes to properties, and live-updates the state."""
        timeout = 10
        while not os.path.exists(self.socket_path) and timeout > 0:
            await asyncio.sleep(0.5)
            timeout -= 1
            
        if timeout <= 0:
            print("[Arbiter] Fatal: MPV Socket never created!", flush=True)
            return
            
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
            
            # Subscribe to the exact properties the Lock Screen and Session Manager need
            props = ["idle-active", "pause", "media-title", "time-pos", "duration", "playlist-pos", "path", "metadata"]
            for i, prop in enumerate(props):
                req = json.dumps({"command": ["observe_property", i + 1, prop]}) + "\n"
                writer.write(req.encode())
            await writer.drain()
            
            while True:
                line = await reader.readline()
                if not line: break
                
                try:
                    event = json.loads(line.decode().strip())
                    if event.get('event') == 'property-change':
                        name = event.get('name')
                        data = event.get('data')
                        
                        if name == 'idle-active':
                            self.state['status'] = 'stopped' if data else 'playing'
                        elif name == 'pause' and self.state['status'] != 'stopped':
                            self.state['status'] = 'paused' if data else 'playing'
                        elif name == 'media-title':
                            self.state['title'] = str(data) if data else 'No Media'
                        elif name == 'time-pos':
                            pos_val = float(data) if data else 0.0
                            self.state['position'] = pos_val
                            self.state['time-pos'] = pos_val
                            self.state['updated_at'] = time.time()
                        elif name == 'duration':
                            self.state['duration'] = float(data) if data else 0.0
                        elif name == 'playlist-pos':
                            self.state['playlist-pos'] = int(data) if data is not None else 0
                        elif name == 'path':
                            self.state['path'] = str(data) if data else ''
                        elif name == 'metadata' and isinstance(data, dict):
                            self.state['artist'] = data.get('artist', data.get('ARTIST', ''))
                except Exception:
                    pass
        except Exception as e:
            print(f"[MPV Listener] Error: {e}", flush=True)

    async def send_command(self, cmd_list):
        """Sends raw JSON commands cleanly via native asyncio."""
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
            payload = json.dumps({"command": cmd_list}) + "\n"
            writer.write(payload.encode())
            await writer.drain()
            
            # Await response to prevent Broken Pipe
            await reader.readline()
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


class MediaArbiter:
    """Referees the 'Last Started Wins' rule and unifies track data for the Lock Screen."""
    def __init__(self, bt_daemon, db, ui_manager):
        self.bt_daemon = bt_daemon
        self.db = db
        self.ui_manager = ui_manager
        self.local_audio = LocalAudioDaemon(self.ui_manager)
        self.active_source = 'NONE'  # 'BT', 'LOCAL', 'USB', or 'NONE'
        self.last_bt_status = 'stopped'
        self.last_local_status = 'stopped'
        self.last_usb_playing = False
        self._usb_loop_proc = None
        self._usb_source_node = None
        self._adb_bridge_proc = None
        self._adb_state = {'connected': False, 'serial': '', 'model': '', 'playing': False, 'peak': 0, 'updated_at': 0.0}
        self.usb_state = {'status': 'stopped', 'title': 'USB Audio In', 'artist': 'USB-C / Hub', 'position': 0.0, 'duration': 0.0, 'source': 'usb', 'codec': 'PCM', 'updated_at': time.time()}
        self.last_audio_activity = time.time()
        self.is_dac_suspended = False
        self.manual_usb_kill = False

    async def start(self):
        """Boots the Local Daemon safely."""
        await self.local_audio.start()
        asyncio.create_task(self._monitor_loop())
        asyncio.create_task(self._dac_power_monitor())
        asyncio.create_task(self._adb_bridge_supervisor())

    async def _get_usb_audio_path(self):
        """Dynamically scans the kernel driver map, multi-interface USB class 01, or deconfigured leaf devices to find the USB DAC."""
        import glob
        proc = await asyncio.create_subprocess_shell(
            "ls /sys/bus/usb/drivers/snd-usb-audio/ 2>/dev/null | grep ':' | head -n 1 | cut -d':' -f1",
            stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        path = stdout.decode().strip()
        if path:
            return path
        try:
            deconfigured_candidate = ""
            for dev in os.listdir('/sys/bus/usb/devices/'):
                if '-' not in dev or ':' in dev:
                    continue
                dev_dir = f"/sys/bus/usb/devices/{dev}"
                # 1. Scan all interfaces (:1.0, :1.1, etc.) to catch composite HID+Audio DACs
                for cls_file in glob.glob(f"{dev_dir}/{dev}:*/bInterfaceClass"):
                    try:
                        with open(cls_file, 'r') as f:
                            if f.read().strip() == '01':
                                return dev
                    except Exception:
                        continue
                # 2. Catch deconfigured leaf devices (bConfigurationValue == 0) that aren't hubs (09) or wireless/BT (e0)
                cfg_file = f"{dev_dir}/bConfigurationValue"
                dcls_file = f"{dev_dir}/bDeviceClass"
                if os.path.exists(cfg_file):
                    try:
                        cfg_val = open(cfg_file, 'r').read().strip()
                        dcls_val = open(dcls_file, 'r').read().strip() if os.path.exists(dcls_file) else ""
                        if cfg_val == '0' and dcls_val not in ('09', 'e0'):
                            deconfigured_candidate = dev
                    except Exception:
                        pass
            if deconfigured_candidate:
                return deconfigured_candidate
        except Exception:
            pass
        return ""

    async def set_usb_dac_power(self, wake: bool, manual: bool = True) -> bool:
        """Manually or automatically wakes/kills the USB DAC via toggle_dac.sh, honoring session-scoped manual kill state."""
        if wake:
            dac_path = getattr(self, '_last_dac_path', None) or await self._get_usb_audio_path()
            if not dac_path:
                return False
            proc_wake = await asyncio.create_subprocess_shell(
                f"sudo /usr/local/bin/toggle_dac.sh {dac_path} 1",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )
            _, err = await proc_wake.communicate()
            if proc_wake.returncode != 0:
                print(f"[MediaArbiter] Failed to wake USB DAC: {err.decode('utf-8').strip()}", flush=True)
                return False
            self.is_dac_suspended = False
            self.manual_usb_kill = False
            for _ in range(30):
                await asyncio.sleep(0.1)
                proc = await asyncio.create_subprocess_shell(
                    r"wpctl status | grep -A 5 'Sinks:' | grep '\*'",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                out, _ = await proc.communicate()
                if proc.returncode != 0 and not out:
                    break
                if b"Built-in" not in out and out.strip():
                    break
            await self.ui_manager.sync_exclusive_volumes()
            return True
        else:
            dac_path = await self._get_usb_audio_path() or getattr(self, '_last_dac_path', None)
            if not dac_path:
                return False
            self._last_dac_path = dac_path
            proc_sleep = await asyncio.create_subprocess_shell(
                f"sudo /usr/local/bin/toggle_dac.sh {dac_path} 0",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )
            _, err = await proc_sleep.communicate()
            if proc_sleep.returncode != 0:
                print(f"[MediaArbiter] Failed to suspend USB DAC: {err.decode('utf-8').strip()}", flush=True)
                return False
            self.is_dac_suspended = True
            if manual:
                self.manual_usb_kill = True
            return True

    async def _dac_power_monitor(self):
        """Background loop monitoring audio idle time to enforce dynamic USB DAC autosuspend."""
        while True:
            await asyncio.sleep(5)
            # If the user manually killed the USB DAC in this session, stay hands-off until manually woken
            if self.manual_usb_kill:
                continue

            import glob
            timeout_dac = int(self.db.get_setting("timeout_dac", "900"))
            current_state = self.get_now_playing().get('status', 'stopped')
            is_hardware_active = False
            for st_path in glob.glob("/proc/asound/card*/pcm*p/sub*/status"):
                try:
                    with open(st_path, "r") as sf:
                        if "RUNNING" in sf.read():
                            is_hardware_active = True
                            break
                except Exception:
                    pass
            
            if current_state == 'playing' or is_hardware_active:
                self.last_audio_activity = time.time()
                if self.is_dac_suspended:
                    await self.set_usb_dac_power(wake=True, manual=False)
                continue
                
            if timeout_dac > 0 and not self.is_dac_suspended:
                elapsed = time.time() - self.last_audio_activity
                if elapsed >= timeout_dac:
                    await self.set_usb_dac_power(wake=False, manual=False)

    async def _set_bt_mute(self, mute: bool):
        """Finds the active Bluetooth sink-input stream and ducks its volume to 0% to prevent suspension."""
        env = os.environ.copy()
        if 'XDG_RUNTIME_DIR' not in env:
            env['XDG_RUNTIME_DIR'] = f"/run/user/{os.getuid()}"
            
        try:
            # Safely extract the exact Sink Input ID (e.g., "607") of the Bluetooth stream
            bash_cmd = "pactl list sink-inputs | grep -e 'Sink Input #' -e 'bluez_input' | grep -B1 'bluez_input' | head -n 1 | awk '{print $3}' | tr -d '#'"
            proc = await asyncio.create_subprocess_shell(
                bash_cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await proc.communicate()
            stream_id = stdout.decode().strip()
            
            if stream_id:
                # Use set-sink-input-volume to duck the app-level stream!
                val = "0%" if mute else "100%"
                await asyncio.create_subprocess_shell(
                    f"pactl set-sink-input-volume {stream_id} {val}",
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
        except Exception:
            pass

    def force_source(self, source: str):
        """Updates visual routing and spawns the sequenced audio crossover."""
        self.active_source = source
        asyncio.create_task(self._apply_crossover(source))
        
    # Stage: USB Audio Source Probe & Loopback Crossover (Routes USB audio input and enforces Last-Active Takeover)
    async def _set_usb_mute(self, mute: bool):
        val = "0%" if mute else "100%"
        cmd = (
            "sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 python3 - << 'EOF'\n"
            "import subprocess\n"
            "out = subprocess.check_output(['pactl', 'list', 'sink-inputs']).decode('utf-8', errors='ignore')\n"
            "for block in out.split('Sink Input #')[1:]:\n"
            "    if 'vip_usb_out' in block:\n"
            "        si_id = block.splitlines()[0].strip()\n"
            f"        subprocess.run(['pactl', 'set-sink-input-volume', si_id, '{val}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "EOF"
        )
        await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    async def _discover_usb_capture_node(self) -> str:
        """Finds any PipeWire USB audio capture node (alsa_input.usb-*) attached via the USB-C Host hub."""
        # Comment (workaround): Pre-check /proc/asound/card*/usbid + pcm*c in pure Python so _monitor_loop never forks sudo+pactl every 1.5s when no USB audio capture hardware is plugged in.
        import glob, os
        has_usb_cap = False
        for usbid_p in glob.glob("/proc/asound/card*/usbid"):
            card_dir = os.path.dirname(usbid_p)
            if glob.glob(os.path.join(card_dir, "pcm*c")):
                has_usb_cap = True
                break
        if not has_usb_cap:
            return ""
        proc = await asyncio.create_subprocess_shell(
            "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 pactl list short sources 2>/dev/null | awk '{print $2}' | grep '^alsa_input\\.usb' | head -n 1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
        return out.decode('utf-8', errors='ignore').strip()

    async def _sync_usb_audio_loopback(self):
        """Maintains the PipeWire vip_usb_in -> vip_usb_out loopback bridge whenever USB Audio Mode is enabled and a USB capture source is present."""
        usb_mode = self.db.get_setting("usb_audio_mode", "auto")
        usb_node = "" if usb_mode == "off" else await self._discover_usb_capture_node()
        if not usb_node:
            if self._usb_loop_proc and self._usb_loop_proc.returncode is None:
                try:
                    self._usb_loop_proc.terminate()
                except Exception:
                    pass
            self._usb_loop_proc = None
            if self._usb_source_node is not None:
                self._usb_source_node = None
                await asyncio.create_subprocess_shell(
                    "pkill -9 -f 'pw-loopback.*vip_usb' 2>/dev/null || true",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
            return ""

        chk = await asyncio.create_subprocess_shell(
            "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-link -i 2>/dev/null | grep -q 'vip_usb_in:input_FL'",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await chk.wait()
        need_spawn = (self._usb_source_node != usb_node) or (not self._usb_loop_proc) or (self._usb_loop_proc.returncode is not None) or (chk.returncode != 0)
        if need_spawn:
            await asyncio.create_subprocess_shell(
                "pkill -9 -f 'pw-loopback.*vip_usb' 2>/dev/null || true",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.sleep(0.08)
            loop_cmd = (
                "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-loopback "
                f"--capture-props='node.name=vip_usb_in node.target={usb_node} node.latency=1024/48000 node.force-quantum=1024 stream.dont-remix=true audio.position=[FL FR]' "
                "--playback-props='node.name=vip_usb_out media.name=vip_usb_out node.latency=1024/48000 node.force-quantum=1024 stream.dont-remix=true audio.position=[FL FR]'"
            )
            self._usb_loop_proc = await asyncio.create_subprocess_shell(
                loop_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            self._usb_source_node = usb_node
            await asyncio.sleep(0.25)
            await self._set_usb_mute(self.active_source != 'USB' and usb_mode != 'on')
        return usb_node

    def _read_adb_audio_state(self) -> dict:
        """Reads live ADB USB audio bridge telemetry if fresh (<3.0s old)."""
        state_path = "/tmp/vipxpert_adb_audio.json"
        try:
            with open(state_path, "r") as f:
                data = json.load(f)
            if (time.time() - float(data.get("updated_at", 0.0))) < 3.0:
                self._adb_state = data
                return data
        except Exception:
            pass
        self._adb_state = {'connected': False, 'serial': '', 'model': '', 'playing': False, 'peak': 0, 'updated_at': 0.0}
        return self._adb_state

    async def _adb_bridge_supervisor(self):
        """Keeps tools/adb_audio_bridge.py running in background when USB Audio Mode is enabled so ADB phone audio coexists with Bluetooth."""
        bridge_script = "/home/vipxpert/vipxpert_minimal_os/tools/adb_audio_bridge.py"
        py_bin = "/home/vipxpert/vipxpert_minimal_os/venv/bin/python3"
        await asyncio.create_subprocess_shell(
            "pkill -9 -f 'tools/adb_audio_bridge.py' 2>/dev/null || true",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        import glob
        while True:
            usb_mode = self.db.get_setting("usb_audio_mode", "auto")
            has_adb_hw = False
            if usb_mode != "off":
                for if_dir in glob.glob("/sys/bus/usb/devices/*:*"):
                    try:
                        if (
                            open(os.path.join(if_dir, "bInterfaceClass")).read().strip().lower() == "ff"
                            and open(os.path.join(if_dir, "bInterfaceSubClass")).read().strip().lower() == "42"
                            and open(os.path.join(if_dir, "bInterfaceProtocol")).read().strip().lower() == "01"
                        ):
                            has_adb_hw = True
                            break
                    except Exception:
                        continue
            if usb_mode == "off" or not has_adb_hw:
                if self._adb_bridge_proc and self._adb_bridge_proc.returncode is None:
                    try:
                        self._adb_bridge_proc.terminate()
                    except Exception:
                        pass
                self._adb_bridge_proc = None
            else:
                if (not self._adb_bridge_proc) or (self._adb_bridge_proc.returncode is not None):
                    if os.path.exists(bridge_script):
                        self._adb_bridge_proc = await asyncio.create_subprocess_exec(
                            py_bin, bridge_script,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL
                        )
            await asyncio.sleep(2.0)

    async def trigger_usb_takeover(self) -> bool:
        """Manually or automatically activates USB Audio Mode takeover (last-active source wins)."""
        usb_node = await self._sync_usb_audio_loopback()
        adb_st = self._read_adb_audio_state()
        if not usb_node and not adb_st.get('connected') and not getattr(self.ui_manager, 'active_hardware_dac', None):
            return False
        if adb_st.get('connected'):
            self.usb_state['title'] = (adb_st.get('model') or "Android USB")[:22]
            self.usb_state['artist'] = "USB ADB Audio"
        else:
            hw_label = getattr(self.ui_manager, 'active_hardware_dac', None) or "USB-C Audio In"
            self.usb_state['title'] = hw_label[:22]
            self.usb_state['artist'] = "USB-C Hub Audio"
        self.usb_state['status'] = 'playing'
        self.usb_state['updated_at'] = time.time()
        self._usb_forced_active = True
        self.was_local_playing = False
        self.force_source('USB')
        return True

    async def _poll_usb_audio_source(self) -> bool:
        import glob
        usb_mode = self.db.get_setting("usb_audio_mode", "auto")
        if usb_mode == "off":
            return False
        adb_st = self._read_adb_audio_state()
        if adb_st.get("connected") and adb_st.get("playing"):
            return True
        if getattr(self, '_usb_forced_active', False) or usb_mode == "on":
            return bool(self._usb_source_node or adb_st.get("connected"))
        # Comment (decision): Latch newly attached UAC capture hardware for immediate Auto takeover, but clear _usb_plug_latched in _apply_crossover when BT or LOCAL starts later so an idle USB-C hub mic does not permanently block BT/LOCAL fallback.
        prev_uac = getattr(self, '_last_seen_uac_node', "")
        cur_uac = self._usb_source_node or ""
        if cur_uac != prev_uac:
            self._last_seen_uac_node = cur_uac
            self._usb_plug_latched = bool(cur_uac)
        if cur_uac and getattr(self, '_usb_plug_latched', False) and self.active_source in ('NONE', 'USB'):
            return True
        if not cur_uac:
            for st_p in glob.glob("/proc/asound/card*/pcm*c/sub*/status"):
                if "/card0/" in st_p or "/card1/" in st_p:
                    continue
                try:
                    if "RUNNING" in open(st_p).read():
                        return True
                except Exception:
                    pass
        return False

    async def _apply_crossover(self, source: str):
        """Sequences the mutes to safely account for PipeWire node creation delays."""
        usb_always_on = self.db.get_setting("usb_audio_mode", "auto") == "on"
        if source == 'LOCAL':
            self._usb_forced_active = False
            self._usb_plug_latched = False
            await self._set_bt_mute(True)
            await self._set_usb_mute(not usb_always_on)
            await self.local_audio.send_command(['set_property', 'mute', False])
            await self.local_audio.send_command(['set_property', 'pause', False])
        elif source == 'BT':
            self._usb_forced_active = False
            self._usb_plug_latched = False
            await self.local_audio.send_command(['set_property', 'pause', True])
            await self._set_usb_mute(not usb_always_on)
            # Give PipeWire a fraction of a second to build the dynamic node before trying to unmute it!
            await asyncio.sleep(0.5)
            await self._set_bt_mute(False)
        elif source == 'USB':
            await self.local_audio.send_command(['set_property', 'pause', True])
            await self._set_bt_mute(True)
            await self._set_usb_mute(False)

    async def _monitor_loop(self):
        """Monitors signals, enforces Last-Active Takeover across BT/LOCAL/USB, and handles physical tape rewinds."""
        usb_poll_tick = 0
        while True:
            await asyncio.sleep(0.5)
            usb_poll_tick = (usb_poll_tick + 1) % 3
            if usb_poll_tick == 0:
                await self._sync_usb_audio_loopback()

            self.bt_daemon = getattr(self.ui_manager, 'bt_daemon', None) or self.bt_daemon
            bt_ms = self.bt_daemon.media_state if self.bt_daemon else {}
            bt_status = bt_ms.get('status', 'stopped')
            if hasattr(bt_status, 'value'): bt_status = bt_status.value
            bt_conn = bool(bt_ms.get('connected_mac') or bt_ms.get('player_path') or getattr(self.ui_manager, 'bt_session_connected', False)) and not getattr(self.ui_manager, 'bt_user_disconnected', False)
            local_status = self.local_audio.state.get('status', 'stopped')

            bt_playing = (bt_status == 'playing')
            local_playing = (local_status == 'playing')
            usb_playing = await self._poll_usb_audio_source()
            self.usb_state['status'] = 'playing' if usb_playing else 'stopped'
            if usb_playing:
                if self._adb_state.get('connected') and (self._adb_state.get('playing') or getattr(self, '_usb_forced_active', False)):
                    self.usb_state['title'] = (self._adb_state.get('model') or "Android USB")[:22]
                    self.usb_state['artist'] = "USB ADB Audio"
                else:
                    hw_label = getattr(self.ui_manager, 'active_hardware_dac', None) or "USB Audio In"
                    self.usb_state['title'] = hw_label[:22]
                    self.usb_state['artist'] = "USB-C Hub Audio"
                self.usb_state['updated_at'] = time.time()

            if bt_conn and self.active_source == 'NONE' and not local_playing and not usb_playing:
                self.active_source = 'BT'
            elif not bt_conn and self.active_source == 'BT':
                if usb_playing:
                    self.force_source('USB')
                else:
                    self.active_source = 'LOCAL' if local_status in ('playing', 'paused') else 'NONE'
            
            bt_just_started = bt_playing and not getattr(self, 'last_bt_playing', False)
            bt_just_stopped = not bt_playing and getattr(self, 'last_bt_playing', False)
            local_just_started = local_playing and not getattr(self, 'last_local_playing', False)
            usb_just_started = usb_playing and not getattr(self, 'last_usb_playing', False)
            
            # --- 1. TAPE REWIND TRAP ---
            current_idx = self.local_audio.state.get('playlist-pos')
            last_idx = getattr(self, 'last_playlist_pos', current_idx)
            if current_idx == 0 and last_idx is not None and last_idx > 0:
                # End of tape hit 0! Pause instantly.
                asyncio.create_task(self.local_audio.send_command(['set_property', 'pause', True]))
            self.last_playlist_pos = current_idx
            
            # --- 2. LAST-ACTIVE TAKEOVER & NOTIFICATION ROLLBACK ---
            # Comment (decision): Whichever audio source starts or connects later (BT, LOCAL, or USB) takes over the active output sink, while short (<2.5s) BT notification tones still roll back to LOCAL.
            if usb_just_started:
                self.was_local_playing = False
                self.force_source('USB')
            elif bt_just_started:
                self.timestamp_interrupted = time.time()
                self.was_local_playing = (self.active_source == 'LOCAL' and local_playing)
                self.force_source('BT')
            elif local_just_started:
                self.was_local_playing = False
                self.force_source('LOCAL')
            elif bt_just_stopped and getattr(self, 'was_local_playing', False):
                if (time.time() - getattr(self, 'timestamp_interrupted', 0)) < 2.5:
                    self.force_source('LOCAL')
                self.was_local_playing = False
            elif self.active_source == 'LOCAL' and not local_playing:
                if usb_playing:
                    self.force_source('USB')
                elif bt_playing:
                    self.force_source('BT')
            elif self.active_source == 'BT' and not bt_playing:
                if usb_playing:
                    self.force_source('USB')
                elif local_playing:
                    self.force_source('LOCAL')
            elif self.active_source == 'USB' and not usb_playing:
                if bt_playing:
                    self.force_source('BT')
                elif local_playing:
                    self.force_source('LOCAL')
                else:
                    self.active_source = 'NONE'
                
            self.last_bt_playing = bt_playing
            self.last_local_playing = local_playing
            self.last_usb_playing = usb_playing

    def get_now_playing(self):
        """Returns a unified, safe dictionary for the Lock Screen regardless of source."""
        self.bt_daemon = getattr(self.ui_manager, 'bt_daemon', None) or self.bt_daemon
        if self.active_source == 'BT' and self.bt_daemon:
            return self.bt_daemon.media_state
        elif self.active_source == 'LOCAL':
            return self.local_audio.state
        elif self.active_source == 'USB':
            return self.usb_state
        else:
            return {'status': 'stopped', 'title': 'No Media', 'artist': '', 'position': 0, 'duration': 0.0, 'source': 'none'}

    async def send_action(self, action: str):
        """Routes Keypad transport commands to the active engine."""
        self.bt_daemon = getattr(self.ui_manager, 'bt_daemon', None) or self.bt_daemon
        if self.active_source == 'BT' and self.bt_daemon:
            await self.bt_daemon.send_media_command(action)
        elif self.active_source == 'LOCAL':
            if action == 'Play':
                await self.local_audio.send_command(['set_property', 'pause', False])
            elif action == 'Pause':
                await self.local_audio.send_command(['set_property', 'pause', True])
            elif action == 'Next':
                await self.local_audio.send_command(['playlist-next'])
            elif action == 'Previous':
                await self.local_audio.send_command(['playlist-prev'])

    async def restart_dac(self):
        """Manually resets recognized USB Hubs and forcefully heals the Bluetooth radio."""
        print("[MediaArbiter] Manual Hardware Reset triggered from Keypad (Key '1')...", flush=True)
        
        # 1. Reset the Physical USB Hubs (Heals DAC + triggers physical BT re-plug)
        hubs = ["214b:7260", "1a40:0101"]
        for hub_id in hubs:
            proc = await asyncio.create_subprocess_shell(
                f"sudo /usr/bin/usbreset {hub_id}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await proc.communicate()
            if proc.returncode == 0:
                print(f"[MediaArbiter] Successfully reset Hub: {hub_id}", flush=True)
                
        # 2. Wait for USB Hub re-enumeration & Realtek 30KB firmware upload (rtl8761bu_fw.bin) to finish
        #    before checking if the Bluetooth radio needs the kernel auto-defibrillator.
        await asyncio.sleep(3.0)
        try:
            kernel = getattr(self.ui_manager, 'os_kernel', None)
            if kernel and hasattr(kernel, '_heal_bluetooth_hardware'):
                await kernel._heal_bluetooth_hardware(force_check=True)
        except Exception as e:
            print(f"[MediaArbiter] Manual Bluetooth heal error: {e}", flush=True)

        print("[MediaArbiter] Hardware Reset routine finished!", flush=True)
