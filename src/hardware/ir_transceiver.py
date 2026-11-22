# /vipxpert_minimal_os/src/hardware/ir_transceiver.py
import asyncio
import os
import glob
import time
import subprocess

class IRTransceiver:
    """
    Hardware abstraction for the Infrared subsystem.
    Interfaces with the modern Linux 'ir-ctl' (v4l-utils) toolkit to send
    and receive pulse-space timing waveforms via the kernel LIRC drivers.
    """
    def __init__(self):
        self.rx_device = None
        self.tx_device = None
        self._probe_hardware()

    def _probe_hardware(self):
        """Dynamically scans /dev/lirc* features via subprocess to assign correct tx/rx nodes."""
        import os
        import subprocess
        
        self.tx_device = None
        self.rx_device = None
        
        try:
            # Grab all lirc nodes, sorted so it's deterministic
            lirc_nodes = sorted([f"/dev/{d}" for d in os.listdir('/dev') if d.startswith('lirc')])
        except FileNotFoundError:
            print("[IR Hardware] WARNING: No /dev/lirc devices found.")
            return
            
        for node in lirc_nodes:
            try:
                # Run ir-ctl feature probe
                result = subprocess.run(['ir-ctl', '-d', node, '-f'], capture_output=True, text=True, check=True)
                output = result.stdout
                
                if "Device can send raw IR" in output:
                    print(f"[IR Hardware] Found Transmitter on {node}")
                    self.tx_device = node
                
                if "Device can receive raw IR" in output:
                    print(f"[IR Hardware] Found Receiver on {node}")
                    self.rx_device = node
                    
            except Exception as e:
                print(f"[IR Hardware] Failed to probe {node}: {e}")

        if not self.tx_device:
            print("[IR Hardware] WARNING: No IR Transmitter found.")
        if not self.rx_device:
            print("[IR Hardware] WARNING: No IR Receiver found.")

    def is_ready(self) -> bool:
        return bool(self.tx_device and self.rx_device)

    @staticmethod
    def parse_pulses(raw_str: str):
        """Parses ir-ctl output lines (+9000 -4500 or pulse 9000 / space 4500) into signed microsecond ints (+mark, -space)."""
        out = []
        toks = raw_str.replace("pulse ", "+").replace("space ", "-").split()
        for t in toks:
            if (t.startswith("+") or t.startswith("-")) and t[1:].isdigit():
                val = int(t[1:])
                out.append(val if t[0] == "+" else -val)
        return out

    @classmethod
    def decode_protocol(cls, pulses: list) -> str:
        """Automatically decodes NEC, Samsung, Sony SIRC, Philips RC5, or RC6 hex addresses from signed us timings."""
        pairs = []
        i = 0
        while i < len(pulses):
            if pulses[i] > 0:
                mk = pulses[i]
                sp = abs(pulses[i + 1]) if (i + 1 < len(pulses) and pulses[i + 1] < 0) else 25000
                pairs.append((mk, sp))
                i += 2
            else:
                i += 1
        if not pairs:
            return "WAITING..."

        h_mk, h_sp = pairs[0]
        # 1. NEC Repeat Frame (~9000us mark, ~2250us space)
        if 7500 <= h_mk <= 10500 and 1700 <= h_sp <= 2800 and len(pairs) <= 3:
            return "NEC REPEAT"

        # 2. NEC / NEC-Ext (~9000us mark, ~4500us space, 32 bits)
        if 7500 <= h_mk <= 10500 and 3600 <= h_sp <= 5400 and len(pairs) >= 33:
            bits = 0
            for idx in range(1, 33):
                _, sp = pairs[idx]
                if sp > 1100:
                    bits |= (1 << (idx - 1))
            addr = bits & 0xFFFF
            cmd = (bits >> 16) & 0xFF
            return f"NEC {addr:04X}:{cmd:02X}"

        # 3. Samsung (~4500us mark, ~4500us space, 32 bits)
        if 3600 <= h_mk <= 5400 and 3600 <= h_sp <= 5400 and len(pairs) >= 33:
            bits = 0
            for idx in range(1, 33):
                _, sp = pairs[idx]
                if sp > 1100:
                    bits |= (1 << (idx - 1))
            addr = bits & 0xFFFF
            cmd = (bits >> 16) & 0xFF
            return f"SAM {addr:04X}:{cmd:02X}"

        # 4. Sony SIRC (~2400us mark, ~600us space, 12/15/20 pulse-width bits)
        if 1900 <= h_mk <= 2900 and 350 <= h_sp <= 950 and len(pairs) >= 12:
            nbits = min(20, len(pairs) - 1)
            bits = 0
            for idx in range(1, nbits + 1):
                mk, _ = pairs[idx]
                if mk > 900:
                    bits |= (1 << (idx - 1))
            cmd = bits & 0x7F
            addr = bits >> 7
            return f"SIRC {addr:02X}:{cmd:02X} ({nbits}b)"

        # 5. Philips RC6 (~2666us leader mark, ~889us space)
        if 2100 <= h_mk <= 3200 and 600 <= h_sp <= 1250 and len(pairs) >= 10:
            sig = 0
            for idx, (mk, sp) in enumerate(pairs[1:17]):
                if mk > 650 or sp > 650:
                    sig |= (1 << idx)
            return f"RC6 0x{sig:04X} ({len(pairs)}p)"

        # 6. Philips RC5 (Manchester ~889us / ~1778us marks & spaces)
        if 600 <= h_mk <= 1950 and len(pairs) >= 6 and all(450 <= m <= 2200 for m, _ in pairs[:6]):
            sig = 0
            for idx, (mk, sp) in enumerate(pairs[:14]):
                if mk > 1300 or sp > 1300:
                    sig |= (1 << idx)
            return f"RC5 0x{sig:04X} ({len(pairs)}p)"

        tot_us = sum(m + min(s, 10000) for m, s in pairs)
        return f"RAW {len(pairs)}p ({tot_us/1000.0:.1f}ms)"
        
    def _fuzzy_match_signals(self, sig1: str, sig2: str, tolerance: float = 0.20) -> bool:
        """Compares two IR strings to see if their pulse counts match and durations are within 20%."""
        tokens1 = sig1.split()
        tokens2 = sig2.split()
        
        # Absolute requirement: Must have the exact same number of pulses
        if len(tokens1) != len(tokens2):
            return False
            
        for t1, t2 in zip(tokens1, tokens2):
            try:
                # Strip the +/- symbols and compare the raw microsecond durations
                val1 = int(t1[1:])
                val2 = int(t2[1:])
                
                # If the difference is greater than 20% of the larger value, they don't match
                max_val = max(val1, val2)
                diff = abs(val1 - val2)
                if diff > (max_val * tolerance):
                    return False
            except ValueError:
                return False
                
        return True

    async def receive_raw(self, timeout: int = 5, cancel_event: asyncio.Event = None) -> str | None:
        """Records incoming IR pulses by streaming real-time density windows."""
        if not self.is_ready():
            self._probe_hardware()
            
        # Lock to a local variable to satisfy strict type-checkers
        rx_node = self.rx_device
        if not rx_node:
            return None
            
        try:
            # Direct executable invocation prevents orphaned background shell processes
            process = await asyncio.create_subprocess_exec(
                "stdbuf", "-o0", "ir-ctl", "-d", rx_node, "-r",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            captured_lines = []
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                if cancel_event and cancel_event.is_set():
                    try:
                        process.kill()
                        await process.wait()
                    except Exception: pass
                    return "CANCELLED"
                    
                try:
                    if process.stdout is None:
                        break
                    read_task = process.stdout.readline()
                    line_bytes = await asyncio.wait_for(read_task, timeout=0.200)
                    
                    if not line_bytes:
                        # Stream ended. Did it crash?
                        await process.wait()
                        if process.returncode != 0 and process.stderr:
                            err = await process.stderr.read()
                            raise Exception(err.decode().strip() or "Device busy or rejected")
                        break
                        
                    line = line_bytes.decode().strip()
                    if line:
                        captured_lines.append(line)
                        
                except asyncio.TimeoutError:
                    # 200ms of silence detected. Run the Payload Isolator!
                    all_tokens = " ".join(captured_lines).split()
                    bursts = []
                    current_burst = []
                    
                    for token in all_tokens:
                        current_burst.append(token)
                        if token.startswith("-"):
                            try:
                                if int(token[1:]) > 20000:
                                    bursts.append(current_burst)
                                    current_burst = []
                            except ValueError: pass
                    if current_burst:
                        bursts.append(current_burst)

                    # Scan for a valid remote frame
                    for burst in bursts:
                        valid_pulses = sum(1 for t in burst if t.startswith("+") and t[1:].isdigit() and int(t[1:]) >= 200)
                        
                        if valid_pulses >= 15:
                            # Clean up the hardware lock immediately and return the string
                            try:
                                process.kill()
                                await process.wait()
                            except Exception: pass
                            isolated_string = " ".join(burst)
                            print(f"[IR Engine] Signal isolated successfully! ({len(burst)} tokens).", flush=True)
                            return isolated_string
                            
                    # Only noise found so far. Clear buffer and keep listening.
                    captured_lines.clear()
            
            # Timeout reached without success
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            return None
                
        except Exception as e:
            print(f"[IR Hardware Fault] Stream tracking failed: {e}", flush=True)
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            raise e  # Bubble this back up to the UI Toast!
        return None

    async def send_raw(self, pulse_string: str) -> bool:
        """Blasts raw pulse-space strings out of the transmitter node."""
        if not self.is_ready():
            self._probe_hardware()
            
        # Lock to a local variable to satisfy strict type-checkers
        tx_node = self.tx_device
        if not tx_node or not pulse_string:
            return False
            
        # --- Translate math symbols to lirc syntax ---
        formatted_lines = []
        for token in pulse_string.split():
            if token.startswith('+'):
                formatted_lines.append(f"pulse {token[1:]}")
            elif token.startswith('-'):
                formatted_lines.append(f"space {token[1:]}")
        lirc_payload = "\n".join(formatted_lines) + "\n"
            
        try:
            # Some ir-ctl versions reject '-' for stdin. 
            # We dump to a RAM-backed temp file to guarantee execution.
            tmp_file = "/tmp/ir_payload.txt"
            with open(tmp_file, "w") as f:
                f.write(lirc_payload)

            # Direct executable invocation prevents orphaned shells
            # Explicitly force 38kHz carrier so the TV receiver doesn't filter it as noise
            process = await asyncio.create_subprocess_exec(
                "ir-ctl", "-d", tx_node, "--carrier=38000", "-s", tmp_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            _, stderr = await process.communicate()
            
            if process.returncode != 0:
                err_text = stderr.decode().strip()
                raise Exception(f"ir-ctl: {err_text}" if err_text else "ir-ctl process failed")
                
            return True
        except Exception as e:
            # Bubble this up to the UI so it doesn't fail silently
            raise e

    async def stream_raw_continuous(self):
        """Continuously yields raw IR pulses for real-time oscilloscope tracking."""
        if not self.is_ready():
            self._probe_hardware()
            
        rx_node = self.rx_device
        if not rx_node:
            yield "ERROR: No RX Device"
            return
            
        process = await asyncio.create_subprocess_exec(
            "stdbuf", "-o0", "ir-ctl", "-d", rx_node, "-r",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            while True:
                if process.stdout is None:
                    break
                try:
                    read_task = process.stdout.readline()
                    # 150ms timeout precisely defines the end of an IR burst
                    line_bytes = await asyncio.wait_for(read_task, timeout=0.150)
                    if not line_bytes:
                        break
                    line = line_bytes.decode().strip()
                    if line:
                        yield line
                except asyncio.TimeoutError:
                    yield "SILENCE"
        finally:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass