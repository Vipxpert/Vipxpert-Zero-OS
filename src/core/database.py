# /home/vipxpert/vipxpert_minimal_os/src/core/database.py
import sqlite3
import os
from typing import Optional

class DatabaseManager:
    """
    Local SQLite storage engine. Handles persistent key-value configurations
    and provides the foundation for future application data (like IR signal sequences).
    """
    def __init__(self, db_path="vipxpert_os.db"):
        self.db_path = db_path
        self._settings_cache = {}
        self._settings_loaded = False
        self._init_db()

    def _init_db(self):
        """Creates base infrastructure tables if they do not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Generic Key-Value table for OS parameters
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            ''')
            
            # Relational IR Hardware Tables
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ir_devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ir_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    payload TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (device_id) REFERENCES ir_devices (id) ON DELETE CASCADE
                )
            ''')
            
            # Bluetooth Anti-Spam Blacklist
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bt_blacklist (
                    mac TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Offline Music Sessions
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS music_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    source_path TEXT NOT NULL,
                    track_index INTEGER DEFAULT 0,
                    position REAL DEFAULT 0.0,
                    is_shuffled BOOLEAN DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()

    # --- SETTINGS API ---

    def set_setting(self, key: str, value: str):
        """Upserts a configuration parameter gracefully and updates the in-memory write-through cache."""
        s_val = str(value)
        if not hasattr(self, "_settings_cache"):
            self._settings_cache = {}
        self._settings_cache[key] = s_val
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            ''', (key, s_val))
            conn.commit()

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieves a configuration value from the O(1) RAM cache, warming from SQLite once on first access."""
        if not getattr(self, "_settings_loaded", False):
            try:
                with self._get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT key, value FROM settings")
                    self._settings_cache = dict(cursor.fetchall())
                self._settings_loaded = True
            except Exception:
                self._settings_cache = getattr(self, "_settings_cache", {})
        return self._settings_cache.get(key, default)
        
    # --- IR DATABASE API ---

    def get_ir_devices(self) -> list:
        """Fetches all IR devices as dictionaries."""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ir_devices ORDER BY name COLLATE NOCASE")
            return [dict(row) for row in cursor.fetchall()]

    def add_ir_device(self, name: str) -> bool:
        """Creates a new IR device. Returns False if name already exists."""
        try:
            with self._get_connection() as conn:
                conn.cursor().execute("INSERT INTO ir_devices (name) VALUES (?)", (name,))
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_ir_device(self, device_id: int):
        """Deletes a device (cascades to automatically delete its commands)."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM ir_devices WHERE id = ?", (device_id,))
            conn.commit()

    def get_ir_commands(self, device_id: int) -> list:
        """Fetches all key slots for a specific device."""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ir_commands WHERE device_id = ? ORDER BY id ASC", (device_id,))
            return [dict(row) for row in cursor.fetchall()]

    def add_ir_command(self, device_id: int, name: str, payload: Optional[str] = None):
        """Adds a new key slot to a device."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "INSERT INTO ir_commands (device_id, name, payload) VALUES (?, ?, ?)",
                (device_id, name, payload)
            )
            conn.commit()

    def update_ir_command(self, command_id: int, name: Optional[str] = None, payload: Optional[str] = None):
        """Updates the name or payload of an existing key slot."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if name is not None:
                cursor.execute("UPDATE ir_commands SET name = ? WHERE id = ?", (name, command_id))
            if payload is not None:
                cursor.execute("UPDATE ir_commands SET payload = ? WHERE id = ?", (payload, command_id))
            conn.commit()
            
    def update_ir_device(self, device_id: int, name: str):
        """Updates the name of an existing device."""
        try:
            with self._get_connection() as conn:
                conn.cursor().execute("UPDATE ir_devices SET name = ? WHERE id = ?", (name, device_id))
                conn.commit()
        except sqlite3.IntegrityError:
            pass

    def delete_ir_command(self, command_id: int):
        """Removes a specific key slot."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM ir_commands WHERE id = ?", (command_id,))
            conn.commit()

    # --- BLUETOOTH BLACKLIST API ---

    def is_device_blocked(self, mac: str) -> bool:
        """Checks if a MAC address is on the auto-reject blacklist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM bt_blacklist WHERE mac = ?", (mac,))
            return cursor.fetchone() is not None

    def add_blocked_device(self, mac: str, name: str):
        """Adds a device to the blacklist (ignoring if it already exists)."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "INSERT OR IGNORE INTO bt_blacklist (mac, name) VALUES (?, ?)",
                (mac, name)
            )
            conn.commit()

    def get_blocked_devices(self) -> list:
        """Fetches all blacklisted devices for the UI."""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM bt_blacklist ORDER BY created_at DESC")
            return [dict(row) for row in cursor.fetchall()]

    def remove_blocked_device(self, mac: str):
        """Unblocks a device by removing it from the blacklist."""
        with self._get_connection() as conn:
            conn.cursor().execute("DELETE FROM bt_blacklist WHERE mac = ?", (mac,))
            conn.commit()

    # --- MUSIC API ---

    def _get_connection(self):
        """Generates a fresh transaction connection with foreign key support."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def get_music_sessions(self):
        with self._get_connection() as conn:
            return conn.execute("SELECT id, name, source_path, track_index, position FROM music_sessions ORDER BY updated_at DESC").fetchall()

    def get_session_by_name(self, name):
        with self._get_connection() as conn:
            return conn.execute("SELECT id, name, source_path, track_index, position FROM music_sessions WHERE name=?", (name,)).fetchone()

    def upsert_music_session(self, name, source_path, track_index=0, position=0.0):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO music_sessions (name, source_path, track_index, position) 
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET 
                source_path=excluded.source_path, 
                track_index=excluded.track_index, 
                position=excluded.position,
                updated_at=CURRENT_TIMESTAMP
            """, (name, source_path, track_index, position))

    def rename_music_session(self, s_id, new_name):
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE music_sessions SET name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (new_name, s_id)
                )
        except sqlite3.IntegrityError:
            pass

    def delete_music_session(self, s_id):
        with self._get_connection() as conn:
            conn.execute("DELETE FROM music_sessions WHERE id=?", (s_id,))