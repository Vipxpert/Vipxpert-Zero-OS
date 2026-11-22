# /vipxpert_minimal_os/src/apps/ir_app.py
import asyncio
from src.ui.base_app import BaseApp
from src.ui.components import CrudListApp
from src.ui.components import ConfirmModal

class IRRootApp(CrudListApp):
    """Custom root app for IR to intercept exit and safely restore Wi-Fi."""
    def __init__(self, display, ui_manager, title, items, crud_config, controller, **kwargs):
        self.controller = controller
        super().__init__(display, ui_manager, title, items, crud_config, **kwargs)

    async def _prompt_wifi_disable(self):
        """Safely checks and prompts to disable Wi-Fi on entry or resume."""
        self.controller.wifi_was_on = await self.controller._check_wifi()
        if self.controller.wifi_was_on:
            async def on_yes():
                self.show_blocking_toast("Disabling...")
                try:
                    await self.controller._set_wifi(False)
                finally:
                    self.is_working = False
                    self.layout.toast_text = None
                    self.ui.request_render()
            
            async def on_no():
                pass
                
            modal = ConfirmModal(self.display, self.ui, "IR Timing", "Disable Wi-Fi?", on_yes, on_no)
            self.ui.active_overlay = modal
            self.ui.request_render()

    async def on_start(self):
        # Trigger super class boot if it exists, then check Wi-Fi
        if hasattr(super(), 'on_start'):
            await super().on_start()
        await self._prompt_wifi_disable()

    async def on_resume(self, previous_app_id: str = None):
        """Intercepts the new UIManager wakeup hook."""
        # The invisible controller double-hops and masks the parent ID.
        # Instead, we just verify we are NOT returning from a known child menu!
        if previous_app_id not in ['ir_tester', 'ir_commands']:
            await self._prompt_wifi_disable()

    async def handle_key(self, action: str, key: str):
        # Intercept the 'D' button only if we are at the root level (NORMAL mode)
        if action == 'CLICK' and key == 'D' and getattr(self, 'action_mode', 'NORMAL') == 'NORMAL':
            if getattr(self.controller, 'wifi_was_on', False):
                
                # Check actual current state before prompting
                current_wifi = await self.controller._check_wifi()
                
                if not current_wifi:
                    async def on_yes():
                        self.show_blocking_toast("Restoring...")
                        try:
                            await self.controller._set_wifi(True)
                        finally:
                            self.controller.wifi_was_on = False
                            self.is_working = False
                            self.layout.toast_text = None
                            await self.ui.switch_app(self.parent_app_id)
                            
                    async def on_no():
                        self.controller.wifi_was_on = False
                        await self.ui.switch_app(self.parent_app_id)
                        
                    modal = ConfirmModal(self.display, self.ui, "Leaving IR", "Restore Wi-Fi?", on_yes, on_no)
                    self.ui.active_overlay = modal
                    self.ui.request_render()
                    return
            
            # If wifi wasn't on initially, or it is ALREADY on right now, just exit cleanly
            self.controller.wifi_was_on = False
            await self.ui.switch_app(self.parent_app_id)
            return
                
        await super().handle_key(action, key)

class IRControllerApp(BaseApp):
    """
    Stateful Controller App acting as a dynamic router for the IR subsystem.
    It provisions CrudListApps on the fly to seamlessly handle IR hardware logic.
    """
    def __init__(self, display, ui_manager, db, ir_hardware):
        super().__init__(display)
        self.ui = ui_manager
        self.db = db
        self.ir = ir_hardware
        self.wifi_was_on = False

    async def _check_wifi(self) -> bool:
        """Polls the kernel for actual Wi-Fi radio state."""
        proc = await asyncio.create_subprocess_shell("nmcli radio wifi", stdout=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        return "enabled" in stdout.decode().strip()

    async def _set_wifi(self, enable: bool):
        """Executes sudo rfkill/nmcli toggle."""
        state = "on" if enable else "off"
        proc = await asyncio.create_subprocess_shell(f"sudo nmcli radio wifi {state}")
        await proc.communicate()

    async def on_start(self):
        # Mount the visual list first so the background isn't black. 
        # The IRRootApp now handles the Wi-Fi entry prompt securely via on_start/on_resume.
        await self._mount_remotes()

    async def on_stop(self): pass
    
    async def update(self):
        await self._mount_remotes()
        
    async def handle_key(self, action: str, key: str): pass


    # --- 1. REMOTE HUB ---

    async def _mount_remotes(self):
        devices = self.db.get_ir_devices()
        
        items = []
        
        # Inject the hardware tester at the top of the hub
        items.append({
            "name": "[ Test Receiver ]",
            "raw": None,
            "action": self._launch_tester
        })
        
        for d in devices:
            items.append({
                "name": d['name'], 
                "raw": d, 
                "action": lambda dev=d: asyncio.create_task(self._mount_commands(dev))
            })
            
        if not items:
            items.append({"name": "(No Devices)", "raw": None, "action": lambda: None})
            
        crud_config = {
            'fields': ['Name'],
            'on_add': lambda res: self.db.add_ir_device(res['Name']),
            'on_edit': lambda dev, res: self.db.update_ir_device(dev['id'], res['Name']),
            'on_delete': lambda dev: self.db.delete_ir_device(dev['id']),
            'on_refresh': self._mount_remotes
        }
        
        safe_sorts = {
            "A-Z": (lambda x: x.get('name', '').lower(), False),
            "Z-A": (lambda x: x.get('name', '').lower(), True)
        }
        
        active_apps = getattr(self.ui, 'active_apps', {})
        if 'ir_remotes' in active_apps:
            app = active_apps['ir_remotes']
            app.refresh_dynamic_items(items)
            await self.ui.switch_app('ir_remotes')
        else:
            app = IRRootApp(self.display, self.ui, "IR Remotes", items, crud_config, controller=self, sort_methods=safe_sorts, default_sort_key="A-Z", parent_app_id='app_selector')
            await self.ui.switch_app('ir_remotes', fallback_app=app)

    def _launch_tester(self):
        """Spawns the raw Oscilloscope UI."""
        from src.apps.ir_test_app import IRTestApp
        app = getattr(self.ui, 'ir_tester', None)
        if not app:
            app = IRTestApp(self.display, self.ui, self.ir)
            self.ui.ir_tester = app
            
        asyncio.create_task(self.ui.switch_app('ir_tester', fallback_app=app))

    # --- 2. COMMAND HUB ---

    async def _mount_commands(self, device):
        cmds = self.db.get_ir_commands(device['id'])
        
        items = []
        for c in cmds:
            suffix = "" if c['payload'] else "(empty)"
            items.append({
                "name": c['name'], 
                "suffix": suffix,
                "raw": c, 
                "action": lambda cmd=c: asyncio.create_task(self._fire_payload(cmd))
            })
            
        if not items:
            items.append({"name": "(No Keys)", "raw": None, "action": lambda: None})
            
        crud_config = {
            'fields': ['Name'],
            'on_add': lambda res: self.db.add_ir_command(device['id'], res['Name']),
            'on_edit': lambda cmd, res: self.db.update_ir_command(cmd['id'], res['Name']),
            'on_delete': lambda cmd: self.db.delete_ir_command(cmd['id']),
            'on_refresh': lambda: self._mount_commands(device),
            'custom_modes': [
                {'mode': 'LEARN', 'label': 'Learn Mode', 'prefix': '[L] ', 'on_select': lambda cmd: self._do_learn_capture(cmd, device)}
            ]
        }

        safe_sorts = {
            "A-Z": (lambda x: x.get('name', '').lower(), False),
            "Z-A": (lambda x: x.get('name', '').lower(), True)
        }
        
        active_apps = getattr(self.ui, 'active_apps', {})
        if 'ir_commands' in active_apps:
            app = active_apps['ir_commands']
            if app.title != device['name']:
                app.title = device['name']
                app.cursor_index = 0
                app.top_index = 0
            app.crud_config = crud_config
            app.refresh_dynamic_items(items)
            await self.ui.switch_app('ir_commands')
        else:
            app = CrudListApp(self.display, self.ui, device['name'], items, crud_config, sort_methods=safe_sorts, default_sort_key="A-Z", parent_app_id='ir_remotes')
            await self.ui.switch_app('ir_commands', fallback_app=app)

    async def _fire_payload(self, cmd):
        if not cmd['payload']:
            return
            
        app = self.ui.current_app
        if hasattr(app, 'show_blocking_toast'):
            app.show_blocking_toast("Transmitting...")
            
        try:
            success = await self.ir.send_raw(cmd['payload'])
            if hasattr(app, 'show_info_toast'):
                app.show_info_toast("Sent!" if success else "Failed!")
        except Exception as e:
            if hasattr(app, 'show_info_toast'):
                app.show_info_toast("Error")


    # --- 3. HARDWARE RECEIVER ---
    
    async def _do_learn_capture(self, cmd, device):
        app = self.ui.current_app
        cancel_event = asyncio.Event()
        
        # We spawn a ConfirmModal configured for single-key Cancel ('D')
        async def on_cancel():
            cancel_event.set()
            
        modal = ConfirmModal(
            self.display, self.ui, f"Learn: {cmd['name']}",
            ["Point remote at Pi", "Press key (1/2)..."],
            None, on_cancel, footer="[D] Cancel Capture"
        )
        self.ui.active_overlay = modal
        self.ui.request_render()
        
        def update_modal(line1: str, line2: str, is_error: bool = False):
            modal.title = f"Learn: {cmd['name']}"
            modal.prompt = [line1, line2]
            modal.footer = "[D] Cancel Capture"
            self.ui.request_render()
        
        captures = []
        max_attempts = 8
        target_matches = 2
        
        try:
            for attempt in range(1, max_attempts + 1):
                if cancel_event.is_set():
                    break
                    
                if attempt == 1:
                    update_modal("Point remote at Pi", "Press key (1/2)...")
                elif attempt == 2:
                    update_modal("Signal seen!", "Press again (2/2)...")
                else:
                    update_modal("Mismatch!", f"Need 3 matches (Try {attempt}/{max_attempts})", is_error=(attempt==3))
                    if attempt == 3:
                        await asyncio.sleep(0.5) # Give them time to read the error on the first mismatch
                    
                payload = await self.ir.receive_raw(timeout=7, cancel_event=cancel_event)
                
                if payload == "CANCELLED":
                    break
                    
                if not payload:
                    if hasattr(app, 'show_info_toast'):
                        app.show_info_toast("Timeout.")
                    break
                    
                captures.append(payload)
                
                # Evaluate Capture State
                if len(captures) == 2:
                    if self.ir._fuzzy_match_signals(captures[0], captures[1]):
                        self.db.update_ir_command(cmd['id'], payload=captures[1])
                        if hasattr(app, 'show_info_toast'):
                            app.show_info_toast("Verified & Saved!")
                        break
                    else:
                        target_matches = 3
                        
                elif len(captures) >= 3:
                    # Find the largest cluster of matching signals
                    best_match_count = 0
                    best_payload = None
                    
                    for base_sig in captures:
                        match_count = sum(1 for c in captures if self.ir._fuzzy_match_signals(base_sig, c))
                        if match_count > best_match_count:
                            best_match_count = match_count
                            best_payload = base_sig
                            
                    if best_match_count >= target_matches:
                        self.db.update_ir_command(cmd['id'], payload=best_payload)
                        if hasattr(app, 'show_info_toast'):
                            app.show_info_toast("Verified & Saved!")
                        break
                        
                if attempt == max_attempts:
                    if hasattr(app, 'show_info_toast'):
                        app.show_info_toast("Too much noise. Fail.")
                        
        except Exception as e:
            if hasattr(app, 'show_info_toast'):
                app.show_info_toast("Hardware Error")
                
        finally:
            if hasattr(app, 'layout'):
                app.layout.modal = None
                app.layout.render()
            self.ui.active_overlay = None
            
        await self._mount_commands(device)
