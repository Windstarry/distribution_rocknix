================================================================================
归档说明（2026-11-13 12:05，北京时间 GMT+8）
  本文件为 A311D libmali / panfrost 双栈适配规划 v2 的历史快照，制定于
  2026-11-13 10:20~10:50，已经用户批准；原文 425 行 / 九节，按原样入库，
  正文未改一字。规划原文通篇使用「拟／需／预期」的未执行时态，其第零节
  「交付仅本规划文件、不往仓库写文件、不改代码」与第九节「本规划未对仓库、
  镜像、设备或 CI 做任何改动」均为 10:50 时点表述，自 11:46 阶段 1 实施
  提交起失效，请按「历史规划快照」理解，不得据以判断当前执行状态。
  当前状态以 tools/a311d_HANDOFF.txt 第二十三节为准：
    阶段 0 / 阶段 1 已实施，提交 26e07ee61c（11:46），11:55 已推送 origin/a311d；
    阶段 2（kbase r54p2 与 libmali r51p0 的 DDK 版本实机实证、libmali 可选值启用）、
    阶段 3（CI 闸门接入与板级验收）、最后备选（DTB 双节点／引导链改造）、
    完整 CI 出图与刷盘上电均 [未执行]。
  禁止据本规划推定「双栈已完成」「libmali 可用」或「已通过板级验证」。
================================================================================

# A311D 平台 libmali / panfrost 双栈切换适配规划（修正版 v2）

（仅规划与差异说明；不含代码、命令清单或操作教程；不代表任何改动已执行）

仓库根：E:/Programs/distribution_rocknix，分支 a311d，HEAD 206269190f
**主参考基准（v2 改设）**：E:/A311D/ROCKNIX-S922X-BOX.aarch64-20261108.img
  —— A311D 实际使用的 ROCKNIX 系统镜像，亦为 A311D bootkit 来源
并列对照（非对齐目标）：ROCKNIX-S922X.aarch64-20261101.img.gz / tag 20261101（cefc1433）
本方镜像：E:/A311D/AURKNIX-A311D.aarch64-20261112.img、AURKNIX-image-A311D-20261112.zip

证据口径：[实证-字节]＝镜像字节可核；[实证-源码]＝本地源码原文可核；
[推断]＝未经板级确认；[未核验]＝受工具或取证范围限制未取证。
全文使用「拟／需／预期」，不出现「已修复／已可运行／已完成」等完成态表述。
本次修正基于两份镜像的只读字节实证：FAT32 BOOT 分区全量枚举、squashfs（zstd）
在内存中解包并枚举 64,890 条目；未挂载、未落盘、未改动仓库与设备。

═══════════════════════════════════════════════════════════════
零、目标、边界与决策
═══════════════════════════════════════════════════════════════

1. 目标形态 [不变]：同一镜像内 libmali（vendor 闭源栈：mali_kbase.ko +
   /usr/lib/mali 用户态库）与 panfrost（Mesa 开源栈）共存，每次开机按
   gpu.driver 择一。
2. **出厂默认 [本轮变更]：panfrost**；libmali 拟仅作可选切换值，不作为首启默认。
   依据：原镜像 /usr/config/system/configs/system.cfg L227 写死
   gpu.driver=panfrost，且镜像内 042-panvk 注释明写 libmali 曾因内核 kbase
   r54p2 与用户态 libmali r51p0 不匹配而加载失败、被迫改用 panfrost。
3. **DTB 双节点 / 引导链改造 [本轮变更]：降级为最后备选**。主路径拟为
   「单 dtb.img + 纯用户态双栈」；仅当「DDK 版本匹配已实证通过、但 libmali
   仍无法绑定 GPU 节点且证据指向节点形态为唯一障碍」时才启动，且须以原
   54,984 B dtb.img 为默认回退分支。
4. 交付 [不变]：仅本规划文件与对话输出；不往仓库写文件、不改代码、不触发 CI。

现状定性 [实证]：A311D 并非「缺一套双栈机制」，而是**在原镜像同源 bootkit 与
同源默认种子之上，叠加了第二套 DTB 选择死链、回退了 gpudriver 的 ICD 互斥与
bind 判定、缺失按驱动分流的 quirk，并以被字节实证否定的 midgard 论断解释默认
注入**。故整体工作拟定性为「回归／对齐原镜像的纯用户态双栈」，而非「从零移植
20261101 的 extlinux + dtbo 模型」。

最大不确定源已从「vendor DDK 绑定哪个 GPU 节点」改判为
「**kbase 与 libmali 的 DDK 版本匹配**」——它是 libmali 可选值能否可用的首要实证项。

═══════════════════════════════════════════════════════════════
一、参考实现结论：20261108 S922X-BOX（主参考）
═══════════════════════════════════════════════════════════════

（一）镜像身份与引导层 [实证-字节]

- /etc/os-release：OS_VERSION=20261108、BUILD_ID=0b991b0e…、HW_DEVICE=S922X-BOX。
- BOOT 分区偏移 16777216、卷标 ROCKNIX，根目录**仅 9 文件**：KERNEL.IMG
  (14,215,168)、KERNEL2.IMG(26,826,752)、DTB.IMG(54,984)、aml_autoscript(1466)、
  CFGLOAD(508)、KERNEL(26,822,664)、SYSTEM(1,672,544,256, squashfs zstd)、
  KERNEL.MD5、SYSTEM.MD5。**无 CONFIG.INI、dtb-panfrost.img、EXTLINUX/、
  OVERLAYS/、device_trees/，无任何子目录**。
- CFGLOAD 全文 5 行：fatload dtb.img 到 0x01000000 并 setenv dtb_mem_addr；
  setenv bootargs 固定串（含 video=HDMI-A-1:1920x1080@60）；kernel.img /
  kernel2.img 两个 bootm 分支；失败 echo。**不 import config.ini、不读 gpu_dtb、
  无任何按条件加载不同 dtb 的分支**；整条链（含 aml_autoscript 的 sddtb/usbdtb）
  引用的 DTB 文件名只有 dtb.img。
- 三方哈希实证：BOX /DTB.IMG = AURKNIX 20261112 /DTB.IMG = AUR
  /device_trees/*.dtb = 本地 bootkit/dtb.img = 90e42d53…；aml_autoscript 三方
  相同；BOX CFGLOAD(508 B) 与 AUR/bootkit cfgload(497 B) 唯一差异是
  video=HDMI-A-1:1920x1080@60 → video=HDMI-A-1:D（uImage 头 dsize/dcrc 未重算）。
  SOURCES.txt、SHA256SUMS 的来源声明全部得到字节支持。
- DTB：单份 FDT（magic 出现 1 次）、580 节点；/soc/gpu@ffe40000 compatible =
  amlogic,meson-g12a-mali + arm,mali-bifrost，**status 属性不存在（缺省 okay）**；
  arm,mali-midgard / mali_vulkan / panfrost / power_policy 均 0 命中。SYSTEM 内另有
  /usr/share/bootloader/meson-g12b-caixiao-cniot.dtb(80,227 B)，compatible 同为
  bifrost，但不是第二形态。

（二）切换链路表：源码位置 → 配置项 → 条件 → 时机 → 三层生效结果 [实证-字节]

| 环节 | 位置 | 条件 / 时机 | 生效结果 |
|---|---|---|---|
| 出厂种子 | /usr/config/system/configs/system.cfg L227 | 首次 set_setting；/etc/profile.d/001-functions L135-137 从只读模板复制到 /storage/.config/system/configs/system.cfg（J_CONF L11-12） | gpu.driver=panfrost；脚本内 CONFDRIVER="libmali" # DEFAULT 仅为键空兜底，**出厂不触发** |
| 唯一生效入口 | /usr/bin/gpudriver(2,896 B) --start；调用方 /usr/lib/autostart/common/003-gpudriver(131 B，全文仅 --start) | 每次冷启动；/usr/bin/autostart 顺序：platform quirks → device quirks → rocknix-automount → common/*（003 排第 6）→ HW_DEVICE/* → /storage/.config/autostart/* → . /etc/profile → systemctl start ${UI_SERVICE} | 见下两支 |
| panfrost 支（默认） | 同上 load_driver panfrost | gpu.driver=panfrost | modprobe -r mali_kbase + modprobe panfrost；umount 全部 mali/glesonly bind；mount --bind /dev/null 屏蔽 mali.json；Mesa libEGL.so.1.1.0 / libEGL_mesa.so.0.0.0 / libGLESv2.so.2.1.0 / libGL.so.1.7.0、/usr/lib/dri/panfrost_dri.so→libdril_dri.so、libvulkan_panfrost.so(16,168,552)、panfrost_icd.aarch64.json(1.4.354) 生效；DTB 单 bifrost 节点不变 |
| libmali 支（可选） | 同上 load_driver libmali | gpu.driver=libmali 且重启后 | modprobe -r panfrost + modprobe mali_kbase；`mount \| grep -q "on /usr/lib/libEGL.so"` 判定（注释：避免 squashfs 下 /proc/mounts 误判）；find /usr/{lib,lib32}/mali 与 glesonly 逐个 mount --bind 覆盖；libGL.so（含 lib32）绑 /dev/null；遍历 panfrost_icd.*.json 绑 /dev/null；/usr/lib/mali/ 10 项、glesonly libSDL2-2.0.so.0.3200.10(1,734,816)、mali.json(→libmali.so, 1.2.207)、/etc/OpenCL/vendors/mali.icd 生效；DTB 仍为同一 bifrost 节点 |
| 改设置（不热切） | 同脚本 libmali / panfrost 两分支 | 用户在 UI 改值当次 | 两分支**体为空**，只有 set_setting、不调 load_driver、不热切；usage 明写 "Configure driver to load on next boot"；全文 dtb/DTB/overlay/config.ini/gpu_dtb/dtbo 命中均为 0 |
| 自动加载抑制 | /usr/lib/modprobe.d/mali-gpu-noauto.conf(40 B) | 恒 | blacklist panfrost + blacklist mali_kbase，两模块只由 gpudriver 单点装入 |
| 模块同源 | /usr/lib/kernel-overlays/base/lib/modules/**6.18.49**/ | 恒 | panfrost.ko(125,176)、gpu-sched.ko(56,168)、**mali-bifrost/mali_kbase.ko(1,150,080)**；modules.dep(20,030 B) GPU 依赖行齐全；与 A311D 当前内核同号 |
| 版本约束 | quirk 042-panvk 注释 | — | 「本机内核 kbase 版本 (r54p2) 与镜像里带的 libmali (r51p0) 不匹配 → libmali 加载失败，被迫改用 panfrost」；panfrost 的 PanVK 需 PAN_I_WANT_A_BROKEN_VULKAN_DRIVER=1 |
| quirk 分流 | /usr/lib/autostart/quirks/platforms/S922X-BOX/ 15 项 | 按 $(gpudriver) | 010-governors（libmali→ffe40000.gpu_mali_vulkan devfreq，panfrost→ffe40000.gpu）、041-panfrost(PAN_MESA_DEBUG=forcepack)、042-panvk、075-mangohud（仅 panfrost）、090-ui_service（UI_SERVICE="sway.service essway.service"；libmali 时写 MALI_WAYLAND_AFBC=0 到 /storage/mali_debug.config 以允许 direct scanout）；另有 044-duckstation-panvk、101-gpu-functions、mali-priority.sh、999-export、s922x-gpu-tune.sh、030-gpu-tune，以及按 $(gpudriver) 分流的 Start BigPEmu/MelonDS/AetherSX2 脚本 |
| bootloader 目录 | /usr/share/bootloader/ **仅 2 项** | 恒 | meson-g12b-caixiao-cniot.dtb + update.sh(2,066，通用脚本含 "Convert from boot.ini to extlinux" 遗留段，对 chain-boot 无效)；**无 config.ini 模板、Generic_cfgload、device_trees/、OVERLAYS/、-panfrost.dtb、dtbo**；/usr/bin/gpu-dtb 与 /usr/bin/dtb_overlay 0 命中 |

（三）模型本质 [实证-字节]

**单 DTB + 双栈同镜像 + 用户态 bind-mount 覆盖 + modprobe 双向黑名单 +
Vulkan ICD 互斥 + 改设置只落盘、下次开机生效**。两套 .ko 与两套 .so 同盘共存，
DTB 只提供一个 bifrost 节点，两驱动靠「互相卸载 + 黑名单」互斥占用同一节点，
不靠换 DTB、不靠 overlay。DTB 在 U-Boot 阶段一次性固定，运行期无任何脚本改写。

（四）与 20261101 S922X 并列对照、以及对齐结论

| 维度 | 20261108 S922X-BOX（主参考） | 20261101 S922X（对照，非对齐目标） |
|---|---|---|
| 引导载体 | aml_autoscript + cfgload + Android boot kernel.img/kernel2.img | U-Boot + EXTLINUX/extlinux.conf(160 B，**初始无 FDTOVERLAYS 行**) |
| BOOT 内容 | 仅 9 文件，单 dtb.img，无 config.ini / dtbo / overlay / 子目录 | 有 EXTLINUX/、OVERLAYS/driver-gpu-panfrost.dtbo(351) + gpu-underclock.dtbo、两份不同机型 .dtb |
| DTB 模型 | 单份 bifrost、无 status | 双节点（bifrost disabled + midgard okay）+ dtbo 翻转 |
| 切换载体 | 纯用户态；引导链零参与 | dtb_overlay 写 FDTOVERLAYS + set_setting system.driver-gpu，U-Boot 阶段应用 |
| SYSTEM | squashfs zstd，64,890 条目全量枚举 [实证-字节] | 未解包 [未核验] |

**对齐结论 [实证]**：两套模型不同源，A311D 拟对齐 20261108 的「单 DTB +
纯用户态双栈」。理由：A311D bootkit 三件二进制与 20261108 哈希同源；A311D
config.ini:37-43 明写「双栈共用 bifrost、纯用户态切换、不换 dtb」并被本轮字节
实证支持；A311D 无 extlinux.conf 与 FDTOVERLAYS 承载点；本地 gpu-dtb 是后加的
第二套机制、与镜像不同源且自身用法不一致。20261101 模型拟仅作「不应照搬」的
对照证据保留，不作为改造目标。

═══════════════════════════════════════════════════════════════
二、A311D 相对 20261108 的偏离（回归／对齐定性）
═══════════════════════════════════════════════════════════════

| 项 | 20261108 原镜像 [实证-字节] | A311D 本地 [实证-源码] | 偏离性质 / 拟处置 |
|---|---|---|---|
| gpudriver ICD 互斥 | panfrost 支屏蔽 mali.json，libmali 支屏蔽 panfrost_icd.*.json | 本地 gpudriver **无 ICD mask** | 功能回退；拟补齐 |
| bind 判定 | `mount \| grep`（避 squashfs 误判） | 本地用 grep -q … /proc/mounts | 实现回退；拟改为不依赖 /proc/mounts |
| DTB 钩子 | @DTB_OVERLAY_LOAD@ / @DTB_OVERLAY_UNLOAD@ 被替换为空 | 本地保留占位符；package.mk A311D 支指向 /usr/bin/gpu-dtb set panfrost\|libmali | 额外叠加的第二套机制；拟清空钩子 |
| gpu-dtb 自身 | 镜像无此脚本 | 脚本 case 只接受 libmali\|panfrost（无 set 子命令），而 package.mk 写 `gpu-dtb set panfrost` | 本地自身用法不一致、死链；拟删除，不再「废弃或重做」二选一 |
| DTB 选择链 | cfgload 不读 config.ini / gpu_dtb，唯一 dtb.img | update.sh:27-32 的 dtb-panfrost.img 死分支、:34-38 device_trees 复制、:41-51 覆盖 /flash/config.ini 与 cfgload | 与镜像通用 update.sh 完全不同源，三环全断；拟清理死分支，引导字节保持不动 |
| quirk 覆盖 | S922X-BOX/ 15 项，含 010/041/042/075/090 分流与 MALI_WAYLAND_AFBC | quirks/platforms/A311D/ **仅 090-ui_service**（固定 UI_SERVICE，无 libmali 分支），另有 devices/A311D/filesystem/etc/profile.d/ 同功能副本 | 缺失／未分流；拟按回归原则评估补齐 |
| 默认注入 | system.cfg L227 gpu.driver=panfrost（同源） | rocknix/package.mk:24-39 echo/sed 注入同值，注释理由为「vendor DDK expects a midgard node」 | 注入结果同源、**注释论断被证伪**；拟保留注入、修正注释 |
| 内核／模块 | 模块目录 6.18.49 | A311D 6.18.49 单点 | **同源**（旧规划误判为差异）；拟作为有利约束 |
| 闸门 | — | a311d_ci_bootchain_check.py 24 PASS / 0 FAIL / 1 WARN，仅 S5 查 bifrost 形态；a311d_gpu_stack_check.py 18 项独立诊断未接入 CI | 覆盖不足；拟补判据并接入 |
| SYSTEM 压缩 | zstd，已纯内存全量枚举 | LZO(compression=3) 1,484,349,440 B，此前缺 LZO 解压能力 | 取证能力差异、非设计差异；A311D 内容未取证前仍标 [未核验] |
| BUILD_ID | 0b991b0e… / 20261108 | 同名 AURKNIX-A311D.aarch64-20261112.img 有 BUILD_ID 混淆（设备现跑 85765b5c6d…，最新 artifact 应为 0ed7e46bf9… [推断]） | 验收结论不得跨构建串用 |

其余结构性事实 [实证]：A311D 为 chain-boot（aml_autoscript + cfgload 497 B +
kernel.img 14,106,624 B）、ROCKNIX_DEVICE_TREE_OVERLAYS=""、KERNEL_TARGET=Image.lzo、
MALI_FAMILY=meson、ADDITIONAL_DRIVERS="RTW88 mali-bifrost"、ADDITIONAL_PACKAGES=libmali、
CONFIG_DRM_PANFROST=m、BOOTIMG_CMDLINE 含 video=HDMI-A-1:D；libmali/package.mk
A311D→r51p0，mesa/package.mk 26.0.4（参考 26.2.4）且 L101-105 仅 DEVICE=S922X 时
删 panfrost ICD。

═══════════════════════════════════════════════════════════════
三、需调整的模块与文件（四层）
═══════════════════════════════════════════════════════════════

标注：[必须] 目标形态所需；[可选] 可后置；[保持] 拟不动；[删除] 拟移除或清空；
[核验] 需先取证。路径根为 E:/Programs/distribution_rocknix。

（一）构建期
1. [必须] projects/ROCKNIX/packages/rocknix/package.mk:24-39
   **保留** gpu.driver=panfrost 注入（与镜像 L227 同源）；**修正** :24-29 注释，
   删除被字节实证否定的「vendor DDK expects a midgard node / No mali devices found /
   Could not initialize EGL」理由，改述为「出厂默认 panfrost，因 kbase r54p2 与
   libmali r51p0 的 DDK 版本不匹配，libmali 暂作可选切换值」。:41-54 的
   system.display_mode_hdmi 注入保持。
2. [必须] projects/ROCKNIX/packages/graphics/gpudriver/sources/bin/gpudriver
   补齐 mali.json / panfrost_icd.*.json 互斥屏蔽；bind 判定由 /proc/mounts 改为
   mount 输出；保留「改设置只落盘、不热切」；清空或移除两个 DTB_OVERLAY 占位符。
3. [必须] projects/ROCKNIX/packages/graphics/gpudriver/package.mk:17-21,37-44
   GPU_OPTIONS="panfrost libmali" 保持；A311D 支的 DTB_OVERLAY_LOAD/UNLOAD 清空为
   与默认 * 分支一致；:17-21 停止安装 gpu-dtb。
4. [删除] projects/ROCKNIX/packages/graphics/gpudriver/sources/bin/gpu-dtb
   认定为空操作死链（写 config.ini:gpu_dtb、dtb-panfrost.img 缺时回退 dtb.img，
   且 case 与调用不符），拟直接删除，不留半可用状态。
5. [核验] projects/ROCKNIX/packages/graphics/gpudriver/modprobe.d/mali-gpu-noauto.conf
   与 autostart/003-gpudriver：确认安装到 /usr/lib/modprobe.d/ 与
   /usr/lib/autostart/common/，且 003-gpudriver 早于 sway/essway 与任何 EGL/GBM
   消费者 [未核验]。本地 003-gpudriver 另有 RK356X min_freq 片段，不影响 A311D。
6. [核验] projects/ROCKNIX/packages/graphics/libmali/package.mk：确认 A311D 解析到
   r51p0 且生成 mali.json（Vulkan ICD 前提）[未核验]。
7. [保持] devices/A311D/options、devices/A311D/linux/linux.aarch64.conf、
   packages/linux/package.mk：MALI_FAMILY / GRAPHIC_DRIVERS / ADDITIONAL_DRIVERS /
   ADDITIONAL_PACKAGES / ROCKNIX_DEVICE_TREE_OVERLAYS / CONFIG_DRM_PANFROST=m /
   6.18.49 单点 / KERNEL_TARGET=Image.lzo 均不动。
8. [保持/记录] packages/graphics/mesa/package.mk：26.0.4 与 26.2.4 的版本差记录；
   L101-105 仅 S922X 删 panfrost ICD 的既定行为保持（镜像 HW_DEVICE=S922X-BOX 故保留）。

（二）引导期（由「最高风险层」降为「保持基线层」）
1. [保持] devices/A311D/bootloader/bootkit/{aml_autoscript,cfgload,dtb.img,
   SOURCES.txt,SHA256SUMS}：497 B cfgload、1,466 B aml_autoscript、54,984 B
   dtb.img 字节不动；任何最后备选形态以「新增可选文件 + 可选分支」存在，默认
   分支无条件走原 dtb.img 与原 bootargs。
2. [保持/统一口径] devices/A311D/bootloader/config.ini:37-43：其「双栈共用 bifrost、
   纯用户态切换、不换 dtb」被字节实证支持，拟升为主口径；package.mk 的 midgard
   论断拟向其统一。
3. [清理] devices/A311D/bootloader/update.sh：:27-32 dtb-panfrost.img 死分支、
   :34-38 device_trees 复制段按第七部分分叉清理；:41-51 覆盖 /flash/config.ini
   与 cfgload 的行为拟作为「任何以 config.ini 承载 DTB 选择」的硬约束记录。
4. [保持] bootloader/{mkbootimg,release}、kernel.img / kernel.img.md5 / SYSTEM.MD5、
   KERNEL-SYSTEM 同源、BOOTIMG_CMDLINE 与 cfgload bootargs 逐字同源（含
   video=HDMI-A-1:D）、DISTRO_BOOTLABEL="ROCKNIX"、加载地址 0x01000000 /
   0x01080000 均不动。

（三）运行期
1. [可选，强建议] packages/hardware/quirks/platforms/A311D/090-ui_service 与
   devices/A311D/filesystem/etc/profile.d/090-ui_service：两份同功能副本统一口径，
   评估按 gpu.driver 条件化 MALI_WAYLAND_AFBC=0 等 vendor EGL/GBM 变量。
2. [可选，阶段 2 后] 评估补齐按驱动分流的 010-governors（devfreq 路径）、
   041-panfrost、042-panvk（PAN_I_WANT_A_BROKEN_VULKAN_DRIVER=1）、075-mangohud
   门控；devfreq 路径与 DTB 节点名绑定，须在阶段 2 实证后再定。
3. [保持] /etc/profile.d/001-functions 的 get_setting / set_setting 持久化机制；
   空键兜底语义记录为「出厂不触发」。

（四）诊断与闸门
1. [必须] tools/a311d_ci_bootchain_check.py：补双 ko、modules.dep、vermagic 与
   6.18.49 一致、gpu.driver 出厂种子为 panfrost 且 libmali 为合法可选值、ICD mask
   齐备、bind 判定不依赖 /proc/mounts、renderD* 相关 DRM 配置、KERNEL-SYSTEM 同源
   判据；移除「Bifrost 节点默认 okay（双栈可用）」这类未实证 PASS 依据。
2. [必须] tools/a311d_gpu_stack_check.py：18 项只读诊断接入 CI 闸门；TC-LIVE-*
   明确标注仅实机。
3. [必须] 发布归档内 a311d-bootchain-check.json 的 S5 判据口径随单 DTB 主路径更新。
4. [引用] HANDOFF 22.6 的 #6（渲染帧率）、#8（libmali vendor 栈验收）作为板级
   验收项，并新增「回退 panfrost 可用」「连续冷启动」判据。

═══════════════════════════════════════════════════════════════
四、切换条件与预期行为（默认 panfrost、libmali 为可选值）
═══════════════════════════════════════════════════════════════

（一）条件 × 时机 × 三层生效矩阵（均为目标行为，非已验证行为）

| 序 | 条件 | 时机 | 用户态库 / 内核模块 / ICD / DTB |
|---|---|---|---|
| 1 | 出厂、新装、重置，gpu.driver=panfrost | 开机 --start | Mesa 三件套 + panfrost_dri.so + libvulkan_panfrost.so + panfrost_icd.*.json；mali.json 与 libGL 被 /dev/null 屏蔽；modprobe panfrost(+gpu-sched)、modprobe -r mali_kbase；bind 列表为空；单 bifrost dtb.img 不变 |
| 2 | 稳定 panfrost 的每次冷启动 | 同 1 | 同 1 |
| 3 | 用户在 UI 改为 libmali | 当次会话 | **运行栈不热切**：仍 panfrost / panfrost.ko；仅 set_setting 写值；不写任何 DTB 选择；UI 拟显式提示需重启 |
| 4 | 改后首次重启 | 开机 --start | /usr/lib/mali/* + glesonly SDL2 bind 覆盖、mali.json 生效、panfrost_icd.*.json 屏蔽、libGL 绑 /dev/null；modprobe mali_kbase、modprobe -r panfrost；单 bifrost dtb.img 不变。**前提：DDK 版本匹配实证通过** |
| 5 | 稳定 libmali 的每次冷启动 | 同 4 | 同 4 |
| 6 | 用户改回 panfrost | 当次不热切，重启后回到 1 | — |
| 7 | 回退（libmali 起不来） | 改回 panfrost 并重启 | 恢复到序 1 的 panfrost 基线 |

（二）不一致窗口与 quirk 错配（替代旧规划的「跨 DTB 一帧窗口」）

- 单 DTB 主路径下，DTB 形态与用户态栈不再解耦于两个时刻，旧规划所述「跨 DTB
  形态的一帧不一致窗口」拟判为**不存在**；也不再有「DTB 选择与 gpu.driver 由
  两次落盘错开」的约束。
- 拟关注「**改设置到重启的整个会话**」：序 3、6 当次会话内 gpu.driver 值已变、
  运行栈未变，凡按 $(gpudriver) 取值的 quirk 与启动脚本若在改值后被重新读取，
  拟出现「设置值 ≠ 运行栈」的错配 [推断]。
- **关键错配风险** [实证-字节 + 推断]：010-governors 在 libmali 条件指向
  ffe40000.gpu_mali_vulkan devfreq 路径，而单 bifrost dtb.img 中 mali_vulkan
  0 命中 → 该路径悬空。即便 libmali 可选值可加载，仍需在阶段 2 决定补
  ffe40000.gpu 路径或禁用该分流。
- 处置原则：003-gpudriver 须早于 sway/essway 与任何 EGL/GBM 消费者；保持
  「改设置不热切、统一等一次重启、UI 显式提示」。

（三）DTB 改造作为最后备选的准入与红线

- 拟**移出阶段序列**，仅在第七部分分叉 (b) 的实证成立时启动：DDK 版本匹配已
  通过、libmali 仍加载失败、且已排除 vermagic / ICD / bind / quirk devfreq 等
  其他因素，证据明确指向「单 bifrost 节点形态」为唯一障碍。
- 准入还需：cfgload 是否改造已评审、BOOTIMG_CMDLINE / 卷标 / 加载地址同源约束
  逐项确认、本地 dts 与 bootkit 二进制不同源问题已有处置、update.sh:41-51 覆盖
  冲突已解决。
- 红线：以原 54,984 B dtb.img（90e42d53…）为**默认回退分支**；任何新增
  dtb/dtbo/载体以「新增可选文件 + 可选分支」存在；config.ini、cfgload、
  aml_autoscript、bootargs 文本、加载地址、BOOTIMG_CMDLINE、DISTRO_BOOTLABEL
  不作为默认改动对象；cfgload 若被改造须保留原始字节副本；每个引导层改动
  单独做连续冷启动 + 回退 panfrost 回归，不以静态检查代替。

═══════════════════════════════════════════════════════════════
五、兼容性约束
═══════════════════════════════════════════════════════════════

1. **首要实证项：kbase 与 libmali 的 DDK 版本匹配** [实证-字节 + 推断]。依据
   042-panvk 注释「r54p2 内核侧 vs r51p0 用户态不匹配 → libmali 加载失败」；本地
   libmali/package.mk A311D→r51p0。匹配未通过前，libmali 拟仅为声明性可选值，
   不得由「源码侧设计上齐全」推定可用。
2. 6.18.49 模块同源 [实证-字节]：参考镜像模块目录与 A311D 内核同号，
   panfrost.ko / gpu-sched.ko / mali-bifrost/mali_kbase.ko 与 modules.dep GPU 依赖
   行齐全；vermagic 须一致。历史「6.18.49 内核 + 6.18.21 模块」混刷曾致
   modprobe FATAL、无 renderD128，拟作为反例。
3. mali-bifrost 目录命名与 dtb 来源 [实证-字节]：mali_kbase.ko 位于 mali-bifrost/
   子目录；dtb.img 为 bootkit 外部二进制（54,984 B / 90e42d53…），与本地
   meson-g12b-a311d-cainiao-cniot-core.dts（80,688 B 树）不同源，不得由本地 dts
   推定镜像内容。
4. Mesa 26.0.4 vs 参考 26.2.4 [实证-源码 + 推断]：bind 覆盖同名 libEGL /
   libGLESv2 / libgbm，ABI 与 panfrost_dri.so 匹配性需实机验证 EGL/GBM 初始化。
5. ICD 互斥 [实证-字节 + 推断]：mali.json(→libmali.so, 1.2.207) 与
   panfrost_icd.aarch64.json(→libvulkan_panfrost.so, 1.4.354) 须互斥屏蔽；本地
   gpudriver 缺该逻辑，双栈 loader 选错实现风险 [推断]。
6. sway/essway 与 MALI_WAYLAND_AFBC [实证-字节 + 推断]：UI_SERVICE 固定
   "sway.service essway.service"（wlroots DRM-KMS）；参考镜像在 libmali 下写
   MALI_WAYLAND_AFBC=0 以允许 direct scanout；A311D 的 090-ui_service 无该分支，
   libmali 下兼容性 [未核验]。
7. config.ini / cfgload 不可动 [实证-字节]：20261108 整链唯一 DTB 名为 dtb.img、
   不 import config.ini、不读 gpu_dtb，SYSTEM 内无 config.ini 模板 /
   Generic_cfgload / device_trees / dtbo；本地 update.sh:41-51 会覆盖
   /flash/config.ini 与 cfgload。
8. 默认种子语义 [实证-字节]：出厂 gpu.driver=panfrost 写在 system.cfg L227，
   脚本的 libmali 空键兜底出厂不触发；存量与新装同值，旧规划「双种子迁移语义」
   拟删除。
9. 同名 BUILD_ID [推断]：同名 20261112 镜像存在 85765b5c6d… 与 0ed7e46bf9…
   的可能混淆，验收结论不得跨构建串用；20261108 的 BUILD_ID=0b991b0e… 拟作
   参考镜像身份标识。
10. CI 闸门仅 S5 查 bifrost 形态，无法在构建期拦截混刷与半可用状态，须补齐。

═══════════════════════════════════════════════════════════════
六、验证要点
═══════════════════════════════════════════════════════════════

（一）离线 / 镜像字节级

- 手段拟具体化：参考镜像 zstd squashfs 可用「纯内存 ctypes 调 libzstd 的
  ZSTD_decompress」全量枚举（本轮已实证可行，不需落盘、不需 unsquashfs，
  64,890 条目）；A311D 的 LZO(compression=3) SYSTEM 拟接入同类内存解包路径
  完成取证，不再作为笼统阻断式前置依赖。A311D SYSTEM 内文件正文在未取证前
  仍标 [未核验]。
- BOOT 判据：dtb.img 54,984 B、单 FDT、580 节点、/soc/gpu@ffe40000 compatible
  与 status 缺省、midgard / mali_vulkan / panfrost / power_policy 命中数；
  cfgload(497/508 B) 与 aml_autoscript 字节、config.ini 文本、SOURCES.txt /
  SHA256SUMS 来源与三方哈希 90e42d53…；kernel.img / kernel2.img / KERNEL /
  SYSTEM 大小与 MD5、BUILD_ID、KERNEL-SYSTEM 同源。
- SYSTEM 判据：panfrost.ko(125,176)、gpu-sched.ko(56,168)、
  mali-bifrost/mali_kbase.ko(1,150,080)、modules.dep(20,030 B) 与 GPU 依赖行、
  vermagic 与 6.18.49 一致、panfrost_dri.so→libdril_dri.so、/usr/lib/mali/ 10 项、
  glesonly SDL2(1,734,816)、Mesa 三件套、mali.json / panfrost_icd.*.json /
  libvulkan_panfrost.so(16,168,552)、/etc/OpenCL/vendors/mali.icd、
  mali-gpu-noauto.conf、003-gpudriver、system.cfg L227、quirk 15 项、
  /usr/share/bootloader/ 仅 2 项、DDK 版本串（r54p2 / r51p0）。

（二）构建期 / CI 闸门

双 ko 存在性、modules.dep 条目、vermagic 与 6.18.49 一致、gpu.driver 出厂种子
为 panfrost 且 libmali 为合法可选值、ICD mask 齐备、bind 判定不依赖
/proc/mounts、renderD* 相关 DRM 配置、KERNEL-SYSTEM 同源补入
a311d_ci_bootchain_check.py；接入 a311d_gpu_stack_check.py 的 TC-SYS-01~10，
TC-LIVE-* 标注仅实机；S5 判据口径随单 DTB 主路径更新；判据中不得保留未经
实证的「双栈可用」结论作为 PASS 依据。

（三）板级实机（必须实机，不得由静态检查替代）

lsmod 观察 panfrost 与 mali_kbase 互斥、无自动加载竞争、modprobe 无 FATAL；
/dev/dri（card0/1/2、renderD128/129）与 /dev/mali* 在两栈下分别符合预期；
mount bind 列表在 libmali 分支覆盖 /usr/lib/libEGL.so 等、panfrost 分支为空，
libGL 与 ICD 屏蔽生效；gpu.driver 往返（查→改 libmali→重启→查→改回 panfrost
→重启）全程与运行栈一致；**DDK 版本串匹配实证**作为 libmali 可选值准入；
libmali 的 EGL/GBM/Vulkan 初始化（HANDOFF 22.6 #8）、渲染帧率与渲染路径（#6）、
MALI_WAYLAND_AFBC=0 与 sway/essway 合成路径、PanVK 变量表现；010-governors 的
libmali devfreq 路径是否存在、是否需回退到 ffe40000.gpu；连续多次冷启动无黑屏、
无 Unable to find LABEL=ROCKNIX、无版本混刷 FATAL；**回退 panfrost 可用**。

不得由静态检查替代：DDK 版本实际匹配与绑定关系、EGL/GBM 初始化成功、帧率与
卡顿、连续冷启动稳定性、BUILD_ID 与板上镜像对应、update.sh 覆盖后设置一致性。

═══════════════════════════════════════════════════════════════
七、分阶段顺序、回退与实证分叉
═══════════════════════════════════════════════════════════════

| 阶段 | 准入 | 核心内容与判据 | 回退 |
|---|---|---|---|
| **0 口径统一与离线核验** | 无 | 统一 package.mk 与 config.ini:37-43 的互斥口径（midgard 论断拟删除、bifrost 主口径确立）；完成 20261108 全量核验并把 A311D LZO SYSTEM 核验路径具体化；明确 A311D 双 ko / modules.dep / vermagic / ICD / system.cfg L227 的取证状态 | 仅诊断与口径，不改文件即可回退 |
| **1 对齐原镜像的纯用户态双栈（默认 panfrost 不动）** | 阶段 0 | 补齐 gpudriver 的 ICD mask 与 bind 判定；清空 DTB 钩子、删除 gpu-dtb 死链；核验黑名单与 003-gpudriver 顺序；**保留** gpu.driver=panfrost 注入并修正注释论断；单 dtb.img 与整条引导链字节不动。判据：panfrost 基线回归（ICD 互斥、bind 判定、模块黑名单、开机顺序、出厂种子一致），引导层零改动 | 恢复原 gpudriver 与注入注释即可，成本最低 |
| **2 libmali 可选值的 DDK 版本匹配实证与启用** | 阶段 1 通过 | 先做 kbase r54p2 与 libmali r51p0 的版本串匹配实证；通过后拟启用 libmali 可选值并补齐按驱动分流 quirk（含 devfreq 路径处置、MALI_WAYLAND_AFBC=0、PanVK 变量）。判据：版本匹配结论、EGL/GBM/Vulkan 初始化、帧率与渲染路径、lsmod 互斥、ICD mask | 改回 panfrost 并重启，回到阶段 1 基线；libmali 保持为声明性可选值 |
| **3 闸门接入与板级验收** | 形态冻结 | 补 CI 判据、接入 18 项诊断、更新 S5 口径、执行第六部分全部实机项（含连续冷启动与回退 panfrost 可用） | 闸门只阻断、不自动改写镜像；panfrost 基线始终保留 |

**最后备选（DTB 双节点／引导链改造）**：不占阶段序号，准入与红线见第四部分（三）。

三种实证分叉：

- **(a) DDK 版本匹配后 libmali 可用** → 保持单 dtb.img；补齐 ICD mask、bind 判定、
  按驱动分流 quirk；删除 gpu-dtb 死链与 update.sh:27-32 死分支；统一 package.mk
  注释。DTB 改造**取消**、引导链零改动。预期达成「单 DTB + 用户态双栈，默认
  panfrost、libmali 可选」。
- **(b) 版本匹配后仍失败且证据指向节点形态** → 才启动最后备选：在「新增可选
  dtb／双节点 dtbo + 可选分支」中评估载体，先解决 update.sh:41-51 覆盖冲突，
  默认仍走原 54,984 B dtb.img；panfrost 基线始终可回退。
- **(c) libmali 始终不可用** → 保留 panfrost 单栈可用；清理死链、统一口径、补齐
  panfrost 侧 ICD/bind 回归；不启动任何 DTB 改造；**明确标注 libmali 为
  [未达成] 的可选值**，不把结果表述为双栈完成。

三分叉共同必需项：gpu-dtb 半可用死链不得保留；config.ini / cfgload / 引导字节
不动；Vulkan ICD 互斥、模块黑名单、003-gpudriver 开机顺序、出厂默认 panfrost
种子、回退 panfrost 可用均不可省；结论不得跨 BUILD_ID 串用。

═══════════════════════════════════════════════════════════════
八、关键文件清单
═══════════════════════════════════════════════════════════════

必须处理
  projects/ROCKNIX/packages/rocknix/package.mk（保留注入、修正注释）
  projects/ROCKNIX/packages/graphics/gpudriver/package.mk（清空 DTB 钩子、停装 gpu-dtb）
  projects/ROCKNIX/packages/graphics/gpudriver/sources/bin/gpudriver（ICD mask、bind 判定）
  tools/a311d_ci_bootchain_check.py、tools/a311d_gpu_stack_check.py
  a311d-bootchain-check.json（发布归档内 S5 口径）
拟删除 / 清理
  projects/ROCKNIX/packages/graphics/gpudriver/sources/bin/gpu-dtb
  projects/ROCKNIX/devices/A311D/bootloader/update.sh（:27-32 死分支、按分叉清理）
需核验
  projects/ROCKNIX/packages/graphics/libmali/package.mk（r51p0 / mali.json）
  projects/ROCKNIX/packages/graphics/mesa/package.mk（26.0.4 / ICD 删除条件）
  projects/ROCKNIX/packages/graphics/gpudriver/modprobe.d/mali-gpu-noauto.conf
  projects/ROCKNIX/packages/graphics/gpudriver/autostart/003-gpudriver
  projects/ROCKNIX/packages/hardware/quirks/platforms/A311D/090-ui_service
  projects/ROCKNIX/devices/A311D/filesystem/etc/profile.d/090-ui_service
保持不动（已验证基线）
  projects/ROCKNIX/devices/A311D/bootloader/bootkit/{aml_autoscript,cfgload,dtb.img,SOURCES.txt,SHA256SUMS}
  projects/ROCKNIX/devices/A311D/bootloader/{config.ini,mkbootimg,release}
  projects/ROCKNIX/devices/A311D/options
  projects/ROCKNIX/devices/A311D/linux/linux.aarch64.conf
  projects/ROCKNIX/packages/linux/package.mk
  projects/ROCKNIX/devices/A311D/linux/dts/amlogic/meson-g12b-a311d-cainiao-cniot-core.dts（仅作最后备选的源，不进镜像）

═══════════════════════════════════════════════════════════════
九、决策变更记录与未核验清单
═══════════════════════════════════════════════════════════════

（一）本轮变更（触发证据均为 20261108 镜像字节实证）

| 项 | v1 | v2 | 触发证据 |
|---|---|---|---|
| 出厂默认 | libmali | **panfrost**（libmali 仅可选） | system.cfg L227 写死 gpu.driver=panfrost；042-panvk 注释 r54p2 vs r51p0 不匹配致 libmali 加载失败。v1「参考版 system.cfg 无 gpu 行 → 默认 libmali」判为证伪 |
| DTB 改造 | 阶段 2 主体、最高风险 | **最后备选** | BOOT 仅 9 文件、CFGLOAD 5 行不读 config.ini / gpu_dtb、整链唯一 dtb.img、单 FDT bifrost、SYSTEM 内无 dtbo / gpu-dtb / dtb_overlay；config.ini:37-43 的纯用户态判断被实证支持 |
| 参考基准 | 20261101 S922X | **20261108 S922X-BOX 为主**，20261101 降为对照 | bootkit 三件二进制哈希同源、两套模型不同源 |

连带修正（非用户另行决策）：最大不确定源由「midgard 节点」改判为「DDK 版本
匹配」；「跨 DTB 一帧窗口」重写为「会话级设置／运行栈不一致 + quirk devfreq
错配」；「gpu-dtb 二选一」改为直接删除；「LZO 不可解」降级为内存解包可行手段；
「移除 gpu.driver=panfrost 注入」反转为「保留注入、修正注释论断」；内核／模块
版本由「差异」改判为 6.18.49 同源；整体工作定性为「回归／对齐」而非「从零移植」。
决策 1（双栈目标）与决策 4（仅规划交付）不变。

（二）[未核验] 清单

1. kbase 内核侧 r54p2 与用户态 r51p0 的实际版本串匹配与板级后果（决定性实证）。
2. A311D LZO SYSTEM 内双 ko / modules.dep / vermagic / ICD / quirk 的字节级点验。
3. libmali 下 sway/essway 与 vendor EGL/GBM 的 UI_SERVICE 兼容性、帧率与渲染路径。
4. 010-governors 的 libmali devfreq 路径 ffe40000.gpu_mali_vulkan 在板上是否存在。
5. 20261101 S922X 的 SYSTEM 未解包，其 dtb_overlay / FDTOVERLAYS 注入逻辑 [未核验]；
   已实证的是其 extlinux.conf 初始不含 FDTOVERLAYS 行。
6. Amlogic 板级 U-Boot 固件内部是否另有 config.ini 解析（固件不在 .img BOOT 分区内）。
7. 同名 20261112 镜像 BUILD_ID 与板上运行镜像的对应。
8. 本规划未对仓库、镜像、设备或 CI 做任何改动；所有条目均为待验证设计。
