# /vipxpert_minimal_os/src/ui/ui_manager.py
import asyncio
import time
from typing import Optional
from src.ui.base_app import BaseApp

class UIManager:
    """
    Central UI state router. Manages app lifecycles, active display focus,
    capacitive auto-timeout locking, and the 2-second system telemetry tick.
    """
    def __init__(self, display, db, timeout_seconds: int = 30):
        self.display = display
        self.db = db
        # Load user settings with fallback defaults
        self.timeout_seconds = int(self.db.get_setting("timeout_lock", str(timeout_seconds)))
        self.marquee_speed = float(self.db.get_setting("marquee_speed", "3.0"))
        self.current_app = None

        # Global hardware context variables
        self.active_hardware_dac = None
        self.power_state = {'voltage': 0.0, 'current': 0.0, 'percent': 0.0, 'is_charging': False, 'icon': '[-]', 'error': True}

        # App Routing & Memory Cache
        self.current_app_id = None
        self.previous_app_id = None
        self.active_apps = {}  # Cache of suspended apps: {app_id: app_instance}
        self.app_registry = {} # Master OS Manifest: {app_id: class_factory}

        # Global Overlay & Universal Compositor Toast State (Single-source toast across all apps & scenes)
        self.active_overlay = None
        self.pending_overlays = []
        self._toast_message = None
        self._toast_expire_ts = 0.0
        self._toast_birth_ts = 0.0
        self._toast_blocking = False
        self.hw_profile = {"tier": "full", "cores": 4, "dsp_cores": "0,2"}

        # Compositor V-Sync State (Apply hardware I2C0 clock profile & marquee speed immediately on boot)
        self.needs_render = True
        self.last_render_time = 0.0
        oled_fps = max(10, min(120, int(self.db.get_setting("oled_fps", "20"))))
        self.frame_interval = 1.0 / oled_fps
        if hasattr(self.display, "apply_i2c_clock_profile"):
            self.display.apply_i2c_clock_profile(oled_fps)
        self.display.contrast_level = int(self.db.get_setting("brightness", "128"))
        if hasattr(self.display, "apply_oled_drive_profile"):
            pwm_m = self.db.get_setting("oled_pwm_mode", "max")
            drv_m = self.db.get_setting("oled_drive_mode", "normal")
            self.display.apply_oled_drive_profile(pwm_m, drv_m)
        self.display.dither_mode = self.db.get_setting("oled_dither_mode", "off")
        try:
            import src.ui.components as ui_components
            ui_components.DEFAULT_MARQUEE_SPEED = self.marquee_speed
        except Exception:
            pass

        self.last_activity = time.time()

        # Stream ID Caches & Exclusive Audio State
        self.mpv_stream_id = None
        self.bt_stream_id = None
        self.i2s_sink_id = None
        self.i2s_source_id = None
        self._mic_sync_task = None
        self._dac_vol_sync_task = None
        self.last_synced_volumes = {}  # Caches volume to prevent wpctl terminal spam
        self.exclusive_audio = self.db.get_setting("exclusive_audio", "False") == "True"
        self.exclusive_volume_float = float(self.db.get_setting("exclusive_volume", "1.0"))
        self.pcm5122_hw_gain_active = False  # True when PCM5122 (0x4c/0x4d) acknowledges I2C register writes
        self.i2s_manually_killed = False     # Session-scoped manual I2S kill state

        # Session-Scoped Transparency & Mic Feed State (Always resets to OFF / Board Mics on reboot)
        self.transparency_enabled = False
        self.mic_feed_mode = "mic"           # 'mic' (Board Mics), 'music' (Music Loop), 'mix' (Mic + Music)
        self._transp_proc = None
        self._feed_music_proc = None
        self._feed_mic_proc = None
        self._mix_vol_sync_task = None
        self._transp_sync_task = None
        self._mix_lock = asyncio.Lock()
        self._transp_lock = asyncio.Lock()

        # Restore lock state from DB on boot
        if self.db.get_setting("is_locked", "True") == "True":
            self.current_app_id = 'lock_screen'

    def poke_activity(self):
        """Resets the inactivity timer on any user hardware interaction."""
        self.last_activity = time.time()

    def show_system_overlay(self, overlay_app):
        """Queues or immediately displays a global system overlay (like Bluetooth pairing)."""
        if self.current_app_id == 'lock_screen':
            self.pending_overlays.append(overlay_app)
        else:
            self.active_overlay = overlay_app

    def _process_pending_overlays(self):
        """Pops the next pending overlay into the active slot if available."""
        if self.pending_overlays and not self.active_overlay:
            self.active_overlay = self.pending_overlays.pop(0)

    async def _ensure_stream_ids(self, force_i2s: bool = False):
        """Lazy-loads PipeWire node IDs directly from the UI thread asynchronously (context-aware to prevent 15s pw-dump hiccups)."""
        import json, asyncio, time
        now = time.monotonic()
        have_hw = bool(self.i2s_sink_id and self.i2s_source_id)
        if not force_i2s and have_hw:
            bt_active = bool(
                getattr(self, 'bt_daemon', None)
                and self.bt_daemon.media_state.get('transport_state') == 'active'
            )
            mpv_active = bool(
                getattr(self, 'media_arbiter', None)
                and getattr(self.media_arbiter, 'local_audio', None)
                and self.media_arbiter.local_audio.state.get('status') == 'playing'
            )
            need_bt = bt_active and not self.bt_stream_id
            need_mpv = mpv_active and not self.mpv_stream_id
            if not need_bt and not need_mpv:
                return
            if now - getattr(self, '_last_stream_hunt_ts', 0.0) < 5.0:
                return
        self._last_stream_hunt_ts = now

        try:
            process = await asyncio.create_subprocess_shell(
                'runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-dump',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await process.communicate()
            if not stdout:
                return

            nodes = json.loads(stdout.decode('utf-8'))
            for node in nodes:
                if node.get('type') == 'PipeWire:Interface:Node':
                    props = node.get('info', {}).get('props', {})
                    node_name = props.get('node.name', '')
                    media_class = props.get('media.class', '')
                    if not self.mpv_stream_id and props.get('application.name') == 'mpv':
                        self.mpv_stream_id = str(node['id'])
                        print(f"[UIManager] Discovered MPV stream: {self.mpv_stream_id}")
                    elif not self.bt_stream_id and node_name.startswith('bluez_input'):
                        self.bt_stream_id = str(node['id'])
                        print(f"[UIManager] Discovered BlueZ stream: {self.bt_stream_id}")
                    elif node_name.startswith('alsa_output.platform-soc_ahub0') and media_class == 'Audio/Sink':
                        self.i2s_sink_id = str(node['id'])
                    elif node_name.startswith('alsa_input.platform-soc_ahub0') and media_class == 'Audio/Source':
                        self.i2s_source_id = str(node['id'])
        except Exception as e:
            print(f"[UIManager] WARNING: Stream ID hunt failed: {e}")

    async def sync_i2s_dac_channels(self):
        """
        Applies L/R channel swap strictly to the internal I2S DAC hardware register (I2S0_TX0CHMAP0 +0x234)
        while keeping PipeWire alsa_output at [ FL, FR ] so active streams never unlink or hiccup.
        """
        import asyncio
        kernel = getattr(self, 'os_kernel', None)
        if kernel and hasattr(kernel, 'enforce_ahub0_24bit_i2s'):
            kernel.enforce_ahub0_24bit_i2s()

        await self._ensure_stream_ids(force_i2s=not bool(self.i2s_sink_id))
        if not self.i2s_sink_id:
            return

        cmd = f"runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-cli s {self.i2s_sink_id} Props '{{ channelMap: [ FL, FR ], mute: false }}'"
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        if proc.returncode != 0:
            self.i2s_sink_id = None

    def trigger_mic_dsp_sync(self):
        """Debounces rapid keypad on_tune ticks so wpctl/pw-cli never floods the CPU."""
        import asyncio
        if self._mic_sync_task and not self._mic_sync_task.done():
            self._mic_sync_task.cancel()
        self._mic_sync_task = asyncio.create_task(self._debounced_mic_sync())

    async def _debounced_mic_sync(self):
        """Executes debounced gain/balance updates without tearing down active PipeWire port links."""
        import asyncio
        try:
            await asyncio.sleep(0.12)
            await self.sync_mic_dsp(relink=False)
        except asyncio.CancelledError:
            pass

    def _get_mic_range_dsp_spec(self, mic_range: str):
        """
        Returns (fg_json_str, fg_disabled_bool, stereo_widen_float) for PipeWire audioconvert,
        honoring the user-configurable 'mic_wind_hpf_hz' cutoff (0=Off, or 40..300Hz in 10Hz steps):
        - 'near':    Bypassed when Wind Cut is Off (0Hz); otherwise applies 4th-order Wind Cut at wind_hz.
        - 'far':     Uses max(100Hz, wind_hz) HPF + 220Hz (-3.5dB) Low-Shelf + 2.5kHz (+4.5dB) Presence boost.
        - 'far_hpf': 4th-order (24dB/oct) cascaded Butterworth Wind-Cut HPF at wind_hz (default 160Hz) + 10kHz LPF.
        - 'far_agc': Same as 'far_hpf' + Stereo-preserving tanhf() soft-knee limiter (widen=-1.0).
        """
        wind_hz = max(0, min(300, int(self.db.get_setting("mic_wind_hpf_hz", "0"))))
        if 0 < wind_hz < 40:
            wind_hz = 40

        if mic_range == "far":
            cutoff = float(max(100, wind_hz))
            fg = (
                '{"nodes":['
                f'{{"type":"builtin","name":"hpf1","label":"bq_highpass","control":{{"Freq":{cutoff:.1f},"Q":0.707}}}},'
                f'{{"type":"builtin","name":"hpf2","label":"bq_highpass","control":{{"Freq":{cutoff:.1f},"Q":0.707}}}},'
                '{"type":"builtin","name":"ls","label":"bq_lowshelf","control":{"Freq":220.0,"Q":0.707,"Gain":-3.5}},'
                '{"type":"builtin","name":"pk","label":"bq_peaking","control":{"Freq":2500.0,"Q":1.0,"Gain":4.5}}'
                '],'
                '"links":['
                '{"output":"hpf1:Out","input":"hpf2:In"},'
                '{"output":"hpf2:Out","input":"ls:In"},'
                '{"output":"ls:Out","input":"pk:In"}'
                '],'
                '"inputs":["hpf1:In"],"outputs":["pk:Out"]}'
            )
            return fg, False, 0.0
        elif mic_range in ("far_hpf", "far_agc"):
            cutoff = float(wind_hz if wind_hz >= 40 else 160)
            fg = (
                '{"nodes":['
                f'{{"type":"builtin","name":"hpf1","label":"bq_highpass","control":{{"Freq":{cutoff:.1f},"Q":0.707}}}},'
                f'{{"type":"builtin","name":"hpf2","label":"bq_highpass","control":{{"Freq":{cutoff:.1f},"Q":0.707}}}},'
                '{"type":"builtin","name":"lpf","label":"bq_lowpass","control":{"Freq":10000.0,"Q":0.707}}'
                '],'
                '"links":['
                '{"output":"hpf1:Out","input":"hpf2:In"},'
                '{"output":"hpf2:Out","input":"lpf:In"}'
                '],'
                '"inputs":["hpf1:In"],"outputs":["lpf:Out"]}'
            )
            widen = -1.0 if mic_range == "far_agc" else 0.0
            return fg, False, widen
        elif wind_hz >= 40:
            cutoff = float(wind_hz)
            fg = (
                '{"nodes":['
                f'{{"type":"builtin","name":"hpf1","label":"bq_highpass","control":{{"Freq":{cutoff:.1f},"Q":0.707}}}},'
                f'{{"type":"builtin","name":"hpf2","label":"bq_highpass","control":{{"Freq":{cutoff:.1f},"Q":0.707}}}}'
                '],'
                '"links":['
                '{"output":"hpf1:Out","input":"hpf2:In"}'
                '],'
                '"inputs":["hpf1:In"],"outputs":["hpf2:Out"]}'
            )
            return fg, False, 0.0
        return "", True, 0.0

    async def sync_mic_dsp(self, relink: bool = True):
        """
        Applies dB gain, per-capsule L/R balance trim, Near/Far filter-graph DSP, L/R capsule swap,
        Stereo vs Mono-L/Mono-R isolation, and Allwinner H618 24-bit I2S slot framing (0x00001f57).
        When relink=False (during < > slider tuning), updates volumes/DSP params only without touching pw-link.
        """
        import asyncio, json
        await self._ensure_stream_ids(force_i2s=not bool(self.i2s_source_id))
        if not self.i2s_source_id:
            return

        # 0. Ensure Allwinner H618 AHUB I2S0 is locked in 24-bit SR mode (0x00001f57) with PI4 Pull-Down
        kernel = getattr(self, 'os_kernel', None)
        if kernel and hasattr(kernel, 'enforce_ahub0_24bit_i2s'):
            kernel.enforce_ahub0_24bit_i2s()

        # 1. Calculate Total dB Preamp (0..130dB; Mic Range is +0dB compensated so toggling never spikes volume)
        base_db = max(0, min(130, int(self.db.get_setting("mic_gain_db", "46"))))
        mic_range = self.db.get_setting("mic_range_mode", "near")
        total_db = min(48, base_db) if mic_range == "far_agc" else base_db

        linear_gain = round(10.0 ** (total_db / 20.0), 4)

        # 2. Only touch alsa_input channelMap when relink=True (boot/mode switch) so PipeWire never resets
        # capture_FL/FR links mid-slider tune. Default swap="0" (Normal) matches hardware 1:1 stereo.
        swap = self.db.get_setting("mic_swap_lr", "0") == "1"
        mode = self.db.get_setting("mic_channel_mode", "stereo")
        bal_steps = max(-60, min(60, int(self.db.get_setting("mic_balance_steps", "39"))))
        bal_db = bal_steps * 0.5
        # Always route Board Mics ('mic') and 'mix' through vip_mic_bus so Bluetooth HFP (bluez_output)
        # receives the exact +46dB preamp gain, L/R balance trim, and 24-bit AHUB framing without
        # double-amplifying alsa_input when Transparency Mode is active.
        mix_active = True
        fg_str, fg_dis, widen = self._get_mic_range_dsp_spec(mic_range)

        if relink:
            # Keep alsa_input at unity [1.0, 1.0] when Transparency/Mix handles scaling, or apply DSP directly when standalone
            src_params = ["audioconvert.filter-graph.0", "", "audioconvert.filter-graph.disable", True, "channelmix.stereo-widen", 0.0] if (mix_active or fg_dis) else [
                "audioconvert.filter-graph.0", fg_str, "audioconvert.filter-graph.disable", False, "channelmix.stereo-widen", widen
            ]
            payload = json.dumps({
                "channelVolumes": [1.0, 1.0],
                "channelMap": ["FL", "FR"],
                "mute": False,
                "params": src_params
            })
            proc = await asyncio.create_subprocess_exec(
                "runuser", "-u", "vipxpert", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000",
                "/usr/local/bin/pw-cli", "s", str(self.i2s_source_id), "Props", payload,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.communicate()
            if proc.returncode != 0:
                self.i2s_source_id = None
                return
        else:
            # Always keep alsa_input filter-graph cleared & disabled when mix_active=True so filters never double-stack!
            src_params = ["audioconvert.filter-graph.0", "", "audioconvert.filter-graph.disable", True, "channelmix.stereo-widen", 0.0] if (mix_active or fg_dis) else [
                "audioconvert.filter-graph.0", fg_str, "audioconvert.filter-graph.disable", False, "channelmix.stereo-widen", widen
            ]
            payload = json.dumps({"params": src_params})
            proc = await asyncio.create_subprocess_exec(
                "runuser", "-u", "vipxpert", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000",
                "/usr/local/bin/pw-cli", "s", str(self.i2s_source_id), "Props", payload,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.communicate()

        # If Transparency or Mic Feed is active, sync its per-channel stream volumes & filter-graph (and links only if relink=True)
        if mix_active:
            await self.sync_mix_stream_volumes(relink=relink)

        # Also ensure any active Bluetooth HFP output stream (bluez_output.*.0) is unmuted at unity (1.0)
        bt_hfp_cmd = (
            "sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 pactl list short sinks | "
            "grep 'bluez_output' | awk '{print $1}' | "
            "xargs -r -I{} sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 pactl set-sink-volume {} 100%"
        )
        p_bt = await asyncio.create_subprocess_shell(bt_hfp_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p_bt.communicate()
        print(f"[UIManager] Synced I2S Mic DSP: +{total_db}dB ({linear_gain}x), bal={bal_db:+.1f}dB, mode={mode}, swap={swap}, relink={relink}", flush=True)

    def trigger_mix_vol_sync(self):
        """Debounces rapid keypad < > ticks on Transparency Vol and Music Feed Vol sliders."""
        import asyncio
        if self._mix_vol_sync_task and not self._mix_vol_sync_task.done():
            self._mix_vol_sync_task.cancel()
        self._mix_vol_sync_task = asyncio.create_task(self._debounced_mix_vol_sync())

    async def _debounced_mix_vol_sync(self):
        import asyncio
        try:
            await asyncio.sleep(0.12)
            await self.sync_mix_stream_volumes()
        except asyncio.CancelledError:
            pass

    async def sync_mix_stream_volumes(self, relink: bool = False):
        """
        Applies live independent stream volumes to active Transparency and Music-to-Mic loopback nodes.
        Serialized by self._mix_lock so rapid slider ticks and mode switches never overlap.
        When relink=False (during < > gain, balance, or range tuning), strictly updates channelVolumes
        on vip_transp_out without touching pw-link ports. Even in Mono L / Mono R modes, Mic Balance
        scales the left/right ear monitoring balance cleanly.
        """
        import asyncio
        if not hasattr(self, '_mix_lock') or self._mix_lock is None:
            self._mix_lock = asyncio.Lock()

        async with self._mix_lock:
            transp_pct = int(self.db.get_setting("transparency_vol_pct", "100"))
            feed_pct = int(self.db.get_setting("mic_feed_vol_pct", "100"))
            transp_scale = round(transp_pct / 100.0, 4)

            base_db = max(0, min(130, int(self.db.get_setting("mic_gain_db", "46"))))
            mic_range = self.db.get_setting("mic_range_mode", "near")
            total_db = min(48, base_db) if mic_range == "far_agc" else base_db
            pure_linear_gain = 10.0 ** (total_db / 20.0)
            linear_gain = pure_linear_gain * transp_scale

            # Support +-60 steps (+-30.0dB in 0.5dB increments).
            # Index 0 (FL) feeds Physical Right Ear/Mic; Index 1 (FR) feeds Physical Left Ear/Mic.
            bal_steps = max(-60, min(60, int(self.db.get_setting("mic_balance_steps", "39"))))
            bal_db = bal_steps * 0.5
            left_scale = (10.0 ** (bal_db / 20.0)) if bal_db < 0 else 1.0
            right_scale = (10.0 ** (-bal_db / 20.0)) if bal_db > 0 else 1.0
            vol_l = round(linear_gain * left_scale, 4)
            vol_r = round(linear_gain * right_scale, 4)
            feed_vol_l = round(pure_linear_gain * left_scale, 4)
            feed_vol_r = round(pure_linear_gain * right_scale, 4)

            swap = self.db.get_setting("mic_swap_lr", "0") == "1"
            mode = self.db.get_setting("mic_channel_mode", "stereo")

            # In Normal (swap=False): Slot 0 (capture_FL) = Physical Right Mic -> input_FL (Right IEM),
            # and Slot 1 (capture_FR) = Physical Left Mic -> input_FR (Left IEM).
            phys_right_port = "capture_FR" if swap else "capture_FL"
            phys_left_port = "capture_FL" if swap else "capture_FR"
            if mode == "left":
                in_fl_src, in_fr_src = phys_left_port, phys_left_port
            elif mode == "right":
                in_fl_src, in_fr_src = phys_right_port, phys_right_port
            else:
                in_fl_src, in_fr_src = phys_right_port, phys_left_port

            fg_str, fg_dis, widen = self._get_mic_range_dsp_spec(mic_range)
            # Keep feed_widen strictly equal to widen (0.0 in Near mode) for a 100% linear passthrough without tanhf() waveshaping
            feed_widen = widen
            relink_flag = "True" if relink else "False"
            fg_dis_flag = "True" if fg_dis else "False"
            cmd = (
                "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 python3 - << 'EOF'\n"
                "import subprocess, json\n"
                f"if {relink_flag}:\n"
                "    SRC = 'alsa_input.platform-soc_ahub0_mach.stereo-fallback'\n"
                f"    want = {{('{in_fl_src}', 'input_FL'), ('{in_fr_src}', 'input_FR')}}\n"
                "    all_pairs = [\n"
                "        ('capture_FL', 'input_FL'), ('capture_FL', 'input_FR'),\n"
                "        ('capture_FR', 'input_FL'), ('capture_FR', 'input_FR'),\n"
                "    ]\n"
                "    for IN in ('vip_transp_in', 'vip_feed_mic_in'):\n"
                "        for s_p, i_p in all_pairs:\n"
                "            if (s_p, i_p) not in want:\n"
                "                subprocess.run(f'/usr/local/bin/pw-link -d {SRC}:{s_p} {IN}:{i_p}', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "        for s_p, i_p in want:\n"
                "            subprocess.run(f'/usr/local/bin/pw-link {SRC}:{s_p} {IN}:{i_p}', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                f"fg_str = {repr(fg_str)}\n"
                f"fg_dis = {fg_dis_flag}\n"
                f"widen = {widen}\n"
                f"feed_widen = {feed_widen}\n"
                "dsp_params = ['channelmix.max-volume', 10000000.0, 'channelmix.stereo-widen', widen]\n"
                "feed_dsp_params = ['channelmix.max-volume', 10000000.0, 'channelmix.stereo-widen', feed_widen]\n"
                "if fg_dis:\n"
                "    dsp_params += ['audioconvert.filter-graph.0', '', 'audioconvert.filter-graph.disable', True]\n"
                "    feed_dsp_params += ['audioconvert.filter-graph.0', '', 'audioconvert.filter-graph.disable', True]\n"
                "else:\n"
                "    dsp_params += ['audioconvert.filter-graph.0', fg_str, 'audioconvert.filter-graph.disable', False]\n"
                "    feed_dsp_params += ['audioconvert.filter-graph.0', fg_str, 'audioconvert.filter-graph.disable', False]\n"
                f"transp_payload = json.dumps({{'channelVolumes': [{vol_l}, {vol_r}], 'channelMap': ['FL', 'FR'], 'mute': False, 'params': dsp_params}})\n"
                f"feed_payload = json.dumps({{'channelVolumes': [{feed_vol_l}, {feed_vol_r}], 'channelMap': ['FL', 'FR'], 'mute': False, 'params': feed_dsp_params}})\n"
                "src_bypass = json.dumps({'params': ['audioconvert.filter-graph.0', '', 'audioconvert.filter-graph.disable', True, 'channelmix.stereo-widen', 0.0]})\n"
                "out = subprocess.check_output(['/usr/local/bin/pw-dump']).decode('utf-8', errors='ignore')\n"
                "for n in json.loads(out):\n"
                "    if n.get('type') != 'PipeWire:Interface:Node': continue\n"
                "    p = n.get('info', {}).get('props', {})\n"
                "    name = p.get('node.name', '')\n"
                "    media = p.get('media.name', '')\n"
                "    nid = str(n['id'])\n"
                "    if name.startswith('alsa_input.platform-soc_ahub0'):\n"
                "        subprocess.run(['/usr/local/bin/pw-cli', 's', nid, 'Props', src_bypass], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "    elif 'vip_transp_in' in name or 'vip_feed_mic_in' in name:\n"
                "        subprocess.run(['/usr/local/bin/pw-cli', 's', nid, 'Props', '{ channelVolumes: [ 1.0, 1.0 ], channelMap: [ FL, FR ], mute: false }'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "    elif 'vip_transp_out' in name or 'vip_transp_out' in media:\n"
                "        subprocess.run(['/usr/local/bin/pw-cli', 's', nid, 'Props', transp_payload], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "    elif 'vip_feed_mic_out' in name or 'vip_feed_mic_out' in media or name == 'vip_mic_bus':\n"
                "        subprocess.run(['/usr/local/bin/pw-cli', 's', nid, 'Props', feed_payload], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                f"    elif 'vip_feed_music_out' in name or 'vip_feed_music_out' in media:\n"
                f"        subprocess.run(['wpctl', 'set-volume', '-l', '2.0', nid, '{feed_pct / 100.0:.3f}'])\n"
                "EOF"
            )
            proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.communicate()

            kernel = getattr(self, 'os_kernel', None)
            if kernel and hasattr(kernel, 'enforce_ahub0_24bit_i2s'):
                kernel.enforce_ahub0_24bit_i2s()

    async def stop_all_mix_streams(self):
        """Terminates all session-scoped Transparency and Mic Feed loopback processes cleanly."""
        import asyncio
        for attr in ('_transp_proc', '_feed_music_proc', '_feed_mic_proc'):
            proc = getattr(self, attr, None)
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            setattr(self, attr, None)
        p_stop = await asyncio.create_subprocess_shell(
            "pkill -9 -f 'pw-loopback.*vip_' 2>/dev/null || true",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await p_stop.wait()

    def trigger_transparency_sync(self):
        """
        Schedules a serialized Transparency / Mic Feed graph transition without cancelling an
        in-flight _transp_lock holder mid-spawn, preventing half-killed pw-loopback races.
        """
        import asyncio
        self._transp_resync_needed = True
        if not getattr(self, '_transp_sync_task', None) or self._transp_sync_task.done():
            self._transp_sync_task = asyncio.create_task(self._run_serialized_transparency_sync())

    async def _run_serialized_transparency_sync(self):
        while getattr(self, '_transp_resync_needed', False):
            self._transp_resync_needed = False
            await self.sync_transparency_and_mic_feed()

    async def sync_transparency_and_mic_feed(self):
        """
        Orchestrates live PipeWire loopback graphs for:
        1. Transparency Mode (I2S Mic -> Active Headphones DAC with independent volume & music ducking).
        2. Mic Input Feed ('mic': Board Mics, 'music': Active DAC Monitor -> Phone Mic, 'mix': Both -> Phone Mic).
        Guarded by self._transp_lock and port-readiness polling to prevent ON/OFF race conditions.
        """
        import asyncio
        if not hasattr(self, '_transp_lock') or self._transp_lock is None:
            self._transp_lock = asyncio.Lock()

        async with self._transp_lock:
            # 1. Manage Transparency Loopback (I2S Mic -> Default Sink)
            if self.transparency_enabled:
                # Verify whether an existing vip_transp_in port is actually alive in PipeWire
                chk_alive = await asyncio.create_subprocess_shell(
                    "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-link -i 2>/dev/null | grep -q 'vip_transp_in:input_FL'",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await chk_alive.wait()
                proc_dead = (not self._transp_proc) or (self._transp_proc.returncode is not None) or (chk_alive.returncode != 0)

                if proc_dead:
                    p_kill = await asyncio.create_subprocess_shell(
                        "pkill -9 -f 'pw-loopback.*vip_transp' 2>/dev/null || true",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                    )
                    await p_kill.wait()
                    await asyncio.sleep(0.12)
                    transp_cmd = (
                        "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-loopback "
                        "--capture-props='node.name=vip_transp_in node.latency=1024/48000 node.force-quantum=1024 node.autoconnect=false node.dont-reconnect=true stream.dont-remix=true audio.position=[FL FR]' "
                        "--playback-props='node.name=vip_transp_out media.name=vip_transp_out node.latency=1024/48000 node.force-quantum=1024 stream.dont-remix=true audio.position=[FL FR]'"
                    )
                    self._transp_proc = await asyncio.create_subprocess_shell(
                        transp_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                    )
                    # Poll up to 1.5s (15 x 0.1s) until vip_transp_in ports register in PipeWire before linking
                    for _ in range(15):
                        await asyncio.sleep(0.1)
                        p_ready = await asyncio.create_subprocess_shell(
                            "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-link -i 2>/dev/null | grep -q 'vip_transp_in:input_FL'",
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                        )
                        await p_ready.wait()
                        if p_ready.returncode == 0:
                            break
            else:
                if self._transp_proc and self._transp_proc.returncode is None:
                    try:
                        self._transp_proc.terminate()
                    except Exception:
                        pass
                self._transp_proc = None
                p_kill = await asyncio.create_subprocess_shell(
                    "pkill -9 -f 'pw-loopback.*vip_transp' 2>/dev/null || true",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await p_kill.wait()

            # 2. Manage Mic Input Feed ('mic', 'music', 'mix') via persistent 1024-quantum vip_mic_bus
            chk_proc = await asyncio.create_subprocess_shell(
                "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-link -i 2>/dev/null | grep -q 'vip_mic_bus:playback_FL'",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await chk_proc.wait()
            if chk_proc.returncode != 0:
                load_cmd = (
                    "sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 pactl unload-module module-null-sink 2>/dev/null || true; "
                    "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-cli create-node adapter "
                    "'{ factory.name=support.null-audio-sink node.name=vip_mic_bus node.description=VIP_Mic_Bus "
                    "media.class=Audio/Duplex object.linger=true audio.position=[FL FR] "
                    "node.latency=1024/48000 node.force-quantum=1024 clock.quantum-limit=1024 monitor.channel-volumes=false }'"
                )
                p_load = await asyncio.create_subprocess_shell(load_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await p_load.communicate()
                await asyncio.sleep(0.15)

            # 2a. Manage Board Mic -> vip_mic_bus loopback (active in 'mic' and 'mix' modes)
            want_mic_feed = self.mic_feed_mode in ("mic", "mix")
            if want_mic_feed:
                chk_mic = await asyncio.create_subprocess_shell(
                    "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-link -i 2>/dev/null | grep -q 'vip_feed_mic_in:input_FL'",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await chk_mic.wait()
                mic_dead = (not getattr(self, '_feed_mic_proc', None)) or (self._feed_mic_proc.returncode is not None) or (chk_mic.returncode != 0)
                if mic_dead:
                    p_k = await asyncio.create_subprocess_shell(
                        "pkill -9 -f 'pw-loopback.*vip_feed_mic' 2>/dev/null || true",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                    )
                    await p_k.wait()
                    await asyncio.sleep(0.08)
                    mic_loop_cmd = (
                        "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-loopback "
                        "--capture-props='node.name=vip_feed_mic_in node.latency=1024/48000 node.force-quantum=1024 node.autoconnect=false node.dont-reconnect=true stream.dont-remix=true audio.position=[FL FR]' "
                        "--playback-props='node.name=vip_feed_mic_out media.name=vip_feed_mic_out node.latency=1024/48000 node.force-quantum=1024 node.target=vip_mic_bus node.dont-reconnect=true node.dont-move=true audio.position=[FL FR]'"
                    )
                    self._feed_mic_proc = await asyncio.create_subprocess_shell(
                        mic_loop_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                    )
                    for _ in range(15):
                        await asyncio.sleep(0.1)
                        p_r = await asyncio.create_subprocess_shell(
                            "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-link -i 2>/dev/null | grep -q 'vip_feed_mic_in:input_FL'",
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                        )
                        await p_r.wait()
                        if p_r.returncode == 0:
                            break
            else:
                if getattr(self, '_feed_mic_proc', None) and self._feed_mic_proc.returncode is None:
                    try:
                        self._feed_mic_proc.terminate()
                    except Exception:
                        pass
                self._feed_mic_proc = None
                p_k = await asyncio.create_subprocess_shell(
                    "pkill -9 -f 'pw-loopback.*vip_feed_mic' 2>/dev/null || true",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await p_k.wait()

            # 2b. Manage Music Monitor -> vip_mic_bus loopback (active in 'music' and 'mix' modes)
            if getattr(self, '_feed_music_proc', None) and self._feed_music_proc.returncode is None:
                try:
                    self._feed_music_proc.terminate()
                except Exception:
                    pass
            self._feed_music_proc = None
            p_mk = await asyncio.create_subprocess_shell(
                "pkill -9 -f 'pw-loopback.*vip_feed_music' 2>/dev/null || true",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await p_mk.wait()

            if self.mic_feed_mode in ("music", "mix"):
                sink_proc = await asyncio.create_subprocess_shell(
                    "sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 pactl get-default-sink",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
                )
                sink_out, _ = await sink_proc.communicate()
                active_sink = sink_out.decode().strip()
                if not active_sink or "vip_mic_bus" in active_sink:
                    active_sink = "alsa_output.platform-soc_ahub0_mach.stereo-fallback"

                music_loop_cmd = (
                    "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 /usr/local/bin/pw-loopback "
                    f"--capture-props='node.name=vip_feed_music_in node.target={active_sink}.monitor stream.capture.sink=true node.dont-move=true' "
                    "--playback-props='node.name=vip_feed_music_out media.name=vip_feed_music_out node.target=vip_mic_bus node.dont-move=true'"
                )
                self._feed_music_proc = await asyncio.create_subprocess_shell(
                    music_loop_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await asyncio.sleep(0.2)

            target_source = "vip_mic_bus"

            # 3. Set Default Audio Source and live-migrate any active recording streams (including native PipeWire bluez_output HFP SCO)
            route_src_cmd = (
                f"sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 pactl set-default-source {target_source} && "
                "runuser -u vipxpert -- env XDG_RUNTIME_DIR=/run/user/1000 python3 - << 'EOF'\n"
                "import subprocess\n"
                "out = subprocess.check_output(['pactl', 'list', 'source-outputs']).decode('utf-8', errors='ignore')\n"
                "for block in out.split('Source Output #')[1:]:\n"
                "    lines = block.splitlines()\n"
                "    so_id = lines[0].strip()\n"
                "    if any(tag in block for tag in ('vip_transp', 'vip_feed_', 'vip_usb_', 'Peak detect', 'parec')):\n"
                "        continue\n"
                f"    subprocess.run(['pactl', 'move-source-output', so_id, '{target_source}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "# Also explicitly link native PipeWire bluez_output.* HFP SCO capture streams to vip_mic_bus:monitor_FL/FR\n"
                "links = subprocess.check_output(['/usr/local/bin/pw-link', '-i']).decode('utf-8', errors='ignore').splitlines()\n"
                "for line in links:\n"
                "    port = line.strip()\n"
                "    if port.startswith('bluez_output.') and (port.endswith(':input_FL') or port.endswith(':input_FR') or port.endswith(':input_MONO')):\n"
                "        subprocess.run(['/usr/local/bin/pw-link', '-d', 'alsa_input.platform-soc_ahub0_mach.stereo-fallback:capture_FL', port], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "        subprocess.run(['/usr/local/bin/pw-link', '-d', 'alsa_input.platform-soc_ahub0_mach.stereo-fallback:capture_FR', port], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "        mon_p = 'vip_mic_bus:capture_FR' if port.endswith(':input_FR') else 'vip_mic_bus:capture_FL'\n"
                "        subprocess.run(['/usr/local/bin/pw-link', mon_p, port], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "EOF"
            )
            p_route = await asyncio.create_subprocess_shell(route_src_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await p_route.communicate()

            # 4. Link ports, wait for ALSA hw_params to settle, and lock 24-bit AHUB in a single pass
            await self.sync_mix_stream_volumes(relink=True)
            await asyncio.sleep(0.15)
            kernel = getattr(self, 'os_kernel', None)
            if kernel and hasattr(kernel, 'enforce_ahub0_24bit_i2s'):
                kernel.enforce_ahub0_24bit_i2s()
            await self.sync_exclusive_volumes()

    async def _set_wpctl_volume(self, target_id: str, vol_float: float, limit: str = "3.0"):
        """Executes wpctl safely, skipping redundant sets, and hunting dead IDs on crash."""
        import asyncio
        if not target_id:
            return

        # Prevent terminal spam and wasted CPU by skipping unchanged volumes
        cache_key = f"{target_id}"
        if self.last_synced_volumes.get(cache_key) == vol_float:
            return

        cmd = f"sudo -u vipxpert XDG_RUNTIME_DIR=/run/user/1000 wpctl set-volume -l {limit} {target_id} {vol_float}"
        process = await asyncio.create_subprocess_shell(cmd)
        await process.communicate()

        if process.returncode == 0:
            self.last_synced_volumes[cache_key] = vol_float
            print(f"[UIManager] Synced {target_id} volume to {vol_float}", flush=True)
        else:
            # wpctl crashed (exit code > 0) meaning PipeWire restarted and the ID is dead
            print(f"[UIManager] wpctl crashed on {target_id}. Wiping ID cache to force a re-hunt.", flush=True)
            if self.mpv_stream_id == target_id:
                self.mpv_stream_id = None
            elif self.bt_stream_id == target_id:
                self.bt_stream_id = None
            self.last_synced_volumes.pop(cache_key, None)

    def _is_i2s_effectively_active(self) -> bool:
        """Returns True if internal I2S is the active audio path (explicitly selected or via USB fallback)."""
        pref = self.db.get_setting("active_dac", "usb")
        return pref == "i2s" or not bool(self.active_hardware_dac)

    def trigger_dac_vol_sync(self):
        """Debounces rapid keypad < > ticks on Digital Volume so I2C0 never floods the CPU."""
        import asyncio
        if self._dac_vol_sync_task and not self._dac_vol_sync_task.done():
            self._dac_vol_sync_task.cancel()
        self._dac_vol_sync_task = asyncio.create_task(self._debounced_dac_vol_sync())

    async def _debounced_dac_vol_sync(self):
        import asyncio
        try:
            await asyncio.sleep(0.12)
            await self._apply_pcm5122_i2c_gain()
        except asyncio.CancelledError:
            pass

    async def _apply_pcm5122_i2c_gain(
        self,
        gain_mode=None,
        filter_mode=None,
        os_prog=None,
        deemph_mode=None,
        dig_vol_steps=None,
        balance_steps=None,
        ramp_mode=None,
        dac_div=None,
        ncp_div=None,
        osr_div=None,
        automute_mode=None,
        pll_mode=None,
        pulse_standby: bool = False
    ) -> bool:
        """
        Configures PCM5122 3-wire I2S 98.304MHz PLL clocking (P=1, J=16, D=0, R=2, DSP=/2)
        with unlocked manual clock dividers (Reg 0x25=0x0A) so Reg 0x1C (DAC Clock up to /32),
        Reg 0x1D (Charge Pump up to /128), and Reg 0x1E (Modulator OSR up to /64) take true hardware effect
        (monitored live in Reg 0x66-0x68). Also configures ROM Oversampling Program (Reg 0x2B),
        Phase Filter (Reg 0x26), De-Emphasis (Reg 0x07), Hardware Zero-Data Auto-Mute (Reg 0x40/0x41),
        Digital Volume & L/R Balance (Reg 0x3C-0x3E), and Analog Gain (Page 1 Reg 0x02).
        """
        import asyncio
        if gain_mode is None:
            gain_mode = self.db.get_setting("i2s_dac_gain", "high")
        if filter_mode is None:
            filter_mode = self.db.get_setting("i2s_dac_filter", "linear")
        if os_prog is None:
            os_prog = int(self.db.get_setting("i2s_dac_os_prog", "1"))
        if deemph_mode is None:
            deemph_mode = self.db.get_setting("i2s_dac_deemph", "0")
        if dig_vol_steps is None:
            dig_vol_steps = int(self.db.get_setting("i2s_dac_dig_vol_steps", "0"))
        if balance_steps is None:
            balance_steps = int(self.db.get_setting("i2s_dac_balance_steps", "0"))
        if ramp_mode is None:
            ramp_mode = self.db.get_setting("i2s_dac_vol_ramp", "fast")
        if dac_div is None:
            dac_div = int(self.db.get_setting("i2s_dac_clk_div", "0"))
        if ncp_div is None:
            ncp_div = int(self.db.get_setting("i2s_dac_ncp_div", "127"))
        if osr_div is None:
            osr_div = int(self.db.get_setting("i2s_dac_osr_div", "127"))
        if automute_mode is None:
            automute_mode = self.db.get_setting("i2s_dac_automute", "off")
        if pll_mode is None:
            pll_mode = self.db.get_setting("i2s_dac_pll_mode", "sync_49m")

        def _write_i2c():
            import smbus2
            from src.hardware.display import I2C_BUS_LOCK
            reg_val = 0x11 if gain_mode == "low" else 0x00
            flt_val = 0x01 if filter_mode == "low_lat" else 0x00
            prog_val = os_prog if os_prog in (1, 2, 3, 7) else 1
            deemph_val = 0x10 if str(deemph_mode) == "1" else 0x00

            # Comment (invariant): Reg 0x3C bits 1:0 (VOLLR) MUST stay 00 so Reg 0x3D (Left) and Reg 0x3E (Right) remain independent; 0x22/0x11 slave L/R together.
            ramp_map = {"fast": 0x00, "med": 0x50, "smooth": 0xA0}
            ramp_val = ramp_map.get(str(ramp_mode), 0x00)

            # Safe non-crunching hardware divider ranges verified on silicon (-1 = Factory Auto ⚙):
            # DAC Clock (0x1C): -1=Auto(/16), 0=/1 (98.3MHz ★), 1=/2, 3=/4, 7=/8, 15=/16, 31=/32
            # Charge Pump (0x1D): -1=Auto(/4), 19=/20, 23=/24, 27=/28, 31=/32, 47=/48, 63=/64, 95=/96, 127=/128 ★ (< /20 collapses VNEG)
            # Modulator OSR (0x1E): -1=Auto(/8), 1=/2, 2=/3, 3=/4, 5=/6, 7=/8, 11=/12, 15=/16, 23=/24, 31=/32, 47=/48, 63=/64, 79=/80, 95=/96, 111=/112, 127=/128 ★
            d_div = int(dac_div) if int(dac_div) in (-1, 0, 1, 3, 7, 15, 31) else 0
            n_div = int(ncp_div) if int(ncp_div) in (-1, 19, 23, 27, 31, 47, 63, 95, 127) else (-1 if int(ncp_div) == 0 else 127)
            o_div = int(osr_div) if int(osr_div) in (-1, 1, 2, 3, 5, 7, 11, 15, 23, 31, 47, 63, 79, 95, 111, 127) else (-1 if int(osr_div) == 0 else 127)

            # When any divider is manual (>=0 for DAC, >0 for NCP/OSR), unlock manual divider tree (0x25=0x0A)
            # and substitute factory auto equivalents (DAC=0x0F /16, NCP=0x03 /4, OSR=0x07 /8) for any '-1' (Auto) slot
            manual_clk = (d_div >= 0) or (n_div > 0) or (o_div > 0)
            reg_0x25 = 0x0A if manual_clk else 0x08
            hw_dac_div = d_div if d_div >= 0 else 0x0F
            hw_ncp_div = n_div if n_div > 0 else 0x03
            hw_osr_div = o_div if o_div > 0 else 0x07

            # Stage: Atomic 400kHz-Scoped PCM5122 Register Latch (Steps TWI0_CCR down to 0x28 and retries so MV overdrive never strands DAC in standby)
            # Comment (invariant): When DAC=/1 (0x00), PLL R=2 (98.3MHz) with DSP=/2 (49.15MHz) runs DAC_CLK 2x faster than DSP_CLK, dropping Left at <=1% volume; R=1 + DSP=/1 (49.15MHz 1:1) locks Reg 0x5A=0x19 cleanly.
            is_98m = (str(pll_mode) == "over_98m")
            reg_0x18 = 0x01 if is_98m else 0x00
            reg_0x1b = 0x01 if is_98m else 0x00
            div_tuple = (prog_val, reg_0x25, reg_0x18, reg_0x1b, hw_dac_div, hw_ncp_div, hw_osr_div)
            do_pulse = pulse_standby or (getattr(self, '_last_pcm5122_divs', None) != div_tuple)

            # Hardware Zero-Data Auto-Mute (Reg 0x40 delay, Reg 0x41 control)
            if automute_mode == "105ms":
                am_time, am_ctrl = 0x77, 0x07
            elif automute_mode == "21ms":
                am_time, am_ctrl = 0x33, 0x07
            else:
                am_time, am_ctrl = 0x77, 0x00

            # Map half-dB step integer + independent L/R balance attenuation to PCM5122 hardware register bytes (0x30 - steps)
            clamped_steps = max(-120, min(12, int(dig_vol_steps)))
            clamped_bal = max(-20, min(20, int(balance_steps)))
            l_steps = clamped_steps - (clamped_bal if clamped_bal > 0 else 0)
            r_steps = clamped_steps - (-clamped_bal if clamped_bal < 0 else 0)
            vol_reg_l = max(0x00, min(0xFF, 0x30 - l_steps))
            vol_reg_r = max(0x00, min(0xFF, 0x30 - r_steps))

            seq = [
                (0x00, 0x00),
                *(([(0x02, 0x11)]) if do_pulse else []),
                (0x2b, prog_val),
                (0x0d, 0x10),
                (0x14, 0x00),
                (0x15, 0x10),
                (0x16, 0x00),
                (0x17, 0x00),
                (0x18, reg_0x18),
                (0x1b, reg_0x1b),
                (0x1c, hw_dac_div),
                (0x1d, hw_ncp_div),
                (0x1e, hw_osr_div),
                (0x25, reg_0x25),
                *(([(0x02, 0x11)]) if do_pulse else []),
                (0x02, 0x00),
                (0x26, flt_val),
                (0x07, deemph_val),
                (0x3c, ramp_val),
                (0x3d, vol_reg_l),
                (0x3e, vol_reg_r),
                (0x40, am_time),
                (0x41, am_ctrl),
                (0x03, 0x00),
                (0x00, 0x01),
                (0x01, 0x00),
                (0x02, reg_val),
                (0x00, 0x00),
            ]

            import struct as _struct
            # Comment (invariant): Serialize 0x4C writes strictly via I2C_BUS_LOCK (<3ms @ 100kHz) without writing shm[12:16] (which Display.show() overwrites) and without firing TWI0_SRST on normal STAT==0x00 or probing non-existent 0x4D when 0x4C is the board DAC.
            target_addrs = (getattr(self, '_pcm5122_addr', 0x4c),) if hasattr(self, '_pcm5122_addr') else (0x4c, 0x4d)
            for attempt in range(3):
                with I2C_BUS_LOCK:
                    prev_ccr = None
                    twi_m = getattr(getattr(self, 'display', None), '_twi_mmap', None)
                    try:
                        if hasattr(self, 'display') and self.display:
                            if twi_m is not None:
                                prev_ccr = int(twi_m[0x14])
                                if twi_m[0x10] == 0x38:
                                    twi_m[0x1C:0x20] = _struct.pack("<I", 0x01)
                                    time.sleep(0.001)
                            self.display._write_twi0_ccr(0x59)
                            time.sleep(0.0004)
                        with smbus2.SMBus(0) as bus:
                            for addr in target_addrs:
                                try:
                                    for reg, val in seq:
                                        bus.write_byte_data(addr, reg, val)
                                    self._pcm5122_addr = addr
                                    self._last_pcm5122_divs = div_tuple
                                    return True
                                except Exception:
                                    try:
                                        bus.write_byte_data(addr, 0x00, 0x00)
                                        bus.write_byte_data(addr, 0x02, 0x00)
                                        bus.write_byte_data(addr, 0x03, 0x00)
                                    except Exception:
                                        pass
                                    continue
                    finally:
                        if prev_ccr is not None and hasattr(self, 'display') and self.display:
                            self.display._write_twi0_ccr(prev_ccr if prev_ccr not in (0x08, 0x88) else 0x90)
                time.sleep(0.008)
            return False

        try:
            self.pcm5122_hw_gain_active = await asyncio.to_thread(_write_i2c)
            return self.pcm5122_hw_gain_active
        except Exception:
            self.pcm5122_hw_gain_active = False
            return False

    async def read_pcm5122_status(self) -> str:
        """
        Reads live PCM5122 silicon telemetry from Page 0 status registers:
        - Reg 0x02 (Standby state)
        - Reg 0x5A (Detected FS Speed & BCK Lock)
        - Reg 0x66, 0x67, 0x68 (Active Hardware Dividers for DAC, Charge Pump, and Modulator OSR)
        - Reg 0x78 (Live Hardware Auto-Mute Status: 0x00 Active vs 0x11 Zero-Muted)
        """
        import asyncio

        def _read_i2c():
            from smbus2 import SMBus, i2c_msg
            from src.hardware.display import I2C_BUS_LOCK
            disp = getattr(self, 'display', None)
            with I2C_BUS_LOCK:
                if disp and hasattr(disp, '_write_twi0_ccr'):
                    disp._write_twi0_ccr(0x59)
                    time.sleep(0.0005)
                try:
                    with SMBus(0) as bus:
                        def _rb(a, r):
                            bus.i2c_rdwr(i2c_msg.write(a, bytes([r])))
                            time.sleep(0.00015)
                            rm = i2c_msg.read(a, 1)
                            bus.i2c_rdwr(rm)
                            return bytes(rm)[0]
                        for addr in (0x4c, 0x4d):
                            try:
                                bus.i2c_rdwr(i2c_msg.write(addr, bytes([0x00, 0x00])))
                                r02 = _rb(addr, 0x02)
                                r5a = _rb(addr, 0x5a)
                                r66 = _rb(addr, 0x66)
                                r67 = _rb(addr, 0x67)
                                r68 = _rb(addr, 0x68)
                                r78 = _rb(addr, 0x78)
                                if r02 & 0x10:
                                    return "Standby (Killed)"
                                if r5a == 0x00:
                                    return "Idle (No BCK)"
                                if r78 & 0x11:
                                    return "Auto-Muted (0s)"
                                d_div = (r66 & 0x7F) + 1
                                c_div = (r67 & 0x7F) + 1
                                m_div = (r68 & 0x7F) + 1
                                return f"D/{d_div} C/{c_div} M/{m_div}"
                            except Exception:
                                continue
                finally:
                    if disp and hasattr(disp, '_write_twi0_ccr'):
                        disp._write_twi0_ccr(getattr(disp, '_overdrive_ccr', 0x90))
            return "I2C Read Error!"

        try:
            return await asyncio.to_thread(_read_i2c)
        except Exception:
            return "I2C Read Error!"

    async def set_pcm5122_power(self, standby: bool) -> bool:
        """Toggles PCM5122 I2C hardware standby (Page 0, Reg 0x02/0x03) for manual Wake / Kill I2S DAC controls."""
        import asyncio
        if not standby:
            self.i2s_manually_killed = False
            gain_mode = self.db.get_setting("i2s_dac_gain", "high")
            return await self._apply_pcm5122_i2c_gain(gain_mode)

        def _sleep_i2c():
            import smbus2
            from src.hardware.display import I2C_BUS_LOCK
            with I2C_BUS_LOCK:
                with smbus2.SMBus(0) as bus:
                    for addr in (0x4c, 0x4d):
                        try:
                            bus.write_byte_data(addr, 0x00, 0x00)
                            bus.write_byte_data(addr, 0x03, 0x11)  # Mute L & R
                            bus.write_byte_data(addr, 0x02, 0x10)  # Enter Standby (powers down charge pump & analog stage)
                            return True
                        except Exception:
                            continue
            return False

        try:
            ok = await asyncio.to_thread(_sleep_i2c)
            if ok:
                self.i2s_manually_killed = True
            return ok
        except Exception:
            return False

    async def sync_exclusive_volumes(self):
        """Applies stream math (Mode 1), sink math (Mode 2), or baseline locks (Mode 0) without waking a manually killed I2S DAC."""
        import asyncio

        # Auto-heal any dead IDs before calculating math
        await self._ensure_stream_ids()

        # Safely read pure state directly from the Database to prevent reboot desyncs
        raw_mode = self.db.get_setting("exclusive_audio", "0")
        if raw_mode == "False": raw_mode = "0"
        if raw_mode == "True": raw_mode = "1"
        mode = int(raw_mode)

        MPV_BASE = 1.0
        BT_BASE = 1.0
        ui_float = float(self.db.get_setting("exclusive_volume", "1.0"))

        if mode == 1:
            # Mode 1 (Stream Scale): Master locked to 1.0, penalties on individual streams
            await self._set_wpctl_volume("@DEFAULT_AUDIO_SINK@", 1.0, limit="3.0")
            mpv_vol = ui_float * MPV_BASE
            bt_vol = ui_float * BT_BASE

        elif mode == 2:
            # Mode 2 (Sink Scale): Streams wide open (1.0), calculate penalty on the Master Sink dynamically
            mpv_vol = 1.0
            bt_vol = 1.0

            # Dynamically determine the active source to scale the Master Sink appropriately
            active_source = "bluetooth" # Default assumption
            try:
                if hasattr(self, 'media_arbiter') and self.media_arbiter:
                    arb_src = str(getattr(self.media_arbiter, 'active_source', '')).lower()
                    if arb_src == "local":
                        active_source = "local"
                    else:
                        now_playing = self.media_arbiter.get_now_playing()
                        if now_playing and str(now_playing.get('source', '')).lower() == 'local':
                            active_source = "local"
            except Exception:
                pass

            sink_target = (ui_float * MPV_BASE) if active_source == "local" else (ui_float * BT_BASE)
            await self._set_wpctl_volume("@DEFAULT_AUDIO_SINK@", sink_target, limit="3.0")

        else:
            # Mode 0 (Standard): Master Sink controlled manually by UI, streams locked to pure baselines
            mpv_vol = MPV_BASE
            bt_vol = BT_BASE

            # Restore standard Master Sink volume from DB so we don't get blasted by leftover Mode 2 math
            std_vol = float(self.db.get_setting("standard_volume", "1.0"))
            await self._set_wpctl_volume("@DEFAULT_AUDIO_SINK@", std_vol, limit="2.0")

        # Apply optional Music Ducking when Transparency Mode is active in this session
        if getattr(self, 'transparency_enabled', False):
            duck_mode = self.db.get_setting("transparency_duck", "off")
            duck_map = {"off": 1.0, "6db": 0.5, "12db": 0.25, "mute": 0.0}
            duck_mult = duck_map.get(duck_mode, 1.0)
            mpv_vol = round(mpv_vol * duck_mult, 4)
            bt_vol = round(bt_vol * duck_mult, 4)

        # Apply ceiling math to active streams via cached IDs without decimal truncation
        if self.mpv_stream_id:
            await self._set_wpctl_volume(self.mpv_stream_id, mpv_vol, limit="3.0")
        if self.bt_stream_id:
            await self._set_wpctl_volume(self.bt_stream_id, bt_vol, limit="3.0")

    async def adjust_pi_volume(self, delta: int):
        """Tracks volume globally, routing commands to streams or sinks depending on the 3-way Exclusive Mode state."""
        import asyncio

        raw_mode = self.db.get_setting("exclusive_audio", "0")
        if raw_mode == "False": raw_mode = "0"
        if raw_mode == "True": raw_mode = "1"
        mode = int(raw_mode)

        # Branch 1: Exclusive Math Modes 1 & 2 (128 steps = 100%, 256 steps = 200%)
        if mode in [1, 2]:
            if not hasattr(self, 'exclusive_volume_step'):
                # Snap the database float directly to our perfect grid on boot
                saved_float = float(self.db.get_setting("exclusive_volume", "1.0"))
                self.exclusive_volume_step = max(0, min(256, round(saved_float * 128)))

            if delta != 0:
                self.exclusive_volume_step = max(0, min(256, self.exclusive_volume_step + delta))
                # Store the exact binary float mathematically derived from the step
                exact_float = self.exclusive_volume_step / 128.0
                self.db.set_setting("exclusive_volume", str(exact_float))
                await self.sync_exclusive_volumes()

            # Calculate live float strictly from the grid step for visual accuracy
            current_vol = self.exclusive_volume_step / 128.0
            self.pi_volume_float = current_vol
            self.pi_volume_str = f"{(current_vol * 100):.2f}%"
            self.request_render()
            return

        # Branch 2: Standard Master Sink
        if not hasattr(self, 'pi_volume_step'):
            # Snap the database standard float directly to our perfect grid on boot
            saved_std = float(self.db.get_setting("standard_volume", "1.0"))
            self.pi_volume_step = max(0, min(256, round(saved_std * 128)))

        if delta != 0:
            # Apply exactly 1 step (1/128th) and clamp between 0 and 256 (200%)
            self.pi_volume_step = max(0, min(256, self.pi_volume_step + delta))
            perfect_float = self.pi_volume_step / 128.0
            self.db.set_setting("standard_volume", str(perfect_float))
            await self._set_wpctl_volume("@DEFAULT_AUDIO_SINK@", perfect_float, limit="2.0")

        elif delta == 0:
            # Ensure hardware actually matches UI state on refresh or boot
            perfect_float = self.pi_volume_step / 128.0
            await self._set_wpctl_volume("@DEFAULT_AUDIO_SINK@", perfect_float, limit="2.0")

        # Format for the screen beautifully to 2 decimals
        current_vol = self.pi_volume_step / 128.0
        self.pi_volume_float = current_vol
        self.pi_volume_str = f"{(current_vol * 100):.2f}%"

        self.request_render()

    def request_render(self):
        """Flags the compositor that a visual state change occurred."""
        self.needs_render = True

    # Stage: Universal Compositor Toast (Single-source top-Z toast overlay across all menus, apps, and 3-phase PWM scenes)
    def show_toast(self, message: str, duration: float = 1.35, blocking: bool = False):
        now = time.monotonic()
        self._toast_message = str(message) if message else None
        self._toast_birth_ts = now
        self._toast_expire_ts = (now + 3600.0) if blocking else (now + max(0.35, float(duration)))
        self._toast_blocking = bool(blocking)
        self.needs_render = True

    def clear_toast(self):
        if self._toast_message is not None:
            self._toast_message = None
            self._toast_blocking = False
            self.needs_render = True

    def _render_universal_toast(self):
        msg = self._toast_message
        if not msg:
            return
        now = time.monotonic()
        if not self._toast_blocking and now >= self._toast_expire_ts:
            self._toast_message = None
            self.needs_render = True
            return

        rem = self._toast_expire_ts - now
        if rem < 0.45 and not self._toast_blocking:
            self.display.has_active_marquee = True
        else:
            # Schedule deterministic wake right when toast expires so static screens clear it with 0 lag
            nxt_w = getattr(self.display, 'next_marquee_wake', 0.0)
            if nxt_w <= 0.0 or self._toast_expire_ts < nxt_w:
                self.display.next_marquee_wake = self._toast_expire_ts

        line = msg.strip()[:20]
        font = self.display.font_small
        try:
            bb = self.display.draw.textbbox((0, 0), line, font=font)
            tw = max(16, bb[2] - bb[0])
        except Exception:
            tw = len(line) * 6
        box_w = min(124, tw + 10)
        x0 = max(2, (128 - box_w) // 2)
        x1 = min(125, x0 + box_w)
        y0, y1 = 49, 62
        tx = x0 + max(3, (box_w - tw) // 2)

        # Crisp bordered high-contrast HUD toast pill + 2px left accent bar
        self.display.draw.rectangle((max(0, x0 - 1), y0 - 1, min(127, x1 + 1), min(63, y1 + 1)), fill=0)
        self.display.draw.rectangle((x0, y0, x1, y1), fill=0, outline=1)
        self.display.draw.rectangle((x0, y0, min(x1, x0 + 2), y1), fill=1)
        self.display.draw.text((tx, y0 + 1), line, font=font, fill=1)

        # Comment (invariant): Mirror active universal toast strip onto _pwm_gray_planes (ph1/lo) whenever 3-phase PWM shading is active on the frame so grayscale savers/visualizers never flicker through the toast box.
        gp = getattr(self.display, '_pwm_gray_planes', None)
        if gp is not None:
            band = self.display.image.crop((0, y0 - 1, 128, 64))
            gp[0].paste(band, (0, y0 - 1))
            gp[1].paste(band, (0, y0 - 1))

    def draw_top_right_battery(self) -> bool:
        """Single-source DRY top-right battery percentage badge ('88%' or '+88%') with clean black left mask."""
        pwr = getattr(self, "power_state", {})
        if pwr.get("error", True) and float(pwr.get("voltage", 0.0)) <= 0.5:
            return False
        pct = max(0, min(100, int(round(float(pwr.get("percent", 0.0))))))
        prefix = "+" if pwr.get("is_charging", False) else ""
        badge = f"{prefix}{pct}%"
        font = self.display.font_small
        tw = int(round(font.getlength(badge))) if hasattr(font, "getlength") else len(badge) * 6
        bx = max(76, 126 - tw)
        self.display.draw.rectangle((bx - 3, 0, 127, 11), fill=0)
        self.display.draw_text(bx, 0, badge)
        return True

    async def composite_frame(self):
        """Master rendering pipeline. Flushes RAM canvas and layers UI components asynchronously."""
        if getattr(self, 'is_screen_off', False) or getattr(self.display, 'is_asleep', False):
            self.needs_render = False
            return

        t_c0 = time.monotonic()
        self.display.clear()
        t_c1 = time.monotonic()

        # 1. Base Layer (Guarded so a scene render error never crashes OSKernel._main_loop)
        if self.current_app and hasattr(self.current_app, '_draw_state'):
            try:
                self.current_app._draw_state()
            except Exception as e:
                import traceback
                print(f"[Compositor] Base _draw_state error in {self.current_app_id}: {e}", flush=True)
                traceback.print_exc()
        t_c2 = time.monotonic()

        # 2. Overlay Layer
        if self.active_overlay and hasattr(self.active_overlay, '_draw_state'):
            try:
                self.active_overlay._draw_state()
            except Exception as e:
                import traceback
                print(f"[Compositor] Overlay _draw_state error: {e}", flush=True)
                traceback.print_exc()

        # 3. Universal Top-Z Toast Layer (Shared by all menus, apps, and Lock Screen scenes)
        self._render_universal_toast()

        # 4. Master Hardware Flush (Yields CPU to background thread)
        await self.display.show()
        t_c3 = time.monotonic()
        self._last_comp_draw_ms = round((t_c2 - t_c1) * 1000.0, 2)
        self._last_comp_total_ms = round((t_c3 - t_c0) * 1000.0, 2)

        self.needs_render = False

    async def suspend_current(self):
        """Pushes the current app to the background without destroying it."""
        if self.current_app_id and self.current_app:
            self.previous_app_id = self.current_app_id
            self.current_app = None

    async def destroy_app(self, app_id: str):
        """Violently kills an app instance and frees the memory."""
        if app_id in self.active_apps:
            app_to_kill = self.active_apps[app_id]
            if hasattr(app_to_kill, 'on_stop'):
                await app_to_kill.on_stop()

            del self.active_apps[app_id]

            # If we just killed the app we are currently looking at
            if self.current_app_id == app_id:
                self.current_app = None
                self.current_app_id = None

    async def switch_app(self, app_id: str, fallback_app: Optional[BaseApp] = None):
        """Mounts an app by ID, pulling from background cache if alive, or registers a new one."""
        # Capture the ID of the app we are leaving before we suspend it
        previous_app_id = getattr(self, 'current_app_id', None)

        # 1. Suspend the currently running app
        await self.suspend_current()

        is_fresh_mount = False

        # 2. Resolve the new app instance
        if fallback_app:
            # Explicitly provided a new temporary instance. Overwrite cache and slay the ghost!
            is_fresh_mount = True
            if app_id in self.active_apps:
                ghost_app = self.active_apps[app_id]
                if hasattr(ghost_app, 'on_stop'):
                    await ghost_app.on_stop()

            self.current_app = fallback_app
            self.active_apps[app_id] = self.current_app
        elif app_id in self.active_apps:
            # Pluck the app directly from the background cache
            self.current_app = self.active_apps[app_id]
        else:
            is_fresh_mount = True
            if app_id in self.app_registry:
                # Instantiate from the Master OS Manifest
                self.current_app = self.app_registry[app_id]()
                self.active_apps[app_id] = self.current_app
            else:
                print(f"[UIManager] Error: App '{app_id}' not found in cache or registry.")
                return

        # 3. Mount it to the screen
        self.current_app_id = app_id

        if is_fresh_mount:
            # Boot it up if it's brand new
            if hasattr(self.current_app, 'on_start'):
                await self.current_app.on_start()
        else:
            # If pulling from cache, trigger on_resume if it exists, otherwise fallback to update
            if hasattr(self.current_app, 'on_resume'):
                # Pass context so apps know where they woke up from
                await self.current_app.on_resume(previous_app_id=previous_app_id)
            elif hasattr(self.current_app, 'update'):
                await self.current_app.update()

    async def inject_key(self, event_tuple):
        """Routes Time-Aware key events (action, char) and handles global OS intercepts."""
        try:
            action, char = event_tuple
            self.poke_activity()

            # --- STRICT WAKE & HEADLESS PASSTHROUGH ---
            if getattr(self, 'is_screen_off', False):
                if char == '*':
                    if action == 'DOWN':
                        self.display.wake()
                        self.is_screen_off = False
                        if hasattr(self, 'power_monitor') and self.power_monitor:
                            self.power_monitor.notify_screen_wake()
                        if self.current_app and hasattr(self.current_app, 'on_screen_wake'):
                            await self.current_app.on_screen_wake()
                        self.request_render()
                        self._wake_key_active = True
                        return
                    # Allow '*' HOLD to sneak through for lock screen unlocking
                    if action != 'HOLD':
                        return
                else:
                    # Headless Navigation: pass non-* keys to active app without waking screen or requesting render
                    if self.current_app and hasattr(self.current_app, 'handle_key'):
                        await self.current_app.handle_key(action, char)
                    return

            # Swallow the CLICK/UP/HOLD events of the key that woke the screen
            if getattr(self, '_wake_key_active', False):
                if action in ('UP', 'DOWN'):
                    self._wake_key_active = False
                    if action == 'UP':
                        return
                # Punch a hole specifically for the Unlock HOLD action to passthrough
                if not (action == 'HOLD' and char == '*'):
                    return

            # Global Input Hook: Guarantee a screen refresh for any awake hardware interaction
            self.request_render()
            # --- GLOBAL OS INTERCEPTS ---

            # 1. Global Lifecycle (* key)
            if char == '*':
                if self.current_app_id == 'lock_screen' and getattr(self.current_app, 'standalone_scene', None) is None:
                    if action == 'HOLD':
                        self._wake_key_active = False
                        self.db.set_setting("is_locked", "False")
                        await self.destroy_app('lock_screen')

                        target = self.previous_app_id
                        if not target or target == 'lock_screen':
                            target = 'home'
                        await self.switch_app(target)
                        self._process_pending_overlays()
                        return
                    elif action == 'CLICK':
                        self.display.sleep()
                        self.is_screen_off = True
                        if self.current_app and hasattr(self.current_app, 'on_screen_sleep'):
                            await self.current_app.on_screen_sleep()
                        return
                else:
                    if action == 'CLICK':
                        self.db.set_setting("is_locked", "True")
                        if getattr(self.active_apps.get('lock_screen'), 'standalone_scene', None) is not None:
                            await self.destroy_app('lock_screen')
                        await self.switch_app('lock_screen')
                        return
                    elif action == 'HOLD':
                        self.display.sleep()
                        self.is_screen_off = True
                        self.db.set_setting("is_locked", "True")
                        if getattr(self.active_apps.get('lock_screen'), 'standalone_scene', None) is not None:
                            await self.destroy_app('lock_screen')
                        await self.switch_app('lock_screen')
                        return
                return

            # 2. Global Task Manager (Hold D)
            if char == 'D' and action == 'HOLD':
                if self.current_app_id != 'lock_screen':
                    await self.switch_app('recent_tab')
                return

            # --- PASS-THROUGH TO ACTIVE APP OR OVERLAY ---
            if self.active_overlay:
                if hasattr(self.active_overlay, 'handle_key'):
                    await self.active_overlay.handle_key(action, char)

                    # Check if the overlay flagged itself for destruction after the input
                    if getattr(self.active_overlay, 'is_dismissed', False):
                        self.active_overlay = None
                        self._process_pending_overlays()

            elif self.current_app and hasattr(self.current_app, 'handle_key'):
                # Pass the full tuple down to the app
                await self.current_app.handle_key(action, char)
        except Exception as e:
            import traceback
            print(f"[CRITICAL] OS Input Router intercepted a fatal exception: {e}")
            traceback.print_exc()

    async def _check_and_heal_pcm5122(self):
        # Stage: Non-Intrusive PCM5122 Self-Heal Watchdog (Re-latches 0x4C strictly when Display.reset_pins_and_recover increments shm[32:36])
        if not self._is_i2s_effectively_active() or getattr(self, 'i2s_manually_killed', False):
            return
        import struct as _struct

        disp = getattr(self, 'display', None)
        shm = getattr(disp, '_shm', None)
        if shm is None:
            return
        # Comment (invariant): Never poll twi_m[0x10] == 0x00 here or pass pulse_standby=True unconditionally; TWI0_SRST leaves STAT=0x00 and pulse_standby=True writes Reg 0x02=0x11 (hardware mute), causing a 3-second audio/scroll stutter loop across every screen (DPR-372).
        rcnt = _struct.unpack("<I", shm[32:36])[0]
        if not hasattr(self, '_last_disp_rcnt'):
            self._last_disp_rcnt = rcnt
            return
        if rcnt != self._last_disp_rcnt:
            self._last_disp_rcnt = rcnt
            await asyncio.sleep(0.20)
            print("[UIManager] Display I2C0 pin reset detected. Re-latching PCM5122 DAC...", flush=True)
            await self._apply_pcm5122_i2c_gain(pulse_standby=False)

    # Stage: Virtual Device Control Adapter (Unix-domain JSON RPC server at /tmp/vipxpert_devctl.sock)
    def get_devctl_state(self) -> dict:
        """Returns a structured JSON-serializable snapshot of active UI, menu rows, lock scene, audio, and power state."""
        import struct as _st
        app = getattr(self, 'current_app', None)
        menu_info = None
        if app is not None and hasattr(app, 'items') and hasattr(app, 'cursor_index'):
            raw_items = getattr(app, 'items', []) or []
            menu_info = {
                "current_menu": getattr(app, 'current_menu', None),
                "title": getattr(app, 'title', ''),
                "cursor_index": int(getattr(app, 'cursor_index', 0)),
                "scroll_offset": int(getattr(app, 'scroll_offset', 0)),
                "is_tuning": bool(getattr(app, 'is_tuning', False)),
                "item_count": len(raw_items),
                "items": [
                    {
                        "index": idx,
                        "name": str(it.get("name", "")) if isinstance(it, dict) else str(it),
                        "suffix": str(it.get("suffix", "")) if isinstance(it, dict) else "",
                    }
                    for idx, it in enumerate(raw_items)
                ],
            }

        lock_info = None
        if app is not None and hasattr(app, '_get_active_scene_id'):
            try:
                cur_sc = app._get_active_scene_id()
            except Exception:
                cur_sc = getattr(app, 'standalone_scene', None)
            lock_info = {
                "active_slot": getattr(app, 'active_slot', None),
                "active_scene_id": cur_sc,
                "standalone_scene": getattr(app, 'standalone_scene', None),
                "pick_db_key": getattr(app, 'pick_db_key', None),
                "category_scenes": list(getattr(app, 'category_scenes', []) or []),
                "manual_override": bool(getattr(app, 'manual_override', False)),
                "hash_step": int(getattr(app, '_hash_step', 0)),
                "viz_fullscreen": bool(getattr(app, 'viz_fullscreen', False)),
                "viz_mode": app.get_effective_viz_mode(cur_sc) if hasattr(app, 'get_effective_viz_mode') and cur_sc else None,
                "viz_bands": app.get_effective_viz_bands(cur_sc) if hasattr(app, 'get_effective_viz_bands') and cur_sc else None,
            }

        arb = getattr(self, 'media_arbiter', None)
        bt_ms = getattr(getattr(self, 'bt_daemon', None), 'media_state', {}) or {}
        loc_st = getattr(getattr(arb, 'local_audio', None), 'state', {}) or {}
        usb_st = getattr(arb, 'usb_state', {}) or {}
        disp = getattr(self, 'display', None)
        shm = getattr(disp, '_shm', None)
        shm_hdr = {}
        if shm is not None:
            try:
                ver, seq, pwm_f, slp_f, slot_i = _st.unpack("<IIIII", shm[0:20])
                shm_hdr = {"ver": ver, "seq": seq, "pwm_mode": pwm_f, "sleep_flag": slp_f, "slot": slot_i}
            except Exception:
                pass

        return {
            "ok": True,
            "current_app_id": getattr(self, 'current_app_id', None),
            "previous_app_id": getattr(self, 'previous_app_id', None),
            "is_screen_off": bool(getattr(self, 'is_screen_off', False)),
            "has_overlay": self.active_overlay is not None,
            "overlay_type": type(self.active_overlay).__name__ if self.active_overlay is not None else None,
            "toast": self._toast_message,
            "hw_profile": dict(getattr(self, 'hw_profile', {})),
            "menu": menu_info,
            "lock": lock_info,
            "audio": {
                "active_source": getattr(arb, 'active_source', 'NONE') if arb else 'NONE',
                "active_dac_pref": self.db.get_setting("active_dac", "usb") if getattr(self, 'db', None) else None,
                "active_hardware_dac": getattr(self, 'active_hardware_dac', None),
                "usb_audio_mode": self.db.get_setting("usb_audio_mode", "auto") if getattr(self, 'db', None) else "auto",
                "usb_capture_node": getattr(arb, '_usb_source_node', None) if arb else None,
                "bt_status": str(bt_ms.get('status', 'stopped')),
                "local_status": str(loc_st.get('status', 'stopped')),
                "usb_status": str(usb_st.get('status', 'stopped')),
                "transparency_enabled": bool(getattr(self, 'transparency_enabled', False)),
            },
            "power": dict(getattr(self, 'power_state', {})),
            "display": {
                "is_asleep": bool(getattr(disp, 'is_asleep', False)),
                "dither_mode": getattr(disp, 'dither_mode', 'off'),
                "shm": shm_hdr,
            },
        }

    async def _handle_devctl_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Executes single-line JSON RPC commands against the live UIManager event loop."""
        import json
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw:
                return
            req = json.loads(raw.decode('utf-8', errors='replace').strip())
            cmd = str(req.get('cmd', 'state')).lower()
            wait_s = max(0.0, min(2.0, float(req.get('wait_ms', 60)) / 1000.0))

            if cmd == 'state':
                resp = self.get_devctl_state()
            elif cmd == 'press':
                key = str(req.get('key', ''))
                act = str(req.get('action', 'CLICK')).upper()
                await self.inject_key((act, key))
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                resp = self.get_devctl_state()
            elif cmd == 'tap':
                key = str(req.get('key', ''))
                await self.inject_key(('DOWN', key))
                await asyncio.sleep(0.015)
                await self.inject_key(('CLICK', key))
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                resp = self.get_devctl_state()
            elif cmd == 'hold':
                key = str(req.get('key', ''))
                await self.inject_key(('DOWN', key))
                await asyncio.sleep(0.03)
                await self.inject_key(('HOLD', key))
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                resp = self.get_devctl_state()
            elif cmd == 'goto':
                app_id = str(req.get('app_id', 'home'))
                if getattr(self, 'is_screen_off', False):
                    await self.inject_key(('DOWN', '*'))
                    await self.inject_key(('UP', '*'))
                if app_id != 'lock_screen' and self.current_app_id == 'lock_screen':
                    self.db.set_setting("is_locked", "False")
                    await self.destroy_app('lock_screen')
                await self.switch_app(app_id)
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                resp = self.get_devctl_state()
            elif cmd == 'select':
                target = str(req.get('name', '')).strip().lower()
                do_click = bool(req.get('click', True))
                app = getattr(self, 'current_app', None)
                items = getattr(app, 'items', None) if app is not None else None
                if not items:
                    resp = {"ok": False, "error": "active_app_has_no_menu_items", "state": self.get_devctl_state()}
                else:
                    found_idx = None
                    for idx, it in enumerate(items):
                        nm = (it.get("name", "") if isinstance(it, dict) else str(it)).strip().lower()
                        if nm == target:
                            found_idx = idx
                            break
                    if found_idx is None:
                        for idx, it in enumerate(items):
                            nm = (it.get("name", "") if isinstance(it, dict) else str(it)).strip().lower()
                            if target and target in nm:
                                found_idx = idx
                                break
                    if found_idx is None:
                        resp = {"ok": False, "error": f"item_not_found:{target}", "state": self.get_devctl_state()}
                    else:
                        cur = int(getattr(app, 'cursor_index', 0))
                        n_items = len(items)
                        fwd = (found_idx - cur) % n_items
                        bwd = (cur - found_idx) % n_items
                        step_key = 'B' if fwd <= bwd else 'A'
                        steps_n = min(fwd, bwd)
                        for _ in range(steps_n):
                            await self.inject_key(('DOWN', step_key))
                            await self.inject_key(('CLICK', step_key))
                        if do_click:
                            await self.inject_key(('DOWN', 'C'))
                            await self.inject_key(('CLICK', 'C'))
                        if wait_s > 0:
                            await asyncio.sleep(wait_s)
                        resp = self.get_devctl_state()
            elif cmd == 'usb_takeover':
                arb = getattr(self, 'media_arbiter', None)
                sim = bool(req.get('simulate', False))
                if arb:
                    if sim:
                        arb._usb_forced_active = True
                        arb.usb_state['status'] = 'playing'
                        arb.usb_state['title'] = 'USB-C Hub Audio'
                        arb.usb_state['updated_at'] = time.time()
                        arb.force_source('USB')
                    else:
                        await arb.trigger_usb_takeover()
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                resp = self.get_devctl_state()
            elif cmd == 'release_sim_usb':
                arb = getattr(self, 'media_arbiter', None)
                if arb:
                    arb._usb_forced_active = False
                    arb.usb_state['status'] = 'stopped'
                    if arb.active_source == 'USB':
                        arb.active_source = 'NONE'
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                resp = self.get_devctl_state()
            else:
                resp = {"ok": False, "error": f"unknown_cmd:{cmd}"}

            writer.write((json.dumps(resp) + "\n").encode('utf-8'))
            await writer.drain()
        except Exception as e:
            try:
                writer.write((json.dumps({"ok": False, "error": str(e)}) + "\n").encode('utf-8'))
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _ensure_devctl_server(self):
        """Lazily binds /tmp/vipxpert_devctl.sock inside the active OS event loop."""
        import os
        if getattr(self, '_devctl_server', None) is not None:
            return
        sock_path = "/tmp/vipxpert_devctl.sock"
        try:
            if os.path.exists(sock_path):
                os.unlink(sock_path)
            self._devctl_server = await asyncio.start_unix_server(self._handle_devctl_client, path=sock_path)
            os.chmod(sock_path, 0o666)
            print(f"[UIManager] Device Control Adapter listening on {sock_path}", flush=True)
        except Exception as e:
            print(f"[UIManager] Device Control Adapter bind warning: {e}", flush=True)

    async def telemetry_ticker_loop(self):
        """Pure 1Hz background telemetry updater; high-FPS marquee V-Sync is driven directly by OSKernel._main_loop."""
        await self._ensure_devctl_server()
        dac_tick = 0
        while True:
            if not getattr(self, 'is_screen_off', False):
                try:
                    if self.active_overlay and hasattr(self.active_overlay, 'update'):
                        await self.active_overlay.update()
                    elif self.current_app:
                        await self.current_app.update()
                    self.request_render()
                except Exception as e:
                    print(f"[UI Manager] Telemetry exception dropped: {e}")
            dac_tick = (dac_tick + 1) % 3
            if dac_tick == 0:
                try:
                    await self._check_and_heal_pcm5122()
                except Exception:
                    pass
            await asyncio.sleep(1.0)

    async def activity_timeout_loop(self):
        """Two-stage asynchronous monitor for Lock Screen and OLED Power."""
        while True:
            t_lock = int(self.db.get_setting("timeout_lock", "30"))
            t_disp = int(self.db.get_setting("timeout_display", "60"))
            elapsed = time.time() - self.last_activity

            # Stage 1: Lock Screen
            if t_lock > 0 and elapsed >= t_lock and self.current_app_id != 'lock_screen':
                self.db.set_setting("is_locked", "True")
                await self.switch_app('lock_screen')

            # Stage 2: Display Sleep
            if t_disp > 0 and elapsed >= t_disp and not getattr(self, 'is_screen_off', False):
                self.display.sleep()
                self.is_screen_off = True
                if self.current_app and hasattr(self.current_app, 'on_screen_sleep'):
                    await self.current_app.on_screen_sleep()

            await asyncio.sleep(1.0)
