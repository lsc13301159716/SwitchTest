#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
零仪表版交换机测试工具  v1.0
====================================================================
不需要任何专业测试仪，两台装了 Python 的电脑即可完成：
    丢包率 / 往返时延(RTT) / 时延抖动 / 链路中断时长(环网自愈) / 单向吞吐

典型用法（两台 PC 分别接在被测交换机两端）

  【接收端 PC-B】
      python switch_test.py server --port 5001 --duration 600

  【发送端 PC-A】
      # 1) 时延 + 丢包 + 中断检测（环网自愈就用这个，拔线时跑）
      python switch_test.py echo --host <PC-B的IP> --port 5001 \
             --frame 64 --interval 0.002 --duration 300

      # 2) 单向吞吐压测（找最大无丢包速率）
      python switch_test.py flood --host <PC-B的IP> --port 5001 \
             --frame 1518 --bandwidth 100 --duration 60

      # 3) 两份报告对比（国产机 vs 原装机）
      python switch_test.py compare --a orig.csv --b 国产.csv

帧长说明：--frame 填的是**以太网帧长**，与 RFC 2544 一致
      64 → UDP 载荷 18 B（小包，最考验包转发率）
      512 → UDP 载荷 466 B
      1518 → UDP 载荷 1472 B（满载）

重要提醒
  * 软件打流受 CPU 和网卡限制，达不到线速，微秒级时延测不出来。
    但仪器缺失时用「同一套软硬件测两台不同交换机」的相对对比，
    结论依然有效、可写进验收报告。
  * 高频发包需要管理员/root？不需要，本工具走普通 UDP，无权限要求。
  * 首次运行请放行防火墙，或临时关闭被测网段的防火墙。
====================================================================
"""

from __future__ import annotations

import argparse
import csv
import os
import socket
import struct
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 报文头：tag(1) + seq(4) + 发送时刻纳秒(8) = 13 字节
HDR = struct.Struct("!cIQ")
HDR_LEN = HDR.size
TAG_ECHO = b"S"   # 请求回显
TAG_FLOOD = b"F"  # 单向打流，不回显

# 以太网帧 -> UDP 载荷 的扣除量
# 14(以太网头) + 4(FCS) + 20(IP) + 8(UDP) = 46
ETH_OVERHEAD = 46


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def payload_for_frame(frame_len: int) -> int:
    """把 RFC 2544 的以太网帧长换算成本工具要发的 UDP 载荷长度。"""
    return max(HDR_LEN, frame_len - ETH_OVERHEAD)


def frame_bits(frame_len: int) -> int:
    """一帧在线上占用的比特数（含 12B 帧间隙 + 8B 前导 = 20B ≈ 160bit）。"""
    return frame_len * 8 + 160


def analyze(records):
    """records: [(seq, rtt_us or None)]，返回统计字典。"""
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
        st["rtt_p99"] = s[min(len(s) - 1, int(len(s) * 0.99))]

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
    return st


def print_stats(st, title, frame_len, duration_s, interval_s=None, extra=""):
    lost_runs = st["outages"]
    print()
    print("=" * 62)
    print(f"  {title}")
    print("=" * 62)
    if extra:
        print(extra)
    print(f"  以太网帧长    : {frame_len} B  (UDP 载荷 {payload_for_frame(frame_len)} B)")
    print(f"  测试时长      : {duration_s:.1f} s")
    print(f"  发送包数      : {st['sent']:,}")
    print(f"  接收包数      : {st['recv']:,}")
    print(f"  丢包数        : {st['lost']:,}")
    print(f"  丢包率        : {st['loss_pct']:.4f} %")
    if st["recv"]:
        print(f"  RTT 最小      : {st['rtt_min']:.0f} µs")
        print(f"  RTT 平均      : {st['rtt_avg']:.0f} µs")
        print(f"  RTT 最大      : {st['rtt_max']:.0f} µs")
        print(f"  RTT P99       : {st['rtt_p99']:.0f} µs")
        print(f"  抖动(RFC3550) : {st['jitter']:.0f} µs")
        print(f"  相邻最大偏差  : {st['max_dev']:.0f} µs")
    if lost_runs:
        print(f"  链路中断次数  : {len(lost_runs)}   (连续丢包 >= 2 个才算一次中断)")
        tail = f"   ≈ {st['max_run'] * interval_s * 1000:.1f} ms 断流" if interval_s else ""
        print(f"  最长断流      : 连续丢 {st['max_run']} 个包{tail}")
        if st["max_run"] >= 2:
            print("                  ← 环网自愈时间直接读这个数")
        print(f"  孤立丢包      : {st['single_loss']} 次")
        for start, n in lost_runs[:10]:
            d = f" ≈ {n * interval_s * 1000:.1f} ms" if interval_s else ""
            print(f"      · seq {start} 起连续丢 {n} 个{d}")
        if len(lost_runs) > 10:
            print(f"      · … 另有 {len(lost_runs) - 10} 次中断")
    else:
        print("  链路中断次数  : 0   (全程无连续丢包，链路稳定)")
        print(f"  孤立丢包      : {st['single_loss']} 次")
    print("=" * 62)


def stamp_out(name):
    return name or f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_report.csv"


# ------------------------------------------------------------------ server
def _server_fast(args):
    """极简回显：只做 recvfrom + sendto，不解析头部、不统计、不打印。
    软件时延测量的瓶颈主要在服务端的用户态处理，这个模式能把基线压到最低。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    except OSError:
        pass
    s.bind((args.bind, args.port))
    # 超时只为了让 --duration 在没有流量时也能到期：recvfrom 一旦阻塞住，
    # while 里的时间判断就没有机会执行。有流量时几乎不触发，
    # 实测对 RTT 基线无可测影响（<20 µs，落在测量噪声内）。
    s.settimeout(1.0)
    t0 = time.time()
    print(f"[{now_str()}] 极简回显模式启动：{args.bind}:{args.port}"
          f"（{'限时 %.0fs' % args.duration if args.duration else '不限时'}）")
    print("             本模式不做统计，丢包与时延一律以【发送端】报告为准")
    n = 0
    try:
        while not (args.duration and time.time() - t0 >= args.duration):
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            # 只回显 echo 包，与普通模式语义一致。不加这个判断的话，
            # flood 灌进来的包也会被回显，单向吞吐测试会变成双向。
            if len(data) >= HDR_LEN and data[0:1] == TAG_ECHO:
                s.sendto(data[:HDR_LEN], addr)
                n += 1
    except KeyboardInterrupt:
        pass
    except OSError:
        pass
    print(f"[{now_str()}] 极简回显结束，共回显 {n:,} 个包")


def cmd_server(args):
    if args.fast:
        return _server_fast(args)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # 与极简模式保持一致：内核接收缓冲区不够大时，来不及取的包会被内核
    # 直接丢弃，而这份丢包会被误算到交换机头上。必须在 bind 之前设置。
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    except OSError:
        pass
    s.bind((args.bind, args.port))
    s.settimeout(0.2)

    print(f"[{now_str()}] 接收端已启动：{args.bind}:{args.port}")
    print("             等待发送端接入… Ctrl+C 结束")

    t0 = time.time()
    last_t = t0
    win_pkts = 0
    win_bytes = 0
    total_pkts = 0
    total_bytes = 0
    max_seq = 0
    echo_cnt = 0
    flood_cnt = 0

    try:
        while True:
            if args.duration and time.time() - t0 >= args.duration:
                break
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                data = None

            if data and len(data) >= HDR_LEN:
                tag, seq, ts = HDR.unpack(data[:HDR_LEN])
                if tag in (TAG_ECHO, TAG_FLOOD):
                    total_pkts += 1
                    win_pkts += 1
                    total_bytes += len(data)
                    win_bytes += len(data)
                    if seq > max_seq:
                        max_seq = seq
                    if tag == TAG_ECHO:
                        echo_cnt += 1
                        try:
                            s.sendto(data[:HDR_LEN], addr)
                        except OSError:
                            pass
                    else:
                        flood_cnt += 1

            now = time.time()
            if now - last_t >= 1.0:
                dt = now - last_t
                pps = win_pkts / dt
                mbps = win_bytes * 8 / dt / 1e6
                loss = 0.0
                if max_seq > 0:
                    loss = max(0.0, (max_seq - total_pkts) / max_seq * 100.0)
                print(
                    f"[{now_str()}] 用时 {now - t0:7.1f}s | "
                    f"收包 {total_pkts:>12,} | {pps:>10,.0f} pps | "
                    f"{mbps:>8.2f} Mbps | 估算丢包 {loss:.3f}%"
                )
                last_t = now
                win_pkts = 0
                win_bytes = 0
    except KeyboardInterrupt:
        print("\n收到中断，正在汇总…")

    dur = time.time() - t0
    print()
    print("=" * 62)
    print("  接收端汇总")
    print("=" * 62)
    print(f"  监听时长      : {dur:.1f} s")
    print(f"  收到总包数    : {total_pkts:,}")
    print(f"  收到总字节    : {total_bytes:,}")
    if dur > 0:
        print(f"  平均速率      : {total_pkts / dur:,.0f} pps / {total_bytes * 8 / dur / 1e6:.2f} Mbps")
    print(f"  回显请求数    : {echo_cnt:,}")
    print(f"  单向打流数    : {flood_cnt:,}")
    if max_seq:
        loss = max(0.0, (max_seq - total_pkts) / max_seq * 100.0)
        print(f"  估算丢包率    : {loss:.4f}%  (按最大序号 {max_seq:,} 推算)")
    print("=" * 62)
    print("  ※ 单向丢包率以接收端为准；时延/抖动请看发送端的报告")


# -------------------------------------------------------------------- echo
def cmd_echo(args):
    payload = payload_for_frame(args.frame)
    filler = b"\x00" * (payload - HDR_LEN)
    addr = (args.host, args.port)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)

    interval_ns = int(args.interval * 1e9)
    timeout_ns = int(max(args.timeout, args.interval * 5) * 1e9)

    print(f"[{now_str()}] 时延/丢包测试 → {args.host}:{args.port}")
    print(f"             帧长 {args.frame} B | 间隔 {args.interval * 1000:.2f} ms "
          f"| 时长 {args.duration} s")
    print("             拔线 / 断电 / 切电源时不要停，脚本会自动算出断流时长")

    # ---- 预热：先建立 ARP 邻居，避开"开头必丢一串包"的假丢包 ----
    # 机理（MS 官方文档 "Diagnose Packet Loss"）：出站包的目的 MAC 未解析时，
    # 本机内核直接把包丢弃，只发 ARP 请求；ARP 请求一旦需要重传（间隔约 1 s），
    # 这段时间发出的包会全部丢失，被 analyze() 误判成一次"链路中断"。
    # 反过来 PC-B 回第一个包时同样要解析 PC-A，所以双向都要先建立。
    # 做法：正式计时前"发包 → 等回包"，探测成功或超时才进入正式统计。
    if args.warmup > 0:
        wend = time.perf_counter_ns() + int(args.warmup * 1e9)
        wseq = 0
        ok = False
        while time.perf_counter_ns() < wend:
            wseq += 1
            try:
                s.sendto(HDR.pack(TAG_ECHO, wseq, time.perf_counter_ns()) + filler, addr)
            except OSError:
                pass
            probe_end = time.perf_counter_ns() + 20_000_000     # 每个探测包等 20 ms
            while time.perf_counter_ns() < probe_end:
                try:
                    s.recvfrom(2048)
                    ok = True
                    break
                except (BlockingIOError, OSError):
                    pass
            if ok:
                break
        # 隔离期：排空在飞的预热回包。预热包的序号也是 1..N，若不排空，
        # 它们会顶掉正式阶段同序号包的 pending 项，算出虚假的极小 RTT。
        time.sleep(0.05)
        try:
            while True:
                s.recvfrom(2048)
        except (BlockingIOError, OSError):
            pass
        print(f"[{now_str()}] 预热{'完成' if ok else '超时'}"
              f"（探测包 {wseq} 个，{'对端已应答' if ok else '未收到回包 —— 先确认服务端已启动、防火墙已放行'}）")

    records = []
    pending = {}          # seq -> 发送时刻 ns
    seq = 0
    sent = 0
    recv = 0
    t0 = time.time()
    next_send = time.perf_counter_ns()
    last_tick = t0
    csv_rows = []

    try:
        while True:
            if args.duration and time.time() - t0 >= args.duration:
                break
            now_ns = time.perf_counter_ns()

            if now_ns >= next_send:
                # seq 只在发送成功之后才前进，以保证恒等式 seq == len(records) 始终成立。
                # 回包时用 records[rseq-1] 按下标回填，这个恒等式是它的前提：
                # 若发送失败也把 seq 加一，下标就永久错位，之后第一个回包即 IndexError 崩溃。
                nxt_seq = seq + 1
                ts = time.perf_counter_ns()
                pkt = HDR.pack(TAG_ECHO, nxt_seq, ts) + filler
                try:
                    s.sendto(pkt, addr)
                except OSError as e:
                    print(f"  发送失败: {e}")
                else:
                    seq = nxt_seq
                    pending[seq] = ts
                    sent += 1
                    records.append([seq, None])
                next_send += interval_ns
                if next_send < now_ns:
                    next_send = now_ns + interval_ns

            # 收包：非阻塞地把已经到达的回包一次性收干净
            try:
                while True:
                    data, _ = s.recvfrom(2048)
                    if len(data) < HDR_LEN:
                        continue
                    tag, rseq, rts = HDR.unpack(data[:HDR_LEN])
                    if tag != TAG_ECHO:
                        continue
                    if rseq in pending:
                        rtt_us = (time.perf_counter_ns() - pending.pop(rseq)) / 1000.0
                        records[rseq - 1][1] = rtt_us
                        csv_rows.append((rseq, f"{rtt_us:.1f}"))
                        recv += 1
            except BlockingIOError:
                pass
            except OSError:
                pass

            # 超时判定
            if pending:
                exp = time.perf_counter_ns() - timeout_ns
                for q in [q for q, t in pending.items() if t < exp]:
                    pending.pop(q, None)
                    csv_rows.append((q, ""))

            if time.time() - last_tick >= 1.0:
                el = time.time() - t0
                loss = (sent - recv) / sent * 100.0 if sent else 0.0
                rtts = [r for _, r in records if r is not None]
                avg = sum(rtts) / len(rtts) if rtts else 0.0
                print(f"[{now_str()}] {el:6.1f}s | 发 {sent:>9,} 收 {recv:>9,} "
                      f"| 丢包 {loss:6.3f}% | RTT 均 {avg:7.0f} µs")
                last_tick = time.time()

            # 不 sleep：时延测量必须靠忙轮询才能拿到真实 RTT，
            # 一旦 sleep，回包会等到下一个发包周期才被读到，RTT 会被量化。
            # 代价是测试期间占满一个 CPU 核心，这是延迟测量的正常做法。

    except KeyboardInterrupt:
        print("\n收到中断，正在汇总…")

    # 收尾：再等一个超时窗口，把最后几个还在路上的回包收干净，
    # 否则测试结束时正在飞的包会被误判成丢包/断流。
    drain_until = time.perf_counter_ns() + min(timeout_ns, 300_000_000)
    while pending and time.perf_counter_ns() < drain_until:
        try:
            data, _ = s.recvfrom(2048)
        except (BlockingIOError, OSError):
            continue
        if len(data) < HDR_LEN:
            continue
        tag, rseq, rts = HDR.unpack(data[:HDR_LEN])
        if tag == TAG_ECHO and rseq in pending:
            rtt_us = (time.perf_counter_ns() - pending.pop(rseq)) / 1000.0
            records[rseq - 1][1] = rtt_us
            csv_rows.append((rseq, f"{rtt_us:.1f}"))
            recv += 1
    for q in list(pending.keys()):
        pending.pop(q, None)
        csv_rows.append((q, ""))

    dur = time.time() - t0
    records = [(q, r) for q, r in records]
    st = analyze(records)
    print_stats(st, "时延 / 丢包 / 链路中断 报告", args.frame, dur,
                interval_s=args.interval,
                extra=f"  对端          : {args.host}:{args.port}\n"
                      f"  发包间隔      : {args.interval * 1000:.3f} ms "
                      f"(单包丢失 ≈ {args.interval * 1000:.3f} ms 断流)")

    out = stamp_out(args.out)
    parent = os.path.dirname(os.path.abspath(out))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["seq", "rtt_us"])
        w.writerows(csv_rows)
    print(f"  明细已保存    : {os.path.abspath(out)}")
    print("  （可与原装交换机的结果做对比：switch_test.py compare --a 原装.csv --b 国产.csv）")


# ------------------------------------------------------------------- flood
def cmd_flood(args):
    payload = payload_for_frame(args.frame)
    filler = b"\x00" * (payload - HDR_LEN)
    addr = (args.host, args.port)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)

    bits = frame_bits(args.frame)
    interval_s = (bits / (args.bandwidth * 1e6)) if args.bandwidth else 0.0

    print(f"[{now_str()}] 单向打流 → {args.host}:{args.port}")
    print(f"             帧长 {args.frame} B | 目标速率 "
          f"{args.bandwidth:.0f} Mbps | 时长 {args.duration:.0f} s")
    print("             结束时请对照【接收端】的丢包率，那才是真实丢包")

    # 预热：与 echo 同理，`flood` 是单向的，只需 PC-A 这侧建立 ARP。
    # 没有回包可依据，所以用固定时长（20 ms 一个探测包，节奏放慢少污染服务端计数）。
    if args.warmup > 0:
        print(f"[{now_str()}] 预热 {args.warmup:.1f} s（建立 ARP，不计入统计）…")
        wend = time.perf_counter_ns() + int(args.warmup * 1e9)
        wseq = 0
        while time.perf_counter_ns() < wend:
            wseq += 1
            try:
                s.sendto(HDR.pack(TAG_FLOOD, wseq, time.perf_counter_ns()) + filler, addr)
            except OSError:
                pass
            time.sleep(0.02)

    t0 = time.time()
    seq = 0
    sent_bytes = 0
    next_send = time.perf_counter_ns()
    last_tick = t0
    last_seq = 0
    last_bytes = 0
    try:
        while time.time() - t0 < args.duration:
            now_ns = time.perf_counter_ns()
            if now_ns >= next_send:
                seq += 1
                pkt = HDR.pack(TAG_FLOOD, seq, now_ns) + filler
                try:
                    s.sendto(pkt, addr)
                    sent_bytes += len(pkt)
                except OSError:
                    pass
                if interval_s > 0:
                    next_send += int(interval_s * 1e9)
                    if next_send < now_ns:
                        next_send = now_ns + int(interval_s * 1e9)
                else:
                    next_send = now_ns
            if time.time() - last_tick >= 1.0:
                dt = time.time() - last_tick
                print(f"[{now_str()}] 已发 {seq:>12,} 包 | "
                      f"{(seq - last_seq) / dt:>10,.0f} pps | "
                      f"{(sent_bytes - last_bytes) * 8 / dt / 1e6:>8.2f} Mbps")
                last_tick = time.time()
                last_seq = seq
                last_bytes = sent_bytes
    except KeyboardInterrupt:
        print("\n收到中断…")

    dur = time.time() - t0
    print()
    print("=" * 62)
    print("  发送端汇总")
    print("=" * 62)
    print(f"  发送总包数    : {seq:,}")
    print(f"  发送总字节    : {sent_bytes:,}")
    print(f"  实测发送速率  : {seq / dur:,.0f} pps / {sent_bytes * 8 / dur / 1e6:.2f} Mbps")
    print("=" * 62)
    print("  ※ 请到接收端查看实际收到多少、丢了多少 —— 发送端的数字不算数")


# ----------------------------------------------------------------- compare
def load_csv(path):
    recs = []
    with open(path, "r", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        for row in rd:
            try:
                q = int(row["seq"])
            except (KeyError, ValueError):
                continue
            v = (row.get("rtt_us") or "").strip()
            recs.append((q, float(v) if v else None))
    return recs


def cmd_compare(args):
    ra, rb = load_csv(args.a), load_csv(args.b)
    sa, sb = analyze(ra), analyze(rb)
    na = os.path.basename(args.a)
    nb = os.path.basename(args.b)

    rows = [
        ("发送包数", f"{sa['sent']:,}", f"{sb['sent']:,}", ""),
        ("丢包数", f"{sa['lost']:,}", f"{sb['lost']:,}", "越低越好"),
        ("丢包率 %", f"{sa['loss_pct']:.4f}", f"{sb['loss_pct']:.4f}", "目标 0"),
        ("RTT 最小 µs", f"{sa['rtt_min']:.0f}", f"{sb['rtt_min']:.0f}", "此值最接近交换机真实时延"),
        ("RTT 平均 µs", f"{sa['rtt_avg']:.0f}", f"{sb['rtt_avg']:.0f}", ""),
        ("RTT 最大 µs", f"{sa['rtt_max']:.0f}", f"{sb['rtt_max']:.0f}", "尖峰代表排队"),
        ("RTT P99 µs", f"{sa['rtt_p99']:.0f}", f"{sb['rtt_p99']:.0f}", ""),
        ("抖动 µs", f"{sa['jitter']:.0f}", f"{sb['jitter']:.0f}", "越小越好"),
        ("相邻最大偏差 µs", f"{sa['max_dev']:.0f}", f"{sb['max_dev']:.0f}", "越小越好"),
        ("中断次数", f"{len(sa['outages'])}", f"{len(sb['outages'])}", "应为 0"),
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


# -------------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(
        description="零仪表版交换机测试工具（丢包率 / 时延 / 抖动 / 环网自愈 / 吞吐）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python switch_test.py server --port 5001 --duration 600\n"
               "  python switch_test.py echo --host 192.168.1.20 --frame 64 --interval 0.002 --duration 300\n"
               "  python switch_test.py flood --host 192.168.1.20 --frame 1518 --bandwidth 100 --duration 60\n"
               "  python switch_test.py compare --a orig.csv --b new.csv\n")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("server", help="接收端：回显 + 统计")
    ps.add_argument("--bind", default="0.0.0.0")
    ps.add_argument("--port", type=int, default=5001)
    ps.add_argument("--duration", type=float, default=0, help="运行秒数，0=不限")
    ps.add_argument("--fast", action="store_true",
                    help="极简回显模式：不做统计，只为压低响应延迟，测时延/抖动时用")
    ps.set_defaults(func=cmd_server)

    pe = sub.add_parser("echo", help="发送端：时延 / 抖动 / 丢包 / 中断检测")
    pe.add_argument("--host", required=True)
    pe.add_argument("--port", type=int, default=5001)
    pe.add_argument("--frame", type=int, default=64, help="以太网帧长 64~1518")
    pe.add_argument("--interval", type=float, default=0.01, help="发包间隔秒，0.001=1ms")
    pe.add_argument("--duration", type=float, default=60)
    pe.add_argument("--timeout", type=float, default=0.05, help="单包超时秒")
    pe.add_argument("--warmup", type=float, default=1.0,
                    help="正式计时前的邻居探测秒数（建立 ARP，不计入统计），0=关闭")
    pe.add_argument("--out", default="")
    pe.set_defaults(func=cmd_echo)

    pf = sub.add_parser("flood", help="发送端：单向吞吐压测")
    pf.add_argument("--host", required=True)
    pf.add_argument("--port", type=int, default=5001)
    pf.add_argument("--frame", type=int, default=1518)
    pf.add_argument("--bandwidth", type=float, default=100, help="Mbps，0=全速")
    pf.add_argument("--duration", type=float, default=60)
    pf.add_argument("--warmup", type=float, default=1.0,
                    help="正式打流前的预热秒数（建立 ARP，不计入统计），0=关闭")
    pf.set_defaults(func=cmd_flood)

    pc = sub.add_parser("compare", help="对比两份 CSV 报告")
    pc.add_argument("--a", required=True)
    pc.add_argument("--b", required=True)
    pc.set_defaults(func=cmd_compare)

    args = p.parse_args()
    if args.cmd in ("echo", "flood") and not (64 <= args.frame <= 1518):
        print("帧长应在 64~1518 之间")
        sys.exit(2)
    args.func(args)


if __name__ == "__main__":
    main()
