# /vipxpert_minimal_os/src/ui/layout.py
import time
import textwrap
from typing import Optional, Tuple, Dict, Any, List

class LayoutEngine:
    """Declarative UI engine handling Flex-Grid math, Headers, Footers, and Modals."""
    def __init__(self, display):
        self.display = display
        self.hide_header: bool = False
        self.header_text: str = ""
        self.header_right_cb = None
        self.footer_text: str = ""
        self.body_lines: List[Tuple[str, str, str]] = [] # [(Left Pinned, Main Text, Right Pinned)]
        self.full_scroll_lines: Optional[List[Tuple[str, str, str]]] = None
        self.scroll_px: Optional[float] = None
        self.scrollbar: Optional[Tuple[int, int, float]] = None
        self.modal: Optional[Dict[str, Any]] = None
        self.toast_text: Optional[str] = None
        
        # Math constants for SH1106 OLED
        self.screen_w = 128
        self.screen_h = 64
        self.font_h = 12
        
    def get_body_capacity(self) -> int:
        """Calculates exactly how many lines fit in the current body bounds."""
        start_y = (self.font_h + 2) if (not self.hide_header and self.header_text) else 0
        end_y = (self.screen_h - self.font_h - 2) if self.footer_text else self.screen_h
        return max(1, (end_y - start_y) // self.font_h)

    def render(self):
        """Paints the declared state to the PIL RAM buffer. Hardware flush is managed by Compositor."""
        # OS Kernel + UIManager Compositor now handle self.display.clear()
        
        start_y = 0
        end_y = self.screen_h
        
        has_header = (not self.hide_header and bool(self.header_text))
        if has_header:
            start_y = self.font_h + 2
        footer_y = self.screen_h - self.font_h
        if self.footer_text:
            end_y = footer_y - 2

        font = getattr(self.display, "font_small", None)

        def _px_w(txt: str) -> int:
            if not txt:
                return 0
            if font is not None and hasattr(font, "getlength"):
                return int(round(font.getlength(txt)))
            return len(txt) * 6

        def _draw_row_2col(y_pos: int, text_left: str, text_main: str, text_right: str):
            right_edge = (self.screen_w - 5) if self.scrollbar else (self.screen_w - 2)
            rw = _px_w(text_right) if text_right else 0
            right_x = max(56, right_edge - rw) if text_right else right_edge
            # Left + Main column hard clip limit: guarantees >=4px clean gap before Column 2 (`text_right`) so columns NEVER overlap!
            col1_limit = (right_x - 4) if text_right else right_edge
            if text_left:
                self.display.draw_text(0, y_pos, text_left)
                left_x = _px_w(text_left)
            else:
                left_x = 0
            if text_main and left_x < col1_limit - 4:
                self.display.draw_text(left_x, y_pos, text_main)
                if _px_w(text_main) + left_x > col1_limit:
                    self.display.draw.rectangle((col1_limit, max(0, y_pos), self.screen_w - 1, min(self.screen_h - 1, y_pos + self.font_h - 1)), fill=0)
            if text_right:
                self.display.draw.rectangle((max(0, right_x - 2), max(0, y_pos), right_edge, min(self.screen_h - 1, y_pos + self.font_h - 1)), fill=0)
                self.display.draw_text(right_x, y_pos, text_right)

        # 1. Flexible Body (Supports smooth high-FPS sub-row pixel scrolling + pixel-exact 2-column separation!)
        if self.full_scroll_lines is not None and self.scroll_px is not None:
            px_off = float(self.scroll_px)
            first_idx = max(0, int(px_off // self.font_h))
            sub_shift = int(round(px_off - first_idx * self.font_h))
            current_y = start_y - sub_shift
            for row_idx in range(first_idx, len(self.full_scroll_lines)):
                if current_y >= end_y:
                    break
                if current_y + self.font_h > start_y:
                    tl, tm, tr = self.full_scroll_lines[row_idx]
                    _draw_row_2col(current_y, tl, tm, tr)
                current_y += self.font_h
            if start_y > 0:
                self.display.draw.rectangle((0, 0, self.screen_w - 1, start_y - 1), fill=0)
            if end_y < self.screen_h:
                self.display.draw.rectangle((0, end_y, self.screen_w - 1, self.screen_h - 1), fill=0)
        else:
            current_y = start_y
            for tl, tm, tr in self.body_lines:
                if current_y + self.font_h > end_y:
                    break
                _draw_row_2col(current_y, tl, tm, tr)
                current_y += self.font_h

        # 2. Header (Top Anchor + Optional Fixed Top-Right Battery Badge)
        if has_header:
            self.display.draw_text(0, 0, self.header_text)
            if callable(getattr(self, "header_right_cb", None)):
                self.header_right_cb()
            self.display.draw.line((0, self.font_h, self.screen_w, self.font_h), fill=255)

        # 3. Footer (Bottom Anchor)
        if self.footer_text:
            self.display.draw.line((0, footer_y - 2, self.screen_w, footer_y - 2), fill=255)
            text_px_width = len(self.footer_text) * 6
            if text_px_width > self.screen_w:
                from src.ui.components import scroll_text
                display_text = scroll_text(self.footer_text, 14)
                self.display.draw_text(0, footer_y, display_text)
            else:
                if "  " in self.footer_text:
                    parts = [p.strip() for p in self.footer_text.split("  ") if p.strip()]
                    if len(parts) >= 2:
                        left_text = parts[0]
                        right_text = parts[-1]
                        self.display.draw_text(0, footer_y, left_text)
                        right_x = self.screen_w - (len(right_text) * 6)
                        self.display.draw_text(right_x, footer_y, right_text)
                    else:
                        self.display.draw_text(0, footer_y, self.footer_text)
                else:
                    self.display.draw_text(0, footer_y, self.footer_text)

        # 4. Scroll Bar (Right Edge — Smooth Fractional Thumb!)
        if self.scrollbar:
            total, w_size, top = self.scrollbar
            if total > w_size:
                bar_max_h = end_y - start_y
                bar_h = max(4, int(bar_max_h * (w_size / total)))
                scroll_pct = max(0.0, min(1.0, float(top) / float(total - w_size)))
                bar_y = start_y + int(round((bar_max_h - bar_h) * scroll_pct))
                self.display.draw.line((126, start_y, 126, end_y), fill=255)
                self.display.draw.rectangle((125, bar_y, 127, bar_y + bar_h), fill=255)
                
        # 5. Floating Modal Overlay (Z-Index Top)
        if self.modal:
            # Centered floating box (Option B)
            box_x1, box_y1 = 8, 8
            box_x2, box_y2 = 120, 56
            
            # Black out background and draw white border
            self.display.draw.rectangle((box_x1, box_y1, box_x2, box_y2), fill="black", outline="white")
            
            if 'title' in self.modal:
                self.display.draw_text(box_x1 + 4, box_y1 + 2, self.modal['title'])
                self.display.draw.line((box_x1, box_y1 + 14, box_x2, box_y1 + 14), fill="white")
                
            if 'body' in self.modal:
                for i, m_line in enumerate(self.modal['body']):
                    self.display.draw_text(box_x1 + 4, box_y1 + 16 + (i * 12), m_line)
                    
            if 'footer' in self.modal:
                self.display.draw_text(box_x1 + 4, box_y2 - 12, self.modal['footer'])
                
        # 6. Toast Notification (Bottom Center Float)
        if self.toast_text:
            toast_w = (len(self.toast_text) * 6) + 8
            toast_x = (self.screen_w - toast_w) // 2
            toast_y = end_y - self.font_h - 4
            
            self.display.draw.rectangle((toast_x, toast_y, toast_x + toast_w, toast_y + self.font_h + 2), fill="black", outline="white")
            self.display.draw_text(toast_x + 4, toast_y + 2, self.toast_text)
                
        # OS Kernel + UIManager Compositor now handle self.display.show()
