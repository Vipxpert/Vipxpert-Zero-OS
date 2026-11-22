# /vipxpert_minimal_os/src/scenes/savers/ambient_stars.py
import random
from src.ui.scene_manager import ScenePlugin


class AmbientStarsScene(ScenePlugin):
    # Interactive 3D starfield scene (Renders perspective star warp and meteor bursts)
    SCENE_ID = "ambient_stars"
    TITLE = "Star Shower"
    CATEGORY = "saver"
    ORDER = 30
    GUIDE_TEXT = "2/4/6/8:Steer  7/9:WarpSpd|5:Meteor Burst 0:Center"
    IS_INTERACTIVE = True
    IS_SAVER = True
    IS_PHYSICS = True

    def handle_key(self, action: str, key: str) -> bool:
        # Star Shower Key Handler (Steers vanishing point, adjusts warp speed, and spawns meteors)
        self.app._handle_ambient_physics_keys(self.SCENE_ID, action, key)
        return True

    def draw(self):
        # Star Shower Frame Render (Projects 3D star coordinates and animates wall-clock meteor streaks)
        self.app._ensure_ambient_physics()
        self.display.has_active_marquee = True
        dt = self.app.get_scene_dt(self.SCENE_ID, ref_fps=30.0)
        cx, cy, spd = self.app.star_cx, self.app.star_cy, self.app.star_speed * dt
        for st in self.app.stars_3d:
            st[2] -= spd
            if st[2] <= 1.5:
                st[0], st[1], st[2] = random.uniform(-60, 60), random.uniform(-32, 32), 64.0
            sx = int(cx + (st[0] / st[2]) * 42.0)
            sy = int(cy + (st[1] / st[2]) * 42.0)
            if 0 <= sx < 128 and 0 <= sy < 64:
                if st[2] < 20.0:
                    self.display.draw.rectangle((sx, sy, sx + 1, sy + 1), fill=1)
                else:
                    self.display.draw.point((sx, sy), fill=1)
        if random.random() < (0.04 * dt) and len(self.app.meteors) < 4:
            self.app.meteors.append([float(random.randint(30, 124)), float(random.randint(2, 20)), -4.2, 2.4, 10.0])
        m_alive = []
        for m in self.app.meteors:
            mx, my, vx, vy, life = m
            self.display.draw.line((int(mx), int(my), int(mx - vx * 2.2), int(my - vy * 2.2)), fill=1)
            m[0] += vx * dt
            m[1] += vy * dt
            m[4] -= dt
            if m[4] > 0.0 and 0 <= m[0] < 128 and 0 <= m[1] < 64:
                m_alive.append(m)
        self.app.meteors = m_alive
