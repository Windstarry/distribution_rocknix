# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2022-present JELOS (https://github.com/JustEnoughLinuxOS)

PKG_NAME="gpudriver"
PKG_VERSION=""
PKG_LICENSE="GPLv2"
PKG_SITE=""
PKG_URL=""
PKG_DEPENDS_TARGET=""
PKG_TOOLCHAIN="manual"
PKG_LONGDESC="GPU driver util for switching between panfrost / panthor and libmali / libmali-vulkan"

post_makeinstall_target() {
  mkdir -p "${INSTALL}/usr/bin/"
  cp -v "${PKG_BUILD}/bin/gpudriver" "${INSTALL}/usr/bin/"

  # chain-boot GPU stack selector, writes gpu_dtb into /flash/config.ini
  if [ "${DEVICE}" = "A311D" ]; then
    cp -v "${PKG_BUILD}/bin/gpu-dtb" "${INSTALL}/usr/bin/"
    chmod +x "${INSTALL}/usr/bin/gpu-dtb"
  fi

  # set the correct mesa pan kernel driver module based on device
  case ${DEVICE} in
    RK3588)
      PAN="panthor"
      GPU_OPTIONS="panfrost libmali"
      DTB_OVERLAY_LOAD="\/usr\/bin\/dtb_overlay set driver-gpu driver-gpu-panthor.dtbo"
      DTB_OVERLAY_UNLOAD="\/usr\/bin\/dtb_overlay set driver-gpu None"
    ;;
    S922X)
      PAN="panfrost"
      GPU_OPTIONS="panfrost libmali"
      DTB_OVERLAY_LOAD="\/usr\/bin\/dtb_overlay set driver-gpu driver-gpu-panfrost.dtbo"
      DTB_OVERLAY_UNLOAD="\/usr\/bin\/dtb_overlay set driver-gpu None"
    ;;
    A311D)
      PAN="panfrost"
      GPU_OPTIONS="panfrost libmali"
      # Chain-boot has no extlinux.conf: the DTB is selected through
      # /flash/config.ini (gpu_dtb) and applied on the next reboot.
      DTB_OVERLAY_LOAD="\/usr\/bin\/gpu-dtb set panfrost"
      DTB_OVERLAY_UNLOAD="\/usr\/bin\/gpu-dtb set libmali"
    ;;
    RK356X)
      PAN="panfrost"
      GPU_OPTIONS="panfrost libmali"
      DTB_OVERLAY_LOAD=""
      DTB_OVERLAY_UNLOAD=""
    ;;
    RK3326S|A527)
      PAN="panfrost"
      GPU_OPTIONS="libmali"
      DTB_OVERLAY_LOAD=""
      DTB_OVERLAY_UNLOAD=""
    ;;
    *)
      PAN="panfrost"
      GPU_OPTIONS="panfrost libmali"
      DTB_OVERLAY_LOAD=""
      DTB_OVERLAY_UNLOAD=""
    ;;
  esac

  sed -e "s/@PAN@/${PAN}/g" \
      -e "s/@GPU_OPTIONS@/${GPU_OPTIONS}/g" \
      -i  ${INSTALL}/usr/bin/gpudriver

  sed -e "s/@DTB_OVERLAY_LOAD@/${DTB_OVERLAY_LOAD}/g" \
      -i  ${INSTALL}/usr/bin/gpudriver

  sed -e "s/@DTB_OVERLAY_UNLOAD@/${DTB_OVERLAY_UNLOAD}/g" \
      -i  ${INSTALL}/usr/bin/gpudriver
}
