# /vipxpert_minimal_os/src/scenes/clocks/clock_digital.py
import time
from src.ui.scene_manager import ScenePlugin


class ClockDigitalScene(ScenePlugin):
    # Digital clock scene plugin (Renders 12/24h digital clock with battery and DAC telemetry)
    SCENE_ID = "clock_digital"
    TITLE = "Digital Clock"
    CATEGORY = "clock"
    ORDER = 10
    GUIDE_TEXT = "*:Unlock Screen   A/B:Slot|2/8:PiVol 7/9:BTV #:HUD"

    def draw(self):
        # Digital Clock Frame Render (Draws 12/24h time with optional seconds/date, battery, and active DAC badge)
        db = self.ui.db
        fmt_ov = db.get_setting("scn_clk_fmt_clock_digital", "global")
        fmt_12h = (fmt_ov if fmt_ov in ("12h", "24h") else db.get_setting("clock_format", "24h")) == "12h"
        show_sec = db.get_setting("scn_clk_sec_clock_digital", "1") == "1"
        show_date = db.get_setting("scn_dig_date", "1") == "1"
        t_local = time.localtime()
        if show_sec:
            current_time = time.strftime("%I:%M:%S" if fmt_12h else "%H:%M:%S", t_local)
            time_x = 18
        else:
            current_time = time.strftime("%I:%M" if fmt_12h else "%H:%M", t_local)
            time_x = 34
        ampm_str = time.strftime("%p", t_local) if fmt_12h else ""
        pwr = getattr(self.ui, "power_state", {})
        if not pwr.get("error", True) or pwr.get("voltage", 0.0) > 0.5:
            self.display.draw_text(2, 0, f"{pwr.get('icon', '[-]')} {pwr.get('percent', 0.0):.1f}%")
        dac_badge = "I2S" if self.ui._is_i2s_effectively_active() else "USB"
        if getattr(self.ui, "transparency_enabled", False):
            dac_badge = f"AMB+{dac_badge}"
        self.display.draw_text(max(68, 126 - len(dac_badge) * 6), 0, dac_badge)
        self.display.draw_text(time_x, 14, current_time, font_type="large")
        if show_date:
            self.display.draw_text(2, 38, time.strftime("%b %d, %Y", t_local))
        if ampm_str:
            self.display.draw_text(110, 38, ampm_str)
