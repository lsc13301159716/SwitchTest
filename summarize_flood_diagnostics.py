"""Summarize archived FLOOD logs and cached packet comparisons without altering evidence."""
import csv
import json
import re
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'out/analysis_all_FLOOD_20261026'
REPORT = ROOT / '报告/FLOOD全部未到达排查汇总_20261026.md'


def number(s):
    return int(s.replace(',', ''))


def warm_stats(cap, session):
    req, rep = {}, {}
    for m in cap['metadata']:
        if m['session'] != session or m['tag'] != 'W':
            continue
        key = (m['seq'], m['timestamp_value'])
        table = req if m['source_ip'] == '192.168.214.230' else rep
        table.setdefault(key, m['capture_ns'])
    rtts = [(rep[k] - v) / 1e6 for k, v in req.items() if k in rep]
    return dict(requests=len(req), matched_replies=len(rtts),
                min_ms=min(rtts) if rtts else None,
                median_ms=statistics.median(rtts) if rtts else None,
                max_ms=max(rtts) if rtts else None)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    captures = {}
    for path in sorted((ROOT / 'out').glob('capture_analysis*/analysis.json')):
        d = json.loads(path.read_text(encoding='utf-8'))
        for session, cmp in d['comparison'].items():
            if not cmp.get('client_unique') or not cmp.get('server_unique'):
                continue  # A mixed capture is not a paired observation for every session.
            # Refuse cached results if an original input has changed since analysis.
            for side in ('client', 'server'):
                original = ROOT / d[side]['path']
                if not original.exists() or original.stat().st_size != d[side]['bytes']:
                    raise ValueError(f'Original input missing or changed: {original}')
                if original.stat().st_mtime > path.stat().st_mtime:
                    raise ValueError(f'Cached analysis older than original: {original}')
            captures[session] = (path, d)

    # Quantities already supported by matched sessions and prior boundary analysis.
    startup_prefix = {'0a453a39':104897, '1c8b37d4':96376, '61d37df8':45680,
                      '26ea33a1':36666, '46bcab73':17818, 'dabacca0':45996}
    rows = []
    for path in sorted((ROOT / 'out/FLOOD').glob('*_server.log')):
        text = path.read_text(encoding='utf-8-sig')
        case = re.search(r'_(WAGO|Weidmuller)_FLOOD_A(\d+)_B(\d+)_F(\d+)_M(\d+)_D(\d+)_(R\d+)_server', path.name)
        if not case:
            continue
        brand, pa, pb, frame, rate, duration, run = case.groups()
        finals = list(re.finditer(r"F \('([^']+)', (\d+)\) session=([0-9a-f]+): 唯一 ([\d,]+), 重复 ([\d,]+), 对账未到达 ([\d,]+)/([\d,]+)", text))
        if len(finals) != 1:
            raise ValueError(f'Expected one reconciled session: {path}')
        ip, sport, session, received, duplicates, missing, total = finals[0].groups()
        received, duplicates, missing, total = map(number, (received, duplicates, missing, total))
        assert received + missing == total
        note = ''
        if session == '0a453a39':
            pa, note = '14', '原日志名A13，操作者已确认实际A14—B01。'
        changes, previous = [], 0
        for line_no, line in enumerate(text.splitlines(), 1):
            m = re.search(r'缺口/未到达 ([\d,]+)', line)
            if m:
                current = number(m[1])
                if current != previous:
                    changes.append(dict(line=line_no, missing=current, delta=current-previous, text=line))
                    previous = current
        prefix = startup_prefix.get(session, 0)
        category = ('零未到达' if not missing else '启动缺失，并伴随后续缺失' if prefix and missing > prefix
                    else '启动阶段缺失' if prefix else '准备正常后的接收缺失' if session in captures
                    else '只有应用日志，原因未定')
        row = dict(brand=brand, port_a=int(pa), port_b=int(pb), round=run,
                   date=path.name[:8], rate_mbps=int(rate), frame_bytes=int(frame), duration_s=int(duration),
                   session=session, app_received=received, app_unreturned=missing, expected=total,
                   duplicates=duplicates, startup_prefix=prefix, residual_missing=missing-prefix,
                   category=category, log=str(path.relative_to(ROOT)), note=note,
                   loss_changes=changes, sender_log_available=False)
        if session in captures:
            analysis_path, d = captures[session]
            cmp = d['comparison'][session]
            assert cmp['expected'] == total and cmp['end_total_observed']
            assert cmp['mismatched_send_timestamps'] == 0
            row['capture'] = dict(analysis=str(analysis_path.relative_to(ROOT)),
                client_unique=cmp['client_unique'], server_unique=cmp['server_unique'],
                client_first_seq=d['client']['streams'][session]['min_seq'],
                server_first_seq=d['server']['streams'][session]['min_seq'],
                neither_count=cmp['neither_count'], neither_ranges=cmp['neither_ranges'],
                client_drop=sum(s.get('dropped', 0) for s in d['client']['interface_statistics']),
                server_drop=sum(s.get('dropped', 0) for s in d['server']['interface_statistics']),
                app_minus_capture=received-cmp['server_unique'],
                warm=warm_stats(d['client'], session))
        rows.append(row)

    counts = {b:dict(rounds=sum(r['brand']==b for r in rows),
                    with_unreturned=sum(r['brand']==b and r['app_unreturned']>0 for r in rows),
                    zero_unreturned=sum(r['brand']==b and r['app_unreturned']==0 for r in rows))
              for b in ('WAGO','Weidmuller')}
    result = dict(scope='当前out/FLOOD中的已完成并取得结束总数的41份服务端日志；不含ECHO、BATCH或已删除原始日志。',
                  counts=counts, rows=rows,
                  caveat='端到端应用未到达，不等于交换机丢包；无发送端日志，不能核实本地发送失败。')
    (OUT/'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    fields = ['brand','port_a','port_b','round','date','rate_mbps','session','expected','app_received',
              'app_unreturned','startup_prefix','residual_missing','category','log','note']
    with (OUT/'all_rounds.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader(); w.writerows(rows)

    lines = ['# FLOOD全部未到达排查汇总（2026-10-26）', '',
        '## 结论与边界', '',
        '目前可确认两类问题并存：启动通信/地址确认尚未恢复就开始正式计数，以及已经正常通信后的少量接收缺失。另有接收端抓包自身丢弃，必须独立计算。现有证据不能把全部问题唯一定位到交换机、某一个物理端口或接收端扩展坞。', '',
        f'本次核对{len(rows)}份FLOOD服务端最终对账日志：WAGO {counts["WAGO"]["rounds"]}轮，其中{counts["WAGO"]["with_unreturned"]}轮有未到达；Weidmuller {counts["Weidmuller"]["rounds"]}轮，其中{counts["Weidmuller"]["with_unreturned"]}轮有未到达。这是轮次数量，不是端口故障率。未统计ECHO/BATCH；正常日志只说明该轮应用无未到达，缺少发送端日志时不能补证本地发送失败为零。', '',
        '## 所有有未到达的已归档轮次', '',
        '| 交换机 | 组合/轮次 | 速率M | 最终未到达 | 启动前缀缺失 | 其余缺失 | 证据分类 |',
        '|---|---|---:|---:|---:|---:|---|']
    for r in rows:
        if r['app_unreturned']:
            known = 'capture' in r
            prefix_display = f'{r["startup_prefix"]:,}' if known else '未定位'
            residual_display = f'{r["residual_missing"]:,}' if known else '未定位'
            lines.append(f'| {r["brand"]} | A{r["port_a"]:02}—B{r["port_b"]:02} {r["round"]} | {r["rate_mbps"]} | {r["app_unreturned"]:,} | {prefix_display} | {residual_display} | {r["category"]} |')
    lines += ['', '启动前缀指从1号起连续未收到的已定位区间；“其余缺失”不代表都已确定发生在稳定阶段。例如Weidmuller A02 R01后续154包紧邻ARP恢复过程。没有对应异常抓包的轮次不以正常复测替代归因。', '',
        'WAGO A03此前口述的78包未到达原始日志已删除，另列为历史异常，未计入上表或41份日志。A03现存三份日志均标R01，按文件时间/session区分。WAGO A14 R01原始日志误写A13，操作者已确认实际A14—B01，本次按实际组合统计。', '',
        '## 新增 A13～A16 的证据', '',
        '**Weidmuller A13 R01：73包，准备正常后的两次接收缺失。** 客户端正式1～487126号完整，预热146/146回包，往返1.682～28.041毫秒（中位2.401毫秒），没有数秒启动延迟。服务端10:34:35.071首次记录25个缺口，10:35:13.076再增48个，最终73。无法用初始ARP未完成解释。', '',
        '**Weidmuller A14 R01：45996包启动缺失，加65包后续缺失，共46061。** 双端正式首包均为45997号，1～45996号双端均未捕获；客户端抓包丢弃为0。客户端10:56:49.268664开始询问.231的MAC，10:56:57.939639才首次捕获对应回复，同一电脑间隔8.670975秒；10:56:57.939704紧接着出现45997号正式包。服务器本机时间10:56:51.068252开始集中捕获请求，随即回复；这里没有用两台电脑时间相减。两侧均无本会话W预热记录，不能据此断言用户关闭预热，需要发送端实际参数核实。', '',
        'A14服务端10:56:51.978首次记录45996个缺口；10:57:29.982再增56，10:57:30.982再增9。故即使排除启动前缀，也还有65包需要独立调查。后两次进度变化只能提供时间窗口，不能证明对应65个包连续丢失。', '',
        '**Weidmuller A15 R01：7包。** 客户端正式1～487012号完整，预热146/146回包，往返1.468～9.181毫秒；服务端11:16:43.520新增7个缺口，此后不增。不同于之前WAGO A15启动延迟的两轮，品牌、session和证据均需分开。', '',
        '**Weidmuller A16 R01：77包。** 客户端正式1～487226号完整，预热146/146回包，往返1.469～4.282毫秒；服务端11:19:37.176在本轮接近结束时新增77个缺口，结束对账及后续等待后仍为77。不是仅凭中途计数误把乱序包算成最终未到达。', '',
        '## 抓包自身漏记核验', '',
        '| 交换机/组合 | 应用唯一接收 | 接收抓包唯一F包 | 差额 | 抓包报告丢弃 |',
        '|---|---:|---:|---:|---:|']
    for r in rows:
        cap = r.get('capture')
        if cap:
            lines.append(f'| {r["brand"]} A{r["port_a"]:02} {r["round"]} | {r["app_received"]:,} | {cap["server_unique"]:,} | {cap["app_minus_capture"]:,} | {cap["server_drop"]:,} |')
    lines += ['', '这些配对轮次的应用接收数减去接收抓包唯一F包数，与抓包报告丢弃数一致，支持差额是捕获漏记。这是计数层面的核验，并非每个遗漏包的身份映射。不能把数万漏抓加入应用未到达，也不能因抓包有漏记而抹去应用最终对账中的7、73、77等缺失。接收应用未保存逐包接收清单，故无法从漏抓文件中唯一挑出全部实际未到达包号。', '',
        '全部已配对会话均核对了结束总数与应用分母、共同包号的内嵌发送时间戳；没有发现会话混配造成计数差异。Weidmuller A06客户端文件含上一轮A05 R02，分析仅使用配对session=46bcab73，不把前一轮流量合并进本轮。', '',
        '## 能确认的程序问题与尚未证实的硬件原因', '',
        '1. 当前SwitchTest.py的FLOOD预热只按固定时长发送W，不读取并确认当前会话的及时回包，随后直接正式计数。因此底层通信延迟数秒时，程序仍会开始统计，是已确认的测试准备判定缺口。它能解释为何异常准备状态进入测试，不能解释底层为何延迟，也不能解释已经正常预热的运行缺失。',
        '2. 发送序号只在sendto返回成功后推进；服务端按session和唯一包位图对账。未发现本次差异由重复包计数、错session或结束总数不一致造成。发送API成功仅说明提交成功，不能证明每包已经物理上线；发送端抓包看到也不能单独证明交换机已收到。',
        '3. 客户端完整、应用缺失的轮次，可能范围仍包括客户端抓包点之后的发送驱动/网卡、线缆、交换机与参考口路径、接收驱动/网卡、系统及应用收取过程。当前没有独立线上观察点和端口丢弃计数，不能选定其中一个责任设备。',
        '4. 两个品牌均出现同类异常，且使用相同电脑、扩展坞和线缆，优先隔离共同测试链路合理；这不等于已经证明电脑或扩展坞故障。接收端抓包漏记较多只证明捕获链路存在压力，不证明它就是应用未到达的原因。当前全网卡IPv4丢弃计数也不能给本测试流定责。', '',
        '## 下一步定位实验', '',
        '1. 优先选择A14类启动异常和A13/A16类运行缺失各一个用例，保留本次失败原始数据及正常复测，不继续仅换A口反复跑。',
        '2. 做实际链路同为100Mbps的直连FLOOD对照（现有DIRECT目录是ECHO，不能替代此对照）。保持两台电脑、帧长、目标速率、时长和抓包方式相同；保留启动后及闲置后再运行两个场景。若直连仍复现，说明该次异常无需经过交换机也能发生；若直连通过，仍需重复和后续替换实验。',
        '3. 每次仅改一项：替换接收端扩展坞/网卡，或线缆，或交换机B01参考口；比较异常是否随某个部件/路径变化。必要时再对换发送与接收角色。',
        '4. 正式轮次开始前应确认当前会话UDP预热连续及时回包；单次较早的ping或固定等待不构成当前正式发送的准备证据。若之后修改预热逻辑，应记录程序版本变化，原数据保留，不把旧FAIL直接覆盖为PASS。',
        '5. 运行缺失独立比较100M与95M；一次一项改变。降速后正常只说明负荷关联，不能区分交换机和端点。保留发送日志，核实本地发送失败、warmup、实际速率和正常完成。',
        '6. 为得到明确丢包位置，需提高接收捕获可靠性，或添加交换机端口计数/镜像等独立证据；也可让接收程序导出最终缺失序号清单，与双端pcap自动交叉核对。镜像/抓包自身仍需检查丢弃。', '',
        '按当前判定规则，已完成且最终有未到达的轮次仍记FAIL，并附“端到端未到达，责任设备未定/启动条件异常”等说明；正常复测另列。这里不把启动缺失自行改成INVALID，不改写正式Word报告。', '',
        '## 归档文件', '',
        '- `out/analysis_all_FLOOD_20261026/summary.json`：全部轮次、日志位置、缺口变化时间、配对捕获核验与预热统计。',
        '- `out/analysis_all_FLOOD_20261026/all_rounds.csv`：全部轮次索引。',
        '- `out/capture_analysis_Weidmuller_A13_20261026`至`A16_20261026`：新增四组双端分析及包号差异CSV。',
        '- 旧轮次的逐包分析与先前补充记录继续保留。所有原始日志和pcap均未修改。', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps(dict(counts=counts, report=str(REPORT), paired_sessions=sum('capture' in r for r in rows),
                          loss_rows=[{k:r[k] for k in ('brand','port_a','round','app_unreturned','category')} for r in rows if r['app_unreturned']]), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
