# /vipxpert_minimal_os/src/apps/launcher_app.py
import time
from src.ui.base_app import BaseApp
from src.ui.components import ListMenuApp


class AppLauncherApp(BaseApp):
    """
    Reversible Modern <-> Minimal App Launcher:
    - 'modern' (default): Horizontal Icon Dock Carousel with crisp 24x24 vector pixel-art icons,
      shaded side-peek cards, smooth high-FPS sub-pixel carousel gliding, direct 1..8 hotkeys,
      and fixed top-right battery %.
    - 'minimal': Delegates 100% to the classic ListMenuApp (instant toggle via '#' or Settings -> Display & UI).
    """

    APP_ENTRIES = [
        {"name": "Cartridges", "sub": "NES / GB / WASM-4", "action": "cartridges", "icon": "gamepad"},
        {"name": "Offline Music", "sub": "Local Mixtapes", "action": "music_app", "icon": "music"},
        {"name": "Audio Hub", "sub": "DAC / Mic / DSP", "action": "audio_settings", "icon": "audio"},
        {"name": "Bluetooth", "sub": "LDAC / Devices", "action": "bluetooth_manager", "icon": "bt"},
        {"name": "Wi-Fi Hub", "sub": "Networks & Scan", "action": "wifi_manager", "icon": "wifi"},
        {"name": "IR Remote", "sub": "Learn & Blast", "action": "ir_remote", "icon": "ir"},
        {"name": "Recent Apps", "sub": "Resume / Kill", "action": "recent_tab", "icon": "tasks"},
        {"name": "Settings", "sub": "System & OLED", "action": "settings", "icon": "gear"},
    ]

    def __init__(self, display, ui_manager, db):
        super().__init__(display)
        self.ui = ui_manager
        self.db = db
        self.cursor_index = 0
        self._anim_pos = 0.0
        self._last_t = time.monotonic()
        self._minimal_menu = ListMenuApp(
            display,
            ui_manager,
            title="Apps",
            items=[{"name": e["name"], "suffix": "->", "action": e["action"]} for e in self.APP_ENTRIES],
            parent_app_id="home",
            use_numbers=True,
        )
        self._minimal_menu.layout.header_right_cb = self.ui.draw_top_right_battery

    def _is_minimal(self) -> bool:
        if hasattr(self.ui, "is_minimal_ui") and self.ui.is_minimal_ui():
            return True
        return self.db.get_setting("ui_launcher_style", "modern") == "minimal"

    def _sync_minimal_cursor(self):
        self._minimal_menu.cursor_index = self.cursor_index
        w = self._minimal_menu.layout.get_body_capacity()
        if self.cursor_index < self._minimal_menu.top_index:
            self._minimal_menu.top_index = self.cursor_index
        elif self.cursor_index >= self._minimal_menu.top_index + w:
            self._minimal_menu.top_index = self.cursor_index - w + 1
        self._minimal_menu._anim_scroll_px = float(self._minimal_menu.top_index * self._minimal_menu.layout.font_h)

    async def on_start(self):
        self._last_t = time.monotonic()
        self._anim_pos = float(self.cursor_index)
        self._sync_minimal_cursor()
        self.ui.request_render()

    async def on_resume(self, previous_app_id=None):
        self._last_t = time.monotonic()
        self._sync_minimal_cursor()
        self.ui.request_render()

    async def on_stop(self):
        pass

    async def update(self):
        self.ui.request_render()

    def _toggle_style(self):
        nxt = "modern" if self._is_minimal() else "minimal"
        self.db.set_setting("ui_launcher_style", nxt)
        if nxt == "minimal":
            self._sync_minimal_cursor()
            self.ui.show_toast("Launcher: Minimal", duration=1.2)
        else:
            self.cursor_index = self._minimal_menu.cursor_index
            self._anim_pos = float(self.cursor_index)
            self.ui.show_toast("Launcher: Modern", duration=1.2)
        self.ui.request_render()

    async def handle_key(self, action: str, key: str):
        if self._is_minimal():
            await self._minimal_menu.handle_key(action, key)
            self.cursor_index = self._minimal_menu.cursor_index
            return

        if action == "DOWN" and getattr(self.ui, "_toast_message", None) and not getattr(self.ui, "_toast_blocking", False):
            self.ui.clear_toast()

        n_apps = len(self.APP_ENTRIES)
        if action in ("DOWN", "REPEAT"):
            if key in ("A", "4"):
                self.cursor_index = (self.cursor_index - 1) % n_apps
                self.ui.request_render()
                return
            if key in ("B", "6"):
                self.cursor_index = (self.cursor_index + 1) % n_apps
                self.ui.request_render()
                return

        if action != "CLICK":
            return

        if key in ("1", "2", "3", "5", "7", "8"):
            idx = int(key) - 1
            if 0 <= idx < n_apps:
                self.cursor_index = idx
                await self.ui.switch_app(self.APP_ENTRIES[idx]["action"])
            return
        if key == "C":
            await self.ui.switch_app(self.APP_ENTRIES[self.cursor_index]["action"])
            return
        if key == "D":
            await self.ui.switch_app("home")
            return

    def _draw_icon(self, cx: int, cy: int, kind: str):
        d = self.display.draw
        if kind == "gamepad":
            d.rectangle((cx - 9, cy - 5, cx + 9, cy + 5), outline=1, fill=0)
            d.line((cx - 5, cy - 2, cx - 5, cy + 2), fill=1)
            d.line((cx - 7, cy, cx - 3, cy), fill=1)
            d.point((cx + 4, cy - 1), fill=1)
            d.point((cx + 6, cy + 1), fill=1)
        elif kind == "music":
            d.line((cx - 3, cy + 4, cx - 3, cy - 6), fill=1)
            d.line((cx + 5, cy + 3, cx + 5, cy - 7), fill=1)
            d.line((cx - 3, cy - 6, cx + 5, cy - 7), fill=1)
            d.line((cx - 3, cy - 4, cx + 5, cy - 5), fill=1)
            d.rectangle((cx - 6, cy + 2, cx - 3, cy + 5), fill=1)
            d.rectangle((cx + 2, cy + 1, cx + 5, cy + 4), fill=1)
        elif kind == "audio":
            for ox, kh in ((-6, -2), (0, 3), (6, -4)):
                d.line((cx + ox, cy - 7, cx + ox, cy + 7), fill=1)
                d.rectangle((cx + ox - 2, cy + kh - 1, cx + ox + 2, cy + kh + 1), fill=1)
        elif kind == "bt":
            d.line((cx, cy - 7, cx, cy + 7), fill=1)
            d.line((cx, cy - 7, cx + 5, cy - 3), fill=1)
            d.line((cx + 5, cy - 3, cx - 4, cy + 4), fill=1)
            d.line((cx, cy + 7, cx + 5, cy + 3), fill=1)
            d.line((cx + 5, cy + 3, cx - 4, cy - 4), fill=1)
        elif kind == "wifi":
            d.arc((cx - 9, cy - 7, cx + 9, cy + 11), 215, 325, fill=1)
            d.arc((cx - 6, cy - 4, cx + 6, cy + 8), 215, 325, fill=1)
            d.arc((cx - 3, cy - 1, cx + 3, cy + 5), 215, 325, fill=1)
            d.rectangle((cx - 1, cy + 4, cx + 1, cy + 6), fill=1)
        elif kind == "ir":
            d.rectangle((cx - 8, cy - 3, cx - 2, cy + 5), outline=1, fill=0)
            d.point((cx - 5, cy + 1), fill=1)
            d.arc((cx - 3, cy - 7, cx + 7, cy + 3), 290, 20, fill=1)
            d.arc((cx - 5, cy - 10, cx + 10, cy + 5), 290, 20, fill=1)
        elif kind == "tasks":
            d.rectangle((cx - 7, cy - 6, cx + 4, cy + 2), outline=1, fill=0)
            d.rectangle((cx - 4, cy - 3, cx + 7, cy + 6), outline=1, fill=0)
        else:
            d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), outline=1, fill=0)
            d.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), outline=1, fill=0)
            for dx, dy in ((0, -8), (0, 8), (-8, 0), (8, 0), (-6, -6), (6, 6), (-6, 6), (6, -6)):
                d.point((cx + dx, cy + dy), fill=1)

    def _draw_state(self):
        if self._is_minimal():
            self._minimal_menu._draw_state()
            return

        now_t = time.monotonic()
        dt = max(0.004, min(0.08, now_t - self._last_t))
        self._last_t = now_t

        n_apps = len(self.APP_ENTRIES)
        target = float(self.cursor_index)
        diff = target - self._anim_pos
        if diff > n_apps * 0.5:
            diff -= n_apps
        elif diff < -n_apps * 0.5:
            diff += n_apps

        smooth_on = self.db.get_setting("ui_smooth_scroll", "1") != "0"
        if smooth_on and abs(diff) > 0.01:
            self._anim_pos = (self._anim_pos + diff * (1.0 - (2.718281828 ** (-22.0 * dt)))) % n_apps
            self.display.has_active_marquee = True
        else:
            self._anim_pos = target

        d = self.display.draw
        self.display.draw_text(2, 0, f"Apps {self.cursor_index + 1}/{n_apps}")
        self.ui.draw_top_right_battery()
        d.line((0, 12, 127, 12), fill=1)

        spacing = 42.0
        for slot_off in (-2, -1, 1, 2, 0):
            idx = (int(round(self._anim_pos)) + slot_off) % n_apps
            rel = idx - self._anim_pos
            if rel > n_apps * 0.5:
                rel -= n_apps
            elif rel < -n_apps * 0.5:
                rel += n_apps
            if abs(rel) > 1.65:
                continue
            cx = int(round(64.0 + rel * spacing))
            cy = 27
            is_focus = (idx == self.cursor_index and abs(rel) < 0.45)
            if is_focus:
                d.rectangle((cx - 17, cy - 12, cx + 17, cy + 11), outline=1, fill=0)
                d.rectangle((cx - 18, cy - 11, cx + 18, cy + 10), outline=1, fill=0)
                self._draw_icon(cx, cy, self.APP_ENTRIES[idx]["icon"])
            else:
                x0, y0, x1, y1 = max(0, cx - 13), cy - 9, min(127, cx + 13), cy + 8
                if x1 - x0 >= 6:
                    self.display.draw_shaded_rect(x0, y0, x1, y1, shade=0.35, phase_id=idx)
                    d.rectangle((x0 + 1, y0 + 1, x1 - 1, y1 - 1), fill=0)
                    self._draw_icon(cx, cy, self.APP_ENTRIES[idx]["icon"])

        entry = self.APP_ENTRIES[self.cursor_index]
        title = entry["name"]
        font = self.display.font_small
        tw = int(round(font.getlength(title))) if hasattr(font, "getlength") else len(title) * 6
        tx = max(2, (128 - tw) // 2)
        self.display.draw_text(tx, 41, title)

        dots_w = n_apps * 5 - 2
        dx0 = (128 - dots_w) // 2
        for i in range(n_apps):
            px = dx0 + i * 5
            if i == self.cursor_index:
                d.rectangle((px, 57, px + 2, 59), fill=1)
            else:
                d.point((px + 1, 58), fill=1)
        self.display.draw_text(2, 53, "A/B")
        self.display.draw_text(100, 53, "C:Open")
