# /vipxpert_minimal_os/tools/device_control.py
"""
Virtual Device Control Adapter & CLI for VIPXpert Minimal OS.
Connects to /tmp/vipxpert_devctl.sock inside the live OS service to inject
virtual keypad events (DOWN/CLICK/HOLD), navigate menus by item label, and
inspect real-time UI/scene/audio/power state without physical button presses.
"""
import argparse
import json
import socket
import sys
from typing import Any, Dict, List


class DeviceAdapter:
    """Client adapter for controlling and inspecting the live VIPXpert OS UI."""

    def __init__(self, sock_path: str = "/tmp/vipxpert_devctl.sock", timeout: float = 5.0):
        self.sock_path = sock_path
        self.timeout = timeout

    def _call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect(self.sock_path)
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            chunks = []
            while True:
                buf = s.recv(4096)
                if not buf:
                    break
                chunks.append(buf)
                if b"\n" in buf:
                    break
        line = b"".join(chunks).decode("utf-8", errors="replace").splitlines()[0]
        resp = json.loads(line)
        if not resp.get("ok", True):
            st = resp.get("state") or {}
            m_items = [x.get("name") for x in (st.get("menu") or {}).get("items", [])]
            raise RuntimeError(f"DevCtl RPC failed: {resp.get('error')} | app={st.get('current_app_id')} menu={(st.get('menu') or {}).get('current_menu')} items={m_items}")
        return resp

    def state(self) -> Dict[str, Any]:
        return self._call({"cmd": "state"})

    def press(self, key: str, action: str = "CLICK", wait_ms: int = 60) -> Dict[str, Any]:
        return self._call({"cmd": "press", "key": key, "action": action, "wait_ms": wait_ms})

    def tap(self, key: str, wait_ms: int = 60) -> Dict[str, Any]:
        return self._call({"cmd": "tap", "key": key, "wait_ms": wait_ms})

    def hold(self, key: str, wait_ms: int = 100) -> Dict[str, Any]:
        return self._call({"cmd": "hold", "key": key, "wait_ms": wait_ms})

    def goto(self, app_id: str, wait_ms: int = 80) -> Dict[str, Any]:
        return self._call({"cmd": "goto", "app_id": app_id, "wait_ms": wait_ms})

    def select(self, name: str, click: bool = True, wait_ms: int = 70) -> Dict[str, Any]:
        return self._call({"cmd": "select", "name": name, "click": click, "wait_ms": wait_ms})

    def usb_takeover(self, simulate: bool = False, wait_ms: int = 120) -> Dict[str, Any]:
        return self._call({"cmd": "usb_takeover", "simulate": simulate, "wait_ms": wait_ms})

    def release_sim_usb(self, wait_ms: int = 120) -> Dict[str, Any]:
        return self._call({"cmd": "release_sim_usb", "wait_ms": wait_ms})

    def tap_many(self, keys: List[str], wait_ms: int = 60) -> Dict[str, Any]:
        st = self.state()
        for k in keys:
            if k.endswith(":hold"):
                st = self.hold(k.split(":", 1)[0], wait_ms=wait_ms)
            else:
                st = self.tap(k, wait_ms=wait_ms)
        return st


def run_verify_scenes(adapter: DeviceAdapter) -> int:
    """Automated end-to-end verification of Category->Scene->Preview and Per-Scene Settings."""
    st0 = adapter.state()
    print(f"[1] Initial state: app={st0.get('current_app_id')} screen_off={st0.get('is_screen_off')} bat={st0.get('power', {}).get('percent', 0):.1f}%")

    # Navigate into Settings root -> Lock & Scenes -> Scene Slots -> Music Slot -> Music & Audio HUDs
    st_set = adapter.goto("settings")
    for _ in range(6):
        if (st_set.get("menu") or {}).get("current_menu") == "root":
            break
        st_set = adapter.tap("D")
    st_lock = adapter.select("Lock & Scenes")
    print(f"[2] Entered: menu={st_lock['menu']['current_menu']} title={st_lock['menu']['title']} items={st_lock['menu']['item_count']}")

    adapter.select("Scene Slots")
    st_cat = adapter.select("Music Slot")
    cat_names = [x["name"] for x in st_cat["menu"]["items"]]
    print(f"[3] Category list: menu={st_cat['menu']['current_menu']} cursor={st_cat['menu']['cursor_index']} cats={cat_names}")

    music_cat = next((c for c in cat_names if "Music" in c), cat_names[-1])
    st_scns = adapter.select(music_cat)
    orig_active = next((x["name"] for x in st_scns["menu"]["items"] if "Active" in x["suffix"]), None)
    print(f"[4] Scene list: title={st_scns['menu']['title']} active_before={orig_active} count={st_scns['menu']['item_count']}")

    # Open Live Scene Preview on active scene in category
    st_prev1 = adapter.tap("C", wait_ms=120)
    sc1 = st_prev1["lock"]["standalone_scene"]
    print(f"[5] Live Preview opened: app={st_prev1['current_app_id']} scene={sc1} pick_key={st_prev1['lock']['pick_db_key']}")

    # Switch scene with B, verify D back without saving
    st_prev2 = adapter.tap("B", wait_ms=100)
    sc2 = st_prev2["lock"]["standalone_scene"]
    assert sc2 != sc1, f"Expected B to switch scene in preview, stayed on {sc1}"
    st_back = adapter.tap("D", wait_ms=100)
    assert st_back["current_app_id"] == "settings" and st_back["menu"]["current_menu"] == "lock_cat_scenes", f"Expected D to return to lock_cat_scenes, got {st_back}"
    still_active = next((x["name"] for x in st_back["menu"]["items"] if "Active" in x["suffix"]), None)
    assert still_active == orig_active, f"Expected D back without saving to preserve {orig_active}, got {still_active}"
    print(f"[6] Verified A/B switch ({sc1} -> {sc2}) + D back without saving (preserved {still_active})")

    # Verify Per-Scene Setup (16 scenes, A/B tuning in Tuning Mode, Custom badge, Reset Scene)
    adapter.tap("D")
    adapter.tap("D")
    adapter.tap("D")
    st_ps = adapter.select("Per-Scene Setup")
    print(f"[7] Per-Scene Setup count: {st_ps['menu']['item_count']} scenes")
    adapter.select("Rain Pond")
    adapter.select("Rain Density", click=False)
    adapter.tap("C")
    adapter.tap("A")
    st_pond_tuned = adapter.tap("C")
    rain_row = next(x for x in st_pond_tuned["menu"]["items"] if x["name"] == "Rain Density")
    assert "Heavy (7)" in rain_row["suffix"], f"Expected A/B tuning on Rain Density -> Heavy (7), got {rain_row}"
    st_ps_after = adapter.tap("D")
    pond_entry = next(x for x in st_ps_after["menu"]["items"] if x["name"] == "Rain Pond")
    assert "Custom" in pond_entry["suffix"], f"Expected Rain Pond badge Custom >, got {pond_entry}"
    adapter.select("Rain Pond")
    adapter.select("Reset Scene")
    st_ps_reset = adapter.tap("D")
    pond_reset = next(x for x in st_ps_reset["menu"]["items"] if x["name"] == "Rain Pond")
    assert "Global" in pond_reset["suffix"], f"Expected Reset Scene to restore Global ★, got {pond_reset}"
    print(f"[8] Verified Per-Scene A/B tuning ({rain_row['suffix']}), Custom badge, and Reset Scene -> {pond_reset['suffix']}")

    # Return cleanly to lock_screen
    adapter.goto("lock_screen")
    print("[9] PASS: All automated Device Control Adapter navigation & scene assertions succeeded!")
    return 0


def main():
    parser = argparse.ArgumentParser(description="VIPXpert OS Virtual Device Control Adapter")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state")
    p_tap = sub.add_parser("tap")
    p_tap.add_argument("keys", nargs="+")
    p_hold = sub.add_parser("hold")
    p_hold.add_argument("key")
    p_goto = sub.add_parser("goto")
    p_goto.add_argument("app_id")
    p_sel = sub.add_parser("select")
    p_sel.add_argument("name")
    p_sel.add_argument("--no-click", action="store_true")
    sub.add_parser("verify-scenes")
    sub.add_parser("verify-usb-audio")

    args = parser.parse_args()
    ad = DeviceAdapter()
    if args.cmd == "state":
        print(json.dumps(ad.state(), indent=2))
    elif args.cmd == "tap":
        print(json.dumps(ad.tap_many(args.keys), indent=2))
    elif args.cmd == "hold":
        print(json.dumps(ad.hold(args.key), indent=2))
    elif args.cmd == "goto":
        print(json.dumps(ad.goto(args.app_id), indent=2))
    elif args.cmd == "select":
        print(json.dumps(ad.select(args.name, click=not args.no_click), indent=2))
    elif args.cmd == "verify-scenes":
        sys.exit(run_verify_scenes(ad))
    elif args.cmd == "verify-usb-audio":
        st_aud = ad.goto("audio_settings")
        if (st_aud.get("menu") or {}).get("current_menu") != "root":
            st_aud = ad.tap("D")
        st_usb = ad.select("USB DAC")
        row_in = next(x for x in st_usb["menu"]["items"] if x["name"] == "USB Audio In")
        print(f"[1] Audio Settings -> USB DAC: USB Audio In = {row_in['suffix']}")
        st_cycled = ad.select("USB Audio In")
        row_in2 = next(x for x in st_cycled["menu"]["items"] if x["name"] == "USB Audio In")
        assert "Always On" in row_in2["suffix"], f"Expected Always On, got {row_in2}"
        ad.select("USB Audio In")
        st_restored = ad.select("USB Audio In")
        row_in3 = next(x for x in st_restored["menu"]["items"] if x["name"] == "USB Audio In")
        assert "Auto (Takeover)" in row_in3["suffix"], f"Expected Auto (Takeover) ★, got {row_in3}"
        print(f"[2] Verified USB Audio In mode cycle -> {row_in3['suffix']}")
        st_tk = ad.usb_takeover(simulate=True)
        assert st_tk["audio"]["active_source"] == "USB", f"Expected USB takeover, got {st_tk['audio']}"
        st_lk = ad.goto("lock_screen")
        print(f"[3] Verified USB Last-Active Takeover: source={st_tk['audio']['active_source']} lock_slot={st_lk['lock']['active_slot']} scene={st_lk['lock']['active_scene_id']}")
        ad.release_sim_usb()
        print("[4] PASS: USB Audio Mode & Last-Active Takeover verified cleanly!")
        sys.exit(0)


if __name__ == "__main__":
    main()
