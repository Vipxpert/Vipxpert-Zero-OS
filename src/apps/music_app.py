# /vipxpert_minimal_os/src/apps/music_app.py
import os
import asyncio
from src.ui.base_app import BaseApp
from src.ui.components import ListMenuApp, CrudListApp

class OfflineMusicApp(BaseApp):
    """Router for Local Audio playback: Instant Play, Playlists, and Stateful Sessions."""
    def __init__(self, display, ui_manager, db):
        super().__init__(display)
        self.ui = ui_manager
        self.db = db
        self._tracker_task = None
        self.current_tape_path = "none"
        
        self.music_dir = os.path.join(os.getcwd(), 'music')
        os.makedirs(self.music_dir, exist_ok=True)
        
        self.mixtapes_dir = os.path.expanduser('~/.local/share/vipxpert_os/mixtapes')
        os.makedirs(self.mixtapes_dir, exist_ok=True)
        self.ghost_tape_path = os.path.join(self.mixtapes_dir, 'ghost_tape.m3u')

    async def on_start(self):
        # Spawn the Immortal Background Tracker!
        self._tracker_task = asyncio.create_task(self._ghost_tracker_loop())
        await self._mount_root_menu()

    async def on_stop(self):
        if self._tracker_task:
            self._tracker_task.cancel()

    async def _ghost_tracker_loop(self):
        """Detached task that writes the playhead to the DB every 5s only while local audio is playing."""
        while True:
            if hasattr(self.ui, 'media_arbiter') and self.ui.media_arbiter.active_source == 'LOCAL':
                state = self.ui.media_arbiter.local_audio.state
                if state.get('status') == 'playing' and self.current_tape_path != "none":
                    idx = state.get('playlist-pos', 0)
                    pos = state.get('time-pos', 0.0)
                    self.db.upsert_music_session('_GHOST_', self.current_tape_path, idx, pos)
                    await asyncio.sleep(5.0)
                    continue
            await asyncio.sleep(15.0)

    async def handle_key(self, action: str, key: str): pass
    
    async def update(self): 
        """Smart Wake: Re-route instantly to the root menu if pulled from background cache."""
        await self._mount_root_menu()

    async def _open_scene_viewer(self, scene_id: str = "music_split"):
        """Mounts any Lock Screen Scene Cartridge (Visualizer / Now Playing / 3D) directly in-app."""
        from src.apps.lock_app import LockScreenApp
        viewer = LockScreenApp(self.display, self.ui, standalone_scene=scene_id, parent_app_id="music_root")
        await self.ui.switch_app("music_scene_viewer", fallback_app=viewer)

    async def _mount_root_menu(self):
        items = [
            {"name": "Instant Play", "action": lambda: asyncio.create_task(self._mount_instant_play())},
            {"name": "Playlists", "action": lambda: asyncio.create_task(self._mount_playlists())},
            {"name": "Sessions", "action": lambda: asyncio.create_task(self._mount_sessions())},
            {"name": "Visualizer / Info", "action": lambda: asyncio.create_task(self._open_scene_viewer(self.db.get_setting("lock_slot_music_scene", "music_split")))}
        ]
        menu = ListMenuApp(self.display, self.ui, "Music", items, parent_app_id='app_selector')
        await self.ui.switch_app('music_root', fallback_app=menu)

    # --- 1. INSTANT PLAY ---

    SUPPORTED_EXTS = ('.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.opus')

    async def _mount_instant_play(self):
        # Scan for all supported audio files (MP3, FLAC, WAV, OGG, M4A, AAC, OPUS)
        audio_files = [
            f for f in os.listdir(self.music_dir)
            if f.lower().endswith(self.SUPPORTED_EXTS)
        ]
        audio_files.sort(key=lambda x: x.lower())

        active_path = ""
        is_local_playing = False
        if hasattr(self.ui, 'media_arbiter') and self.ui.media_arbiter.active_source == 'LOCAL':
            st = self.ui.media_arbiter.local_audio.state
            active_path = st.get('path', '')
            is_local_playing = st.get('status') in ('playing', 'paused')
        
        items = []
        all_paths = []
        
        # Build the dynamic paths list
        for f in audio_files:
            full_path = os.path.join(self.music_dir, f)
            all_paths.append(full_path)
            stem = os.path.splitext(f)[0]
            prefix = "► " if (is_local_playing and active_path == full_path) else ""
            items.append({
                "name": stem,
                "prefix": prefix,
                "path": full_path,
                "action": lambda p=full_path: self._play_raw(p)
            })
            
        # Inject "Shuffle All" at the very top of the list if there are tracks
        if items:
            items.insert(0, {
                "name": f"Shuffle All ({len(all_paths)})",
                "action": lambda paths=all_paths: self._play_shuffle(paths)
            })
        else:
            items.append({"name": "(No Audio Files)", "action": lambda: None})
            
        menu = ListMenuApp(
            self.display, self.ui, "All Tracks", items,
            sort_methods=self.STANDARD_TEXT_SORTS,
            parent_app_id='music_root'
        )
        await self.ui.switch_app('music_instant', fallback_app=menu)

    def _play_shuffle(self, paths):
        """Randomizes the given paths, generates a temporary list, and fires it."""
        import random
        shuffled = paths.copy()
        random.shuffle(shuffled)
        
        menu = self.ui.current_app
        if hasattr(menu, 'show_info_toast'):
            menu.show_info_toast("Shuffling...")
            
        if hasattr(self.ui, 'media_arbiter'):
            self.ui.media_arbiter.force_source('LOCAL')
            
            # Write physical tape to disk
            with open(self.ghost_tape_path, 'w') as f:
                f.write("\n".join(shuffled))
            self.current_tape_path = self.ghost_tape_path
                
            self._cmd_task = asyncio.create_task(self._send_playlist_sequence(self.ghost_tape_path, 0))

    # --- 2. PLAYLISTS ---

    async def _mount_playlists(self):
        # Scan for M3U map files
        m3u_files = [f for f in os.listdir(self.music_dir) if f.lower().endswith('.m3u')]
        m3u_files.sort()
        
        items = []
        for f in m3u_files:
            items.append({
                "name": f[:-4],
                "action": lambda path=os.path.join(self.music_dir, f): self._play_raw(path)
            })
            
        if not items:
            items.append({"name": "(No Playlists)", "action": lambda: None})
            
        menu = ListMenuApp(self.display, self.ui, "Playlists", items, parent_app_id='music_root')
        await self.ui.switch_app('music_playlists', fallback_app=menu)

    # --- 3. SESSIONS (Tapes) ---

    async def _mount_sessions(self):
        sessions = self.db.get_music_sessions()
        ghost = next((s for s in sessions if s[1] == '_GHOST_'), None)
        saved = [s for s in sessions if s[1] != '_GHOST_']
        
        items = []
        if ghost:
            items.append({
                "name": "[ Last Session ]",
                "raw": {'id': ghost[0], 'name': ghost[1], 'path': ghost[2], 'idx': ghost[3], 'pos': ghost[4]},
                "action": lambda session_data={'id': ghost[0], 'name': ghost[1], 'path': ghost[2], 'idx': ghost[3], 'pos': ghost[4]}: asyncio.create_task(self._prompt_session_action(session_data))
            })
            
        for s in saved:
            items.append({
                "name": s[1],
                "raw": {'id': s[0], 'name': s[1], 'path': s[2], 'idx': s[3], 'pos': s[4]},
                "action": lambda session_data={'id': s[0], 'name': s[1], 'path': s[2], 'idx': s[3], 'pos': s[4]}: asyncio.create_task(self._prompt_session_action(session_data))
            })
            
        if not items:
            items.append({"name": "(No Sessions)", "raw": None, "action": lambda: None})
            
        crud_config = {
            'fields': ['Name'],
            'on_add': lambda res: self._db_add_session(res['Name']),
            'on_edit': lambda s, res: self._db_edit_session(s['id'], res['Name']),
            'on_delete': lambda s: self._db_delete_session(s['id']),
            'on_refresh': self._mount_sessions
        }
        
        app = CrudListApp(self.display, self.ui, "Sessions", items, crud_config, parent_app_id='music_root')
        await self.ui.switch_app('music_sessions', fallback_app=app)

    async def _prompt_session_action(self, session_data):
        """The 2-Step Workflow: Ask the user what to do with the clicked Session."""
        items = [
            {"name": "Resume Session", "action": lambda: self._play_session(session_data)},
            {"name": "Overwrite w/ Current", "action": lambda: self._overwrite_session(session_data)},
            {"name": "Cancel", "action": lambda: asyncio.create_task(self._mount_sessions())}
        ]
        menu = ListMenuApp(self.display, self.ui, "Action", items, parent_app_id='music_sessions')
        await self.ui.switch_app('music_session_action', fallback_app=menu)

    # --- AUDIO EXECUTION ---

    def _play_raw(self, path):
        """Fires a standard track or playlist."""
        menu = self.ui.current_app
        if hasattr(menu, 'show_info_toast'):
            menu.show_info_toast("Playing...")
            
        if hasattr(self.ui, 'media_arbiter'):
            self.ui.media_arbiter.force_source('LOCAL')
            
            # If it's an m3u file (from the Playlists menu), just load the list natively
            if path.lower().endswith('.m3u'):
                self.current_tape_path = path
                self._cmd_task = asyncio.create_task(self.ui.media_arbiter.local_audio.send_command(['loadlist', path, 'replace']))
                return
                
            # If it's an MP3 (from Instant Play), build a physical playlist so Next/Prev works!
            if hasattr(menu, 'items'):
                valid_paths = [item['path'] for item in menu.items if 'path' in item]
                if valid_paths and path in valid_paths:
                    clicked_index = valid_paths.index(path)
                    
                    with open(self.ghost_tape_path, 'w') as f:
                        f.write("\n".join(valid_paths))
                    self.current_tape_path = self.ghost_tape_path
                        
                    asyncio.create_task(self._send_playlist_sequence(self.ghost_tape_path, clicked_index))
                    return
                    
            self.current_tape_path = path
            self._cmd_task = asyncio.create_task(self.ui.media_arbiter.local_audio.send_command(['loadfile', path]))

    def _play_session(self, session_data):
        """Resumes a specific tape exactly where they left off."""
        if hasattr(self.ui, 'media_arbiter'):
            self.current_tape_path = session_data['path']
            self.ui.media_arbiter.force_source('LOCAL')
            # Fire the unified, strictly ordered startup sequence
            asyncio.create_task(self._send_playlist_sequence(session_data['path'], session_data['idx'], session_data['pos']))
            asyncio.create_task(self.ui.switch_app('lock_screen'))

    async def _send_playlist_sequence(self, m3u_path, index, start_pos=0.0):
        """Helper to safely sequence loading a list, jumping tracks, and seeking time."""
        local_audio = self.ui.media_arbiter.local_audio
        
        # 1. Load the playlist file
        await local_audio.send_command(['loadlist', m3u_path, 'replace'])
        await asyncio.sleep(0.2) # Wait for MPV to map the M3U file
        
        # 2. Jump to the correct track if it's not the first one
        if index > 0:
            await local_audio.send_command(['set_property', 'playlist-pos', index])
            await asyncio.sleep(0.3) # Wait for MPV to transition the file
            
        # 3. Seek to the exact timestamp
        if start_pos > 0.0:
            await local_audio.send_command(['set_property', 'time-pos', start_pos])

    def _overwrite_session(self, session_data):
        """Takes current Arbiter state and saves it to this Session label."""
        if hasattr(self.ui, 'media_arbiter'):
            state = self.ui.media_arbiter.local_audio.state
            idx = int(state.get('playlist-pos', 0) or 0)
            pos = float(state.get('time-pos', state.get('position', 0.0)) or 0.0)
            target_path = self.current_tape_path
            if target_path == "none":
                ghost = self.db.get_session_by_name('_GHOST_')
                target_path = ghost[2] if ghost else (state.get('path') or "none")

            if target_path == self.ghost_tape_path and session_data['name'] != '_GHOST_' and os.path.exists(self.ghost_tape_path):
                import shutil
                new_path = os.path.join(self.mixtapes_dir, f"{session_data['name']}.m3u")
                shutil.copy(self.ghost_tape_path, new_path)
                target_path = new_path

            if target_path and target_path != "none":
                self.db.upsert_music_session(session_data['name'], target_path, idx, pos)

        async def _finish_overwrite():
            await self._mount_sessions()
            active = getattr(self.ui, 'current_app', None)
            if active and hasattr(active, 'show_info_toast'):
                active.show_info_toast("Overwritten!")

        asyncio.create_task(_finish_overwrite())

    # --- DB HELPERS ---
    def _db_add_session(self, name):
        """Saves current ghost session as a new physical tape."""
        ghost = self.db.get_session_by_name('_GHOST_')
        state = self.ui.media_arbiter.local_audio.state if hasattr(self.ui, 'media_arbiter') else {}
        target_path = self.current_tape_path if self.current_tape_path != "none" else (ghost[2] if ghost else "none")
        idx = int(state.get('playlist-pos', ghost[3] if ghost else 0) or 0)
        pos = float(state.get('time-pos', state.get('position', ghost[4] if ghost else 0.0)) or 0.0)
        
        # If saving an Instant/Shuffle play, physically duplicate the tape so it persists!
        if target_path == self.ghost_tape_path and os.path.exists(self.ghost_tape_path):
            import shutil
            new_path = os.path.join(self.mixtapes_dir, f"{name}.m3u")
            shutil.copy(self.ghost_tape_path, new_path)
            target_path = new_path
            
        self.db.upsert_music_session(name, target_path, idx, pos)
        
    def _db_edit_session(self, s_id, name):
        if name:
            self.db.rename_music_session(s_id, name)
        
    def _db_delete_session(self, s_id):
        self.db.delete_music_session(s_id)