"""Stream pcapng locally; compare FLOOD session/sequence, never dump packet bodies."""
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import csv
import json
from pathlib import Path
import struct

TZ = timezone(timedelta(hours=8))
HDR = struct.Struct('!cIIQ')


def opts(data, endian):
    pos = 0
    while pos + 4 <= len(data):
        code, size = struct.unpack_from(endian + 'HH', data, pos)
        pos += 4
        if code == 0:
            break
        if pos + size > len(data):
            raise ValueError('Truncated pcapng option')
        yield code, data[pos:pos + size]
        pos += (size + 3) & ~3


def local_time(ns):
    return datetime.fromtimestamp(ns / 1e9, TZ).isoformat(timespec='microseconds')


def scan(path):
    interfaces, streams, metadata, statistics, arp = [], {}, [], [], []
    count = truncated = 0
    first = last = None
    protocols = Counter()
    endian = '<'
    with path.open('rb') as f:
        while True:
            head = f.read(8)
            if not head:
                break
            if len(head) != 8:
                raise ValueError('Incomplete block header')
            if head[:4] == b'\x0a\x0d\x0d\x0a':
                bom = f.read(4)
                endian = '<' if bom == b'\x4d\x3c\x2b\x1a' else '>'
                size = struct.unpack(endian + 'I', head[4:])[0]
                tail = f.read(size - 12)
                data = bom + tail[:-4]
                typ = 0x0a0d0d0a
            else:
                typ, size = struct.unpack(endian + 'II', head)
                if size < 12 or size > 100_000_000:
                    raise ValueError('Invalid block length')
                tail = f.read(size - 8)
                data = tail[:-4]
            if len(tail) < 4 or struct.unpack(endian + 'I', tail[-4:])[0] != size:
                raise ValueError('Truncated block or mismatched trailer')
            if typ == 1:
                link, _, snaplen = struct.unpack_from(endian + 'HHI', data)
                item = {'linktype': link, 'snaplen': snaplen, 'resolution': 1e-6}
                for code, value in opts(data[8:], endian):
                    if code in (2, 3, 11):
                        item[{2:'device', 3:'description', 11:'filter'}[code]] = value.decode('utf-8', 'replace')
                    elif code == 9:
                        item['resolution'] = 2 ** -(value[0] & 127) if value[0] & 128 else 10 ** -value[0]
                interfaces.append(item)
            elif typ == 5:
                iid, hi, lo = struct.unpack_from(endian + 'III', data)
                item = {'interface': iid}
                names = {4:'received', 5:'dropped', 6:'filter_accepted', 7:'os_dropped', 8:'delivered'}
                for code, value in opts(data[12:], endian):
                    if code in names and len(value) == 8:
                        item[names[code]] = struct.unpack(endian + 'Q', value)[0]
                statistics.append(item)
            elif typ == 6:
                iid, hi, lo, caplen, wirelen = struct.unpack_from(endian + 'IIIII', data)
                packet = data[20:20 + caplen]
                if len(packet) != caplen:
                    raise ValueError('Truncated EPB packet')
                ns = round(((hi << 32) | lo) * interfaces[iid]['resolution'] * 1e9)
                count += 1
                truncated += caplen < wirelen
                first = ns if first is None else min(first, ns)
                last = ns if last is None else max(last, ns)
                if interfaces[iid]['linktype'] != 1 or len(packet) < 14:
                    protocols['unsupported link'] += 1
                    continue
                etype = struct.unpack_from('!H', packet, 12)[0]
                off = 14
                while etype in (0x8100, 0x88a8) and len(packet) >= off + 4:
                    etype = struct.unpack_from('!H', packet, off + 2)[0]
                    off += 4
                if etype == 0x0806 and len(packet) >= off + 28:
                    raw = packet[off:]
                    hardware, protocol, hlen, plen, operation = struct.unpack_from('!HHBBH', raw)
                    if hlen == 6 and plen == 4:
                        arp.append({'time':local_time(ns), 'capture_ns':ns, 'operation':operation,
                            'sender_mac':raw[8:14].hex(':'), 'sender_ip':'.'.join(map(str,raw[14:18])),
                            'target_mac':raw[18:24].hex(':'), 'target_ip':'.'.join(map(str,raw[24:28]))})
                if etype != 0x0800 or len(packet) < off + 20:
                    protocols[hex(etype)] += 1
                    continue
                ip = packet[off:]
                ihl = (ip[0] & 15) * 4
                frag = struct.unpack_from('!H', ip, 6)[0]
                if ip[9] != 17 or frag & 0x1fff or len(ip) < ihl + 8:
                    protocols['IPv4 protocol ' + str(ip[9]) + (' fragment' if frag & 0x1fff else '')] += 1
                    continue
                sport, dport, ulen, _ = struct.unpack_from('!HHHH', ip, ihl)
                if 5001 not in (sport, dport):
                    protocols['other UDP'] += 1
                    continue
                payload = ip[ihl + 8:]
                if len(payload) < HDR.size:
                    protocols['short test UDP'] += 1
                    continue
                tag, seq, session, stamp = HDR.unpack_from(payload)
                protocols['test ' + tag.decode('ascii', 'replace')] += 1
                key = f'{session:08x}'
                if tag != b'F':
                    metadata.append({'tag':tag.decode('ascii','replace'), 'seq':seq,
                                     'session':key, 'timestamp_value':stamp, 'capture_ns':ns,
                                     'source_ip':'.'.join(map(str,ip[12:16])),
                                     'destination_ip':'.'.join(map(str,ip[16:20])),
                                     'source_port':sport, 'destination_port':dport,
                                     'source_mac':packet[6:12].hex(':'),
                                     'destination_mac':packet[:6].hex(':')})
                    continue
                if key not in streams:
                    streams[key] = {'packets':{}, 'duplicates':0, 'out_of_order':0,
                                    'max_seen':0, 'sport':sport, 'dport':dport,
                                    'source_ip':'.'.join(map(str,ip[12:16])),
                                    'destination_ip':'.'.join(map(str,ip[16:20])),
                                    'source_mac':packet[6:12].hex(':'),
                                    'destination_mac':packet[:6].hex(':')}
                stream = streams[key]
                if seq < stream['max_seen']:
                    stream['out_of_order'] += 1
                stream['max_seen'] = max(stream['max_seen'], seq)
                if seq in stream['packets']:
                    stream['duplicates'] += 1
                else:
                    stream['packets'][seq] = (ns, stamp, struct.unpack_from('!H', ip, 4)[0])
    return {'path':str(path), 'bytes':path.stat().st_size, 'packet_count':count,
            'truncated_packets':truncated, 'first_time':local_time(first) if first else None,
            'last_time':local_time(last) if last else None,
            'interfaces':interfaces, 'interface_statistics':statistics,
            'protocols':dict(protocols), 'streams':streams, 'metadata':metadata, 'arp':arp}


def ranges(values):
    result = []
    for value in sorted(values):
        if result and result[-1][1] + 1 == value:
            result[-1][1] = value
        else:
            result.append([value, value])
    return result


def public(capture):
    result = {k:v for k,v in capture.items() if k not in ('streams', 'metadata')}
    result['metadata'] = capture['metadata']
    result['streams'] = {}
    for key, stream in capture['streams'].items():
        packets = stream['packets']
        seq = sorted(packets)
        missing = set(range(1, seq[-1] + 1)) - packets.keys()
        result['streams'][key] = {'unique':len(seq), 'min_seq':seq[0], 'max_seq':seq[-1],
            'duplicates':stream['duplicates'], 'out_of_order':stream['out_of_order'],
            **{k:stream[k] for k in ('sport','dport','source_ip','destination_ip','source_mac','destination_mac')},
            'first_packet_time':local_time(min(p[0] for p in packets.values())),
            'last_packet_time':local_time(max(p[0] for p in packets.values())),
            'unseen_through_max':len(missing), 'unseen_ranges':ranges(missing)}
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('client', type=Path)
    ap.add_argument('server', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    client, server = scan(args.client), scan(args.server)
    result = {'client':public(client), 'server':public(server), 'comparison':{}}
    for key in client['streams'].keys() | server['streams'].keys():
        cp = client['streams'].get(key, {}).get('packets', {})
        sp = server['streams'].get(key, {}).get('packets', {})
        ends = [m['seq'] for cap in (client, server) for m in cap['metadata']
                if m['session'] == key and m['tag'] == 'E' and m['timestamp_value'] == ord('F')]
        if ends and len(set(ends)) != 1:
            raise ValueError('Conflicting end totals')
        expected = ends[0] if ends else max([*cp, *sp], default=0)
        only_client, only_server = cp.keys() - sp.keys(), sp.keys() - cp.keys()
        neither = set(range(1, expected + 1)) - (cp.keys() | sp.keys())
        cmp = {'paired_in_both_captures':bool(cp) and bool(sp),
               'scope_note':('Matched session appears in both captures; check coverage and capture drops.'
                             if cp and sp else 'Session appears in only one input capture. These differences do not establish network loss; verify capture coverage and case identity.'),
               'expected':expected, 'end_total_observed':bool(ends), 'client_unique':len(cp),
               'server_unique':len(sp), 'client_only_count':len(only_client),
               'server_only_count':len(only_server), 'neither_count':len(neither),
               'client_only_ranges':ranges(only_client), 'server_only_ranges':ranges(only_server),
               'neither_ranges':ranges(neither),
               'mismatched_send_timestamps':sum(cp[q][1] != sp[q][1] for q in cp.keys() & sp.keys())}
        stamp0 = min((v[1] for v in list(cp.values()) + list(sp.values())), default=0)
        bins = {}
        for label, packets in [('client',cp), ('server',sp)]:
            counts = Counter(int((v[1] - stamp0)/1e9) for v in packets.values())
            bins[label] = dict(sorted(counts.items()))
        cmp['packets_by_embedded_send_second'] = bins
        result['comparison'][key] = cmp
        with (args.out / f'{key}_differences.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['seq','category','client_capture_time','server_capture_time','embedded_send_relative_s'])
            for seq in sorted(only_client | only_server | neither):
                c, s = cp.get(seq), sp.get(seq)
                v = c or s
                writer.writerow([seq,'client_only' if c else 'server_only' if s else 'neither',
                    local_time(c[0]) if c else '', local_time(s[0]) if s else '',
                    (v[1]-stamp0)/1e9 if v else ''])
        # Useful boundary evidence without exposing the entire packet stream.
        boundaries = {1, expected}
        for a,b in ranges(only_client) + ranges(only_server) + ranges(neither):
            boundaries.update((a-1,a,b,b+1))
        cmp['boundary_samples'] = [{'seq':q,
            'client_capture_time':local_time(cp[q][0]) if q in cp else None,
            'server_capture_time':local_time(sp[q][0]) if q in sp else None,
            'send_relative_s':((cp.get(q) or sp[q])[1]-stamp0)/1e9 if q in cp or q in sp else None}
            for q in sorted(boundaries) if 1 <= q <= expected][:100]
    (args.out / 'analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    brief = {'client':{k:v for k,v in result['client'].items() if k not in ('metadata','streams','arp')},
             'server':{k:v for k,v in result['server'].items() if k not in ('metadata','streams','arp')},
             'sessions':{key:{k:v for k,v in cmp.items() if not k.endswith('_ranges') and k not in
                         ('boundary_samples','packets_by_embedded_send_second')} for key,cmp in result['comparison'].items()}}
    print(json.dumps(brief, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
