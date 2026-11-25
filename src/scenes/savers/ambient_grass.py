# /vipxpert_minimal_os/src/scenes/savers/ambient_grass.py
import math
from src.ui.scene_manager import ScenePlugin


class AmbientGrassScene(ScenePlugin):
    # Interactive windy grass scene (Renders swaying 2-segment grass blades and drifting fireflies)
    SCENE_ID = "ambient_grass"
    TITLE = "Windy Grass"
    CATEGORY = "saver"
    ORDER = 50
    GUIDE_TEXT = "4/6:Wind Dir   2/8:Blade Ht|5:Spawn Fireflies 0:Calm"
    IS_INTERACTIVE = True
    IS_SAVER = True
    IS_PHYSICS = True

    def handle_key(self, action: str, key: str) -> bool:
        # Windy Grass Key Handler (Adjusts wind direction, blade height, and firefly count)
        self.app._handle_ambient_physics_keys(self.SCENE_ID, action, key)
        return True

    def draw(self):
        # Windy Grass Frame Render (Draws wall-clock harmonic swaying grass blades and blinking fireflies)
        self.app._ensure_ambient_physics()
        self.display.has_active_marquee = True
        ph, dt = self.app.advance_scene_phase(self.SCENE_ID, ref_fps=30.0)
        self.app.grass_wind += (self.app.grass_target_wind - self.app.grass_wind) * min(1.0, 0.15 * dt)
        self.display.draw.line((0, 62, 127, 62), fill=1)
        self.display.draw.line((0, 63, 127, 63), fill=1)
        h_base = self.app.grass_height
        for i in range(32):
            gx = 2 + i * 4
            b_h = max(7, h_base + ((i * 7) % 7) - 3)
            gust = math.sin(ph * 0.21 + i * 0.38) * 3.8 + math.sin(ph * 0.37 + i * 0.8) * 1.4 + self.app.grass_wind * 4.8
            mx, my = int(gx + gust * 0.45), 62 - (b_h // 2)
            tx, ty = int(gx + gust), max(4, 62 - b_h + int(abs(gust) * 0.22))
            self.display.draw.line((gx, 62, mx, my), fill=1)
            self.display.draw.line((mx, my, tx, ty), fill=1)
        ph_i = int(ph)
        for ff in self.app.fireflies:
            ff[0] = (ff[0] + (self.app.grass_wind * 0.6 + math.cos(ph * 0.08 + ff[2]) * 0.5) * dt) % 128.0
            ff[1] = 6.0 + ((ff[1] - 6.0 + math.sin(ph * 0.10 + ff[2]) * 0.4 * dt) % 36.0)
            fx, fy = int(ff[0]), int(ff[1])
            if (ph_i + int(ff[2] * 10)) % 6 != 0:
                self.display.draw.rectangle((fx, fy, fx + 1, fy + 1), fill=1)
