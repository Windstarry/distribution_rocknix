# SPDX-License-Identifier: GPL-2.0
# Copyright (C) 2023 JELOS (https://github.com/JustEnoughLinuxOS)

PKG_NAME="rocknix"
PKG_VERSION=""
PKG_LICENSE="GPLv2"
PKG_SITE=""
PKG_URL=""
PKG_DEPENDS_TARGET="toolchain autostart"
PKG_LONGDESC="ROCKNIX Meta Package"
PKG_TOOLCHAIN="make"

make_target() {
  :
}

makeinstall_target() {

  mkdir -p ${INSTALL}/usr/config/
  rsync -av ${PKG_DIR}/config/* ${INSTALL}/usr/config/
  ln -sf /storage/.config/system ${INSTALL}/system
  find ${INSTALL}/usr/config/system/ -type f -exec chmod o+x {} \;

  ### Factory default for A311D is panfrost/Mesa. The vendor stack stays
  ### in the image as an optional gpu.driver value: the reference
  ### S922X-BOX 20261108 image documents a kbase r54p2 / libmali r51p0
  ### DDK mismatch that broke libmali, and that match has not been
  ### board-verified on A311D. libmali must not become the first-boot
  ### default until the version match is proven.
  ### The chain-boot dtb.img only exposes the bifrost node (gpu@ffe40000,
  ### "arm,mali-bifrost"); both stacks bind that same node, so no DTB swap
  ### is involved in switching - selection is entirely userspace.
  ### get_setting() only reads /storage/.config/system/configs/system.cfg,
  ### which is seeded from this file on first boot
  ### (001-functions:set_setting / post-update).
  if [ "${DEVICE}" = "A311D" ]
  then
    if grep -q '^gpu.driver=' ${INSTALL}/usr/config/system/configs/system.cfg
    then
      sed -i "s#^gpu.driver=.*#gpu.driver=panfrost#" ${INSTALL}/usr/config/system/configs/system.cfg
    else
      echo "gpu.driver=panfrost" >> ${INSTALL}/usr/config/system/configs/system.cfg
    fi

    ### Default HDMI output mode: 1920x1080@60Hz.
    ### sway/111-sway-init only appends "output HDMI-A-1 mode ..." when
    ### system.display_mode_hdmi is set; without it wlroots takes the sink's
    ### preferred mode from EDID. The EDID this box is usually paired with
    ### ("FFA SMART TV") advertises 3840x2160@60 as the preferred mode, so a
    ### fresh image comes up at 4K: ES text becomes tiny and the compositor
    ### fills ~4x the pixels on a G52. Pin 1080p60 as the build default; the
    ### user can still override it from ES or by deleting this key.
    if grep -q '^system.display_mode_hdmi=' ${INSTALL}/usr/config/system/configs/system.cfg
    then
      sed -i "s#^system.display_mode_hdmi=.*#system.display_mode_hdmi=1920x1080@60Hz#" ${INSTALL}/usr/config/system/configs/system.cfg
    else
      echo "system.display_mode_hdmi=1920x1080@60Hz" >> ${INSTALL}/usr/config/system/configs/system.cfg
    fi
  fi

  mkdir -p ${INSTALL}/usr/bin/

  ### Compatibility links for ports
  ln -s /storage/roms ${INSTALL}/roms

  ### Add some quality of life customizations for hardworking devs.
  if [ -n "${LOCAL_SSH_KEYS_FILE}" ]
  then
    mkdir -p ${INSTALL}/usr/config/ssh
    cp ${LOCAL_SSH_KEYS_FILE} ${INSTALL}/usr/config/ssh/authorized_keys
  fi

  if [ -n "${LOCAL_WIFI_SSID}" ]
  then
    sed -i "s#wifi.enabled=0#wifi.enabled=1#g" ${INSTALL}/usr/config/system/configs/system.cfg
    mkdir -p ${INSTALL}/usr/config/iwd
    cat <<EOF >> ${INSTALL}/usr/config/iwd/${LOCAL_WIFI_SSID}.psk
[Security]
Passphrase=${LOCAL_WIFI_KEY}
EOF
  fi
  # Always install the update script
  mkdir -p $INSTALL/usr/share/bootloader
  find_file_path bootloader/detect_subdevice.sh && cp -av ${FOUND_PATH} ${INSTALL}/usr/share/bootloader
  find_file_path bootloader/update.sh && cp -av ${FOUND_PATH} ${INSTALL}/usr/share/bootloader
}

post_install() {
  ln -sf rocknix.target ${INSTALL}/usr/lib/systemd/system/default.target

  if [ ! -d "${INSTALL}/usr/share" ]
  then
    mkdir "${INSTALL}/usr/share"
  fi
  cp ${PKG_DIR}/sources/post-update ${INSTALL}/usr/share
  chmod 755 ${INSTALL}/usr/share/post-update

  # Issue banner
  cat <<EOF >> ${INSTALL}/etc/issue
... Version: ${OS_VERSION} (aurknix-from-kk)
... Built: ${BUILD_DATE}

EOF
  cp ${PKG_DIR}/sources/motd ${INSTALL}/etc
  cat ${INSTALL}/etc/issue >> ${INSTALL}/etc/motd

  cp ${PKG_DIR}/sources/scripts/* ${INSTALL}/usr/bin
  chmod 0755 ${INSTALL}/usr/bin/* 2>/dev/null ||:

  ### Fix and migrate to autostart package
  enable_service rocknix-autostart.service
  
  ### ZRAM/Swap and Memory Manager Service
  enable_service rocknix-memory-manager.service

  ### Take a backup of the system configuration on shutdown
  enable_service save-sysconfig.service

  sed -i "s#@DEVICENAME@#${DEVICE}#g" ${INSTALL}/usr/config/system/configs/system.cfg

  ### Defaults for non-main builds.
  BUILD_BRANCH="$(git branch --show-current)"
  if [ ! "${BUILD_BRANCH}" = "main" ]
  then
    sed -i "s#samba.enabled=0#samba.enabled=1#g" ${INSTALL}/usr/config/system/configs/system.cfg
    sed -i "s#ssh.enabled=0#ssh.enabled=1#g" ${INSTALL}/usr/config/system/configs/system.cfg
    sed -i "s#wifi.enabled=0#wifi.enabled=1#g" ${INSTALL}/usr/config/system/configs/system.cfg
    sed -i "s#system.loglevel=none#system.loglevel=verbose#g" ${INSTALL}/usr/config/system/configs/system.cfg
  fi

  ### Enable HDMI hotplug service on H700
  if [ "${DEVICE}" = "H700" ]
  then
    enable_service hdmi-hotplug.path
  fi

}
