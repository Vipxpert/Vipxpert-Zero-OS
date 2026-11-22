# /vipxpert_minimal_os/src/scenes/clocks/clock_analog.py
import time
import math
from src.ui.scene_manager import ScenePlugin


class ClockAnalogScene(ScenePlugin):
    # Analog clock scene plugin (Renders 12-tick analog dial alongside digital readout)
    SCENE_ID = "clock_analog"
    TITLE = "Analog Clock"
    CATEGORY = "clock"
    ORDER = 20
    GUIDE_TEXT = "*:Unlock Screen   A/B:Slot|2/8:PiVol 7/9:BTV #:HUD"

    def draw(self):
        # Analog Clock Frame Render (Draws dial, hour/minute/second hands, and side telemetry)
        t_local = time.localtime()
        cx, cy, r = 32, 25, 22
        self.display.draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=1)
        for tick in range(12):
            ang = tick * (math.pi / 6.0)
            x0, y0 = int(cx + math.sin(ang) * (r - 3)), int(cy - math.cos(ang) * (r - 3))
            x1, y1 = int(cx + math.sin(ang) * r), int(cy - math.cos(ang) * r)
            self.display.draw.line((x0, y0, x1, y1), fill=1)
        sec_a = (t_local.tm_sec / 60.0) * 2.0 * math.pi
        min_a = ((t_local.tm_min + t_local.tm_sec / 60.0) / 60.0) * 2.0 * math.pi
        hr_a = (((t_local.tm_hour % 12) + t_local.tm_min / 60.0) / 12.0) * 2.0 * math.pi
        db = self.ui.db
        fmt_ov = db.get_setting("scn_clk_fmt_clock_analog", "global")
        fmt_12h = (fmt_ov if fmt_ov in ("12h", "24h") else db.get_setting("clock_format", "24h")) == "12h"
        show_sec_hand = db.get_setting("scn_ana_sec", "1") == "1"
        show_dig_sec = db.get_setting("scn_clk_sec_clock_analog", "1") == "1"
        self.display.draw.line((cx, cy, int(cx + math.sin(hr_a) * 11), int(cy - math.cos(hr_a) * 11)), fill=1, width=2)
        self.display.draw.line((cx, cy, int(cx + math.sin(min_a) * 16), int(cy - math.cos(min_a) * 16)), fill=1)
        if show_sec_hand:
            self.display.draw.line((cx, cy, int(cx + math.sin(sec_a) * 19), int(cy - math.cos(sec_a) * 19)), fill=1)
        if fmt_12h:
            t_fmt = "%I:%M:%S %p" if show_dig_sec else "%I:%M %p"
        else:
            t_fmt = "%H:%M:%S" if show_dig_sec else "%H:%M"
        self.display.draw_text(60, 2, time.strftime(t_fmt, t_local))
        self.display.draw_text(60, 16, time.strftime("%a %b %d", t_local))
        pwr = getattr(self.ui, "power_state", {})
        if not pwr.get("error", True) or pwr.get("voltage", 0.0) > 0.5:
            self.display.draw_text(62, 30, f"{pwr.get('icon', '[-]')} {pwr.get('percent', 0.0):.1f}%")
