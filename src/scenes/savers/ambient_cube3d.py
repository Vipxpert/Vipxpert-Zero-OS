# /vipxpert_minimal_os/src/scenes/savers/ambient_cube3d.py
from src.ui.scene_manager import ScenePlugin


class AmbientCube3dScene(ScenePlugin):
    # Interactive 3D wireframe scene (Renders rotatable cube/octahedron with shading and zoom controls)
    SCENE_ID = "ambient_cube3d"
    TITLE = "Interactive 3D"
    CATEGORY = "saver"
    ORDER = 10
    GUIDE_TEXT = "2/4/6/8:Rotate 5:Auto 0:Rst|1:Mesh 3:Gray 7/9:Zoom"
    IS_INTERACTIVE = True
    IS_SAVER = True

    def handle_key(self, action: str, key: str) -> bool:
        # 3D Keypad Input Handler (Processes rotation, auto-spin, mesh toggle, dithering, and zoom)
        self.app._handle_interactive_3d_keys(action, key)
        return True

    def draw(self):
        # 3D Solid & Wireframe Render (Projects wall-clock rotated 3D mesh with outward Lambertian shading onto 1-bit canvas)
        import math
        self.display.has_active_marquee = True
        ph, _ = self.app.advance_scene_phase(self.SCENE_ID, ref_fps=30.0)
        if self.app.cube_auto:
            ax, ay = self.app.cube_pitch + ph * 0.04, self.app.cube_yaw + ph * 0.07
        else:
            ax, ay = self.app.cube_pitch, self.app.cube_yaw
        if self.app.cube_mesh == 0:
            verts = [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]
            edges = [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)]
            faces = ((4,5,6,7), (1,0,3,2), (0,4,7,3), (5,1,2,6), (3,7,6,2), (0,1,5,4))
        else:
            verts = [(0,1.3,0),(0,-1.3,0),(1.3,0,0),(-1.3,0,0),(0,0,1.3),(0,0,-1.3)]
            edges = [(0,2),(0,3),(0,4),(0,5),(1,2),(1,3),(1,4),(1,5),(2,4),(4,3),(3,5),(5,2)]
            faces = ((0,4,2), (0,3,4), (0,5,3), (0,2,5), (1,2,4), (1,4,3), (1,3,5), (1,5,2))

        # Comment (workaround): Display.render_wireframe_3d inverts Z-normals on Y-down OLED coordinates (forcing lambert=0 / shade=0.48) and writes _pwm_gray_planes (shm_pwm=2 full-screen MV mode); rasterizing outward faces directly onto display.image with ordered 4x4 Bayer shading + crisp white edges guarantees 100% visibility in every dither mode without touching display.py.
        cx, cy, scale, dist = 64, 31, max(8.0, float(self.app.cube_scale)), 3.5
        cx_a, sx_a = math.cos(ax), math.sin(ax)
        cy_a, sy_a = math.cos(ay), math.sin(ay)
        rot_v, proj = [], []
        for vx, vy, vz in verts:
            y1 = vy * cx_a - vz * sx_a
            z1 = vy * sx_a + vz * cx_a
            x2 = vx * cy_a + z1 * sy_a
            z2 = -vx * sy_a + z1 * cy_a
            rot_v.append((x2, y1, z2))
            fov = dist / max(1.8, dist - z2 * 0.45)
            proj.append((int(round(cx + x2 * scale * fov)), int(round(cy + y1 * scale * fov))))

        bayer = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))
        use_shade = getattr(self.display, "dither_mode", "pwm") != "off"
        vis = []
        for fv in faces:
            p0, p1, p2 = proj[fv[0]], proj[fv[1]], proj[fv[2]]
            # In Y-down screen space with outward CCW 3D faces, front-facing polygons have cross_z > 0
            if (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0]) <= 0:
                continue
            v0, v1, v2 = rot_v[fv[0]], rot_v[fv[1]], rot_v[fv[2]]
            ux, uy, uz = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
            wx, wy, wz = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
            nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
            nl = max(1e-6, math.sqrt(nx * nx + ny * ny + nz * nz))
            lum = max(0.0, (nx * -0.35 + ny * -0.45 + nz * 0.82) / nl)
            avg_z = sum(rot_v[i][2] for i in fv) / len(fv)
            vis.append((avg_z, [proj[i] for i in fv], max(1, min(11, int(1 + lum * 10)))))

        vis.sort(key=lambda it: it[0])
        px = self.display.image.load()
        for _, poly, thresh in vis:
            min_y = max(0, min(p[1] for p in poly))
            max_y = min(63, max(p[1] for p in poly))
            np_l = len(poly)
            for y in range(min_y, max_y + 1):
                nodes = []
                j = np_l - 1
                for i in range(np_l):
                    yi, yj = poly[i][1], poly[j][1]
                    if (yi < y <= yj) or (yj < y <= yi):
                        nodes.append(int(poly[i][0] + (y - yi) * (poly[j][0] - poly[i][0]) / float(yj - yi)))
                    j = i
                nodes.sort()
                brow = bayer[y & 3]
                for k in range(0, len(nodes) - 1, 2):
                    sx0, sx1 = max(0, nodes[k]), min(127, nodes[k + 1])
                    for x in range(sx0, sx1 + 1):
                        px[x, y] = (1 if brow[x & 3] < thresh else 0) if use_shade else 0
            for i in range(np_l):
                self.display.draw.line((poly[i], poly[(i + 1) % np_l]), fill=1)
        if not vis:
            for i, j in edges:
                self.display.draw.line((proj[i], proj[j]), fill=1)
