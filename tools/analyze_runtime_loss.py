"""Inspect runtime loss windows, retaining the distinction between capture and app loss."""
import argparse
import json
import re
import statistics
from datetime import datetime
from pathlib import Path
from analyze_flood_capture import scan, ranges, local_time


def spacing(packets, low, high, column):
    values = [packets[q][column] for q in sorted(packets) if low <= q <= high]
    differences = [(b-a)/1e6 for a,b in zip(values, values[1:])]
    return dict(max_gap_ms=max(differences, default=0),
                median_gap_ms=statistics.median(differences) if differences else None,
                gaps_over_1ms=sum(x>1 for x in differences),
                gaps_over_10ms=sum(x>10 for x in differences))


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('session'); args=ap.parse_args()
    summary=json.loads(Path('out/analysis_all_FLOOD_20261026/summary.json').read_text(encoding='utf-8'))
    row=next(r for r in summary['rows'] if r['session']==args.session)
    cached=json.loads(Path(row['capture']['analysis']).read_text(encoding='utf-8'))
    client=scan(Path(cached['client']['path'])); server=scan(Path(cached['server']['path']))
    cp=client['streams'][args.session]['packets']; sp=server['streams'][args.session]['packets']
    absent=set(range(1,row['expected']+1))-sp.keys()
    events=[]; prev=None
    for line in Path(row['log']).read_text(encoding='utf-8-sig').splitlines():
        m=re.search(r'^\[([^]]+)\].*收唯一包 ([\d,]+).*缺口/未到达 ([\d,]+).*未对账流 (\d+)',line)
        if not m: continue
        t,u,l,unknown=m.groups(); u=int(u.replace(',','')); l=int(l.replace(',',''))
        cur=(t,u,l,int(unknown))
        if prev and l>prev[2] and (prev[1]>0 or row['startup_prefix']==0):
            low=prev[1]+prev[2]+1; high=u+l
            candidates=sorted(q for q in absent if low<=q<=high)
            segments=ranges(candidates); delta=l-prev[2]
            suspicious=[(a,b) for a,b in segments if b-a+1==delta or b-a+1>=20]
            details=[]
            for a,b in suspicious[:30]:
                if a-1 not in cp or b+1 not in cp or a-1 not in sp or b+1 not in sp: continue
                cs=(cp[b+1][0]-cp[a-1][0])/1e6
                ss=(sp[b+1][0]-sp[a-1][0])/1e6
                details.append(dict(start=a,end=b,count=b-a+1,
                    client_before=local_time(cp[a-1][0]),client_after=local_time(cp[b+1][0]),
                    server_before=local_time(sp[a-1][0]),server_after=local_time(sp[b+1][0]),
                    client_capture_span_ms=cs, server_capture_span_ms=ss,
                    client_embedded_span_ms=(cp[b+1][1]-cp[a-1][1])/1e6,
                    client_segment_spacing=spacing(cp,a-1,b+1,1),
                    additional_server_span_ms=ss-cs))
            day=datetime.strptime(row['date'],'%Y%m%d').date().isoformat()
            start_ns=int(datetime.fromisoformat(day+'T'+prev[0]+'+08:00').timestamp()*1e9)
            end_ns=int(datetime.fromisoformat(day+'T'+t+'+08:00').timestamp()*1e9)
            common=[q for q in cp.keys() & sp.keys() if low<=q<=high]
            offsets=[(sp[q][0]-cp[q][0])/1e6 for q in common]
            event=dict(log_start=prev[0],log_end=t,app_missing_increment=delta,
                       inferred_seq_low=low,inferred_seq_high=high,
                       final_receiver_capture_absent_count=len(candidates),candidate_ranges=segments,
                       client_embedded_spacing=spacing(cp,low,high,1),
                       client_capture_spacing=spacing(cp,low,high,0),
                       receiver_capture_spacing=spacing(sp,low,high,0),
                       matched_capture_offset_range_ms=max(offsets)-min(offsets) if offsets else None,
                       receiver_arp_in_log_window=[a for a in server['arp'] if start_ns<=a['capture_ns']<=end_ns],
                       candidate_boundaries=details)
            events.append(event)
        prev=cur
    result=dict(brand=row['brand'],port=row['port_a'],round=row['round'],session=args.session,
                caveats=['Sequence windows use a single FLOOD flow and observed-max progress before END; not exact per-packet application receipts.',
                         'Missing receiver capture IDs may be capture drops; candidate boundaries are not proven application loss IDs.',
                         'Offset ranges and differences of spans cancel fixed clock offsets but still include clock drift and capture timing effects.'],events=events,
                client_whole_embedded_spacing=spacing(cp,min(cp),max(cp),1))
    out=Path('out/runtime_loss_analysis_20261027');out.mkdir(exist_ok=True)
    (out/f'{args.session}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(brand=row['brand'],port=row['port_a'],session=args.session,events=[{k:v for k,v in e.items() if k not in ('candidate_ranges','candidate_boundaries','receiver_arp_in_log_window')}|{'arp_count':len(e['receiver_arp_in_log_window']),'notable_boundaries':e['candidate_boundaries'][:2]} for e in events]),ensure_ascii=False))


if __name__=='__main__':main()
