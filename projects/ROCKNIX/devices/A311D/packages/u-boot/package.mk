# SPDX-License-Identifier: GPL-2.0
# Copyright (C) 2024-present ROCKNIX (https://github.com/ROCKNIX)

PKG_NAME="u-boot"
PKG_VERSION="2025.04"
PKG_SHA256="439d3bef296effd54130be6a731c5b118be7fddd7fcc663ccbc5fb18294d8718"
PKG_LICENSE="GPL"
PKG_SITE="https://www.denx.de/wiki/U-Boot"
PKG_URL="https://ftp.denx.de/pub/u-boot/${PKG_NAME}-${PKG_VERSION}.tar.bz2"
PKG_DEPENDS_TARGET="toolchain u-boot-tools:host"
PKG_LONGDESC="Verified stage-B boot chain (cfgload / aml_autoscript / dtb.img) for A311D."
PKG_TOOLCHAIN="manual"

PKG_NEED_UNPACK="${PROJECT_DIR}/${PROJECT}/bootloader ${PROJECT_DIR}/${PROJECT}/devices/${DEVICE}/bootloader"

# The stage-B chain is pinned as verified binaries, not rebuilt from .src:
#   * mkimage writes a fresh uImage timestamp/CRC on every build;
#   * the Amlogic 2015.01 U-Boot needs the 8-byte length prefix that the
#     CoreELEC scripts carry in front of the payload.
# Rebuilding therefore produces bytes that were never validated on the
# board. The three files in bootloader/bootkit/ were extracted from
# AURKNIX-A311D.aarch64-20261109-bootsafe-edidmode.img and their md5 was
# confirmed against the live /flash on 192.168.31.7 (2026-11-10).
# See tools/a311d_aurknix_runnable_proof_20261110.txt.
#
# Generic_cfgload_env / ceboot is deliberately NOT produced: the verified
# aml_autoscript uses 'autoscr' directly and has no ceboot indirection.

make_target() {
  BOOTKIT="${PROJECT_DIR}/${PROJECT}/devices/${DEVICE}/bootloader/bootkit"
  for f in cfgload aml_autoscript dtb.img; do
    if [ ! -f "${BOOTKIT}/${f}" ]; then
      echo "A311D: missing verified bootkit/${f}" >&2
      exit 1
    fi
    cp -p "${BOOTKIT}/${f}" "${f}" || exit 1
  done
  # mkimage_aml_boot() mcopies Generic_cfgload -> /cfgload on the FAT volume.
  mv -f cfgload Generic_cfgload
  rm -f Generic_cfgload_env
}

makeinstall_target() {
  mkdir -p ${INSTALL}/usr/share/bootloader/device_trees

  cp -v Generic_cfgload ${INSTALL}/usr/share/bootloader/
  cp -v aml_autoscript  ${INSTALL}/usr/share/bootloader/
  cp -v dtb.img         ${INSTALL}/usr/share/bootloader/

  # update.sh uses cmp against this file before refreshing /flash/dtb.img,
  # so both must be the same verified 54,984-byte bootkit dtb.
  cp -v dtb.img ${INSTALL}/usr/share/bootloader/device_trees/meson-g12b-a311d-cainiao-cniot-core.dtb

  CONFIG_INI="${PROJECT_DIR}/${PROJECT}/devices/${DEVICE}/bootloader/config.ini"
  [ -f "${CONFIG_INI}" ] && cp -v "${CONFIG_INI}" ${INSTALL}/usr/share/bootloader/
}
