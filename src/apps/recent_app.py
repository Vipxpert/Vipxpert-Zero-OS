# /vipxpert_minimal_os/src/apps/recent_app.py

from src.ui.base_app import BaseApp
from src.ui.components import ListMenuApp, ConfirmModal

class RecentApp(BaseApp):
    """Invisible router managing the multi-layered Task Manager experience."""
    def __init__(self, display, ui_manager):
        super().__init__(display)
        self.ui = ui_manager

    async def on_start(self):
        await self._mount_root_menu()

    async def on_stop(self): pass
    
    async def update(self):
        # Smart Wake: Return to root menu when pulled from background
        await self._mount_root_menu()
        
    async def handle_key(self, action: str, key: str): pass

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