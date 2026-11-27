# /vipxpert_minimal_os/tools/build_demo_cart.py
"""Generates a playable 4-shade WASM-4 runner cartridge (/cartridges/geo_dash.wasm) using pure Python."""
import os


def uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def sleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if (n == 0 and (b & 0x40) == 0) or (n == -1 and (b & 0x40) != 0):
            out.append(b)
            return bytes(out)
        out.append(b | 0x80)


def sec(sec_id: int, payload: bytes) -> bytes:
    return bytes([sec_id]) + uleb(len(payload)) + payload


def vec(items) -> bytes:
    return uleb(len(items)) + b"".join(items)


def i32_const(v: int) -> bytes:
    return b"\x41" + sleb(v)


def i32_load(addr: int) -> bytes:
    return i32_const(addr) + b"\x28\x02\x00"


def i32_store(addr: int, val_code: bytes) -> bytes:
    return i32_const(addr) + val_code + b"\x36\x02\x00"


def i32_store16(addr: int, val: int) -> bytes:
    return i32_const(addr) + i32_const(val) + b"\x3b\x01\x00"


def i32_store8(addr: int, val_code: bytes) -> bytes:
    return i32_const(addr) + val_code + b"\x3a\x00\x00"


def build_geo_dash_wasm() -> bytes:
    A_TITLE = 0x2000
    A_SCORE_STR = 0x2020
    A_TICK = 0x2100
    A_PY = 0x2104
    A_PVY = 0x2108
    A_OBSX = 0x210C
    A_SCORE = 0x2110
    GROUND_FP = 104 * 16

    types = vec([
        b"\x60\x00\x00",
        b"\x60\x04\x7f\x7f\x7f\x7f\x00",
        b"\x60\x03\x7f\x7f\x7f\x00",
    ])

    def imp(mod: str, name: str, desc: bytes) -> bytes:
        mb, nb = mod.encode(), name.encode()
        return uleb(len(mb)) + mb + uleb(len(nb)) + nb + desc

    imports = vec([
        imp("env", "memory", b"\x02\x01\x01\x01"),
        imp("env", "rect", b"\x00\x01"),
        imp("env", "tone", b"\x00\x01"),
        imp("env", "text", b"\x00\x02"),
    ])

    funcs = vec([b"\x00", b"\x00"])
    exports = vec([
        uleb(5) + b"start" + b"\x00" + uleb(3),
        uleb(6) + b"update" + b"\x00" + uleb(4),
    ])

    start_body = bytearray()
    for i, rgb in enumerate((0x00080808, 0x00555555, 0x00AAAAAA, 0x00FFFFFF)):
        start_body += i32_store(0x04 + i * 4, i32_const(rgb))
    start_body += i32_store(A_TICK, i32_const(0))
    start_body += i32_store(A_PY, i32_const(GROUND_FP))
    start_body += i32_store(A_PVY, i32_const(0))
    start_body += i32_store(A_OBSX, i32_const(156))
    start_body += i32_store(A_SCORE, i32_const(0))
    start_body += b"\x0b"

    u = bytearray()
    u += i32_store(A_TICK, i32_load(A_TICK) + i32_const(1) + b"\x6a")
    u += i32_const(0x16) + b"\x2d\x00\x00" + b"\x21\x00"
    u += b"\x20\x00" + i32_const(0x41) + b"\x71" + i32_const(0) + b"\x47"
    u += i32_load(A_PY) + i32_const(GROUND_FP) + b"\x4e" + b"\x71"
    u += b"\x04\x40"
    u += i32_store(A_PVY, i32_const(-88))
    u += i32_const(280 | (560 << 16)) + i32_const(6) + i32_const(45) + i32_const(0) + b"\x10\x01"
    u += b"\x0b"

    u += i32_store(A_PY, i32_load(A_PY) + i32_load(A_PVY) + b"\x6a")
    u += i32_store(A_PVY, i32_load(A_PVY) + i32_const(6) + b"\x6a")
    u += i32_load(A_PY) + i32_const(GROUND_FP) + b"\x4a"
    u += b"\x04\x40"
    u += i32_store(A_PY, i32_const(GROUND_FP))
    u += i32_store(A_PVY, i32_const(0))
    u += b"\x0b"

    u += i32_store(A_OBSX, i32_load(A_OBSX) + i32_const(-2) + b"\x6a")
    u += i32_load(A_OBSX) + i32_const(-12) + b"\x48"
    u += b"\x04\x40"
    u += i32_store(A_OBSX, i32_const(164))
    u += i32_store(A_SCORE, i32_load(A_SCORE) + i32_const(1) + b"\x6a")
    u += b"\x0b"

    u += i32_load(A_PY) + i32_const(4) + b"\x75" + b"\x21\x01"
    u += i32_load(A_OBSX) + b"\x21\x02"

    u += b"\x20\x02" + i32_const(40) + b"\x48"
    u += b"\x20\x02" + i32_const(20) + b"\x4a" + b"\x71"
    u += b"\x20\x01" + i32_const(92) + b"\x4a" + b"\x71"
    u += b"\x04\x40"
    u += i32_store(A_SCORE, i32_const(0))
    u += i32_store(A_OBSX, i32_const(164))
    u += i32_const(160 | (60 << 16)) + i32_const(12) + i32_const(55) + i32_const(3) + b"\x10\x01"
    u += b"\x0b"

    u += i32_load(A_SCORE) + b"\x21\x03"
    for idx, div in enumerate((1000, 100, 10, 1)):
        digit = b"\x20\x03" + i32_const(div) + b"\x6e" + i32_const(10) + b"\x70" + i32_const(48) + b"\x6a"
        u += i32_store8(A_SCORE_STR + idx, digit)

    u += i32_store16(0x14, 0x0002)
    u += i32_const(0) + i32_const(40) + i32_const(160) + i32_const(18) + b"\x10\x00"
    u += i32_store16(0x14, 0x0003)
    u += i32_const(160) + i32_load(A_TICK) + i32_const(127) + b"\x71" + b"\x6b"
    u += i32_const(60) + i32_const(22) + i32_const(10) + b"\x10\x00"
    u += i32_store16(0x14, 0x0043)
    u += i32_const(0) + i32_const(114) + i32_const(160) + i32_const(6) + b"\x10\x00"
    u += i32_store16(0x14, 0x0024)
    u += i32_const(30) + b"\x20\x01" + i32_const(10) + i32_const(10) + b"\x10\x00"
    u += i32_store16(0x14, 0x0043)
    u += b"\x20\x02" + i32_const(100) + i32_const(10) + i32_const(14) + b"\x10\x00"
    u += i32_store16(0x14, 0x0004)
    u += i32_const(A_TITLE) + i32_const(6) + i32_const(45) + b"\x10\x02"
    u += i32_const(A_SCORE_STR) + i32_const(120) + i32_const(45) + b"\x10\x02"
    u += b"\x0b"

    def encode_func(locals_spec: bytes, body: bytes) -> bytes:
        raw = locals_spec + body
        return uleb(len(raw)) + raw

    code = vec([
        encode_func(b"\x00", bytes(start_body)),
        encode_func(b"\x01\x04\x7f", bytes(u)),
    ])

    def data_seg(addr: int, raw: bytes) -> bytes:
        return b"\x00" + i32_const(addr) + b"\x0b" + uleb(len(raw)) + raw

    data = vec([
        data_seg(A_TITLE, b"GEO DASH\x00"),
        data_seg(A_SCORE_STR, b"0000\x00"),
    ])

    return (
        b"\x00asm\x01\x00\x00\x00"
        + sec(1, types)
        + sec(2, imports)
        + sec(3, funcs)
        + sec(7, exports)
        + sec(10, code)
        + sec(11, data)
    )


if __name__ == "__main__":
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    out_dir = os.path.join(root, "cartridges")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "geo_dash.wasm")
    wasm = build_geo_dash_wasm()
    with open(out_path, "wb") as f:
        f.write(wasm)
    print(f"[OK] Built {out_path} ({len(wasm)} bytes)")
