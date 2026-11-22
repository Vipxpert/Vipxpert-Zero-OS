# /vipxpert_minimal_os/src/apps/bluetooth_app.py
import asyncio
import subprocess
from src.ui.components import ListMenuApp, ConfirmModal

class BluetoothManagerApp(ListMenuApp):
    """Main Bluetooth Hub managing discoverability timers and active connections."""
    def __init__(self, display, ui_manager, db):
        self.db = db
        # 0: Hidden, 1: 60s Timer, 2: Always On, 3: Smart
        self.visibility_mode = int(self.db.get_setting("bt_visibility", "0"))
        # 0: Off, 1: On Boot, 2: On Plug
        self.autoconnect_mode = int(self.db.get_setting("bt_autoconnect", "0"))
        
        self.timer = 0
        self.timer_task = None
        self.is_powered = True
        self._last_smart_mac = None
        
        # Reset the timer state on boot
        if self.visibility_mode == 1:
            self.visibility_mode = 0
            
        super().__init__(
            display=display,
            ui_manager=ui_manager,
            title="Bluetooth Hub",
            items=[],
            parent_app_id='settings'
        )
        self._sync_hardware_visibility()
        self._rebuild_menu()

    def _sync_hardware_visibility(self):
        """Pushes the requested discoverability state to the BlueZ kernel."""
        if self.visibility_mode in [1, 2]:
            state = "on"
        elif self.visibility_mode == 3:
            state = "off" if getattr(self, 'active_mac', None) else "on"
        else:
            state = "off"
            
        # Using Popen for non-blocking execution
        subprocess.Popen(["bluetoothctl", "discoverable", state], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def _countdown_task(self):
        """Background task that runs regardless of app focus."""
        try:
            while self.timer > 0:
                await asyncio.sleep(1.0)
                self.timer -= 1
                
            # Time is up! Trigger hardware sync globally
            self.visibility_mode = 0
            self.db.set_setting("bt_visibility", "0")
            self._sync_hardware_visibility()
            
            # Force UI redraw ONLY if the user is currently looking at this menu
            if self.ui.current_app is self:
                self._rebuild_menu()
        except asyncio.CancelledError:
            pass

    def _toggle_visibility(self):
        """Cycles through the 4 visibility states and saves preferences to SQLite."""
        self.visibility_mode = (self.visibility_mode + 1) % 4
        self.db.set_setting("bt_visibility", str(self.visibility_mode))
        
        # Kill existing background timer if user manually cycles the mode
        if self.timer_task:
            self.timer_task.cancel()
            self.timer_task = None
            
        if self.visibility_mode == 1:
            self.timer = 60
            self.timer_task = asyncio.create_task(self._countdown_task())
            self.show_info_toast("60s Timer Started")
        else:
            state_names = {0: "Hidden", 2: "Always On", 3: "Smart"}
            self.show_info_toast(f"Vis: {state_names[self.visibility_mode]}")
            
        self._sync_hardware_visibility()
        self._rebuild_menu()

    async def _check_power_state(self):
        """Dynamically scans all adapters to find if any working radio is ON."""
        self.is_powered = False
        daemon = getattr(self.ui, 'bt_daemon', None)
        if not daemon or not daemon.bus:
            return

        try:
            intro = await daemon.bus.introspect('org.bluez', '/')
            proxy = daemon.bus.get_proxy_object('org.bluez', '/', intro)
            obj_manager = proxy.get_interface('org.freedesktop.DBus.ObjectManager')
            objects = await obj_manager.call_get_managed_objects()
            
            for path, interfaces in objects.items():
                if 'org.bluez.Adapter1' in interfaces:
                    powered = interfaces['org.bluez.Adapter1'].get('Powered')
                    if powered and powered.value:
                        self.is_powered = True
                        break
        except Exception:
            self.is_powered = False

    async def _toggle_power(self):
        """Asynchronously flips ALL Bluetooth radio states natively via DBus."""
        target_state = not getattr(self, 'is_powered', True)
        new_state_str = "ON" if target_state else "OFF"
        self.show_blocking_toast(f"Turning {new_state_str}...")
        
        daemon = getattr(self.ui, 'bt_daemon', None)
        if daemon and daemon.bus:
            try:
                intro = await daemon.bus.introspect('org.bluez', '/')
                proxy = daemon.bus.get_proxy_object('org.bluez', '/', intro)
                obj_manager = proxy.get_interface('org.freedesktop.DBus.ObjectManager')
                objects = await obj_manager.call_get_managed_objects()
                from dbus_next import Variant
                
                for path, interfaces in objects.items():
                    if 'org.bluez.Adapter1' in interfaces:
                        try:
                            a_intro = await daemon.bus.introspect('org.bluez', path)
                            a_proxy = daemon.bus.get_proxy_object('org.bluez', path, a_intro)
                            a_props = a_proxy.get_interface('org.freedesktop.DBus.Properties')
                            await a_props.call_set('org.bluez.Adapter1', 'Powered', Variant('b', target_state))
                        except Exception as err:
                            print(f"[Bluetooth] Failed to toggle {path}: {err}")
                await asyncio.sleep(0.5)  # Give BlueZ time to cycle the hardware
            except Exception as e:
                print(f"[Bluetooth] Power Toggle DBus Error: {e}")
            
        self.is_working = False
        self.layout.toast_text = None
        await self._fetch_async_state()
        self._rebuild_menu()
        self.ui.request_render()

    async def on_start(self):
        """Asynchronous startup hook to fetch DBus states before rendering."""
        await self._fetch_async_state()
        self._rebuild_menu()
        if hasattr(super(), 'on_start'):
            await super().on_start()

    async def _fetch_async_state(self):
        """Gathers DBus states asynchronously to prevent UI dead-hangs."""
        await self._check_power_state()
        self.active_name = "None"
        self.active_mac = None
        
        daemon = getattr(self.ui, 'bt_daemon', None)
        if daemon and daemon.bus:
            try:
                intro = await daemon.bus.introspect('org.bluez', '/')
                proxy = daemon.bus.get_proxy_object('org.bluez', '/', intro)
                obj_manager = proxy.get_interface('org.freedesktop.DBus.ObjectManager')
                objects = await obj_manager.call_get_managed_objects()
                
                for path, interfaces in objects.items():
                    if 'org.bluez.Device1' in interfaces:
                        dev = interfaces['org.bluez.Device1']
                        if dev.get('Connected') and dev['Connected'].value:
                            self.active_name = dev.get('Alias', dev.get('Name')).value
                            self.active_mac = dev.get('Address').value
                            break
            except Exception:
                pass
                
        # Smart Mode check: If connection state changed, push hardware update
        if self.visibility_mode == 3 and self.active_mac != getattr(self, '_last_smart_mac', None):
            self._last_smart_mac = self.active_mac
            self._sync_hardware_visibility()

    def _prompt_disconnect(self, mac: str, name: str):
        """Summons an overlay to drop the active connection."""
        async def execute_disconnect():
            self.ui.bt_user_disconnected = True
            self.ui.bt_session_connected = False
            subprocess.Popen(["bluetoothctl", "disconnect", mac], stdout=subprocess.DEVNULL)
            self.show_info_toast(f"Dropped {name[:8]}")
            
        async def cancel():
            pass

        modal = ConfirmModal(
            display=self.ui.display, ui_manager=self.ui,
            title="Disconnect", prompt=f"Drop {name[:11]}?",
            on_yes=execute_disconnect, on_no=cancel
        )
        self.ui.show_system_overlay(modal)

    def _toggle_autoconnect(self):
        self.autoconnect_mode = (self.autoconnect_mode + 1) % 3
        self.db.set_setting("bt_autoconnect", str(self.autoconnect_mode))
        self._rebuild_menu()

    async def _toggle_mic_mode(self):
        from src.core.bluetooth_daemon import BluetoothDaemon
        cur = BluetoothDaemon.read_mic_mode()
        nxt = "msbc" if cur == "cvsd" else "cvsd"
        self.show_blocking_toast(f"Switching {nxt.upper()}...")
        try:
            if hasattr(self.ui, 'bt_daemon') and self.ui.bt_daemon:
                await self.ui.bt_daemon.apply_codec_rules(mic_mode=nxt)
                if hasattr(self.ui, 'kernel') and self.ui.kernel:
                    await self.ui.kernel._enforce_audio_routing()
        finally:
            self.is_working = False
            self.layout.toast_text = None
            self._rebuild_menu()
            self.ui.request_render()

    def _rebuild_menu(self):
        """Constructs the dynamic dictionary list for the UI Layout Engine."""
        from src.core.bluetooth_daemon import BluetoothDaemon
        power_text = "ON" if getattr(self, 'is_powered', False) else "OFF"
        vis_text = ["Hidden", f"Timer ({self.timer}s)", "Always On", "Smart"][self.visibility_mode]
        auto_text = ["Off", "On Boot", "On Plug"][self.autoconnect_mode]
        mic_text = "mSBC (16k)" if BluetoothDaemon.read_mic_mode() == "msbc" else "CVSD (8k)"
        
        stream_name = getattr(self, 'active_name', "None")
        stream_mac = getattr(self, 'active_mac', None)
        
        # Make the row inert if there is no device, otherwise bind the disconnect prompt
        active_action = (lambda: self._prompt_disconnect(stream_mac, stream_name)) if stream_mac else (lambda: None)
        
        new_items = [
            {"name": f"Bluetooth: {power_text}", "action": self._toggle_power},
            {"name": "Visibility", "suffix": vis_text, "action": self._toggle_visibility},
            {"name": "Auto Connect", "suffix": auto_text, "action": self._toggle_autoconnect},
            {"name": "Mic Mode", "suffix": mic_text, "action": self._toggle_mic_mode},
            {"name": "Active Device", "suffix": stream_name, "action": active_action},
            {"name": "Codec Settings", "suffix": "->", "action": "bt_codec_app"},
            {"name": "Codec Quality", "suffix": "->", "action": "bt_quality_app"},
            {"name": "Paired Devices", "suffix": "->", "action": "bt_devices_app"},
            {"name": "Blocked Devices", "suffix": "->", "action": "bt_blocked_app"}
        ]
        self.update_items(new_items, preserve_cursor=True)
        

    async def update(self):
        """Overridden 1s telemetry hook to redraw active menu states."""
        needs_rebuild = False
        
        # 1. If timer is active, UI needs to redraw every second to show the countdown
        if self.visibility_mode == 1:
            needs_rebuild = True
                
        # 2. Async re-poll active connections every 5 seconds without blocking OS
        self._tick_count = getattr(self, '_tick_count', 0) + 1
        if self._tick_count % 5 == 0:
            await self._fetch_async_state()
            needs_rebuild = True

        if needs_rebuild:
            self._rebuild_menu()
            self.ui.request_render()
            
        await super().update()

class BluetoothDevicesApp(ListMenuApp):
    """Sub-menu displaying all trusted devices with Forget capabilities."""
    def __init__(self, display, ui_manager):
        super().__init__(
            display=display,
            ui_manager=ui_manager,
            title="Saved Devices",
            items=[],
            parent_app_id='bluetooth_manager'
        )

    async def on_start(self):
        await self._refresh_devices()
        if hasattr(super(), 'on_start'):
            await super().on_start()

    async def _refresh_devices(self):
        """Asynchronously extracts trusted MAC addresses natively from BlueZ DBus."""
        daemon = getattr(self.ui, 'bt_daemon', None)
        if not daemon or not daemon.bus:
            self.update_items([{"name": "No Radio", "action": lambda: None}])
            return

        try:
            intro = await daemon.bus.introspect('org.bluez', '/')
            proxy = daemon.bus.get_proxy_object('org.bluez', '/', intro)
            obj_manager = proxy.get_interface('org.freedesktop.DBus.ObjectManager')
            objects = await obj_manager.call_get_managed_objects()
            
            items = []
            from src.core.database import DatabaseManager
            primary_mac = DatabaseManager().get_setting("bt_primary_mac", "")
            
            for path, interfaces in objects.items():
                if 'org.bluez.Device1' in interfaces:
                    dev = interfaces['org.bluez.Device1']
                    mac = dev.get('Address', getattr(dev.get('Address'), 'value', '')).value
                    name = dev.get('Alias', dev.get('Name', getattr(dev.get('Name'), 'value', 'Unknown'))).value
                    
                    prefix = "★ " if mac == primary_mac else ""
                    items.append({
                        "name": name, 
                        "suffix": "", 
                        "prefix": prefix, 
                        "action": lambda m=mac, n=name: self._mount_device_actions(m, n)
                    })
            
            if not items:
                items = [{"name": "No Devices", "action": lambda: None}]
            self.update_items(items)
            self.ui.request_render()
        except Exception as e:
            print(f"[Bluetooth] Device fetch error: {e}")
            self.update_items([{"name": "DBus Error", "action": lambda: None}])

    def _mount_device_actions(self, mac: str, name: str):
        """Instantiates an inline context menu to manage the specific device."""
        app = BluetoothDeviceActionApp(self.display, self.ui, mac, name, parent=self)
        # Ensure we route using UIManager's string ID parameter properly
        asyncio.create_task(self.ui.switch_app(f'bt_action_{mac}', fallback_app=app))
        
class BluetoothDeviceActionApp(ListMenuApp):
    """Context menu for setting Primary targets and deleting devices."""
    def __init__(self, display, ui_manager, mac, name, parent):
        self.mac = mac
        self.device_name = name
        self.parent_app = parent
        
        super().__init__(
            display=display, ui_manager=ui_manager,
            title=name[:14], items=[], parent_app_id='bt_devices_app'
        )
        
    async def on_start(self):
        from src.core.database import DatabaseManager
        self.db = DatabaseManager()
        
        async def set_primary():
            self.db.set_setting("bt_primary_mac", self.mac)
            self.show_info_toast("Primary Set!")
            await self.parent_app._refresh_devices()
            await self.ui.switch_app('bt_devices_app', fallback_app=self.parent_app)
            
        def connect_now():
            self.ui.bt_user_disconnected = False
            subprocess.Popen(["bluetoothctl", "connect", self.mac], stdout=subprocess.DEVNULL)
            self.show_info_toast("Connecting...")
            
        self.update_items([
            {"name": "Set Auto Primary", "action": set_primary},
            {"name": "Connect Now", "action": connect_now},
            {"name": "Forget Device", "action": lambda: self._prompt_forget(self.mac, self.device_name)}
        ])
        if hasattr(super(), 'on_start'):
            await super().on_start()

    def _prompt_forget(self, mac: str, name: str):
        """Summons the global overlay to confirm deletion."""
        async def execute_forget():
            subprocess.Popen(["bluetoothctl", "remove", mac], stdout=subprocess.DEVNULL)
            await asyncio.sleep(0.5) # Give BlueZ time to clear cache
            await self.parent_app._refresh_devices()
            await self.ui.switch_app('bt_devices_app', fallback_app=self.parent_app)

        async def cancel_forget():
            pass

        modal = ConfirmModal(
            display=self.ui.display,
            ui_manager=self.ui,
            title="Manage Device",
            prompt=f"Forget {name[:11]}?",
            on_yes=execute_forget,
            on_no=cancel_forget
        )
        self.ui.show_system_overlay(modal)

class BluetoothBlockedApp(ListMenuApp):
    """Sub-menu displaying blacklisted devices with Unblock capabilities."""
    def __init__(self, display, ui_manager):
        # Instantiate a local DB connection to avoid signature crashes in the OS router
        from src.core.database import DatabaseManager
        self.db = DatabaseManager()
        
        super().__init__(
            display=display,
            ui_manager=ui_manager,
            title="Blocked Devices",
            items=[],
            parent_app_id='bluetooth_manager'
        )

    async def on_start(self):
        self._refresh_blocked()
        await super().on_start()

    def _refresh_blocked(self):
        """Fetches the blacklist from the database."""
        blocked_list = self.db.get_blocked_devices()
        items = []
        for b in blocked_list:
            mac, name = b['mac'], b['name']
            items.append({"name": name, "action": lambda m=mac, n=name: self._prompt_unblock(m, n)})
            
        if not items:
            items = [{"name": "No Blocks", "action": lambda: None}]
            
        self.update_items(items)

    def _prompt_unblock(self, mac: str, name: str):
        """Summons the global overlay to confirm unblocking a device."""
        async def execute_unblock():
            self.db.remove_blocked_device(mac)
            self._refresh_blocked()
            self.show_info_toast(f"Allowed {name[:8]}")
            
        async def cancel():
            pass

        modal = ConfirmModal(
            display=self.ui.display,
            ui_manager=self.ui,
            title="Unblock Device",
            prompt=f"Allow {name[:10]}?",
            on_yes=execute_unblock,
            on_no=cancel
        )
        self.ui.show_system_overlay(modal)

class BluetoothCodecApp(ListMenuApp):
    """Sub-menu for dynamic FiiO-style Bluetooth codec toggling."""
    def __init__(self, display, ui_manager):
        from src.core.database import DatabaseManager
        self.db = DatabaseManager()
        # The exhaustive God Mode list of codecs PipeWire can theoretically support.
        # If a codec library isn't compiled on the Pi, PipeWire safely ignores it.
        self.available_codecs = [
            "sbc", "sbc_xq", "aac", "aac_eld", "aptx", "aptx_hd", "aptx_ll", 
            "aptx_ll_duplex", "faststream", "faststream_duplex", "lc3plus_h3", 
            "ldac", "opus_05", "opus_05_51", "opus_05_71", "opus_05_duplex", 
            "opus_05_pro", "opus_g", "lc3"
        ]
        
        super().__init__(
            display=display,
            ui_manager=ui_manager,
            title="Codec Menu",
            items=[],
            parent_app_id='bluetooth_manager'
        )

    async def on_start(self):
        """Fetches live codecs directly from 53-no-hearing-protection.conf on load."""
        from src.core.bluetooth_daemon import BluetoothDaemon
        self.layout.modal = None
        cfg = BluetoothDaemon.read_codec_config()
        self.original_codecs = [c for c in cfg["codecs"] if c in self.available_codecs]
        if "sbc" not in self.original_codecs:
            self.original_codecs.insert(0, "sbc")
        self.pending_codecs = self.original_codecs.copy()
        self._rebuild_menu()
        if hasattr(super(), 'on_start'):
            await super().on_start()

    async def on_resume(self, previous_app_id=None):
        """Re-reads 53-no-hearing-protection.conf and clears stale modals when pulled from UIManager cache."""
        await self.on_start()

    def _rebuild_menu(self):
        """Constructs the visual checklist based on pending memory states."""
        items = []
        for codec in self.available_codecs:
            is_on = codec in self.pending_codecs
            prefix = "[★] " if is_on else "[ ] "
            items.append({
                "name": codec.upper().replace("_", " "),
                "prefix": prefix,
                "action": lambda c=codec: self._toggle_codec(c)
            })
        self.update_items(items, preserve_cursor=True)
        self.ui.request_render()

    def _toggle_codec(self, codec: str):
        """Flips the local codec state in memory."""
        if codec == "sbc" and codec in self.pending_codecs:
            self.show_info_toast("SBC is Required")
            return
        if codec in self.pending_codecs:
            self.pending_codecs.remove(codec)
        else:
            self.pending_codecs.append(codec)
        self._rebuild_menu()

    async def handle_key(self, action: str, key: str):
        """Intercepts the back button to conditionally trigger the save modal."""
        if action == 'CLICK' and key == 'D':
            if set(self.pending_codecs) != set(self.original_codecs):
                async def on_yes():
                    ordered = [c for c in self.available_codecs if c in self.pending_codecs]
                    self.show_blocking_toast("Saving...")
                    try:
                        if hasattr(self.ui, 'bt_daemon') and self.ui.bt_daemon:
                            await self.ui.bt_daemon.apply_codec_rules(codecs=ordered)
                            if hasattr(self.ui, 'kernel') and self.ui.kernel:
                                await self.ui.kernel._enforce_audio_routing()
                    finally:
                        self.is_working = False
                        self.layout.toast_text = None
                        self.layout.modal = None
                        self.original_codecs = ordered.copy()
                        self.pending_codecs = ordered.copy()
                        await self.ui.switch_app(self.parent_app_id)

                async def on_no():
                    self.layout.modal = None
                    self.pending_codecs = self.original_codecs.copy()
                    self._rebuild_menu()
                    await self.ui.switch_app(self.parent_app_id)

                from src.ui.components import ConfirmModal
                modal = ConfirmModal(self.display, self.ui, "Save Changes?", "Apply Codecs?", on_yes, on_no)
                self.ui.active_overlay = modal
                self.ui.request_render()
            else:
                self.layout.modal = None
                await self.ui.switch_app(self.parent_app_id)
            return
        await super().handle_key(action, key)


class BluetoothQualityApp(ListMenuApp):
    """Sub-menu for configuring advanced AAC and LDAC codec quality settings directly in 53-no-hearing-protection.conf."""
    def __init__(self, display, ui_manager):
        self.original_state = {}
        self.pending_state = {}
        super().__init__(
            display=display,
            ui_manager=ui_manager,
            title="Quality Settings",
            items=[],
            parent_app_id='bluetooth_manager'
        )

    async def on_start(self):
        """Fetches live quality flags directly from 53-no-hearing-protection.conf on load."""
        from src.core.bluetooth_daemon import BluetoothDaemon
        self.layout.modal = None
        cfg = BluetoothDaemon.read_codec_config()
        self.original_state = {
            'aac_max': cfg["aac_max"],
            'ldac_max': cfg["ldac_max"]
        }
        self.pending_state = self.original_state.copy()
        self._rebuild_menu()
        if hasattr(super(), 'on_start'):
            await super().on_start()

    async def on_resume(self, previous_app_id=None):
        """Re-reads 53-no-hearing-protection.conf and clears stale modals when pulled from UIManager cache."""
        await self.on_start()

    def _rebuild_menu(self):
        """Constructs the visual checklist based on pending memory states."""
        items = [
            {
                "name": "AAC VBR (Max)",
                "prefix": "[★] " if self.pending_state['aac_max'] else "[ ] ",
                "action": lambda: self._toggle_quality('aac_max')
            },
            {
                "name": "LDAC HQ (990k)",
                "prefix": "[★] " if self.pending_state['ldac_max'] else "[ ] ",
                "action": lambda: self._toggle_quality('ldac_max')
            }
        ]
        self.update_items(items, preserve_cursor=True)
        self.ui.request_render()

    def _toggle_quality(self, key: str):
        """Flips the local boolean state in memory."""
        self.pending_state[key] = not self.pending_state[key]
        self._rebuild_menu()

    async def handle_key(self, action: str, key: str):
        """Intercepts the back button to conditionally trigger the save modal."""
        if action == 'CLICK' and key == 'D':
            if self.pending_state != self.original_state:
                async def on_yes():
                    self.show_blocking_toast("Saving...")
                    try:
                        if hasattr(self.ui, 'bt_daemon') and self.ui.bt_daemon:
                            await self.ui.bt_daemon.apply_codec_rules(
                                aac_max=self.pending_state['aac_max'],
                                ldac_max=self.pending_state['ldac_max']
                            )
                            if hasattr(self.ui, 'kernel') and self.ui.kernel:
                                await self.ui.kernel._enforce_audio_routing()
                    finally:
                        self.is_working = False
                        self.layout.toast_text = None
                        self.layout.modal = None
                        self.original_state = self.pending_state.copy()
                        await self.ui.switch_app(self.parent_app_id)

                async def on_no():
                    self.layout.modal = None
                    self.pending_state = self.original_state.copy()
                    self._rebuild_menu()
                    await self.ui.switch_app(self.parent_app_id)

                from src.ui.components import ConfirmModal
                modal = ConfirmModal(self.display, self.ui, "Save Changes?", "Apply Quality?", on_yes, on_no)
                self.ui.active_overlay = modal
                self.ui.request_render()
            else:
                # No changes made, instantly jump back
                await self.ui.switch_app(self.parent_app_id)
            return
            
        await super().handle_key(action, key)