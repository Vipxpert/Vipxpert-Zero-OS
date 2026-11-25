# /vipxpert_minimal_os/src/scenes/savers/ambient_3d_test.py
from src.ui.scene_manager import ScenePlugin


class Ambient3dTestScene(ScenePlugin):
    # Dedicated 3D Wireframe + 5-Band Grayscale Hardware Benchmark Scene (Promoted from legacy standalone Settings button)
    SCENE_ID = "ambient_3d_test"
    TITLE = "3D Gray Bench"
    CATEGORY = "saver"
    ORDER = 15
    GUIDE_TEXT = "1:Mesh 3:GrayMode 5:Spin|2/4/6/8:Rotate 7/9:Zoom"
    IS_INTERACTIVE = True
    IS_SAVER = True

    def handle_key(self, action: str, key: str) -> bool:
        self.app._handle_interactive_3d_keys(action, key)
        return True

    def draw(self):
        self.display.has_active_marquee = True
        ph, _ = self.app.advance_scene_phase(self.SCENE_ID, ref_fps=30.0)
        ax = (self.app.cube_pitch + ph * 0.045) if self.app.cube_auto else self.app.cube_pitch
        ay = (self.app.cube_yaw + ph * 0.075) if self.app.cube_auto else self.app.cube_yaw

        if self.app.cube_mesh == 0:
            verts = [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]
            edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
        else:
            verts = [(0, 1.3, 0), (0, -1.3, 0), (1.3, 0, 0), (-1.3, 0, 0), (0, 0, 1.3), (0, 0, -1.3)]
            edges = [(0, 2), (0, 3), (0, 4), (0, 5), (1, 2), (1, 3), (1, 4), (1, 5), (2, 4), (4, 3), (3, 5), (5, 2)]

        # Top 5-step hardware gray reference ramp + live 3D hardware-shaded wireframe mesh
        if hasattr(self.display, "draw_shaded_rect"):
            for i, sh in enumerate((0.20, 0.35, 0.50, 0.67, 1.0)):
                x0 = 4 + i * 24
                self.display.draw_shaded_rect(x0, 2, x0 + 21, 7, shade=sh, phase_id=i)
        self.display.draw.rectangle((2, 1, 125, 8), outline=1, fill=None)

        scale = max(10.0, float(getattr(self.app, "cube_scale", 18.0)))
        if hasattr(self.display, "render_wireframe_3d"):
            self.display.render_wireframe_3d(verts, edges, ax, ay, cx=64, cy=36, scale=scale, dist=3.3)
