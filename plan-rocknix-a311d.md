# ROCKNIX A311D（CAINIAO CNIoT-CORE / LEMO CORE A311D 版）链式启动正式实施方案

> 状态：**仅方案，零改动**。仓库 `d:\ProgramData\distribution_rocknix` 未修改任何文件，测试机 `192.168.31.57` 未写入任何内容。
> 依据：仓库现有代码（带文件:行号）+ 四份外部参照（按可信度排序：amlogic-devices/cainiao-cniot-core README 为一手同板评测、armbian `.csc` 为同板官方板级定义、batocera.linux/cainiao-cniot-core 为同板构建实现、aoc-65t33z 为**不同 SoC** 的旁证）。
> 落地必须经用户再次确认。

---

## 0. 一句话摘要

**不改原厂 U-Boot、不动 eMMC**：新建独立 `DEVICE=A311D`（内核与 GPU 复用 S922X 的 mainline 路线，引导包装复用 S905 的 aml_autoscript/cfgload + Android bootimg 路线），`mkbootimg` 装载地址按本机实测改为 `--base 0x0 --kernel_offset 0x01080000 --ramdisk_offset 0x01000000`，内核追加 `CONFIG_KERNEL_LZO=y` 与 `CONFIG_RD_ZSTD=y` 使 `Image.lzo` + zstd 外部 initramfs 可被原厂 U-Boot 的 `bootm` 吃下；Mali-G52 的两套用户态由**同一镜像内**的 `libmali`（厂商 DDK，`mali_kbase.ko`）与 `panfrost`（mesa）提供，运行期用 `gpudriver` 切换，但链式引导没有 extlinux.conf，因此用**新增的 `config.ini: gpu_dtb` 键 + 两份 dtb 变体（`dtb.img` / `dtb-panfrost.img`）+ 重启**来落地等价切换。

### 0.1 两条硬边界的落点

| 边界 | 本方案如何满足 | 反向证据（必须避免的路径） |
|---|---|---|
| 不覆盖原厂 bootloader | 只产出 `/flash` 下的 `aml_autoscript`、`cfgload`、`cfgload_env`、`config.ini`、`kernel.img`、`dtb.img`，外加 `device_trees/`；U-Boot 侧只用原厂 `saveenv`/`fatload`/`bootm` | `projects/ROCKNIX/bootloader/mkimage:39-42` 的 `dd u-boot.bin bs=512 seek=1`；`devices/S922X/bootloader/update.sh:42-45` 同样的 `seek=1`；`devices/S922X/packages/u-boot/package.mk:23` 的 `odroidgou_defconfig` + `:38` 的 `sd_fuse/u-boot.bin` |
| 两套 GPU 栈都能出且可切换 | 内核同时保留 `CONFIG_DRM_PANFROST=m`（S922X conf:3842）与 `CONFIG_MALI_MIDGARD=m`（mali-bifrost package.mk:52 注入）；用户态同时装 `libmali`（S922X options:75）与 mesa panfrost gallium（config/graphic:98-102） | 只开 `GRAPHIC_DRIVERS="mali"` 会触发 `linux/package.mk:367-373` 关掉 panfrost，属“二选一”，被边界二否决 |

---

## 0.2 关键参数表（一手事实与来源）

| 项 | 值 | 证据 |
|---|---|---|
| 板型/厂商 | CAINIAO CNIoT-CORE（LEMO CORE A311D 版），可拆主机 + 底座 | amlogic-devices README:9；armbian `.csc` 第 1 行注释与 `BOARD_NAME="CNIoT-CORE"` |
| SoC | Amlogic A311D（G12B：4×A73 + 2×A53），Mali-G52 MP4 | README:15；`.csc` 注释；S922X patch 0007 的 `arm,mali-bifrost` 节点 |
| 内存 | 2 GB DDR | README:15；`.csc` 注释；batocera dts `memory@0 reg=0x77359400` |
| 存储 | 16 GB eMMC，**无 SD 卡槽** | README:15/17；`.csc` 注释 |
| 网络 | 千兆 RGMII（RTL8211F）+ RTL8822CS WiFi/BT（SDIO + UART） | README:19；batocera dts `&ethmac`/`&sd_emmc_a`/`&uart_A` |
| USB | 1× USB3（dwc3）+ USB2（Type-C 下载口/侧触点经 SGM7227 切换） | README:21-40；`.csc` 注释 "USB3"；S922X conf:4591-4601 |
| 其它 | 6×WS2812（GPIOH_4，主线复用为 SPI MOSI → spidev）；PWM 风扇；ADC recovery 键；电源键；HT6872 功放 | README:25/292-302；batocera dts |
| 原厂引导链 | stock U-Boot → boot 分区 `aml_autoscript`/`cfgload`/`cfgload_env`/`config.ini` → `bootm` Android bootimg(`kernel.img` + `dtb.img`) | 仓库 `devices/S905/bootloader/scripts/aml_autoscript.src:1-19`、`Generic_cfgload.src:1-52`；本机 `.57` 现网 EE 即此链路 |
| bootimg 头实测 | `kernel_addr=0x01080000`、`ramdisk_addr=0x01000000`；kernel 段 LZO 魔数 `89 4c 5a 4f`；page size 2048（mkbootimg 默认） | 本机 `/flash/kernel.img` 头字段实测（记录于 `E:\A311d\device-info\` 的引导链/DTB 对比文档）；**不是**外部参照给出 |
| 引导介质（本方案） | **U 盘** `sda1`(FAT, label `AURKNIX`) + `sda2`(ext4, label `STORAGE`) + `sda3`(GAMES) | `distributions/ROCKNIX/options:203-204`；本机 EE 现网同构 |
| 外部参照的引导介质 | **eMMC**（写主线 U-Boot FIP 到 `seek=1`），并明确“无 SD 卡槽只能从 eMMC 加载 FIP” | README:17/338-361；`.csc` `write_uboot_platform(){ dd ... seek=1 }`；batocera `genimage.cfg` u-boot 分区 |

> **冲突裁决（关键）**：三份外部参照都走“主线 U-Boot 写 eMMC FIP”，与边界一直接冲突，**一律不采纳**；它们对硬件（无 SD 槽、8822CS、音频、LED、风扇）的描述与本机实测一致，作为硬件事实采纳。引导可行性的最高权威是**本机一手实测**（EE 用原厂 U-Boot 从 U 盘启动成功 = stock U-Boot 具备 `usb start`/`fatload usb 0:1`/`bootm` 能力，见 `aml_autoscript.src:7,10`）。

---

## 1. 仓库里“两套 GPU 栈如何切换”的真实机制

结论先行：**编译期决定“能不能出”，运行期决定“当前用哪套”；两套在同一镜像内并存，默认 `libmali`。**

### 1.1 编译期：三个开关 + 两条包依赖

1. `GRAPHIC_DRIVERS`（`devices/S922X/options:46` = `panfrost`）
   - `config/graphic:98-102`：命中 `panfrost` → mesa 加 `GALLIUM_DRIVERS+=panfrost` 与 `VULKAN_DRIVERS_MESA+=panfrost`（panfrost 用户态）。
   - `linux/package.mk:367-373`：**只有**当 `GRAPHIC_DRIVERS` 含 `mali` 且不含 `panfrost` 时才 `--disable CONFIG_DRM_PANFROST`。因此 `GRAPHIC_DRIVERS="panfrost"` 是“保留 panfrost 内核驱动”的必要条件。
2. `MALI_FAMILY`（`devices/S922X/options:45` = `meson`）
   - `packages/graphics/libmali/package.mk:75-76`：`-Dgpu=${MALI_FAMILY} -Dversion=${DRIVER_VERSION} -Dplatform=${PLATFORM}` → 决定拉取 `libmali-meson-<ver>-wayland-gbm.so`。
3. `ADDITIONAL_DRIVERS`（`devices/S922X/options:67` = `RTW88 mali-bifrost`）
   - `packages/linux-drivers/mali-bifrost/package.mk:51-52`：以 `CONFIG_MALI_MIDGARD=m CONFIG_MALI_PLATFORM_NAME=meson ...` 编译厂商 DDK，`:55-60` 安装 `mali_kbase.ko`。
   - `mali-bifrost/package.mk:42-49`：**只有 `S922X` 走 `MALI_PLATFORM="meson"`，其余走 `devicetree`**（A311D 必须新增分支，否则厂商 DDK 在 Amlogic 上取时钟/电源域会失效）。
4. 用户态版本选择：`libmali/package.mk:21-39`，`S922X → DRIVER_VERSION="r51p0"` 且追加 `vulkan-wsi-layer vulkan-tools`（:24）；`S905 → g24p0`（:27）。
5. 两套驱动“不许自动加载”：`packages/graphics/gpudriver/modprobe.d/mali-gpu-noauto.conf`（`blacklist panfrost` / `blacklist mali_kbase`），只由 `gpudriver --start` 显式加载。

### 1.2 运行期：`gpudriver` + `gpu.driver` 设置

- `packages/graphics/gpudriver/sources/bin/gpudriver:7` 设置键 `gpu.driver`；`:9-15` 未设置时**默认 `libmali`**。
- `:24-36` 选 `libmali`：`modprobe -r panfrost` → `modprobe mali_kbase` → 把 `/usr/lib/mali/*` bind-mount 覆盖到 `/usr/lib/`，`/usr/lib/libGL.so` 绑到 `/dev/null`。
- `:37-45` 选 `panfrost`：`modprobe -r mali_kbase` → `modprobe panfrost` → 反向 umount。
- `:53-68` 子命令：`--options` 打印 `@GPU_OPTIONS@`（`package.mk:51-53` 由 `GPU_OPTIONS` 注入，S922X 为 `"panfrost libmali"`，:27）；`--start` 按设置加载；`libmali|panfrost` 写设置并执行 `@DTB_OVERLAY_UNLOAD@` / `@DTB_OVERLAY_LOAD@`。
- 开机自动执行：`autostart/003-gpudriver:5` `/usr/bin/gpudriver --start`。

### 1.3 双 GPU 节点 DTB：`S922X` 的核心技巧

- `devices/S922X/patches/linux/S922X/0007-arm64-meson-g12-common-add-dual-panfrost-mali-to-dts.patch`：
  - 给 `mali: gpu@ffe40000`（`compatible="amlogic,meson-g12a-mali","arm,mali-bifrost"`）加 `status="disabled"`；
  - 新增 `mali_vulkan: gpu_mali_vulkan@ffe40000`（`compatible="arm,mali-midgard"`，额外 reg `0xFFD01000/0xFF800000/0xFF63c000`，`system-coherency=<0>`、`power_policy="always_on"`、`status="okay"`）→ 供厂商 `mali_kbase`（meson 平台）使用。
- 反向切换 overlay：`packages/kernel-drivers/device-tree-overlays/sources/S922X/driver-gpu-panfrost.dts:1-18`（`&mali` = okay，`&mali_vulkan` = disabled）。
- 运行期改 overlay 的方式：`sources/dtb_overlay:5`（`SPECIALIZATIONS="... driver-gpu"`）、`:19/:42` **`grep FDTOVERLAYS /flash/extlinux/extlinux.conf`**、`:84-103` `write_dtbo()`（remount rw `/flash` → 改写/插入 `FDTOVERLAYS` 行 → remount ro）、`:149-151` `set driver-gpu <dtbo>`。

### 1.4 决定性命中：链式引导没有 extlinux.conf

`dtb_overlay` 的唯一落点是 `/flash/extlinux/extlinux.conf`（:19/:42/:95-101），而链式引导的 FDT 来自 `fatload ... dtb.img`（`Generic_cfgload.src:46`）。因此 **`gpudriver` 的 `libmali/panfrost` 切换在链式引导下“改设置但改不动 DTB”**，只切内核模块会与 DTB 节点状态自相矛盾（厂商 DDK 找不到 `arm,mali-midgard` 节点 / panfrost 拿不到 `&mali`）。

### 1.5 本方案的链式等价切换设计（新增，需用户批准）

1. **两份同源 dtb**
   - `meson-g12b-a311d-cainiao-cniot-core.dts`：`mali_vulkan`(midgard) `okay`、`mali`(bifrost) `disabled` → 厂商 DDK（默认，对齐 `gpudriver:12` 默认 `libmali`），产物作 `dtb.img`。
   - `meson-g12b-a311d-cainiao-cniot-core-panfrost.dts`：反之（`mali` okay、`mali_vulkan` disabled），产物作 `dtb-panfrost.img`。
   - 两者 `#include "meson-g12b-a311d-cainiao-cniot-core-common.dtsi"`，差异仅 status 两行。
2. **`config.ini` 新增键** `gpu_dtb`（默认空/`dtb.img`）。
3. **`Generic_cfgload.src` 支持可变 dtb**：`fatload ... ${dtb_mem_addr} ${dtbfile}`，`dtbfile` 默认 `dtb.img`，被 `config.ini` 的 `gpu_dtb` 覆盖（`config.ini` 在 `:18` 已被 `env import -t`）。
4. **`gpudriver` A311D 分支**把 `@DTB_OVERLAY_LOAD@/@DTB_OVERLAY_UNLOAD@` 指向新脚本 `gpu-dtb set panfrost|libmali`（写 `/flash/config.ini`），重启后生效；UI 需提示“重启生效”。
5. 备选（若不接受改共享脚本）：保持 `dtb.img` 唯一，改为在 `kernel.img` 里预置两个 `.dtb` 并在 `cfgload` 用 `fdt addr`/`fdt resize` 动态改写 status —— **不推荐**（原厂 U-Boot 的 fdt 命令支持度未知，风险更高）。

---

## 2. 设备归属结论：**新建独立 `DEVICE=A311D`**（不做 S922X 的 SUBDEVICE）

理由（全部有代码依据）：

1. **写盘步骤必须隔离**：S922X 的 `mkimage_options="dtb,extlinux,gou_extras,uboot"`（`config.xml:212`）中 `uboot` 会走到 `bootloader/mkimage:39-42` 的 `dd u-boot.bin seek=1`；A311D 只能用 `aml_boot`（`config.xml:173` 的 S905 形态，`mkimage_aml_boot` 在 `:169-190`，只复制脚本/dtb，不写盘）。
2. **UPDATE 通道必须隔离**：`devices/S922X/bootloader/update.sh:42-45` 会在设备上写 `u-boot.bin` 到 `seek=1`；`devices/S905/bootloader/update.sh` 全篇无写 U-Boot 动作（:9-48 只更新 dtb.img/device_trees/启动脚本）。独立 DEVICE 才能真正拿到 S905 版 update.sh。
3. **镜像参数必须不同**：`KERNEL_TARGET`/`BUILD_ANDROID_BOOTIMG`/`ANDROID_BOOTIMG_OPTIONS`/`KERNEL_NAME` 是 options 级变量（S905 `options:30-33`；S922X 为 `Image`，`:30`，无 bootimg），无法在同一 DEVICE 内并存。
4. **内核复用仍成立**：`linux/package.mk:69-71` 的 `S922X|RK3399|RK3566 → 6.18.21 kernel.org` 只需把 `A311D` 加进该分支；`PKG_PATCH_DIRS="${LINUX} mainline ${DEVICE} default"`（:19）天然支持独立设备目录，互不污染。
5. **SUBDEVICE 的可行但不划算**：子设备机制存在（`distributions/ROCKNIX/config/functions:264`、`:369-385` 收集/执行子设备 `mkimage_options`），可给 S922X 加一个带 `mkimage_options="aml_boot"` 的 `<file>`；但 DEVICE 仍是 S922X → 继承 S922X 的 options/update.sh/u-boot 包（odroidgou）与 `UBOOT_FIRMWARE="amlogic-boot-fip"`（:42），要逐项否决，反而更易误伤边界一。
6. 代价：内核配置与 dts 需要各维护一份（可由 S922X conf 复制 + 增量），接受。

---

## 3. 文件级改动清单

### 3.0 目标目录骨架（新增）

```
projects/ROCKNIX/devices/A311D/
├── options
├── linux/
│   ├── linux.aarch64.conf
│   └── dts/amlogic/
│       ├── meson-g12b-a311d-cainiao-cniot-core-common.dtsi
│       ├── meson-g12b-a311d-cainiao-cniot-core.dts          # 默认：厂商 DDK
│       └── meson-g12b-a311d-cainiao-cniot-core-panfrost.dts  # 可选：panfrost
├── bootloader/
│   ├── config.ini
│   ├── mkbootimg
│   ├── release
│   ├── update.sh
│   └── scripts/
│       ├── aml_autoscript.src
│       └── Generic_cfgload.src
└── packages/
    └── u-boot/
        └── package.mk
```

### 3.1 新增文件（逐条：改动点 + 依据）

| 文件 | 内容要点 | 依据 |
|---|---|---|
| `devices/A311D/options` | 以 S922X options 为骨架，Amlogic 引导段换成 S905 形态：`TARGET_CPU="cortex-a73.cortex-a53"`；`KERNEL_TARGET="Image.lzo"`；`KERNEL_NAME="kernel.img"`；`BUILD_ANDROID_BOOTIMG="yes"`；**`ANDROID_BOOTIMG_OPTIONS="--base 0x0 --kernel_offset 0x01080000 --ramdisk_offset 0x01000000"`**；`KERNEL_MAKE_EXTRACMD=" dtbs"`；`HB_CPU="Amlogic A311D"`；`BOOTLOADER="u-boot"`；`UBOOT_FIRMWARE=""`；`MALI_FAMILY="meson"`；`GRAPHIC_DRIVERS="panfrost"`；`PREFER_GLES="yes"`；`DISPLAYSERVER="wl"`；`WINDOWMANAGER="swaywm-env"`；`ADDITIONAL_DRIVERS="RTW88 mali-bifrost"`；`ADDITIONAL_PACKAGES="libmali"`；`ROCKNIX_DEVICE_TREE_OVERLAYS=""`（P1，理由见 3.6）；`ROCKNIX_JOYPAD="no"` | S922X `options:11,45-46,49,58,61,67,75`；S905 `options:30-36,42,45,90,93`；`linux.aarch64.conf` 证据见 3.2；bootimg 值来自本机实测 |
| `devices/A311D/linux/linux.aarch64.conf` | 复制 `devices/S922X/linux/linux.aarch64.conf`（6.18.21），增量：`CONFIG_KERNEL_LZO=y` + `# CONFIG_KERNEL_GZIP is not set`；`CONFIG_RD_ZSTD=y`（保留 `CONFIG_RD_LZO=y`）；`CONFIG_RTW88=m`/`CONFIG_RTW88_SDIO=m`/`CONFIG_RTW88_8822CS=m`（P2，可先用 `ADDITIONAL_DRIVERS` 的外置 RTW88 兜底）；`CONFIG_BT_HCIUART_RTL=y` 已存在 | `KERNEL_TARGET="Image.lzo"` 要求 `CONFIG_KERNEL_LZO`；ramdisk 由 `mkbootimg` zstd 压缩（`devices/S905/bootloader/mkbootimg:11-12`），需 `CONFIG_RD_ZSTD`（S905 conf:205 即如此）；S922X conf 现为 `CONFIG_RD_LZO=y`/`RD_ZSTD` 未开（:223/:225），且无任何 `CONFIG_KERNEL_*` 压缩项（仅 `:314 KERNEL_MODE_NEON`） |
| `devices/A311D/linux/dts/amlogic/*.dts{,i}` | 自写 mainline G12B 菜鸟 dts（外设清单见 3.5），双变体 | dts 内容以 batocera 同板 dts 为基准（见 §6），节点命名与 mainline 一致 |
| `devices/A311D/bootloader/mkbootimg` | 复制 S905 版，仅 `ANDROID_BOOTIMG_OPTIONS` 由 options 提供，无需改脚本本体 | `devices/S905/bootloader/mkbootimg:5-16`；`linux/package.mk:513` 用 `find_file_path bootloader/mkbootimg`（`config/functions:1181-1183`，设备目录优先） |
| `devices/A311D/bootloader/release` | 复制 S905 版：把 `usr/share/bootloader` → `3rdparty/bootloader`，`device_trees` 入列，选默认 dtb 生成 `dtb.img`；P3 追加生成 `dtb-panfrost.img` | `devices/S905/bootloader/release:4-11,14-23` |
| `devices/A311D/bootloader/update.sh` | 复制 S905 版：只更新 `dtb.img`/`device_trees`/脚本（含 `cfgload_env`），**无任何 U-Boot 写盘**；P3 加 `dtb-panfrost.img` 同步 | `devices/S905/bootloader/update.sh:6-48`（尤其 :12-21、:30-45）；对照被禁止的 `S922X/bootloader/update.sh:42-45` |
| `devices/A311D/bootloader/config.ini` | 复制 S905 版（`vout/hdmitx/hdr_policy/quiet/consoleopt/cec/mac/wol/gpio...`）；新增注释模板 `# gpu_dtb=dtb-panfrost.img` | `devices/S905/bootloader/config.ini:1-35`；解析点 `Generic_cfgload.src:18` |
| `devices/A311D/bootloader/scripts/aml_autoscript.src` | 原样复制（搜索顺序 `mmc 0:1 → usb 0:1 → mmc 1:p`）；本板无 SD 槽，实际生效为 `usb 0:1` | `S905/scripts/aml_autoscript.src:5-12`；README:17 无 SD 卡槽 |
| `devices/A311D/bootloader/scripts/Generic_cfgload.src` | 复制 S905 版并改两处：①新增 `setenv dtbfile "dtb.img"` 与 `if test "${gpu_dtb}" != ""; then setenv dtbfile "${gpu_dtb}"; fi`；②`:46` 改为 `fatload ... ${dtb_mem_addr} ${dtbfile}` | `S905/scripts/Generic_cfgload.src:7,12,18,38,45-52` |
| `devices/A311D/packages/u-boot/package.mk` | 复制 S905 版（纯脚本生成器，`u-boot 2025.04` tarball 仅做 `mkimage` 宿主工具），**不要**任何 `defconfig`/`u-boot.bin` | `S905/packages/u-boot/package.mk:4-12,16-38`；对照被禁止的 `S922X/packages/u-boot/package.mk:23,30,38` |

### 3.2 修改共享文件（逐条：行号 + 建议改法）

| 文件:行 | 现状 | A311D 改法 | 理由 |
|---|---|---|---|
| `projects/ROCKNIX/config.xml:173` 附近 | `<S905 mkimage_options="aml_boot" dtb_prefix="amlogic">…</S905>`（:173-211） | 新增同级 `<A311D mkimage_options="aml_boot" dtb_prefix="amlogic">`，列两枚 dtb：`meson-g12b-a311d-cainiao-cniot-core`、`meson-g12b-a311d-cainiao-cniot-core-panfrost` | `functions:264` 收集子项、`:369-385` 执行 `mkimage_aml_boot`；`aml_boot` 步骤不写盘（`bootloader/mkimage:169-190`） |
| `Makefile:29` `world:` | 含 S922X，**无 S905** | **不加 A311D 到 world**（与 S905 同策略，避免 CI 成本）；如用户要求进 CI 再加 | S905 至今不在 world，却能人肉构建 |
| `Makefile:42-45` 附近 | `S922X:` 双架构目标 | 新增 `A311D:` 目标：`unset DEVICE_ROOT` + `PROJECT=ROCKNIX DEVICE=A311D ARCH=aarch64 ./scripts/build_distro`（暂不做 32 位） | 与 `S922X:`/`A527:`（:42-45,:72-74）同构；`docker-%`（:109）自动可用 `make docker-A311D` |
| `packages/linux/package.mk:69` | `S922X\|RK3399\|RK3566) → PKG_VERSION="6.18.21"` | `S922X\|RK3399\|RK3566\|A311D)` | 否则落到 `:73-77` 的 `6.12.79` 分支，与 S922X GPU/DTB 方案脱节 |
| `packages/linux/package.mk:112-115` | `elif S905 → zstd:host + bl30` | 拆成：`elif S905\|A311D → zstd:host`；`bl30` 仍仅 S905 | `mkbootimg:11-12` 需要 zstd；bl30 是 S905 厂商路径专用 |
| `packages/linux/package.mk:334-338` | `if S905 → CONFIG_INITRAMFS_SOURCE=""`（外部 ramdisk） | `S905` 或 `A311D` | 链式引导用外部 zstd ramdisk，内核内嵌 initramfs 会导致双重 ramdisk |
| `packages/linux/package.mk:508` | `if BUILD_ANDROID_BOOTIMG=yes -a DEVICE=S905` 才生成 boot.img | 条件扩展为 `S905` 或 `A311D` | 否则 A311D 不出 `kernel.img` |
| `packages/linux/package.mk:616-633` | `if S905 → common_drivers dtbs → device_trees`；否则平铺 dtb | 新增 `elif A311D` 分支：从 `arch/${TARGET_KERNEL_ARCH}/boot/dts/amlogic/meson-g12b-a311d-cainiao-cniot-core*.dtb` 拷进 `usr/share/bootloader/device_trees/` | 链式 `release` 脚本按 `device_trees/` 取 dtb（`S905/release:7-11,15-23`） |
| `packages/graphics/libmali/package.mk:21-39` | `case DEVICE` 无 A311D | 新增 `A311D)` = `DRIVER_VERSION="r51p0"` + `PKG_DEPENDS_TARGET+=" vulkan-wsi-layer vulkan-tools"` | 与 S922X 同 G52/`meson` 族（:22-25）；`MALI_FAMILY` 已在 options |
| `packages/graphics/libmali/package.mk:113` | `=~ S922X\|RK3326\|...` 才把 `libMaliVulkan.*` 移出 `/usr/lib/mali` | 正则加 `A311D` | 否则 Vulkan 库会被 bind-mount 到 `/usr/lib/` 时被“藏”在 mali 目录 |
| `packages/linux-drivers/mali-bifrost/package.mk:42-49` | 仅 `S922X → MALI_PLATFORM="meson"` | 分支加 `A311D` | 否则走 `devicetree`，Amlogic 时钟/电源域处理错误 |
| `packages/graphics/gpudriver/package.mk:18-49` | `case DEVICE`：S922X 用 `dtb_overlay set driver-gpu ...` | 新增 `A311D)`：`PAN="panfrost"`、`GPU_OPTIONS="panfrost libmali"`、`DTB_OVERLAY_LOAD/UNLOAD` 指向 `gpu-dtb set panfrost|libmali` | 链式引导无 extlinux（`dtb_overlay:19,42`）→ 必须换路径；默认值对齐 `gpudriver:12` |
| `packages/graphics/gpudriver/package.mk:13-16`（`post_makeinstall_target`） | 只装 `bin/gpudriver` | 追加：当 `DEVICE=A311D` 时安装 `sources/bin/gpu-dtb`（新脚本，写 `/flash/config.ini` 的 `gpu_dtb` 键，remount rw/ro 参照 `dtb_overlay:92,102`） | 运行期切换的落点 |
| （可选）`packages/linux/package.mk:181-220`（`post_patch`） | 仅 S905 有 `install_common_drivers` + dts 注入 | 若采用「设备 dts 目录」方案：新增 `A311D` 段 `rsync` dts 到 `arch/arm64/boot/dts/amlogic/`，并 `>>` 追加 `dtb-y += <name>.dtb`；**禁止**像 S905 那样整体覆盖目标 Makefile | `install_common_drivers:174-178` 覆盖的是 common_drivers 自有目录（`S905/linux/dts/amlogic/Makefile:1-44` 是完整 vendored 列表），mainline `Makefile` 不能被覆盖 |

> 说明：若不想改 `linux/package.mk` 的 `post_patch`，可退回 **S922X 的 patch 方式**（`devices/S922X/patches/linux/S922X/0008-...patch` 仅新增 dts 文件，不改 Makefile），再配合 `KERNEL_MAKE_EXTRACMD` 显式 dtb 目标（`S922X/options:33` 的 `$(get_kernel_make_extracmd)`）。两种方式都可行，方案默认采用 **`dtbs` + 设备 dts 目录 + append `dtb-y`**（可读性更好、便于维护两份变体）。

### 3.3 从 S905 借用的东西（链式引导包装）

| 借用项 | 源 | 必须的改写 |
|---|---|---|
| `aml_autoscript.src` | `devices/S905/bootloader/scripts/aml_autoscript.src:1-19` | 无（`usb 0:1` 即本板唯一可用介质） |
| `Generic_cfgload.src` | 同上 `Generic_cfgload.src:1-52` | 两处：`dtbfile` 变量（§1.5）、保持 `BOOT_IMAGE=kernel.img boot=LABEL=AURKNIX disk=LABEL=STORAGE`（:14）与本仓库 label 一致（`distributions/ROCKNIX/options:203-204`） |
| `config.ini` | `devices/S905/bootloader/config.ini:1-35` | 增 `gpu_dtb` 模板键；`max_freq_a53`/`cec_func_config` 等厂商键对 mainline 内核无效，保留但注明“惰性” |
| `mkbootimg` | `devices/S905/bootloader/mkbootimg:1-16` | **不照抄 `--kernel_offset 0x2000000`**（`S905/options:33`），改用本机实测 `0x01080000`（+显式 `--ramdisk_offset 0x01000000`） |
| `release` / `update.sh` | `devices/S905/bootloader/release:1-24`、`update.sh:1-50` | dtb 列表换成 a311d 两枚；`update.sh` 增加 `dtb-panfrost.img` |
| `packages/u-boot/package.mk` | `devices/S905/packages/u-boot/package.mk:1-38` | 无（纯脚本 mkimage 生成器） |
| 内核 `CONFIG_RD_ZSTD=y` 范式 | `devices/S905/linux/linux.aarch64.conf:205`（另 `:199-204`） | 无 |

### 3.4 从 S922X 借用的东西（mainline 内核 / GPU / 显示）

| 借用项 | 源 | 改写 |
|---|---|---|
| 内核版本与补丁框架 `6.18.21` + `PKG_PATCH_DIRS` | `packages/linux/package.mk:69-71`、`:19` | 仅加 DEVICE 名（§3.2） |
| 内核配置基线（`DRM_MESON=y`、`DRM_PANFROST=m`、`SND_SOC_MESON_T9015=y`、`SND_SIMPLE_CARD=y`、`CEC_MESON_G12A_AO=y`、`CPUFREQ_DT=y`、`SCHED_MC=y`、`DRM_SIMPLEDRM` 已关） | `S922X/linux/linux.aarch64.conf:3825-3842,4101,4372-4373,3282-3283,642,663,3630,3864` | 复制后增量见 §3.1 |
| 双 GPU 节点思路 | `patches/linux/S922X/0007-...patch` | **复制到板级 dts 内部**（两变体各写一次 status），不改 `meson-g12-common.dtsi`（避免影响其他 G12 板） |
| `libmali` r51p0 + Vulkan | `libmali/package.mk:22-25`、`:113` | 加 A311D（§3.2） |
| `mali-bifrost` meson 平台 | `mali-bifrost/package.mk:42-49` | 加 A311D |
| `gpudriver` 用户态切换逻辑（modprobe 互斥 + bind-mount） | `gpudriver/bin:21-50` | **不改逻辑**，只换 DTB 落点（§1.5） |
| **不要**借用：`odroidgou_defconfig` u-boot、`amlogic-boot-fip`、`extlinux`/`gou_extras` mkimage 步骤、`update.sh` 的 `seek=1` | `S922X/packages/u-boot/package.mk:23,38`；`config.xml:212`；`S922X/bootloader/update.sh:42-45` | 全部与边界一冲突 |

### 3.5 新写 mainline g12b 菜鸟 dts 的外设清单（分阶段启用）

**P1（最小可验证集：能起系统 + 能进 ES）**
- `memory@0`（`0x0..0x77359400` = 2 GB）
- `vddcpu_a`（`pwm_AO_cd`，ch1）+ `vddcpu_b`（`pwm_ab`，ch0）pwm-regulator + `cpu_opp_table_0`/`cpub_opp_table_1`（**缺它 CPU 起不来或死机**）
- `&cpu0/&cpu1 → cpu-supply=<&vddcpu_b>`；`&cpu100..&cpu103 → cpu-supply=<&vddcpu_a>`
- `usb@ff500000`（dwc3，`CONFIG_USB_DWC3_MESON_G12A=y` 已开 :4599）+ hub 电源 `GPIOH_8`/`GPIOAO_6`/`GPIOA_14`（**U 盘是根介质，掉电即失根**）
- `&hdmi`/`&vpu`/dw-hdmi 相关（mainline 默认 `DRM_MESON=y` :3825）
- `&uart_AO`（console `ttyAML0`，与 `EXTRA_CMDLINE`/`consoleopt` 一致）
- `&saradc` + `adc-keys`（recovery）+ 电源键 `gpio-keys-polled`（ES 退出/关机路径）
- `emmc_pwrseq`（`BOOT_12`）与 `&sd_emmc_c`（eMMC 可选，P1 可不启用；不写盘亦可）
- `mali`(bifrost) + `mali_vulkan`(midgard) 双节点（默认 `mali_vulkan=okay`）

**P2（外设补齐）**
- `&ethmac`：RGMII + `rtl8211f`、`tx-delay=2`、复位 `GPIOZ_15`、中断 `IRQID_GPIOZ_14`
- `&sd_emmc_a`：SDIO 4-bit、`max-frequency=100MHz`、`keep-power-in-suspend`、`vmmc=vsys_3v3`/`vqmmc=vddao_1v8`、`sdio_pwrseq`（`GPIOX_6`）+ `wifi32k`（`pwm_ef` 32768 Hz）→ 节点内 `rtl8822cs: wifi@1`
- `&uart_A`：`rtl8822cs-bt`，`GPIOX_17/18/19`（BT 使能/复位）
- `axg-sound-card` + `&tdm`/`&frddr`/`&toacodec`/`&tohdmitx` + `ht6872` 功放（`GPIOAO_2`）
- `pwm-fan`（`pwm_cd` ch1，tach `GPIOA_3`）+ `&cpu_thermal` trips（55 °C，hysteresis 5 °C，cooling-map→fan0）
- `&spicc1`（WS2812，`spidev`，~3.34 MHz）——注意 mainline 与 README 的 2.4 MHz demo 差异，实测调
- `&usb`（host）+ `&pcie`（底座 VGA/HDMI 型号差异，按需）

**明确不覆盖**：IR（本板无 IR 接收头，batocera dts 亦无；S905 conf 的 `CONFIG_IR_MESON=m`(:3631) 可留可关）、NPU（README:304-316 仍在试验）。

### 3.6 GPU 两套栈相关的 package.mk 分支（汇总 diff 视图）

```make
# packages/graphics/gpudriver/package.mk  (case ${DEVICE} 内新增)
    A311D)
      PAN="panfrost"
      GPU_OPTIONS="panfrost libmali"
      DTB_OVERLAY_LOAD="\/usr\/bin\/gpu-dtb set panfrost"
      DTB_OVERLAY_UNLOAD="\/usr\/bin\/gpu-dtb set libmali"
    ;;

# packages/graphics/libmali/package.mk  (case "${DEVICE}" 内新增)
  A311D)
    DRIVER_VERSION="r51p0"
    PKG_DEPENDS_TARGET+=" vulkan-wsi-layer vulkan-tools"
  ;;
# 并把 :113 的 =~ S922X|RK3326|RK3566|RK356X 加上 A311D

# packages/linux-drivers/mali-bifrost/package.mk  (make_target 内)
    S922X|A311D)
      MALI_PLATFORM="meson"
```

> `ROCKNIX_DEVICE_TREE_OVERLAYS`：P1 设 `""`（与 `S905/options:90` 一致）→ `packages/virtual/linux-drivers/package.mk:21-23` 不拉 `device-tree-overlays`，也就不装 `dtb_overlay`（避免它去 grep 不存在的 extlinux.conf）。P3 若想要 `undervolt-gpu` 之类叠加，再考虑 `yes` + `sources/A311D/*.dts`（`device-tree-overlays/package.mk:10-18`）。

---

## 4. 分阶段实施与验证（测试机 `192.168.31.57`）

> 验证总原则：**设备本身零写入**。所有 ROCKNIX 验证都在**另一支 U 盘**上完成，物理换盘启动；换回 EE 原盘即“零成本回滚”。`.57` 上只做只读采样（SSH 到 EE 现网系统）。

### P0 —— 只读基线核对（零构建、零写入）

- 目标：把“能不能喂进去”的不确定性压到最低。
- 动作（全部只读）：`cat /proc/device-tree/model`、`cat /proc/cmdline`、`/flash/kernel.img` 头字段复测、`ls /flash`、`ls /flash/device_trees`、`cat /proc/mounts | grep flash`、`dmesg | grep -Ei 'usb|8822|rtw|mali'`、`cat /proc/cpuinfo`。
- 通过标准：bootimg 头仍为 `0x01080000/0x01000000`；`/flash` 为 FAT 且 label `AURKNIX`；原厂 U-Boot 能识别 `usb 0:1`（EE 已证）。
- 回滚：无需（只读）。
- **失败即改道**：若原厂 U-Boot 不支持 `bootm` 解 LZO 的 **mainline Image**，P1 改用 `Image.gz`（`CONFIG_KERNEL_GZIP=y`），再退一步用 `booti`（raw `Image` + `initrd` + `fdt` 三分参，需原厂 U-Boot 有 `booti` 命令）。

### P1 —— 最小可验证集：能起系统 + 能进 ES（GPU 用默认 libmali 栈）

- 改动范围：§3.1 全部新增文件 + §3.2 中标 P1 的共享文件改动（config.xml、Makefile、linux/package.mk 6 处、libmali、mali-bifrost、gpudriver 的 A311D 分支；`gpu-dtb` 脚本可延后到 P3，此时 `DTB_OVERLAY_*` 置空，等价 `RK356X` 分支 `gpudriver/package.mk:31-36`）。
- dts 只启用 §3.5 P1 清单。
- 构建：`make docker-A311D`（或 `PROJECT=ROCKNIX DEVICE=A311D ARCH=aarch64 ./scripts/build_distro`），产物 `target/ROCKNIX-aarch64-*.img.gz`（`scripts/image:100-111`）。
- 写盘对象：**新 U 盘**（`xz -dc *.img.gz | dd of=<新U盘>` 或 Rufus/Etcher）。
- 验证：插入 `.57` 上电 → HDMI 有画面 → ES 前台 → SSH 可登录 → `/storage` 可写、`/flash` 可读 → 连续 3 次冷启动稳定。
- 通过标准：以上全绿 + `dmesg` 无 `Kernel panic`、无 `Unable to mount root`（root 通过 `boot=LABEL=AURKNIX` 找到）。
- 回滚：断电，换回 EE 原 U 盘。
- 未过则定位分层：无 HDMI → dts/dw-hdmi；无 SSH → 网络未起（P2 才加网卡，P1 用串口或 `systemd.debug_shell`）；启动即重启 → bootimg 地址/压缩格式。

### P2 —— 外设补齐（网络优先，便于后续全远程验证）

- 范围：§3.5 P2 全部（ethmac→SDIO 8822CS→BT→音频→风扇/温控→LED→USB 触点）；内核 conf 补 `CONFIG_RTW88*`（或启用外置 `RTW88` 驱动包，`S922X/options:67` 现有做法）。
- 验证（只读）：`ip -s link show eth0`、`iw dev`/`nmcli`、`aplay -l` 看到 `cniotcore` 三 device、`alsactl init hw:cniotcore` + `alsaucm ... HDMI/Speaker`（UCM 可借 `armbian/build/packages/bsp/cainiao-cniot-core/*.conf`，`.csc` 的 `PACKAGE_LIST_BOARD="alsa-ucm-conf"` 即为此）、`cat /sys/class/thermal/thermal_zone*/temp`、`sensors`。
- 通过标准：GBE 协商 1 Gbps；WiFi 关联 + BT 扫描；HDMI 与内置扬声器可同时出声；风扇随温度启停。
- 回滚：仅换盘；不改 eMMC。

### P3 —— 双 GPU 栈切换（边界二的兑现）

- 范围：第二枚 dtb（panfrost 变体）、`config.ini` 的 `gpu_dtb`、`Generic_cfgload.src` 的 `dtbfile`、`gpudriver` 的 `DTB_OVERLAY_*` 指向 `gpu-dtb`、`release`/`update.sh` 增加 `dtb-panfrost.img`。
- 验证（两栈都要跑一遍）：
  1. `gpudriver` → 应输出 `libmali`（默认，`gpudriver:12`）；
  2. `gpudriver panfrost` → 检查 `/flash/config.ini` 出现 `gpu_dtb=dtb-panfrost.img` → 重启；
  3. 重启后 `cat /proc/device-tree/soc/gpu*/status`、`lsmod | grep -E 'panfrost|mali'`、`gpudriver` 输出 `panfrost`、`weston-simple-egl`/`kmscube` 或 ES 正常渲染；
  4. 反向切回 `libmali` → 重启 → `mali_kbase` 加载、`/usr/lib/mali` bind-mount 生效（`grep /usr/lib/libEGL.so /proc/mounts`）。
- 通过标准：两栈各自“能跑 + 状态自洽（DTB 节点 status 与已加载模块一致）”；切换命令后 UI 提示重启，重启后生效。
- 回滚：`gpudriver libmali` 或手工把 `config.ini` 的 `gpu_dtb` 改回 `dtb.img`。
- **警示**：`simpledrm` 与 panfrost 冲突（`.csc` 的 `MODULES_BLACKLIST="simpledrm"`）；本仓库 S922X conf 已 `# CONFIG_DRM_SIMPLEDRM is not set`（:3630）与 `# CONFIG_FB_SIMPLE is not set`（:3864），A311D conf 必须保持这两项关闭。

### P4 —— 调频/亲和性/性能收尾

- 范围：确认 `policy*` 数量与顺序（G12B mainline：`cpu0/1`=A53、`cpu100..103`=A73）；参照 `packages/hardware/quirks/platforms/S922X/010-governors:6`（`policy0`/`policy2`）与 `:8-17`（按 GPU 栈选择 devfreq 路径：`ffe40000.gpu_mali_vulkan` vs `ffe40000.gpu`）；为 A311D 新增对应 quirk 平台文件；`config.ini` 的 `max_freq_*` 对 mainline 无效，如需要改用 `cpufreq` 服务/quirks。
- 验证：`cat /sys/devices/system/cpu/cpufreq/policy*/scaling_max_freq`、`cat .../ffe40000.gpu*/devfreq/*/cur_freq`、满载 30 min 温漂与降频记录。
- 通过标准：无明显卡顿、温度稳定、GPU 频率随栈正确指向。
- 回滚：删除 `/storage/.config/profile.d/010-governors`。

---

## 5. 风险清单与未解问题

| # | 风险 | 证据/机理 | 缓解 |
|---|---|---|---|
| R1 | **原厂 U-Boot 的 `bootm` 对 mainline `Image.lzo` + zstd ramdisk 的兼容性（头号风险）** | 仓库内唯一被验证的组合是 S905 的**厂商内核**（`linux/package.mk:50-57` CoreELEC linux-amlogic）+ `Image.lzo`（`S905/options:30`）+ bootimg；mainline 组合无人验证 | 首选 `Image.lzo`（与本机现网 kernel.img 格式一致）；失败→`CONFIG_KERNEL_GZIP=y` 改 `Image.gz`；再失败→`booti` 三分参（raw `Image`）；最后兜底→P1 先用厂商内核（S905 路径）+ 只做 panfrost 之外的 DDK，双栈顺延 |
| R2 | `kernel.img` 装载地址若用错会直接不启动 | S905 用 `--kernel_offset 0x2000000`（`S905/options:33`），与本机实测 `0x01080000` 不符；原厂 U-Boot `loadaddr` 就是 `0x01080000` | 按 §3.1 显式写 `--base 0x0 --kernel_offset 0x01080000 --ramdisk_offset 0x01000000`；P0 复测头字段 |
| R3 | 8822CS 在 mainline 下的驱动路径 | S922X conf `# CONFIG_RTW88 is not set`（:2196），靠外置 `ADDITIONAL_DRIVERS="RTW88"`（options:67）；同板一手评测标 WiFi/BT Working（README:135-149） | 优先内核内 `rtw88`（6.18 支持 8822cs）；SDIO 供电/时钟完全照 dts：`vmmc=vsys_3v3`/`vqmmc=vddao_1v8`、`sdio_pwrseq`(GPIOX_6)、`wifi32k`(pwm_ef 32768 Hz)、`max-frequency=100MHz`、`keep-power-in-suspend`；BT 需 `uart_A`+`CONFIG_BT_HCIUART_RTL`（已开 :1476）+ 固件 |
| R4 | G12B 调频/亲和性配置与 S922X 不同 | S922X quirk 硬编码 `policy0`/`policy2`（`010-governors:6`）；mainline G12B 是 `cpu0/1`(A53)+`cpu100..103`(A73)，policy 编号需实测；`CONFIG_SCHED_MC=y`（:663）、`ENERGY_MODEL` 关（:594）、`ARM_PSCI_CPUIDLE` 关（:615） | P4 实测后按实际 policy 目录生成 A311D quirk；先不设阈值治理，避免误配 |
| R5 | U 盘介质下 `/storage` 与 roms 布局 | 镜像布局 `SYSTEM_SIZE=2048`（options:191）、`SYSTEM_PART_START=32768`（:194）、`STORAGE_SIZE=32`（:197），`/flash` 有 `resize_storage_10G`（`scripts/mkimage:283-285`）；EE 现网是 `roms/ports` + `ports_scripts` 分裂布局 | 采用 ROCKNIX 单一 `/storage/roms`（不复制 EE 分裂布局）；U 盘 ≥ 64 GB 建议（SYSTEM 2 GB + STORAGE 起步 32 GB 再扩容） |
| R6 | 切换 GPU 栈需重启（链式引导无 overlay） | `dtb_overlay:19,42,95-101` 只改 extlinux.conf | UI/文档明确“切换后重启”；P3 验证闭环 |
| R7 | 无 SD 卡槽 → 无 SD 救援通道 | README:17 明示；`.csc` 同样走 eMMC FIP | 全程不碰 eMMC；测试用独立 U 盘；坏镜像最坏结果=换回原盘 |
| R8 | 2 GB 内存 + Wayland/ES | 同板 2 GB（README:15） | 保留 `ZRAM_COMPRESSION_ALGO="zstd"`（S922X options:84）与 SWAP=0（:90） |
| R9 | HDMI 无 EDID 时无输出 | 链式引导把 `vout=1080p60hz` 塞进 bootargs（`Generic_cfgload.src:7,41-43`），mainline 不解析 `vout` | 必要时给 `EXTRA_CMDLINE` 加 `video=HDMI-A-1:1920x1080@60`（参照 `S922X/bootloader/update.sh:56` 的写法） |
| R10 | 只做 32/64 位中的一种 | `libmali` 32 位依赖 `ADDITIONAL_PACKAGES_32BIT`（`S905/options:81`），S922X 双架构（`Makefile:44-45`） | P1 仅 aarch64；PortMaster 32 位需求出现时再加 `ARCH=arm` 目标 |
| R11 | 外部参照“写 FIP 到 eMMC”与边界一冲突 | README:338-361；`.csc write_uboot_platform`；batocera `genimage.cfg` | 明令禁止；`update.sh`/`mkimage` 步骤级别隔离（§2 理由 1、2） |
| R12 | `simpledrm` 抢 framebuffer 导致 panfrost 起不来 | `.csc` `MODULES_BLACKLIST="simpledrm" # SimpleDRM conflicts with Panfrost` | 保持 `CONFIG_DRM_SIMPLEDRM`/`CONFIG_FB_SIMPLE` 关闭（S922X conf:3630/:3864 现状） |

### 未解问题（需实测或需用户拍板）

1. 原厂 U-Boot 版本/`bootm` 能力细节（`version`、`printenv`）——P0 只读取；决定 R1 走哪条分支。
2. `.57` 是否允许**物理换盘**做 P1 验证（若不允，需另找同板或改走串口）。
3. 是否允许新增 `config.ini` 键与改 `Generic_cfgload.src`（属对外行为扩展，非纯板级）。
4. 默认 GPU 栈选 `libmali`（与本仓库默认一致）还是 `panfrost`（更“纯主线”）。
5. `A311D` 是否进 `world`（CI 成本）。
6. ES/主题与手柄映射是否沿用 S922X 的 `ROCKNIX_JOYPAD="no"` 形态（TV box 无内建手柄，`S905/options:93` 同）。
7. 底座型号差异（HDMI/VGA、USB HUB）是否需要额外 dts 变体（README:50-52 称“软件应该通用”）。

---

## 6. 四份参照对照表（结论分三类）

| 维度 | ROCKNIX 上游 S922X（本仓库 `devices/S922X`） | amlogic-devices `cainiao-cniot-core`（**一手同板评测，主依据**） | batocera.linux `cainiao-cniot-core` | amlogic-devices `aoc-65t33z`（旁证） |
|---|---|---|---|---|
| 结构 | `options` + `linux/linux.aarch64.conf` + `patches/linux/S922X/*` + `packages/{u-boot,amlogic-boot-fip}` + `bootloader/update.sh`（无 `mkimage`/`mkbootimg`） | **只有** `README.md` + `breathing-light/ws2812.c` + `pictures/`（无 device.conf/构建脚本，纯文档） | `genimage.cfg` + `build-uboot.sh` + `create-boot-script.sh` + `boot/extlinux.conf` + `linux-defconfig.config` + `linux-defconfig-fragment.config` + `dts/*.dts` + `patches/u-boot-2026.01/` | `README.md` + `pictures/`（纯文档） |
| SoC | S922X（同为 G12B 家族、Mali-G52） | **A311D / G12B，2 GB，16 GB eMMC，无 SD 槽** | 同左（同板） | **A311D2（T7）**，4 GB/32 GB，无 SD 槽 → **不同 SoC** |
| 引导 | 自带 U-Boot（`odroidgou_defconfig`，`dd ... seek=1`） | **主线 U-Boot 写 eMMC FIP**（`gxlimg` 重封 `DDR.USB`，`dd seek=1` 到 user area 与 boot area） | **主线 U-Boot 写 eMMC**（`genimage.cfg` u-boot 分区 `offset=512`） | 主线 U-Boot（`65t33z_defconfig` + `fip/mk_script.sh`，`seek=1`） |
| bootimg 地址 | 无（extlinux + FDTDIR） | **无（不使用 Android bootimg）** | 无（extlinux `LINUX/FDT`） | 无 |
| 内核 | mainline `6.18.21`（`linux/package.mk:69-71`） | mainline（armbian/build，`KERNEL_TARGET="current,edge"`，无显式 `KERNELBRANCH`） | mainline（`linux-defconfig.config`：`DRM_PANFROST=y`、`RD_*` 全开、`ARCH_MESON=y`） | **仅厂商 5.15**（README 段所述） |
| dts | 自写/补丁新增（0008 RGB10、0007 双 GPU） | 自写 mainline（armbian/build，`BOOT_FDT_FILE="amlogic/meson-g12b-a311d-cainiao-cniot-core.dtb"`） | 自写 mainline（`dts/meson-g12b-a311d-cainiao-cniot-core.dts`，`include "amlogic/meson-g12b-a311d.dtsi"`） | 厂商 dts（khadas-common-drivers/coreelec-common-drivers） |
| GPU 双栈 | **有**：编译期 `GRAPHIC_DRIVERS`/`MALI_FAMILY`，运行期 `gpudriver` + `dtb_overlay` 改 extlinux | **无切换机制**；Armbian 桌面默认 panfrost（`MODULES_BLACKLIST="simpledrm"`） | 单一 `DRM_PANFROST=y`（无厂商 DDK） | 可选 panfrost（改 dts+config），**非运行期双栈** |
| 音频 | `SND_SOC_MESON_T9015` + `SND_SIMPLE_CARD`（conf:4101,4372-4373） | `axg-sound-card` + amixer 路由 + ALSA UCM（`packages/bsp/cainiao-cniot-core/*.conf`） | `linux-defconfig` 含 `SND_SIMPLE_CARD=y`/`SND_SOC_MESON_T9015=y` | 未涉及 |
| 8822CS | 外置 `RTW88` 包 + `BT_HCIUART_RTL` | WiFi/BT Working；SDIO 供电/时钟在 dts（`sdio_pwrseq`+`wifi32k`） | dts `&sd_emmc_a`（4-bit/100 MHz/`rtl8822cs: wifi@1`）+ `&uart_A` BT | 未涉及 |
| 无 SD 槽 | 有槽（型号差异） | **明示无 SD 槽，只能 eMMC 加载 FIP** | 同左 | **同左** |

### 6.1 可直接照搬

1. 同板 dts 的外设拓扑（ethmac/RTL8211F 复位与延时、SDIO 8822CS 供电+32 kHz+100 MHz、`uart_A` BT、`axg-sound-card` 路由、`ht6872` 功放、pwm-fan + thermal trips、adc-keys、`emmc_pwrseq`、USB hub 电源）——batocera dts 与 amlogic-devices README 相互印证。
2. 内核侧的 `panfrost` 与 `meson` 显示/音频 config 集合（本仓库 S922X conf 已具备，:3825-3842,4101,4372-4373）。
3. `libmali` r51p0 + `MALI_FAMILY="meson"`、`mali-bifrost` 的 `MALI_PLATFORM="meson"`（G52 同族）。
4. S905 的链式引导脚本三件套与 `mkbootimg` 框架（只换 offset/文件名）。
5. ALSA UCM 文件（`armbian/build/packages/bsp/cainiao-cniot-core`）用于 P2 音频实例切换。

### 6.2 需要改写

1. **`kernel_offset`**：外部参照都没有 Android bootimg；本机 EE 的 `0x01080000/0x01000000` 才是唯一可用事实 → 覆盖 S905 的 `0x2000000`。
2. **GPU 切换落点**：S922X/ROCKNIX 靠 `extlinux.conf` 的 `FDTOVERLAYS`；链式引导改写为 `config.ini: gpu_dtb` + 两份 dtb + 重启。
3. **dts 交付方式**：不照抄 S922X 的 `patch`（0008 模式）也不照抄 S905 的“整目录 rsync（会覆盖 mainline Makefile）”；改为“设备 dts 目录 + `post_patch` 追加 `dtb-y`”。
4. **引导包装**：外部参照的 FIP/`seek=1` 全部改写为 `aml_boot` 步骤（`config.xml:173` + `bootloader/mkimage:169-190`）。
5. **内核压缩/initramfs**：外部参照用 `Image`+`initrd.lz4`（batocera `extlinux.conf`）；我们必须 `Image.lzo` + zstd bootimg ramdisk（`CONFIG_KERNEL_LZO`/`CONFIG_RD_ZSTD`）。
6. **`update.sh`**：以 S905 版为底（不含 u-boot 写入），不是 S922X 版。

### 6.3 在我们这里不可行（及原因）

1. **任何写 `u-boot.bin` 到 sector 1 / FIP 到 eMMC 的步骤**（四份参照都这么做）：违反边界一；且本板无 SD 槽，写坏 eMMC 无可救（README:473-477 明确救援困难）。
2. **依赖主线 U-Boot 的启动特性**：外部参照依赖主线 U-Boot 的 `boot_targets`/extlinux/script/TFTP 搜盘（README:371-375），原厂 U-Boot 没有这些；我们只有 `aml_autoscript` 的 `mmc 0:1 → usb 0:1 → mmc 1:p` 固定搜索。
3. **aoc-65t33z 的任何实现**：SoC 是 A311D2（T7），与 G12B 不同，且内核仅厂商 5.15、无运行期双栈 → 只能作知识旁证。
4. **`dtb_overlay`/`FDTOVERLAYS` 运行期 DTB 切换**：链式引导无 `extlinux.conf`（`dtb_overlay:19,42`）。
5. **“无 SD 槽也能 SD 试验区”**：本板没有 SD 槽（README:17），此路彻底排除。

---

## 7. 因参照而改变的判断（明确标注）

| 旧判断 | 新判断 | 触发来源 |
|---|---|---|
| A311D 应整体照搬 S905（厂商内核） | 内核/GPU 走 S922X 的 mainline 6.18.21，**只有引导包装**用 S905 | ROCKNIX 上游把 A311D 归 S922X（`config.xml:214` 的 RGB10、patch 0008 `compatible` 含 `amlogic,a311d`）+ 边界二要求 panfrost |
| mainline 路线 = 只用 panfrost | **DDK + panfrost 并存、运行期切换**；S922X 已实现（patch 0007 + `gpudriver`） | 仓库代码（§1） |
| `mkbootimg` 可用 S905 的 `--kernel_offset 0x2000000` | 必须用实测 `0x01080000`（+`--ramdisk_offset 0x01000000`） | 本机 `/flash/kernel.img` 头字段 |
| “必须自己写菜鸟 dts” | 有现成 mainline 同板 dts 可**照搬再改双 GPU 节点** | batocera `dts/meson-g12b-a311d-cainiao-cniot-core.dts`；armbian `.csc` 的 `BOOT_FDT_FILE` 同名 |
| 切 GPU 只需 `gpudriver` | 链式引导还必须换 `dtb.img` 并重启 | `dtb_overlay:19,42` + `Generic_cfgload.src:46` |
| 可以在 SD 上试写 U-Boot 保命 | **本板无 SD 槽**，写 U-Boot 只能进 eMMC，已彻底排除 | README:17 |
| 音频/8822CS/LED 需自行摸 | 同板一手评测已给全套（UCM、`sdio_pwrseq`、`wifi32k`、GPIOH_4 复用 SPI） | README:135-302；batocera dts |
| 写盘“可以试” | **禁止**；三份外部参照均与边界一冲突，一律改写 | `.csc`/`genimage.cfg`/README:338-361 |

---

## 8. 零改动声明与待拍板清单

- 本轮：**未修改仓库任何文件**（`git status --short` 为空，分支 `next`，HEAD `acceeda94b`）；**未向 `192.168.31.57` 写入任何内容**（仅 SSH 只读采样）。
- 落地前需用户确认：
  1. 采纳“独立 `DEVICE=A311D` + 链式引导包装 + S922X mainline 内核/GPU”的总路线；
  2. 允许新增 `config.ini: gpu_dtb` 键并修改 `Generic_cfgload.src`（链式引导下的双栈切换落点）；
  3. 允许修改共享文件（`config.xml`、`Makefile`、`packages/linux/package.mk` 6 处、`libmali`、`mali-bifrost`、`gpudriver`）；
  4. 默认 GPU 栈（建议 `libmali`，与仓库默认一致）；
  5. P1 验证方式（独立 U 盘 + `.57` 物理换盘）；
  6. `A311D` 是否进 `world`（建议暂不进）。
- 构建命令（落地后）：`make docker-A311D`，或 `PROJECT=ROCKNIX DEVICE=A311D ARCH=aarch64 ./scripts/build_distro`；产物 `target/ROCKNIX-aarch64-*.img.gz`（`scripts/image:100-111`）。

---

# 实施记录（P1 落地，2026-10-29）

> 状态：**代码已写入工作区，未 commit、未 push、未构建、未写设备**。
> 校验：`bash -n` 全部通过；`config.xml` 可解析；`make -n` 目标可解析；新增脚本 100% LF；被改的 6 个共享文件行尾未被翻转（仍与仓库原有 CRLF 一致）。

## A. 新增文件（`??` 未跟踪）

| 文件 | 作用 | 依据 |
|---|---|---|
| `projects/ROCKNIX/devices/A311D/options` | 设备变量：`KERNEL_TARGET="Image.lzo"`、`KERNEL_NAME="kernel.img"`、`BUILD_ANDROID_BOOTIMG="yes"`、`ANDROID_BOOTIMG_OPTIONS="--base 0x0 --kernel_offset 0x01080000 --ramdisk_offset 0x01000000"`、`KERNEL_MAKE_EXTRACMD=" dtbs"`、`MALI_FAMILY="meson"`、`GRAPHIC_DRIVERS="panfrost"`、`UBOOT_FIRMWARE=""`、`ROCKNIX_DEVICE_TREE_OVERLAYS=""`、`ROCKNIX_JOYPAD="no"`、`ADDITIONAL_DRIVERS="RTW88 mali-bifrost"`、`ADDITIONAL_PACKAGES="libmali"`、`DEBUG_TTY="/dev/ttyAML0"` | 骨架 = `devices/S922X/options:5-24,26-27,44-46,49-52,58-61,67,75,84-90`；引导段 = `devices/S905/options:30-36,42,45,63,80,90`；bootimg 偏移 = 目标机 `kernel.img` 头实测 |
| `devices/A311D/linux/linux.aarch64.conf` | 内核配置 = S922X conf 原样复制（6.18.21），仅 3 处增量：`CONFIG_RD_ZSTD=y`（:225）、`CONFIG_KERNEL_LZO=y`（:314）、`CONFIG_INPUT_ADC_KEYS=y`（:2259） | `S905/linux/linux.aarch64.conf:199-205`（RD_* 范式）；`mkbootimg:11-12`（ramdisk 用 zstd）；`Generic g12-common` 的 `adc-keys` 需 `INPUT_ADC_KEYS` |
| `devices/A311D/linux/dts/amlogic/meson-g12b-a311d-cainiao-cniot-core.dts` | 唯一默认 mainline g12b dts。P1 仅覆盖：`memory@0`(2 GB)、`vddcpu_a`(`pwm_ab` ch0)/`vddcpu_b`(`pwm_AO_cd` ch1) pwm-regulator、`cpu-supply`×6、`usb_hub_power`(GPIOH_8/GPIOAO_6/GPIOA_14)、`&hdmi_tx`、`&uart_AO`、电源键(`GPIOAO_3`)+`adc-keys`(saradc ch2)、`mali_vulkan`(midgard, okay)/`&mali`(bifrost, disabled) | 节点/标签全部对照 mainline `meson-g12-common.dtsi`（`hdmi_tx:201`、`hdmitx_hpd_pins:381`、`hdmitx_ddc_pins:371`、`pwm_a_e_pins:1970`、`pwm_ao_d_e_pins:2026`、`saradc:2132`、`usb:2478`、`mali:2521`、`gpu_opp_table:62`、`uart_AO:2081`）与 `meson-g12b-a311d.dtsi:7,10-134`（OPP 与 `cpu0/1→cpu_opp_table_0`、`cpu100..103→cpub_opp_table_1` 已由 dtsi 提供，板级只补 `cpu-supply`）；双 GPU 节点形态 = `devices/S922X/patches/linux/S922X/0007-*.patch` |
| `devices/A311D/bootloader/mkbootimg` | 复制 S905 版（zstd 压 initramfs + `mkbootimg`），不硬编码偏移，由 options 提供 | `devices/S905/bootloader/mkbootimg:5-16` |
| `devices/A311D/bootloader/scripts/aml_autoscript.src` | 复制 S905 版：`defenv` + `cfgloadsd/cfgloadusb/cfgloademmc` 搜索（本板无 SD 槽，实际命中 `usb 0:1`）+ `saveenv` | `S905/.../aml_autoscript.src:5-19` |
| `devices/A311D/bootloader/scripts/Generic_cfgload.src` | 复制 S905 版 + **新增 `dtbfile` 逻辑**（默认 `dtb.img`，若 `config.ini` 给了 `gpu_dtb` 则用它），`fatload ... ${dtbfile}`，随后 `bootm` 五步 | `S905/.../Generic_cfgload.src:7,14,18,38,45-52`；`dtb_overlay:19,42` 证明 extlinux 路线在本机不可用 |
| `devices/A311D/bootloader/config.ini` | 复制 S905 模板，新增 `gpu_dtb` 键（默认注释掉，注释说明 `dtb.img`=libmali / `dtb-panfrost.img`=panfrost、重启生效） | `S905/bootloader/config.ini:1-35`；解析点 `Generic_cfgload.src:18` |
| `devices/A311D/bootloader/release` | 复制 S905 逻辑：把 `usr/share/bootloader` 铺到 `3rdparty/bootloader` + `device_trees`，并用 `meson-g12b-a311d-cainiao-cniot-core.dtb` 生成 `dtb.img` | `S905/bootloader/release:1-24`；调用点 `scripts/image:277`（`find_file_path bootloader/release` 设备目录优先，`config/functions` `find_path` 顺序：devices/$DEVICE → PROJECT → PKG_DIR） |
| `devices/A311D/bootloader/update.sh` | 设备内更新：只更新 `dtb.img`（cmp 一致才换）、`dtb-panfrost.img`（若已存在）、`device_trees`、`aml_autoscript`、`config.ini`、`cfgload`、`cfgload_env`；**全程无 U-Boot 写入** | `S905/bootloader/update.sh:6-48`；对照被禁止的 `S922X/bootloader/update.sh:42-45`（`dd seek=1`） |
| `devices/A311D/packages/u-boot/package.mk` | 纯脚本生成器：用 `u-boot 2025.04` 的 `mkimage` 生成 `aml_autoscript`/`Generic_cfgload`/`Generic_cfgload_env` 并装 `config.ini`；**无 defconfig、无 u-boot.bin 产出** | `S905/packages/u-boot/package.mk:4-12,16-38`；包查找顺序 `find_path`：`devices/$DEVICE/packages/$PKG_NAME` 最先 |
| `projects/ROCKNIX/packages/graphics/gpudriver/sources/bin/gpu-dtb` | 链式引导的 GPU 栈切换：`gpu-dtb libmali|panfrost` → remount rw `/flash` → 改/追加 `config.ini` 的 `gpu_dtb=` → remount ro → 提示重启 | `device-tree-overlays/sources/dtb_overlay:92,95-101,102`（同样的 remount+sed 手法），但落点改为 `config.ini`（因无 `extlinux.conf`） |

## B. 修改共享文件（`M`，全部为并列分支扩展，未改既有行为）

| 文件:行 | 改动 |
|---|---|
| `Makefile:44-48`（新增段） | `A311D:` 目标 = `unset DEVICE_ROOT` + `PROJECT=ROCKNIX DEVICE=A311D ARCH=aarch64 ./scripts/build_distro`；**未加入 `world:`（第 29 行）** |
| `projects/ROCKNIX/config.xml:212-215`（新增段） | `<A311D mkimage_options="aml_boot" dtb_prefix="amlogic">` + `<file>meson-g12b-a311d-cainiao-cniot-core</file>`；`aml_boot` → `projects/ROCKNIX/bootloader/mkimage:169-190` 只 mcopy 脚本/dtb，不写盘 |
| `packages/linux/package.mk:69` | 内核分支 `S922X\|RK3399\|RK3566` → 追加 `A311D`（6.18.21 kernel.org） |
| `packages/linux/package.mk:115-117` | 新增 `elif A311D → PKG_DEPENDS_TARGET+=" zstd:host"`（`mkbootimg` 需要 zstd）；`bl30` 仍仅 S905 |
| `packages/linux/package.mk:245-257` | `post_patch`：RK356X 块之后并列新增 A311D 块，把 `devices/A311D/linux/dts/amlogic/*.dts` 对应的 `dtb-$(CONFIG_ARCH_MESON) += …dtb` 幂等追加到内核 `arch/arm64/boot/dts/amlogic/Makefile` |
| `packages/linux/package.mk:350` | `CONFIG_INITRAMFS_SOURCE=""`（外部 ramdisk）条件扩为 `S905 -o A311D` |
| `packages/linux/package.mk:524` / `:578` | `BUILD_ANDROID_BOOTIMG=yes` 生成 `boot.img` / 安装 `Image.lzo` 的条件扩为 `S905 -o A311D`（`kernel_make ... ${KERNEL_TARGET} ... modules` 见 `:518`） |
| `packages/linux/package.mk:639-643` | `makeinstall_target` 新增 `elif A311D`：把 `arch/arm64/boot/dts/amlogic/meson-g12b-a311d-cainiao-cniot-core*.dtb` 装进 `usr/share/bootloader/device_trees`（S905 分支保持原样） |
| `packages/graphics/libmali/package.mk:26-29` | 新增 `A311D)` 分支：`DRIVER_VERSION="r51p0"` + `vulkan-wsi-layer vulkan-tools`（与 S922X 同 G52） |
| `packages/graphics/libmali/package.mk:113` | 正则 `S922X` → `S922X\|A311D`（Vulkan 库不参与 mali 目录 bind-mount） |
| `packages/linux-drivers/mali-bifrost/package.mk:42` | `S922X)` → `S922X\|A311D)`（`MALI_PLATFORM="meson"`，否则走 `devicetree` 会在 Amlogic 上取错时钟/电源域） |
| `packages/graphics/gpudriver/package.mk:30-39` | 新增 `A311D)` 分支：`PAN="panfrost"`、`GPU_OPTIONS="panfrost libmali"`、`DTB_OVERLAY_LOAD/UNLOAD` 改为 `gpu-dtb set panfrost|libmali`（S922X 仍用 `dtb_overlay`） |
| `packages/graphics/gpudriver/package.mk:14-20` | `post_makeinstall_target` 追加：`DEVICE=A311D` 时安装并 `chmod +x` `usr/bin/gpu-dtb` |

## C. 已做校验

1. `bash -n`：新增/改动的全部 shell 脚本（`options`、`mkbootimg`、`release`、`update.sh`、两个 `.src`、`gpu-dtb`）= 通过；4 个 `package.mk` 经 CR 归一化后 = 通过（工作区 CRLF 是 `core.autocrlf=true` 的既有产物）。
2. 行尾：新增文件 `crlf_pairs=0`（纯 LF）；被改 6 文件 `LF-only_lines=0`（仍全 CRLF，未翻转）。
3. XML：`[xml]` 解析 `config.xml` 通过，`A311D.file` 计数 = 1。
4. `make -n A311D` / `make -n docker-A311D`：目标可解析（输出为 `scripts/build_distro` 调用链）。
5. 禁项扫描：`devices/A311D` 全树 + 改动文件，无 `seek=1` / `u-boot.bin` / `dd if=` / eMMC 写盘字样；无 IP/凭据。
6. 内核符号：新 conf 具备 `ARCH_MESON=y`(:342)、`MESON_SM=y`(:1625)、`CPUFREQ_DT=y`(:643)、`REGULATOR_PWM=y`(:3240)、`PWM_MESON=y`(:5900)、`PINCTRL_MESON_G12A=y`(:2656)、`MESON_SARADC=y`(:5421)、`USB_DWC3_MESON_G12A=y`(:4601)、`DRM_PANFROST=m`(:3844)、`RD_LZO/RD_ZSTD`、`KEYBOARD_GPIO_POLLED/INPUT_ADC_KEYS`。
7. dts 标签：逐个对照 mainline 6.x `meson-g12-common.dtsi` / `meson-g12b-a311d.dtsi`，确认 `hdmi_tx`、`hdmitx_hpd_pins`、`hdmitx_ddc_pins`、`pwm_a_e_pins`、`pwm_ao_d_e_pins`、`saradc`、`usb`、`mali`、`gpu_opp_table`、`uart_AO`、`cpu0..cpu103` 全部存在；OPP 由 dtsi 提供，板级只设 `cpu-supply`。

## D. 未做 / 未验证（须在真机构建时确认）

1. **未运行真实交叉编译**（本机无 `rocknix-build` 镜像、Windows/NTFS 宿主；`make docker-A311D` 需先 `docker pull ghcr.io/rocknix/rocknix-build`）。
2. **未用 `dtc` 编译 dts**（本机无 dtc；语法仅做了标签存在性核对）。
3. `Image.lzo` 能否被**原厂 U-Boot** `bootm` 解包 + zstd ramdisk 是否能被内核吃下：本机 EE 现网证明厂商内核可，**mainline 未证**（首要风险，方案 §5 R1；回退梯度 `Image.gz` → `booti`）。
4. USB host 在 mainline 下能否枚举 U 盘根介质（`&usb { dr_mode="host" }` 未实测，`usb2_phy0/1`、`usb3_pcie_phy` 依赖 g12-common 默认状态）。
5. `mali_vulkan`(midgard) 节点是否被 `mali_kbase`（meson 平台）正确绑定（P1 默认 libmali，需 `lsmod` + `dmesg` 验证）；P3 才做双栈切换闭环（当前 `dtb-panfrost.img` 尚未产出）。
6. GPU 栈切换目前只落地“写 `config.ini`”这一半，**panfrost 变体 dtb 属 P3**，故 P1 下 `gpudriver panfrost` 会提示但无 `dtb-panfrost.img`（脚本已给 warning）。

## E. 用户可自行执行的构建命令

```sh
# 首次（构建容器）
docker pull ghcr.io/rocknix/rocknix-build:latest
make docker-A311D                # 等价：make A311D
# 或直接用本机 Linux/WSL 工具链：
PROJECT=ROCKNIX DEVICE=A311D ARCH=aarch64 ./scripts/build_distro
```

产物：`target/ROCKNIX-aarch64-*.img.gz`（含 `kernel.img`、`SYSTEM`、`aml_autoscript`、`cfgload`、`cfgload_env`、`config.ini`、`dtb.img`、`device_trees/`）。

## F. U 盘验证步骤与判据（用户在 `192.168.31.57` 手动换盘）

1. 写盘到**新 U 盘**（不动 eMMC）：`xz -dc target/ROCKNIX-aarch64-*.img.gz | sudo dd of=/dev/sdX bs=4M conv=fsync`。
2. 断电 → 拔原 EE U 盘 → 插新盘 → 上电。原厂 U-Boot 的 `aml_autoscript` 会按 `mmc 0:1 → usb 0:1` 顺序找 `cfgload`（本板无 SD 槽，命中 `usb 0:1`）。
3. **通过判据（全绿才算 P1 过）**：
   - HDMI 出画且能看到 ES 启动；
   - `/flash` 内可见 `kernel.img`/`dtb.img`/`aml_autoscript`/`cfgload`/`config.ini`；
   - `ssh` 可登录；`cat /proc/cmdline` 含 `BOOT_IMAGE=kernel.img boot=LABEL=AURKNIX disk=LABEL=STORAGE`；
   - `dmesg` 无 `Kernel panic`、无 `Unable to mount root`；
   - `cat /proc/device-tree/model` = `CAINIAO CNIoT-CORE`；
   - `lsmod | grep mali` 有 `mali_kbase`、`gpudriver` 输出 `libmali`；
   - 连续 3 次冷启动稳定。
4. **回滚**：断电，换回原 EE U 盘即可（未触碰 eMMC，零残留）。
5. 若停在 U-Boot 阶段（无画面/无内核日志）：优先怀疑 `kernel_offset` 与压/解格式（见 §D.3），用串口 `ttyAML0` 抓 `bootm` 输出；回退梯度：`CONFIG_KERNEL_GZIP=y` → `Image.gz` → `booti` 三分参。

## G. 未提交

`git status`：6 个 `M` + `devices/A311D/`（未跟踪）+ `gpu-dtb`（未跟踪），`git diff --stat` = 6 files changed, 52 insertions(+), 6 deletions(-)。未 `git add`、未 commit、未 push。
