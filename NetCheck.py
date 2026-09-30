#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""net_check.py —— 交换机测试前的「测试端网卡体检」工具（Windows / 纯标准库）

为什么需要它
------------
丢包率和时延数据是「端到端」的：测试端网卡本身的问题，会原样出现在报告里，
而报告上看起来就像交换机的问题。最典型的两个源头是
  ① USB / Type-C 扩展坞上的网卡（USB 是轮询总线，时延抖动 100 µs ~ 几 ms）
  ② 机器上的虚拟网卡 / VPN（把流量引到错误的出口）

怎么用
------
    python net_check.py                 # 体检本机
    python net_check.py 192.168.1.20    # 顺便算一下：真要发往这个地址，
                                        # 内核会从哪块网卡出去

输出怎么看
----------
    [OK]    板载 PCIe 网卡   —— 可用于时延测量
    [!!]    USB 网卡         —— 不要用于时延测量（理由与处置见输出）
    [(!)]   虚拟网卡 / VPN   —— 路由干扰源；测量时必须确认没被选中
    [OK]/[!!] 协商速率       —— 1 Gbps 才算达标；掉到 100 Mbps 说明线缆/对端有问题
    【五】段还会给出该网卡的 InErrors/OutErrors，测试前后各看一次，增量须为 0
    【七】段总判定           —— 一句话结论：这台机器适合当哪种测量端
                               （优 / 可（降级）/ 只适合丢包·环网 / 不可用）
"""
import ctypes
import re
import socket
import subprocess
import sys
import winreg

CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}"
NETKEY = r"SYSTEM\CurrentControlSet\Control\Network\{4d36e972-e325-11ce-bfc1-08002be10318}"
USB_ENUM = r"SYSTEM\CurrentControlSet\Enum"

# ---------------------------------------------------------------------------
# 读「实际协商速率」用 iphlpapi!GetIfTable2（纯标准库 ctypes）。
# 为什么不查注册表：注册表里只有「配置成什么」（*SpeedDuplex=0 表示自动协商），
# 拿不到「谈成了多少」。网口到底跑 1 Gbps 还是掉到 100 Mbps，只能问内核。
# 为什么不用 PowerShell：本环境里 PowerShell 工具的输出会被吞（exit 0 无输出），
# 走 ctypes 才稳。
_IF_COUNTERS = [
    "InOctets", "InUcastPkts", "InNUcastPkts", "InDiscards", "InErrors",
    "InUnknownProtos", "InUcastOctets", "InMulticastOctets", "InBroadcastOctets",
    "OutOctets", "OutUcastPkts", "OutNUcastPkts", "OutDiscards", "OutErrors",
    "OutUcastOctets", "OutMulticastOctets", "OutBroadcastOctets", "OutQLen",
]


class _MIB_IF_ROW2(ctypes.Structure):
    # 字段顺序/对齐必须与 Windows SDK 的 MIB_IF_ROW2 一致，
    # 否则读出来的是错位的垃圾值（结构体必须定义到最后一个计数器，
    # 不然 GetIfTable2 会把整张表写到我们的缓冲区之外）。
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", ctypes.c_uint32),
        ("InterfaceGuid", ctypes.c_ubyte * 16),
        ("Alias", ctypes.c_wchar * 257),
        ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", ctypes.c_uint32),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
        ("Mtu", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TunnelType", ctypes.c_uint32),
        ("MediaType", ctypes.c_uint32),
        ("PhysicalMediumType", ctypes.c_uint32),
        ("AccessType", ctypes.c_uint32),
        ("DirectionType", ctypes.c_uint32),
        ("InterfaceAndOperStatusFlags", ctypes.c_uint8),
        ("OperStatus", ctypes.c_uint32),
        ("AdminStatus", ctypes.c_uint32),
        ("MediaConnectState", ctypes.c_uint32),
        ("NetworkGuid", ctypes.c_ubyte * 16),
        ("ConnectionType", ctypes.c_uint32),
        ("TransmitLinkSpeed", ctypes.c_uint64),
        ("ReceiveLinkSpeed", ctypes.c_uint64),
    ] + [(n, ctypes.c_uint64) for n in _IF_COUNTERS]


class _MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", ctypes.c_uint32), ("_pad", ctypes.c_uint32)]


def if_speeds():
    """返回 {适配器别名: {...}}，别名与 ipconfig 里的名字一致（如 "Ethernet 2"）。
    失败时返回空 dict —— 这项只是体检的加分项，读不到不影响其余判断。"""
    try:
        iphlpapi = ctypes.windll.iphlpapi
        ptr = ctypes.POINTER(_MIB_IF_TABLE2)()
        if iphlpapi.GetIfTable2(ctypes.byref(ptr)) != 0:
            return {}
    except Exception:
        return {}
    out = {}
    try:
        row_sz = ctypes.sizeof(_MIB_IF_ROW2)
        base = ctypes.addressof(ptr.contents) + ctypes.sizeof(_MIB_IF_TABLE2)
        for i in range(ptr.contents.NumEntries):
            r = _MIB_IF_ROW2.from_address(base + i * row_sz)
            out[r.Alias] = {
                "tx": r.TransmitLinkSpeed / 1e6,
                "rx": r.ReceiveLinkSpeed / 1e6,
                "oper": r.OperStatus,
                "media": r.MediaConnectState,
                "in_err": r.InErrors,
                "out_err": r.OutErrors,
                "desc": r.Description,
            }
    finally:
        try:
            ctypes.windll.iphlpapi.FreeMibTable(ptr)
        except Exception:
            pass
    return out

# USB 网卡芯片线索（PID -> 说明）
CHIP_HINTS = {
    "8153": "Realtek RTL8153 系（老工艺，长时间高负载发热后可能掉速）",
    "8156": "Realtek RTL8156（2.5G）",
    "88179": "ASIX AX88179",
    "8152": "Realtek RTL8152（多为百兆）",
}


def reg_read(path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def reg_int(path, name):
    """读注册表里的数值项。注意：同一项在不同驱动下可能是 REG_DWORD(int)
    也可能是 REG_SZ(str)，所以统一转成 int 再比较，否则会 TypeError。"""
    v = reg_read(path, name)
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def enum_adapters():
    """枚举网卡类下的所有适配器 -> [(idx, desc, pnp, path)]"""
    rows = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CLASS) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                    i += 1
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                path = CLASS + "\\" + sub
                desc = reg_read(path, "DriverDesc")
                if not desc:
                    continue
                guid = reg_read(path, "NetCfgInstanceId")
                pnp = ""
                if guid:
                    pnp = reg_read(NETKEY + "\\" + guid + "\\Connection",
                                   "PnpInstanceID") or ""
                rows.append((sub, desc, pnp, path))
    except Exception as e:
        print("  !! 枚举网卡失败:", e)
    return rows


def classify(desc, pnp):
    u = (pnp or "").upper()
    d = (desc or "").lower()
    if u.startswith("USB"):
        return "USB"
    if u.startswith("PCI"):
        return "PCIe"
    if u.startswith("BTH"):
        return "BTH"
    if u.startswith("ROOT") or u.startswith("{"):
        return "VIRT"
    if any(x in d for x in ("vmware", "virtual", "vpn", "pangp", "tap", "loopback")):
        return "VIRT"
    return "OTHER"


# 网卡类别有优劣之分：板载 > 扩展坞(雷电隧道 PCIe) > USB。
# 同名网卡（比如两块一模一样的 USB 网卡）取等级更高的那一块。
_KIND_RANK = {"onboard": 3, "dock": 2, "usb": 1}


def mark_kind(store, desc, kind):
    if _KIND_RANK.get(kind, 0) >= _KIND_RANK.get(store.get(desc), -1):
        store[desc] = kind


def is_vpn_virt(desc):
    d = (desc or "").lower()
    return any(x in d for x in ("vpn", "pangp", "globalprotect", "anyconnect",
                                "openvpn", "tap", "tunnel", "wan miniport"))


def pci_location(pnp):
    """读 PCI 设备的 LocationInformation，返回 (bus, dev, func) 或 None"""
    loc = reg_read(USB_ENUM + "\\" + pnp, "LocationInformation")
    if not loc:
        return None
    m = re.search(r"\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)", str(loc))
    return tuple(int(x) for x in m.groups()) if m else None


def onboard_verdict(pnp):
    """判断 PCIe 网卡是「板载」还是「经扩展坞/雷电隧道」。
    两条判据（任一命中即可）：
      ① PCI bus == 0  —— 板载设备挂在 PCH 的 0 号总线上
      ② ContainerID 为全 0/ffff —— Windows 用它表示「不属于任何物理外接容器」
    扩展坞出来的网卡通常落在高 bus（如 15）且带真实 ContainerID。
    返回 (是否板载, 判据说明)
    """
    loc = pci_location(pnp)
    if loc:
        if loc[0] == 0:
            return True, f"PCI bus 0, dev {loc[1]}（PCH 板载）"
        return False, f"PCI bus {loc[0]}（高总线号 → 扩展坞 / 雷电隧道）"
    cont = reg_read(USB_ENUM + "\\" + pnp, "ContainerID") if pnp else None
    if cont:
        if str(cont).lower().startswith("{00000000-"):
            return True, "ContainerID 为全 0（无外接容器 → 板载）"
        return False, "带独立 ContainerID（属于某个外接设备 → 扩展坞）"
    return True, "未能判断（按板载处理，请自行确认）"


def parse_ipconfig():
    """解析 ipconfig /all：返回 {适配器名: {'desc','ip','mac','state'}}

    注意：条目之间用空行分隔，但块内也可能出现空行，所以这里用「逐行状态机」
    而不是按空行切块 —— 并且正则不能写 \\s*（在 MULTILINE 下会跨行吞噬）。
    """
    try:
        r = subprocess.run(["ipconfig", "/all"], capture_output=True, text=True,
                           encoding="gbk", errors="replace")
        text = r.stdout or ""
    except Exception:
        return {}

    out = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^[ \t]*(?:[\w\- ]+ )?adapter (.+?):[ \t]*$", line)
        if m:
            cur = m.group(1).strip()
            out[cur] = {"desc": "", "ip": "", "mac": "", "state": ""}
            continue
        if cur is None:
            continue
        for key, pat in (("desc", r"^[ \t]*Description[ .]+: (.+?)[ \t]*$"),
                         ("mac", r"^[ \t]*Physical Address[ .]+: (.+?)[ \t]*$"),
                         ("ip", r"^[ \t]*IPv4 Address[ .]+: (.+?)[ \t]*$"),
                         ("state", r"^[ \t]*Media State[ .]+: (.+?)[ \t]*$")):
            mm = re.match(pat, line)
            if mm:
                out[cur][key] = mm.group(1).strip()
                break
    return out


def route_of(host):
    """问内核：发往 host 时，源地址会是哪个（不真发包）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 5001))
        return s.getsockname()[0]
    except Exception as e:
        return "解析失败: " + str(e)
    finally:
        s.close()


def usb_flags(idx, pnp):
    """读取 USB 网卡的省电相关设置"""
    flags = []
    path = CLASS + "\\" + idx

    extra = reg_int(path, "EnableExtraPowerSaving")
    if extra == 1:
        flags.append(("中", "额外省电已开启 (EnableExtraPowerSaving=1)",
                      "网卡驱动自带省电逻辑；可在驱动属性里关闭"))
    eee = reg_int(path, "*EEE")
    if eee == 1:
        flags.append(("中", "节能以太网 EEE 已开启 (*EEE=1)",
                      "链路空闲时进低功耗，唤醒需时间 → 周期性时延抖动"))
    jumbo = reg_int(path, "*JumboPacket")
    if jumbo and jumbo > 1514:
        flags.append(("低", f"巨帧已开启 (*JumboPacket={jumbo})",
                      "与交换机 MTU 不一致时会造成丢包；测试建议关闭"))

    # USB 选择性挂起：在 Enum\<pnp>\Device Parameters 下
    parts = (pnp or "").split("\\")
    if len(parts) >= 3 and parts[0].upper() in ("USB", "USBSTOR"):
        dev_path = (USB_ENUM + "\\" + parts[0] + "\\" + parts[1] + "\\"
                    + "\\".join(parts[2:]) + "\\Device Parameters")
        sus = reg_int(dev_path, "DeviceSelectiveSuspended")
        if sus == 1:
            flags.append(("高", "USB 选择性挂起已启用 (DeviceSelectiveSuspended=1)",
                          "空闲时网卡会被挂起；长测中可能掉链、唤醒有延迟"))
    return flags


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None

    hostname = socket.gethostname()
    print("=" * 74)
    print(f" 测试端网卡体检   主机: {hostname}")
    print("=" * 74)

    rows = enum_adapters()
    ipcfg = parse_ipconfig()
    usb_list, pcie_list, virt_list = [], [], []
    # 【七】总判定要用到：每块有线网卡属于哪一类、通没通、跑到多少
    wired_kind, connected, gig = {}, {}, {}
    route_kind = None

    for idx, desc, pnp, path in rows:
        kind = classify(desc, pnp)
        if kind == "USB":
            usb_list.append((idx, desc, pnp, path))
        elif kind == "PCIe":
            pcie_list.append((idx, desc, pnp, path))
        else:
            virt_list.append((idx, desc, pnp, kind))
    # PCIe 里的 Wi-Fi 也归「非有线」，单独提示
    wifi = [r for r in pcie_list if "wi-fi" in r[1].lower() or "wireless" in r[1].lower()]

    print("\n【一】有线网卡（PCIe）")
    if not pcie_list:
        print("  未找到 PCIe 网卡 —— 这台机器可能只能用扩展坞，风险见【二】")
    for idx, desc, pnp, path in pcie_list:
        if "wi-fi" in desc.lower() or "wireless" in desc.lower():
            continue
        prov = reg_read(path, "ProviderName") or "-"
        ver = reg_read(path, "DriverVersion") or "-"
        jumbo = reg_int(path, "*JumboPacket")
        onbd, why = onboard_verdict(pnp)
        mark_kind(wired_kind, desc, "onboard" if onbd else "dock")
        print(f"  [{'OK' if onbd else '(!)'}] {idx}  {desc}")
        print(f"       总线: PCIe   位置: {why}")
        print(f"       厂商: {prov}   驱动: {ver}")
        if jumbo and jumbo > 1514:
            print(f"       (!) 巨帧开启 (*JumboPacket={jumbo})，测试建议关闭")
        if onbd:
            print("       -> 时延测量的首选。")
        else:
            print("       -> 疑似经扩展坞（雷电/USB4 隧道）接出：延迟高于板载网口，")
            print("          时延测量请优先改用板载网口。")

    if wifi:
        print("\n  [--] 无线网卡（不用于有线测试，仅列出）")
        for idx, desc, pnp, path in wifi:
            print(f"       {idx}  {desc}")

    print("\n【二】USB / 扩展坞网卡  <== 重点看这里")
    if not usb_list:
        print("  [OK] 未发现 USB 网卡。")
    for idx, desc, pnp, path in usb_list:
        pid = ""
        m = re.search(r"PID_([0-9A-Fa-f]{4})", pnp or "")
        if m:
            pid = m.group(1).lower()
        print(f"  [!!] {idx}  {desc}")
        print(f"       总线: USB (扩展坞 / 外接)   PnP: {pnp}")
        mark_kind(wired_kind, desc, "usb")
        if pid and pid in CHIP_HINTS:
            print(f"       芯片: {CHIP_HINTS[pid]}")
        flags = usb_flags(idx, pnp)
        if flags:
            print("       需要处理的设置：")
            for lvl, item, why in flags:
                print(f"         - [{lvl}] {item}")
                print(f"                 {why}")
        print("       结论：不要用它做时延测量 —— USB 是轮询总线，会给 RTT 带进")
        print("             100 µs ~ 几 ms 的固定偏置与周期抖动，足以盖住交换机之间")
        print("             的真实时延差异。**但丢包率、环网中断时长不受影响**：")
        print("             只要发包速率低于这块网卡的接收上限就行（手册 1.6 / 1.8 节）。")

    print("\n【三】虚拟网卡 / VPN / 蓝牙（路由干扰源）")
    if not virt_list:
        print("  无。")
    for idx, desc, pnp, kind in virt_list:
        if is_vpn_virt(desc):
            tag = "VPN!"
        elif kind == "BTH":
            tag = "蓝牙"
        elif "wi-fi direct" in desc.lower():
            tag = "WiFi直连"
        elif kind == "OTHER":
            tag = "其它"
        else:
            tag = "虚拟"
        print(f"  [(!)] {idx}  {desc}   [{tag}]")
    if any(is_vpn_virt(r[1]) for r in virt_list):
        print("       ⚠ 检测到 VPN 虚拟网卡：测量时务必确认它没有接管到被测网段的")
        print("         路由（VPN 连接状态下尤其危险），否则流量会绕进隧道。")

    print("\n【四】本机 IPv4 地址（对照用）")
    for name, d in ipcfg.items():
        if d["ip"] or d["state"]:
            print(f"  {name}")
            print(f"     描述: {d['desc'] or '-'}")
            print(f"     IP  : {d['ip'] or '-'}   MAC: {d['mac'] or '-'}"
                  f"   {d['state'] or ''}")

    # ---- 实际协商速率：千兆测试的硬前提（手册 1.5 节）----
    speeds = if_speeds()
    desc2alias = {}
    for name, d in ipcfg.items():
        if d.get("desc"):
            desc2alias.setdefault(d["desc"].strip(), name)

    print("\n【五】实际协商速率与错误计数  <== 千兆测试的前提")
    wired = [(idx, desc) for idx, desc, _p, _pa in pcie_list
             if not any(x in desc.lower() for x in ("wi-fi", "wireless"))]
    wired += [(idx, desc) for idx, desc, _p, _pa in usb_list]
    if not wired:
        print("  没有可查的有线网卡。")
    slow = []
    for idx, desc in wired:
        alias = desc2alias.get(desc.strip(), "")
        sp = speeds.get(alias)
        if not sp:
            connected[desc] = gig[desc] = False
            print(f"  [--] {desc}   （未启用 / 未连接，读不到速率）")
            continue
        tx = sp["tx"]
        up = (sp["oper"] == 1 and sp["media"] == 1)
        # 同名网卡只要有一块通、有一块跑到千兆，就认为这台机器能用它做测量端
        connected[desc] = connected.get(desc, False) or up
        gig[desc] = gig.get(desc, False) or (up and tx >= 1000)
        if not up:
            print(f"  [--] {desc}   （链路未连接，速率 {tx:.0f} Mbps）")
            continue
        ok = tx >= 1000
        if not ok:
            slow.append((desc, tx))
        print(f"  [{'OK' if ok else '!!'}] {desc}")
        print(f"       当前协商速率: {tx:.0f} Mbps   链路 已连接")
        print(f"       错误计数    : InErrors={sp['in_err']:,}  "
              f"OutErrors={sp['out_err']:,}   (测试前后各看一次，增量必须是 0)")
        if not ok:
            print("       ⚠ 没跑到 1 Gbps：可能线缆只有 2 对通 / 水晶头压接不良 /")
            print("         对端是百兆口。百兆链路上测不出千兆交换机的性能（手册 1.5）")
    if wired:
        if slow:
            print(f"\n  ⚠ 共 {len(slow)} 块网卡没协商到千兆 —— 先把链路解决，再谈交换机指标。")
        else:
            print("\n  ✓ 有线网卡都跑到千兆（或没有已连接的有线网卡）。")

    if target:
        src = route_of(target)
        print(f"\n【六】路由决策：如果发往 {target}")
        print(f"  内核会使用源地址: {src}")
        hit = None
        for name, d in ipcfg.items():
            if d["ip"] and src and d["ip"].split("(")[0] in src:
                hit = (name, d)
                break
        if hit:
            name, d = hit
            low = (d["desc"] or "").lower()
            k = wired_kind.get((d["desc"] or "").strip())
            print(f"  -> 即从「{name}」({d['desc']}) 出去")
            if k == "usb" or any(x in low for x in ("usb", "realtek")):
                route_kind = "usb"
                print("  ⚠ 走的是 USB 扩展坞网卡 —— 时延数据会被 USB 抖动污染（100 µs~ms）")
            elif any(x in low for x in ("vpn", "pangp", "globalprotect", "wan miniport")):
                route_kind = "vpn"
                print("  ⚠ 走的是 VPN / 隧道虚拟网卡 —— 流量进了隧道，必须修正路由再测")
            elif any(x in low for x in ("wi-fi", "wireless", "wlan")):
                route_kind = "wifi"
                print("  ⚠ 走的是无线网卡 —— 无线不适合做有线测试的测量端")
            elif any(x in low for x in ("vmware", "virtual", "vmnet", "hyper-v")):
                route_kind = "virt"
                print("  ⚠ 走的是虚拟机虚拟网卡 —— 流量没出物理网口")
            else:
                route_kind = "wired"
                print("  ✓ 看起来是有线网卡；若是扩展坞接出的 PCIe 网卡，见【一】的提示")
        else:
            print(f"  （源地址 {src} 未对应到已列出的网卡，请人工确认）")

    # ---- 总判定：把前面各项收成一句话，两台机器各跑一遍即可横向对比 ----
    print("\n【七】总判定：这台机器适合当哪种测量端")
    onboard_any = [d for d, k in wired_kind.items()
                   if k == "onboard" and connected.get(d)]
    onboard_gig = [d for d in onboard_any if gig.get(d)]
    dock_any = [d for d, k in wired_kind.items() if k == "dock" and connected.get(d)]
    usb_any = [d for d, k in wired_kind.items() if k == "usb" and connected.get(d)]

    if route_kind in ("wifi", "vpn", "virt"):
        grade = "不可用（先修路由）"
        why = {"wifi": "被测流量会从无线网卡出去",
               "vpn": "被测流量会进 VPN 隧道",
               "virt": "被测流量进了虚拟网卡"}[route_kind]
    elif onboard_gig:
        grade = "优"
        why = f"有板载千兆有线口（{onboard_gig[0]}）—— 时延/抖动/丢包/环网 全部可测"
    elif onboard_any:
        grade = "可（降级）"
        why = (f"板载口存在（{onboard_any[0]}）但没协商到千兆 —— 先把线缆/对端口解决，"
               f"百兆链路上测不出千兆交换机")
    elif dock_any:
        grade = "可（降级）"
        why = (f"有线口来自扩展坞（{dock_any[0]}，雷电/USB4 隧道 PCIe）—— "
               f"时延仅次于板载，建议只做两台交换机之间的相对对比")
    elif usb_any:
        grade = "只适合丢包 / 环网"
        why = (f"只有 USB 网卡（{usb_any[0]}）—— 时延绝对值被 USB 抖动污染，"
               f"丢包率与环网中断时长不受影响")
    else:
        grade = "不可用"
        why = "没有已连接的有线网卡"
    print(f"  等级: {grade}")
    print(f"  理由: {why}")
    if route_kind is None and any(connected.get(d) for d in wired_kind):
        print("  （没给对端 IP，所以没检查出口路由。正式测前请再跑一次：")
        print("    python net_check.py <对端IP>）")

    print("\n" + "=" * 74)
    print(" 提示：时延测量的两端都应使用板载 PCIe 有线网卡；")
    print("       USB 扩展坞网卡只适合做「铺端口 / 造背景流量」，")
    print("       以及丢包率、环网自愈这类不依赖时延精度的项目。")
    print("=" * 74)


if __name__ == "__main__":
    main()
