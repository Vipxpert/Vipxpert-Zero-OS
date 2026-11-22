# /vipxpert_minimal_os/src/scenes/savers/ambient_bounce.py
from src.ui.scene_manager import ScenePlugin


class AmbientBounceScene(ScenePlugin):
    # Interactive corner bounce scene (Renders bouncing badge/diamond/orb with wall and corner hit counters)
    SCENE_ID = "ambient_bounce"
    TITLE = "Corner Bounce"
    CATEGORY = "saver"
    ORDER = 40
    GUIDE_TEXT = "2/4/6/8:Steer  5:Speed Kick|1:Badge/Dia/Orb 0:Corner"
    IS_INTERACTIVE = True
    IS_SAVER = True
    IS_PHYSICS = True

    def handle_key(self, action: str, key: str) -> bool:
        # Corner Bounce Key Handler (Steers velocity, kicks speed, cycles shape, or snaps to corner)
        self.app._handle_ambient_physics_keys(self.SCENE_ID, action, key)
        return True

    def draw(self):
        # Corner Bounce Frame Render (Updates wall-clock kinematics, detects corner hits, and draws shape)
        self.app._ensure_ambient_physics()
        self.display.has_active_marquee = True
        dt = self.app.get_scene_dt(self.SCENE_ID, ref_fps=30.0)
        self.app.bnc_x += self.app.bnc_vx * dt
        self.app.bnc_y += self.app.bnc_vy * dt
        max_x, max_y = 127 - self.app.bnc_w, 63 - self.app.bnc_h
        hit_x, hit_y = False, False
        if self.app.bnc_x <= 0.0:
            self.app.bnc_x, self.app.bnc_vx, hit_x = 0.0, abs(self.app.bnc_vx), True
        elif self.app.bnc_x >= max_x:
            self.app.bnc_x, self.app.bnc_vx, hit_x = float(max_x), -abs(self.app.bnc_vx), True
        if self.app.bnc_y <= 0.0:
            self.app.bnc_y, self.app.bnc_vy, hit_y = 0.0, abs(self.app.bnc_vy), True
        elif self.app.bnc_y >= max_y:
            self.app.bnc_y, self.app.bnc_vy, hit_y = float(max_y), -abs(self.app.bnc_vy), True
        if hit_x and hit_y:
            self.app.bnc_corners += 1
            self.app.bnc_walls += 1
            self.app.bnc_shape = (self.app.bnc_shape + 1) % 3
            self.app.bnc_flash = 4.0
        elif hit_x or hit_y:
            self.app.bnc_walls += 1
            if ((self.app.bnc_x <= 1.5) or (self.app.bnc_x >= max_x - 1.5)) and ((self.app.bnc_y <= 1.5) or (self.app.bnc_y >= max_y - 1.5)):
                self.app.bnc_corners += 1
                self.app.bnc_flash = 4.0
        bx, by, bw, bh = int(self.app.bnc_x), int(self.app.bnc_y), self.app.bnc_w, self.app.bnc_h
        if self.app.bnc_flash > 0:
            self.app.bnc_flash = max(0.0, self.app.bnc_flash - dt)
            self.display.draw.rectangle((bx, by, bx + bw, by + bh), fill=1)
            self.display.draw.text((bx + 3, by + 1), "HIT!", font=self.display.font_small, fill=0)
        elif self.app.bnc_shape == 0:
            self.display.draw.rectangle((bx, by, bx + bw, by + bh), outline=1, fill=0)
            self.display.draw_text(bx + 4, by + 1, "VIP")
        elif self.app.bnc_shape == 1:
            pts = [(bx + bw // 2, by), (bx + bw, by + bh // 2), (bx + bw // 2, by + bh), (bx, by + bh // 2)]
            self.display.draw.polygon(pts, outline=1)
        else:
            self.display.draw.ellipse((bx, by, bx + bw, by + bh), outline=1)
            self.display.draw.point((bx + bw // 2, by + bh // 2), fill=1)
