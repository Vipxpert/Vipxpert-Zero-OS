# /vipxpert_minimal_os/src/scenes/scopes/ir_scope.py
from src.ui.scene_manager import ScenePlugin


class IrScopeScene(ScenePlugin):
    # Infrared logic analyzer scene (Renders captured LIRC pulse/space square waves with zoom, pan, and TX replay)
    SCENE_ID = "ir_scope"
    TITLE = "IR Logic Scope"
    CATEGORY = "scope"
    ORDER = 30
    GUIDE_TEXT = "4/6:Pan Wave   7/9:TimeZoom|5:TX Replay    0:ClearBuf"
    IS_INTERACTIVE = True

    async def handle_key(self, action: str, key: str) -> bool:
        # IR Scope Async Key Handler (Pans/zooms pulse timeline, clears buffer, or blasts captured raw IR)
        await self.app._handle_ir_scope_keys(action, key)
        return True

    def draw(self):
        # IR Logic Scope Frame Render (Draws digital logic square wave and protocol header)
        self.app._draw_ir_scope()
