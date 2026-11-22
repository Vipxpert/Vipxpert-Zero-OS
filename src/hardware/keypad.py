# /vipxpert_minimal_os/src/hardware/keypad.py
import asyncio
import os
import threading
import time
import gpiod
from gpiod.line import Direction, Value, Bias, Drive

class Keypad:
    # Reverted to confirmed working rows and mapped C4 to valid Pin 7 (PI12 -> GPIO 268)
    # ROWS = [226, 227, 229, 230] 
    # COLS = [231, 232, 233, 268]

    # ROWS = [226, 227, 229, 230] 
    # COLS = [231, 232, 233, 268]

    # Final corrected software-to-hardware mapping layout
    ROWS = [226, 227, 229, 230] 
    COLS = [231, 232, 233, 268]
    
    KEYMAP = [
        ['1', '2', '3', 'A'],
        ['4', '5', '6', 'B'],
        ['7', '8', '9', 'C'],
        ['*', '0', '#', 'D']
    ]

    def __init__(self):
        self.chip_path = "/dev/gpiochip1"
        self.request = None
        self.last_pressed = None
        self.pressed_time = 0
        
        # Dedicated Hardware Thread & Zero-Lag Event Queue State
        self.is_running = False
        self._raw_active_key = None
        self._hardware_thread = None
        self._loop = None
        self._event_queue = None
        self._release_miss_count = 0

        # Diagnostics
        self.debug_noise = True

        # Time-Aware Edge Detection Configs
        self.hold_threshold = 0.8
        self.current_key = None
        self.press_time = 0.0
        self.hold_emitted = False
        self.last_repeat_time = 0.0
        self.current_repeat_delay = 0.15


        # Adjust these parameters to eliminate capacitive ghosting / EMI floating triggers.
        
#         self.settle_time = 0.005 # Time (s) for row pin voltage to stabilize after dropping LOW.
#         self.target_samples = 5 # Total matching sequential validations required to accept a press.
#         self.sample_delay = 0.02 # Time (s) between checks inside the multi-sample validation window.
#         self.master_poll_interval = 0.02 # Global sleep time (s) before starting a completely new matrix sweep.

        # --- ALL-16-BUTTON HIGH-SENSITIVITY TIMING KNOBS ---
        # Comment (hardware-quirk): Row 1 (GPIO 226 / PH2: keys 1, 2, 3, A) and Row 2 (GPIO 227 / PH3: keys 4, 5, 6, B) share higher pad capacitance + membrane contact resistance, and the old single-sample gatekeeper + 2-stage polling sleep rejected taps when sample #1 bounced or added ~24ms CLICK lag. Use Drive.OPEN_DRAIN + Bias.PULL_UP on all 4 rows, unconditional 3-pass batch get_values(COLS) 2-of-3 majority voting, 1-sweep release hysteresis, and direct call_soon_threadsafe asyncio.Queue wake.
        self.row0_settle_time = 0.00095   # 950us uniform RC settle for PH2/PH3 UART5 mux pads (Rows 1..2)
        self.settle_time = 0.00085        # 850us RC settle for Rows 3..4
        self.target_samples = 3           # Unconditional 3-pass batch column snapshot per row
        self.min_valid_samples = 2        # 2-of-3 majority threshold (catches >=4ms taps, rejects <350us EMI)
        self.sample_delay = 0.00035       # 350us between batch reads (~1.55ms total per row, ~6.4ms full matrix)
        self.master_poll_interval = 0.004 # 4ms inter-sweep rest (~93Hz full-matrix cadence, <10ms worst-case latency)
        self.release_debounce_sweeps = 1  # Bridge 1 transient open sweep (~9ms) so worn domes never chatter/drop

    def setup(self):
        line_config = {}
        for r in self.ROWS:
            line_config[r] = gpiod.LineSettings(
                direction=Direction.OUTPUT,
                drive=Drive.OPEN_DRAIN,
                bias=Bias.PULL_UP,
                output_value=Value.ACTIVE,
            )
        for c in self.COLS:
            line_config[c] = gpiod.LineSettings(
                direction=Direction.INPUT,
                bias=Bias.PULL_UP,
            )

        self.request = gpiod.request_lines(
            self.chip_path,
            consumer="os-keypad",
            config=line_config,
        )

        self.is_running = True
        self._hardware_thread = threading.Thread(
            target=self._hardware_scan_loop,
            daemon=True,
            name="KeypadScanner",
        )
        self._hardware_thread.start()

    def _emit_event(self, evt):
        loop = self._loop
        q = self._event_queue
        if loop is not None and q is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(q.put_nowait, evt)
            except Exception:
                pass

    def scan_matrix_sync(self):
        """Unconditional 3-pass batch-ioctl matrix scan across all 16 buttons with multi-key ghost guard."""
        if not self.request:
            return None

        min_hits = self.min_valid_samples
        hits = []
        try:
            for r_idx, r_pin in enumerate(self.ROWS):
                self.request.set_value(r_pin, Value.INACTIVE)
                time.sleep(self.row0_settle_time if r_idx <= 1 else self.settle_time)

                # Take all 3 batch snapshots of COLS unconditionally so an initial contact bounce never aborts detection
                s0 = self.request.get_values(self.COLS)
                time.sleep(self.sample_delay)
                s1 = self.request.get_values(self.COLS)
                time.sleep(self.sample_delay)
                s2 = self.request.get_values(self.COLS)

                self.request.set_value(r_pin, Value.ACTIVE)
                time.sleep(0.00018)

                for c_idx in range(4):
                    low_cnt = (
                        (1 if s0[c_idx] == Value.INACTIVE else 0)
                        + (1 if s1[c_idx] == Value.INACTIVE else 0)
                        + (1 if s2[c_idx] == Value.INACTIVE else 0)
                    )
                    if low_cnt >= min_hits:
                        hits.append(self.KEYMAP[r_idx][c_idx])
        except Exception:
            return None

        if not hits:
            return None
        if len(hits) == 1:
            return hits[0]
        if self.current_key in hits:
            return self.current_key
        return None

    def _hardware_scan_loop(self):
        """Deterministic edge/hold/repeat state machine pinned to Cores 0,2 feeding poll_keys with 0ms polling lag."""
        # Comment (invariant): Pin KeypadScanner strictly to Cores 0,2 and push validated transitions into _event_queue via call_soon_threadsafe so fast <20ms taps on R1/R2/C1 never vanish between poll sleeps and GPIO ioctls never touch Core 1 or Core 3.
        try:
            os.sched_setaffinity(0, {0, 2})
        except Exception:
            pass

        while self.is_running:
            raw_key = self.scan_matrix_sync()
            self._raw_active_key = raw_key
            now = time.monotonic()

            if raw_key is not None:
                self._release_miss_count = 0
                if raw_key != self.current_key:
                    if self.current_key is not None:
                        if not self.hold_emitted:
                            self._emit_event(('CLICK', self.current_key))
                        self._emit_event(('UP', self.current_key))
                    self.current_key = raw_key
                    self.press_time = now
                    self.hold_emitted = False
                    self.last_repeat_time = now
                    self.current_repeat_delay = 0.15
                    self._emit_event(('DOWN', raw_key))
                else:
                    duration = now - self.press_time
                    if duration >= self.hold_threshold and not self.hold_emitted:
                        self.hold_emitted = True
                        self._emit_event(('HOLD', raw_key))
                    if duration >= 0.25:
                        elapsed = now - self.last_repeat_time
                        if elapsed >= self.current_repeat_delay:
                            ticks = min(4, max(1, int(elapsed / self.current_repeat_delay)))
                            for _ in range(ticks):
                                self._emit_event(('REPEAT', raw_key))
                            self.last_repeat_time = time.monotonic()
                            self.current_repeat_delay = max(0.020, self.current_repeat_delay * 0.7)
            else:
                if self.current_key is not None:
                    if self._release_miss_count < self.release_debounce_sweeps:
                        self._release_miss_count += 1
                    else:
                        released = self.current_key
                        was_hold = self.hold_emitted
                        self.current_key = None
                        self.hold_emitted = False
                        self._release_miss_count = 0
                        if not was_hold:
                            self._emit_event(('CLICK', released))
                        self._emit_event(('UP', released))

            sleep_time = self.master_poll_interval
            if hasattr(self, 'ui_manager') and getattr(self.ui_manager, 'is_screen_off', False) and self.current_key is None:
                sleep_time = 0.030  # ~33Hz low-power wake scan (<36ms '*' wake response)
            time.sleep(sleep_time)

    async def poll_keys(self, callback):
        """Awaits hardware key transitions directly from _event_queue with zero polling sleep lag."""
        self._loop = asyncio.get_running_loop()
        q = asyncio.Queue()
        self._event_queue = q
        try:
            while self.is_running:
                evt = await q.get()
                if evt is None:
                    break
                if evt[0] == 'REPEAT' and q.qsize() > 2:
                    continue
                await callback(evt)
                if evt[0] in ('REPEAT', 'HOLD'):
                    self.last_repeat_time = time.monotonic()
        except asyncio.CancelledError:
            return
        except Exception as e:
            if "RequestReleasedError" in type(e).__name__:
                return
            raise e

    def cleanup(self):
        self.is_running = False
        self._emit_event(None)
        if self.request:
            self.request.release()
