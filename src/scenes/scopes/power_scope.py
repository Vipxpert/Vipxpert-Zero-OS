# /vipxpert_minimal_os/src/scenes/scopes/power_scope.py
from src.ui.scene_manager import ScenePlugin


class PowerScopeScene(ScenePlugin):
    # USB power and INA219 oscilloscope scene (Renders current waveform, cable resistance test, and USB audit)
    SCENE_ID = "power_scope"
    TITLE = "USB Power Scope"
    CATEGORY = "scope"
    ORDER = 10
    GUIDE_TEXT = "1:Wave 2:CableR 3:USBAudit|5:Hold 7/9:Time 0:TareBase"
    IS_INTERACTIVE = True

    def handle_key(self, action: str, key: str) -> bool:
        # Power Scope Key Handler (Switches view modes, pauses capture, tares baseline, and changes timebase)
        self.app._handle_power_scope_keys(action, key)
        return True

    def draw(self):
        # Power Scope Frame Render (Draws INA219 current graph, cable sag resistance, or delta power audit)
        self.app._draw_power_scope()
