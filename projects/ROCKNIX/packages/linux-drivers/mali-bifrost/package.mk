# SPDX-License-Identifier: GPL-2.0
# Copyright (C) 2023 JELOS (https://github.com/JustEnoughLinuxOS)

PKG_NAME="mali-bifrost"
PKG_LICENSE="GPL"
PKG_SITE="https://developer.arm.com/downloads/-/mali-drivers/bifrost-kernel"
PKG_LONGDESC="mali-bifrost: Linux drivers for Mali Bifrost GPUs"
PKG_TOOLCHAIN="manual"
PKG_IS_KERNEL_PKG="yes"

case ${DEVICE} in
  S922X|A311D)
  # Match the only available userspace blob (libmali-meson-r51p0-wayland-gbm.so,
  # "Bifrost-r51p0-00eac0") with an official ARM r51p0 kernel DDK. The
  # ROCKNIX/mali_kbase tree starts at r54p1 and never carried r51p0, so the
  # previous default branch produced r54p2 kbase against r51p0 userspace —
  # the DDK mismatch behind GPU fault 0x51 INSTR_INVALID_ENC.
  # Follow the RK3326S r52p0 precedent: official developer.arm.com tarball
  # plus a device-kernel-version patch directory for 6.18 (patches/6.18).
  PKG_VERSION="r51p0-00eac0"
  # SHA256 of official BX304L01B-SW-99002-r51p0-00eac0.tar (MD5 3a7b123518fff2bf33745cda46ec5eb3)
  PKG_SHA256="e991b10682df586b0c5569ced846206ce5d8869a91e2a04544d2cff99b26bfa5"
  PKG_URL="https://developer.arm.com/-/media/Files/downloads/mali-drivers/kernel/mali-bifrost-gpu/BX304L01B-SW-99002-${PKG_VERSION}.tar"
  PKG_PATCH_DIRS+=" 6.18"
  ;;
  RK3326)
  PKG_VERSION="39da994bb6fc8819e5e8c1873907dd21d17e53c1"
  PKG_SHA256="f947b99a1bcb5b86ea270c1ce4cd189f4df26b3c0c8ba92c1ef256bb757afa66"
  PKG_URL="http://github.com/rocknix/mali_kbase/archive/${PKG_VERSION}.tar.gz"
  ;;
  RK3326S)
  # RK3562/RK3326S uses ARM official r52p0 DDK, kernel patch provides version_compat_defs.h compatibility
  PKG_VERSION="r52p0-00eac0"
  PKG_URL="https://developer.arm.com/-/media/Files/downloads/mali-drivers/kernel/mali-valhall-gpu/VX504X08X-SW-99002-${PKG_VERSION}.tar"
  ;;
  RK356X)
  PKG_VERSION="422e192b7e3aa3140f34de34765b798817c1f749"
  PKG_URL="http://github.com/rocknix/mali_kbase/archive/${PKG_VERSION}.tar.gz"
  ;;
  RK3566)
  PKG_VERSION="39da994bb6fc8819e5e8c1873907dd21d17e53c1"
  PKG_SHA256="f947b99a1bcb5b86ea270c1ce4cd189f4df26b3c0c8ba92c1ef256bb757afa66"
  PKG_URL="http://github.com/rocknix/mali_kbase/archive/${PKG_VERSION}.tar.gz"
  ;;
  *)
  PKG_VERSION="422e192b7e3aa3140f34de34765b798817c1f749"
  PKG_URL="http://github.com/rocknix/mali_kbase/archive/${PKG_VERSION}.tar.gz"
  ;;
esac

make_target() {
  # S922X is an actual Amlogic Meson SoC — it requires the meson platform
  # backend for correct GPU clock and power domain handling.
  # Rockchip devices use the devicetree backend which properly coordinates
  # with kernel power domain drivers during runtime PM suspend/resume cycles.
  case ${DEVICE} in
    S922X|A311D)
      MALI_PLATFORM="meson"
      ;;
    *)
      MALI_PLATFORM="devicetree"
      ;;
  esac

  kernel_make KDIR=$(kernel_path) -C ${PKG_BUILD} \
       CONFIG_MALI_MIDGARD=m CONFIG_MALI_PLATFORM_NAME=${MALI_PLATFORM} CONFIG_MALI_REAL_HW=y CONFIG_MALI_DEVFREQ=y CONFIG_MALI_GATOR_SUPPORT=y
}

makeinstall_target() {
  DRIVER_DIR=${PKG_BUILD}/product/kernel/drivers/gpu/arm/midgard

  mkdir -p ${INSTALL}/$(get_full_module_dir)/${PKG_NAME}
    cp ${DRIVER_DIR}/mali_kbase.ko ${INSTALL}/$(get_full_module_dir)/${PKG_NAME}
}
