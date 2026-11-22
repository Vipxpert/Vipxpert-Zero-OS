# /vipxpert_minimal_os/src/apps/hotspot_app.py

import asyncio
from src.ui.components import ListMenuApp, TextInputModal

class HotspotManagerApp(ListMenuApp):
    """Simple menu for toggling hardware AP mode and managing broadcast credentials."""
    def __init__(self, display, ui_manager, db):
        self.db = db
        self.is_active = False
        
        # Fetch or initialize defaults
        self.ssid = self.db.get_setting("hotspot_ssid", "VipXpert_AP")
        self.pwd = self.db.get_setting("hotspot_pwd", "12345678")
        
        super().__init__(
            display=display, 
            ui_manager=ui_manager, 
            title="Hotspot", 
            items=[], 
            parent_app_id='settings'
        )

    async def on_start(self):
        self.show_blocking_toast("Checking radio...")
        await self._check_status()
        self._rebuild_menu()
        await super().on_start()
        
    async def update(self):
        """Silently poll hardware every 5 seconds to keep the toggle UI in sync."""
        self._tick_count = getattr(self, '_tick_count', 0) + 1
        if self._tick_count % 5 == 0:
            old_status = self.is_active
            await self._check_status()
            if old_status != self.is_active:
                self._rebuild_menu()
                
        await super().update()

    async def _check_status(self):
        """Checks if NetworkManager is currently broadcasting the AP."""
        try:
            # Check the active connection strictly attached to the wlan0 interface
            proc = await asyncio.create_subprocess_exec(
                'nmcli', '-t', '-f', 'DEVICE,CONNECTION', 'dev', 'status',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, _ = await proc.communicate()
            lines = out.decode().strip().split('\n')
            
            self.is_active = False
            for line in lines:
                if line.startswith('wlan0:'):
                    # nmcli names the AP profile 'Hotspot' by default
                    conn = line.split(':', 1)[1]
                    if conn == 'Hotspot':
                        self.is_active = True
                        break
        except Exception:
            pass

    async def _toggle_hotspot(self):
        """Executes the root-level hardware transition."""
        self.show_blocking_toast("Configuring...")
        try:
            if self.is_active:
                # Tear down the hotspot connection profile; nmcli auto-reconnects to known Wi-Fi
                proc = await asyncio.create_subprocess_exec('sudo', 'nmcli', 'con', 'down', 'Hotspot')
            else:
                # Hijack the radio, severing active Wi-Fi to spin up the AP
                proc = await asyncio.create_subprocess_exec(
                    'sudo', 'nmcli', 'device', 'wifi', 'hotspot', 
                    'ifname', 'wlan0', 'ssid', self.ssid, 'password', self.pwd
                )
            await proc.communicate()
        except Exception as e:
            print(f"[Hotspot] Toggle Error: {e}")
            
        await self._check_status()
        self._rebuild_menu()

    def _edit_ssid(self):
        async def on_complete(val):
            if val:
                self.ssid = val
                self.db.set_setting("hotspot_ssid", val)
            await self.ui.switch_app('hotspot_manager')
            self._rebuild_menu()
            
        async def on_cancel():
            await self.ui.switch_app('hotspot_manager')
            
        modal = TextInputModal(self.display, self.ui, "SSID:", on_complete, on_cancel)
        asyncio.create_task(self.ui.switch_app('text_input', fallback_app=modal))

    def _edit_pwd(self):
        async def on_complete(val):
            if val and len(val) >= 8:
                self.pwd = val
                self.db.set_setting("hotspot_pwd", val)
            elif val:
                # NetworkManager strictly enforces the WPA2 8-character minimum
                if hasattr(self, 'show_info_toast'):
                    self.show_info_toast("Err: 8 char min")
            await self.ui.switch_app('hotspot_manager')
            self._rebuild_menu()
            
        async def on_cancel():
            await self.ui.switch_app('hotspot_manager')
            
        modal = TextInputModal(self.display, self.ui, "Password:", on_complete, on_cancel)
        asyncio.create_task(self.ui.switch_app('text_input', fallback_app=modal))

    def _rebuild_menu(self):
        """Constructs the UI rows mapping to your actions."""
        status_str = "ON" if self.is_active else "OFF"
        obscured_pwd = "*" * min(len(self.pwd), 8) if self.pwd else "None"
        
        items = [
            {"name": f"Status: {status_str}", "action": lambda: asyncio.create_task(self._toggle_hotspot())},
            {"name": f"SSID: {self.ssid[:8]}", "action": self._edit_ssid},
            {"name": f"PWD: {obscured_pwd}", "action": self._edit_pwd}
        ]
        self.update_items(items, preserve_cursor=True)
        
        # Drop the UI lock and clear the toast so the user can interact again
        if getattr(self, 'is_working', False):
            self.is_working = False
            self.layout.toast_text = None
