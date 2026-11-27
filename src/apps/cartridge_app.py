# /vipxpert_minimal_os/src/apps/cartridge_app.py
import asyncio
import json
import mmap
import os
import struct
import subprocess
from typing import Dict, List, Optional
from src.ui.base_app import BaseApp
from src.ui.components import ListMenuApp

SHM_HEADER_SIZE = 64
OLED_W = 128
OLED_H = 64
SHM_TOTAL_SIZE = SHM_HEADER_SIZE + OLED_W * OLED_H

KEY_TO_BIT = {
    '0': 0, '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7,
    '8': 8, '9': 9, 'A': 10, 'B': 11, 'C': 12, 'D': 13, '*': 14, '#': 15,
}

KEY_TO_W4_BIT = {
    '5': 0x01, 'C': 0x01,
    'A': 0x02, 'B': 0x02,
    '4': 0x10,
    '6': 0x20,
    '2': 0x40,
    '8': 0x80,
}

SCALE_MODE_LABELS = ("Scale: Fill (Cover)", "Scale: Fit (64x64)", "Scale: 1:1 Crop")


def _get_repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _get_cart_dir() -> str:
    return os.path.join(_get_repo_root(), "cartridges")


EXT_TO_RUNTIME = {
    ".wasm": ("wasm4", "[W4]"),
    ".nes": ("libretro:nestopia", "[NES]"),
    ".gb": ("libretro:gambatte", "[GB]"),
    ".gbc": ("libretro:gambatte", "[GBC]"),
    ".gba": ("libretro:mgba", "[GBA]"),
}


def discover_cartridges() -> List[Dict]:
    """Scans /cartridges/ for .wasm, .nes, .gb/.gbc, .gba ROMs and manifest.json cartridge bundles."""
    cart_dir = _get_cart_dir()
    os.makedirs(cart_dir, exist_ok=True)
    carts: List[Dict] = []

    for entry in sorted(os.listdir(cart_dir)):
        full = os.path.join(cart_dir, entry)
        ext = os.path.splitext(entry)[1].lower()
        if os.path.isfile(full) and ext in EXT_TO_RUNTIME:
            rt, tag = EXT_TO_RUNTIME[ext]
            stem = os.path.splitext(entry)[0].replace("_", " ").replace("-", " ").title()
            carts.append({
                "name": stem,
                "runtime": rt,
                "tag": tag,
                "entry": full,
                "fps": 60,
            })
        elif os.path.isdir(full):
            manifest_path = os.path.join(full, "manifest.json")
            meta: Dict = {}
            if os.path.isfile(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as mf:
                        meta = json.load(mf)
                except Exception as e:
                    print(f"[Cartridges] Invalid manifest in {entry}: {e}", flush=True)
            runtime = str(meta.get("runtime", "")).lower()
            entry_rel = meta.get("entry")
            entry_path = os.path.join(full, entry_rel) if entry_rel else ""
            if not entry_path or not os.path.exists(entry_path):
                for cand in sorted(os.listdir(full)):
                    if cand.lower().endswith(".wasm"):
                        entry_path = os.path.join(full, cand)
                        runtime = runtime or "wasm4"
                        break
            if entry_path and os.path.isfile(entry_path):
                carts.append({
                    "name": str(meta.get("name") or entry.replace("_", " ").title()),
                    "runtime": runtime or "wasm4",
                    "entry": entry_path,
                    "fps": int(meta.get("fps", 60)),
                })
    return carts


class CartridgeHostApp(BaseApp):
    """Process-isolated SHM+IPC cartridge host blitting 4-shade frames into Display.draw_gray_frame()."""
    def __init__(self, display, ui_manager, cart_spec: Dict):
        super().__init__(display)
        self.ui = ui_manager
        self.cart_spec = cart_spec
        self._proc: Optional[subprocess.Popen] = None
        self._shm_path: Optional[str] = None
        self._shm_fd: Optional[int] = None
        self._shm_mm: Optional[mmap.mmap] = None
        self._last_fb: bytes = bytes(OLED_W * OLED_H)
        self._last_seq: int = 0
        self._buttons_down: int = 0
        self._w4_gamepad1: int = 0
        # Comment (quality): Default to scale_mode=3 (exact fractional area-weighted box resampling) so cartridges open flicker-free in Hybrid mode without waiting for '#'.
        self._scale_mode: int = 3
        self._zoom_mode: bool = False
        self._zoom_pct: int = 100
        self._pan_x: int = 0
        self._pan_y: int = 0
        self._preset_idx: int = 0
        self._audio_muted: bool = False
        self._pump_task: Optional[asyncio.Task] = None
        self._error_msg: Optional[str] = None

    def _safe_worker_cores(self) -> List[int]:
        # Comment (invariant): Never pin sandboxed cartridges onto Core 1 (OLED_Compositor) or Core 3 (PipeWire/I2S DMA); fall back safely on weak 1-2 core boards per N007.
        cpu_n = os.cpu_count() or 1
        if cpu_n >= 4:
            return [0, 2]
        return [0]

    def _build_command(self, shm_path: str) -> Optional[List[str]]:
        runtime = self.cart_spec.get("runtime", "wasm4")
        entry = self.cart_spec.get("entry", "")
        if runtime == "wasm4":
            adapter = os.path.join(_get_repo_root(), "bin", "cart_wasm4")
            if not os.path.isfile(adapter):
                self._error_msg = "Missing bin/cart_wasm4"
                return None
            return [adapter, entry, shm_path]
        if runtime.startswith("libretro:"):
            core_name = runtime.split(":", 1)[1]
            adapter = os.path.join(_get_repo_root(), "bin", "cart_libretro")
            if not os.path.isfile(adapter):
                self._error_msg = "Missing bin/cart_libretro"
                return None
            so_name = f"{core_name}_libretro.so"
            core_so = None
            for base in (
                os.path.join(_get_repo_root(), "bin", "libretro"),
                "/usr/lib/aarch64-linux-gnu/libretro",
                "/usr/lib/libretro",
            ):
                cand = os.path.join(base, so_name)
                if os.path.isfile(cand):
                    core_so = cand
                    break
            if not core_so:
                self._error_msg = f"Missing core {so_name}"
                return None
            return [adapter, core_so, entry, shm_path]
        if runtime == "native" and os.access(entry, os.X_OK):
            return [entry, shm_path]
        self._error_msg = f"Unsupported runtime: {runtime}"
        return None

    def _sync_shm_control(self, quit_req: bool = False):
        if self._shm_mm is None:
            return
        flags = (1 if quit_req else 0) | (2 if self._audio_muted else 0) | ((self._scale_mode & 0x3) << 2)
        fps = max(15, min(60, int(self.cart_spec.get("fps", 60))))
        struct.pack_into("<IIII", self._shm_mm, 12, flags, self._buttons_down, self._w4_gamepad1, fps)
        # Comment (invariant): Offset 28 is worker status; pack host zoom/pan at offsets 32, 36, 40.
        struct.pack_into("<Iii", self._shm_mm, 32, int(self._zoom_pct), int(self._pan_x), int(self._pan_y))

    def _get_db(self):
        return getattr(self.ui, "db", None) or getattr(getattr(self.ui, "os_kernel", None), "db", None)

    def _cart_view_key(self) -> str:
        entry = str(self.cart_spec.get("entry") or self.cart_spec.get("name") or "default")
        return f"cart_view:{os.path.basename(entry)}"

    def _load_cart_view(self):
        db = self._get_db()
        if not db:
            return
        try:
            raw = db.get_setting(self._cart_view_key(), "")
            if not raw:
                return
            data = json.loads(raw)
            if "scale_mode" in data:
                self._scale_mode = max(0, min(3, int(data["scale_mode"])))
            if "zoom_pct" in data:
                zp = int(data["zoom_pct"])
                self._zoom_pct = zp if zp in (3, 4) else max(10, min(400, zp))
            if "pan_x" in data:
                self._pan_x = max(-256, min(256, int(data["pan_x"])))
            if "pan_y" in data:
                self._pan_y = max(-256, min(256, int(data["pan_y"])))
        except Exception as e:
            print(f"[Cartridges] Failed to load saved view: {e}", flush=True)

    def _save_cart_view(self):
        db = self._get_db()
        if not db:
            return
        try:
            payload = json.dumps({
                "scale_mode": int(self._scale_mode),
                "zoom_pct": int(self._zoom_pct),
                "pan_x": int(self._pan_x),
                "pan_y": int(self._pan_y),
            })
            db.set_setting(self._cart_view_key(), payload)
        except Exception as e:
            print(f"[Cartridges] Failed to save view: {e}", flush=True)

    async def on_start(self):
        self._error_msg = None
        self._buttons_down = 0
        self._w4_gamepad1 = 0
        self._last_seq = 0
        self._load_cart_view()

        shm_path = f"/dev/shm/vipxpert_cart_{os.getpid()}_{id(self) & 0xFFFF:x}.shm"
        self._shm_path = shm_path
        cmd = self._build_command(shm_path)
        if not cmd:
            if hasattr(self.ui, 'show_toast'):
                self.ui.show_toast(self._error_msg or "Cartridge Error", duration=2.0)
            self.ui.request_render()
            return

        try:
            fd = os.open(shm_path, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600)
            os.ftruncate(fd, SHM_TOTAL_SIZE)
            mm = mmap.mmap(fd, SHM_TOTAL_SIZE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            mm[:4] = b"VCRT"
            struct.pack_into("<I", mm, 4, 1)
            self._shm_fd = fd
            self._shm_mm = mm
            self._sync_shm_control(quit_req=False)

            safe_cores = self._safe_worker_cores()

            def _preexec():
                try:
                    os.sched_setaffinity(0, set(safe_cores))
                except Exception:
                    pass

            proc_env = os.environ.copy()
            lr_dir = os.path.join(_get_repo_root(), "bin", "libretro")
            if os.path.isdir(lr_dir):
                prev_ld = proc_env.get("LD_LIBRARY_PATH", "")
                proc_env["LD_LIBRARY_PATH"] = f"{lr_dir}:{prev_ld}" if prev_ld else lr_dir
            # Comment (audio): vipxpert_os.service runs as root while PipeWire runs under user uid (e.g. /run/user/1000); expose XDG_RUNTIME_DIR so pw-cat connects to the live PipeWire daemon.
            if not proc_env.get("XDG_RUNTIME_DIR") or not os.path.exists(os.path.join(proc_env.get("XDG_RUNTIME_DIR", ""), "pipewire-0")):
                for _cand in ("/run/user/1000", "/run/user/1001"):
                    if os.path.exists(os.path.join(_cand, "pipewire-0")):
                        proc_env["XDG_RUNTIME_DIR"] = _cand
                        proc_env["PIPEWIRE_RUNTIME_DIR"] = _cand
                        break

            # Comment (invariant): Keep stdin=PIPE open as a parent-death EOF guard so cart_wasm4 exits immediately even if the host process crashes.
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=proc_env,
                preexec_fn=_preexec,
            )
            self._pump_task = asyncio.create_task(self._frame_pump_loop())
            if hasattr(self.ui, 'show_toast'):
                self.ui.show_toast(self.cart_spec.get("name", "Cartridge")[:18], duration=1.0)
        except Exception as e:
            self._error_msg = f"Launch failed: {e}"
            await self.on_stop()
        self.ui.request_render()

    async def _frame_pump_loop(self):
        try:
            while self._proc is not None and self._shm_mm is not None:
                if self._proc.poll() is not None:
                    break
                seq = struct.unpack_from("<I", self._shm_mm, 8)[0]
                if seq != self._last_seq:
                    self._last_seq = seq
                    self._last_fb = self._shm_mm[SHM_HEADER_SIZE:SHM_TOTAL_SIZE]
                    self.ui.request_render()
                await asyncio.sleep(1.0 / 60.0)
        except asyncio.CancelledError:
            pass

    async def on_stop(self):
        self._save_cart_view()
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
        self._pump_task = None

        if self._shm_mm is not None:
            try:
                self._sync_shm_control(quit_req=True)
            except Exception:
                pass

        if self._proc is not None:
            proc = self._proc
            self._proc = None
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.35)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=0.25)

        if self._shm_mm is not None:
            try:
                self._shm_mm.close()
            except Exception:
                pass
            self._shm_mm = None

        if self._shm_fd is not None:
            try:
                os.close(self._shm_fd)
            except Exception:
                pass
            self._shm_fd = None

        if self._shm_path and os.path.exists(self._shm_path):
            try:
                os.unlink(self._shm_path)
            except Exception:
                pass
        self._shm_path = None

    async def update(self):
        pass

    def _draw_state(self):
        if self._error_msg:
            from src.ui.components import scroll_text
            self.layout.hide_header = False
            self.layout.header_text = self.cart_spec.get("name", "Cartridge")[:18]
            self.layout.footer_text = "D: Back"
            err_line = scroll_text(self._error_msg, 20) if len(self._error_msg) > 20 else self._error_msg
            self.layout.body_lines = [("", err_line, "")]
            self.layout.scrollbar = None
            self.layout.render()
            if len(self._error_msg) > 20:
                self.display.has_active_marquee = True
            return
        self.display.draw_gray_frame(0, 0, OLED_W, OLED_H, self._last_fb, scaled_source=True)

    async def handle_key(self, action: str, key: str):
        if self._zoom_mode:
            if action not in ('DOWN', 'REPEAT', 'CLICK'):
                return
            if key in ('C', '#') and action == 'CLICK':
                self._zoom_mode = False
                if hasattr(self.ui, 'show_toast'):
                    self.ui.show_toast(f"View Locked ({self._zoom_pct}%)", duration=1.1)
                return
            if key == 'D' and action == 'CLICK':
                self._zoom_mode = False
                self._scale_mode = 3
                self._zoom_pct = 100
                self._pan_x = 0
                self._pan_y = 0
                self._sync_shm_control()
                if hasattr(self.ui, 'show_toast'):
                    self.ui.show_toast("View Reset (100%)", duration=1.1)
                return
            if key == '5' and action == 'CLICK':
                self._pan_x = 0
                self._pan_y = 0
                self._sync_shm_control()
                if hasattr(self.ui, 'show_toast'):
                    self.ui.show_toast("Snap: Center", duration=0.9)
                return
            if key == '0' and action == 'CLICK':
                # Comment (input): '*' is globally reserved for Lock Screen; use '0' inside Zoom & Pan mode to cycle fit and edge-snap presets.
                presets = (
                    ("Fit Width", 3, 0, 0),
                    ("Fit Height", 4, 0, 0),
                    ("1:1 Native", 200, 0, 0),
                    ("Edge Top", 3, 0, -1000),
                    ("Edge Bottom", 3, 0, 1000),
                    ("Edge Left", 4, -1000, 0),
                    ("Edge Right", 4, 1000, 0),
                )
                self._preset_idx = (self._preset_idx + 1) % len(presets)
                lbl, zp, px, py = presets[self._preset_idx]
                self._zoom_pct, self._pan_x, self._pan_y = zp, px, py
                self._sync_shm_control()
                if hasattr(self.ui, 'show_toast'):
                    self.ui.show_toast(f"Fit: {lbl}", duration=1.0)
                return
            if action in ('DOWN', 'REPEAT'):
                if key in ('A', 'B'):
                    base_z = 100 if self._zoom_pct == 3 else (53 if self._zoom_pct == 4 else self._zoom_pct)
                    delta = 10 if key == 'A' else -10
                    self._zoom_pct = max(50, min(250, base_z + delta))
                    self._sync_shm_control()
                    if hasattr(self.ui, 'show_toast'):
                        self.ui.show_toast(f"Zoom: {self._zoom_pct}%", duration=0.8)
                    return
                if key in ('2', '8', '4', '6'):
                    step = 125
                    snap_msg = None
                    if key == '4':
                        self._pan_x = max(-1000, self._pan_x - step)
                    elif key == '6':
                        self._pan_x = min(1000, self._pan_x + step)
                    elif key == '2':
                        self._pan_y = max(-1000, self._pan_y - step)
                    elif key == '8':
                        self._pan_y = min(1000, self._pan_y + step)
                    # Magnetic snap to center (0) and edges (-1000 / +1000)
                    if abs(self._pan_x) <= 75:
                        self._pan_x = 0
                        if key in ('4', '6'):
                            snap_msg = "Snap: Center X"
                    elif self._pan_x == -1000 and key == '4':
                        snap_msg = "Snap: Left Edge"
                    elif self._pan_x == 1000 and key == '6':
                        snap_msg = "Snap: Right Edge"
                    if abs(self._pan_y) <= 75:
                        self._pan_y = 0
                        if key in ('2', '8'):
                            snap_msg = "Snap: Center Y"
                    elif self._pan_y == -1000 and key == '2':
                        snap_msg = "Snap: Top Edge"
                    elif self._pan_y == 1000 and key == '8':
                        snap_msg = "Snap: Bottom Edge"
                    self._sync_shm_control()
                    if hasattr(self.ui, 'show_toast'):
                        self.ui.show_toast(snap_msg or f"Pan {self._pan_x//10},{self._pan_y//10}", duration=0.8)
                    return
            return

        if key == 'D' and action == 'CLICK':
            await self.ui.switch_app('cartridges')
            return

        if key == '#' and action == 'CLICK':
            self._zoom_mode = True
            self._scale_mode = 3
            self._buttons_down = 0
            self._w4_gamepad1 = 0
            self._sync_shm_control()
            if hasattr(self.ui, 'show_toast'):
                self.ui.show_toast("A/B:Zoom 2468:Pan C:OK", duration=1.6)
            return

        if key == '0' and action == 'CLICK':
            self._audio_muted = not self._audio_muted
            self._sync_shm_control()
            if hasattr(self.ui, 'show_toast'):
                self.ui.show_toast("Audio: MUTED" if self._audio_muted else "Audio: ON", duration=1.0)
            return

        bit = KEY_TO_BIT.get(key)
        w4_bit = KEY_TO_W4_BIT.get(key, 0)
        if action in ('DOWN', 'REPEAT'):
            if bit is not None:
                self._buttons_down |= (1 << bit)
            if w4_bit:
                self._w4_gamepad1 |= w4_bit
            self._sync_shm_control()
        elif action == 'CLICK':
            # Comment (timing): Pulse brief taps for 80ms so 60Hz SHM workers never miss a DOWN+UP pair occurring between frames.
            if bit is not None:
                self._buttons_down |= (1 << bit)
            if w4_bit:
                self._w4_gamepad1 |= w4_bit
            self._sync_shm_control()
            async def _release_later(b=bit, wb=w4_bit):
                await asyncio.sleep(0.08)
                if b is not None:
                    self._buttons_down &= ~(1 << b)
                if wb:
                    self._w4_gamepad1 &= ~wb
                self._sync_shm_control()
            asyncio.create_task(_release_later())
        elif action in ('UP', 'RELEASE'):
            if bit is not None:
                self._buttons_down &= ~(1 << bit)
            if w4_bit:
                self._w4_gamepad1 &= ~w4_bit
            self._sync_shm_control()


class CartridgeLauncherApp(ListMenuApp):
    """Sandboxed App & Cartridge Library listing /cartridges/ bundles and .wasm files."""
    def __init__(self, display, ui_manager, db):
        super().__init__(display, ui_manager, title="Cartridges", items=[], parent_app_id='app_selector')
        self.db = db
        self._carts: List[Dict] = []

    async def on_start(self):
        self._reload_list()
        await super().on_start()

    def _reload_list(self):
        self._carts = discover_cartridges()
        items: List[Dict] = []
        for idx, c in enumerate(self._carts):
            tag = c.get("tag") or ("[W4]" if c.get("runtime") == "wasm4" else "[SHM]")
            items.append({"name": f"{c['name']} {tag}", "id": f"cart:{idx}"})
        if not items:
            items.append({"name": "Drop .wasm in /cartridges", "id": "empty"})
        static_items = [{"name": "Rescan Cartridges", "id": "rescan"}]
        self.update_items(items, preserve_cursor=True, new_static_items=static_items)

    async def handle_key(self, action: str, key: str):
        if action == 'CLICK' and key == 'C':
            if not self.items:
                return
            item_id = self.items[self.cursor_index].get("id", "")
            if item_id == "rescan":
                self._reload_list()
                if hasattr(self.ui, 'show_toast'):
                    self.ui.show_toast(f"Found {len(self._carts)} cart(s)", duration=1.0)
                self.ui.request_render()
                return
            if item_id.startswith("cart:"):
                idx = int(item_id.split(":", 1)[1])
                if 0 <= idx < len(self._carts):
                    host = CartridgeHostApp(self.display, self.ui, self._carts[idx])
                    await self.ui.switch_app('cartridge_host', fallback_app=host)
                return
        await super().handle_key(action, key)
