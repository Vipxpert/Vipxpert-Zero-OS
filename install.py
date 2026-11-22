# /vipxpert_minimal_os/install.py
import os
import sys
import subprocess
import getpass
import urllib.request

# --- Pre-flight Checks ---
if os.geteuid() != 0:
    print("🚨 FATAL: This script must be run as root. Try: sudo python3 install.py")
    sys.exit(1)
    
SUDO_USER = os.environ.get('SUDO_USER')
if not SUDO_USER or SUDO_USER == 'root':
    print("🚨 FATAL: Could not detect the original user. Please run with sudo, not by logging in as root directly.")
    sys.exit(1)
    
USER_UID = subprocess.run(['id', '-u', SUDO_USER], capture_output=True, text=True).stdout.strip()
FORCE_OVERWRITE = False

def prompt(step_name):
    """Silently allows execution since the main menu now handles selection routing."""
    print(f"\n>>> Executing: {step_name}...")
    return True

def run_cmd(cmd, as_user=None):
    """Executes a bash command, optionally dropping privileges to the sudo_user."""
    if as_user:
        # Wrap command to run as the target user with their environment
        cmd = f"sudo -u {as_user} XDG_RUNTIME_DIR=/run/user/{USER_UID} {cmd}"
    subprocess.run(cmd, shell=True, executable='/bin/bash')

def write_file(filepath, content, permissions=0o644):
    """Idempotent file writer. Obeys FORCE_OVERWRITE flag."""
    if not FORCE_OVERWRITE and os.path.exists(filepath):
        with open(filepath, 'r') as f:
            if f.read().strip() == content.strip():
                print(f"  -> [SKIP] {filepath} is already correct.")
                return
    with open(filepath, 'w') as f:
        f.write(content.strip() + "\n")
    os.chmod(filepath, permissions)
    print(f"  -> [OK] Wrote {filepath}")

def step_purge_ghosts():
    """Aggressively hunts down and destroys ghost libraries and manual config hacks."""
    if not FORCE_OVERWRITE:
        return
        
    print("\n  -> [PURGE] Nuking old configs, ghost libraries, and manual audio hacks...")
    
    # 1. Nuke local user wireplumber configs (where the deadly ALSA patch lives)
    run_cmd(f"rm -rf /home/{SUDO_USER}/.config/wireplumber")
    
    # 2. Nuke system wireplumber configs just in case
    run_cmd("rm -rf /etc/wireplumber")
    
    # 3. Hunt down and destroy ghost shared libraries from old manual installs
    run_cmd("find /usr/local/lib -name 'libpipewire-*.so*' -delete 2>/dev/null")
    run_cmd("find /usr/local/lib -name 'libwireplumber-*.so*' -delete 2>/dev/null")
    run_cmd("find /usr/local/lib -name 'libldacBT*.so*' -delete 2>/dev/null")
    
    # 4. Remove old Bluetooth firmware blobs to guarantee a fresh download
    run_cmd("rm -f /lib/firmware/rtl_bt/rtl8761b*.bin")
    
    # 5. Refresh the Linux dynamic linker cache to instantly forget the deleted ghost files
    run_cmd("ldconfig")
    
    ## 6. PERMANENTLY exterminate competing ghost servers AND distro PipeWire
    print("  -> [PURGE] Eradicating PulseAudio, Bluez-ALSA, and distro PipeWire ghosts...")
    run_cmd("apt-get purge -y pulseaudio pulseaudio-module-bluetooth bluez-alsa-utils pipewire wireplumber pipewire-pulse libpipewire-0.3-* libwireplumber-*")
    run_cmd("apt-get autoremove -y")
    
    # 7. Mask them so DBus and systemd can NEVER auto-start them again, even if accidentally reinstalled
    run_cmd("systemctl --user mask pulseaudio.service pulseaudio.socket", as_user=SUDO_USER)
    
    # 8. Mask LightDM display manager audio services & disable Xorg greeter to prevent background session/RAM hijacking
    run_cmd("mkdir -p /var/lib/lightdm/.config/systemd/user")
    for srv in ["pipewire.service", "pipewire.socket", "wireplumber.service", "pipewire-pulse.service", "pipewire-pulse.socket"]:
        run_cmd(f"ln -sf /dev/null /var/lib/lightdm/.config/systemd/user/{srv}")
    run_cmd("pkill -9 -u 109 pipewire wireplumber 2>/dev/null || true")
    run_cmd("pkill -9 -u lightdm 2>/dev/null || true")
    run_cmd("loginctl terminate-user lightdm 2>/dev/null || true")
    run_cmd("systemctl disable --now lightdm 2>/dev/null || true")
    run_cmd("systemctl set-default multi-user.target 2>/dev/null || true")
    write_file("/etc/modprobe.d/no-beep.conf", "blacklist pcspkr\nblacklist snd_pcsp\n")
    run_cmd("modprobe -r pcspkr snd_pcsp 2>/dev/null || true")
    
    print("  -> [PURGE] Audio subsystem wiped clean. PipeWire and LightDM conflicts eradicated.")

# --- Installation Steps ---

def step_apt_packages():
    if prompt("Install APT dependencies"):
        # PipeWire is now built from source. APT provides build tools and hardware dev libs.
        # APT Package Manifest (Includes build tools, gcc/rustc/cargo for zero-GC DSP workers, adb for Android USB audio bridge, mpv, and ffmpeg/ffprobe)
        packages = "gcc cmake git meson ninja-build pkg-config rustc cargo adb python3 python3-pip python3-venv i2c-tools libgpiod-dev python3-dev libjpeg-dev zlib1g-dev libfreetype-dev libasound2-dev libdbus-1-dev libglib2.0-dev libudev-dev mpv ffmpeg socat cpufrequtils rtkit v4l-utils libbluetooth-dev libsbc-dev libfreeaptx-dev libfdk-aac-dev"
        
        missing = []
        for pkg in packages.split():
            if subprocess.run(['dpkg', '-s', pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                missing.append(pkg)
                
        if FORCE_OVERWRITE:
            print("  -> [FORCE] Aggressively reinstalling all APT dependencies to repair broken packages...")
            run_cmd(f"apt-get update && apt-get install --reinstall -y {packages}")
        elif not missing:
            print("  -> [SKIP] All APT dependencies are already installed.")
        else:
            print(f"  -> Missing packages detected: {' '.join(missing)}")
            print("  -> Updating APT cache and installing...")
            run_cmd(f"apt-get update && apt-get install -y {packages}")
            
        # Attempt to install optional high-res Bluetooth codec dev packages individually
        codec_pkgs = "libfdk-aac-dev libfreeaptx-dev libldacbt-dev libldacbt-abr-dev libldacbt-enc-dev libopus-dev liblc3-dev"
        print("  -> Scanning OS repository for optional high-res Bluetooth codecs...")
        for pkg in codec_pkgs.split():
            # In overwrite mode, force reinstall codecs to recover from any aggressive manual file deletions
            if not FORCE_OVERWRITE and subprocess.run(['dpkg', '-s', pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                print(f"  -> [SKIP] {pkg} is already installed.")
                continue
                
            res = subprocess.run(['apt-cache', 'show', pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0:
                print(f"  -> Installing {pkg}...")
                run_cmd(f"apt-get install {'--reinstall ' if FORCE_OVERWRITE else ''}-y {pkg}")
            else:
                print(f"  -> [SKIP] {pkg} is not available in your OS repository.")

def step_groups_and_udev():
    if prompt("Configure Hardware Groups and udev rules (I2C, GPIO, IR, Bluetooth)"):
        # Ensure group exists and add user (including bluetooth & audio to prevent DBus HFP method_return rejections)
        run_cmd("groupadd -f gpio")
        run_cmd("groupadd -f plugdev")
        run_cmd(f"usermod -aG i2c,gpio,input,bluetooth,audio,plugdev {SUDO_USER}")
        
        # Ensure DBus explicitly permits WirePlumber (running as SUDO_USER) to return HFP Profile1/Agent calls to bluetoothd
        dbus_bt_conf = f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="{SUDO_USER}">
    <allow send_destination="org.bluez"/>
    <allow send_interface="org.bluez.Profile1"/>
    <allow send_interface="org.bluez.HandsfreeAudioAgent1"/>
    <allow send_type="method_return"/>
    <allow send_type="error"/>
  </policy>
</busconfig>"""
        write_file('/etc/dbus-1/system.d/vipxpert-bt-hfp.conf', dbus_bt_conf)
        run_cmd("systemctl reload dbus 2>/dev/null || true")
        
        # Advertise Wearable Audio Headset/Mic Class of Device (0x240404) so Android/iOS routes HFP voice/mic calls to the board
        if os.path.exists('/etc/bluetooth/main.conf'):
            run_cmd("sed -i 's/^#*Class =.*/Class = 0x240404/' /etc/bluetooth/main.conf")
            run_cmd("grep -q '^Class = 0x240404' /etc/bluetooth/main.conf || sed -i '/[General]/a Class = 0x240404' /etc/bluetooth/main.conf")
        
        # Write udev rules
        write_file('/etc/udev/rules.d/99-gpio.rules', 'SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660"')
        write_file('/etc/udev/rules.d/99-lirc.rules', 'SUBSYSTEM=="lirc", KERNEL=="lirc*", GROUP="gpio", MODE="0660"')
        write_file('/etc/udev/rules.d/99-i2c.rules', 'SUBSYSTEM=="i2c-dev", KERNEL=="i2c-[0-9]*", GROUP="i2c", MODE="0660"')
        bt_udev_rules = (
            'ACTION=="add", SUBSYSTEM=="usb", TEST=="power/control", ATTR{idVendor}=="0bda", ATTR{power/control}="on"\n'
            'ACTION=="add", SUBSYSTEM=="usb", TEST=="power/control", ATTR{idVendor}=="2357", ATTR{power/control}="on"\n'
            'ACTION=="add", SUBSYSTEM=="usb", TEST=="power/control", ATTR{idVendor}=="1a40", ATTR{power/control}="on"\n'
            'ACTION=="add", SUBSYSTEM=="usb", TEST=="power/control", ATTR{idVendor}=="214b", ATTR{power/control}="on"'
        )
        write_file('/etc/udev/rules.d/50-bluetooth-nosuspend.rules', bt_udev_rules)

        # Universal Android ADB USB Audio permissions (covers major OEM VIDs + USB Class ff/42/01 ADB interface)
        android_udev_rules = (
            'SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", MODE="0666", GROUP="plugdev"\n'
            'SUBSYSTEM=="usb", ATTR{idVendor}=="04e8", MODE="0666", GROUP="plugdev"\n'
            'SUBSYSTEM=="usb", ATTR{idVendor}=="22b8", MODE="0666", GROUP="plugdev"\n'
            'SUBSYSTEM=="usb", ATTR{idVendor}=="2717", MODE="0666", GROUP="plugdev"\n'
            'SUBSYSTEM=="usb", ATTR{idVendor}=="2a70", MODE="0666", GROUP="plugdev"\n'
            'SUBSYSTEM=="usb", ATTR{idVendor}=="0bb4", MODE="0666", GROUP="plugdev"\n'
            'SUBSYSTEM=="usb", ATTR{idVendor}=="12d1", MODE="0666", GROUP="plugdev"\n'
            'SUBSYSTEM=="usb", ATTR{idVendor}=="0fce", MODE="0666", GROUP="plugdev"\n'
            'SUBSYSTEM=="usb", ATTR{bInterfaceClass}=="ff", ATTR{bInterfaceSubClass}=="42", ATTR{bInterfaceProtocol}=="01", MODE="0666", GROUP="plugdev"'
        )
        write_file('/etc/udev/rules.d/51-android.rules', android_udev_rules)
        
        run_cmd("udevadm control --reload-rules && udevadm trigger")

def step_sudoers_and_scripts():
    if prompt("Configure Passwordless Sudo and DAC script"):
        # DAC script
        dac_script = """#!/bin/bash
PORT=$1
STATE=$2
CONFIG_FILE="/sys/bus/usb/devices/${PORT}/bConfigurationValue"
if [ ! -f "$CONFIG_FILE" ]; then exit 0; fi
if [ "$STATE" -eq 1 ]; then echo 1 > "$CONFIG_FILE"; else echo 0 > "$CONFIG_FILE"; fi"""
        write_file('/usr/local/bin/toggle_dac.sh', dac_script, 0o755)
        
        # Master sudoers rule
        cmds = "/usr/bin/nmcli, /usr/bin/chrt, /usr/bin/cpufreq-set, /usr/bin/tee, /usr/bin/l2ping, /usr/bin/usbreset, /usr/sbin/rfkill, /usr/sbin/rmmod, /usr/sbin/modprobe, /usr/sbin/i2cget, /usr/sbin/i2cset, /usr/bin/systemctl, /usr/bin/loginctl, /usr/bin/killall, /usr/bin/x11vnc, /usr/bin/setterm, /usr/bin/hciconfig, /bin/hciconfig, /usr/sbin/hciconfig, /usr/local/bin/toggle_dac.sh"
        write_file('/etc/sudoers.d/vipxpert_os_master', f"{SUDO_USER} ALL=(ALL) NOPASSWD: {cmds}", 0o440)

        # Provision ~/set_display.sh and ~/.xprofile for dynamic X11/VNC CVT resolution & DPI switching
        set_disp_script = """#!/bin/bash
# /home/vipxpert/set_display.sh
export DISPLAY=:0
OUTPUT=$(xrandr | grep " connected" | awk '{print $1}' | head -n 1)
MODE=$1
DPI=$2
if [ -z "$MODE" ] || [ -z "$DPI" ]; then
    echo "Usage: ./set_display.sh [resolution_fps] [dpi]"
    exit 1
fi
echo "$MODE $DPI" > ~/.last_display_profile
echo "Xft.dpi: $DPI" > ~/.Xresources
if [ ! -z "$OUTPUT" ]; then
    xrandr --output "$OUTPUT" --mode "$MODE" 2>/dev/null || true
    xrdb -merge ~/.Xresources 2>/dev/null || true
fi
echo "Swapped and Logged: Mode $MODE at ${DPI} DPI!"
"""
        xprofile_script = """#!/bin/bash
# /home/vipxpert/.xprofile
OUTPUT=$(xrandr | grep " connected" | awk '{print $1}' | head -n 1)
if [ ! -z "$OUTPUT" ]; then
    BASE_RESOLUTIONS=("854 480" "640 480" "1280 720" "1024 768" "1280 960" "1920 1080" "1600 1200" "2560 1440" "3840 2160")
    RATES=(30 60 90 120)
    for res in "${BASE_RESOLUTIONS[@]}"; do
        read -r W H <<< "$res"
        for FPS in "${RATES[@]}"; do
            CVT_L=$(cvt $W $H $FPS 2>/dev/null | grep "Modeline")
            if [ ! -z "$CVT_L" ]; then
                DATA_L=$(echo "$CVT_L" | sed 's/.*Modeline //' | tr -d '"')
                NAME_L=$(echo "$DATA_L" | awk '{print $1}')
                xrandr --newmode $DATA_L 2>/dev/null
                xrandr --addmode "$OUTPUT" "$NAME_L" 2>/dev/null
            fi
            CVT_V=$(cvt $H $W $FPS 2>/dev/null | grep "Modeline")
            if [ ! -z "$CVT_V" ]; then
                DATA_V=$(echo "$CVT_V" | sed 's/.*Modeline //' | tr -d '"')
                NAME_V=$(echo "$DATA_V" | awk '{print $1}')
                xrandr --newmode $DATA_V 2>/dev/null
                xrandr --addmode "$OUTPUT" "$NAME_V" 2>/dev/null
            fi
        done
    done
    if [ -f ~/.last_display_profile ]; then
        read -r SAVED_MODE SAVED_DPI < ~/.last_display_profile
        echo "Xft.dpi: $SAVED_DPI" > ~/.Xresources
        xrdb -merge ~/.Xresources
        xrandr --output "$OUTPUT" --mode "$SAVED_MODE"
    else
        echo "Xft.dpi: 65" > ~/.Xresources
        xrdb -merge ~/.Xresources
        xrandr --output "$OUTPUT" --mode "1280x720_30.00"
    fi
fi
"""
        write_file(f"{USER_HOME}/set_display.sh", set_disp_script, 0o755)
        write_file(f"{USER_HOME}/.xprofile", xprofile_script, 0o755)
        run_cmd(f"chown {SUDO_USER}:{SUDO_USER} {USER_HOME}/set_display.sh {USER_HOME}/.xprofile")

def step_armbian_env():
    if prompt("Inject Hardware Overlays into /boot/armbianEnv.txt"):
        env_path = '/boot/armbianEnv.txt'
        if not os.path.exists(env_path):
            print(f"  -> [SKIP] {env_path} not found. Skipping overlay injection.")
            return
            
        required_overlays = {'bananapi-m4-pi-5-6-i2c0', 'bananapi-m4-sdio-wifi-bt', 'ir'}
        conflicting_overlays = {'i2c3-ph'} # Clean up OLED killers
        
        with open(env_path, 'r') as f:
            lines = f.readlines()
        
        modified = False
        for i, line in enumerate(lines):
            if line.startswith('overlays='):
                current_overlays = set(line.strip().split('=')[1].split())
                
                # Remove conflicting hardware overlays
                for bad in conflicting_overlays:
                    if bad in current_overlays:
                        current_overlays.remove(bad)
                        modified = True
                        
                # Check if we need to add anything
                missing = required_overlays - current_overlays
                if missing:
                    current_overlays.update(required_overlays)
                    modified = True
                    
                if modified:
                    lines[i] = "overlays=" + " ".join(current_overlays) + "\n"
                break
        else:
            # If the 'overlays=' line didn't exist at all
            lines.append("overlays=" + " ".join(required_overlays) + "\n")
            modified = True

        if modified:
            with open(env_path, 'w') as f:
                f.writelines(lines)
            print(f"  -> [OK] Updated standard overlays in {env_path}")
        else:
            print(f"  -> [SKIP] Standard overlays are already correct.")
            
        # Compile and inject custom IR Blaster Overlay (Moved to Pin 31 for I2S audio upgrade)
        print("  -> Compiling and installing custom IR Blaster overlay (Pin 31)...")
        
        # Clean up old ghost IR overlays to prevent boot conflicts
        run_cmd("sed -i 's/ir-blaster-pc1//g' /boot/armbianEnv.txt")
        run_cmd("rm -f /boot/overlay-user/ir-blaster-pc1.dtbo")
        run_cmd("sed -i 's/ ir-blaster //g' /boot/armbianEnv.txt")
        run_cmd("rm -f /boot/overlay-user/ir-blaster.dtbo")
        
        ir_blaster_dts = """/dts-v1/;
/plugin/;

/ {
    compatible = "allwinner,sun50i-h618", "allwinner,sun50i-h616";

    fragment@0 {
        target = <&pio>;
        __overlay__ {
            ir_tx_pins: ir_tx_pins {
                pins = "PI9";
                function = "gpio_out";
                bias-pull-down;
            };
        };
    };

    fragment@1 {
        target-path = "/";
        __overlay__ {
            ir_tx {
                compatible = "gpio-ir-tx";
                pinctrl-names = "default";
                pinctrl-0 = <&ir_tx_pins>;
                gpios = <&pio 8 9 0>; /* Bank I (8), Pad 9 = Physical Pin 31 */
            };
        };
    };
};"""
        write_file('/tmp/ir-blaster-pi9.dts', ir_blaster_dts)
        run_cmd("armbian-add-overlay /tmp/ir-blaster-pi9.dts")
        print("  -> [OK] Custom IR Blaster overlay (Pin 31) installed.")
        
        _inject_i2s0_audio_overlay()

def _inject_i2s0_audio_overlay():
    """Compiles and injects the full-duplex Allwinner H618 ahub0 I2S0 overlay on PI1-PI4 with internal pull-downs."""
    print("  -> Compiling and installing Full-Duplex I2S0 Audio overlay (Pins 12, 35, 38, 40)...")
    run_cmd("sed -i 's/i2s3-audio//g' /boot/armbianEnv.txt")
    run_cmd("rm -f /boot/overlay-user/i2s3-audio.dtbo")
    
    i2s0_dts = """/dts-v1/;
/plugin/;

/ {
    compatible = "allwinner,sun50i-h618", "allwinner,sun50i-h616";

    fragment@0 {
        target = <&pio>;
        __overlay__ {
            ahub_daudio0_pins_a: ahub_daudio0@0 {
                pins = "PI1", "PI2";
                function = "i2s0";
                drive-strength = <30>;
                bias-pull-down;
            };
            ahub_daudio0_pins_b: ahub_daudio0@1 {
                pins = "PI3";
                function = "i2s0_dout0";
                drive-strength = <10>;
                bias-pull-down;
            };
            ahub_daudio0_pins_c: ahub_daudio0@2 {
                pins = "PI4";
                function = "i2s0_din0";
                drive-strength = <10>;
                bias-pull-down;
            };
            ahub_daudio0_pins_d: ahub_daudio0_sleep {
                pins = "PI1", "PI2", "PI3", "PI4";
                function = "gpio_in";
                bias-pull-down;
            };
        };
    };

    fragment@1 {
        target-path = "/soc";
        __overlay__ {
            ahub0_plat: ahub0_plat {
                #sound-dai-cells = <0x00>;
                compatible = "allwinner,sunxi-snd-plat-ahub";
                apb_num = <0x00>;
                dmas = <0x1c 0x03 0x1c 0x03>;
                dma-names = "tx", "rx";
                playback_cma = <0x80>;
                capture_cma = <0x80>;
                tx_fifo_size = <0x80>;
                rx_fifo_size = <0x80>;
                tdm_num = <0x00>;
                tx_pin = <0x00>;
                rx_pin = <0x00>;
                pinctrl-names = "default", "sleep";
                pinctrl-0 = <&ahub_daudio0_pins_a>, <&ahub_daudio0_pins_b>, <&ahub_daudio0_pins_c>;
                pinctrl-1 = <&ahub_daudio0_pins_d>;
                status = "okay";
            };

            ahub0_mach: ahub0_mach {
                compatible = "allwinner,sunxi-snd-mach";
                soundcard-mach,name = "ahubi2s0";
                soundcard-mach,format = "i2s";
                soundcard-mach,frame-master = <&ahub0_cpu>;
                soundcard-mach,bitclock-master = <&ahub0_cpu>;
                soundcard-mach,slot-num = <0x02>;
                soundcard-mach,slot-width = <0x20>;
                status = "okay";

                ahub0_cpu: soundcard-mach,cpu {
                    sound-dai = <&ahub0_plat>;
                    soundcard-mach,pll-fs = <0x04>;
                    soundcard-mach,mclk-fs = <0x00>;
                };

                ahub0_codec: soundcard-mach,codec {
                };
            };
        };
    };
};"""
    write_file('/tmp/i2s0-audio.dts', i2s0_dts)
    run_cmd("armbian-add-overlay /tmp/i2s0-audio.dts")
    
    # Guarantee both user overlays remain registered in armbianEnv.txt
    env_path = '/boot/armbianEnv.txt'
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            content = f.read()
        if 'user_overlays=' in content:
            for ov in ['ir-blaster-pi9', 'i2s0-audio']:
                if ov not in content:
                    run_cmd(f"sed -i 's/^user_overlays=/user_overlays={ov} /' {env_path}")
        else:
            with open(env_path, 'a') as f:
                f.write("user_overlays=ir-blaster-pi9 i2s0-audio\n")
    print("  -> [OK] Full-Duplex I2S0 Audio overlay with internal pull-downs installed.")

def step_compile_ldac():
    # NOTE FOR MODDERS (LDAC SINK STATUS):
    # While anonymix007/libldacdec compiles and links cleanly with PipeWire's bluez5 LDAC decoder plugin
    # (allowing phones to negotiate A2DP LDAC HQ with the board), LDAC playback is currently SILENT for
    # unknown reasons (likely floating-point/PCM sample scale or frame header mismatches inside libldacdec).
    # Keep AAC / aptX HD / SBC-XQ active for daily listening until upstream or a modder resolves the silent output.
    if prompt("Check and ensure Sony LDAC codec library (Encoder & Decoder)"):
        # Check if our custom decoder is already installed
        if os.path.exists('/usr/lib/libldacBT_dec.so') and not FORCE_OVERWRITE:
            print("  -> [SKIP] LDAC Decoder is already natively installed.")
            return
            
        print("  -> LDAC Decoder missing. Compiling anonymix007 LDAC from source...")
        
        # 1. Ensure APT dependencies for both encoder headers and the build tools
        run_cmd("apt-get install -y libldacbt-enc-dev libldacbt-abr-dev libsndfile1-dev libsamplerate0-dev")
        
        # 2. Build the decoder library with STRICT ARM64 memory alignment to prevent SIGBUS crashes
        ldac_cmd = (
            "cd /tmp && rm -rf libldacdec && "
            "git clone https://github.com/anonymix007/libldacdec.git && "
            "cd libldacdec && git submodule update --init && "
            "sed -i 's/-march=native//g' Makefile && "
            "sed -i 's/-O3/-O2 -mstrict-align -fno-strict-aliasing/g' Makefile && "
            "make libldacdec.so -j$(nproc)"
        )
        run_cmd(ldac_cmd)
        
        # 3. Forge the installation paths and header files
        forge_cmd = (
            "cd /tmp/libldacdec && "
            "cp libldacdec.so /usr/lib/libldacBT_dec.so && "
            "cp libldacdec.so /usr/lib/libldacBT_dec.so.2 && "
            "mkdir -p /usr/include/ldac && "
            "cp *.h /usr/include/ldac/ 2>/dev/null || true && "
            "ln -sf /usr/include/ldac/ldacdec.h /usr/include/ldac/ldacBT_dec.h 2>/dev/null || true && "
            "ln -sf /usr/include/ldac/libldacdec.h /usr/include/ldac/ldacBT_dec.h 2>/dev/null || true"
        )
        run_cmd(forge_cmd)
        
        # 4. Forge the pkg-config file
        import textwrap
        pc_content = textwrap.dedent("""\
            prefix=/usr
            libdir=${prefix}/lib
            includedir=${prefix}/include

            Name: ldacBT-dec
            Description: LDAC Bluetooth decoder library
            Version: 2.0.2.3
            Libs: -L${libdir} -lldacBT_dec
            Cflags: -I${includedir}/ldac""")
            
        write_file('/usr/lib/pkgconfig/ldacBT-dec.pc', pc_content)
        run_cmd("ldconfig")
        
        print("  -> [OK] LDAC Decoder compiled, forged, and installed.")

def step_compile_pipewire():
    if prompt("Compile PipeWire & WirePlumber from source (Audiophile Codecs)"):
        if not FORCE_OVERWRITE and os.path.exists('/usr/local/bin/pipewire'):
            print("  -> [SKIP] Audiophile PipeWire is already compiled.")
            return
            
        print("  -> Cloning and compiling PipeWire with LDAC, AAC, and aptX support...")
        pw_src_dir = f"/home/{SUDO_USER}/pipewire"
        run_cmd(f"rm -rf {pw_src_dir} && git clone https://gitlab.freedesktop.org/pipewire/pipewire.git {pw_src_dir}", as_user=SUDO_USER)
        
        # =========================================================================================
        # MODDER ADVISORY: UPSTREAM PIPEWIRE 1.7.x AUDIOCONVERT NULL-DEREFERENCE PATCH
        # =========================================================================================
        # In PipeWire 1.7.0 git master, opening a 1-channel Mono Bluetooth HFP SCO microphone stream
        # alongside a 2-channel Stereo node causes a fatal SIGSEGV in WirePlumber's data-loop thread
        # inside `spa/plugins/audioconvert/audioconvert.c` (`impl_node_process`):
        #   - Specifically, when `port->ramp_start` is true on an empty input port (`buf == NULL`),
        #     `struct gaps_state *gs = this->gaps.states[remap];` can evaluate to NULL (`0x0`) before
        #     `recalc_stages()` initializes `gaps.states`, crashing on `gs->mode = GAPS_MODE_FADE_OUT;`
        #     (`strb w0, [x4, #8]`).
        #   - Earlier 1.7.0 snapshots also had an unguarded write to `[x4, #8]` in the `else` branch.
        #
        # ADVICE FOR FUTURE MODDERS:
        # Inspect `/home/<user>/pipewire/spa/plugins/audioconvert/audioconvert.c` around line 4175-4255
        # when pulling a newer upstream PipeWire version. If upstream has already added a NULL check for
        # `gs` (or refactored `this->gaps.states[remap]`), you can safely remove this Python patch block.
        # =========================================================================================
        audioconvert_path = os.path.join(pw_src_dir, "spa/plugins/audioconvert/audioconvert.c")
        if os.path.exists(audioconvert_path):
            with open(audioconvert_path, "r") as f:
                ac_src = f.read()
            old_snippet = (
                "struct gaps_state *gs = this->gaps.states[remap];\n"
                "\t\t\t\t\t\tspa_log_info(this->log, \"%p: %p ramp start\", this, port);\n"
                "\t\t\t\t\t\tgs->mode = GAPS_MODE_FADE_OUT;\n"
                "\t\t\t\t\t\tgs->count = 0;"
            )
            new_snippet = (
                "struct gaps_state *gs = this->gaps.states[remap];\n"
                "\t\t\t\t\t\tif (gs) {\n"
                "\t\t\t\t\t\t\tspa_log_info(this->log, \"%p: %p ramp start\", this, port);\n"
                "\t\t\t\t\t\t\tgs->mode = GAPS_MODE_FADE_OUT;\n"
                "\t\t\t\t\t\t\tgs->count = 0;\n"
                "\t\t\t\t\t\t}"
            )
            if old_snippet in ac_src:
                with open(audioconvert_path, "w") as f:
                    f.write(ac_src.replace(old_snippet, new_snippet, 1))
                print("  -> [PATCH] Applied NULL-guard patch to audioconvert.c (prevents HFP SCO SIGSEGV).")
            else:
                print("  -> [INFO] audioconvert.c pattern not matched (upstream may have already patched it).")

        # Patch channelmix-ops-c.c and channelmix-ops.c so channelmix.stereo-widen > 0.5f
        # sums both I2S MEMS capsules onto both output ports with tanhf() soft-clipping for HFP S16LE.
        cm_c_path = os.path.join(pw_src_dir, "spa/plugins/audioconvert/channelmix-ops-c.c")
        if os.path.exists(cm_c_path):
            with open(cm_c_path, "r") as f:
                cm_c_src = f.read()
            old_copy_c = (
                "void\n"
                "channelmix_copy_c(struct channelmix *mix, void * SPA_RESTRICT dst[],\n"
                "\t\tconst void * SPA_RESTRICT src[], uint32_t n_samples)\n"
                "{\n"
                "\tCHANNELMIX_DEF_MATRIX(matrix, mix, matrix);\n"
                "\tuint32_t i, n_dst = mix->dst_chan;\n"
                "\tfloat **d = (float **)dst;\n"
                "\tconst float **s = (const float **)src;\n"
                "\tfor (i = 0; i < n_dst; i++)\n"
                "\t\tvol_c(d[i], s[i], matrix[i][i], n_samples);\n"
                "}"
            )
            new_copy_c = (
                "void\n"
                "channelmix_copy_c(struct channelmix *mix, void * SPA_RESTRICT dst[],\n"
                "\t\tconst void * SPA_RESTRICT src[], uint32_t n_samples)\n"
                "{\n"
                "\tCHANNELMIX_DEF_MATRIX(matrix, mix, matrix);\n"
                "\tuint32_t i, n_dst = mix->dst_chan;\n"
                "\tfloat **d = (float **)dst;\n"
                "\tconst float **s = (const float **)src;\n"
                "\tif (n_dst == 2 && mix->src_chan == 2 && mix->widen > 0.5f) {\n"
                "\t\tconst float v0 = matrix[0][0];\n"
                "\t\tconst float v1 = matrix[1][1];\n"
                "\t\tfor (i = 0; i < n_samples; i++) {\n"
                "\t\t\tfloat m = s[0][i] * v0 + s[1][i] * v1;\n"
                "\t\t\tif (m > 0.95f || m < -0.95f)\n"
                "\t\t\t\tm = tanhf(m);\n"
                "\t\t\td[0][i] = m;\n"
                "\t\t\td[1][i] = m;\n"
                "\t\t}\n"
                "\t\treturn;\n"
                "\t}\n"
                "\tif (n_dst == 2 && mix->src_chan == 2 && mix->widen < -0.5f) {\n"
                "\t\tconst float v0 = matrix[0][0];\n"
                "\t\tconst float v1 = matrix[1][1];\n"
                "\t\tfor (i = 0; i < n_samples; i++) {\n"
                "\t\t\tfloat l = s[0][i] * v0;\n"
                "\t\t\tfloat r = s[1][i] * v1;\n"
                "\t\t\tif (l > 0.85f || l < -0.85f) l = tanhf(l);\n"
                "\t\t\tif (r > 0.85f || r < -0.85f) r = tanhf(r);\n"
                "\t\t\td[0][i] = l;\n"
                "\t\t\td[1][i] = r;\n"
                "\t\t}\n"
                "\t\treturn;\n"
                "\t}\n"
                "\tfor (i = 0; i < n_dst; i++)\n"
                "\t\tvol_c(d[i], s[i], matrix[i][i], n_samples);\n"
                "}"
            )
            if old_copy_c in cm_c_src:
                with open(cm_c_path, "w") as f:
                    f.write(cm_c_src.replace(old_copy_c, new_copy_c, 1))
                print("  -> [PATCH] Patched channelmix_copy_c with mono-sum (>0.5) and stereo tanhf limiter (<-0.5).")

        cm_ops_path = os.path.join(pw_src_dir, "spa/plugins/audioconvert/channelmix-ops.c")
        if os.path.exists(cm_ops_path):
            with open(cm_ops_path, "r") as f:
                cm_ops_src = f.read()
            old_flag = "\tSPA_FLAG_UPDATE(mix->flags, CHANNELMIX_FLAG_IDENTITY,\n\t\t\tdst_chan == src_chan && SPA_FLAG_IS_SET(mix->flags, CHANNELMIX_FLAG_COPY));"
            new_flag = "\tif (mix->widen > 0.5f || mix->widen < -0.5f)\n\t\tSPA_FLAG_CLEAR(mix->flags, CHANNELMIX_FLAG_COPY);\n" + old_flag
            if "mix->widen < -0.5f" not in cm_ops_src and old_flag in cm_ops_src:
                with open(cm_ops_path, "w") as f:
                    f.write(cm_ops_src.replace(old_flag, new_flag, 1))
                print("  -> [PATCH] Patched channelmix-ops.c to keep MIX_BIT active when |stereo-widen| > 0.5f.")

        # Patch spa/plugins/bluez5/decode-buffer.h so spa_bt_decode_buffer_get_auto_latency()
        # enforces a 4x duration (4096-sample / 85ms) minimum A2DP safety cushion, preventing
        # periodic 1024-sample zero-fill underruns when alsa_output runs at 1024 period-size.
        db_h_path = os.path.join(pw_src_dir, "spa/plugins/bluez5/decode-buffer.h")
        if os.path.exists(db_h_path):
            with open(db_h_path, "r") as f:
                db_h_src = f.read()
            old_db_block = (
                "\ttarget = SPA_CLAMP(SPA_ROUND_UP(SPA_MAX(spike * 3/2, duration),\n"
                "\t\t\t\tSPA_CLAMP((int)this->rate / 50, 1, INT32_MAX)),\n"
                "\t\t\tduration, max_buf - 2*packet_size);\n\n"
                "\ttarget = SPA_MAX(target, this->min_latency);\n"
                "\treturn SPA_MIN(target, duration + SPA_CLAMP(this->max_extra, 0, INT32_MAX - duration));"
            )
            new_db_block = (
                "\ttarget = SPA_CLAMP(SPA_ROUND_UP(SPA_MAX(spike * 2, duration * 4),\n"
                "\t\t\t\tSPA_CLAMP((int)this->rate / 50, 1, INT32_MAX)),\n"
                "\t\t\tduration * 4, max_buf - 2*packet_size);\n\n"
                "\ttarget = SPA_MAX(target, this->min_latency);\n"
                "\treturn target;"
            )
            if old_db_block in db_h_src:
                with open(db_h_path, "w") as f:
                    f.write(db_h_src.replace(old_db_block, new_db_block, 1))
                print("  -> [PATCH] Patched decode-buffer.h with 4x duration (4096-sample) A2DP jitter cushion.")

        # Patch backend-native.c, media-sink.c, and sco-io.c for 48-byte CVSD SCO MTU,
        # SPA_IO_RateMatch follower sizing, and multi-packet 1024-quantum SCO FIFO drain.
        bn_path = os.path.join(pw_src_dir, "spa/plugins/bluez5/backend-native.c")
        if os.path.exists(bn_path):
            with open(bn_path, "r") as f:
                bn_src = f.read()
            old_bn = "\t\tt->read_mtu = sco_opt.mtu;\n\t\tt->write_mtu = sco_opt.mtu;"
            new_bn = (
                "\t\tif (sco_opt.mtu == 255 || (sco_opt.mtu & 1) || sco_opt.mtu > 144) {\n"
                "\t\t\tt->read_mtu = 48;\n"
                "\t\t\tt->write_mtu = 48;\n"
                "\t\t} else {\n"
                "\t\t\tt->read_mtu = sco_opt.mtu;\n"
                "\t\t\tt->write_mtu = sco_opt.mtu;\n"
                "\t\t}"
            )
            if old_bn in bn_src:
                with open(bn_path, "w") as f:
                    f.write(bn_src.replace(old_bn, new_bn, 1))
                print("  -> [PATCH] Patched backend-native.c to clamp bogus Realtek USB SCO MTU (255) to 48 bytes.")

        ms_path = os.path.join(pw_src_dir, "spa/plugins/bluez5/media-sink.c")
        if os.path.exists(ms_path):
            with open(ms_path, "r") as f:
                ms_src = f.read()
            ms_src = ms_src.replace(
                "\tif (!this->transport_started)\n\t\tport->ratectl.corr = 1.0;\n\n\tif (port->rate_match) {",
                "\tif (!this->transport_started || this->codec->kind == MEDIA_CODEC_HFP)\n\t\tport->ratectl.corr = 1.0;\n\n\tif (port->rate_match) {\n\t\tif (this->codec->kind == MEDIA_CODEC_HFP) {\n\t\t\tport->rate_match->rate = 1.0;\n\t\t\tSPA_FLAG_UPDATE(port->rate_match->flags, SPA_IO_RATE_MATCH_FLAG_ACTIVE, this->following);\n\t\t\treturn 0;\n\t\t}",
                1
            )
            ms_src = ms_src.replace(
                "\t\tif (SPA_LIKELY(this->position)) {",
                "\t\tif (is_sco) {\n\t\t\tstruct timespec ts_now;\n\t\t\tuint64_t now_mono;\n\t\t\tspa_system_clock_gettime(this->data_system, CLOCK_MONOTONIC, &ts_now);\n\t\t\tnow_mono = SPA_TIMESPEC_TO_NSEC(&ts_now);\n\t\t\tif (this->next_flush_time < now_mono)\n\t\t\t\tthis->next_flush_time = now_mono;\n\t\t\tthis->next_flush_time += packet_time;\n\t\t} else if (SPA_LIKELY(this->position)) {",
                1
            )
            ms_src = ms_src.replace(
                "\t\treset_buffer(this);\n\t\tenable_flush_timer(this, true);",
                "\t\treset_buffer(this);\n\t\tif (is_sco) {\n\t\t\tenable_flush_timer(this, false);\n\t\t\tgoto again;\n\t\t}\n\t\tenable_flush_timer(this, true);",
                1
            )
            ms_src = ms_src.replace(
                "\tval = this->codec->send_buf_size > 0\n\t\t\t/* The kernel doubles the SO_SNDBUF option value set by setsockopt(). */\n\t\t\t? this->codec->send_buf_size / 2 + this->codec->send_buf_size % 2\n\t\t\t: FILL_FRAMES * this->transport->write_mtu;",
                "\tval = is_sco ? 65536 : (this->codec->send_buf_size > 0\n\t\t\t/* The kernel doubles the SO_SNDBUF option value set by setsockopt(). */\n\t\t\t? this->codec->send_buf_size / 2 + this->codec->send_buf_size % 2\n\t\t\t: FILL_FRAMES * this->transport->write_mtu);",
                1
            )
            ms_src = ms_src.replace(
                "if (this->codec->kind != MEDIA_CODEC_BAP)\n\t\t\t\treturn 0;",
                "if (this->codec->kind != MEDIA_CODEC_BAP && this->codec->kind != MEDIA_CODEC_HFP)\n\t\t\t\treturn 0;",
                1
            )
            ms_src = ms_src.replace(
                "if (this->codec->kind != MEDIA_CODEC_BAP)\n\t\t\treturn -ENOENT;",
                "if (this->codec->kind != MEDIA_CODEC_BAP && this->codec->kind != MEDIA_CODEC_HFP)\n\t\t\treturn -ENOENT;",
                1
            )
            with open(ms_path, "w") as f:
                f.write(ms_src)
            print("  -> [PATCH] Patched media-sink.c for HFP SPA_IO_RateMatch and full-quantum SCO packet drain.")

        sco_path = os.path.join(pw_src_dir, "spa/plugins/bluez5/sco-io.c")
        if os.path.exists(sco_path):
            with open(sco_path, "w") as f:
                f.write('/* Spa SCO I/O */\n/* SPDX-FileCopyrightText: Copyright © 2019 Collabora Ltd. */\n/* SPDX-License-Identifier: MIT */\n\n#include <unistd.h>\n#include <stddef.h>\n#include <stdio.h>\n#include <string.h>\n#include <errno.h>\n#include <arpa/inet.h>\n#include <sys/ioctl.h>\n#include <sys/socket.h>\n\n#include <spa/support/plugin.h>\n#include <spa/support/loop.h>\n#include <spa/support/log.h>\n#include <spa/support/system.h>\n#include <spa/utils/list.h>\n#include <spa/utils/keys.h>\n#include <spa/utils/names.h>\n#include <spa/utils/cleanup.h>\n#include <spa/monitor/device.h>\n\n#include <spa/node/node.h>\n#include <spa/node/utils.h>\n#include <spa/node/io.h>\n#include <spa/node/keys.h>\n#include <spa/param/param.h>\n#include <spa/param/audio/format.h>\n#include <spa/param/audio/format-utils.h>\n#include <spa/pod/filter.h>\n\n#include <sbc/sbc.h>\n\n#include "defs.h"\n#include "media-codecs.h"\n#include "hfp-codec-caps.h"\n\nSPA_LOG_TOPIC_DEFINE_STATIC(log_topic, "spa.bluez5.sco-io");\n#undef SPA_LOG_TOPIC_DEFAULT\n#define SPA_LOG_TOPIC_DEFAULT &log_topic\n\n#include "decode-buffer.h"\n\n#define MAX_MTU 1024\n#define TX_FIFO_SIZE 16384\n#define CVSD_PKT_SIZE 48\n\nstruct spa_bt_sco_io {\n        uint8_t read_buffer[MAX_MTU];\n        size_t read_size;\n\n        uint8_t write_buffer[TX_FIFO_SIZE];\n        size_t write_size;\n\n        int fd;\n        uint16_t read_mtu;\n        uint16_t write_mtu;\n\n        struct spa_log *log;\n        struct spa_loop *data_loop;\n        struct spa_system *data_system;\n        struct spa_source source;\n\n        struct spa_bt_recvmsg_data recv;\n\n        int (*source_cb)(void *userdata, uint8_t *data, int size, uint64_t rx_time);\n        void *source_userdata;\n\n        const struct media_codec *codec;\n        void *codec_data;\n\n        uint64_t last_tx_time;\n        uint64_t last_rx_time;\n        uint64_t stat_last_ns;\n        uint32_t rx_pkts;\n        uint32_t rx_bytes;\n        uint32_t tx_pkts;\n        uint32_t tx_bytes;\n        uint32_t write_calls;\n        uint32_t write_in_bytes;\n        int last_err;\n};\n\n\nstatic void flush_tx_fifo(struct spa_bt_sco_io *io)\n{\n        size_t pkt = (io->codec->id == SPA_BLUETOOTH_AUDIO_CODEC_CVSD) ? CVSD_PKT_SIZE : (io->read_size ? io->read_size : HFP_H2_PACKET_SIZE);\n        if (pkt == 0)\n                pkt = CVSD_PKT_SIZE;\n\n        /* Pace TX strictly to 1 packet per 3.75ms synchronous RX slot (or 2 if FIFO > 8 packets) */\n        int max_send = (io->write_size > pkt * 8) ? 2 : 1;\n        while (max_send-- > 0 && io->write_size >= pkt) {\n                ssize_t written = send(io->fd, io->write_buffer, pkt, MSG_DONTWAIT | MSG_NOSIGNAL);\n                if (written < 0) {\n                        if (errno == EINTR) {\n                                max_send++;\n                                continue;\n                        }\n                        io->last_err = errno;\n                        break;\n                }\n                if (written > 0) {\n                        memmove(io->write_buffer, io->write_buffer + written, io->write_size - written);\n                        io->write_size -= written;\n                        io->tx_pkts++;\n                        io->tx_bytes += written;\n                } else {\n                        break;\n                }\n        }\n}\n\nstatic void sco_io_on_ready(struct spa_source *source)\n{\n        struct spa_bt_sco_io *io = source->data;\n\n        if (SPA_FLAG_IS_SET(source->rmask, SPA_IO_IN)) {\n                int res;\n                int dummy;\n                uint64_t rx_time = 0;\n\n        read_again:\n                res = spa_bt_recvmsg(&io->recv, io->read_buffer, SPA_MIN(io->read_mtu, MAX_MTU), &rx_time, &dummy);\n                if (res <= 0) {\n                        if (errno == EINTR)\n                                goto read_again;\n                        else if (errno == EAGAIN || errno == EWOULDBLOCK)\n                                goto read_done;\n                        goto stop;\n                }\n\n                io->rx_pkts++;\n                io->rx_bytes += res;\n                io->last_rx_time = rx_time;\n                if (!io->last_tx_time)\n                        io->last_tx_time = rx_time;\n\n                if (io->codec->id == SPA_BLUETOOTH_AUDIO_CODEC_CVSD) {\n                        res &= ~1;\n                        io->read_size = CVSD_PKT_SIZE;\n                } else {\n                        io->read_size = res;\n                }\n\n                if (io->source_cb && res > 0) {\n                        int cb_res = io->source_cb(io->source_userdata, io->read_buffer, res, rx_time);\n                        if (cb_res)\n                                io->source_cb = NULL;\n                }\n\n                flush_tx_fifo(io);\n        }\n\nread_done:\n        if (SPA_FLAG_IS_SET(source->rmask, SPA_IO_ERR) || SPA_FLAG_IS_SET(source->rmask, SPA_IO_HUP))\n                goto stop;\n        return;\n\nstop:\n        if (io->source.loop)\n                spa_loop_remove_source(io->data_loop, &io->source);\n}\n\nint spa_bt_sco_io_write(struct spa_bt_sco_io *io, const uint8_t *buf, size_t size)\n{\n        const size_t orig_size = size;\n\n        io->write_calls++;\n        io->write_in_bytes += size;\n\n        size &= ~((size_t)1);\n        if (size == 0)\n                return orig_size;\n\n        if (size > TX_FIFO_SIZE - io->write_size) {\n                size_t drop = ((size - (TX_FIFO_SIZE - io->write_size) + (CVSD_PKT_SIZE - 1)) / CVSD_PKT_SIZE) * CVSD_PKT_SIZE;\n                if (drop >= io->write_size) {\n                        io->write_size = 0;\n                } else {\n                        memmove(io->write_buffer, io->write_buffer + drop, io->write_size - drop);\n                        io->write_size -= drop;\n                }\n        }\n\n        if (size <= TX_FIFO_SIZE - io->write_size) {\n                memcpy(io->write_buffer + io->write_size, buf, size);\n                io->write_size += size;\n        }\n\n        return orig_size;\n}\n\nvoid spa_bt_sco_io_write_start(struct spa_bt_sco_io *io)\n{\n        io->write_size = 0;\n}\n\nstruct spa_bt_sco_io *spa_bt_sco_io_create(struct spa_bt_transport *transport, struct spa_loop *data_loop,\n                struct spa_system *data_system, struct spa_log *log)\n{\n        spa_autofree struct spa_bt_sco_io *io = NULL;\n        struct spa_audio_info format = { 0 };\n        int sndbuf = 65536;\n\n        spa_log_topic_init(log, &log_topic);\n\n        io = calloc(1, sizeof(struct spa_bt_sco_io));\n        if (io == NULL)\n                return io;\n\n        io->fd = transport->fd;\n        io->read_mtu = transport->read_mtu;\n        io->write_mtu = transport->write_mtu;\n        io->data_loop = data_loop;\n        io->data_system = data_system;\n        io->log = log;\n        io->read_size = (transport->media_codec->id == SPA_BLUETOOTH_AUDIO_CODEC_CVSD) ? CVSD_PKT_SIZE : HFP_H2_PACKET_SIZE;\n        io->codec = transport->media_codec;\n\n        setsockopt(io->fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));\n\n        if (io->codec->validate_config(io->codec, 0, NULL, 0, &format) < 0)\n                return NULL;\n\n        io->codec_data = io->codec->init(io->codec, 0, NULL, 0, &format, NULL, transport->write_mtu);\n        if (!io->codec_data)\n                return NULL;\n\n        spa_bt_recvmsg_init(&io->recv, io->fd, io->data_system, io->log);\n\n        io->source.data = io;\n        io->source.fd = io->fd;\n        io->source.func = sco_io_on_ready;\n        io->source.mask = SPA_IO_IN | SPA_IO_ERR | SPA_IO_HUP;\n        io->source.rmask = 0;\n        spa_loop_add_source(io->data_loop, &io->source);\n\n        return spa_steal_ptr(io);\n}\n\nstatic int do_remove_source(struct spa_loop *loop,\n                            bool async,\n                            uint32_t seq,\n                            const void *data,\n                            size_t size,\n                            void *user_data)\n{\n        struct spa_bt_sco_io *io = user_data;\n\n        if (io->source.loop)\n                spa_loop_remove_source(io->data_loop, &io->source);\n\n        return 0;\n}\n\nvoid spa_bt_sco_io_destroy(struct spa_bt_sco_io *io)\n{\n        spa_log_debug(io->log, "%p: destroy", io);\n        spa_loop_locked(io->data_loop, do_remove_source, 0, NULL, 0, io);\n        io->codec->deinit(io->codec_data);\n        free(io);\n}\n\nvoid spa_bt_sco_io_set_source_cb(struct spa_bt_sco_io *io, int (*source_cb)(void *, uint8_t *, int, uint64_t), void *userdata)\n{\n        io->source_cb = source_cb;\n        io->source_userdata = userdata;\n        io->last_rx_time = 0;\n        io->last_tx_time = 0;\n}\n')
            print("  -> [PATCH] Patched sco-io.c with RX-slot-paced 16KB CVSD TX FIFO.")

        nas_path = os.path.join(pw_src_dir, "spa/plugins/support/null-audio-sink.c")
        if os.path.exists(nas_path):
            with open(nas_path, "r") as f:
                nas_src = f.read()
            old_nas = "\tif (SPA_LIKELY(this->position)) {\n\t\tduration = this->position->clock.target_duration;\n\t\trate = this->position->clock.target_rate.denom;\n\t}"
            new_nas = "\tif (SPA_LIKELY(this->position)) {\n\t\tduration = 1024;\n\t\trate = 48000;\n\t\tthis->position->clock.target_duration = 1024;\n\t\tthis->position->clock.target_rate = SPA_FRACTION(1, 48000);\n\t}"
            if old_nas in nas_src:
                with open(nas_path, "w") as f:
                    f.write(nas_src.replace(old_nas, new_nas, 1))
                print("  -> [PATCH] Patched null-audio-sink.c to lock 1024/48000 Hz timer against 8kHz HFP rate drag.")

        pw_cmd = (
            f"cd {pw_src_dir} && "
            "meson setup build --prefix=/usr/local -Dbluez5-codec-ldac=enabled -Dbluez5-codec-aac=enabled -Dbluez5-codec-aptx=enabled && "
            "ninja -C build && "
            "ninja -C build install && "
            "ldconfig"
        )
        run_cmd(pw_cmd)
        
        # Eliminate any ABI-conflicting distro SPA/PipeWire libraries in /usr/lib so /usr/local/lib has a strict monopoly
        run_cmd("rm -rf /usr/lib/aarch64-linux-gnu/spa-0.2 /usr/lib/aarch64-linux-gnu/pipewire-0.3 /usr/lib/aarch64-linux-gnu/libpipewire-0.3.so*")
        write_file('/etc/ld.so.conf.d/00-usr-local-priority.conf', '/usr/local/lib/aarch64-linux-gnu')
        run_cmd("ldconfig")

        # =========================================================================================
        # MODDER ADVISORY: REALTEK USB BT (RTL8761B/BU) HFP WIDEBAND SPEECH (mSBC / LC3-SWB) WORKAROUND
        # =========================================================================================
        # In PipeWire 1.7.0 (`spa/plugins/bluez5/backend-native.c:3826`), PipeWire advertises
        # `SPA_BT_HFP_SDP_HF_FEATURE_WIDEBAND_SPEECH` in the Bluetooth SDP record if `libspa-codec-bluez5-msbc.so`
        # exists on disk—even when `bluez5.enable-msbc = false` is set in WirePlumber!
        # On Realtek RTL8761B/BU USB adapters, transparent eSCO (mSBC) frames trigger kernel `btusb`
        # isochronous packet errors (`Bluetooth: hci0: corrupted SCO packet`) and immediate disconnects.
        # Moving `libspa-codec-bluez5-msbc.so` and `libspa-codec-bluez5-lc3-swb.so` into `media-codecs-disabled/`
        # forces clean 8kHz hardware CVSD SCO for reliable Bluetooth microphone recording.
        # =========================================================================================
        # Ensure TP-Link UB500 (vendor 2357) Realtek quirk is registered in bluez-hardware.conf
        hw_conf = "/usr/local/share/spa-0.2/bluez5/bluez-hardware.conf"
        if os.path.exists(hw_conf):
            with open(hw_conf, "r", encoding="utf-8") as f:
                c = f.read()
            if 'vendor-id = "usb:2357"' not in c:
                c = c.replace('{ bus-type = "usb", vendor-id = "usb:0bda" },',
                              '{ bus-type = "usb", vendor-id = "usb:0bda" },\n    { bus-type = "usb", vendor-id = "usb:2357" },')
                with open(hw_conf, "w", encoding="utf-8") as f:
                    f.write(c)
        print("  -> [OK] Audiophile PipeWire installed with wideband mSBC & Realtek hardware quirks.")

def step_enable_audio_services():
    """Enables the compiled Audiophile PipeWire audio engine."""
    if prompt("Enable Audiophile PipeWire Audio Services"):
        run_cmd("loginctl enable-linger " + SUDO_USER)
        
        # Enable and start the native services
        run_cmd("systemctl --user enable pipewire wireplumber pipewire-pulse", as_user=SUDO_USER)
        run_cmd("systemctl --user start pipewire wireplumber pipewire-pulse", as_user=SUDO_USER)
        print("  -> [OK] Audiophile audio services enabled.")



# def step_seed_codecs():
#     if prompt("Seed God Mode Bluetooth Codecs (Lua + SQLite)"):
#         import sqlite3
#         db_path = f"/home/{SUDO_USER}/vipxpert_minimal_os/vipxpert_os.db"
        
#         # The exhaustive God Mode master list
#         god_mode_codecs = "sbc,sbc_xq,aac,aac_eld,aptx,aptx_hd,aptx_ll,aptx_ll_duplex,faststream,faststream_duplex,lc3plus_h3,ldac,opus_05,opus_05_51,opus_05_71,opus_05_duplex,opus_05_pro,opus_g,lc3"
        
#         # 1. Seed SQLite Database
#         try:
#             conn = sqlite3.connect(db_path)
#             cursor = conn.cursor()
#             cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
#             # Use INSERT OR IGNORE so we never overwrite the user's saved preferences if they run the installer again
#             cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("bt_enabled_codecs", god_mode_codecs))
#             cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("bt_aac_max", "1"))
#             cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("bt_ldac_max", "1"))
#             conn.commit()
#             conn.close()
#             run_cmd(f"chown {SUDO_USER}:{SUDO_USER} {db_path}")
#             print("  -> [OK] SQLite database seeded with God Mode codecs.")
#         except Exception as e:
#             print(f"  -> [ERROR] Failed to seed database: {e}")

#         # 2. Clean up legacy WirePlumber 0.4 .lua directories (WirePlumber 0.5+ strictly uses SPA-JSON in wireplumber.conf.d)
#         run_cmd(f"rm -rf /home/{SUDO_USER}/.config/wireplumber/bluetooth.lua.d /home/{SUDO_USER}/.config/wireplumber/alsa.lua.d")
        
#         codec_string = god_mode_codecs.replace(",", " ")
#         sbc_xq_flag = "true" if "sbc_xq" in god_mode_codecs else "false"
        
#         # 4. Seed PipeWire Master Tuning
#         pw_conf_dir = f"/home/{SUDO_USER}/.config/pipewire/pipewire.conf.d"
#         run_cmd(f"mkdir -p {pw_conf_dir}", as_user=SUDO_USER)
#         pw_conf_file = os.path.join(pw_conf_dir, "99-master-tuning.conf")
#         # resample.disable = false
#         pw_conf_content = """context.properties = {
#     # Master Clock & Quantums (48kHz hardware-valid baseline; unlocks dynamic switching across all standard rates)
#     default.clock.rate = 48000
#     default.clock.allowed-rates = [ 48000 ]
#     default.clock.min-quantum = 32
#     default.clock.max-quantum = 1024
#     default.clock.quantum = 1024

#     # Resampling Engine Optimization
#     resample.quality = 14
#     resample.cutoff = 1.0

#     # CPU Instruction Tuning (Disable SIMD overhead for Pi)
#     resample.disable-neon = true
#     resample.disable-sse  = true
# }

# stream.properties = {
#     # Audio Stream Limits
#     audio.allowed-rates = [ 8000 16000 32000 44100 48000 88200 96000 176400 192000 352800 384000 ]
#     # Stream DSP & Math
#     channelmix.mix-lfe = false
#     volume.linear = true
#     resample.peaks = false
# }"""
#         write_file(pw_conf_file, pw_conf_content)
#         run_cmd(f"chown {SUDO_USER}:{SUDO_USER} {pw_conf_file}")
        
#         # 5. Disable WirePlumber Hearing Protection & Seed WP 0.5+ Native Bluetooth + Universal ALSA Rules
#         wp_conf_dir = f"/home/{SUDO_USER}/.config/wireplumber/wireplumber.conf.d"
#         run_cmd(f"mkdir -p {wp_conf_dir}", as_user=SUDO_USER)
#         run_cmd(f"rm -f {wp_conf_dir}/51-bluez-config.conf")
#         wp_vol_file = os.path.join(wp_conf_dir, "53-no-hearing-protection.conf")
        
#         # NOTE:
#         # - bluez5.enable-msbc is set to true for wideband 16kHz speech with RTL8761B 60:12 SCO MTU.
#         # - bluez5.roles excludes a2dp_source so phones treat the board as a Headset/Sink rather than an Audio Gateway.
#         # - LDAC is included in bluez5.codecs, though libldacdec sink output is currently silent (see step_compile_ldac note).
#         wp_vol_content = f"""wireplumber.settings = {{
#     device.routes.default-sink-volume = 1.0
#     device.routes.default-source-volume = 1.0
# }}

# monitor.bluez.properties = {{
#     bluez5.a2dp.aac.bitratemode = 5
#     bluez5.a2dp.ldac.quality = "hq"
#     bluez5.enable-sbc-xq = {sbc_xq_flag}
#     bluez5.enable-msbc = false
#     bluez5.enable-hw-volume = true
#     bluez5.hfphsp-backend = "native"
#     bluez5.codecs = [ {codec_string} ]
#     bluez5.roles = [ a2dp_sink hfp_hf hsp_hs ]
# }}

# monitor.bluez.rules = [
#     {{
#         matches = [
#             {{
#                 device.name = "~bluez_card.*"
#             }}
#         ]
#         actions = {{
#             update-props = {{
#                 bluez5.auto-connect = [ hfp_hf hsp_hs a2dp_sink ]
#             }}
#         }}
#     }}
# ]


def step_seed_codecs():
    if prompt("Seed God Mode Bluetooth Codecs (Lua + SQLite)"):
        import sqlite3
        db_path = f"/home/{SUDO_USER}/vipxpert_minimal_os/vipxpert_os.db"
        
        # The exhaustive God Mode master list
        god_mode_codecs = "sbc,sbc_xq,aac,aac_eld,aptx,aptx_hd,aptx_ll,aptx_ll_duplex,faststream,faststream_duplex,lc3plus_h3,ldac,opus_05,opus_05_51,opus_05_71,opus_05_duplex,opus_05_pro,opus_g,lc3,msbc"
        
        # 1. Clean up deprecated SQLite codec keys (53-no-hearing-protection.conf is now the single source of truth)
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            cursor.execute("DELETE FROM settings WHERE key IN ('bt_enabled_codecs', 'bt_aac_max', 'bt_ldac_max')")
            conn.commit()
            conn.close()
            run_cmd(f"chown {SUDO_USER}:{SUDO_USER} {db_path}")
            print("  -> [OK] Verified SQLite settings table (53-no-hearing-protection.conf is sole codec source of truth).")
        except Exception as e:
            print(f"  -> [ERROR] Failed to verify database: {e}")

        # 2. Clean up legacy WirePlumber 0.4 .lua directories (WirePlumber 0.5+ strictly uses SPA-JSON in wireplumber.conf.d)
        run_cmd(f"rm -rf /home/{SUDO_USER}/.config/wireplumber/bluetooth.lua.d /home/{SUDO_USER}/.config/wireplumber/alsa.lua.d")
        
        codec_string = god_mode_codecs.replace(",", " ")
        sbc_xq_flag = "true" if "sbc_xq" in god_mode_codecs else "false"
        
        # 4. Seed PipeWire Master Tuning
        pw_conf_dir = f"/home/{SUDO_USER}/.config/pipewire/pipewire.conf.d"
        run_cmd(f"mkdir -p {pw_conf_dir}", as_user=SUDO_USER)
        pw_conf_file = os.path.join(pw_conf_dir, "99-master-tuning.conf")
        # resample.disable = false
        pw_conf_content = """context.properties = {
    # Master Clock & Quantums (Strict 1024/48000 hardware lock for Allwinner H618 ahub0)
    default.clock.rate = 48000
    default.clock.allowed-rates = [ 48000 ]
    default.clock.min-quantum = 1024
    default.clock.max-quantum = 1024
    default.clock.quantum = 1024

    # Resampling Engine Optimization (ARM64 NEON SIMD enabled for zero-hiccup adaptive rate matching)
    resample.quality = 4
    resample.disable-neon = false
}

stream.properties = {
    # Audio Stream Limits
    audio.allowed-rates = [ 8000 16000 32000 44100 48000 88200 96000 176400 192000 352800 384000 ]
    # Stream DSP & Math
    channelmix.mix-lfe = false
    volume.linear = true
    resample.peaks = false
}"""
        write_file(pw_conf_file, pw_conf_content)
        run_cmd(f"chown {SUDO_USER}:{SUDO_USER} {pw_conf_file}")
        
        # 5. Disable WirePlumber Hearing Protection & Seed WP 0.5+ Native Bluetooth + Universal ALSA Rules
        wp_conf_dir = f"/home/{SUDO_USER}/.config/wireplumber/wireplumber.conf.d"
        run_cmd(f"mkdir -p {wp_conf_dir}", as_user=SUDO_USER)
        run_cmd(f"rm -f {wp_conf_dir}/51-bluez-config.conf")
        wp_vol_file = os.path.join(wp_conf_dir, "53-no-hearing-protection.conf")
        
        # NOTE:
        # - bluez5.enable-msbc is set to false to prevent Realtek USB btusb corrupted eSCO packets.
        # - bluez5.roles excludes a2dp_source so phones treat the board as a Headset/Sink rather than an Audio Gateway.
        # - LDAC is included in bluez5.codecs, though libldacdec sink output is currently silent (see step_compile_ldac note).
        wp_vol_content = f"""wireplumber.settings = {{
    device.routes.default-sink-volume = 1.0
    device.routes.default-source-volume = 1.0
}}

monitor.bluez.properties = {{
    bluez5.a2dp.aac.bitratemode = 5
    bluez5.a2dp.ldac.quality = "hq"
    bluez5.enable-sbc-xq = {sbc_xq_flag}
    bluez5.enable-msbc = true
    bluez5.enable-hw-volume = true
    bluez5.hfphsp-backend = "native"
    bluez5.codecs = [ {codec_string} ]
    bluez5.roles = [ a2dp_sink hfp_hf hsp_hs ]
}}

monitor.bluez.rules = [
    {{
        matches = [
            {{
                device.name = "~bluez_card.*"
            }}
        ]
        actions = {{
            update-props = {{
                bluez5.auto-connect = [ hfp_hf hsp_hs a2dp_sink ]
            }}
        }}
    }}
    {{
        matches = [
            {{
                node.name = "~bluez_input.*"
            }}
        ]
        actions = {{
            update-props = {{
                priority.driver = 0
                node.latency = "4096/48000"
                resample.quality = 4
                resample.disable = false
            }}
        }}
    }}
    {{
        matches = [
            {{
                node.name = "~bluez_output.*"
            }}
        ]
        actions = {{
            update-props = {{
                node.latency = "1024/48000"
                priority.driver = 0
                resample.quality = 4
                resample.disable = false
            }}
        }}
    }}
]

monitor.alsa.rules = [
    {{
        matches = [
            {{
                device.name = "alsa_card.platform-soc_ahub1_mach"
            }}
        ]
        actions = {{
            update-props = {{
                device.disabled = true
            }}
        }}
    }}
    {{
        matches = [
            {{
                node.name = "~alsa_output.*"
            }}
        ]
        actions = {{
            update-props = {{
                audio.format = "S32LE"
                audio.rate = 48000
                audio.allowed-rates = [ 48000 ]
                dither.method = "wannamaker3"
                api.alsa.soft-mixer = true
                api.alsa.headroom = 0
                resample.disable = false
                priority.driver = 3000
                priority.session = 3000
                session.suspend-on-idle = false
                session.suspend-timeout-seconds = 0
                api.alsa.disable-tsched = true
                api.alsa.period-size = 1024
                api.alsa.start-delay = 0
                api.alsa.period-num = 4
                api.alsa.rtio = true
            }}
        }}
    }}
    {{
        matches = [
            {{
                node.name = "~alsa_input.usb-*"
            }}
        ]
        actions = {{
            update-props = {{
                node.disabled = true
            }}
        }}
    }}
    {{
        matches = [
            {{
                node.name = "~alsa_input.platform-*"
            }}
        ]
        actions = {{
            update-props = {{
                audio.format = "S32LE"
                audio.rate = 48000
                audio.allowed-rates = [ 48000 ]
                api.alsa.soft-mixer = false
                api.alsa.disable-tsched = true
                api.alsa.period-size = 1024
                api.alsa.period-num = 4
                session.suspend-on-idle = false
                session.suspend-timeout-seconds = 0
            }}
        }}
    }}
]"""
        write_file(wp_vol_file, wp_vol_content)
        run_cmd(f"chown {SUDO_USER}:{SUDO_USER} {wp_vol_file}")
        
        # 6. Configure PCM5122 3-Wire I2S BCK PLL Lock (Reg 0x0d=0x10, Reg 0x25=0x08), Low-Latency FIR (Reg 0x26=0x01), VCOM Mode, & Disable Auto-Mute (Reg 0x41=0x00)
        for addr in ["0x4c", "0x4d"]:
            run_cmd(f"i2cset -y 0 {addr} 0x00 0x00 2>/dev/null || true")
            run_cmd(f"i2cset -y 0 {addr} 0x0d 0x10 2>/dev/null || true")
            run_cmd(f"i2cset -y 0 {addr} 0x25 0x08 2>/dev/null || true")
            run_cmd(f"i2cset -y 0 {addr} 0x26 0x01 2>/dev/null || true")
            run_cmd(f"i2cset -y 0 {addr} 0x41 0x00 2>/dev/null || true")
            run_cmd(f"i2cset -y 0 {addr} 0x03 0x00 2>/dev/null || true")
            run_cmd(f"i2cset -y 0 {addr} 0x00 0x01 2>/dev/null || true")
            run_cmd(f"i2cset -y 0 {addr} 0x01 0x00 2>/dev/null || true")
            run_cmd(f"i2cset -y 0 {addr} 0x00 0x00 2>/dev/null || true")
        
        print("  -> [OK] Audio configuration (Bluetooth, Universal ALSA, Anti-Pop Keep-Alive, Master Tuning, and Volume) ensured.")

def step_systemd_service():
    if prompt("Create and enable OS Kernel systemd service"):
        svc_content = f"""[Unit]
Description=VIPXpert Minimal OS Kernel
After=network.target bluetooth.target sound.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/{SUDO_USER}/vipxpert_minimal_os
ExecStart=/home/{SUDO_USER}/vipxpert_minimal_os/venv/bin/python main.py
Restart=always
RestartSec=5
Environment="PYTHONUNBUFFERED=1"
LimitRTPRIO=99
AmbientCapabilities=CAP_SYS_NICE

[Install]
WantedBy=multi-user.target"""
        write_file('/etc/systemd/system/vipxpert_os.service', svc_content)
        run_cmd("systemctl daemon-reload && systemctl enable vipxpert_os.service")

def step_python_requirements():
    if prompt("Install Python requirements to venv"):
        venv_path = f"/home/{SUDO_USER}/vipxpert_minimal_os/venv"
        req_path = f"/home/{SUDO_USER}/vipxpert_minimal_os/requirements.txt"
        
        if os.path.exists(venv_path) and not  FORCE_OVERWRITE:
            print("  -> [SKIP] Python virtual environment already exists.")
            return
            
        if not os.path.exists(venv_path):
            print("  -> Creating Python virtual environment...")
            run_cmd(f"python3 -m venv {venv_path}", as_user=SUDO_USER)
            
        if os.path.exists(req_path):
            print("  -> Installing Python dependencies...")
            run_cmd(f"{venv_path}/bin/pip install -r {req_path}", as_user=SUDO_USER)
        else:
            print(f"  -> [WARN] {req_path} not found. Skipping pip install.")

def step_tplink_bluetooth():
    """
    Verifies and installs the required Realtek firmware for TP-Link UB500 
    and UB500 Plus (RTL8761B / RTL8761BU chips). 
    The btusb kernel module natively supports the hardware, but requires these blobs.
    """
    if prompt("Install open-source firmware for TP-Link UB500/UB500 Plus Bluetooth"):
        fw_dir = "/lib/firmware/rtl_bt"
        run_cmd(f"mkdir -p {fw_dir}")
        
        files = [
            "rtl8761b_fw.bin",
            "rtl8761b_config.bin",
            "rtl8761bu_fw.bin",
            "rtl8761bu_config.bin"
        ]
        
        # Broken into variables so the markdown parser cannot corrupt it with brackets
        host = "gitlab.com"
        path = "/kernel-firmware/linux-firmware/-/raw/main/rtl_bt/"
        base_url = f"https://{host}{path}"
        
        for fw in files:
            target_path = os.path.join(fw_dir, fw)
            if not os.path.exists(target_path) or FORCE_OVERWRITE:
                print(f"  -> Downloading {fw}...")
                full_url = base_url + fw
                try:
                    req = urllib.request.Request(full_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req) as response, open(target_path, 'wb') as out_file:
                        out_file.write(response.read())
                except Exception as e:
                    print(f"  -> [ERROR] Failed to download {fw}: {e}")
            else:
                print(f"  -> [SKIP] {fw} already exists.")
        
        # Reload btusb to apply the new firmware immediately if device is plugged in
        run_cmd("modprobe -r btusb && modprobe btusb")
        print("  -> [OK] TP-Link Bluetooth firmware ensured.")

def step_fonts():
    if prompt("Download and install Fusion Pixel Font (CJK/Unicode support)"):
        font_dir = f"/home/{SUDO_USER}/vipxpert_minimal_os/fonts"
        run_cmd(f"mkdir -p {font_dir}")

        font_file = "fusion-pixel.ttf"
        target_path = os.path.join(font_dir, font_file)

        if not os.path.exists(target_path) or FORCE_OVERWRITE:
            print(f"  -> Querying GitHub for latest Fusion Pixel Font release...")
            import json, zipfile, io
            api_url = "https://api.github.com/repos/TakWolf/fusion-pixel-font/releases/latest"
            try:
                req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as response:
                    release_info = json.loads(response.read())

                # Find the asset for 12px monospaced TTF zip
                download_url = None
                for asset in release_info.get('assets', []):
                    # Use 12px monospaced TTF package
                    if '12px' in asset['name'] and 'monospaced-ttf' in asset['name'] and asset['name'].endswith('.zip'):
                        download_url = asset['browser_download_url']
                        break

                if not download_url:
                    print("  -> [ERROR] Could not find the TTF zip asset in the latest release.")
                    return

                print(f"  -> Downloading {download_url.split('/')[-1]} (~20MB)...")
                zip_req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(zip_req) as zip_resp:
                    zip_data = zip_resp.read()

                print("  -> Extracting zh_hans TTF...")
                with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                    ttf_filename = None
                    for name in z.namelist():
                        if name.endswith('zh_hans.ttf'):
                            ttf_filename = name
                            break
                    if ttf_filename:
                        with open(target_path, 'wb') as f:
                            f.write(z.read(ttf_filename))
                        run_cmd(f"chown {SUDO_USER}:{SUDO_USER} {target_path}")
                        print("  -> [OK] Font downloaded and extracted successfully.")
                    else:
                        print("  -> [ERROR] zh_hans.ttf not found in the downloaded zip.")
            except Exception as e:
                print(f"  -> [ERROR] Failed to download font: {e}")
        else:
            print(f"  -> [SKIP] {font_file} already exists.")

def step_adb_and_native_workers():
    if prompt("Provision ADB USB Audio Bridge (scrcpy-server v2.7) & Compile Native C/Rust Workers"):
        repo_dir = f"/home/{SUDO_USER}/vipxpert_minimal_os"
        bin_dir = os.path.join(repo_dir, "bin")
        run_cmd(f"mkdir -p {bin_dir}")

        # 1. Ensure bin/adb wrapper/symlink exists if bundled bin/adb is absent
        adb_target = os.path.join(bin_dir, "adb")
        if not os.path.exists(adb_target) and os.path.exists("/usr/bin/adb"):
            run_cmd(f"ln -sf /usr/bin/adb {adb_target}")
            print("  -> [OK] Linked /usr/bin/adb -> bin/adb.")

        # 2. Ensure scrcpy-server.jar (v2.7) is present for 48kHz raw PCM Android USB Audio capture
        jar_target = os.path.join(bin_dir, "scrcpy-server.jar")
        if not os.path.exists(jar_target) or FORCE_OVERWRITE:
            scrcpy_url = "https://github.com/Genymobile/scrcpy/releases/download/v2.7/scrcpy-server-v2.7"
            print("  -> Downloading scrcpy-server v2.7 for ADB USB Audio Bridge...")
            try:
                req = urllib.request.Request(scrcpy_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=30) as resp, open(jar_target, 'wb') as jf:
                    jf.write(resp.read())
                os.chmod(jar_target, 0o644)
                print("  -> [OK] Installed bin/scrcpy-server.jar (v2.7).")
            except Exception as e:
                print(f"  -> [WARN] Failed to download scrcpy-server.jar: {e}")
        else:
            print("  -> [SKIP] bin/scrcpy-server.jar already exists.")

        # 3. Compile native C/Rust real-time workers (prefers Rust workspace if present, compiles C fallback)
        c_src = os.path.join(repo_dir, "src", "core", "viz_dsp.c")
        viz_out = os.path.join(bin_dir, "viz_dsp")
        rust_manifest = os.path.join(repo_dir, "rust_workers", "Cargo.toml")
        user_cargo = f"/home/{SUDO_USER}/.cargo/bin/cargo"
        cargo_bin = user_cargo if os.path.exists(user_cargo) else ("cargo" if subprocess.run(["which", "cargo"], stdout=subprocess.DEVNULL).returncode == 0 else "")
        if os.path.exists(rust_manifest) and cargo_bin:
            print("  -> Building Rust real-time workers (cargo build --release)...")
            run_cmd(f"PATH=/home/{SUDO_USER}/.cargo/bin:$PATH {cargo_bin} build --release --manifest-path {rust_manifest}", as_user=SUDO_USER)
            for b_name in ("viz_dsp", "mic_scope_dsp"):
                rust_b = os.path.join(repo_dir, "rust_workers", "target", "release", b_name)
                if os.path.exists(rust_b):
                    b_dst = os.path.join(bin_dir, b_name)
                    run_cmd(f"cp -f {rust_b} {b_dst} && chmod 755 {b_dst}")
                    print(f"  -> [OK] Installed Rust release binary -> bin/{b_name}.")
        elif os.path.exists(c_src):
            print("  -> Compiling C99 Goertzel DSP worker (src/core/viz_dsp.c -> bin/viz_dsp)...")
            run_cmd(f"gcc -O3 -Wall -Wextra {c_src} -lm -o {viz_out}")

        run_cmd(f"chown -R {SUDO_USER}:{SUDO_USER} {bin_dir}")

# --- Main Execution ---
if __name__ == '__main__':
    print(f"--- VIPXpert Minimal OS Installer ---")
    print(f"Target User: {SUDO_USER} (UID: {USER_UID})")
    
    STEPS = [
        (step_purge_ghosts, "Purge Ghosts & Audio Conflicts"),
        (step_apt_packages, "Install APT Dependencies"),
        (step_groups_and_udev, "Configure Hardware Groups & UDEV"),
        (step_sudoers_and_scripts, "Configure Passwordless Sudo & DAC Script"),
        (step_armbian_env, "Inject Armbian Overlays"),
        (step_compile_ldac, "Compile LDAC Decoder"),
        (step_compile_pipewire, "Compile Audiophile PipeWire"),
        (step_tplink_bluetooth, "Install TP-Link BT Firmware"),
        (step_enable_audio_services, "Enable/Restart Audio Services"),
        (step_python_requirements, "Install Python VENV & Requirements"),
        (step_fonts, "Install Fonts"),
        (step_seed_codecs, "Seed Audio Codecs, Tuning & Configs"),
        (step_adb_and_native_workers, "Provision ADB USB Audio & Compile Native/Rust Workers"),
        (step_systemd_service, "Create OS Systemd Service")
    ]
    
    print("\n--- Installation Scopes ---")
    for i, (func, desc) in enumerate(STEPS, 1):
        print(f"  [{i}] {desc}")
    print("\n  [A] All Steps")
    print("  [C] Config Apply Only (Steps 12 & 9)")
    
    scope = input("\n1. Enter install scope (e.g., 1,4,12 or A or C): ").strip().lower()
    
    to_run = []
    if 'a' in scope:
        to_run = list(range(1, len(STEPS) + 1))
    elif 'c' in scope:
        to_run = [12, 9] 
    else:
        for x in scope.split(','):
            if x.strip().isdigit():
                to_run.append(int(x.strip()))
                
    if not to_run:
        print("No valid steps selected. Exiting.")
        sys.exit(0)
        
    mode = input("2. Force Overwrite & Reinstall? [y/N] (Default is Smart Skip): ").strip().lower()
    if mode in ['y', 'yes', 'true', '1', 'f', 'force']:
        FORCE_OVERWRITE = True
        
    # Ensure purge/overwrite logic triggers if step 1 is explicitly typed
    if 1 in to_run:
        FORCE_OVERWRITE = True
        
    print(f"\n🚀 Starting Execution (Force Overwrite: {FORCE_OVERWRITE})...")
        
    for idx in to_run:
        if 1 <= idx <= len(STEPS):
            STEPS[idx-1][0]()
            
    print("\n✅ Setup Complete! Please REBOOT the board if you made hardware/group changes.")
