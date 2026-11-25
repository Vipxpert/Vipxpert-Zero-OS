# /vipxpert_minimal_os/src/hardware/usb_dac_mixer.py
import glob
import os
import re
import subprocess
from typing import Dict, List, Optional

_PREFERRED_CONTROLS = ("pcm", "master", "headphone", "speaker", "dac", "line", "digital")
_CTRL_HDR_RE = re.compile(r"^Simple mixer control '([^']+)',(\d+)")
_LIMITS_RE = re.compile(r"Limits:\s*(?:Playback\s+)?(\d+)\s*-\s*(\d+)")
_PLAYBACK_VAL_RE = re.compile(r"Playback\s+(\d+)\s+\[(\d+)%\](?:\s+\[([^\]]+)\])?")


def _parse_scontents(text: str) -> List[Dict]:
    """Parses `amixer -c <idx> scontents` into playback-capable hardware volume controls."""
    controls: List[Dict] = []
    cur: Optional[Dict] = None

    for raw_line in text.splitlines():
        m_hdr = _CTRL_HDR_RE.match(raw_line)
        if m_hdr:
            if cur is not None:
                controls.append(cur)
            cur = {
                "control": m_hdr.group(1),
                "control_idx": int(m_hdr.group(2)),
                "caps": "",
                "min_val": 0,
                "max_val": 0,
                "cur_val": 0,
                "pct": 0,
                "db_str": "",
            }
            continue
        if cur is None:
            continue
        line = raw_line.strip()
        if line.startswith("Capabilities:"):
            cur["caps"] = line.split(":", 1)[1].strip()
        elif line.startswith("Limits:"):
            # Comment (constraint): Some USB DACs report `Limits: Playback 0 - 120`, others `Limits: 0 - 64`; match both and ignore capture-only limits.
            if "Capture" in line and "Playback" not in line:
                continue
            m_lim = _LIMITS_RE.search(line)
            if m_lim:
                cur["min_val"] = int(m_lim.group(1))
                cur["max_val"] = int(m_lim.group(2))
        else:
            m_val = _PLAYBACK_VAL_RE.search(line)
            if m_val and cur["cur_val"] == 0 and cur["pct"] == 0:
                cur["cur_val"] = int(m_val.group(1))
                cur["pct"] = int(m_val.group(2))
                cur["db_str"] = (m_val.group(3) or "").strip()

    if cur is not None:
        controls.append(cur)

    valid: List[Dict] = []
    for c in controls:
        caps_tokens = set(c["caps"].split())
        has_pvol = bool(caps_tokens & {"pvolume", "pvolume-joined", "volume", "volume-joined"})
        if has_pvol and c["max_val"] > c["min_val"]:
            valid.append(c)
    return valid


def _score_control(ctrl: Dict) -> tuple:
    name_l = ctrl["control"].lower()
    rank = len(_PREFERRED_CONTROLS)
    for i, pref in enumerate(_PREFERRED_CONTROLS):
        if name_l == pref or pref in name_l:
            rank = i
            break
    return (rank, -(ctrl["max_val"] - ctrl["min_val"]), ctrl["control_idx"])


def discover_usb_dac_mixer() -> Dict:
    """
    Universally discovers any connected USB audio card via `/proc/asound/card*/usbid`
    and probes its ALSA hardware playback volume control (`pvolume`).
    Returns dict with `status` in ('ok', 'unsupported', 'no_dac').
    """
    usb_cards: List[tuple] = []
    for usbid_path in sorted(glob.glob("/proc/asound/card*/usbid")):
        card_dir = os.path.dirname(usbid_path)
        base = os.path.basename(card_dir)
        if not base.startswith("card"):
            continue
        try:
            card_idx = int(base[4:])
        except ValueError:
            continue
        try:
            with open(usbid_path, "r", encoding="utf-8") as f:
                usbid = f.read().strip()
        except Exception:
            usbid = ""
        card_name = f"USB-{card_idx}"
        id_path = os.path.join(card_dir, "id")
        if os.path.isfile(id_path):
            try:
                with open(id_path, "r", encoding="utf-8") as f:
                    card_name = f.read().strip() or card_name
            except Exception:
                pass
        usb_cards.append((card_idx, usbid, card_name))

    if not usb_cards:
        return {"status": "no_dac"}

    first_unsupported: Optional[Dict] = None
    for card_idx, usbid, card_name in usb_cards:
        try:
            res = subprocess.run(
                ["amixer", "-c", str(card_idx), "scontents"],
                capture_output=True, text=True, timeout=1.5
            )
            if res.returncode != 0:
                continue
            ctrls = _parse_scontents(res.stdout)
            if ctrls:
                ctrls.sort(key=_score_control)
                best = ctrls[0]
                return {
                    "status": "ok",
                    "card": card_idx,
                    "usbid": usbid,
                    "card_name": card_name,
                    "control": best["control"],
                    "min_val": best["min_val"],
                    "max_val": best["max_val"],
                    "cur_val": best["cur_val"],
                    "pct": best["pct"],
                    "db_str": best["db_str"],
                }
            if first_unsupported is None:
                first_unsupported = {
                    "status": "unsupported",
                    "card": card_idx,
                    "usbid": usbid,
                    "card_name": card_name,
                }
        except Exception:
            continue

    return first_unsupported or {"status": "no_dac"}


def set_usb_dac_volume(card: int, control: str, raw_val: int) -> Optional[Dict]:
    """Sets exact ALSA hardware mixer step on any USB DAC and returns parsed readback."""
    try:
        res = subprocess.run(
            ["amixer", "-c", str(card), "sset", control, str(int(raw_val))],
            capture_output=True, text=True, timeout=1.5
        )
        if res.returncode != 0:
            return None
        ctrls = _parse_scontents(res.stdout)
        if ctrls:
            c = ctrls[0]
            return {
                "cur_val": c["cur_val"],
                "pct": c["pct"],
                "db_str": c["db_str"],
            }
    except Exception:
        pass
    return None
