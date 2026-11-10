#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A311D USB 启动盘双盘对照取证工具（纯只读，不修改设备任何文件）。

背景
----
2026-11-10 的 A311D 启动失败现场是两块盘、两套系统的固定对照环境：
  * 152d:0583  JMicron JMS583 NVMe USB3.2 硬盘盒 -> ROCKNIX 20261108（6.18.49），可进系统
  * 048d:1234  ITE / Chipsbank CBM2199 USB2.0 U 盘 -> AURKNIX 20261110（6.18.21），
    报 "Can't lookup blockdev LABEL=ROCKNIX" 后 poweroff

本工具在"能进系统"的那一侧执行只读采集，用于回答三个问题：
  1. 另一块盘（AURKNIX 盘）在已知良好内核下能否生成 /dev/sdX；
  2. 该盘 FAT 分区的 LABEL 到底是不是 ROCKNIX（区分卷标问题与块设备缺失）；
  3. 内核是否为该盘完成了 usb-storage / uas -> scsi host -> sdX 的绑定。

使用方法
--------
  # 先把 AURKNIX 的 CBM2199 盘插到设备上另一个 USB 口（无需重启、无需换启动盘）
  python3 tools/a311d_usb_boot_probe.py --host 192.168.31.7 --out <报告.txt>
  # 密码可用环境变量 RN_PASS 覆盖（默认 rocknix）

判定规则见输出末尾的「自动判定」段。
"""
import argparse
import io
import os
import sys

try:
    import paramiko
except ImportError:
    print("需要 paramiko：python3 -m pip install paramiko", file=sys.stderr)
    sys.exit(2)

CMDS = [
    ("UNAME", "uname -a"),
    ("OSREL", "cat /etc/os-release 2>/dev/null | head -12"),
    ("CMDLINE", "cat /proc/cmdline"),
    ("PARTITIONS", "cat /proc/partitions"),
    ("BLKID", "blkid"),
    ("BY_LABEL", "ls -l /dev/disk/by-label/ 2>/dev/null"),
    ("LSBLK", "lsblk -o NAME,SIZE,TYPE,LABEL,MOUNTPOINT 2>/dev/null"),
    ("USBSYS", "for d in /sys/bus/usb/devices/*; do "
               "[ -f \"$d/idVendor\" ] && echo \"$d $(cat $d/idVendor) $(cat $d/idProduct) "
               "$(cat $d/product 2>/dev/null) $(cat $d/manufacturer 2>/dev/null) "
               "speed=$(cat $d/speed 2>/dev/null)\"; done"),
    ("SD_DEVS", "for d in /sys/block/sd*; do [ -e \"$d\" ] && echo \"$d -> "
                "$(readlink -f $d)\"; done"),
    ("DMESG_USB", "dmesg | grep -iE 'usb|scsi|sd [0-9]|uas|xhci|dwc3|hub|"
                  "Checking disk|blockdev|Mass Storage|regulator' | tail -220"),
    ("DMESG_TAIL", "dmesg | tail -60"),
    ("KCONFIG", "zcat /proc/config.gz 2>/dev/null | grep -E "
                "'CONFIG_(USB_STORAGE|USB_UAS|SCSI=|BLK_DEV_SD|USB_XHCI_HCD|"
                "USB_DWC3|USB_DWC3_MESON_G12A|INITRAMFS|DEVTMPFS)'"),
]


def collect(host, user, password, timeout=25):
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=user, password=password, timeout=12,
                look_for_keys=False, allow_agent=False)
    out = {}
    for name, cmd in CMDS:
        try:
            _, so, se = cli.exec_command(cmd, timeout=timeout)
            txt = so.read().decode("utf-8", "replace")
            err = se.read().decode("utf-8", "replace")
            if err.strip():
                txt += "\n[stderr] " + err[:400]
        except Exception as exc:
            txt = "[采集异常] %s" % exc
        out[name] = txt
    cli.close()
    return out


def verdict(d):
    lines = []
    usbsys = d.get("USBSYS", "")
    blkid = d.get("BLKID", "")
    dm = d.get("DMESG_USB", "")
    cbm = "048d" in usbsys.lower() and "1234" in usbsys
    jms = "152d" in usbsys.lower() and "0583" in usbsys

    lines.append("1. USB 设备枚举")
    lines.append("   JMS583/ROCKNIX 盘 (152d:0583): %s" % ("已在位" if jms else "未检测到"))
    lines.append("   CBM2199/AURKNIX 盘 (048d:1234): %s" % ("已在位" if cbm else "未检测到（请确认已插好）"))

    labels = [ln for ln in blkid.splitlines() if "LABEL=" in ln]
    lines.append("2. blkid 可见的带标签分区：%d 个" % len(labels))
    for ln in labels:
        lines.append("   " + ln.strip())
    roknix = 'LABEL="ROCKNIX"' in blkid
    lines.append("   存在 LABEL=\"ROCKNIX\": %s" % ("是" if roknix else "否"))

    lines.append("3. 内核 USB 存储绑定链")
    for key, desc in (
        ("registered new interface driver uas", "uas 驱动注册"),
        ("registered new interface driver usb-storage", "usb-storage 驱动注册"),
        ("USB Mass Storage device detected", "大容量存储接口识别"),
        ("scsi host", "SCSI host 建立"),
        ("Attached SCSI disk", "sdX 块设备就绪"),
    ):
        lines.append("   %-24s : %s" % (desc, "出现" if key.lower() in dm.lower() else "未出现"))

    lines.append("4. 结论提示")
    if not cbm:
        lines.append("   AURKNIX 盘未插入，本轮只能作为基线，无法判定盘侧问题。")
    elif roknix and "Attached SCSI disk" in dm:
        lines.append("   良好内核可以识别 AURKNIX 盘且 LABEL 正确 -> "
                     "盘体与卷标无问题，故障在 AURKNIX 侧启动链/DTB/U-Boot env。")
        lines.append("   建议：把 20261109-bootsafe-edidmode.img 刷到该盘做交叉验证，")
        lines.append("         或用串口读 U-Boot 的 env print / 实际 dtb_mem_addr。")
    elif not roknix:
        lines.append("   良好内核下也看不到 LABEL=ROCKNIX -> 盘侧问题：")
        lines.append("   分区表/BPB 卷标、刷写完整性，或该 CBM2199 盘在 Linux 下完成不了")
        lines.append("   大容量存储绑定（供电 / 主控兼容 / 假容量盘）。")
        lines.append("   建议：换用 JMS583 硬盘盒，或加带独立供电的 USB HUB 后重测。")
    else:
        lines.append("   需结合 dmesg 时间线人工判读，见上文完整日志。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="A311D USB 启动盘双盘对照取证（只读）")
    ap.add_argument("--host", default="192.168.31.7")
    ap.add_argument("--user", default="root")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    pwd = os.environ.get("RN_PASS", "rocknix")

    print("连接 %s@%s ..." % (args.user, args.host))
    d = collect(args.host, args.user, pwd)

    buf = io.StringIO()
    for name, _ in CMDS:
        buf.write("\n===== %s =====\n%s\n" % (name, d.get(name, "")))
    buf.write("\n===== 自动判定 =====\n%s\n" % verdict(d))
    text = buf.getvalue()

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print("报告已写入: %s" % args.out)
    print("\n===== 自动判定 =====\n" + verdict(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
