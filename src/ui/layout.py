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
        self.footer_text: str = ""
        self.body_lines: List[Tuple[str, str, str]] = [] # [(Left Pinned, Main Text, Right Pinned)]
        self.scrollbar: Optional[Tuple[int, int, int]] = None
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
        
        # 1. Header (Top Anchor)
        if not self.hide_header and self.header_text:
            self.display.draw_text(0, 0, self.header_text)
            self.display.draw.line((0, self.font_h, self.screen_w, self.font_h), fill=255)
            start_y = self.font_h + 2
        
        # 2. Footer (Bottom Anchor)
        if self.footer_text:
            footer_y = self.screen_h - self.font_h
            self.display.draw.line((0, footer_y - 2, self.screen_w, footer_y - 2), fill=255)
            
            text_px_width = len(self.footer_text) * 6  # 6px avg width for standard font
            if text_px_width > self.screen_w:
                from src.ui.components import scroll_text # Local import to prevent circular dependency
                display_text = scroll_text(self.footer_text, 14) 
                self.display.draw_text(0, footer_y, display_text)
            else:
                # Justified Alignment for strings with intentional space gaps
                if "  " in self.footer_text:
                    parts = [p.strip() for p in self.footer_text.split("  ") if p.strip()]
                    if len(parts) >= 2:
                        left_text = parts[0]
                        right_text = parts[-1]
                        self.display.draw_text(0, footer_y, left_text)
                        
                        # Anchor right-side text to the screen edge
                        right_x = self.screen_w - (len(right_text) * 6)
                        self.display.draw_text(right_x, footer_y, right_text)
                    else:
                        self.display.draw_text(0, footer_y, self.footer_text)
                else:
                    self.display.draw_text(0, footer_y, self.footer_text)
                    
            end_y = footer_y - 2
            
        # 3. Flexible Body
        current_y = start_y
        for text_left, text_main, text_right in self.body_lines:
            if current_y + self.font_h > end_y:
                break  # Out of vertical bounds
                
            # Draw Left Pinned
            if text_left:
                self.display.draw_text(0, current_y, text_left)
                left_x = len(text_left) * 6
            else:
                left_x = 0
                
            # Draw Main Scrolling Meat
            if text_main:
                self.display.draw_text(left_x, current_y, text_main)
                
            # Draw Right Pinned
            if text_right:
                right_x = self.screen_w - 6 - (len(text_right) * 6)
                if self.scrollbar:
                    right_x -= 4
                self.display.draw_text(right_x, current_y, text_right)
                
            current_y += self.font_h
            
        # 4. Scroll Bar (Right Edge)
        if self.scrollbar:
            total, w_size, top = self.scrollbar
            if total > w_size:
                bar_max_h = end_y - start_y
                bar_h = max(4, int(bar_max_h * (w_size / total)))
                
                # Prevent division by zero
                scroll_pct = top / (total - w_size) if total > w_size else 0
                bar_y = start_y + int((bar_max_h - bar_h) * scroll_pct)
                
                # Draw the track and the thumb
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