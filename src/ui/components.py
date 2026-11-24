# /vipxpert_minimal_os/src/ui/components.py
from src.ui.base_app import BaseApp
import time
import asyncio
from typing import Optional

DEFAULT_MARQUEE_SPEED = 3.0
CURRENT_FRAME_TIME = 0.0

def scroll_text(text: str, max_chars: int, speed: Optional[float] = None, pause_beats: int = 4, max_scrolls: int = 30, start_time: float = None) -> str:
    """Emits a declarative marquee token evaluated by Display's V-Sync Slot Marquee Engine."""
    if len(text) <= max_chars:
        return text
    if speed is None:
        speed = DEFAULT_MARQUEE_SPEED

    gap_size = max(4, max_chars // 2)
    cycle_text = text + (" " * gap_size)
    pause_ms = int(pause_beats * 300) if pause_beats > 0 else 1200
    max_sc = max_scrolls if start_time is not None else 0
    return f"\x1f{speed:.1f}:{max_chars}:{pause_ms}:{max_sc}\x1f{cycle_text}\x1f"


def render_progress_alert(display, title: str, subtitle: str, progress_pct: Optional[float] = None):
    # Universal Centered Alert & Progress Card (Renders bordered modal with auto-scrolling subtitle and optional progress bar)
    box_w, box_h = 116, (40 if progress_pct is not None else 32)
    cx, cy = 64, 34
    x1, y1 = cx - box_w // 2, cy - box_h // 2
    x2, y2 = x1 + box_w, y1 + box_h
    display.draw.rectangle((x1, y1, x2, y2), fill=0, outline=1)
    display.draw.rectangle((x1 + 1, y1 + 1, x2 - 1, y1 + 12), fill=1)
    hdr = title[:18]
    if progress_pct is not None:
        pct_i = int(round(max(0.0, min(1.0, progress_pct)) * 100.0))
        hdr = f"{title[:12]} {pct_i}%"
    display.draw.text((x1 + 4, y1 + 1), hdr, font=display.font_small, fill=0)
    safe_sub = scroll_text(str(subtitle), max_chars=16)
    display.draw_text(x1 + 4, y1 + 15, safe_sub)
    if progress_pct is not None:
        pct = max(0.0, min(1.0, float(progress_pct)))
        bar_y = y1 + 30
        display.draw.rectangle((x1 + 5, bar_y, x2 - 5, bar_y + 5), outline=1, fill=0)
        fill_w = int((box_w - 12) * pct)
        if fill_w > 0:
            display.draw.rectangle((x1 + 6, bar_y + 1, x1 + 6 + fill_w, bar_y + 4), fill=1)
    display.has_active_marquee = True

class ListMenuApp(BaseApp):
    """A reusable scrolling list menu with Marquee text, Sort modes, and action callbacks."""
    def __init__(self, display, ui_manager, title: str, items: list, static_items: Optional[list] = None, sort_methods: Optional[dict] = None, default_sort_key: Optional[str] = None, parent_app_id: str = 'home', use_numbers: bool = False):
        super().__init__(display)
        self.ui = ui_manager
        self.title = title
        self.parent_app_id = parent_app_id
        self.use_numbers = use_numbers
        
        # Sorting Engine
        self.sort_methods = sort_methods or {}
        self.sort_keys = list(self.sort_methods.keys())
        self.current_sort_idx = -1 # -1 means "Original Order"
        if default_sort_key and default_sort_key in self.sort_keys:
            self.current_sort_idx = self.sort_keys.index(default_sort_key)
            
        # UI State
        self.cursor_index = 0
        self.top_index = 0
        self._anim_scroll_px = 0.0
        self._last_scroll_t = 0.0
        self.last_direction = 1  # 1 for Down, -1 for Up
        
        # Unified OS State
        self.toast_task: Optional[asyncio.Task] = None
        self.is_working: bool = False
        self.pending_multiplier: int = 1
        
        # Data Compartments
        self.static_items = static_items or []
        self.original_dynamic = []
        self.items = []
        
        # Load the initial data through the hydration engine
        self.update_items(items)

    def update_items(self, new_dynamic_items: list, preserve_cursor: bool = False, new_static_items: Optional[list] = None):
        """Hydrates the list, automatically reapplying the active sort mode and clamping the cursor."""
        self.original_dynamic = list(new_dynamic_items)
        if new_static_items is not None:
            self.static_items = list(new_static_items)
        
        if self.current_sort_idx == -1:
            dynamic_sorted = list(self.original_dynamic)
        else:
            sort_name = self.sort_keys[self.current_sort_idx]
            sort_config = self.sort_methods[sort_name]
            
            # Support advanced (lambda, reverse) tuples or simple lambdas
            if isinstance(sort_config, tuple):
                dynamic_sorted = sorted(self.original_dynamic, key=sort_config[0], reverse=sort_config[1])
            else:
                dynamic_sorted = sorted(self.original_dynamic, key=sort_config)
                
        # Stitch the dynamic sorted items and static footer items together
        self.items = dynamic_sorted + self.static_items
        self._dynamic_count = len(dynamic_sorted)
                
        if preserve_cursor and self.items:
            self.cursor_index = min(self.cursor_index, max(0, len(self.items) - 1))
        else:
            self.cursor_index = 0
            self.top_index = 0
        
    async def on_start(self):
        await self.update()

    async def on_stop(self):
        if self.toast_task and not self.toast_task.done():
            self.toast_task.cancel()
            self.toast_task = None
        self.layout.toast_text = None
        if hasattr(self.ui, 'clear_toast'):
            self.ui.clear_toast()

    def show_blocking_toast(self, message: str):
        """Displays a universal blocking toast and locks menu input."""
        self.layout.toast_text = None
        self.is_working = True
        if hasattr(self.ui, 'show_toast'):
            self.ui.show_toast(message, blocking=True)
        else:
            self.layout.toast_text = message
            self.ui.request_render()

    def show_info_toast(self, message: str, duration: float = 1.5, timeout: Optional[float] = None):
        """Delegates to the single universal compositor toast and unlocks keypad."""
        if timeout is not None:
            duration = timeout
        self.layout.toast_text = None
        self.is_working = False
        if hasattr(self.ui, 'show_toast'):
            self.ui.show_toast(message, duration=duration, blocking=False)
        else:
            self.layout.toast_text = message
            self.ui.request_render()

    def clear_toast(self):
        """Clears any active blocking or info toast from both local layout and the universal UIManager compositor."""
        self.layout.toast_text = None
        self.is_working = False
        if hasattr(self.ui, 'clear_toast'):
            self.ui.clear_toast()
        else:
            self.ui.request_render()

    async def handle_key(self, action: str, key: str):
        # Ensure tuning state exists cleanly
        if not hasattr(self, 'is_tuning'):
            self.is_tuning = False

        # Hardware Lock check
        if self.is_working:
            return

        # Ask the layout engine exactly how many items fit right now
        window_size = self.layout.get_body_capacity()

        # Dismiss non-blocking universal info toast immediately on next menu keypress
        if action == 'DOWN' and getattr(self.ui, '_toast_message', None) and not getattr(self.ui, '_toast_blocking', False):
            self.ui.clear_toast()

        # --- Continuous Repeat Block ---
        if action in ['DOWN', 'REPEAT']:
            # Intercept A/B for Active Tuning Mode
            if getattr(self, 'is_tuning', False) and key in ['A', 'B']:
                if self.items:
                    active_item = self.items[self.cursor_index]
                    if 'on_tune' in active_item:
                        jump = getattr(self, 'pending_multiplier', 1)
                        # A = Increase (+), B = Decrease (-)
                        delta = jump if key == 'A' else -jump
                        res = active_item['on_tune'](delta)
                        if __import__('inspect').isawaitable(res):
                            await res
                        self.pending_multiplier = 1  # One-shot reset
                        return

            if not self.items:
                return

            if key == 'A':  # Scroll Up
                self.last_direction = -1
                jump = getattr(self, 'pending_multiplier', 1)
                self.cursor_index -= jump
                if self.cursor_index < 0:
                    self.cursor_index = (self.cursor_index % len(self.items))
                    self.top_index = max(0, len(self.items) - window_size)
                self.pending_multiplier = 1  # One-shot reset
                
            elif key == 'B':  # Scroll Down
                self.last_direction = 1
                jump = getattr(self, 'pending_multiplier', 1)
                self.cursor_index += jump
                if self.cursor_index >= len(self.items):
                    self.cursor_index = (self.cursor_index % len(self.items))
                    self.top_index = 0
                self.pending_multiplier = 1  # One-shot reset

            # Keep cursor visible in the dynamically sized window
            if self.cursor_index < self.top_index:
                self.top_index = self.cursor_index
            elif self.cursor_index >= self.top_index + window_size:
                self.top_index = self.cursor_index - window_size + 1
                
            await self.update()
            return

        # NOW we kill anything that isn't a single click
        if action != 'CLICK':
            return

        # --- Single Click Actions ---
        if key in ['1', '2', '3', '4', '5', '6', '7', '8', '9']:  # Charge Multiplier
            self.pending_multiplier = int(key)
            self.show_info_toast(f"Scroll: {self.pending_multiplier}x", duration=1.0)
            return
            
        # Any other click (C, D, #) resets the multiplier as a safety abort
        self.pending_multiplier = 1
            
        if key == '#':  # Cycle Sort Mode
            if self.sort_keys:
                # Advance cycle: [-1 (Original)] -> [0] -> [1] -> back to [-1]
                self.current_sort_idx += 1
                if self.current_sort_idx >= len(self.sort_keys):
                    self.current_sort_idx = -1
                    
                # +1 because Original (-1) counts as a valid mode in the total cycle
                total_modes = len(self.sort_keys) + 1 
                current_mode_num = self.current_sort_idx + 2 # Shift -1 to 1 for display
                    
                if self.current_sort_idx == -1:
                    sort_name = "Original"
                else:
                    sort_name = self.sort_keys[self.current_sort_idx]
                    
                # Rehydrate using the new sort setting, resetting the cursor to the top
                self.update_items(self.original_dynamic, preserve_cursor=False)
                
                # Show Tripwire Toast! (e.g., "Sort 2/3: Name")
                self.show_info_toast(f"Sort {current_mode_num}/{total_modes}: {sort_name}", duration=1.0)
                
        elif key == 'C':  # Select / Enter
            if self.items:
                active_item = self.items[self.cursor_index]
                if getattr(self, 'is_tuning', False):
                    # Confirm and exit tuning state
                    self.is_tuning = False
                elif 'on_tune' in active_item:
                    # Enter tuning state
                    self.is_tuning = True
                    self.pending_multiplier = 1
                    self.show_info_toast("Tuning Mode", duration=1.0)
                else:
                    # Standard click action routing
                    target = active_item.get('action')
                    if callable(target):
                        result = target() # Execute dynamic lambda callback
                        # Only await it if the callback was an async function
                        if asyncio.iscoroutine(result):
                            await result
                    elif target:
                        await self.ui.switch_app(str(target)) # Route to app ID
            return
            
        elif key == 'D':  # Back / Cancel
            if getattr(self, 'is_tuning', False):
                self.is_tuning = False
                return
            await self.ui.switch_app(self.parent_app_id)
            return

        # Keep cursor visible in the dynamically sized window
        if self.cursor_index < self.top_index:
            self.top_index = self.cursor_index
        elif self.cursor_index >= self.top_index + window_size:
            self.top_index = self.cursor_index - window_size + 1
            
        await self.update()

    async def update(self):
        # Triggered by external events. Request compositor hardware flush.
        self.ui.request_render()

    def _draw_state(self):
        """Synchronous layout compilation and render function."""
        self.layout.header_text = self.title
        
        if not self.items:
            self.layout.full_scroll_lines = None
            self.layout.scroll_px = None
            self.layout.body_lines = [("", "(Empty List)", "")]
            self.layout.scrollbar = None
            self.layout.render()
            return

        window_size = self.layout.get_body_capacity()
        font_h = self.layout.font_h
        target_px = float(self.top_index * font_h)
        now_t = time.monotonic()
        dt = max(0.004, min(0.08, now_t - getattr(self, "_last_scroll_t", now_t - 0.016)))
        self._last_scroll_t = now_t
        db_obj = getattr(self.ui, "db", None)
        smooth_on = (db_obj.get_setting("ui_smooth_scroll", "1") != "0") if db_obj else True

        cur_px = float(getattr(self, "_anim_scroll_px", target_px))
        max_px = float(max(0, len(self.items) - window_size) * font_h)
        cur_px = max(0.0, min(max_px, cur_px))
        if not smooth_on or abs(target_px - cur_px) > font_h * (window_size + 2):
            cur_px = target_px
        elif abs(target_px - cur_px) > 0.35:
            alpha = 1.0 - (2.718281828 ** (-24.0 * dt))
            cur_px += (target_px - cur_px) * alpha
            if abs(target_px - cur_px) <= 0.35:
                cur_px = target_px
            else:
                self.display.has_active_marquee = True
        else:
            cur_px = target_px
        self._anim_scroll_px = cur_px

        first_vis = max(0, int(cur_px // font_h))
        last_vis = min(len(self.items), first_vis + window_size + 2)

        def _fmt_row(global_idx: int):
            item = self.items[global_idx]
            name = item.get('name', '')
            suffix = item.get('suffix', '')
            prefix = item.get('prefix', '')
            end_indicator = item.get('end_indicator', '')
            # Comment (decision): Universally extract trailing submenu arrows ('>' or '->') into right-pinned end_indicator so submenu buttons stay visually distinct from inline value toggles across all apps.
            if not end_indicator and suffix:
                s_strip = suffix.strip()
                if s_strip in ('>', '->'):
                    end_indicator = s_strip
                    suffix = ''
                elif s_strip.endswith(' ->'):
                    end_indicator = '->'
                    suffix = s_strip[:-3].rstrip()
                elif s_strip.endswith(' >'):
                    end_indicator = '>'
                    suffix = s_strip[:-2].rstrip()

            cursor_str = "> " if global_idx == self.cursor_index else "  "
            if self.use_numbers and global_idx < getattr(self, '_dynamic_count', len(self.items)):
                num_str = f"{global_idx + 1}. "
            else:
                num_str = ""
            left_pinned = f"{cursor_str}{num_str}{prefix}"

            if global_idx == self.cursor_index:
                if getattr(self, 'is_tuning', False):
                    safe_suffix = f"<{suffix}>" if suffix else "< >"
                    right_raw = f"{safe_suffix}{end_indicator}"
                    if len(left_pinned) + len(name) + 1 + len(right_raw) <= 19:
                        right_pinned = right_raw
                        display_name = name
                    else:
                        right_pinned = end_indicator
                        avail = max(4, 19 - len(left_pinned) - len(right_pinned))
                        display_name = scroll_text(f"{name} {safe_suffix}", max_chars=avail)
                else:
                    right_pinned = end_indicator
                    available_chars = max(3, 19 - len(left_pinned) - len(right_pinned))
                    fusion_text = f"{name} {suffix}" if suffix else name
                    display_name = scroll_text(fusion_text, max_chars=available_chars) if len(fusion_text) > available_chars else fusion_text
            else:
                if suffix and len(suffix) <= (8 - len(end_indicator)):
                    right_pinned = f"{suffix}{end_indicator}"
                    available_chars = max(3, 19 - len(left_pinned) - len(right_pinned))
                    display_name = scroll_text(name, max_chars=available_chars) if len(name) > available_chars else name
                else:
                    right_pinned = end_indicator
                    available_chars = max(3, 19 - len(left_pinned) - len(right_pinned))
                    fusion_inact = f"{name} {suffix}" if suffix else name
                    display_name = scroll_text(fusion_inact, max_chars=available_chars) if len(fusion_inact) > available_chars else fusion_inact
            return (left_pinned, display_name, right_pinned)

        if abs(cur_px - target_px) > 0.01:
            full_rows = [("", "", "")] * len(self.items)
            for g_i in range(first_vis, last_vis):
                full_rows[g_i] = _fmt_row(g_i)
            self.layout.full_scroll_lines = full_rows
            self.layout.scroll_px = cur_px
            self.layout.scrollbar = (len(self.items), window_size, cur_px / float(font_h))
        else:
            self.layout.full_scroll_lines = None
            self.layout.scroll_px = None
            self.layout.body_lines = [_fmt_row(g_i) for g_i in range(self.top_index, min(len(self.items), self.top_index + window_size))]
            self.layout.scrollbar = (len(self.items), window_size, float(self.top_index))
        self.layout.render()

class TextInputModal(BaseApp):
    """A Nokia-style T9 multi-tap text entry overlay with Viewport and Cursor animations."""
    def __init__(self, display, ui_manager, title: str, on_complete, on_cancel, default_text: str = ""):
        super().__init__(display)
        self.ui = ui_manager
        self.title = title
        self.on_complete = on_complete  
        self.on_cancel = on_cancel      
        
        self.text = list(default_text)
        self.cursor = len(self.text)
        self.is_pristine = bool(default_text)
        self.window_start = 0
        self.input_mode = 0  # 0: [abc], 1: [ABC], 2: [123]
        
        # T9 State
        self.active_key = None
        self.last_time = 0.0
        self.cycle_idx = 0
        self.blink_task: Optional[asyncio.Task] = None
        
        # Numbers stripped to force pure multi-tap. [123] mode handles numbers natively.
        self.KEY_MAP = {
            '2': ['a', 'b', 'c'],
            '3': ['d', 'e', 'f'],
            '4': ['g', 'h', 'i'],
            '5': ['j', 'k', 'l'],
            '6': ['m', 'n', 'o'],
            '7': ['p', 'q', 'r', 's'],
            '8': ['t', 'u', 'v'],
            '9': ['w', 'x', 'y', 'z'],
            '0': [' '],
            '1': ['-', '_', '.', '@'],
            '*': ['!', '#', '$', '%', '^', '&', '*', '?', '/']
        }

    async def _blink_loop(self):
        """Asynchronous animation task to blink the cursor every 0.5s."""
        try:
            while True:
                await asyncio.sleep(0.5)
                if self.ui.current_app is self:
                    self.ui.request_render()
        except asyncio.CancelledError:
            pass

    async def on_start(self):
        self.blink_task = asyncio.create_task(self._blink_loop())
        await self.update()
        
    async def on_stop(self):
        if self.blink_task:
            self.blink_task.cancel()
        if self.ui.previous_app_id:
            parent_app = self.ui.active_apps.get(self.ui.previous_app_id)
            if parent_app and hasattr(parent_app, 'layout'):
                parent_app.layout.modal = None

    def _clear_canvas(self):
        """Brutally wipes the floating box from the parent layout before executing callbacks."""
        if self.ui.previous_app_id:
            parent_app = self.ui.active_apps.get(self.ui.previous_app_id)
            if parent_app and hasattr(parent_app, 'layout'):
                parent_app.layout.modal = None
                parent_app.layout.render()

    async def handle_key(self, action: str, key: str):
        if action != 'CLICK':
            return
            
        current_time = time.time()
        
        if key == 'C':  # Confirm
            self._clear_canvas()
            self.is_dismissed = True
            await self.on_complete("".join(self.text))
            return
            
        elif key == 'D':  # Backspace / Cancel
            if self.is_pristine:
                self.text.clear()
                self.cursor = 0
                self.is_pristine = False
            elif len(self.text) > 0:
                if self.cursor > 0:
                    self.text.pop(self.cursor - 1)
                    self.cursor -= 1
                self.active_key = None
                self.last_time = 0.0
            else:
                self._clear_canvas()
                self.is_dismissed = True
                await self.on_cancel()
            await self.update()
            return
            
        elif key == '#':  # Cycle 3-State Mode
            self.is_pristine = False
            self.input_mode = (self.input_mode + 1) % 3
            self.active_key = None
            self.last_time = 0.0
            await self.update()
            return
            
        elif key == 'A':  # Cursor Left
            self.is_pristine = False
            self.cursor = max(0, self.cursor - 1)
            self.active_key = None
            self.last_time = 0.0
            await self.update()
            return
            
        elif key == 'B':  # Cursor Right
            self.is_pristine = False
            self.cursor = min(len(self.text), self.cursor + 1)
            self.active_key = None
            self.last_time = 0.0
            await self.update()
            return
            
        # --- Handle Typing Mechanics ---
        if self.is_pristine:
            self.text.clear()
            self.cursor = 0
            self.is_pristine = False
            
        if self.input_mode == 2:  # [123] Number Mode (Instant Execution)
            val = key if key.isdigit() else '*'
            self.text.insert(self.cursor, val)
            self.cursor += 1
            self.active_key = None
            self.last_time = 0.0
            
        else:  # [abc] or [ABC] Mode (Multi-Tap Execution)
            if key in self.KEY_MAP:
                chars = self.KEY_MAP[key]
                
                if key == self.active_key and (current_time - self.last_time) < 1.5:
                    # Cycle the existing character behind the cursor
                    self.cycle_idx = (self.cycle_idx + 1) % len(chars)
                    char = chars[self.cycle_idx]
                    if self.input_mode == 1 and char.isalpha():
                        char = char.upper()
                    self.text[self.cursor - 1] = char
                else:
                    # Insert fresh character
                    self.cycle_idx = 0
                    char = chars[0]
                    if self.input_mode == 1 and char.isalpha():
                        char = char.upper()
                    self.text.insert(self.cursor, char)
                    self.cursor += 1
                    
                self.active_key = key
                self.last_time = current_time
                
        await self.update()

    async def update(self):
        self._draw_state()

    def _draw_state(self):
        parent_app = None
        if self.ui.previous_app_id:
            parent_app = self.ui.active_apps.get(self.ui.previous_app_id)
        
        active_layout = parent_app.layout if (parent_app and hasattr(parent_app, 'layout')) else self.layout
        
        modes = ["[abc]", "[ABC]", "[123]"]
        mode_str = modes[self.input_mode]
        
        # --- Sliding Viewport Math ---
        max_w = 14 # Reserve space for cursor
        if self.cursor < self.window_start:
            self.window_start = self.cursor
        elif self.cursor > self.window_start + max_w:
            self.window_start = self.cursor - max_w
            
        visible_text = self.text[self.window_start : self.window_start + max_w + 1]
        
        # --- Dynamic Cursor Rendering ---
        current_time = time.time()
        is_cycling = (self.active_key is not None) and (current_time - self.last_time < 1.5)
        
        # Cursor blinks every 0.5s, but stays solid if you are actively cycling a letter
        show_cursor = is_cycling or (int(current_time * 2) % 2 == 0)
        # Using '#' as a solid font-safe alternative to the missing Unicode block glyph
        cursor_char = "#" if is_cycling else "_"
        
        # Inject the cursor into the exact horizontal string position
        local_cursor = self.cursor - self.window_start
        display_str = "".join(visible_text[:local_cursor])
        if show_cursor:
            display_str += cursor_char
        display_str += "".join(visible_text[local_cursor:])
        
        active_layout.modal = {
            'title': f"{self.title} {mode_str}",
            'body': [display_str], # Locked to a single scrolling line!
            'footer': "[C] OK   [D] Del"
        }
        active_layout.render()

class ConfirmModal(BaseApp):
    """A simple Yes/No confirmation overlay."""
    def __init__(self, display, ui_manager, title: str, prompt, on_yes, on_no, footer: str = "[C] Yes  [D] No"):
        super().__init__(display)
        self.ui = ui_manager
        self.title = title
        self.prompt = prompt
        self.on_yes = on_yes
        self.on_no = on_no
        self.footer = footer
        self.is_dismissed = False

    def _get_parent_app(self):
        """Dynamically resolve parent layout whether mounted as an app or global overlay."""
        if getattr(self.ui, 'active_overlay', None) is self:
            return self.ui.current_app
        if self.ui.previous_app_id:
            return self.ui.active_apps.get(self.ui.previous_app_id)
        return None

    async def on_start(self):
        await self.update()

    def _clear_canvas(self):
        """Wipes the floating box from the parent layout before jumping."""
        parent_app = self._get_parent_app()
        if parent_app and hasattr(parent_app, 'layout'):
            parent_app.layout.modal = None
            parent_app.layout.render()

    async def handle_key(self, action: str, key: str):
        if action != 'CLICK' or self.is_dismissed:
            return
        if key in ('C', 'D'):
            cb = self.on_yes if key == 'C' else self.on_no
            if not callable(cb):
                return
            self.is_dismissed = True
            self._clear_canvas()
            if getattr(self.ui, 'active_overlay', None) is self:
                self.ui.active_overlay = None
                if hasattr(self.ui, '_process_pending_overlays'):
                    self.ui._process_pending_overlays()
            res = cb()
            if asyncio.iscoroutine(res):
                await res

    def _draw_state(self):
        if self.is_dismissed:
            return
        parent_app = self._get_parent_app()
            
        active_layout = parent_app.layout if (parent_app and hasattr(parent_app, 'layout')) else self.layout
        body_lines = self.prompt if isinstance(self.prompt, list) else [str(self.prompt)]
        
        active_layout.modal = {
            'title': self.title,
            'body': body_lines,
            'footer': self.footer
        }
        active_layout.render()

    async def update(self):
        self._draw_state()

    async def on_stop(self):
        if self.ui.previous_app_id:
            parent_app = self.ui.active_apps.get(self.ui.previous_app_id)
            if parent_app and hasattr(parent_app, 'layout'):
                parent_app.layout.modal = None

class CrudListApp(ListMenuApp):
    """Reusable CRUD List combining a Mode Engine and a Multi-Field Input Wizard."""
    def __init__(self, display, ui_manager, title, items, crud_config, parent_app_id=None, **kwargs):
        self.crud_config = crud_config
        self.action_mode = 'NORMAL'
        self.raw_dynamic_items = items
        super().__init__(display, ui_manager, title, [], parent_app_id=parent_app_id, **kwargs)
        self._apply_mode('NORMAL')

    async def _safe_call(self, func, *args):
        """Helper to safely execute both sync and async lambda callbacks."""
        res = func(*args)
        if __import__('asyncio').iscoroutine(res):
            await res

    def _apply_mode(self, mode):
        self.action_mode = mode
        
        processed_items = []
        prefix = ""
        if mode == 'DELETE': prefix = "[D] "
        elif mode == 'EDIT': prefix = "[E] "
        else:
            for cm in self.crud_config.get('custom_modes', []):
                if mode == cm['mode']: prefix = cm.get('prefix', '')
        
        for item in self.raw_dynamic_items:
            new_item = item.copy()
            new_item['prefix'] = prefix + new_item.get('prefix', '')
            processed_items.append(new_item)
            
        static_items = []
        if mode != 'NORMAL':
            static_items.append({"name": "Cancel", "action": lambda: self._apply_mode('NORMAL')})
        else:
            if 'on_add' in self.crud_config:
                static_items.append({"name": "Add", "action": lambda: __import__('asyncio').create_task(self._run_wizard())})
            if 'on_edit' in self.crud_config:
                static_items.append({"name": "Edit", "action": lambda: self._apply_mode('EDIT')})
            if 'on_delete' in self.crud_config:
                static_items.append({"name": "Delete", "action": lambda: self._apply_mode('DELETE')})
            for cm in self.crud_config.get('custom_modes', []):
                static_items.append({"name": cm['label'], "action": lambda m=cm['mode']: self._apply_mode(m)})
                
        self.update_items(processed_items, preserve_cursor=True, new_static_items=static_items)
        self._draw_state()

    async def _run_wizard(self, edit_item=None):
        fields = self.crud_config.get('fields', [])
        results = {}
        return_app_id = self.ui.current_app_id
        asyncio = __import__('asyncio')
        
        for field in fields:
            answer = []
            event = asyncio.Event()
            
            async def on_complete(val, ev=event, ans=answer, f=field):
                ans.append(val)
                ev.set()
                
            async def on_cancel(ev=event):
                ev.set()
                
            default_val = str(edit_item.get('name', '')) if (edit_item and field == 'Name') else ""
            modal = TextInputModal(self.display, self.ui, f"{field}:", on_complete, on_cancel, default_text=default_val)
            modal.is_dismissed = False
            self.ui.active_overlay = modal
            self.ui.request_render()
            
            await event.wait()
            if not answer:
                self._apply_mode('NORMAL')
                self.ui.active_overlay = None
                self.ui.request_render()
                return
            results[field] = answer[0]
                
        self.show_blocking_toast("Saving...")
        try:
            if edit_item and 'on_edit' in self.crud_config:
                await self._safe_call(self.crud_config['on_edit'], edit_item['raw'], results)
            elif not edit_item and 'on_add' in self.crud_config:
                await self._safe_call(self.crud_config['on_add'], results)
        except Exception as e:
            print(f"[CRUD Error] {e}")
        finally:
            # Drop the toast sign and RELEASE the hardware lock now that the job is done
            self.layout.toast_text = None
            self.is_working = False
            
        self._apply_mode('NORMAL')
        if 'on_refresh' in self.crud_config:
            await self._safe_call(self.crud_config['on_refresh'])
        await self.ui.switch_app(return_app_id)

    async def handle_key(self, action: str, key: str):
        if action == 'CLICK' and key == 'C':
            global_idx = self.cursor_index
            is_dynamic = global_idx < getattr(self, '_dynamic_count', len(self.items))
            
            if is_dynamic and self.action_mode != 'NORMAL':
                item = self.items[global_idx]
                if item.get('raw') is None:
                    self._apply_mode('NORMAL')
                    return
                    
                if self.action_mode == 'DELETE':
                    async def on_yes():
                        try:
                            await self._safe_call(self.crud_config['on_delete'], item['raw'])
                        except Exception as e:
                            pass
                        self._apply_mode('NORMAL')
                        if 'on_refresh' in self.crud_config:
                            await self._safe_call(self.crud_config['on_refresh'])
                        self.ui.request_render()
                        
                    async def on_no():
                        self._apply_mode('NORMAL')
                        self.ui.request_render()
                        
                    modal = ConfirmModal(self.display, self.ui, "Delete?", f"Del {item.get('name', '')[:10]}?", on_yes, on_no)
                    self.ui.active_overlay = modal
                    self.ui.request_render()
                    return
                    
                elif self.action_mode == 'EDIT':
                    __import__('asyncio').create_task(self._run_wizard(edit_item=item))
                    return
                    
                else:
                    for cm in self.crud_config.get('custom_modes', []):
                        if self.action_mode == cm['mode']:
                            await self._safe_call(cm['on_select'], item['raw'])
                            self._apply_mode('NORMAL')
                            if 'on_refresh' in self.crud_config:
                                await self._safe_call(self.crud_config['on_refresh'])
                            return
                            
        await super().handle_key(action, key)
        
    def refresh_dynamic_items(self, new_items):
        self.raw_dynamic_items = new_items
        # Rehydrate and rebuild the master items list while preserving the active CRUD mode prefixes and cursor
        self._apply_mode(self.action_mode)
