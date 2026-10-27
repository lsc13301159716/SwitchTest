import csv
import json
import math
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'out'
DEST = OUT / 'analysis_20261014'
DEST.mkdir(exist_ok=True)

def quantile(values, p):
    # Linear interpolation, index (n-1)*p; values sorted ascending.
    x = (len(values)-1)*p
    a = math.floor(x)
    b = math.ceil(x)
    return values[a] + (values[b]-values[a])*(x-a)

summaries, events = [], []
for path in sorted(OUT.glob('*/*.csv')):
    if path.name.endswith('.late.csv') or path.parent == DEST:
        continue
    match = re.search(r'_(DIRECT|WAGO|Weidmuller)_ECHO_A(\d+)_B(\d+)_R(\d+)_F(\d+)\.csv$', path.name)
    if not match:
        raise ValueError(f'Unrecognized name: {path}')
    device, a, b, rep, frame = match.groups()
    log = path.with_suffix('.log').read_text(encoding='utf-8-sig')
    params = {}
    for key in ('frame', 'interval', 'duration', 'timeout', 'drain', 'warmup', 'jobs'):
        m = re.search(r"'"+key+r"': ([\d.]+)", log)
        params[key] = float(m.group(1)) if m else None
    session = re.search(r'session=([0-9a-f]+)', log).group(1)
    start = re.search(r'启动时间：([^\r\n]+)', log).group(1)
    duration_match = re.search(r'发包时长 ([\d.]+)s', log)
    issues = []
    if not duration_match or abs(float(duration_match.group(1))-params['duration']) > .02:
        issues.append('发包时长缺失或不符')
    counts = Counter()
    rtts, all_rtts, gaps = [], [], []
    n, run, max_run, run_count, last_s = 0, 0, 0, 0, None
    for row in csv.DictReader(path.open(encoding='utf-8-sig', newline='')):
        n += 1
        if int(row['worker']) != 0 or int(row['seq']) != n:
            issues.append(f'序号/worker异常:{n}')
        status = row['status']
        counts[status] += 1
        send_s = float(row['send_s'])
        if last_s is not None:
            gaps.append((send_s-last_s)*1000)
            if send_s < last_s:
                issues.append(f'发送时刻倒退:{n}')
        last_s = send_s
        if status == 'on_time':
            value = float(row['rtt_us'])/1000
            rtts.append(value)
            all_rtts.append(value)
            if value > params['timeout']*1000 + .0001:
                issues.append(f'按时RTT越限:{n}')
            if run >= 2:
                run_count += 1
            run = 0
        else:
            run += 1
            max_run = max(max_run, run)
            late = float(row['late_rtt_us'])/1000 if row['late_rtt_us'] else None
            if late is not None:
                all_rtts.append(late)
            events.append(dict(device=device, a=int(a), b=int(b), frame=int(frame), seq=n,
                               send_s=send_s, status=status, late_rtt_ms=late, file=path.name))
    if run >= 2:
        run_count += 1
    summary_match = re.search(r'尝试 ([\d,]+)；按时回包 ([\d,]+)；超时或本地失败 ([\d,]+)', log)
    if not summary_match or tuple(int(v.replace(',', '')) for v in summary_match.groups()) != (n, counts['on_time'], n-counts['on_time']):
        issues.append('日志/CSV汇总不一致')
    late_path = Path(str(path)+'.late.csv')
    if counts['late']:
        if not late_path.exists():
            issues.append('缺迟到附件')
        else:
            late_rows = list(csv.DictReader(late_path.open(encoding='utf-8-sig', newline='')))
            if len(late_rows) != counts['late']:
                issues.append('迟到附件数量不符')
    rtts.sort()
    all_rtts.sort()
    loss = n-counts['on_time']
    verdict = 'INVALID' if issues else ('PASS' if loss == 0 else ('FAIL' if max_run >= 2 or loss/n*100 > .001 else 'REVIEW'))
    summaries.append(dict(device=device, a=int(a), b=int(b), frame=int(frame), n=n,
                          on_time=counts['on_time'], late=counts['late'], unreturned=counts['unreturned'],
                          send_failed=counts['send_failed'], failure_pct=loss/n*100,
                          max_run=max_run, consecutive_segments=run_count,
                          p50_ms=quantile(rtts,.5), p90_ms=quantile(rtts,.9), p99_ms=quantile(rtts,.99),
                          max_on_time_ms=max(rtts), max_received_ms=max(all_rtts),
                          trimmed_max_ms=all_rtts[max(0,len(all_rtts)-math.ceil(len(all_rtts)*.001)-1)],
                          max_send_gap_ms=max(gaps), actual_pps=n/float(duration_match.group(1)),
                          params=params, session=session, start=start, verdict=verdict, issues=issues, file=path.name))

for name, rows in [('round_summary', summaries), ('exception_packets', events)]:
    with (DEST / (name+'.csv')).open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
(DEST / 'analysis.json').write_text(json.dumps({'rounds': summaries, 'events': events},ensure_ascii=False,indent=2),encoding='utf-8')
for device in ('DIRECT','WAGO','Weidmuller'):
    rows = [r for r in summaries if r['device']==device]
    print(device, 'rounds', len(rows), 'n',sum(r['n'] for r in rows), 'late',sum(r['late'] for r in rows),
          'unreturned',sum(r['unreturned'] for r in rows),'send_failed',sum(r['send_failed'] for r in rows),
          'verdicts',dict(Counter(r['verdict'] for r in rows)))
    for r in rows:
        if r['late'] or r['unreturned'] or r['issues']:
            print({k:r[k] for k in ('a','b','frame','n','late','unreturned','max_run','p50_ms','p99_ms','max_received_ms','max_send_gap_ms','issues')})
print('events',events)
print('parameter sets', {str(r['params']) for r in summaries})

# Correlate late events with actual local send gaps, rather than assumed 1ms slots.
for path_name in {e['file'] for e in events}:
    path = next(OUT.glob('*/'+path_name))
    with path.open(encoding='utf-8-sig',newline='') as f:
        rows = list(csv.DictReader(f))
    for event in [e for e in events if e['file']==path_name]:
        idx = event['seq']-1
        nearby = [(i, (float(rows[i]['send_s'])-float(rows[i-1]['send_s']))*1000)
                  for i in range(max(1,idx),min(len(rows),idx+4))]
        gap_idx, gap_ms = max(nearby,key=lambda x:x[1])
        event['nearby_send_gap_ms'] = gap_ms
        event['gap_before_seq'] = int(rows[gap_idx]['seq'])
with (DEST/'exception_packets.csv').open('w',encoding='utf-8-sig',newline='') as f:
    writer = csv.DictWriter(f,fieldnames=list(events[0]))
    writer.writeheader()
    writer.writerows(events)
(DEST/'analysis.json').write_text(json.dumps({'rounds':summaries,'events':events},ensure_ascii=False,indent=2),encoding='utf-8')

lines = ['# 交换机测试数据分析与重测建议', '',
         '分析日期：2026-10-14。数据来源：out/DIRECT、out/WAGO、out/Weidmuller及out/BATCH。设备名称按目录保留，不推定哪台是原装或国产。原始文件未修改。', '',
         '## 1. 结论', '',
         '优先复核Weidmuller A13—B01，再复核两台的A02—B01；随后复核WAGO A11—B01、A16—B01、A05—B01，以及Weidmuller A03—B01。这些是异常端口对，不是已确认故障端口。', '',
         '99轮均为60秒。9个超时回包最终全部收到：没有观察结束未回包、没有本地发送失败。8个异常轮次均在迟到附近出现超过50ms的实际发送间隙，间隙与迟到RTT接近，提示发送端程序/系统暂停可能参与异常；无法据此证明报文何时到达内核，也不能直接给交换机或PC定责。', '',
         '## 2. 覆盖与数据一致性', '',
         '- 直连：64/512/1518B各1轮、每轮60秒；尚未完成原计划每帧长600秒的长基线。直连文件名A01_B01作为命名标签，不解释为交换机端口。',
         '- 两台交换机：A02—B01至A16—B01，以及A01—B02；每组3种帧长，各48轮，16口短测覆盖完整。',
         '- 参数一致：interval=0.001s、timeout=0.05s、drain=0.5s、warmup=3s、jobs=1、duration=60s；发送端日志与CSV尝试数/按时数/失败数一致；CSV worker=0，序号连续，发送时刻单调；迟到附件数量匹配。',
         '- 发包时长均为60.000s。版本哈希、实际链路速率、网卡/线缆信息和现场动作未随这些记录提供，无法核实这些环境条件完全一致。',
         '- 99份发送端日志均报告socket错误0；CSV中本地发送失败为0。', '',
         '| 设备 | 轮数 | 尝试总数 | 无超时轮数 | 异常轮数 | 迟到包数 | 未回包 | 最长连续超时 |',
         '|---|---:|---:|---:|---:|---:|---:|---:|']
for device in ('DIRECT','WAGO','Weidmuller'):
    rs = [r for r in summaries if r['device']==device]
    lines.append(f"| {device} | {len(rs)} | {sum(r['n'] for r in rs):,} | {sum(r['late']==0 and r['unreturned']==0 for r in rs)} | {sum(r['late']>0 or r['unreturned']>0 for r in rs)} | {sum(r['late'] for r in rs)} | {sum(r['unreturned'] for r in rs)} | {max(r['max_run'] for r in rs)} |")
lines += ['', '## 3. 异常轮次与证据', '',
          '时间为正式测试开始后的实际发送时间，RTT与发包间隙单位为ms。每行对应一个异常轮次；两个连续迟到包保留各自RTT。', '',
          '| 设备 | A口—B口 | 帧长B | 迟到包号 | 发送时间s | 迟到RTT ms | 附近发包间隙ms | 连续超时包数 | 超时比例 |',
          '|---|---|---:|---|---|---|---:|---:|---:|']
for r in summaries:
    if not r['late']:
        continue
    es = [e for e in events if e['file']==r['file']]
    lines.append(f"| {r['device']} | A{r['a']:02}—B{r['b']:02} | {r['frame']} | {', '.join(str(e['seq']) for e in es)} | {', '.join(f'{e["send_s"]:.6f}' for e in es)} | {', '.join(f'{e["late_rtt_ms"]:.3f}' for e in es)} | {max(e['nearby_send_gap_ms'] for e in es):.3f} | {r['max_run']} | {r['failure_pct']:.6f}% |")
lines += ['', 'Weidmuller A13—B01的1518B轮次：16366、16367号包间隔约1ms，随后到16368号包出现116.412ms间隙；两包RTT分别117.532、116.544ms。其他7个异常轮次均为单包迟到，迟到包到下一包的发送间隙超过50ms。全批次超过50ms的最大发包间隙恰好出现在这8个异常轮次。', '',
          '按《短时测试判据建议.md》的暂定规则，8轮均属于FAIL（本轮不达标）：单包/约6万次的比例约0.0017%，已超过0.001%的暂定复核界限；A13的1518B还出现连续2包。该标签不代表交换机硬件故障或拒收，阈值也尚非已确认验收标准。其余91轮在现有完整性检查下为PASS，仅表示本轮未观察到失败。不能用整批合并比例稀释异常轮次，也不能用后续通过覆盖首次异常。', '',
          '## 4. 主体时延对比', '',
          '下表为同帧长各轮分位数的中位数，不是把所有包合并后的分位数；直连每帧长仅1轮。分位数采用排序后位置(n−1)×p的线性插值，单位ms；只统计按时回包，存在50ms门槛截断。逐轮真实最大收到RTT（含迟到）、按时最大值及裁去最大0.1%后的最大值另见round_summary.csv。', '',
          '| 设备 | 帧长B | p50 | p90 | p99 |', '|---|---:|---:|---:|---:|']
import statistics
for device in ('DIRECT','WAGO','Weidmuller'):
    for frame in (64,512,1518):
        rs = [r for r in summaries if r['device']==device and r['frame']==frame]
        lines.append(f"| {device} | {frame} | {statistics.median(r['p50_ms'] for r in rs):.3f} | {statistics.median(r['p90_ms'] for r in rs):.3f} | {statistics.median(r['p99_ms'] for r in rs):.3f} |")
lines += ['', 'Weidmuller主体p50比WAGO高约2%～5%，p90高约1%～3%；p99轮次中位数高约24%～70%，表现为尾部时延较大。两台分日期测试（WAGO为10月13日、Weidmuller为10月14日），每个配置仅1轮，因此不能把差异直接解释为设备固有性能差异。1518B经交换机p50约1.21～1.23ms，直连约0.596ms；两台都有此现象，需通过同日交替对照确认，不能单独作为某台不合格依据。', '',
          '附加时延观察项：Weidmuller A06—B01三个帧长的p99约4.289～5.153ms，A11—B01的64B为5.433ms，是较高的尾部时延候选；它们未超过50ms门槛，不按超时失败处理。若业务对毫秒级时延敏感，可在异常复核之后补同条件交替对照。', '',
          '## 5. 服务端日志', '',
          'server.log保存了10月13日的最终按session汇总。按发送端session匹配，3轮直连与48轮WAGO全部找到记录，唯一收到包数与对应发送端尝试数一致，匹配流的重复与已观察序号缺口均为0。', '',
          'server2.log覆盖10月14日测试时段，但文件结束仍是每秒进度，没有最终逐session汇总。其最后累计缺口1856、未对账流62是服务端整批统计，包含未与本次48轮一一对应的流，不能直接记为Weidmuller这些测试的丢包。发送端48轮CSV中每次尝试均最终取得回包，是这些轮次已回包的证据。建议本批服务停止时用Ctrl+C保存最终汇总并补充归档；如已停止且无汇总，不补造计数。', '',
          'server.log中的内核IPv4丢弃增量15022为全网卡统计，不能归因于本测试或某台交换机；匹配轮次的回包与服务端接收记录应分别保留。', '',
          '## 6. 重测安排', '',
          '先补当日直连基线：64、512、1518B各600秒，保持50ms timeout及原输出方式；若再次出现迟到，检查对应发送间隙，记录电脑负载、休眠/电源设置、USB网卡与现场动作。异常无需等待全部基线结束才记录。', '',
          '| 优先级 | 端口对 | 首先复核帧长 | 安排与理由 |', '|---|---|---|---|',
          '| 1 | Weidmuller A13—B01 | 1518B、64B | 两种帧长异常，1518B连续2包；WAGO同端口对照 |',
          '| 2 | 两台 A02—B01 | 512B | 两台相同组合/帧长都迟到，优先判断共同路径或端点因素 |',
          '| 3 | WAGO A11—B01、A16—B01 | 分别64B、512B | 迟到约127、124ms；Weidmuller同组合对照 |',
          '| 4 | Weidmuller A03—B01、WAGO A05—B01 | 分别64B、1518B | 单包迟到约89、67ms；另一台同组合对照 |', '',
          '异常配置先做600秒原接线复测，并在另一台同端口对、同帧长交替对照。有差异时补到3组配对复测；不要只延长异常设备而缺少对照。保持帧长、interval、timeout、服务端模式、程序版本和输出方式一致。', '',
          '如原接线复现，增加替代参考口的组合，例如A13—B08，并更换已编号的线缆逐项排查；多个异常都共享B01，不能只归因于A侧口。每次只改变一个条件，另记新用例。', '',
          '异常复核之后，补两台A01—B08、A01—B16、A08—B16三种帧长各600秒的代表性长测；生产关键端口不在这些组合时追加。原规程另列A01—B15为历史异常候选；本次A15—B01三种短测没有失败，可按生产重要性保留或后排，不能把旧线索当成本次故障。', '',
          '对持续复现、设备之间有差异或生产最关键的组合，再延长至1800秒。一次600秒无异常只能记为本轮未复现，首次事件仍保留。', '',
          '## 7. 输出文件', '',
          '- round_summary.csv：99轮完整指标、参数、原始文件名、暂定状态。',
          '- exception_packets.csv：9个迟到包的序号、时间、RTT及对应发送间隙。',
          '- analysis.json：结构化分析结果。',
          '- 根目录analyze_results.py：可重复计算的分析脚本；不改动原始测试记录。', '',
          '裁尾最大算法：将所有已收到回包的RTT（按时与迟到）排序，移除最大ceil(n×0.001)个样本后取最大值。该值只用于主体对比，不替代原始最大值或业务最坏响应要求。']
(DEST/'测试分析与重测建议.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
