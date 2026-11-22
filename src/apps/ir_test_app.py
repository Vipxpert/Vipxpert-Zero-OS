# /vipxpert_minimal_os/src/apps/ir_test_app.py
import asyncio
import time
from collections import deque
from src.ui.base_app import BaseApp

class IRTestApp(BaseApp):
    """Live Mode2 IR Square-Wave Logic Analyzer & NEC/Samsung/SIRC/RC5/RC6 Protocol Decoder."""
    def __init__(self, display, ui_manager, ir_hardware):
        super().__init__(display)
        self.ui = ui_manager
        self.ir = ir_hardware
        self.stream_task = None
        self.is_flashing = False
        self.live_tokens = []
        self.last_pulses = []
        self.last_raw_str = ""
        self.decoded_str = "WAITING PH16..."
        self.zoom_us = 45000  # Visible microsecond window across 124px (15ms, 45ms, 90ms)
        self.pan_us = 0
        self.layout.hide_header = True
        self.layout.footer_text = ""

    async def on_start(self):
        self.live_tokens.clear()
        self.is_flashing = False
        if not self.last_pulses:
            self.decoded_str = "WAITING PH16..."
        self.stream_task = asyncio.create_task(self._read_stream())
        self.ui.request_render()

    async def on_stop(self):
        if self.stream_task:
            self.stream_task.cancel()

    async def update(self):
        pass

    async def handle_key(self, action: str, key: str):
        if action not in ("CLICK", "REPEAT"):
            return
        if key == "D" and action == "CLICK":
            await self.ui.switch_app("ir_remotes")
            return
        if key == "4":
            self.pan_us = max(0, self.pan_us - (self.zoom_us // 4))
        elif key == "6":
            self.pan_us = min(200000, self.pan_us + (self.zoom_us // 4))
        elif key == "7" and action == "CLICK":
            opts = [15000, 30000, 45000, 90000]
            idx = opts.index(self.zoom_us) if self.zoom_us in opts else 2
            self.zoom_us = opts[max(0, idx - 1)]
        elif key == "9" and action == "CLICK":
            opts = [15000, 30000, 45000, 90000]
            idx = opts.index(self.zoom_us) if self.zoom_us in opts else 2
            self.zoom_us = opts[min(len(opts) - 1, idx + 1)]
        elif key == "0" and action == "CLICK":
            self.last_pulses.clear()
            self.last_raw_str = ""
            self.pan_us = 0
            self.decoded_str = "CLEARED"
        elif key == "5" and action == "CLICK" and self.last_raw_str:
            self.decoded_str = "TX REPLAYING..."
            self.ui.request_render()
            try:
                await self.ir.send_raw(self.last_raw_str)
                self.decoded_str = "TX BLAST OK!"
            except Exception:
                self.decoded_str = "TX ERROR"
        self.ui.request_render()

    async def _read_stream(self):
        async for data in self.ir.stream_raw_continuous():
            if data == "SILENCE":
                if self.live_tokens:
                    raw_burst = " ".join(self.live_tokens)
                    pulses = self.ir.parse_pulses(raw_burst)
                    self.live_tokens.clear()
                    self.is_flashing = False
                    if len(pulses) >= 6:
                        self.last_pulses = pulses
                        self.last_raw_str = " ".join(f"+{p}" if p > 0 else f"{p}" for p in pulses)
                        self.pan_us = 0
                        self.decoded_str = self.ir.decode_protocol(pulses)
                    else:
                        self.decoded_str = f"NOISE ({len(pulses)}e)"
                    self.ui.request_render()
            elif data.startswith("ERROR"):
                self.decoded_str = "IR RX OFFLINE"
                self.ui.request_render()
            else:
                norm = data.replace("pulse ", "+").replace("space ", "-")
                self.live_tokens.append(norm)
                self.is_flashing = True
                self.ui.request_render()

    def _draw_state(self):
        super()._draw_state()
        # 1. Top Inverted Protocol Banner (y=0..11)
        self.display.draw.rectangle((0, 0, 127, 11), fill=1)
        rx_tag = " [RX!]" if self.is_flashing else ""
        self.display.draw.text((2, 0), f"{self.decoded_str}{rx_tag}", font=self.display.font_small, fill=0)

        # 2. Mode2 Square-Wave Logic Analyzer Canvas (x=2..125, y=14..48)
        x1, y_hi, x2, y_lo = 2, 16, 125, 44
        self.display.draw.rectangle((x1 - 1, y_hi - 3, x2 + 1, y_lo + 4), outline=1, fill=0)

        pulses = self.last_pulses
        if pulses:
            w_px = x2 - x1
            t_cur = 0
            t_start = self.pan_us
            t_end = t_start + max(1000, self.zoom_us)
            prev_y = y_lo
            for p in pulses:
                dur = min(abs(p), 15000)
                seg_s = t_cur
                seg_e = t_cur + dur
                t_cur = seg_e
                y_lvl = y_hi if p > 0 else y_lo
                if seg_e <= t_start:
                    prev_y = y_lvl
                    continue
                if seg_s >= t_end:
                    break
                px0 = x1 + int(max(0.0, min(1.0, (seg_s - t_start) / float(t_end - t_start))) * w_px)
                px1 = x1 + int(max(0.0, min(1.0, (seg_e - t_start) / float(t_end - t_start))) * w_px)
                if y_lvl != prev_y:
                    self.display.draw.line((px0, prev_y, px0, y_lvl), fill=1)
                self.display.draw.line((px0, y_lvl, max(px0, px1), y_lvl), fill=1)
                prev_y = y_lvl
        else:
            self.display.draw_text(16, 24, "Point IR Remote")

        # 3. Bottom Controls & Timing Readout (y=51)
        zm_ms = self.zoom_us // 1000
        if pulses and len(pulses) >= 2:
            h0, h1 = abs(pulses[0]), abs(pulses[1])
            self.display.draw_text(2, 51, f"+{h0}/-{h1}us [{zm_ms}ms] 5:TX")
        else:
            self.display.draw_text(2, 51, f"[{zm_ms}ms] 46:Pan 79:Zm 5:TX")