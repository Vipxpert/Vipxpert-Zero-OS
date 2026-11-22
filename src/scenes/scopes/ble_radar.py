# /vipxpert_minimal_os/src/scenes/scopes/ble_radar.py
from src.ui.scene_manager import ScenePlugin


class BleRadarScene(ScenePlugin):
    # BLE proximity radar scene (Renders polar RSSI radar, AirTag/SmartTag/Tile tracker list, and Geiger audio mode)
    SCENE_ID = "ble_radar"
    TITLE = "BLE Tracker Radar"
    CATEGORY = "scope"
    ORDER = 40
    GUIDE_TEXT = "1:Radar Sweep  2:TrackerLst|4/6:Target 5:Geiger 0:Clr"
    IS_INTERACTIVE = True

    def handle_key(self, action: str, key: str) -> bool:
        # BLE Radar Key Handler (Toggles radar/list view, selects target MAC, or toggles Geiger clicker)
        self.app._handle_ble_radar_keys(action, key)
        return True

    def draw(self):
        # BLE Radar Frame Render (Draws polar radar rings, distance estimate, and RSSI history trace)
        self.app._draw_ble_radar()
