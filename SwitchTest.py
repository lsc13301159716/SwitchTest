#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""软件 UDP 端到端交换机测试工具 v2。

命令仍为 server / echo / flood / compare / gaps / bench。
协议已更新，收发两端必须一起部署 SwitchTest.py 和 switch_core.py。
详细用法、统计定义与限制见 README.md；无需第三方 Python 库。
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import math
import multiprocessing as mp
import os
import socket
import sys
import time
import queue
import contextlib
from pathlib import Path
from switch_core import (HDR, TAG_ECHO, TAG_FLOOD, TAG_WARM, TAG_END, TAG_ACK,
                         ReceiveLedger, EchoLedger, new_session)
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# v2 报文头：tag(1) + seq(4) + session(4) + 发送时刻纳秒(8) = 17 字节
# HDR imported from switch_core (v2 protocol)
HDR_LEN = HDR.size
# 预热包专用 tag：预热只为填 ARP 邻居表，不该进入任何统计。
# 原先 flood 的预热包也用 TAG_FLOOD、序号同样从 1 开始，于是它的包数会被
# 接收端算进"应收包数"里 —— 接收端按 (最大序号 - 收包数) 估丢包时，
# 这部分多余的计数会把真丢包**掩盖成更小的值**（估算偏乐观，是危险方向）。
# 接收端对 TAG_WARM 的处理是「**回显但不计数**」：回显是为了让发送端能探测
# 对端是否在线，不计数是为了不污染丢包统计（见 cmd_server / _server_fast）。

# 以太网帧 -> UDP 载荷 的扣除量
# 14(以太网头) + 4(FCS) + 20(IP) + 8(UDP) = 46
ETH_OVERHEAD = 46

# 多流（-j）时每条流各有自己的序号空间，序号都从 1 开始；
# 落盘 CSV 靠 worker 列区分是哪条流，seq 不做偏移 —— 这样流数不受 4 字节序号限制，
# 读回来时按 worker 分组即可正确判定每条的「连续丢包 = 一次中断」。
MAX_JOBS = 16   # 上限只为拦住"一台 PC 开几十个进程互相抢 CPU"的误用，不是协议限制


# ------------------------------------------------- 内核 IP 层丢弃计数（Windows）
# 它想解决什么问题：接收端报告里的丢包是按 (该源最大序号 - 收到包数) 推算的，
#   只能回答"有多少包没到"，回答不了"在哪一段丢的"。USB/扩展坞网卡、驱动缓冲、
#   内核接收缓冲，都可能在包**已经到了本机之后**再把它扔掉 —— 那类丢包会被
#   原样记到交换机头上。
#
# 但它的局限必须写清楚（本机 Windows 实测，勿删）：
#   把一块"故意不读"的 socket 收缓冲打满，灌进 30,000 个包，应用层只读到 456
#   个（29,544 个被缓冲溢出丢掉），而 dwInDiscards 只涨了 129。也就是说
#   **socket 因缓冲溢出丢的包，并不计入这个计数器**。另外 loopback 流量的
#   dwInReceives 也明显不完整（30,000 个包只涨 10,716），同样不能用来对账。
#   结论：这个计数**只在增长时可信**；它为 0 不代表"接收端没丢包"。
#   因此只作辅助证据，需结合直连基线与网卡、交换机端口计数定位。
class _MIB_IPSTATS(ctypes.Structure):
    # 补齐到 MIB_IPSTATS 的完整长度（23 个 DWORD），别让结构体比内核写入的短。
    _fields_ = [
        ("dwForwarding", ctypes.c_uint32),
        ("dwDefaultTTL", ctypes.c_uint32),
        ("dwInReceives", ctypes.c_uint32),
        ("dwInHdrErrors", ctypes.c_uint32),
        ("dwInAddrErrors", ctypes.c_uint32),
        ("dwForwDatagrams", ctypes.c_uint32),
        ("dwInUnknownProtos", ctypes.c_uint32),
        ("dwInDiscards", ctypes.c_uint32),
        ("dwInDelivers", ctypes.c_uint32),
    ] + [(f"_tail_{i}", ctypes.c_uint32) for i in range(14)]


def net_discards():
    """内核 IPv4 丢弃计数（全网卡合计）；读不到返回 None。"""
    if not sys.platform.startswith("win"):
        return None
    buf = ctypes.create_string_buffer(512)
    p = ctypes.cast(buf, ctypes.POINTER(_MIB_IPSTATS))
    try:
        if ctypes.windll.iphlpapi.GetIpStatisticsEx(p, socket.AF_INET) != 0:
            return None
    except Exception:
        return None
    return p.contents.dwInDiscards


def _print_kernel_verdict(d0, d1):
    if d0 is None or d1 is None:
        print("  内核 IPv4 丢弃增量：不可用")
    else:
        print(f"  内核 IPv4 丢弃增量：{(d1-d0) & 0xffffffff:,}（全网卡合计）")
    print("  此计数不能给本测试流归因；零增量也不能排除 socket 缓冲溢出。")
    print("  降速后丢包减少仅说明负载相关；需直连基线及网卡/交换机端口计数定位。")


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class _LogTee:
    """Keep the console live; append complete UTF-8 lines to the log."""
    def __init__(self, console, log):
        self.console, self.log = console, log
        self.pending = ""

    @property
    def encoding(self):
        return getattr(self.console, "encoding", "utf-8")

    def write(self, text):
        self.console.write(text)
        self.pending += text
        end = self.pending.rfind("\n")
        if end >= 0:
            self.log.write(self.pending[:end + 1])
            self.pending = self.pending[end + 1:]
        return len(text)

    def flush(self):
        self.console.flush()
        if self.pending:
            self.log.write(self.pending)
            self.pending = ""
        self.log.flush()

    def isatty(self):
        return self.console.isatty()


@contextlib.contextmanager
def _log_output(path, append=False):
    if not path:
        yield
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not append:
        # All processes must append. A parent with a fixed file offset would
        # overwrite bytes appended by workers when it resumes its own output.
        with target.open("xb") as created:
            created.write(b"\xef\xbb\xbf")
    with target.open("a", encoding="utf-8", buffering=1) as output:
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _LogTee(stdout, output), _LogTee(stderr, output)
        try:
            yield
        finally:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            finally:
                sys.stdout, sys.stderr = stdout, stderr


def _default_log_path(command):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return str((Path("out") / "logs" / f"{stamp}_{command}.log").resolve())


def payload_for_frame(frame_len: int) -> int:
    """把 RFC 2544 的以太网帧长换算成本工具要发的 UDP 载荷长度。"""
    return max(HDR_LEN, frame_len - ETH_OVERHEAD)


def frame_bits(frame_len: int) -> int:
    """一帧在线上占用的比特数（含 12B 帧间隙 + 8B 前导 = 20B ≈ 160bit）。"""
    return frame_len * 8 + 160


def analyze(records):
    """records: [(seq, rtt_us or None)]，返回统计字典。"""
    records = sorted(records, key=lambda row: row[0])
    sent = len(records)
    rtts = [r for _, r in records if r is not None]
    recv = len(rtts)
    lost = sent - recv
    st = {
        "sent": sent,
        "recv": recv,
        "lost": lost,
        "loss_pct": (lost / sent * 100.0) if sent else 0.0,
        "rtt_min": min(rtts) if rtts else 0.0,
        "rtt_avg": (sum(rtts) / recv) if recv else 0.0,
        "rtt_max": max(rtts) if rtts else 0.0,
        "rtt_p99": 0.0,
        "jitter": 0.0,
        "max_dev": 0.0,
        "outages": [],
    }
    if rtts:
        s = sorted(rtts)
        # P99 取「第 ceil(0.99*n) 个样本」再回退一格。原来的 int(n*0.99) 是个
        # off-by-one：n=100 时正好取到 s[99] = 最大值，把 P99 抬成了 max
        # （偏悲观，会让两台交换机的对比结论失真）。
        st["rtt_p99"] = s[min(len(s) - 1, max(0, math.ceil(len(s) * 0.99) - 1))]

    # RFC 3550 平滑抖动 + 相邻最大偏差
    j = 0.0
    prev = None
    maxdev = 0.0
    for r in rtts:
        if prev is not None:
            d = abs(r - prev)
            j += (d - j) / 16.0
            if d > maxdev:
                maxdev = d
        prev = r
    st["jitter"] = j
    st["max_dev"] = maxdev

    # 连续丢失 = 一次链路中断
    seq_map = {q: r for q, r in records}
    ordered = sorted(seq_map.items())
    run_start = None
    run_len = 0
    runs = []
    for seq, r in ordered:
        if r is None:
            if run_start is None:
                run_start = seq
                run_len = 1
            else:
                run_len += 1
        else:
            if run_start is not None:
                runs.append((run_start, run_len))
                run_start = None
                run_len = 0
    if run_start is not None:
        runs.append((run_start, run_len))
    # 连续丢 1 个包只是零星丢包（不构成链路中断），>=2 个才算中断事件
    st["runs"] = runs
    st["outages"] = [(s_, n) for s_, n in runs if n >= 2]
    st["single_loss"] = sum(1 for _, n in runs if n == 1)
    st["max_run"] = max([n for _, n in runs], default=0)
    # 最后一段丢失一直延续到第 sent 个包 → 尾部整段丢失（见 print_stats 的警示）
    st["tail_cut"] = any(s0 + n - 1 == (ordered[-1][0] if ordered else 0) for s0, n in st["outages"])
    st["streams"] = 1
    st["per_stream"] = [st]
    return st


def analyze_multi(groups):
    """groups: [[(seq, rtt or None), ...], ...] —— 每个元素是一条**独立流**的记录。

    为什么必须分流处理：-j N 时每条流各有自己的序号空间。若把 N 条流按序号混排，
    「连续丢包」会被跨流交错彻底打乱（A 流第 10 个包丢了、B 流第 11 个包到了），
    算出的中断次数毫无意义。所以：**中断/断流按流单独判定，RTT 分位数合并统计**。
    """
    sts = [analyze(g) for g in groups]
    all_rtts = [r for g in groups for _, r in g if r is not None]
    sent = sum(s["sent"] for s in sts)
    recv = sum(s["recv"] for s in sts)
    lost = sent - recv
    st = {
        "sent": sent,
        "recv": recv,
        "lost": lost,
        "loss_pct": (lost / sent * 100.0) if sent else 0.0,
        "rtt_min": min(all_rtts) if all_rtts else 0.0,
        "rtt_avg": (sum(all_rtts) / len(all_rtts)) if all_rtts else 0.0,
        "rtt_max": max(all_rtts) if all_rtts else 0.0,
        "rtt_p99": 0.0,
        # 抖动（RFC3550）是「相邻两包 RTT 之差」的平滑值，跨流相减没有物理意义
        # → 每条流各算一份，取最差的那条作为整轮的代表值。
        "jitter": max([s["jitter"] for s in sts], default=0.0),
        "max_dev": max([s["max_dev"] for s in sts], default=0.0),
        "outages": [x for s in sts for x in s["outages"]],
        "runs": [x for s in sts for x in s["runs"]],
        "single_loss": sum(s["single_loss"] for s in sts),
        "max_run": max([s["max_run"] for s in sts], default=0),
        "tail_cut": any(s.get("tail_cut") for s in sts),
        "streams": len(sts),
        "per_stream": sts,
    }
    if all_rtts:
        srt = sorted(all_rtts)
        # 同 analyze()：修正 P99 的 off-by-one（int(n*0.99) 在 n=100 时取到 max）
        st["rtt_p99"] = srt[min(len(srt) - 1, max(0, math.ceil(len(srt) * 0.99) - 1))]
    return st


LOST_SEQ_SHOW = 20      # 报告里最多展开这么多个丢失包号，其余折叠成"另有 N 个"


def fmt_lost_seqs(seqs, cap=LOST_SEQ_SHOW):
    """把丢失的包号排成一行；超过 cap 个就折叠，免得尾部整段丢失时刷屏。"""
    if not seqs:
        return ""
    head = ", ".join(str(x) for x in seqs[:cap])
    if len(seqs) > cap:
        head += f" …（另有 {len(seqs) - cap} 个）"
    return head


def time_hint(seq, interval_s=None, pps=None):
    """把包号换算成「测试开始后第几秒」。

    包号连续递增（每次尝试都前进一格），所以它等价于一根时间轴。
    **优先用 pps（实测发包速率）**：按 `(seq-1)/pps` 换算才准。

    为什么不能只按 `--interval` 算：软件打流经常打不满设定值 —— 本机 600 s
    长跑实测只达成 96.8%（968/1000 pps）。这时 `(seq-1)×interval` 会**系统性偏小**：
    同样一个 seq 546166，按 1 ms 算是第 546 s，按实测 968 pps 算是第 564 s，
    **差了 18 秒** —— 足够让你对错墙钟上的那件事。所以报告里一律优先给实测速率。

    两个都没给就不返回（宁可不猜）。
    """
    if seq is None or seq <= 0:
        return ""
    if pps and pps > 0:
        t = (seq - 1) / pps
    elif interval_s:
        t = (seq - 1) * interval_s
    else:
        return ""
    return f"  （≈ 第 {t:.3f} s）"


def print_singles(singles, interval_s, pps, indent="      "):
    """孤立丢包怎么排版：≤5 个就逐个给出「包号 + 时刻」，更多才折叠成一行。

    之所以逐个给：孤立丢包的价值全在**时刻**上 —— "第 0.05 s 丢的"（开局 ARP/STP）
    和"第 564 s 丢的"（中途某次瞬态）结论完全不同，只报个包号等于没说。
    """
    if not singles:
        return
    if len(singles) <= 5:
        for q in singles:
            print(f"{indent}· 孤立丢包 seq {q}{time_hint(q, interval_s, pps)}")
    else:
        print(f"{indent}· 丢的是    : {fmt_lost_seqs(singles)}"
              f"{time_hint(singles[0], interval_s, pps)}")


def print_stats(st, title, frame_len, duration_s, interval_s=None, extra="",
                want_pps=None, snd_fail=0):
    print("\n" + "=" * 62)
    print(title)
    if extra:
        print(extra)
    print(f"帧长 {frame_len} B / UDP 载荷 {payload_for_frame(frame_len)} B；发包时长 {duration_s:.3f}s")
    print(f"尝试 {st['sent']:,}；按时回包 {st['recv']:,}；超时或本地失败 {st['lost']:,}（{st['loss_pct']:.4f}%）")
    print(f"本地发送失败 {snd_fail:,}（独立记录，不等于交换机丢包）")
    if want_pps and duration_s > 0:
        submitted = (st['sent'] - snd_fail) / duration_s
        print(f"实际提交速率 {submitted:,.0f} pps；目标 {want_pps:,.0f} pps；达成 {submitted/want_pps*100:.1f}%")
    if st['recv']:
        print(f"RTT µs：最小 {st['rtt_min']:.1f} / 平均 {st['rtt_avg']:.1f} / P99 {st['rtt_p99']:.1f} / 最大 {st['rtt_max']:.1f}")
        print(f"RTT 平滑变化 {st['jitter']:.1f} µs；相邻最大变化 {st['max_dev']:.1f} µs")
    print(f"连续探测失败段（>=2包）{len(st['outages'])}；最长 {st['max_run']} 包；孤立失败 {st['single_loss']}")
    print("连续探测失败不自动证明链路中断；发送时间边界只表示探测分辨率。")
    if st.get('tail_cut'):
        print("尾部连续失败：截至观察结束未看到按时恢复，不能给出完整自愈时长。")
    if 'late' in st:
        late = st['late']
        print(f"迟到回包 {len(late):,}；观察结束未回包/本地失败 {st['lost']-len(late):,}")
        if late:
            print(f"迟到回包最大实际 RTT {max(u for _, u in late)/1000:.3f} ms")
    if st.get('streams', 1) > 1:
        for wid, stream in zip(st.get('worker_ids', range(st['streams'])), st['per_stream']):
            print(f"流{wid}：尝试 {stream['sent']:,}，超时/失败 {stream['lost']:,}，最长失败段 {stream['max_run']}")
    print("=" * 62)


def stamp_out(name):
    return name or f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_report.csv"


# ------------------------------------------------------------------ server
def _sender_loss(senders):
    expected = sum(v.expected if v.expected is not None else v.maximum
                   for v in senders.values())
    lost = sum(v.missing for v in senders.values())
    unknown = sum(v.expected is None for v in senders.values())
    return (lost / expected * 100 if expected else 0), lost, len(senders), unknown


def _export_missing(senders, log_path):
    """Write final FLOOD missing IDs after closing reception, never in the hot loop."""
    streams = [(key, ledger) for key, ledger in senders.items() if key[0] == TAG_FLOOD]
    if not streams:
        return []
    if log_path:
        log = Path(log_path)
        directory = log.parent / (log.stem + '_missing')
    else:
        directory = Path('out/missing') / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + f'_{os.getpid()}')
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = datetime.now().astimezone().isoformat(timespec='milliseconds')
    files = []
    for (tag, addr, session), ledger in streams:
        ip, port = addr
        path = directory / f'F_{session:08x}_{ip}_{port}_missing.csv'
        basis = 'reconciled' if ledger.expected is not None else 'observed_prefix_tail_unknown'
        with path.open('x', encoding='utf-8-sig', newline='') as output:
            writer = csv.writer(output)
            writer.writerow(['session', 'source_ip', 'source_port', 'missing_seq',
                             'basis', 'expected_total', 'observed_max', 'unique_received',
                             'missing_count', 'snapshot_time'])
            for seq in ledger.missing_sequences():
                writer.writerow([f'{session:08x}', ip, port, seq, basis,
                                 ledger.expected if ledger.expected is not None else '',
                                 ledger.maximum, ledger.unique, ledger.missing, snapshot])
        files.append(path)
        print(f'缺失包号清单：{path.resolve()}（{ledger.missing:,}条，'
              + ('已取得结束总数）' if ledger.expected is not None else '尾部未知，仅已观察范围）'))
    return files


def _server_fast(args):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        sock.bind((args.bind, args.port))
        sock.settimeout(0.1)
        start = time.perf_counter()
        print(f"v2 极简回显启动：{args.bind}:{args.port}", flush=True)
        print("不统计 flood，也不确认 flood 结束总数；flood 请使用普通 server。", flush=True)
        try:
            while not args.duration or time.perf_counter()-start < args.duration:
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                if len(data) >= HDR_LEN and data[:1] in (TAG_ECHO, TAG_WARM):
                    try:
                        sock.sendto(data[:HDR_LEN], addr)
                    except OSError:
                        pass
        except KeyboardInterrupt:
            pass


def cmd_server(args):
    if args.fast:
        return _server_fast(args)
    senders = {}
    total_pkts = total_bytes = win_pkts = win_bytes = 0
    disc0 = net_discards()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        sock.bind((args.bind, args.port))
        sock.settimeout(0.1)
        t0 = last_tick = time.perf_counter()
        print(f"[{now_str()}] v2 接收端启动：{args.bind}:{args.port}", flush=True)
        print("未取得发送端结束总数的流只能报告序号缺口，无法识别尾部丢失。", flush=True)
        try:
            while not args.duration or time.perf_counter() - t0 < args.duration:
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    data = b""
                if len(data) >= HDR_LEN:
                    tag, seq, session, ts = HDR.unpack_from(data)
                    if tag == TAG_WARM:
                        sock.sendto(data[:HDR_LEN], addr)
                    elif tag == TAG_END and ts in (ord('S'), ord('F')):
                        key = (bytes([ts]), addr, session)
                        ledger = senders.get(key)
                        if ledger is None:
                            ledger = senders[key] = ReceiveLedger()
                        try:
                            ledger.finish(seq)
                        except ValueError as exc:
                            print(f"结束计数无效：{exc}", flush=True)
                        else:
                            sock.sendto(HDR.pack(TAG_ACK, seq, session, ts), addr)
                    elif tag in (TAG_ECHO, TAG_FLOOD) and seq > 0:
                        # Reply before allocating/updating statistics: that work must
                        # not unnecessarily delay the response being measured.
                        if tag == TAG_ECHO:
                            try:
                                sock.sendto(data[:HDR_LEN], addr)
                            except OSError:
                                pass
                        key = (tag, addr, session)
                        ledger = senders.get(key)
                        if ledger is None:
                            ledger = senders[key] = ReceiveLedger()
                        if ledger.add(seq):
                            total_pkts += 1
                            total_bytes += len(data)
                            win_pkts += 1
                            win_bytes += len(data)
                now = time.perf_counter()
                if now - last_tick >= 1:
                    loss, lost, streams, unknown = _sender_loss(senders)
                    print(f"[{now_str()}] 已运行 {now-t0:.1f}s | 收唯一包 {total_pkts:,} | {win_pkts / (now-last_tick):,.0f} pps | "
                          f"UDP {win_bytes*8/(now-last_tick)/1e6:.2f} Mbps | "
                          f"缺口/未到达 {lost:,} | 未对账流 {unknown}", flush=True)
                    last_tick, win_pkts, win_bytes = now, 0, 0
        except KeyboardInterrupt:
            pass
    print("\n接收端汇总（端到端应用层，不用于归因交换机）")
    print(f"唯一包 {total_pkts:,}；UDP 载荷字节 {total_bytes:,}")
    for (tag, addr, session), ledger in senders.items():
        basis = ledger.expected if ledger.expected is not None else ledger.maximum
        label = "对账未到达" if ledger.expected is not None else "已观察序号缺口（尾部未知）"
        print(f"{tag.decode()} {addr} session={session:08x}: 唯一 {ledger.unique:,}, "
              f"重复 {ledger.duplicates:,}, {label} {ledger.missing:,}/{basis:,}")
    _print_kernel_verdict(disc0, net_discards())
    _export_missing(senders, getattr(args, '_log_path', None))


def _open_sender(cfg):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        if cfg["bind"]:
            sock.bind((cfg["bind"], 0))  # failure aborts; never fall back to default route
        addr = (socket.gethostbyname(cfg["host"]), cfg["port"])
        sock.setblocking(False)
        return sock, addr
    except BaseException:
        sock.close()
        raise


def _finish_stream(sock, addr, tag, session, total):
    # Metadata is retried and acknowledged. Its loss never implies zero packet loss.
    sock.settimeout(0.1)
    packet = HDR.pack(TAG_END, total, session, tag[0])
    for _ in range(5):
        try:
            sock.sendto(packet, addr)
            end = time.perf_counter() + 0.1
            while time.perf_counter() < end:
                data, peer = sock.recvfrom(65535)
                if peer == addr and len(data) >= HDR_LEN:
                    if HDR.unpack_from(data) == (TAG_ACK, total, session, tag[0]):
                        return True
        except OSError:
            pass
    return False


def _collect_workers(procs, result_queue):
    # Poll rather than waiting forever when a process exits without putting a result.
    remaining = set(range(len(procs)))
    dead_since = {}
    try:
        while remaining:
            try:
                item = result_queue.get(timeout=0.2)
            except queue.Empty:
                now = time.perf_counter()
                for wid in list(remaining):
                    proc = procs[wid]
                    if proc.exitcode is not None:
                        first = dead_since.setdefault(wid, now)
                        if now - first >= 1:
                            remaining.remove(wid)
                            error = f"进程退出但未返回结果 (exitcode={proc.exitcode})"
                            yield ((wid, None, error) if proc.name != "bench" else
                                   (wid, 0, 0, 0.0, error))
                continue
            if item[0] in remaining:
                remaining.remove(item[0])
                yield item
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
            proc.join(timeout=2)
        result_queue.close()


def _echo_core(cfg, prefix=""):
    session = new_session()
    ledger = EchoLedger(int(cfg["timeout"] * 1e9))
    filler = bytes(payload_for_frame(cfg["frame"]) - HDR_LEN)
    sock, addr = _open_sender(cfg)
    sock_err = snd_fail = 0
    sock_err_first = snd_fail_first = ""
    interval_ns = max(1, int(cfg["interval"] * 1e9))
    try:
        print(f"{prefix}v2 echo → {addr} session={session:08x} 超时 {cfg['timeout']*1000:g} ms")
        warm_end = time.perf_counter() + cfg["warmup"]
        while time.perf_counter() < warm_end:
            stamp = time.perf_counter_ns()
            try:
                sock.sendto(HDR.pack(TAG_WARM, 0, session, stamp) + filler, addr)
                time.sleep(0.01)
                data, peer = sock.recvfrom(65535)
                if peer == addr and data[:HDR_LEN] == HDR.pack(TAG_WARM, 0, session, stamp):
                    break
            except OSError:
                pass
        start_ns = next_send = time.perf_counter_ns()
        last_tick = start_ns
        stop_ns = start_ns + int(cfg["duration"] * 1e9)

        def receive_batch():
            nonlocal sock_err, sock_err_first
            # Bound each batch so a continuous incoming stream cannot starve sends/deadlines.
            for _ in range(256):
                try:
                    data, peer = sock.recvfrom(65535)
                    received_ns = time.perf_counter_ns()
                except BlockingIOError:
                    break
                except OSError as exc:
                    sock_err += 1
                    sock_err_first = sock_err_first or str(exc)
                    break
                if peer != addr or len(data) < HDR_LEN:
                    continue
                tag, seq, run, timestamp = HDR.unpack_from(data)
                if tag == TAG_ECHO and run == session:
                    # Timestamp at the application read boundary, before parsing
                    # or updating Python measurement state.
                    ledger.receive(seq, timestamp, received_ns)

        try:
            while time.perf_counter_ns() < stop_ns:
                now = time.perf_counter_ns()
                if now >= next_send:
                    seq = len(ledger.records) + 1
                    if seq > 0xffffffff:
                        raise ValueError("序号空间耗尽，请分轮测试")
                    stamp = time.perf_counter_ns()
                    success = True
                    try:
                        sock.sendto(HDR.pack(TAG_ECHO, seq, session, stamp) + filler, addr)
                    except OSError as exc:
                        success = False
                        snd_fail += 1
                        snd_fail_first = snd_fail_first or str(exc)
                    ledger.sent(stamp, success)
                    next_send = max(next_send + interval_ns, stamp + interval_ns)
                receive_batch()
                now = time.perf_counter_ns()
                ledger.expire(now)
                if now - last_tick >= 1_000_000_000:
                    losses = len(ledger.records) - ledger.received - len(ledger.pending)
                    avg_us = ledger.rtt_sum / ledger.received if ledger.received else None
                    avg_label = f"{avg_us:.1f} µs" if avg_us is not None else "—"
                    print(f"{prefix}[{now_str()}] 已运行 {(now-start_ns)/1e9:.1f}/{cfg['duration']:g}s | "
                          f"尝试 {len(ledger.records):,} 按时回包 {ledger.received:,} "
                          f"超时/失败 {losses:,} 在途 {len(ledger.pending)} | RTT 累计均 {avg_label}")
                    last_tick = now
        except KeyboardInterrupt:
            print(f"{prefix}停止发包，继续收尾观察…")
        send_end_ns = time.perf_counter_ns()
        # Observe late arrivals for an explicit window; never call them permanent loss.
        drain_ns = int(cfg.get("drain", max(0.2, cfg["timeout"] * 2)) * 1e9)
        drain_end = max(send_end_ns + drain_ns,
                        (ledger.sent_ns[-1] + ledger.timeout_ns) if ledger.sent_ns else send_end_ns)
        while time.perf_counter_ns() < drain_end:
            receive_batch()
            ledger.expire(time.perf_counter_ns())
            if ledger.received + len(ledger.late) + snd_fail == len(ledger.records):
                break
            time.sleep(0.0001)
        ledger.finish()
        observation_s = (time.perf_counter_ns() - send_end_ns) / 1e9
        return {"records": [tuple(row) for row in ledger.records],
                "csv_rows": [(seq, str(rtt) if rtt is not None else "") for seq, rtt in ledger.records],
                "sent": len(ledger.records) - snd_fail, "recv": ledger.received,
                "sock_err": sock_err, "sock_err_first": sock_err_first,
                "snd_fail": snd_fail, "snd_fail_first": snd_fail_first,
                "dur": (send_end_ns - start_ns) / 1e9,
                "observation_s": observation_s, "wid": cfg["wid"],
                "late": list(ledger.late.items()), "outage_bounds": ledger.outage_bounds(),
                "send_s": [(stamp - start_ns) / 1e9 for stamp in ledger.sent_ns],
                "status": ledger.status}
    finally:
        sock.close()


def _echo_cfg(args, wid=0):
    return {
        "host": args.host, "port": args.port, "frame": args.frame,
        "interval": args.interval, "duration": args.duration,
        "timeout": args.timeout, "warmup": args.warmup,
        "drain": args.drain,
        "log_path": getattr(args, "_log_path", None),
        "bind": getattr(args, "bind", "") or "", "wid": wid,
    }


def _echo_worker(cfg, q):
    """多进程 worker：跑完一条流，把结果（或异常字符串）送回父进程。"""
    try:
        with _log_output(cfg.get("log_path"), append=True):
            q.put((cfg["wid"], _echo_core(cfg, prefix=f"[流{cfg['wid']}] "), None))
    except BaseException as e:      # 必须把异常带回父进程，否则只看到进程静默退出
        q.put((cfg["wid"], None, f"{type(e).__name__}: {e}"))


def _write_csv(out_arg, rows):
    """rows: [(worker, seq, rtt_us_str), ...] —— worker 列用于多流 CSV 分流统计。"""
    out = stamp_out(out_arg)
    parent = os.path.dirname(os.path.abspath(out))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["worker", "seq", "rtt_us", "send_s", "status", "late_rtt_us"])
        w.writerows(sorted(rows))
    print(f"  明细已保存    : {os.path.abspath(out)}")
    print("  （可与原装交换机的结果做对比：switch_test.py compare --a 原装.csv --b 国产.csv）")
    return out


def _write_late_csv(out_path, late):
    """把「迟到回包」明细写到 <csv>.late.csv（列：worker,seq,late_us）。没有迟到就不建文件。

    late: [(worker, seq, 迟到时长 us), ...]
    用途：想核对"某几个空 RTT 的包到底回来没有"时，直接在这份文件里按 seq 找。
    """
    if not late:
        return
    path = out_path + ".late.csv"
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["worker", "seq", "late_us"])
            w.writerows(sorted(late))
        print(f"  迟到明细      : {os.path.abspath(path)}")
    except OSError as e:
        print(f"  迟到明细写入失败: {e}")


def _report_echo(args, res_list, errs):
    """把一条或多条流的结果合并成一份报告 + 一份 CSV。"""
    res_list = sorted(res_list, key=lambda r: r["wid"])
    groups = [r["records"] for r in res_list]
    st = analyze_multi(groups)
    # 各流的「迟到回包」合并。多流时 seq 是各流内部编号，所以 late_all 带 worker 列；
    # 报告只数数量和最大值，故 st["late"] 里按 (seq, 时长) 存。
    late_all = [(r["wid"], q, u) for i, r in enumerate(res_list) for q, u in r.get("late", [])]
    st["late"] = [(q, u) for _, q, u in late_all]
    dur = max(r["dur"] for r in res_list)
    jobs = len(res_list)
    sock_err = sum(r["sock_err"] for r in res_list)
    firsts = [r["sock_err_first"] for r in res_list if r["sock_err_first"]]
    snd_fail = sum(r.get("snd_fail", 0) for r in res_list)
    snd_firsts = [r.get("snd_fail_first", "") for r in res_list if r.get("snd_fail_first")]
    if sock_err:
        err_note = (f"  socket 错误   : {sock_err} 次   首次 {firsts[0]}"
                    f"\n                  ← 有 socket 错误说明回显曾经中断。"
                    f"尾部那段丢包很可能是服务端退出/被杀，"
                    f"需核对服务端、链路及端口计数，不能单独归因")
    else:
        err_note = "  socket 错误   : 0 次"
    if snd_fail:
        err_note += (f"\n  本地发送失败  : {snd_fail} 次   首次 {snd_firsts[0] if snd_firsts else '-'}"
                     f"\n                  ← 这些包**没离开本机**（拔线 / 无路由 / 缓冲满），"
                     f"已按丢包计入。")
        if st["sent"] and snd_fail / st["sent"] > 0.5:
            err_note += (f"\n                     占比 >50%：先别读丢包率 —— "
                         f"检查 --bind 的源 IP 是否属于本机、网段是否正确、网线是否插好。")
    else:
        err_note += "\n  本地发送失败  : 0 次"
    extra = (f"对端 {args.host}:{args.port}；设定间隔 {args.interval*1000:g} ms/流\n"
             f"超时门槛 {args.timeout*1000:g} ms（按应用读取时刻判定，包含端点调度）\n"
             f"源地址 {args.bind or '默认路由，请确认有线出口'}\n" + err_note)
    st["worker_ids"] = [r["wid"] for r in res_list]
    if errs:
        extra += "\n  失败的流      : " + "；".join(f"流{w} {e}" for w, e in errs)
    title = "时延 / 丢包 / 链路中断 报告"
    if jobs > 1:
        title += f"（{jobs} 条并发流，合并统计）"
    print_stats(st, title, args.frame, dur, interval_s=None, extra=extra,
                want_pps=(jobs / args.interval) if args.interval else None,
                snd_fail=snd_fail)

    for r in res_list:
        print(f"  流{r['wid']} 收尾观察 {r['observation_s']:.3f}s；实际探测发送时间边界：")
        for start, count, lower, upper, tail in r["outage_bounds"][:10]:
            bound = f"{lower:.3f}~{upper:.3f} ms" if upper is not None else f">= {lower:.3f} ms（边界不完整）"
            print(f"    seq {start} 连续失败 {count}：{bound}" + ("，未观察到按时恢复" if tail else ""))
    rows = []
    for i, r in enumerate(res_list):
        late_map = dict(r["late"])
        for q, v in r["csv_rows"]:
            rows.append((r["wid"], q, v, r["send_s"][q-1], r["status"][q-1], late_map.get(q, "")))
    out_path = _write_csv(args.out, rows)
    _write_late_csv(out_path, late_all)
    if jobs > 1:
        print("  提示：多核的数字要和单流对照着看 —— 只有「单流已打不满、合计也没涨」")
        print("        时应结合 CPU、网卡与对端计数定位瓶颈。")


def cmd_echo(args):
    if args.jobs > 1:
        return _echo_multi(args)
    _report_echo(args, [_echo_core(_echo_cfg(args, 0))], [])


def _echo_multi(args):
    """多核并发：每个 worker 一条独立流（独立 socket / 独立序号空间 / 独立源端口）。

    为什么用进程而不是线程：Python 的发包循环（打包 + 计数）一直持着 GIL，
    只有 sendto 那一瞬间会释放，多线程在 CPython 里根本无法并行吃满多核。
    想真正用上每个核，只有多进程一条路。
    """
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    # flush=True 必须加：父进程的这几行若不立即刷出，会被子进程的输出挤到后面，
    # 屏幕上看起来像"先跑完了才打印启动信息"（输出重定向到文件时尤其明显）。
    print(f"[{now_str()}] 多核并发模式：{args.jobs} 条流，每进程占 1 核", flush=True)
    print(f"             每条流 {1 / args.interval:,.0f} pps，合计目标 ≈ "
          f"{args.jobs / args.interval:,.0f} pps", flush=True)
    print("             接收端能力可能限制测试，需直连基线与端口计数定位",
          flush=True)
    procs = [ctx.Process(target=_echo_worker, args=(_echo_cfg(args, i), q))
             for i in range(args.jobs)]
    for p in procs:
        p.start()
    res_list, errs = [], []
    try:
        for wid, res, err in _collect_workers(procs, q):
            if err:
                errs.append((wid, err))
            else:
                res["wid"] = wid
                res_list.append(res)
    except KeyboardInterrupt:
        print("\n收到中断，正在停止各条流…")
        for p in procs:
            p.terminate()
    for p in procs:
        p.join()
    if not res_list:
        print("  所有流都没跑成，未生成报告：")
        for wid, err in errs:
            print(f"     流{wid} 异常: {err}")
        raise ValueError("所有 echo 流均失败，未生成有效报告")
    _report_echo(args, res_list, errs)
    if errs:
        raise ValueError("部分 echo 流失败，报告仅包含成功返回的流")


# ------------------------------------------------------------------- flood
def _flood_core(cfg, prefix=""):
    """单向打流内核。单进程与多进程（-j）共用。

    cfg 键：host port frame bandwidth duration warmup bind wid
    返回：{seq, sent_bytes, dur}
    """
    session = new_session()
    payload = payload_for_frame(cfg["frame"])
    filler = b"\x00" * (payload - HDR_LEN)
    addr = (cfg["host"], cfg["port"])
    s, addr = _open_sender(cfg)

    try:
        bits = frame_bits(cfg["frame"])
        interval_s = (bits / (cfg["bandwidth"] * 1e6)) if cfg["bandwidth"] else 0.0

        print(f"{prefix}[{now_str()}] 单向打流 → {cfg['host']}:{cfg['port']}"
              + (f"  源地址 {cfg['bind']}" if cfg["bind"] else ""))
        print(f"{prefix}             帧长 {cfg['frame']} B | 目标速率 "
              f"{cfg['bandwidth']:.0f} Mbps | 时长 {cfg['duration']:.0f} s")
        print(f"{prefix}             结束时请核对【接收端】唯一包数与结束总数")

        # 预热：与 echo 同理，`flood` 是单向的，只需 PC-A 这侧建立 ARP。
        # 没有回包可依据，所以用固定时长（20 ms 一个探测包，节奏放慢少污染服务端计数）。
        if cfg["warmup"] > 0:
            print(f"{prefix}[{now_str()}] 预热 {cfg['warmup']:.1f} s（建立 ARP，不计入统计）…")
            wend = time.perf_counter_ns() + int(cfg["warmup"] * 1e9)
            wseq = 0
            while time.perf_counter_ns() < wend:
                wseq += 1
                try:
                    s.sendto(HDR.pack(TAG_WARM, wseq, session, time.perf_counter_ns()) + filler, addr)
                except OSError:
                    pass
                time.sleep(0.02)

        t0 = time.perf_counter()
        seq = 0
        sent_bytes = 0
        # 本地发送失败计数（拔线 / 无路由 / 发送缓冲满）。这里与 echo 的处理**正好相反**：
        # echo 失败要记一条丢包（它的丢包在本端算，不记就看不见断流）；
        # flood 失败**绝不能推进 seq** —— 接收端是用 (最大序号 − 收包数) 估丢包的，
        # seq 一旦在失败时前进，接收端就会看到一段序号缺口，把**根本没上线的包
        # 算成交换机丢包**。探针实测：2.3 万个包里有 4,700 个没发出去，
        # 工具却自报 23,482 个 —— 那 4,700 个会变成凭空捏造的丢包率。
        snd_fail = 0
        snd_fail_first = ""
        snd_fail_shown = 0
        SND_FAIL_SHOW = 3
        next_send = time.perf_counter_ns()
        last_tick = t0
        last_seq = 0
        last_bytes = 0
        # 结束条件不能每包查一次：循环里调 time.perf_counter() 本身就是可观开销，
        # 高 pps 打流时它会直接吃掉一截发包能力。改成每 64 个包查一次，
        # 结束时刻误差最大 64 个包（50 万 pps 下约 0.13 ms），对压测无影响；
        # 目标速率很低时退回每包检查（那时这点开销无所谓）。
        deadline = time.perf_counter() + cfg["duration"]
        check_mask = 0x3F if interval_s < 0.005 else 0
        tick = 0
        try:
            while True:
                tick += 1
                if (tick & check_mask) == 0 and time.perf_counter() >= deadline:
                    break
                now_ns = time.perf_counter_ns()
                if now_ns >= next_send:
                    # seq 只在**发送成功**之后才前进：接收端按 (最大序号 − 收包数)
                    # 推算丢包，序号里不能出现"从未上线的空洞"，否则会凭空造出丢包。
                    nxt_seq = seq + 1
                    if nxt_seq > 0xffffffff:
                        raise ValueError("序号空间耗尽，请分轮测试")
                    pkt = HDR.pack(TAG_FLOOD, nxt_seq, session, now_ns) + filler
                    try:
                        s.sendto(pkt, addr)
                        seq = nxt_seq
                        sent_bytes += len(pkt)
                    except OSError as e:
                        snd_fail += 1
                        if not snd_fail_first:
                            snd_fail_first = f"{type(e).__name__} errno={e.errno}"
                        if snd_fail_shown < SND_FAIL_SHOW:
                            snd_fail_shown += 1
                            extra_note = ("" if snd_fail_shown < SND_FAIL_SHOW
                                          else "  (后续同类失败不再逐条打印)")
                            print(f"{prefix}  发送失败（本地）: {snd_fail_first}{extra_note}")
                            if snd_fail_shown == 1:
                                print(f"{prefix}    ← 包没离开本机，因此**不推进序号**："
                                      f"否则接收端会把它们当成交换机的丢包。")
                    if interval_s > 0:
                        next_send += int(interval_s * 1e9)
                        if next_send < now_ns:
                            next_send = now_ns + int(interval_s * 1e9)
                    else:
                        next_send = now_ns
                if time.perf_counter() - last_tick >= 1.0:
                    dt = time.perf_counter() - last_tick
                    fail_note = f" | 本地失败 {snd_fail:,}" if snd_fail else ""
                    print(f"{prefix}[{now_str()}] 已运行 {time.perf_counter()-t0:.1f}/{cfg['duration']:g}s | 已发 {seq:>12,} 包 | "
                          f"{(seq - last_seq) / dt:>10,.0f} pps | "
                          f"{(sent_bytes - last_bytes) * 8 / dt / 1e6:>8.2f} Mbps"
                          f"{fail_note}")
                    last_tick = time.perf_counter()
                    last_seq = seq
                    last_bytes = sent_bytes
        except KeyboardInterrupt:
            print(f"{prefix}\n收到中断…")

        dur = time.perf_counter() - t0
        confirmed = _finish_stream(s, addr, TAG_FLOOD, session, seq)
        return {"wid": cfg["wid"], "session": session, "confirmed": confirmed, "seq": seq, "sent_bytes": sent_bytes, "dur": dur,
                "snd_fail": snd_fail, "snd_fail_first": snd_fail_first}
    finally:
        s.close()


def _flood_hosts(args):
    """--host 支持逗号分隔的多个目标：一条命令把几条流分给几个对端（铺端口/造背景流量）。"""
    return [h.strip() for h in str(args.host).split(",") if h.strip()] or [args.host]


def _flood_cfg(args, wid=0, hosts=None):
    hs = hosts or _flood_hosts(args)
    return {
        "host": hs[wid % len(hs)], "port": args.port, "frame": args.frame,
        "bandwidth": args.bandwidth, "duration": args.duration,
        "warmup": args.warmup, "bind": getattr(args, "bind", "") or "", "wid": wid,
        "log_path": getattr(args, "_log_path", None),
    }


def _flood_worker(cfg, q):
    try:
        with _log_output(cfg.get("log_path"), append=True):
            res = _flood_core(cfg, prefix=f"[流{cfg['wid']}] ")
        res["host"] = cfg["host"]
        q.put((cfg["wid"], res, None))
    except BaseException as e:
        q.put((cfg["wid"], None, f"{type(e).__name__}: {e}"))


def cmd_flood(args):
    if args.jobs > 1:
        return _flood_multi(args)
    _report_flood(args, [_flood_core(_flood_cfg(args, 0))], [])


def _flood_multi(args):
    """多核并发打流：每进程一条流，合计速率 = 每条流的目标速率 × 流数。

    注意：多流**不会**让你的出口带宽变大。千兆口还是 ~940 Mbps 上限，
    多流的意义只在于：单核 CPU 先顶不住时（小包高 pps 场景），把 CPU 分摊掉。
    """
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    hosts = _flood_hosts(args)
    print(f"[{now_str()}] 多核并发模式：{args.jobs} 条流，每进程占 1 核", flush=True)
    if len(hosts) > 1:
        print(f"             多目标：{len(hosts)} 个对端轮流分配 —— "
              f"{', '.join(hosts)}", flush=True)
    if args.bandwidth:
        print(f"             每条流 {args.bandwidth:.0f} Mbps，"
              f"合计目标 {args.bandwidth * args.jobs:.0f} Mbps —— 别超过网口带宽",
              flush=True)
    else:
        print("             全速模式（--bandwidth 0）：每核各自尽快发，合计看网口和接收端",
              flush=True)
    procs = [ctx.Process(target=_flood_worker, args=(_flood_cfg(args, i, hosts), q))
             for i in range(args.jobs)]
    for p in procs:
        p.start()
    res_list, errs = [], []
    try:
        for wid, res, err in _collect_workers(procs, q):
            if err:
                errs.append((wid, err))
            else:
                res["wid"] = wid
                res_list.append(res)
    except KeyboardInterrupt:
        print("\n收到中断，正在停止各条流…")
        for p in procs:
            p.terminate()
    for p in procs:
        p.join()
    if not res_list:
        print("  所有流都没跑成：")
        for wid, err in errs:
            print(f"     流{wid} 异常: {err}")
        raise ValueError("所有 flood 流均失败，未生成有效报告")
    _report_flood(args, res_list, errs)
    if errs:
        raise ValueError("部分 flood 流失败，报告仅包含成功返回的流")


def _report_flood(args, res_list, errs):
    res_list = sorted(res_list, key=lambda r: r["wid"])
    dur = max(r["dur"] for r in res_list)
    total_pkts = sum(r["seq"] for r in res_list)
    total_bytes = sum(r["sent_bytes"] for r in res_list)
    snd_fail = sum(r.get("snd_fail", 0) for r in res_list)
    fail_first = next((r.get("snd_fail_first") for r in res_list
                       if r.get("snd_fail_first")), "")
    jobs = len(res_list)
    print()
    print("=" * 62)
    print("  发送端汇总" + (f"（{jobs} 条并发流）" if jobs > 1 else ""))
    print("=" * 62)
    print(f"  发送总包数    : {total_pkts:,}")
    print(f"  发送总字节    : {total_bytes:,}")
    if snd_fail:
        tried = total_pkts + snd_fail
        print(f"  本地发送失败  : {snd_fail:,} 次（占尝试 {snd_fail / tried * 100:.1f}%）"
              f"   首次 {fail_first}")
        print("                  ← 这些包没离开本机（拔线 / 无路由 / 发送缓冲满）。")
        print("                    它们**不算交换机丢包**，也没有计入上面的发送总包数；")
        print("                    但占比高说明这一轮根本没打满，结论不可用 —— 先查")
        print("                    --bind 源 IP、网段、网线，再重测。")
    if dur > 0:
        got_pps = total_pkts / dur
        udp_mbps = total_bytes * 8 / dur / 1e6
        got_mbps = total_pkts * frame_bits(args.frame) / dur / 1e6
        print(f"  实测发送速率  : {got_pps:,.0f} pps / UDP {udp_mbps:.2f} Mbps / 估算线速 {got_mbps:.2f} Mbps")
        want = args.bandwidth * jobs
        if want:
            print(f"  设定目标速率  : {want:,.0f} Mbps   达成 {got_mbps / want * 100:.1f}%")
            if got_mbps / want < 0.8:
                print("                  ← 达成率偏低：请检查发送调度、CPU、网卡与链路限制，")
                print("                    发送端速率无法单独定位瓶颈。")
    if jobs > 1:
        print("  各条流        :")
        for i, r in enumerate(res_list):
            print(f"     流{r['wid']} → {r.get('host', args.host)}:{args.port}"
                  f"   发 {r['seq']:,} 包")
    if errs:
        print(f"  失败的流      : " + "；".join(f"流{w} {e}" for w, e in errs))
    print("=" * 62)
    print("  ※ 发包成功仅表示内核接受，不代表实际到达。请核对接收端。")
    for r in res_list:
        print(f"  流{r['wid']} session={r['session']:08x} 结束总数确认：{r['confirmed']}")
    if any(not r["confirmed"] for r in res_list):
        print("  结束总数未确认：接收端尾部丢失未知，不能报告完整丢包率。")


# ----------------------------------------------------------------- compare
def load_csv(path):
    groups = {}
    seen = set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for line, row in enumerate(csv.DictReader(f), 2):
            try:
                seq = int(row["seq"])
                worker = int(row.get("worker") or 0)
                raw = (row.get("rtt_us") or "").strip()
                rtt = float(raw) if raw else None
                if seq < 1 or worker < 0 or (rtt is not None and (not math.isfinite(rtt) or rtt < 0)):
                    raise ValueError("invalid value")
                if (worker, seq) in seen:
                    raise ValueError("duplicate worker/sequence")
            except (KeyError, ValueError) as exc:
                raise ValueError(f"{path} 第{line}行无效：{exc}") from exc
            seen.add((worker, seq))
            groups.setdefault(worker, []).append((seq, rtt))
    ordered = [sorted(groups[k]) for k in sorted(groups)]
    for records in ordered:
        if [seq for seq, _ in records] != list(range(1, len(records)+1)):
            raise ValueError(f"{path} 序号不连续，明细不完整，拒绝计算丢包率")
    if not ordered:
        raise ValueError(f"{path} 无测试数据")
    return ordered


def cmd_compare(args):
    ra, rb = load_csv(args.a), load_csv(args.b)
    sa, sb = analyze_multi(ra), analyze_multi(rb)
    na = os.path.basename(args.a)
    nb = os.path.basename(args.b)

    rows = [
        ("发送包数", f"{sa['sent']:,}", f"{sb['sent']:,}", ""),
        ("丢包数", f"{sa['lost']:,}", f"{sb['lost']:,}", "越低越好"),
        ("丢包率 %", f"{sa['loss_pct']:.4f}", f"{sb['loss_pct']:.4f}", "目标 0"),
        ("RTT 最小 µs", f"{sa['rtt_min']:.0f}", f"{sb['rtt_min']:.0f}", "端到端测量，含端点开销"),
        ("RTT 平均 µs", f"{sa['rtt_avg']:.0f}", f"{sb['rtt_avg']:.0f}", ""),
        ("RTT 最大 µs", f"{sa['rtt_max']:.0f}", f"{sb['rtt_max']:.0f}", "尖峰也可能来自端点调度"),
        ("RTT P99 µs", f"{sa['rtt_p99']:.0f}", f"{sb['rtt_p99']:.0f}", ""),
        ("RTT 平滑变化 µs", f"{sa['jitter']:.0f}", f"{sb['jitter']:.0f}", "越小越好"),
        ("相邻最大偏差 µs", f"{sa['max_dev']:.0f}", f"{sb['max_dev']:.0f}", "越小越好"),
        ("连续探测失败段", f"{len(sa['outages'])}", f"{len(sb['outages'])}", "不能单独证明链路中断"),
        ("最长连续丢包", f"{sa['max_run']}", f"{sb['max_run']}", "应为 0"),
    ]

    w = 34
    print()
    print("=" * 78)
    print("  对比报告")
    print("=" * 78)
    print(f"  {'指标':<{w}}{na[:18]:>20}{nb[:18]:>20}")
    print("  " + "-" * 74)
    for name, va, vb, note in rows:
        print(f"  {name:<{w}}{va:>20}{vb:>20}   {note}")
    print("=" * 78)
    print("  A = 原装/基准交换机，B = 待评估交换机。相对差异比绝对值更有意义。")


def cmd_gaps(args):
    groups = load_csv(args.csv)
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    workers = sorted({int(row.get("worker") or 0) for row in rows})
    timings = {(int(row.get("worker") or 0), int(row["seq"])): float(row["send_s"])
               for row in rows if row.get("send_s")}
    for wid, records in zip(workers, groups):
        stat = analyze(records)
        print(f"流{wid}：尝试 {stat['sent']:,}；超时/本地失败 {stat['lost']:,}")
        for start, count in stat['runs']:
            keys = [(wid, start), (wid, start + count - 1)]
            if all(key in timings for key in keys):
                hint = f"实际发送 {timings[keys[0]]:.6f}~{timings[keys[1]]:.6f}s"
            else:
                hint = "估计" + time_hint(start, args.interval, args.pps or None)
            print(f"  seq {start}~{start+count-1}：{hint}")
    print("空 RTT 包括本地失败、迟到及观察结束未回包；新 CSV 可用 status 列区分。")


def _bench_worker(cfg, q):
    """纯发包基准：不解析、不统计、不打印。

    分别数「尝试次数」和「成功次数」—— 原来只数尝试次数，却把结果叫"sendto 成功
    次数"，于是链路不通时（每次都抛 OSError）依然能报出很高的 pps，看起来像
    "本机很强"，实际一个包都没出去。两者都报，差额一目了然。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
        if cfg["bind"]:
            s.bind((cfg["bind"], 0))
        pkt = b"\x00" * payload_for_frame(cfg["frame"])
        addr = (cfg["host"], cfg["port"])
        sendto = s.sendto
        n_try = 0
        n_ok = 0
        t0 = time.perf_counter()
        end = t0 + cfg["duration"]
        while time.perf_counter() < end:
            n_try += 1
            try:
                sendto(pkt, addr)
                n_ok += 1
            except OSError:
                pass
        q.put((cfg["wid"], n_ok, n_try, time.perf_counter() - t0, None))
    except BaseException as e:
        q.put((cfg["wid"], 0, 0, 0.0, f"{type(e).__name__}: {e}"))


def cmd_bench(args):
    """量这台机器「每个核能发多少 pps、加到几条流就不再涨」。

    压测前先跑一次，才知道 -j 该给几条：给少了打不满、给多了白占 CPU
    还会把对端压垮（对端垮掉产生的丢包会被误记成交换机丢包）。
    """
    ctx = mp.get_context("spawn")
    jobs_list = [int(x) for x in str(args.jobs).replace("，", ",").split(",") if x.strip()]
    jobs_list = [j for j in jobs_list if j >= 1] or [1]
    print(f"[{now_str()}] 发包能力基准：目标 {args.host}:{args.port}"
          f" | 帧长 {args.frame} B | 每轮 {args.duration:.0f} s", flush=True)
    print("             目标是本机/不存在的地址 → 测的是「CPU + 协议栈」上限；")
    print("             指向真实对端 → 仍然只是本机 sendto 提交能力，接收吞吐需接收端对账。")
    sys.stdout.flush()
    base = None
    rows = []
    for jobs in jobs_list:
        q = ctx.Queue()
        procs = [ctx.Process(name="bench", target=_bench_worker,
                             args=({"host": args.host, "port": args.port,
                                    "frame": args.frame, "duration": args.duration,
                                    "bind": args.bind or "", "wid": i}, q))
                 for i in range(jobs)]
        for p in procs:
            p.start()
        tot = 0
        tot_try = 0
        dt = 0.0
        errs = []
        try:
            for wid, n, n_try, d, err in _collect_workers(procs, q):
                tot += n
                tot_try += n_try
                dt = max(dt, d)
                if err:
                    errs.append(err)
        except KeyboardInterrupt:
            print("\n收到中断，正在停止…")
            for p in procs:
                p.terminate()
        for p in procs:
            p.join()
        if dt <= 0:
            print(f"  jobs={jobs:>2} : 失败 {errs[:1]}")
            continue
        pps = tot / dt
        fails = tot_try - tot
        if base is None:
            base = pps
        rows.append((jobs, pps))
        fail_note = (f"  | sendto 失败 {fails:,}（{fails / max(tot_try, 1) * 100:.0f}%，"
                     f"这些包没上线）" if fails else "")
        print(f"  jobs={jobs:>2} : 合计 {pps:>11,.0f} pps （相对单进程 x{pps / max(base, 1e-12):.2f}）"
              f" | 平均每核 {tot / jobs / dt:>10,.0f} pps{fail_note}")
    if rows:
        best_jobs, best = max(rows, key=lambda r: r[1])
        line = 1e9 / frame_bits(args.frame)
        print("-" * 62)
        print(f"  最高：{best_jobs} 条流 = {best:,.0f} pps"
              f"（千兆线速下 {args.frame} B 帧极限 {line:,.0f} pps，"
              f"本机达到 {best / line * 100:.0f}%）")
        if len(rows) > 1 and rows[-1][1] < best * 1.15:
            print("  结论：再加流已经不涨了 —— 请检查 CPU、协议栈、网卡与对端限制，")
            print("        压测时 -j 给 2~4 条即可，堆更多只会让对端更快被压垮。")
        else:
            print("  结论：还在随流数增长 —— 可尝试增加流数，同时观察接收端与链路计数。")
    print("=" * 62)


# -------------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(
        description="零仪表版交换机测试工具（丢包率 / 时延 / 抖动 / 环网自愈 / 吞吐）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python switch_test.py server --port 5001 --duration 600\n"
               "  python switch_test.py echo --host 192.168.1.20 --frame 64 --interval 0.002 --duration 300\n"
               "  python switch_test.py flood --host 192.168.1.20 --frame 1518 --bandwidth 100 --duration 60\n"
               "  python switch_test.py compare --a orig.csv --b new.csv\n"
               "  python switch_test.py gaps --csv 20261007_101530_report.csv --interval 0.001\n")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("server", help="接收端：回显 + 统计")
    ps.add_argument("--bind", default="0.0.0.0", help="监听地址，全部网卡=0.0.0.0")
    ps.add_argument("--port", type=int, default=5001, help="监听 UDP 端口，两端必须一致")
    ps.add_argument("--duration", type=float, default=0, help="运行秒数，0=不限")
    ps.add_argument("--fast", action="store_true",
                    help="极简回显模式：不做统计，只为压低响应延迟，测时延/抖动时用")
    ps.set_defaults(func=cmd_server)

    pe = sub.add_parser("echo", help="发送端：时延 / 抖动 / 丢包 / 中断检测")
    pe.add_argument("--host", required=True, help="服务端 IP（必需）")
    pe.add_argument("--port", type=int, default=5001, help="服务端 UDP 端口，两端必须一致")
    pe.add_argument("--frame", type=int, default=64, help="以太网帧长 64~1518")
    pe.add_argument("--interval", type=float, default=0.01, help="发包间隔秒，0.001=1ms")
    pe.add_argument("--duration", type=float, default=60, help="正式统计时长秒")
    pe.add_argument("--timeout", type=float, default=0.05, help="单包超时秒")
    pe.add_argument("--drain", type=float, default=0.2,
                    help="停止发包后继续观察迟到回包的秒数，至少等完最后一包超时")
    pe.add_argument("--warmup", type=float, default=1.0,
                    help="正式计时前的邻居探测秒数（建立 ARP，不计入统计），0=关闭")
    pe.add_argument("--out", default="", help="CSV 输出路径，留空自动命名")
    pe.add_argument("--bind", default="",
                    help="发送端源 IP，如 192.168.1.10。锁定出口网卡，"
                         "避免本机还有 Wi-Fi/VPN 时被默认路由抢走")
    pe.add_argument("-j", "--jobs", type=int, default=1,
                    help="并发流数（多进程，每进程占 1 核；1=单进程）。"
                         "小包高 pps 场景才有用，先跑 bench 看该给几")
    pe.set_defaults(func=cmd_echo)

    pf = sub.add_parser("flood", help="发送端：单向吞吐压测")
    pf.add_argument("--host", required=True,
                    help="服务端 IP（必需）。可用逗号分隔多个目标，如 "
                         "192.168.1.20,192.168.1.21 —— 配合 -j 把几条流分给几个对端")
    pf.add_argument("--port", type=int, default=5001, help="服务端 UDP 端口，两端必须一致")
    pf.add_argument("--frame", type=int, default=1518, help="以太网帧长 64~1518")
    pf.add_argument("--bandwidth", type=float, default=100, help="Mbps，0=全速")
    pf.add_argument("--duration", type=float, default=60, help="打流时长秒")
    pf.add_argument("--warmup", type=float, default=1.0,
                    help="正式打流前的预热秒数（建立 ARP，不计入统计），0=关闭")
    pf.add_argument("--bind", default="",
                    help="发送端源 IP，锁定出口网卡（不去绑定就可能走 Wi-Fi/VPN）")
    pf.add_argument("-j", "--jobs", type=int, default=1,
                    help="并发流数（多进程，每进程占 1 核；1=单进程）。"
                         "注意多流不会让网口带宽变大，只在单核吃不下时有用")
    pf.set_defaults(func=cmd_flood)

    pc = sub.add_parser("compare", help="对比两份 CSV 报告")
    pc.add_argument("--a", required=True, help="基准版 CSV（原交换机）")
    pc.add_argument("--b", required=True, help="对比版 CSV（国产交换机）")
    pc.set_defaults(func=cmd_compare)

    pg = sub.add_parser("gaps", help="从 CSV 明细定位到底丢了哪些包、丢在第几秒")
    pg.add_argument("--csv", required=True, help="echo 生成的 CSV 明细（worker,seq,rtt_us）")
    pg.add_argument("--pps", type=float, default=0,
                    help="那轮的**实际发包速率**（报告里「实际发包速率」那一行）。"
                         "换算时刻最准的方式 —— 软件常打不满设定值，只给 --interval 会偏小")
    pg.add_argument("--interval", type=float, default=0,
                    help="那轮的发包间隔秒（如 0.001）。没有 --pps 时退而用它换算")
    pg.add_argument("--show", type=int, default=20, help="每段最多列出多少个包号")
    pg.set_defaults(func=cmd_gaps)

    pbn = sub.add_parser("bench", help="量本机每核发包能力，决定 -j 该给几条流")
    pbn.add_argument("--host", default="127.0.0.1",
                     help="目标 IP。用本机/同网段不存在的地址 → 测 CPU+协议栈上限；"
                          "指向真实对端 → 测链路真实可用吞吐")
    pbn.add_argument("--port", type=int, default=5001, help="目标 UDP 端口")
    pbn.add_argument("--frame", type=int, default=64, help="以太网帧长 64~1518")
    pbn.add_argument("--jobs", default="1,2,4", help="要对比的流数，逗号分隔，如 1,2,4,8")
    pbn.add_argument("--duration", type=float, default=3, help="每个流数测多少秒")
    pbn.add_argument("--bind", default="", help="发送端源 IP")
    pbn.set_defaults(func=cmd_bench)

    # 让每个子命令的 --help 自动显示参数默认值。默认的 HelpFormatter 不显示，
    # 于是"这个参数有默认值、可以不写"这件事在帮助里根本看不见。
    # 必须在全部 add_parser 之后执行，否则 sub.choices 还是空的。
    for _sp in sub.choices.values():
        _sp.formatter_class = argparse.ArgumentDefaultsHelpFormatter
        _logs = _sp.add_mutually_exclusive_group()
        _logs.add_argument("--log", default=None, help="日志路径；默认自动保存到 out/logs，同时显示进度")
        _logs.add_argument("--no-log", action="store_true", help="仅显示控制台输出，不创建日志")

    args = p.parse_args()
    args._log_path = None if args.no_log else (str(Path(args.log).resolve()) if args.log else _default_log_path(args.cmd))
    try:
        with _log_output(args._log_path):
            if args._log_path:
                print(f"日志自动保存：{args._log_path}", flush=True)
                print(f"启动时间：{datetime.now().isoformat(timespec='seconds')}")
                print(f"运行参数：{vars(args)}")
            _run_checked(args, p)
    except FileExistsError:
        p.exit(1, "日志已存在，请更换 --log 文件名；已有日志未覆盖。\n")
    except OSError as exc:
        p.exit(1, f"日志或测试启动失败：{exc}\n")


def _run_checked(args, p):
    for key in ("duration", "warmup", "timeout", "interval", "bandwidth", "pps", "drain"):
        if hasattr(args, key):
            value = getattr(args, key)
            if not math.isfinite(value) or value < 0:
                p.error(f"--{key} 必须是有限非负数")
    if hasattr(args, "port") and not 1 <= args.port <= 65535:
        p.error("--port 必须在 1~65535 之间")
    if args.cmd in ("echo", "flood", "bench") and args.duration <= 0:
        p.error("发送测试 --duration 必须大于 0")
    if args.cmd == "echo" and args.timeout <= 0:
        p.error("--timeout 必须大于 0")
    if args.cmd == "bench":
        try:
            values = [int(x) for x in args.jobs.replace("，", ",").split(",")]
            if not values or any(not 1 <= x <= MAX_JOBS for x in values):
                raise ValueError()
        except ValueError:
            p.error(f"bench --jobs 需为 1~{MAX_JOBS} 的整数列表")
    if args.cmd in ("echo", "flood", "bench") and not (64 <= args.frame <= 1518):
        print("帧长应在 64~1518 之间")
        sys.exit(2)
    if args.cmd in ("echo", "flood"):
        if args.cmd == "echo" and "," in str(args.host):
            print("echo 只支持单个对端（多条流打的是同一个对端，用于压 pps）。")
            print("要往多个对端铺流量，请用：flood --host a,b,c -j N")
            sys.exit(2)
        if args.jobs < 1:
            print("-j/--jobs 至少为 1")
            sys.exit(2)
        if args.jobs > MAX_JOBS:
            print(f"-j/--jobs 上限 {MAX_JOBS}：再多只会让几条流互相抢 CPU，"
                  f"并更快把接收端压垮。按 bench 的结论给 2~4 通常就够。")
            sys.exit(2)
    # 参数合法性：这三个以前都没有校验，后果分别是崩溃和"跑了个寂寞"。
    if args.cmd == "echo" and args.interval <= 0:
        print("--interval 必须大于 0（0.001 = 1 ms；0.0002 ≈ 5 kpps）")
        sys.exit(2)
    if args.cmd == "flood" and args.bandwidth < 0:
        print("--bandwidth 不能为负（0 = 全速）")
        sys.exit(2)
    if args.cmd == "flood" and args.duration <= 0:
        print("--duration 必须大于 0（--duration 0 会让打流瞬间结束、报告全空）")
        sys.exit(2)
    if args.cmd in ("echo", "flood", "bench") and args.bind:
        pass   # 源地址绑定由内核校验，绑错了会在各 worker 里逐条报出来
    try:
        args.func(args)
    except (OSError, ValueError) as exc:
        p.exit(1, f"测试失败：{exc}\n")


if __name__ == "__main__":
    main()
