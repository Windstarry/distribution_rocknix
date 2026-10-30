#!/bin/sh
# SPDX-License-Identifier: GPL-2.0
# Copyright (C) 2024-present ROCKNIX (https://github.com/ROCKNIX)
#
# Chain-boot boot-partition update: dtbs and boot scripts only.
# This device uses the stock Amlogic U-Boot, so nothing is ever written to
# the bootloader area (no u-boot.bin, no sector 1, no eMMC boot partition).

[ -z "$SYSTEM_ROOT" ] && SYSTEM_ROOT=""
[ -z "$BOOT_ROOT" ] && BOOT_ROOT="/flash"
[ -z "$BOOT_PART" ] && BOOT_PART=$(df "$BOOT_ROOT" | tail -1 | awk {' print $1 '})

mount -o remount,rw $BOOT_ROOT

# update dtb.img
for dtb in meson-g12b-a311d-cainiao-cniot-core.dtb; do
  if [ -f $BOOT_ROOT/dtb.img ] && [ -f $SYSTEM_ROOT/usr/share/bootloader/device_trees/$dtb ]; then
    # only update if current dtb.img matches this dtb
    if cmp -s $BOOT_ROOT/dtb.img $SYSTEM_ROOT/usr/share/bootloader/device_trees/$dtb 2>/dev/null; then
      echo "Updating dtb.img from $dtb..."
      cp -p $SYSTEM_ROOT/usr/share/bootloader/device_trees/$dtb $BOOT_ROOT/dtb.img
      break
    fi
  fi
done

# update the panfrost GPU-stack dtb, if it is already installed
if [ -f $BOOT_ROOT/dtb-panfrost.img ] && \
   [ -f $SYSTEM_ROOT/usr/share/bootloader/device_trees/meson-g12b-a311d-cainiao-cniot-core-panfrost.dtb ]; then
  echo "Updating dtb-panfrost.img..."
  cp -p $SYSTEM_ROOT/usr/share/bootloader/device_trees/meson-g12b-a311d-cainiao-cniot-core-panfrost.dtb $BOOT_ROOT/dtb-panfrost.img
fi

# update device_trees
if [ -d $SYSTEM_ROOT/usr/share/bootloader/device_trees ]; then
  mkdir -p $BOOT_ROOT/device_trees
  cp -f $SYSTEM_ROOT/usr/share/bootloader/device_trees/*.dtb $BOOT_ROOT/device_trees/
fi

# update boot scripts
for f in aml_autoscript config.ini; do
  if [ -f $SYSTEM_ROOT/usr/share/bootloader/$f ]; then
    echo "Updating $f..."
    cp -p $SYSTEM_ROOT/usr/share/bootloader/$f $BOOT_ROOT/
  fi
done

if [ -f $SYSTEM_ROOT/usr/share/bootloader/Generic_cfgload ]; then
  echo "Updating cfgload..."
  cp -p $SYSTEM_ROOT/usr/share/bootloader/Generic_cfgload $BOOT_ROOT/cfgload
fi

if [ -f $SYSTEM_ROOT/usr/share/bootloader/Generic_cfgload_env ]; then
  echo "Updating cfgload_env..."
  cp -p $SYSTEM_ROOT/usr/share/bootloader/Generic_cfgload_env $BOOT_ROOT/cfgload_env
fi

sync
mount -o remount,ro $BOOT_ROOT

echo "UPDATE" > /storage/.boot.hint
