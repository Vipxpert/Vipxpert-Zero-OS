# /vipxpert_minimal_os/src/scenes/savers/ambient_pond.py
import random
from src.ui.scene_manager import ScenePlugin


class AmbientPondScene(ScenePlugin):
    # Interactive rain pond scene (Renders falling droplets, expanding concentric ripples, and lightning storm)
    SCENE_ID = "ambient_pond"
    TITLE = "Rain Pond"
    CATEGORY = "saver"
    ORDER = 20
    GUIDE_TEXT = "1-9:Drop Pebble at Grid|0:Thunderstorm  A/B:Slot"
    IS_INTERACTIVE = True
    IS_SAVER = True
    IS_PHYSICS = True

    def handle_key(self, action: str, key: str) -> bool:
        # Rain Pond Key Handler (Spawns ripples at 1-9 grid coordinates or triggers thunderstorm on 0)
        self.app._handle_ambient_physics_keys(self.SCENE_ID, action, key)
        return True

    def draw(self):
        # Rain Pond Frame Render (Advances wall-clock droplet physics and draws concentric ripple ellipses)
        self.app._ensure_ambient_physics()
        self.display.has_active_marquee = True
        dt = self.app.get_scene_dt(self.SCENE_ID, ref_fps=30.0)
        if self.app.pond_storm > 0:
            self.app.pond_storm = max(0.0, self.app.pond_storm - dt)
            if int(round(self.app.pond_storm)) % 2 == 1:
                self.display.draw.rectangle((0, 0, 127, 63), fill=1)
                return
        # Comment (invariant): Never call draw_shaded_rect(0, 46, 127, 63, ...) here; in UI PWM mode (not is_fs_pwm), _flush_precompiled_pages skips Pages 6 & 7 (y=48..63) on ph0/ph1 and flushes lo_copy (solid 0) on ph2, permanently blacking out the bottom third of the pond!
        for d in self.app.pond_drops:
            d[1] += 4.0 * dt
            dy = int(d[1])
            self.display.draw.line((int(d[0]), dy, int(d[0]), dy + 3), fill=1)
            if d[1] >= d[2]:
                self.app.pond_ripples.append([float(d[0]), float(d[2]), 1.0, float(random.randint(18, 30))])
                d[0], d[1], d[2] = random.randint(10, 118), float(random.randint(-12, 0)), random.randint(18, 56)
        alive = []
        r_spd = getattr(self.app, "pond_ripple_spd", 1.15) * dt
        for rp in self.app.pond_ripples:
            cx, cy, r, max_r = rp
            rx, ry = int(r), max(1, int(r * 0.48))
            self.display.draw.ellipse((int(cx) - rx, int(cy) - ry, int(cx) + rx, int(cy) + ry), outline=1)
            if 6.0 < r < max_r * 0.75:
                rx2, ry2 = int(r * 0.6), max(1, int(r * 0.28))
                self.display.draw.ellipse((int(cx) - rx2, int(cy) - ry2, int(cx) + rx2, int(cy) + ry2), outline=1)
            rp[2] += r_spd
            if rp[2] < max_r:
                alive.append(rp)
        self.app.pond_ripples = alive[-12:]
