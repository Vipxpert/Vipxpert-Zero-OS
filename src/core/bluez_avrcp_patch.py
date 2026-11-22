"""
Idempotent binary patch for BlueZ 5.72 (/usr/libexec/bluetooth/bluetoothd on aarch64).

In avrcp_get_item_attributes_rsp (0x511d0), Android AVRCP Browsing (PDU 0x73)
can return status 0x04 (AVRCP_STATUS_SUCCESS) with 0 attributes (73 00 02 04 00)
or a Duration-only stub. Replacing b.hi 0x5121c at 0x511fc (08 01 00 54) with
NOP (1f 20 03 d5) forces execution to fall through to BlueZ's built-in 0x51200
fallback: avrcp_get_element_attributes(session) (Control channel PDU 0x20).
"""
import os
import shutil
import subprocess

BLUETOOTHD_PATH = "/usr/libexec/bluetooth/bluetoothd"
PATCH_OFFSET = 0x511FC
ORIG_BYTES = bytes.fromhex("08010054")   # b.hi 0x5121c
PATCH_BYTES = bytes.fromhex("1f2003d5")  # nop


def ensure_bluez_avrcp_patch() -> bool:
    if not os.path.exists(BLUETOOTHD_PATH):
        return False
    try:
        with open(BLUETOOTHD_PATH, "rb") as f:
            f.seek(PATCH_OFFSET)
            cur = f.read(4)
        if cur == PATCH_BYTES:
            return True
        if cur != ORIG_BYTES:
            return False

        bak = BLUETOOTHD_PATH + ".bak"
        new = BLUETOOTHD_PATH + ".new"
        if not os.path.exists(bak):
            subprocess.run(["sudo", "-n", "cp", "-p", BLUETOOTHD_PATH, bak], check=False)

        data = bytearray(open(BLUETOOTHD_PATH, "rb").read())
        data[PATCH_OFFSET:PATCH_OFFSET + 4] = PATCH_BYTES
        tmp = "/tmp/bluetoothd.patched"
        with open(tmp, "wb") as f:
            f.write(data)
        subprocess.run(
            ["sudo", "-n", "bash", "-c",
             f"cp --attributes-only --preserve=mode,ownership {BLUETOOTHD_PATH} {tmp} && "
             f"mv -f {tmp} {BLUETOOTHD_PATH} && systemctl restart bluetooth.service"],
            check=True,
        )
        return True
    except Exception:
        return False


if __name__ == "__main__":
    ok = ensure_bluez_avrcp_patch()
    print("BlueZ AVRCP patch status:", "APPLIED/VERIFIED" if ok else "SKIPPED")
