"""Recompute archived evidence for the WAGO replacement assessment.

Only writes into out/replacement_analysis_20261026. Does not run network tests.
"""
import argparse
import csv
import gc
import hashlib
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path

from analyze_flood_capture import scan, ranges, public

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'out/replacement_analysis_20261026'
BRANDS = ('WAGO', 'Weidmuller')


def dump(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def csv_out(name, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                        for k, v in row.items()})


def quantile(sorted_values, p):
    if not sorted_values:
        return None
    pos = (len(sorted_values) - 1) * p
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos-lo)


def echo():
    rows, events, segments = [], [], []
    for device in ('DIRECT', *BRANDS):
        for path in sorted((ROOT / 'out' / device).rglob('*.csv')):
            m = re.fullmatch(r'\d+_\d+_(DIRECT|WAGO|Weidmuller)_ECHO_A(\d+)_B(\d+)_R(\d+)_F(\d+)\.csv', path.name)
            if not m:
                assert path.name.endswith('.late.csv'), path
                continue
            brand, pa, pb, rep, frame = m.groups()
            text = path.with_suffix('.log').read_text(encoding='utf-8-sig')
            params = {k: float(re.search("'"+k+r"': ([\d.]+)", text).group(1))
                      for k in ('frame', 'interval', 'duration', 'timeout', 'drain', 'warmup', 'jobs')}
            session = re.search(r'session=([0-9a-f]+)', text).group(1)
            start = re.search(r'启动时间：([^\r\n]+)', text).group(1)
            dur = re.search(r'发包时长 ([\d.]+)s', text)
            issues, counts, timely, all_rtt = [], Counter(), [], []
            n, last_s, max_gap, max_late, max_bad, late_run, bad_run = 0, None, 0, 0, 0, 0, 0
            local_events, local_segments, current, prev_t = [], [], None, None
            for r in csv.DictReader(path.open(encoding='utf-8-sig', newline='')):
                n += 1
                seq, t = int(r['seq']), float(r['send_s'])
                if seq != n or int(r['worker']) != 0:
                    issues.append(f'seq/worker:{n}')
                if last_s is not None:
                    gap = (t-last_s)*1000
                    max_gap = max(max_gap, gap)
                    if gap < 0:
                        issues.append(f'send_s decreases:{n}')
                    for e in local_events[-10:]:
                        if 0 < seq-e['seq'] <= 3:
                            e['nearby_send_gap_ms'] = max(e['nearby_send_gap_ms'], gap)
                            if seq == e['seq']+1:
                                e['next_send_gap_ms'] = gap
                last_s = t
                status = r['status']
                assert status in ('on_time', 'late', 'unreturned', 'send_failed'), status
                counts[status] += 1
                late_run = late_run+1 if status == 'late' else 0
                bad_run = bad_run+1 if status != 'on_time' else 0
                max_late, max_bad = max(max_late, late_run), max(max_bad, bad_run)
                val = float(r['rtt_us'])/1000 if status == 'on_time' else float(r['late_rtt_us'])/1000 if r['late_rtt_us'] else None
                if val is not None:
                    all_rtt.append(val)
                if status == 'on_time':
                    timely.append(val)
                    if val > params['timeout']*1000 + 1e-6:
                        issues.append(f'on_time exceeds timeout:{n}')
                    if current:
                        current['next_success_send_s'] = t
                        local_segments.append(current)
                        current = None
                else:
                    if not current:
                        current = dict(first_seq=seq, last_seq=seq, count=0, first_send_s=t,
                                       last_send_s=t, max_rtt_ms=0, previous_success_send_s=prev_t)
                    current.update(last_seq=seq, last_send_s=t, count=current['count']+1,
                                   max_rtt_ms=max(current['max_rtt_ms'], val or 0))
                    local_events.append(dict(seq=seq, send_s=t, status=status, rtt_ms=val,
                                             nearby_send_gap_ms=0, next_send_gap_ms=None))
                prev_t = t
            if current:
                current['next_success_send_s'] = None
                local_segments.append(current)
            actual = float(dur.group(1)) if dur else None
            if actual is None or abs(actual-params['duration']) > .02:
                issues.append('duration incomplete')
            mcount = re.search(r'尝试 ([\d,]+)；按时回包 ([\d,]+)；超时或本地失败 ([\d,]+)', text)
            if not mcount or tuple(int(x.replace(',', '')) for x in mcount.groups()) != (n, counts['on_time'], n-counts['on_time']):
                issues.append('log/csv count mismatch')
            attachment = Path(str(path)+'.late.csv')
            if counts['late']:
                if not attachment.exists():
                    issues.append('missing late attachment')
                else:
                    aux = list(csv.DictReader(attachment.open(encoding='utf-8-sig', newline='')))
                    ev = [e for e in local_events if e['status']=='late']
                    if len(aux) != len(ev) or any(int(a['seq']) != e['seq'] or abs(float(a['late_us'])/1000-e['rtt_ms'])>.000051 for a,e in zip(aux,ev)):
                        issues.append('late attachment mismatch')
            socket = re.search(r'socket 错误\s*:\s*([\d,]+)', text)
            verdict = ('INVALID' if issues else 'FAIL' if counts['unreturned'] or counts['send_failed']
                       else 'INVALID' if max_late >= 10 else 'REVIEW' if counts['late'] else 'PASS')
            timely.sort()
            all_rtt.sort()
            base = dict(brand=brand, port_a=int(pa), port_b=int(pb), round=f'R{int(rep):02}',
                        frame_bytes=int(frame), duration_s=params['duration'], session=session,
                        start=start, file=str(path.relative_to(ROOT)))
            row = dict(**base, n=n, **{k:counts[k] for k in ('on_time','late','unreturned','send_failed')},
                       longest_late=max_late, longest_failure=max_bad, verdict=verdict,
                       verdict_reason='记录完整性问题' if issues else '未回包或本地发送失败' if verdict=='FAIL'
                       else '连续迟到至少10包' if verdict=='INVALID' else '最终回包全部收到但有迟到' if verdict=='REVIEW' else '无超时或本地失败',
                       p50_ms=quantile(timely,.5), p90_ms=quantile(timely,.9), p99_ms=quantile(timely,.99),
                       max_received_ms=max(all_rtt), max_on_time_ms=max(timely),
                       trimmed_max_ms=all_rtt[max(0,len(all_rtt)-math.ceil(len(all_rtt)*.001)-1)],
                       max_send_gap_ms=max_gap, actual_pps=n/actual if actual else None,
                       actual_duration_s=actual, socket_errors=int(socket.group(1).replace(',','')) if socket else None,
                       params=params, integrity_issues=issues)
            rows.append(row)
            events.extend(dict(**base, **e) for e in local_events)
            segments.extend(dict(**base, **s) for s in local_segments)
            print(f'ECHO {brand} {pa}-{pb} {frame} {rep} {params["duration"]:.0f}s {verdict} late={counts["late"]}', flush=True)
    # Match archived server session summaries without attributing global counters.
    servers = {}
    for log in sorted((ROOT/'out/BATCH').glob('*.log')):
        text = log.read_text(encoding='utf-8-sig')
        for m in re.finditer(r"S \('([^']+)', (\d+)\) session=([0-9a-f]+): 唯一 ([\d,]+), 重复 ([\d,]+), 已观察序号缺口（尾部未知） ([\d,]+)/([\d,]+)", text):
            ip, port, sid, unique, dup, gap, mx = m.groups()
            servers.setdefault(sid, []).append(dict(log=str(log.relative_to(ROOT)), unique=int(unique.replace(',','')),
                                                    duplicates=int(dup.replace(',','')), gaps=int(gap.replace(',',''))))
    for r in rows:
        r['server_summaries'] = servers.get(r['session'], [])
        for s in r['server_summaries']:
            assert s['unique'] == r['n'] and s['duplicates']==0 and s['gaps']==0, (r, s)
    assert len({r['session'] for r in rows}) == len(rows)
    dump('echo.json', dict(rounds=rows, events=events, segments=segments))
    csv_out('echo_rounds.csv', rows)
    csv_out('echo_events.csv', events)
    csv_out('echo_failure_segments.csv', segments)


def flood():
    rows = []
    for path in sorted((ROOT/'out/FLOOD').glob('*_server.log')):
        m = re.search(r'_(WAGO|Weidmuller)_FLOOD_A(\d+)_B(\d+)_F(\d+)_M(\d+)_D(\d+)_(R\d+)_server', path.name)
        if not m:
            continue
        brand, pa, pb, frame, rate, dur, rep = m.groups()
        text = path.read_text(encoding='utf-8-sig')
        finals = list(re.finditer(r"F \('([^']+)', (\d+)\) session=([0-9a-f]+): 唯一 ([\d,]+), 重复 ([\d,]+), 对账未到达 ([\d,]+)/([\d,]+)", text))
        assert len(finals)==1, path
        ip, sport, sid, unique, dup, missing, total = finals[0].groups()
        unique, dup, missing, total = [int(x.replace(',','')) for x in (unique,dup,missing,total)]
        assert unique+missing == total
        note = ''
        if sid == '0a453a39':
            pa, note = '14', '沿用项目补充记录中的人工接线纠错：原日志名A13，实际A14'
        changes, prev = [], 0
        for line in text.splitlines():
            mg = re.search(r'缺口/未到达 ([\d,]+)', line)
            if mg:
                val = int(mg.group(1).replace(',',''))
                if val != prev:
                    changes.append(dict(delta=val-prev, count=val, line=line))
                    prev=val
        rows.append(dict(brand=brand, port_a=int(pa), port_b=int(pb), round=rep,
                         stamp=path.name[:15], date=path.name[:8], session=sid,
                         frame_bytes=int(frame), target_mbps=int(rate), nominal_duration_s=int(dur),
                         expected=total, received=unique, missing=missing, duplicates=dup,
                         missing_pct=missing/total*100, nominal_submitted_mbps=total*(int(frame)+20)*8/int(dur)/1e6,
                         verdict='FAIL' if missing else '零未到达',
                         file=str(path.relative_to(ROOT)), note=note, loss_changes=changes,
                         sender_log_available=False))
    assert len({r['session'] for r in rows})==len(rows)
    dump('flood.json', rows)
    csv_out('flood_rounds.csv', rows)


def warm_stats(capture, sid):
    req, rep = {}, {}
    for m in capture['metadata']:
        if m['tag']!='W' or m['session']!=sid:
            continue
        table=req if m['source_ip']=='192.168.214.230' else rep
        table.setdefault((m['seq'],m['timestamp_value']),m['capture_ns'])
    vals=[(rep[k]-v)/1e6 for k,v in req.items() if k in rep]
    return dict(requests=len(req), replies=len(vals), min_ms=min(vals) if vals else None,
                median_ms=statistics.median(vals) if vals else None, max_ms=max(vals) if vals else None)


def captures():
    logrows=json.loads((OUT/'flood.json').read_text(encoding='utf-8'))
    bysid={r['session']:r for r in logrows}
    results=[]
    capdir=OUT/'captures'
    capdir.mkdir(exist_ok=True)
    clients=sorted((ROOT/'WireShark抓包').glob('*_Client.pcapng'))
    assert len(clients)*2==len(list((ROOT/'WireShark抓包').glob('*.pcapng')))
    for i,client_path in enumerate(clients,1):
        server_path=client_path.with_name(client_path.name.replace('_Client.pcapng','_Server.pcapng'))
        print(f'PCAP {i}/{len(clients)} {client_path.name}', flush=True)
        c,s=scan(client_path),scan(server_path)
        pair=dict(client=public(c), server=public(s), comparison={})
        for sid in c['streams'].keys() | s['streams'].keys():
            cp=c['streams'].get(sid,{}).get('packets',{})
            sp=s['streams'].get(sid,{}).get('packets',{})
            if not cp or not sp:
                pair['comparison'][sid]=dict(paired=False,client_unique=len(cp),server_unique=len(sp))
                continue
            ends=[m['seq'] for cap in (c,s) for m in cap['metadata'] if m['tag']=='E' and m['session']==sid and m['timestamp_value']==ord('F')]
            assert ends and len(set(ends))==1, sid
            expected=ends[0]
            assert sid in bysid and expected==bysid[sid]['expected'], sid
            mismatch=sum(cp[q][1]!=sp[q][1] for q in cp.keys() & sp.keys())
            assert mismatch==0
            neither=set(range(1,expected+1))-(cp.keys()|sp.keys())
            cd=sum(t.get('dropped',0) for t in c['interface_statistics'])
            sd=sum(t.get('dropped',0) for t in s['interface_statistics'])
            prefix=min(sp)-1
            if prefix:
                # The client can capture a submitted UDP packet before ARP has
                # completed. Use the receiver prefix, not the union of captures.
                assert cd==0 and prefix<=bysid[sid]['missing']
                assert bysid[sid]['loss_changes'][0]['count']>=prefix
            row=dict(brand=bysid[sid]['brand'],port_a=bysid[sid]['port_a'],port_b=bysid[sid]['port_b'],
                     round=bysid[sid]['round'],session=sid,expected=expected,
                     app_received=bysid[sid]['received'],app_missing=bysid[sid]['missing'],
                     client_unique=len(cp),server_unique=len(sp),client_first=min(cp),server_first=min(sp),
                     client_only=len(cp.keys()-sp.keys()),server_only=len(sp.keys()-cp.keys()),
                     neither_count=len(neither),neither_ranges=ranges(neither),
                     startup_prefix=prefix,other_missing=bysid[sid]['missing']-prefix,
                     client_drop=cd,server_drop=sd,
                     app_minus_server_capture=bysid[sid]['received']-len(sp),
                     mismatched_send_timestamps=mismatch,warm=warm_stats(c,sid),
                     client_truncated=c['truncated_packets'],server_truncated=s['truncated_packets'],
                     client_file=str(client_path.relative_to(ROOT)),server_file=str(server_path.relative_to(ROOT)),
                     client_bytes=c['bytes'],server_bytes=s['bytes'])
            assert row['other_missing']>=0
            pair['comparison'][sid]=row
            results.append(row)
            print(f'  {sid} app_missing={row["app_missing"]} startup={prefix} capture_drop={sd}', flush=True)
        (capdir/(client_path.stem.replace('_Client','')+'.json')).write_text(json.dumps(pair,ensure_ascii=False,indent=2),encoding='utf-8')
        del c,s,pair,cp,sp,neither
        gc.collect()
        dump('capture_summary.json',results)
    assert len({r['session'] for r in results})==len(results)
    csv_out('capture_summary.csv',results)


def summary():
    e=json.loads((OUT/'echo.json').read_text(encoding='utf-8'))
    f=json.loads((OUT/'flood.json').read_text(encoding='utf-8'))
    result={'echo':{},'flood':{}}
    for b in ('DIRECT',*BRANDS):
        result['echo'][b]={}
        for dur in (60,600):
            rs=[r for r in e['rounds'] if r['brand']==b and r['duration_s']==dur]
            result['echo'][b][str(dur)]=dict(rounds=len(rs),n=sum(r['n'] for r in rs),late=sum(r['late'] for r in rs),
                unreturned=sum(r['unreturned'] for r in rs),send_failed=sum(r['send_failed'] for r in rs),
                statuses=dict(Counter(r['verdict'] for r in rs)),max_received_ms=max((r['max_received_ms'] for r in rs),default=None),
                max_late_run=max((r['longest_late'] for r in rs),default=0),
                quantiles_by_frame={str(frame):{k:statistics.median(r[k] for r in rs if r['frame_bytes']==frame)
                    for k in ('p50_ms','p90_ms','p99_ms')} for frame in (64,512,1518) if any(r['frame_bytes']==frame for r in rs)})
    for b in BRANDS:
        rs=[r for r in f if r['brand']==b]
        standard=[r for r in rs if r['target_mbps']==100 and r['frame_bytes']==1518 and r['nominal_duration_s']==60]
        first={}
        for r in sorted(standard,key=lambda r:r['stamp']):
            first.setdefault((r['port_a'],r['port_b']),r)
        for name, data in [('all',rs),('100M_all',standard),('100M_first_per_pair',list(first.values()))]:
            result['flood'].setdefault(b,{})[name]=dict(rounds=len(data),with_missing=sum(r['missing']>0 for r in data),
                zero_missing=sum(r['missing']==0 for r in data),expected=sum(r['expected'] for r in data),
                missing=sum(r['missing'] for r in data),port_pairs=sorted(f'{r["port_a"]}-{r["port_b"]}' for r in data),
                aggregate_missing_pct=sum(r['missing'] for r in data)/sum(r['expected'] for r in data)*100)
    firsts={}
    for b in BRANDS:
        firsts[b]={}
        for r in sorted(f,key=lambda r:r['stamp']):
            if r['brand']==b and r['target_mbps']==100:
                firsts[b].setdefault((r['port_a'],r['port_b']),r)
    common=sorted(firsts['WAGO'].keys() & firsts['Weidmuller'].keys())
    result['flood_common_first']={b:dict(rounds=len(common),with_missing=sum(firsts[b][k]['missing']>0 for k in common),
        expected=sum(firsts[b][k]['expected'] for k in common),missing=sum(firsts[b][k]['missing'] for k in common),
        pairs=[list(k) for k in common]) for b in BRANDS}
    dump('summary.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


def refine_captures():
    """Refine the prefix classification from the saved raw reparse results."""
    rows=json.loads((OUT/'capture_summary.json').read_text(encoding='utf-8'))
    f={r['session']:r for r in json.loads((OUT/'flood.json').read_text(encoding='utf-8'))}
    for r in rows:
        prefix=r['server_first']-1
        if prefix:
            assert r['client_drop']==0 and prefix<=r['app_missing']
            assert f[r['session']]['loss_changes'][0]['count']>=prefix
        r['startup_prefix']=prefix
        r['other_missing']=r['app_missing']-prefix
    dump('capture_summary.json',rows)
    csv_out('capture_summary.csv',rows)
    for p in (OUT/'captures').glob('*.json'):
        d=json.loads(p.read_text(encoding='utf-8'))
        for r in rows:
            if r['session'] in d['comparison'] and d['comparison'][r['session']].get('expected'):
                d['comparison'][r['session']]=r
        p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
    old=json.loads((ROOT/'out/analysis_all_FLOOD_20261026/summary.json').read_text(encoding='utf-8'))
    for r in old['rows']:
        if 'capture' not in r:
            continue
        n=next(x for x in rows if x['session']==r['session'])
        assert n['startup_prefix']==r['startup_prefix']
        for oldkey,newkey in [('client_unique','client_unique'),('server_unique','server_unique'),('server_drop','server_drop')]:
            assert n[newkey]==r['capture'][oldkey]
    print('Raw reparse agrees with the 13 previously paired sessions; added A04 R02.',flush=True)


def audit():
    e=json.loads((OUT/'echo.json').read_text(encoding='utf-8'))
    f=json.loads((OUT/'flood.json').read_text(encoding='utf-8'))
    paths={ROOT/r['file'] for r in e['rounds']} | {ROOT/r['file'] for r in f}
    paths|={p.with_suffix('.log') for p in list(paths) if p.suffix=='.csv'}
    paths|={Path(str(p)+'.late.csv') for p in list(paths) if p.suffix=='.csv' and Path(str(p)+'.late.csv').exists()}
    paths|=set((ROOT/'WireShark抓包').glob('*.pcapng')) | set((ROOT/'out/BATCH').glob('*.log'))
    paths|={ROOT/'报告/交换机转发可靠性对比试验报告_V0.2_判定规则修订.docx',
            ROOT/'报告/FLOOD全部未到达排查汇总_20261026.md',ROOT/'SwitchTest.py',ROOT/'switch_core.py',
            ROOT/'analyze_flood_capture.py',ROOT/'analyze_replacement.py'}
    rows=[]
    for p in sorted(paths):
        before=p.stat()
        with p.open('rb') as stream:
            sha=hashlib.file_digest(stream,'sha256').hexdigest()
        after=p.stat()
        assert before.st_size==after.st_size and before.st_mtime_ns==after.st_mtime_ns, p
        rows.append(dict(file=str(p.relative_to(ROOT)),bytes=before.st_size,mtime_ns=before.st_mtime_ns,sha256=sha))
    dump('source_manifest.json',rows)
    print(f'Audited {len(rows)} source files, {sum(r["bytes"] for r in rows):,} bytes.',flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--phase',choices=['all','echo','flood','captures','summary','refine','audit'],default='all')
    args=ap.parse_args()
    OUT.mkdir(exist_ok=True)
    if args.phase in ('all','echo'):
        echo()
    if args.phase in ('all','flood'):
        flood()
    if args.phase in ('all','captures'):
        captures()
    if args.phase in ('all','refine'):
        refine_captures()
    if args.phase in ('all','summary'):
        summary()
    if args.phase in ('all','audit'):
        audit()
