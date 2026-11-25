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
            d[1] += 5.4 * dt
            dy = int(d[1])
            if dy >= 0:
                self.display.draw.line((int(d[0]), dy, int(d[0]), dy + 3), fill=1)
            if d[1] >= d[2]:
                # Wider ring impact (36..54px radius!) + staggered negative respawn (-36..-12) so fewer drops fall at once
                self.app.pond_ripples.append([float(d[0]), float(d[2]), 2.5, float(random.randint(36, 54))])
                d[0], d[1], d[2] = random.randint(14, 114), float(random.randint(-36, -12)), random.randint(20, 52)
        alive = []
        r_spd = getattr(self.app, "pond_ripple_spd", 1.95) * dt
        for rp in self.app.pond_ripples:
            cx, cy, r, max_r = rp
            life_u = r / max(1.0, max_r)
            rx, ry = int(r), max(1, int(r * 0.50))
            icx, icy = int(cx), int(cy)
            if life_u < 0.56:
                self.display.draw.ellipse((icx - rx, icy - ry, icx + rx, icy + ry), outline=1)
            else:
                fade_u = (life_u - 0.56) / 0.44
                arc_span = max(5, int(round(36.0 * (1.0 - fade_u * 0.84))))
                rot = int(cx * 3.0 + r * 3.0) % 360
                for a0 in range(rot, rot + 360, 45):
                    self.display.draw.arc((icx - rx, icy - ry, icx + rx, icy + ry), a0, a0 + arc_span, fill=1)
            if 7.0 < r < max_r * 0.84:
                rx2, ry2 = int(r * 0.62), max(1, int(r * 0.31))
                if life_u < 0.66:
                    self.display.draw.ellipse((icx - rx2, icy - ry2, icx + rx2, icy + ry2), outline=1)
                else:
                    for a0 in range(20, 380, 60):
                        self.display.draw.arc((icx - rx2, icy - ry2, icx + rx2, icy + ry2), a0, a0 + 16, fill=1)
            rp[2] += r_spd * (1.0 - 0.22 * life_u)
            if rp[2] < max_r:
                alive.append(rp)
        self.app.pond_ripples = alive[-8:]
