# /vipxpert_minimal_os/src/apps/wifi_app.py
import asyncio
from src.ui.components import ListMenuApp, TextInputModal
from typing import Optional

class WiFiSubMenu(ListMenuApp):
    """Dynamic context menu for interacting with a specific Wi-Fi network."""
    def __init__(self, display, ui_manager, ssid, state, auto_conn, parent_wifi_app):
        self.ssid = ssid
        self.parent_wifi = parent_wifi_app
        
        # Build options based on the exact network state
        items: list = []
        if state == 'active':
            items.append({"name": "Disconnect", "id": "disconnect"})
        elif state == 'saved':
            items.append({"name": "Connect", "id": "connect"})
            
        # Add the Auto-Connect toggle if it's a saved/known network
        if state in ['active', 'saved']:
            auto_str = "ON" if auto_conn else "OFF"
            items.append({"name": f"Auto-Conn: {auto_str}", "id": "toggle_auto"})
            items.append({"name": "Forget", "id": "forget"})
            
        static_items = [{"name": "Back", "id": "back"}]
        
        super().__init__(display, ui_manager, title=ssid[:10], items=items, static_items=static_items, parent_app_id='wifi_manager')

    async def handle_key(self, action: str, key: str):
        if action == 'CLICK' and key == 'C':
            cmd = self.items[self.cursor_index].get("id")
            if cmd == 'back':
                await self.ui.switch_app('wifi_manager')
            else:
                # Pass the execution back to the parent app
                await self.parent_wifi.execute_cmd(cmd, self.ssid)
            return
            
        await super().handle_key(action, key)


class WiFiManagerApp(ListMenuApp):
    """Asynchronous Wi-Fi Manager parsing direct nmcli hardware states."""
    def __init__(self, display, ui_manager, db):
        super().__init__(display, ui_manager, title="Wi-Fi", items=[], parent_app_id='settings')
        self.is_working: bool = False
        self._poll_task: Optional[asyncio.Task] = None
        self._ssid_to_profile: dict = {}

    async def on_start(self):
        self.show_blocking_toast("Loading...")
        await self._fetch_list()
        
        # Start the silent background polling loop (every 5 seconds)
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._auto_poll())

    async def on_stop(self):
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        if getattr(self, 'is_working', False):
            self.clear_toast()

    async def _auto_poll(self):
        """Silently refreshes the network list based on the global OS boot policy."""
        # Initialize the boot flag if it doesn't exist yet
        if not hasattr(self, '_has_boot_scanned'):
            self._has_boot_scanned = False
            
        while True:
            await asyncio.sleep(5)
            if self.is_working or self.ui.current_app_id != 'wifi_manager':
                continue
                
            policy = self.ui.db.get_setting("wifi_boot_policy", "Smart")
            
            if policy == "Keep Alive":
                await self._fetch_list()
            else:
                if policy == "Smart" and not self._has_boot_scanned:
                    await self._fetch_list()
                    self._has_boot_scanned = True
                else:
                    # Lightweight zero-cost check to detect if the background OS daemon killed the radio
                    proc = await asyncio.create_subprocess_exec(
                        'nmcli', '-t', 'radio', 'wifi',
                        stdout=asyncio.subprocess.PIPE
                    )
                    out, _ = await proc.communicate()
                    is_on = (out.decode().strip() == 'enabled')
                    
                    # If the physical hardware state drifted from our UI state, force a redraw
                    if getattr(self, '_last_radio_state', None) != is_on:
                        await self._fetch_list()

    async def _fetch_list(self):
        try:
            # 0. Check global radio state
            proc_radio = await asyncio.create_subprocess_exec(
                'nmcli', '-t', 'radio', 'wifi',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out_radio, _ = await proc_radio.communicate()
            is_on = (out_radio.decode().strip() == 'enabled')
            self._last_radio_state = is_on
            
            unique_networks = []
            
            if is_on:
                # 1. Get all saved Wi-Fi connection profiles and map 802-11-wireless.ssid -> profile NAME
                # Comment (invariant): NetworkManager profile NAMEs (e.g. 'netplan-wlan0-Stereo Google') often differ from the raw SSID; map by 802-11-wireless.ssid so saved networks show '*' and nmcli con up/modify/delete targets the real profile ID.
                proc_saved = await asyncio.create_subprocess_exec(
                    'nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                out_saved, _ = await proc_saved.communicate()
                wifi_profiles = []
                for _pline in out_saved.decode().strip().split('\n'):
                    if not _pline or ':' not in _pline:
                        continue
                    _pname, _ptype = _pline.rsplit(':', 1)
                    if _ptype == '802-11-wireless' and _pname:
                        wifi_profiles.append(_pname)
                async def _get_profile_ssid(_pname: str):
                    _p = await asyncio.create_subprocess_exec(
                        'nmcli', '-t', '-f', '802-11-wireless.ssid', 'con', 'show', 'id', _pname,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    _o, _ = await _p.communicate()
                    _s = _o.decode().strip()
                    if ':' in _s:
                        _s = _s.split(':', 1)[1]
                    return _pname, _s
                _mapped = await asyncio.gather(*(_get_profile_ssid(n) for n in wifi_profiles)) if wifi_profiles else []
                ssid_to_profile = {}
                for _pname, _pssid in _mapped:
                    if _pssid and (_pssid not in ssid_to_profile or _pname == _pssid):
                        ssid_to_profile[_pssid] = _pname
                self._ssid_to_profile = ssid_to_profile
    
                # 2. Get visible networks: SSID, SIGNAL, IN-USE
                proc_net = await asyncio.create_subprocess_exec(
                    'sudo', 'nmcli', '-t', '-f', 'SSID,SIGNAL,IN-USE', 'dev', 'wifi',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                out_net, _ = await proc_net.communicate()
                
                raw_networks = out_net.decode().strip().split('\n')
                parsed_networks = []
                
                for line in raw_networks:
                    if not line: continue
                    # Reverse split to handle SSIDs that contain colons safely
                    parts = line.rsplit(':', 2)
                    if len(parts) == 3:
                        ssid, signal_str, in_use_flag = parts
                        if not ssid: continue # Skip hidden networks
                        
                        try:
                            signal = int(signal_str)
                        except ValueError:
                            signal = 0
                            
                        is_active = (in_use_flag == '*')
                        is_saved = (ssid in ssid_to_profile)
                        parsed_networks.append({'ssid': ssid, 'signal': signal, 'active': is_active, 'saved': is_saved})
    
                # Sort: Active first, then by signal strength descending
                parsed_networks.sort(key=lambda x: (x['active'], x['signal']), reverse=True)
                
                # Remove duplicates
                seen = set()
                for net in parsed_networks:
                    if net['ssid'] not in seen:
                        seen.add(net['ssid'])
                        unique_networks.append(net)

            # 3. Build the UI List with your indicators
            new_items: list = []
            for net in unique_networks:
                prefix = "**" if net['active'] else "*" if net['saved'] else ""
                new_items.append({"name": net['ssid'], "prefix": prefix, "id": net['ssid']})

            # Append the dynamic radio toggle and boot policy
            radio_str = "ON" if is_on else "OFF"
            policy = self.ui.db.get_setting("wifi_boot_policy", "Smart")
            
            static_footer = [
                {"name": f"Policy: {policy}", "id": "toggle_boot_policy"},
                {"name": "Refresh", "id": "refresh"},
                {"name": f"Wi-Fi: {radio_str}", "id": "toggle_radio"}
            ]

            self.update_items(new_items, preserve_cursor=True, new_static_items=static_footer)
            if getattr(self, 'is_working', False):
                # Comment (invariant): Call clear_toast() instead of only clearing layout.toast_text so UIManager._toast_blocking is cleared on the universal Layer-3 compositor.
                self.clear_toast()
            
        except Exception as e:
            print(f"[WiFi App] Poll Error: {e}")
            if not self.items:
                self.update_items([{"name": "Scan Error", "id": "none"}], new_static_items=[])
            if getattr(self, 'is_working', False):
                self.clear_toast()

        # Only force redraw if user is actively looking at the list
        if self.ui.current_app_id == 'wifi_manager':
            await self.update()

    async def _force_rescan(self):
        """Forces a physical radio sweep."""
        self.show_blocking_toast("Scanning Radio...")
        
        try:
            proc = await asyncio.create_subprocess_exec('sudo', 'nmcli', 'dev', 'wifi', 'rescan')
            await proc.communicate()
        except Exception:
            pass
            
        await self._fetch_list()

    async def _toggle_radio(self):
        """Toggles the global Wi-Fi radio hardware state."""
        self.show_blocking_toast("Toggling Radio...")
        try:
            proc_check = await asyncio.create_subprocess_exec('nmcli', '-t', 'radio', 'wifi', stdout=asyncio.subprocess.PIPE)
            out, _ = await proc_check.communicate()
            is_on = (out.decode().strip() == 'enabled')
            
            new_state = 'off' if is_on else 'on'
            proc_set = await asyncio.create_subprocess_exec('sudo', 'nmcli', 'radio', 'wifi', new_state)
            await proc_set.communicate()
            
            # Give the radio a second to power cycle before scanning
            if new_state == 'on':
                await asyncio.sleep(1.5)
                
        except Exception as e:
            print(f"[WiFi App] Radio Toggle Error: {e}")
            
        await self._fetch_list()

    async def handle_key(self, action: str, key: str):
        if self.is_working: return
            
        if action != 'CLICK':
            await super().handle_key(action, key)
            return

        if key == 'C':
            if not self.items or self.items[0].get("id") == "none":
                return
                
            selected_ssid = self.items[self.cursor_index].get("id")
            prefix = self.items[self.cursor_index].get("prefix", "")
            
            if selected_ssid == "refresh":
                await self._force_rescan()
                return
            elif selected_ssid == "toggle_radio":
                await self._toggle_radio()
                return
            elif selected_ssid == "toggle_boot_policy":
                current = self.ui.db.get_setting("wifi_boot_policy", "Smart")
                next_mode = {"Smart": "Keep Alive", "Keep Alive": "Offline", "Offline": "Smart"}.get(current, "Smart")
                self.ui.db.set_setting("wifi_boot_policy", next_mode)
                
                # Hard fetch to rebuild the list and update the footer text instantly
                await self._fetch_list()
                return

            # Determine the state for the submenu
            if prefix == '**':
                state = 'active'
            elif prefix == '*':
                state = 'saved'
            else:
                state = 'unknown'

            if state in ['active', 'saved']:
                profile_id = self._ssid_to_profile.get(selected_ssid, selected_ssid)
                proc = await asyncio.create_subprocess_exec(
                    'nmcli', '-t', '-f', 'connection.autoconnect', 'con', 'show', 'id', profile_id,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                out, _ = await proc.communicate()
                val = out.decode().strip().lower()
                # nmcli returns 'yes', but we make it robust against empty strings or localized output
                auto_conn = ('yes' in val or 'true' in val)
                
                # Mount the dynamic context menu
                submenu = WiFiSubMenu(self.display, self.ui, selected_ssid, state, auto_conn, self)
                await self.ui.switch_app('wifi_submenu', fallback_app=submenu)
                
            else:
                # Unknown network: Mount the T9 Keyboard to get the password
                async def on_pwd_complete(password):
                    await self.execute_cmd('connect_new', selected_ssid, password)
                    
                async def on_pwd_cancel():
                    await self.ui.switch_app('wifi_manager')
                    
                text_ui = TextInputModal(
                    self.display, self.ui,
                    title=f"Pwd:{selected_ssid[:6]}",
                    on_complete=on_pwd_complete,
                    on_cancel=on_pwd_cancel
                )
                await self.ui.switch_app('text_input', fallback_app=text_ui)
            return
            
        await super().handle_key(action, key)

    async def execute_cmd(self, cmd: str, ssid: str, password: Optional[str] = None):
        """Executes actual nmcli network modifications."""
        await self.ui.switch_app('wifi_manager')
        if hasattr(self, 'show_blocking_toast'):
            self.show_blocking_toast("Executing...")
        
        try:
            profile_id = self._ssid_to_profile.get(ssid, ssid)
            if cmd == 'disconnect':
                proc = await asyncio.create_subprocess_exec('sudo', 'nmcli', 'con', 'down', 'id', profile_id)
            elif cmd == 'forget':
                proc = await asyncio.create_subprocess_exec('sudo', 'nmcli', 'con', 'delete', 'id', profile_id)
            elif cmd == 'connect':
                proc = await asyncio.create_subprocess_exec('sudo', 'nmcli', 'con', 'up', 'id', profile_id)
            elif cmd == 'connect_new':
                if password:
                    proc = await asyncio.create_subprocess_exec('sudo', 'nmcli', 'dev', 'wifi', 'connect', ssid, 'password', password)
                else:
                    proc = await asyncio.create_subprocess_exec('sudo', 'nmcli', 'dev', 'wifi', 'connect', ssid)
            elif cmd == 'toggle_auto':
                # Reading doesn't require sudo
                proc_chk = await asyncio.create_subprocess_exec(
                    'nmcli', '-t', '-f', 'connection.autoconnect', 'con', 'show', 'id', profile_id, 
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                out, _ = await proc_chk.communicate()
                val = out.decode().strip().lower()
                
                new_state = "yes" if ('no' in val or val == '') else "no"
                
                # Writing the new state requires sudo
                proc = await asyncio.create_subprocess_exec('sudo', 'nmcli', 'con', 'modify', 'id', profile_id, 'connection.autoconnect', new_state)
            
            await proc.communicate()
            
        except Exception as e:
            print(f"[WiFi App] Command failed: {e}")
            
        # Hard fetch to reflect the changes immediately
        await self._fetch_list()
