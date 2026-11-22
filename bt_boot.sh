#!/bin/bash
echo "1. Master clock ON & Power Save OFF..."
rfkill unblock all
ip link set wlan0 up 2>/dev/null
iw dev wlan0 set power_save off 2>/dev/null

echo "2. Hard Resetting BT_REG_ON (GPIO 208)..."
echo 208 > /sys/class/gpio/export 2>/dev/null
echo out > /sys/class/gpio/gpio208/direction
echo 0 > /sys/class/gpio/gpio208/value
sleep 1
echo 1 > /sys/class/gpio/gpio208/value
sleep 2

echo "3. Clearing processes..."
killall hciattach btattach brcm_patchram_plus 2>/dev/null

echo "4. Raw Hex Debug (115200 Baud, GPIO 208, C5 Firmware)..."
brcm_patchram_plus -d --enable_hci --no2bytes --tosleep 200000 --baudrate 115200 \
--patchram /lib/firmware/brcm/BCM4345C5.hcd /dev/ttyS1
