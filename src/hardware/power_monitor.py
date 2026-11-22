# /vipxpert_minimal_os/src/hardware/power_monitor.py
# /vipxpert_minimal_os/src/hardware/power_monitor.py
import asyncio
import os
import threading
from collections import deque
from ina219 import INA219

class PowerMonitor:
    """Background daemon tracking Waveshare UPS HAT telemetry via I2C."""
    def __init__(self, ui_manager, busnum=None, address=0x43):
        self.ui = ui_manager

        # Auto-hunt for the correct I2C bus (Prioritize 0 on Banana Pi M4 Zero, fallback to any available)
        if busnum is None:
            try:
                active_buses = [int(f.split('-')[1]) for f in os.listdir('/dev') if f.startswith('i2c-')]
                if 0 in active_buses:
                    self.busnum = 0
                elif active_buses:
                    self.busnum = min(active_buses)
                else:
                    self.busnum = 0
            except Exception:
                self.busnum = 0
        else:
            self.busnum = busnum

        self.address = address
        self.ina = None
        self.is_running = False
        self._wake_event = threading.Event()
        self.scope_active = False
        self.scope_paused = False
        self.scope_history = deque(maxlen=96)
        self.scope_stats = {
            'v_max': 0.0, 'v_min': 99.0,
            'c_min': 0.0, 'c_max': -9999.0,
            'tare_ma': 0.0, 'tare_v': 0.0,
            'r_mohm': 0
        }

        # Stage: Boot Battery Telemetry Hydration (Restores last known battery telemetry from DB so UI never blanks during the 2s boot settle)
        # Comment (invariant): Seed power_state from last_bat_state in SQLite on boot so the battery icon/percent renders immediately before the first 136ms INA219 ADC_128SAMP conversion finishes.
        init_state = {
            'voltage': 0.0,
            'current': 0.0,
            'percent': 0.0,
            'is_charging': False,
            'icon': '[-]',
            'error': True
        }
        try:
            db = getattr(self.ui, 'db', None)
            raw_bat = db.get_setting("last_bat_state", "") if db else ""
            if raw_bat and "|" in raw_bat:
                b_parts = raw_bat.split("|")
                v_c = float(b_parts[0])
                c_c = float(b_parts[1])
                p_c = float(b_parts[2])
                if v_c > 2.5:
                    init_state = {
                        'voltage': v_c,
                        'current': c_c,
                        'percent': p_c,
                        'is_charging': c_c > 0,
                        'icon': '[+]' if c_c > 0 else '[-]',
                        'error': False
                    }
        except Exception:
            pass
        self.ui.power_state = init_state
        self.ui.power_monitor = self

    def notify_screen_wake(self):
        """Wakes the 0Hz sleeping monitor thread immediately when the OLED screen turns on."""
        self._wake_event.set()

    def set_scope_mode(self, enabled: bool):
        """Toggles 15Hz real-time oscilloscope burst sampling on/off and wakes the monitor thread."""
        new_state = bool(enabled)
        if self.scope_active != new_state:
            self.scope_active = new_state
            if not new_state:
                self.scope_paused = False
            self._wake_event.set()

    def reset_scope_stats(self):
        """Tares the current mA/V reading as the baseline for peripheral spike & cable droop auditing."""
        ps = getattr(self.ui, 'power_state', {})
        v_now = float(ps.get('voltage', 0.0))
        c_now = float(ps.get('current', 0.0))
        self.scope_stats.update({
            'v_max': v_now, 'v_min': v_now if v_now > 0 else 99.0,
            'c_min': c_now, 'c_max': c_now,
            'tare_ma': c_now, 'tare_v': v_now,
            'r_mohm': 0
        })
        self.scope_history.clear()

    async def start(self):
        self.is_running = True
        import threading
        
        # Fire and forget the dedicated background thread
        self._hardware_thread = threading.Thread(
            target=self._hardware_monitor_loop,
            daemon=True,
            name="PowerMonitor"
        )
        self._hardware_thread.start()

    def _hardware_monitor_loop(self):
        """Permanent background thread isolated from asyncio."""
        import time
        from src.hardware.display import I2C_BUS_LOCK
        time.sleep(2.0)
        disp = getattr(self.ui, 'display', None)
        from smbus2 import SMBus, i2c_msg

        # Stage: Pure STOP-Separated SMBus INA219 Driver (Eliminates Adafruit_PureIO Repeated-START Errno 11 collisions on Allwinner H618 TWI0)
        # Comment (hardware-quirk): pi-ina219 executes unpatched Repeated-START I2C reads inside INA219.__init__(), causing Errno 11 on H618 mv64xxx_i2c whenever OLED_Compositor is active; direct STOP-separated smbus2 transfers with Errno-11 retries avoid all Repeated-START faults.
        class _SMBusINA219:
            RANGE_16V = 0
            ADC_16SAMP = 0x0C
            ADC_128SAMP = 0x0F

            def __init__(self, bus_id, addr):
                self.bus_id = bus_id
                self.addr = addr
                self._cfg_word = 0x399F

            def _xfer(self, fn):
                # Comment (invariant): Never retry or sleep inside _xfer while _run_100k_locked holds I2C_BUS_LOCK; 6 retries x kernel mv64xxx_i2c Ctlr Error + 3ms sleep held I2C_BUS_LOCK for 118..130ms every 1.77s, causing severe visible stutter across all visualizers and scenes (P-484). Keep a single <1.5ms attempt per poll cycle.
                with SMBus(self.bus_id) as _b:
                    return fn(_b)

            def _write_u16(self, reg, val):
                hi, lo = (val >> 8) & 0xFF, val & 0xFF
                self._xfer(lambda _b: _b.i2c_rdwr(i2c_msg.write(self.addr, bytes([reg & 0xFF, hi, lo]))))

            def _read_u16(self, reg):
                def _op(_b):
                    _b.i2c_rdwr(i2c_msg.write(self.addr, bytes([reg & 0xFF])))
                    time.sleep(0.0003)
                    rm = i2c_msg.read(self.addr, 2)
                    _b.i2c_rdwr(rm)
                    d = bytes(rm)
                    return (d[0] << 8) | d[1]
                return self._xfer(_op)

            def configure(self, voltage_range=0, bus_adc=0x0F, shunt_adc=0x0F):
                # 16V range (bit 13 = 0), PGA /8 +/-320mV (bits 12:11 = 0b11), continuous shunt+bus (0b111)
                self._cfg_word = ((voltage_range & 0x01) << 13) | (0x03 << 11) | ((bus_adc & 0x0F) << 7) | ((shunt_adc & 0x0F) << 3) | 0x07
                self._write_u16(0x05, 0x1000)
                self._write_u16(0x00, self._cfg_word)

            def wake(self):
                self._write_u16(0x00, self._cfg_word)

            def sleep(self):
                self._write_u16(0x00, self._cfg_word & ~0x07)

            def voltage(self):
                raw = self._read_u16(0x02)
                return ((raw >> 3) & 0x1FFF) * 0.004

            def current(self):
                raw = self._read_u16(0x01)
                if raw > 32767:
                    raw -= 65536
                # 10uV/LSB across 0.1 ohm shunt = 0.1mA/LSB
                return raw * 0.1

        import struct as _st

        # Stage: Sub-Millisecond 100kHz-Scoped I2C0 Transaction Helper
        # Comment (invariant): Never hold I2C_BUS_LOCK across INA219's 145ms ADC_128SAMP conversion sleep (froze OLED_Compositor for 179ms in DPR-372), and never rely on shm[12:16] pause writes (Display.show() overwrites shm[0:20] every UI frame in DPR-361). Always step TWI0_CCR down to 0x59 (100kHz) and back to _overdrive_ccr (0x90) inside <1.2ms I2C_BUS_LOCK bursts.
        def _run_100k_locked(fn):
            d_ref = getattr(self.ui, 'display', None)
            with I2C_BUS_LOCK:
                if d_ref and hasattr(d_ref, '_write_twi0_ccr'):
                    d_ref._write_twi0_ccr(0x59)
                    time.sleep(0.0004)
                try:
                    return fn()
                finally:
                    if d_ref and hasattr(d_ref, '_write_twi0_ccr'):
                        d_ref._write_twi0_ccr(getattr(d_ref, '_overdrive_ccr', 0x90))

        try:
            self.ina = _SMBusINA219(self.busnum, self.address)
            # Use 128-sample hardware DSP averaging (68ms window) to eliminate 1.88kHz audio-band ADC switching noise on the 3.3V rail
            _run_100k_locked(lambda: self.ina.configure(
                voltage_range=self.ina.RANGE_16V,
                bus_adc=self.ina.ADC_128SAMP,
                shunt_adc=self.ina.ADC_128SAMP
            ))
            # Comment (hardware-quirk): INA219 ADC_128SAMP requires 136.2ms (68.1ms bus + 68.1ms shunt) for its first conversion; waiting 145ms prevents reading 0.00V on boot.
            time.sleep(0.145)
            v0, c0 = _run_100k_locked(lambda: (self.ina.voltage(), self.ina.current()))
            if v0 > 2.5:
                pct0 = max(0.0, min(100.0, (v0 - 3.2) / (4.2 - 3.2) * 100.0))
                self.ui.power_state.update({
                    'voltage': v0,
                    'current': c0,
                    'percent': pct0,
                    'is_charging': c0 > 0,
                    'icon': '[+]' if c0 > 0 else '[-]',
                    'error': False
                })
                try:
                    db = getattr(self.ui, 'db', None)
                    if db:
                        db.set_setting("last_bat_state", f"{v0:.3f}|{c0:.1f}|{pct0:.2f}")
                except Exception:
                    pass
                self.ui.request_render()
        except Exception as e:
            print(f"[Power Monitor] Hardware init deferred ({e}); will retry in loop.")
            self.ina = None

        v_hist = []
        c_hist = []
        was_screen_off = False
        in_fast_adc = False
        fail_streak = 0

        while self.is_running:
            # Complete 0Hz idle while OLED screen is off: force scope off and sleep until screen wakes
            if getattr(self.ui, 'is_screen_off', False):
                self.scope_active = False
                if in_fast_adc and getattr(self, 'ina', None) is not None:
                    try:
                        _run_100k_locked(lambda: (
                            self.ina.configure(voltage_range=self.ina.RANGE_16V, bus_adc=self.ina.ADC_128SAMP, shunt_adc=self.ina.ADC_128SAMP),
                            self.ina.sleep()
                        ))
                    except Exception:
                        pass
                    in_fast_adc = False
                was_screen_off = True
                self._wake_event.clear()
                self._wake_event.wait(timeout=1.0)
                continue

            # Stage: Screen Wake Recovery (Wakes INA219 from power-down without wiping prior valid history)
            need_wake = was_screen_off
            if was_screen_off:
                was_screen_off = False

            try:
                if self.scope_active:
                    if getattr(self, 'ina', None) is None:
                        self.ina = _SMBusINA219(self.busnum, self.address)
                        need_wake = True
                    if not in_fast_adc or need_wake:
                        _run_100k_locked(lambda: (
                            self.ina.wake(),
                            self.ina.configure(voltage_range=self.ina.RANGE_16V, bus_adc=self.ina.ADC_16SAMP, shunt_adc=self.ina.ADC_16SAMP)
                        ))
                        in_fast_adc = True
                        if not self.scope_history:
                            self.reset_scope_stats()
                    voltage, current = _run_100k_locked(lambda: (self.ina.voltage(), self.ina.current()))
                    avg_v, avg_c = voltage, current
                    if not self.scope_paused:
                        self.scope_history.append((voltage, current))
                        st = self.scope_stats
                        if st['tare_v'] == 0.0:
                            st['tare_v'], st['tare_ma'] = voltage, current
                        st['v_max'] = max(st['v_max'], voltage)
                        st['v_min'] = min(st['v_min'], voltage)
                        st['c_max'] = max(st['c_max'], current)
                        st['c_min'] = min(st['c_min'], current)
                        di = abs(st['c_max'] - st['c_min'])
                        dv = abs(st['v_max'] - st['v_min'])
                        if di >= 40.0:
                            st['r_mohm'] = int(min(9999, (dv / (di / 1000.0)) * 1000.0))
                else:
                    disp = getattr(self.ui, 'display', None)
                    shm = getattr(disp, '_shm', None) if disp else None
                    # Stage: Full-Screen MV I2C0 Priority Guard (Defers 15s battery poll whenever 155.2Hz full-screen video PWM is active)
                    if shm is not None:
                        if _st.unpack("<I", shm[8:12])[0] == 2:
                            self._wake_event.clear()
                            self._wake_event.wait(timeout=5.0)
                            continue
                    # Comment (workaround): Re-create _SMBusINA219 lazily if boot probe collided on I2C0 so battery monitoring self-heals without a service restart.
                    if getattr(self, 'ina', None) is None:
                        self.ina = _SMBusINA219(self.busnum, self.address)
                        _run_100k_locked(lambda: self.ina.configure(
                            voltage_range=self.ina.RANGE_16V,
                            bus_adc=self.ina.ADC_128SAMP,
                            shunt_adc=self.ina.ADC_128SAMP
                        ))
                        in_fast_adc = False
                        time.sleep(0.145)
                    elif need_wake or in_fast_adc:
                        was_fast = in_fast_adc
                        _run_100k_locked(lambda: (
                            self.ina.wake() if need_wake else None,
                            self.ina.configure(voltage_range=self.ina.RANGE_16V, bus_adc=self.ina.ADC_128SAMP, shunt_adc=self.ina.ADC_128SAMP) if was_fast else None
                        ))
                        in_fast_adc = False
                        if was_fast:
                            time.sleep(0.145)
                    voltage, current = _run_100k_locked(lambda: (self.ina.voltage(), self.ina.current()))
                    v_hist.append(voltage)
                    c_hist.append(current)
                    if len(v_hist) > 3:
                        v_hist.pop(0)
                        c_hist.pop(0)
                    avg_v = sum(v_hist) / len(v_hist)
                    avg_c = sum(c_hist) / len(c_hist)

                if avg_v > 2.5:
                    pct = max(0.0, min(100.0, (avg_v - 3.2) / (4.2 - 3.2) * 100))
                    is_charging = avg_c > 0
                    fail_streak = 0
                    prev_ps = getattr(self.ui, 'power_state', {})
                    was_valid = not prev_ps.get('error', True)
                    was_chg = bool(prev_ps.get('is_charging', False))
                    sfx = getattr(self.ui, 'sound_manager', None)
                    if sfx and was_valid:
                        if is_charging and not was_chg:
                            sfx.play("charge_start", cooldown_s=4.0)
                        if is_charging or pct >= 20.0:
                            self._low_bat_stage = 0
                        elif pct <= 5.0 and getattr(self, '_low_bat_stage', 0) < 2:
                            self._low_bat_stage = 2
                            sfx.play("battery_low", cooldown_s=30.0)
                        elif pct <= 15.0 and getattr(self, '_low_bat_stage', 0) < 1:
                            self._low_bat_stage = 1
                            sfx.play("battery_low", cooldown_s=30.0)
                    self.ui.power_state.update({
                        'voltage': avg_v,
                        'current': avg_c,
                        'percent': pct,
                        'is_charging': is_charging,
                        'icon': '[+]' if is_charging else '[-]',
                        'error': False
                    })
                    try:
                        if not self.scope_active and getattr(self.ui, 'db', None):
                            self.ui.db.set_setting("last_bat_state", f"{avg_v:.3f}|{avg_c:.1f}|{pct:.2f}")
                    except Exception:
                        pass
                self.ui.request_render()
            except Exception as e:
                old_ina = getattr(self, 'ina', None)
                self.ina = None
                if old_ina is not None and hasattr(old_ina, '_bus'):
                    try:
                        old_ina._bus.close()
                    except Exception:
                        pass
                fail_streak += 1
                if not v_hist and fail_streak >= 2:
                    self.ui.power_state['error'] = True
                # Comment (invariant): NEVER write to /sys/bus/platform/drivers/mv64xxx_i2c/unbind while OLED_Compositor or Display holds /dev/i2c-0 open; i2c_del_adapter() enters uninterruptible kernel sleep (st=D) inside I2C_BUS_LOCK, permanently freezing the OS and blocking SIGKILL on service restart (R-454). Pulse TWI0_SRST (0x05002000+0x1C = 0x01) via /dev/mem in <0.3ms instead, and back off up to 60s even when v_hist is empty so 0x43 NACKs never cause a 1.5s stutter storm (P-484).
                try:
                    import mmap as _mmap
                    disp = getattr(self.ui, 'display', None)
                    with I2C_BUS_LOCK:
                        fd_twi = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
                        try:
                            with _mmap.mmap(fd_twi, 4096, _mmap.MAP_SHARED, _mmap.PROT_READ | _mmap.PROT_WRITE, offset=0x05002000) as twi:
                                twi[0x1C:0x20] = _st.pack("<I", 0x01)
                                time.sleep(0.0002)
                                ccr_restore = getattr(disp, '_overdrive_ccr', 0x90) if disp else 0x90
                                twi[0x14:0x18] = _st.pack("<I", ccr_restore)
                        finally:
                            os.close(fd_twi)
                except Exception:
                    pass

            self._wake_event.clear()
            # Comment (invariant): When INA219 0x43 fails 3 consecutive probes (e.g. UPS telemetry disconnected or bus arbitration fault), park background polling until manual scope/wake instead of retrying every 60s; every failed 0x43 probe leaves mv64xxx_i2c in status 0x38 and stalls the next 0x3c OLED flush by ~87ms (DHP-513).
            if self.scope_active:
                wait_s = 0.066 if fail_streak == 0 else min(5.0, 0.5 * fail_streak)
            elif fail_streak >= 3:
                while self.is_running and not self.scope_active:
                    self._wake_event.wait(timeout=3600.0)
                    self._wake_event.clear()
                    if self.scope_active:
                        fail_streak = 0
                        break
                continue
            elif fail_streak > 0:
                wait_s = min(30.0, 5.0 * fail_streak)
            else:
                wait_s = 15.0
            self._wake_event.wait(timeout=wait_s)

    def stop(self):
        self.is_running = False
