# /vipxpert_minimal_os/src/core/bluetooth_daemon.py
from src.core.bluez_avrcp_patch import ensure_bluez_avrcp_patch
# /vipxpert_minimal_os/src/core/bluetooth_daemon.py
import asyncio
from dbus_next.aio.message_bus import MessageBus
from dbus_next.constants import BusType
from dbus_next.service import ServiceInterface, method
from dbus_next.errors import DBusError

class PairingAgent(ServiceInterface):
    """DBus Agent that intercepts BlueZ pairing requests and routes them to the UIManager."""
    def __init__(self, ui_manager, db):
        super().__init__('org.bluez.Agent1')
        self.ui = ui_manager
        self.db = db
        self._pending_future = None

    @method()
    async def RequestConfirmation(self, device: 'o', passkey: 'u'):  # type: ignore
        """Triggered by the BlueZ kernel when a device attempts to pair."""
        print(f"[Bluetooth] Pairing request from {device} with passkey {passkey:06d}")
        
        # Create an asyncio.Future to pause the DBus response until the user clicks Yes/No
        loop = asyncio.get_running_loop()
        user_response_future = loop.create_future()
        self._pending_future = user_response_future
        
        async def on_yes():
            if not user_response_future.done():
                user_response_future.set_result(True)

        async def on_no():
            if not user_response_future.done():
                user_response_future.set_result(False)

        from src.ui.components import ConfirmModal
        
        # Extract clean MAC address from DBus object path (e.g., /org/bluez/hci0/dev_78_F2...)
        mac_addr = device.split('/')[-1].replace('dev_', '').replace('_', ':')
        
        # 1. THE INTERCEPT: Auto-Reject if blacklisted
        if self.db.is_device_blocked(mac_addr):
            print(f"[Bluetooth] Auto-rejecting blacklisted device: {mac_addr}")
            raise DBusError('org.bluez.Error.Rejected', 'Device is blacklisted')
        
        modal = ConfirmModal(
            display=self.ui.display, 
            ui_manager=self.ui,
            title="Bluetooth Pair", 
            prompt=f"Pair {mac_addr[:11]}..?", 
            on_yes=on_yes, 
            on_no=on_no
        )
        self.ui.show_system_overlay(modal)
        
        try:
            # The backend pauses here, waiting for the UI overlay to resolve the future
            accepted = await user_response_future
            if not accepted:
                # 2. THE TRIGGER: Add to blacklist on explicit user rejection
                self.db.add_blocked_device(mac_addr, f"Blocked {mac_addr[:8]}")
                raise DBusError('org.bluez.Error.Rejected', 'User rejected pairing')
            # If accepted, we just return safely and BlueZ finalizes the trust
        except asyncio.CancelledError:
            # 3. TIMEOUT SAFETY: Cancelled futures skip the blacklist step entirely
            raise DBusError('org.bluez.Error.Canceled', 'Pairing timed out')
        finally:
            if self._pending_future is user_response_future:
                self._pending_future = None

    @method()
    def Cancel(self):
        """Triggered by BlueZ if the pairing attempt times out before the user answers."""
        print("[Bluetooth] Pairing canceled or timed out by kernel.")
        if self._pending_future and not self._pending_future.done():
            self._pending_future.cancel()

        async def dead_callback():
            pass  # Dead future, just dismiss safely

        # Morph active overlay if it's the pairing request
        if getattr(self.ui.active_overlay, 'title', '') == "Bluetooth Pair":
            self.ui.active_overlay.title = "Missed Pair"
            self.ui.active_overlay.prompt = "Request timed out."
            self.ui.active_overlay.on_yes = dead_callback
            self.ui.active_overlay.on_no = dead_callback
            self.ui.request_render()

        # Morph any queued overlays hiding behind the lock screen
        for overlay in self.ui.pending_overlays:
            if getattr(overlay, 'title', '') == "Bluetooth Pair":
                overlay.title = "Missed Pair"
                overlay.prompt = "Request timed out."
                overlay.on_yes = dead_callback
                overlay.on_no = dead_callback

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's'):  # type: ignore
        """Triggered when a paired device asks to use a specific profile (Audio/Calls)."""
        print(f"[Bluetooth] Auto-authorizing service {uuid} for {device}")
        return

    @method()
    def RequestAuthorization(self, device: 'o'):  # type: ignore
        """Triggered for initial profile trust connections."""
        print(f"[Bluetooth] Auto-authorizing connection for {device}")
        return

from dbus_next.message import Message
from dbus_next import Variant
import time
import os

class BluetoothDaemon:
    """Background service managing BlueZ DBus connections and the Agent lifecycle."""
    def __init__(self, ui_manager, db):
        self.ui = ui_manager
        self.db = db
        self.bus = None
        self.agent = PairingAgent(self.ui, self.db)
        
        # THE LINK: Expose this daemon instance globally to the UI and apps
        self.ui.bt_daemon = self
        
        # Recover codec from disk to survive OS restarts while paused
        self.cache_file = os.path.join(os.path.dirname(__file__), 'codec_cache.txt')
        self.last_known_codec = 'SBC'
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r') as f:
                    self.last_known_codec = f.read().strip() or 'SBC'
            except Exception: pass
            
        # The instant-access cache for the UI to read
        ensure_bluez_avrcp_patch()
        self.media_state = {
            'status': 'stopped',
            'title': 'No Media',
            'artist': '',
            'position': 0,
            'duration': 0.0,
            'source': 'bt',
            'updated_at': time.time(),
            'device_name': 'Unknown',
            'codec': 'N/A',  # Strictly default to N/A if no device is connected yet
            'player_path': None,
            'transport_path': None,
            'bt_volume': None,
            'transport_state': None
        }
        try:
            import json as _jc
            with open("/tmp/vip_bt_track_cache.json", "r") as _f:
                _c = _jc.load(_f)
                if _c.get("title") and _c["title"] != "No Media":
                    self.media_state["title"] = _c["title"]
                    self.media_state["artist"] = _c.get("artist", "")
        except Exception:
            pass
        
    def _parse_media_properties(self, props):
        """Extracts and safely caches BlueZ media dictionary payloads without leaking stale Artist/Position."""
        now_t = time.time()
        if 'Status' in props:
            new_st = str(props['Status'].value).lower()
            if self.media_state.get('status') == 'playing' and new_st != 'playing' and 'Position' not in props:
                self.media_state['position'] = float(self.media_state.get('position', 0.0)) + max(0.0, now_t - float(self.media_state.get('updated_at', now_t)))
            self.media_state['status'] = new_st
            self.media_state['updated_at'] = now_t

        if 'Position' in props:
            self.media_state['position'] = float(props['Position'].value) / 1000.0
            self.media_state['updated_at'] = now_t

        if 'Track' in props:
            track_info = props['Track'].value or {}
            old_title = self.media_state.get('title', 'No Media')
            if 'Title' in track_info:
                val = track_info['Title']
                new_title = str(val.value if hasattr(val, 'value') else val).strip()
                if new_title:
                    self.media_state['title'] = new_title
                    if new_title != old_title:
                        # Comment (invariant): Clear previous track's duration when title changes so a missing AVRCP Duration field on the new track never clamps position at the prior shorter song's duration.
                        if 'Duration' not in track_info:
                            self.media_state['duration'] = 0.0
                        if 'Position' not in props:
                            self.media_state['position'] = 0.0
                            self.media_state['updated_at'] = now_t
                if 'Artist' in track_info:
                    val_a = track_info['Artist']
                    raw_artist = val_a.value if hasattr(val_a, 'value') else val_a
                    self.media_state['artist'] = ", ".join(str(a) for a in raw_artist) if isinstance(raw_artist, list) else str(raw_artist)
                else:
                    self.media_state['artist'] = ''
            elif 'Artist' in track_info:
                val_a = track_info['Artist']
                raw_artist = val_a.value if hasattr(val_a, 'value') else val_a
                self.media_state['artist'] = ", ".join(str(a) for a in raw_artist) if isinstance(raw_artist, list) else str(raw_artist)

            if self.media_state.get('title') and self.media_state.get('title') != 'No Media':
                try:
                    import json as _jc
                    with open("/tmp/vip_bt_track_cache.json", "w") as _f:
                        _jc.dump({"title": self.media_state["title"], "artist": self.media_state.get("artist", "")}, _f)
                except Exception:
                    pass

            if 'Duration' in track_info:
                val = track_info['Duration']
                raw_dur = val.value if hasattr(val, 'value') else val
                try:
                    self.media_state['duration'] = float(raw_dur) / 1000.0
                except Exception:
                    self.media_state['duration'] = 0.0
            
        # Fire codec check only on Status/Track changes (or if codec is still uncached),
        # avoiding 10-15s pw-dump subprocess forks on routine AVRCP Position ticks
        if 'Status' in props or 'Track' in props or self.media_state.get('codec') in (None, 'Unknown'):
            asyncio.create_task(self._update_pipewire_codec())

    def _reset_media_state(self):
        """Forcefully clears all media state caches on device hardware drop."""
        try:
            import os as _os
            if _os.path.exists("/tmp/vip_bt_track_cache.json"):
                _os.remove("/tmp/vip_bt_track_cache.json")
        except Exception:
            pass
        self.media_state.update({
            'title': 'No Media',
            'artist': '',
            'status': 'stopped', # Must be 'stopped' so the lock screen auto-reverts to the clock!
            'position': 0.0,
            'duration': 0.0,
            'codec': 'N/A', # Drop back to N/A instantly when device disconnects
            'updated_at': time.time(),
            'player_path': None,
            'transport_path': None,
            'bt_volume': None,
            'transport_state': None
        })

    def _dbus_signal_handler(self, msg):
        """Instantly catches BlueZ changes, connections, and sudden dropouts."""
        if msg.interface == 'org.freedesktop.DBus.Properties' and msg.member == 'PropertiesChanged':
            if msg.body[0] == 'org.bluez.Device1':
                changed = msg.body[1]
                if 'Connected' in changed:
                    is_conn = bool(changed['Connected'].value)
                    mac = msg.path.split('/')[-1].replace('dev_', '').replace('_', ':')
                    if is_conn:
                        was_conn = (self.media_state.get('connected_mac') == mac)
                        self.ui.bt_session_connected = True
                        self.ui.bt_user_disconnected = False
                        self.media_state['connected_mac'] = mac
                        print(f"[Bluetooth] Device connected: {mac}", flush=True)
                        sfx = getattr(self.ui, 'sound_manager', None)
                        if sfx and not was_conn:
                            sfx.play("bt_connect", cooldown_s=2.0)
                    else:
                        # Stage: Adapter-Aware Disconnect Classifier (Preserves session reconnect state when USB hci0 resets)
                        import os as _os
                        kernel = getattr(self.ui, 'os_kernel', None)
                        adapter_name = msg.path.split('/')[3] if len(msg.path.split('/')) > 3 else 'hci0'
                        adapter_present = _os.path.exists(f"/sys/class/bluetooth/{adapter_name}")
                        if not getattr(kernel, '_bt_recovering', False) and adapter_present:
                            self.ui.bt_user_disconnected = True
                            self.ui.bt_session_connected = False
                            print(f"[Bluetooth] Clean disconnect from {mac}. Marking intentional disconnect.", flush=True)
                            sfx = getattr(self.ui, 'sound_manager', None)
                            if sfx:
                                sfx.play("bt_disconnect", cooldown_s=2.0)
                        else:
                            print(f"[Bluetooth] Hardware/adapter drop for {mac} (recovering={getattr(kernel, '_bt_recovering', False)}, adapter={adapter_present}). Preserving session.", flush=True)
                        self.media_state['connected_mac'] = None
                        self._reset_media_state()
                    self.ui.request_render()
            elif msg.body[0] == 'org.bluez.MediaPlayer1':
                self.media_state['player_path'] = msg.path
                self._parse_media_properties(msg.body[1])
            elif msg.body[0] == 'org.bluez.MediaTransport1':
                if 'Volume' in msg.body[1]:
                    self.media_state['transport_path'] = msg.path
                    self.media_state['bt_volume'] = msg.body[1]['Volume'].value
                    
                    # Defender: If the phone attempts to change absolute volume, crush it and re-sync
                    if getattr(self.ui, 'bt_stream_id', None):
                        # Fire asynchronously with a tiny delay to ensure we run *after* WirePlumber
                        async def _defend_volume():
                            import asyncio
                            await asyncio.sleep(0.5)
                            await self.ui.sync_exclusive_volumes()
                        asyncio.create_task(_defend_volume())
                        
                    self.ui.request_render()
                if 'State' in msg.body[1]:
                    state_val = msg.body[1]['State'].value
                    now_t = time.time()
                    self.media_state['transport_path'] = msg.path
                    self.media_state['transport_state'] = state_val
                    if state_val == 'active':
                        # Comment (invariant): Accumulate elapsed time into position if already playing before resetting updated_at so redundant MediaTransport1 State='active' signals do not snap position backward.
                        if self.media_state.get('status') == 'playing':
                            self.media_state['position'] = float(self.media_state.get('position', 0.0)) + max(0.0, now_t - float(self.media_state.get('updated_at', now_t)))
                        self.media_state['status'] = 'playing'
                        self.media_state['updated_at'] = now_t
                        asyncio.create_task(self._update_pipewire_codec())
                    elif self.media_state.get('status') == 'playing':
                        self.media_state['position'] = float(self.media_state.get('position', 0.0)) + max(0.0, now_t - float(self.media_state.get('updated_at', now_t)))
                        self.media_state['updated_at'] = now_t
                        self.media_state['status'] = 'paused'
                        
                        # Give PipeWire 500ms to build the bluez_input/bluez_output nodes, then sync volumes
                        async def _delayed_sync():
                            import asyncio
                            await asyncio.sleep(0.5)
                            await self.ui.sync_exclusive_volumes()
                            if getattr(self.ui, 'mic_feed_mode', 'mic') in ('music', 'mix') and hasattr(self.ui, 'trigger_transparency_sync'):
                                self.ui.trigger_transparency_sync()
                        asyncio.create_task(_delayed_sync())
                        
                    self.ui.request_render()
                
        elif msg.interface == 'org.freedesktop.DBus.ObjectManager' and msg.member == 'InterfacesAdded':
            obj_path = msg.body[0]
            interfaces = msg.body[1]
            if 'org.bluez.MediaPlayer1' in interfaces:
                self.media_state['player_path'] = obj_path
                self._parse_media_properties(interfaces['org.bluez.MediaPlayer1'])
            if 'org.bluez.MediaTransport1' in interfaces:
                self.media_state['transport_path'] = obj_path
                if 'Volume' in interfaces['org.bluez.MediaTransport1']:
                    self.media_state['bt_volume'] = interfaces['org.bluez.MediaTransport1']['Volume'].value
                if 'State' in interfaces['org.bluez.MediaTransport1']:
                    state_val = interfaces['org.bluez.MediaTransport1']['State'].value
                    self.media_state['transport_state'] = state_val
                    if state_val == 'active':
                        async def _delayed_sync():
                            import asyncio
                            await asyncio.sleep(0.5)
                            await self.ui.sync_exclusive_volumes()
                        asyncio.create_task(_delayed_sync())
                self.ui.request_render()
                
        elif msg.interface == 'org.freedesktop.DBus.ObjectManager' and msg.member == 'InterfacesRemoved':
            removed_interfaces = msg.body[1]
            if 'org.bluez.MediaPlayer1' in removed_interfaces:
                if msg.body[0] == self.media_state.get('player_path'):
                    self.media_state['player_path'] = None
                    if not self.media_state.get('connected_mac') and self.media_state.get('transport_state') != 'active':
                        self._reset_media_state()
            if 'org.bluez.MediaTransport1' in removed_interfaces:
                self.media_state['transport_path'] = None
                self.media_state['transport_state'] = None
                self.media_state['bt_volume'] = None
                if self.media_state.get('status') == 'playing':
                    self.media_state['status'] = 'paused'
                    self.media_state['updated_at'] = time.time()
                self.ui.request_render()

    async def _apply_bt_stream_volume(self):
        """
        High-frequency polling loop catching the dynamic bluez_input stream ID 
        the exact millisecond PipeWire creates it, applying unified volume math immediately.
        """
        import json
        
        # High-frequency sniper loop: poll every 100ms for up to 1.5s max
        max_attempts = 15
        for attempt in range(max_attempts):
            try:
                process = await asyncio.create_subprocess_shell(
                    'XDG_RUNTIME_DIR=/run/user/1000 pw-dump',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                stdout, _ = await process.communicate()
                if stdout:
                    nodes = json.loads(stdout.decode('utf-8'))
                    for node in nodes:
                        if node.get('type') == 'PipeWire:Interface:Node':
                            props = node.get('info', {}).get('props', {})
                            if props.get('node.name', '').startswith('bluez_input'):
                                node_id = str(node['id'])
                                self.ui.bt_stream_id = node_id  # Cache the ID globally
                                
                                print(f"[BluetoothDaemon] Caught BlueZ stream {node_id} on attempt {attempt + 1}! Syncing volume baselines...", flush=True)
                                await self.ui.sync_exclusive_volumes()
                                return  # Sniped successfully, exit immediately!
            except Exception as e:
                print(f"[BluetoothDaemon] WARNING: Polling exception on attempt {attempt + 1}: {e}", flush=True)
                
            await asyncio.sleep(0.1)

    async def _update_pipewire_codec(self):
        """Asks PipeWire for the active Bluetooth codec using pw-dump."""
        await asyncio.sleep(0.5) # Give PipeWire time to negotiate the codec handshake
        
        # STRICT GUARD: Never ask PipeWire for the codec if the stream is closed!
        # PipeWire defaults to reporting "sbc" when idle, which would overwrite and ruin our cache.
        if self.media_state.get('transport_state') != 'active':
            return
            
        try:
            # Use pw-dump instead of pw-cli, keeping the systemd environment fix
            cmd = 'XDG_RUNTIME_DIR=/run/user/1000 pw-dump | grep "api.bluez5.codec" | head -n 1'
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode('utf-8').strip()
            
            if output and '"' in output:
                # Parses: "api.bluez5.codec": "aptx", -> extracts aptx
                parts = output.split('"')
                if len(parts) >= 4:
                    codec = parts[3].upper()
                    self.media_state['codec'] = codec
                    try:
                        with open(self.cache_file, 'w') as f:
                            f.write(codec)
                    except Exception: pass
        except Exception:
            pass # Fail silently if disconnected or PipeWire is restarting

    @staticmethod
    def read_mic_mode():
        """Reads current HFP mic mode ('cvsd' or 'msbc') from 53-no-hearing-protection.conf."""
        wp_file = "/home/vipxpert/.config/wireplumber/wireplumber.conf.d/53-no-hearing-protection.conf"
        if os.path.exists(wp_file):
            try:
                with open(wp_file, "r", encoding="utf-8") as f:
                    txt = f.read()
                if "bluez5.enable-msbc = true" in txt:
                    return "msbc"
            except Exception:
                pass
        return "cvsd"

    @staticmethod
    def read_codec_config():
        """Reads enabled codecs and quality flags directly from 53-no-hearing-protection.conf."""
        import re
        default_codecs = [
            "sbc", "sbc_xq", "aac", "aac_eld", "aptx", "aptx_hd", "aptx_ll",
            "aptx_ll_duplex", "faststream", "faststream_duplex", "lc3plus_h3",
            "ldac", "opus_05", "opus_05_51", "opus_05_71", "opus_05_duplex",
            "opus_05_pro", "opus_g", "lc3", "msbc"
        ]
        wp_file = "/home/vipxpert/.config/wireplumber/wireplumber.conf.d/53-no-hearing-protection.conf"
        codecs = list(default_codecs)
        aac_max = True
        ldac_max = True
        if os.path.exists(wp_file):
            try:
                with open(wp_file, "r", encoding="utf-8") as f:
                    txt = f.read()
                m_c = re.search(r"bluez5\.codecs\s*=\s*\[\s*([^\]]*)\]", txt)
                if m_c:
                    parsed = [c.strip() for c in m_c.group(1).split() if c.strip()]
                    if parsed:
                        codecs = parsed
                m_aac = re.search(r"bluez5\.a2dp\.aac\.bitratemode\s*=\s*(\d+)", txt)
                aac_max = (m_aac.group(1) == "5") if m_aac else False
                m_ldac = re.search(r'bluez5\.a2dp\.ldac\.quality\s*=\s*"([^"]+)"', txt)
                ldac_max = (m_ldac.group(1) == "hq") if m_ldac else False
            except Exception as e:
                print(f"[Bluetooth Daemon] Error reading {wp_file}: {e}")
        return {"codecs": codecs, "aac_max": aac_max, "ldac_max": ldac_max}

    async def apply_codec_rules(self, codecs=None, aac_max=None, ldac_max=None, mic_mode=None):
        """Updates monitor.bluez.properties in 53-no-hearing-protection.conf as the single source of truth."""
        import re, shutil
        cfg = self.read_codec_config()
        if mic_mode is None:
            mic_mode = self.read_mic_mode()
        if codecs is None:
            codecs = cfg["codecs"]
        if aac_max is None:
            aac_max = cfg["aac_max"]
        if ldac_max is None:
            ldac_max = cfg["ldac_max"]

        if "sbc" not in codecs:
            codecs = ["sbc"] + list(codecs)
        if mic_mode == "msbc":
            if "msbc" not in codecs:
                codecs.append("msbc")
        else:
            codecs = [c for c in codecs if c != "msbc"]
        sbc_xq_flag = "true" if "sbc_xq" in codecs else "false"
        codec_string = " ".join(codecs)
        aac_val = "5" if aac_max else "0"
        ldac_val = '"hq"' if ldac_max else '"auto"'

        wp_dir = "/home/vipxpert/.config/wireplumber/wireplumber.conf.d"
        os.makedirs(wp_dir, exist_ok=True)
        # Purge conflicting legacy files so 53-no-hearing-protection.conf is never shadowed
        for stale in ("51-codecs.conf", "51-bluez-config.conf"):
            stale_p = os.path.join(wp_dir, stale)
            if os.path.exists(stale_p):
                os.remove(stale_p)
        shutil.rmtree("/home/vipxpert/.config/wireplumber/bluetooth.lua.d", ignore_errors=True)

        wp_file = os.path.join(wp_dir, "53-no-hearing-protection.conf")
        msbc_flag = "true" if mic_mode == "msbc" else "false"
        new_bluez_props = (
            "monitor.bluez.properties = {\n"
            f"  bluez5.a2dp.aac.bitratemode = {aac_val}\n"
            f"  bluez5.a2dp.ldac.quality = {ldac_val}\n"
            f"  bluez5.enable-sbc-xq = {sbc_xq_flag}\n"
            f"  bluez5.enable-msbc = {msbc_flag}\n"
            "  bluez5.enable-hw-volume = true\n"
            '  bluez5.hfphsp-backend = "native"\n'
            f"  bluez5.codecs = [ {codec_string} ]\n"
            "  bluez5.roles = [ a2dp_sink hfp_hf hsp_hs ]\n"
            "}"
        )

        try:
            if os.path.exists(wp_file):
                with open(wp_file, "r", encoding="utf-8") as f:
                    txt = f.read()
                if "monitor.bluez.properties" in txt:
                    txt = re.sub(
                        r"monitor\.bluez\.properties\s*=\s*\{[^}]*\}",
                        new_bluez_props,
                        txt,
                        count=1
                    )
                else:
                    txt = new_bluez_props + "\n\n" + txt
            else:
                txt = new_bluez_props + "\n"

            with open(wp_file, "w", encoding="utf-8", newline="\n") as f:
                f.write(txt)
            subprocess.run(["chown", "vipxpert:vipxpert", wp_file], check=False)

            sco_mtu_val = "60:12" if mic_mode == "msbc" else "48:12"
            subprocess.run(["hciconfig", "hci0", "scomtu", sco_mtu_val], check=False)

            cmd = 'sudo -u vipxpert env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user restart pipewire wireplumber pipewire-pulse'
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await proc.communicate()
            print(f"[Bluetooth Daemon] FiiO hot-swap executed in 53-no-hearing-protection.conf. Codecs: {codec_string}")
        except Exception as e:
            print(f"[Bluetooth Daemon] Codec override failed: {e}")

    async def send_media_command(self, command: str):
        """Sends an AVRCP command (Play, Pause, Next, Previous) to the active phone."""
        path = self.media_state.get('player_path')
        if not path or not self.bus: return
        try:
            msg = Message(destination='org.bluez', path=path, interface='org.bluez.MediaPlayer1', member=command)
            await self.bus.call(msg)
        except Exception as e:
            print(f"[Bluetooth] Media command '{command}' failed: {e}")

    async def adjust_bt_volume(self, delta: int):
        """Adjusts absolute volume (0-127) via MediaTransport1 if enabled on the phone."""
        path = self.media_state.get('transport_path')
        current_vol = self.media_state.get('bt_volume')
        if not path or not self.bus or current_vol is None: return
        
        # Clamp new volume strictly within DBus AVRCP limits
        new_vol = max(0, min(127, current_vol + delta))
        try:
            msg = Message(
                destination='org.bluez',
                path=path,
                interface='org.freedesktop.DBus.Properties',
                member='Set',
                signature='ssv',
                body=['org.bluez.MediaTransport1', 'Volume', Variant('q', new_vol)]
            )
            await self.bus.call(msg)
            # Optimistically update to avoid waiting for the round-trip signal
            self.media_state['bt_volume'] = new_vol
            self.ui.request_render()
        except Exception as e:
            print(f"[Bluetooth] Volume command failed: {e}")

    async def _ghost_watchdog_loop(self):
        """Background heartbeat that actively tests the radio link when idle to detect and slay firmware ghosts."""
        import asyncio
        while True:
            await asyncio.sleep(15)

            # 1. Skip if user intentionally disconnected or if A2DP audio is actively streaming
            # (active A2DP transport proves the L2CAP radio link is alive; avoids 15s RF/USB jitter)
            if getattr(self.ui, 'bt_user_disconnected', False):
                continue
            if self.media_state.get('transport_state') == 'active':
                continue

            mac = self.media_state.get('connected_mac')
            player_path = self.media_state.get('player_path')
            if not mac and player_path:
                try:
                    mac = player_path.split('/')[4].replace('dev_', '').replace('_', ':')
                except Exception:
                    mac = None
            if not mac:
                continue

            try:
                # Ping the idle device natively. If it's a ghost (phone crashed without HCI disconnect), this fails.
                proc = await asyncio.create_subprocess_shell(
                    f"l2ping -c 1 {mac}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                await proc.communicate()

                # Re-verify that a clean disconnect didn't arrive while l2ping was in flight
                if getattr(self.ui, 'bt_user_disconnected', False) or (not self.media_state.get('connected_mac') and not self.media_state.get('player_path')):
                    continue

                # Return code != 0 means the physical L2CAP link is completely dead despite DBus state
                if proc.returncode != 0:
                    print(f"[Bluetooth Watchdog] Ghost connection detected for {mac}. Triggering OS-level Auto-Defibrillator...", flush=True)
                    # Preserve session flag so hardware recovery knows to auto-reconnect
                    self.ui.bt_session_connected = True
                    self.ui.bt_user_disconnected = False
                    self._reset_media_state()
                    self.ui.request_render()

                    # Offload the heavy lifting to the centralized OS Kernel hotplug/recovery flow
                    if hasattr(self.ui, 'os_kernel'):
                        self.ui.os_kernel._debounce_bt_restart()

                    # Sleep for 15 seconds to let the kernel completely rebuild the USB bus and restart BlueZ
                    await asyncio.sleep(15)

            except Exception as e:
                print(f"[Bluetooth Watchdog] Error: {e}", flush=True)

    async def start(self):
        """Connects to the system DBus and registers the pairing agent."""
        try:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            
            # Export our Python agent to the DBus
            self.bus.export('/vipxpert/bluetooth/agent', self.agent)
            
            # Tell BlueZ to use our agent
            introspection = await self.bus.introspect('org.bluez', '/org/bluez')
            proxy = self.bus.get_proxy_object('org.bluez', '/org/bluez', introspection)
            agent_manager = proxy.get_interface('org.bluez.AgentManager1')
            
            # Register as a DisplayYesNo capability agent
            await agent_manager.call_register_agent('/vipxpert/bluetooth/agent', 'DisplayYesNo')  # type: ignore
            await agent_manager.call_request_default_agent('/vipxpert/bluetooth/agent')  # type: ignore
            print("[Bluetooth Daemon] Successfully registered as default BlueZ Agent.")
            
            # Link the Python handler to the DBus
            self.bus.add_message_handler(self._dbus_signal_handler)
            
            # Catch existing media connections already running on boot
            try:
                root_introspection = await self.bus.introspect('org.bluez', '/')
                root_proxy = self.bus.get_proxy_object('org.bluez', '/', root_introspection)
                obj_manager = root_proxy.get_interface('org.freedesktop.DBus.ObjectManager')
                objects = await obj_manager.call_get_managed_objects() # type: ignore
                
                for path, interfaces in objects.items():
                    if 'org.bluez.Device1' in interfaces:
                        dev = interfaces['org.bluez.Device1']
                        if dev.get('Connected') and dev['Connected'].value:
                            mac = dev.get('Address').value if 'Address' in dev else path.split('/')[-1].replace('dev_', '').replace('_', ':')
                            self.media_state['connected_mac'] = mac
                            self.ui.bt_session_connected = True
                            self.ui.bt_user_disconnected = False
                    if 'org.bluez.MediaPlayer1' in interfaces:
                        self.media_state['player_path'] = path
                        self._parse_media_properties(interfaces['org.bluez.MediaPlayer1'])
                    if 'org.bluez.MediaTransport1' in interfaces:
                        self.media_state['transport_path'] = path
                        if 'Volume' in interfaces['org.bluez.MediaTransport1']:
                            self.media_state['bt_volume'] = interfaces['org.bluez.MediaTransport1']['Volume'].value
                        if 'State' in interfaces['org.bluez.MediaTransport1']:
                            state_val = interfaces['org.bluez.MediaTransport1']['State'].value
                            self.media_state['transport_state'] = state_val
                            if state_val == 'active':
                                asyncio.create_task(self._update_pipewire_codec())
                                async def _delayed_sync():
                                    import asyncio
                                    await asyncio.sleep(0.5)
                                    await self.ui.sync_exclusive_volumes()
                                asyncio.create_task(_delayed_sync())
            except Exception as e:
                print(f"[Bluetooth Daemon] Boot-time media poll skipped: {e}")
            
            # Mount the Device Connection State Sniper (PropertiesChanged on org.bluez.Device1)
            await self.bus.call(
                Message(destination='org.freedesktop.DBus',
                        path='/org/freedesktop/DBus',
                        interface='org.freedesktop.DBus',
                        member='AddMatch',
                        signature='s',
                        body=["type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',arg0='org.bluez.Device1'"])
            )

            # Mount the Main Track-Change Sniper (PropertiesChanged)
            await self.bus.call(
                Message(destination='org.freedesktop.DBus',
                        path='/org/freedesktop/DBus',
                        interface='org.freedesktop.DBus',
                        member='AddMatch',
                        signature='s',
                        body=["type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',arg0='org.bluez.MediaPlayer1'"])
            )
            
            # Mount the Transport Volume Sniper (PropertiesChanged)
            await self.bus.call(
                Message(destination='org.freedesktop.DBus',
                        path='/org/freedesktop/DBus',
                        interface='org.freedesktop.DBus',
                        member='AddMatch',
                        signature='s',
                        body=["type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',arg0='org.bluez.MediaTransport1'"])
            )
            
            # Mount the Mid-Stream Connector Rule
            await self.bus.call(
                Message(destination='org.freedesktop.DBus',
                        path='/org/freedesktop/DBus',
                        interface='org.freedesktop.DBus',
                        member='AddMatch',
                        signature='s',
                        body=["type='signal',interface='org.freedesktop.DBus.ObjectManager',member='InterfacesAdded'"])
            )
            
            # Mount the Disconnection Sniper Rule
            await self.bus.call(
                Message(destination='org.freedesktop.DBus',
                        path='/org/freedesktop/DBus',
                        interface='org.freedesktop.DBus',
                        member='AddMatch',
                        signature='s',
                        body=["type='signal',interface='org.freedesktop.DBus.ObjectManager',member='InterfacesRemoved'"])
            )
            
            # Execute an immediate codec check on boot to catch existing streams
            asyncio.create_task(self._update_pipewire_codec())
            
            # Force radios ON at boot dynamically, bypassing crashed chips
            try:
                root_intro = await self.bus.introspect('org.bluez', '/')
                root_proxy = self.bus.get_proxy_object('org.bluez', '/', root_intro)
                obj_manager = root_proxy.get_interface('org.freedesktop.DBus.ObjectManager')
                all_objs = await obj_manager.call_get_managed_objects()
                from dbus_next import Variant
                
                for path, interfaces in all_objs.items():
                    if 'org.bluez.Adapter1' in interfaces:
                        try:
                            hci_intro = await self.bus.introspect('org.bluez', path)
                            hci_proxy = self.bus.get_proxy_object('org.bluez', path, hci_intro)
                            hci_props = hci_proxy.get_interface('org.freedesktop.DBus.Properties')
                            await hci_props.call_set('org.bluez.Adapter1', 'Powered', Variant('b', True))
                            print(f"[Bluetooth Daemon] Radio {path} powered ON automatically.")
                        except Exception as err:
                            print(f"[Bluetooth Daemon] Boot power-on failed for {path}: {err}")
            except Exception as e:
                print(f"[Bluetooth Daemon] Dynamic boot power-on scan failed: {e}")
            
            # Spawn the self-healing hardware watchdog
            self.watchdog_task = asyncio.create_task(self._ghost_watchdog_loop())
            
            # Keep the bus alive
            await self.bus.wait_for_disconnect()
        except Exception as e:
            print(f"[Bluetooth Daemon] Initialization failed: {e}")

    async def cleanup(self):
        """Safely tears down the DBus agent and unhooks message handlers."""
        try:
            if hasattr(self, 'watchdog_task'):
                self.watchdog_task.cancel()
                
            if hasattr(self, 'bus') and self.bus:
                # Tell BlueZ we are unregistering as the agent
                introspection = await self.bus.introspect('org.bluez', '/org/bluez')
                proxy = self.bus.get_proxy_object('org.bluez', '/org/bluez', introspection)
                agent_manager = proxy.get_interface('org.bluez.AgentManager1')
                await agent_manager.call_unregister_agent('/vipxpert/bluetooth/agent') # type: ignore
                
                # Unhook handlers and disconnect
                self.bus.remove_message_handler(self._dbus_signal_handler)
                self.bus.disconnect()
                print("[Bluetooth Daemon] DBus connection safely closed.")
        except Exception as e:
            print(f"[Bluetooth Daemon] Warning during cleanup: {e}")
