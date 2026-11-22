# /vipxpert_minimal_os/src/scenes/scopes/mic_ultrasonic.py
from src.ui.scene_manager import ScenePlugin


class MicUltrasonicScene(ScenePlugin):
    # Dual-mic FFT and ultrasonic heterodyne scope scene (Renders 18-band FFT, 18-24kHz ultrasonic spectrum, and heterodyne tuner)
    SCENE_ID = "mic_ultrasonic"
    TITLE = "Mic & Ultrasonic"
    CATEGORY = "scope"
    ORDER = 20
    GUIDE_TEXT = "1:FFT 2:18-24kHz 3:Heterod|5:Listen 4/6:LO 7/9:Gain"
    IS_INTERACTIVE = True

    def handle_key(self, action: str, key: str) -> bool:
        # Mic Scope Key Handler (Switches FFT/ultrasonic/heterodyne views, toggles audio monitor, and tunes LO carrier)
        self.app._handle_mic_scope_keys(action, key)
        return True

    def draw(self):
        # Mic & Ultrasonic Frame Render (Draws stereo VU bars, FFT bins, or heterodyne carrier telemetry)
        self.app._draw_mic_ultrasonic()
