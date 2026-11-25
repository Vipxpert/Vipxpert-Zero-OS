# /vipxpert_minimal_os/src/apps/recent_app.py

import time
from src.ui.base_app import BaseApp
from src.ui.components import ListMenuApp, ConfirmModal

class RecentApp(BaseApp):
    """
    Modern Live Screencap Card Switcher & Task Manager:
    - Modern mode: Horizontal live 72x36 screencap card deck with smooth gliding, card-zoom launch animation (C),
      swipe-up kill animation (0/2), and Kill All (#).
    - Minimal mode (ui_minimal_mode=1): Delegates to the classic text Task Manager menu.
    """
    PROTECTED_APPS = ('home', 'recent_tab', 'app_selector', 'task_mgr_root', 'task_mgr_list', 'confirm_modal', 'lock_screen')

    def __init__(self, display, ui_manager):
        super().__init__(display)
        self.ui = ui_manager
        self.cursor_index = 0
        self._anim_pos = 0.0
        self._last_t = time.monotonic()
        self._kill_anim_id = None
        self._kill_anim_ts = 0.0

    def _is_minimal(self) -> bool:
        if hasattr(self.ui, "is_minimal_ui") and self.ui.is_minimal_ui():
            return True
        db = getattr(self.ui, "db", None)
        return (db.get_setting("ui_recent_style", "modern") == "minimal") if db else False

    def _get_recent_ids(self) -> list:
        return [aid for aid in self.ui.active_apps.keys() if aid not in self.PROTECTED_APPS]

    async def on_start(self):
        if self._is_minimal():
            await self._mount_root_menu()
            return
        self._last_t = time.monotonic()
        ids = self._get_recent_ids()
        self.cursor_index = min(self.cursor_index, max(0, len(ids) - 1))
        self._anim_pos = float(self.cursor_index)
        self.ui.request_render()

    async def on_resume(self, previous_app_id=None):
        await self.on_start()

    async def on_stop(self):
        pass

    async def update(self):
        if self._is_minimal():
            await self._mount_root_menu()
            return
        self.ui.request_render()

    async def handle_key(self, action: str, key: str):
        if self._is_minimal():
            return
        ids = self._get_recent_ids()
        n = len(ids)
        if action in ("DOWN", "REPEAT") and n > 1:
            if key in ("A", "4"):
                self.cursor_index = (self.cursor_index - 1) % n
                self.ui.request_render()
                return
            if key in ("B", "6"):
                self.cursor_index = (self.cursor_index + 1) % n
                self.ui.request_render()
                return
        if action != "CLICK":
            return
        if key == "D":
            await self.ui.switch_app("home")
            return
        if n == 0:
            return
        cur_id = ids[min(self.cursor_index, n - 1)]
        if key == "C":
            # Trigger sleek card-zoom-to-fullscreen launch animation into the resumed app!
            try:
                self.ui._trans_prev_img = self.display.image.copy()
                self.ui._trans_start_ts = time.monotonic()
                self.ui._trans_kind = "zoom"
                self.ui._trans_dur = 0.20
            except Exception:
                pass
            await self.ui.switch_app(cur_id)
            return
        if key in ("0", "2"):
            self._kill_anim_id = cur_id
            self._kill_anim_ts = time.monotonic()
            await self.ui.destroy_app(cur_id)
            new_ids = self._get_recent_ids()
            self.cursor_index = min(self.cursor_index, max(0, len(new_ids) - 1))
            self.ui.show_toast(f"Closed {cur_id.replace('_', ' ').title()[:12]}", duration=1.0)
            self.ui.request_render()
            return
        if key == "#":
            await self._prompt_kill_all()
            return

    def _draw_state(self):
        if self._is_minimal():
            super()._draw_state()
            return
        d = self.display.draw
        ids = self._get_recent_ids()
        n = len(ids)

        # Header + Fixed Top-Right Battery %
        hdr = f"Recent {self.cursor_index + 1}/{n}" if n > 0 else "Recent Apps"
        self.display.draw_text(2, 0, hdr)
        if hasattr(self.ui, "draw_top_right_battery"):
            self.ui.draw_top_right_battery()
        d.line((0, 12, 127, 12), fill=1)

        if n == 0:
            self.display.draw_text(18, 28, "No Active Apps")
            self.display.draw_text(30, 52, "[D] Home")
            return

        self.cursor_index = min(self.cursor_index, n - 1)
        now_t = time.monotonic()
        dt = max(0.004, min(0.08, now_t - self._last_t))
        self._last_t = now_t
        diff = float(self.cursor_index) - self._anim_pos
        if n > 2:
            if diff > n * 0.5:
                diff -= n
            elif diff < -n * 0.5:
                diff += n
        if abs(diff) > 0.01:
            self._anim_pos = (self._anim_pos + diff * (1.0 - (2.718281828 ** (-22.0 * dt)))) % n
            self.display.has_active_marquee = True
        else:
            self._anim_pos = float(self.cursor_index)

        # Draw side-peek cards + center 72x36 live screencap card
        snaps = getattr(self.ui, "app_snapshots", {})
        spacing = 82.0
        slots = (-1, 1, 0) if n > 1 else (0,)
        for s_off in slots:
            idx = (int(round(self._anim_pos)) + s_off) % n
            rel = idx - self._anim_pos
            if n > 2:
                if rel > n * 0.5:
                    rel -= n
                elif rel < -n * 0.5:
                    rel += n
            if abs(rel) > 1.3:
                continue
            cx = int(round(64.0 + rel * spacing))
            cw, ch = (72, 36) if s_off == 0 else (56, 28)
            x0, y0 = cx - cw // 2, 15 + (36 - ch) // 2
            x1, y1 = x0 + cw - 1, y0 + ch - 1
            aid = ids[idx]
            snap = snaps.get(aid)
            if snap is not None:
                thumb = snap.resize((cw - 2, ch - 2))
                self.display.image.paste(thumb, (x0 + 1, y0 + 1))
            else:
                d.rectangle((x0 + 1, y0 + 1, x1 - 1, y1 - 1), fill=0)
                lbl = aid.replace("_", " ").title()[:10]
                self.display.draw_text(max(0, x0 + 6), y0 + ch // 2 - 6, lbl)
            d.rectangle((x0, y0, x1, y1), outline=1)
            if s_off == 0:
                d.rectangle((x0 - 1, y0 - 1, x1 + 1, y1 + 1), outline=1)

        # Footer: Active app name + quick controls
        cur_name = ids[self.cursor_index].replace("_", " ").title()[:11]
        self.display.draw_text(2, 52, cur_name)
        self.display.draw_text(74, 52, "C:Go 0:X")

    async def _mount_root_menu(self):
        items = [
            {"name": "Resume App", "action": lambda: self._mount_app_list('resume')},
            {"name": "Kill App", "action": lambda: self._mount_app_list('kill')},
            {"name": "Kill All Apps", "action": self._prompt_kill_all}
        ]
        menu = ListMenuApp(self.display, self.ui, "Task Manager", items, parent_app_id='home')
        await self.ui.switch_app('task_mgr_root', fallback_app=menu)

    async def _mount_app_list(self, mode: str, preserve_cursor=False):
        dynamic_items = []
        # Filter out core OS router apps from the killable list
        protected_apps = ['home', 'recent_tab', 'app_selector', 'task_mgr_root', 'task_mgr_list', 'confirm_modal', 'lock_screen']
        
        for app_id in self.ui.active_apps.keys():
            if app_id not in protected_apps:
                display_name = app_id.replace('_', ' ').title()
                
                # Bind dynamic lambda execution based on mode
                if mode == 'resume':
                    cb = lambda a=app_id: self.ui.switch_app(a)
                else:
                    cb = lambda a=app_id: self._execute_kill(a)
                    
                dynamic_items.append({"name": display_name, "action": cb})

        if not dynamic_items:
            dynamic_items.append({"name": "(No Apps)", "action": lambda: None})

        title = "Resume" if mode == 'resume' else "Kill App"
        
        if 'task_mgr_list' in self.ui.active_apps:
            app = self.ui.active_apps['task_mgr_list']
            # If we switched modes, reset the cursor
            keep_cursor = preserve_cursor and (app.title == title)
            app.title = title
            app.update_items(dynamic_items, preserve_cursor=keep_cursor, new_static_items=[])
            await self.ui.switch_app('task_mgr_list')
        else:
            menu = ListMenuApp(self.display, self.ui, title, dynamic_items, sort_methods=self.STANDARD_TEXT_SORTS, default_sort_key="A-Z", parent_app_id='task_mgr_root')
            await self.ui.switch_app('task_mgr_list', fallback_app=menu)

    async def _execute_kill(self, app_id: str):
        """Violently destroys the targeted app and instantly refreshes the current Kill list."""
        await self.ui.destroy_app(app_id)
        # Re-mount the list in 'kill' mode, perfectly preserving the cursor position
        await self._mount_app_list('kill', preserve_cursor=True)

    async def _prompt_kill_all(self):
        async def execute_kill_all():
            protected_apps = ['home', 'recent_tab', 'app_selector', 'task_mgr_root', 'task_mgr_list', 'confirm_modal', 'lock_screen']
            for a_id in list(self.ui.active_apps.keys()):
                if a_id not in protected_apps:
                    await self.ui.destroy_app(a_id)
            await self.ui.switch_app('home')

        async def cancel_kill():
            await self.ui.switch_app('task_mgr_root')

        confirm_ui = ConfirmModal(
            self.display, self.ui, 
            title="Warning", prompt="Kill all apps?", 
            on_yes=execute_kill_all, on_no=cancel_kill
        )
        await self.ui.switch_app('confirm_modal', fallback_app=confirm_ui)
