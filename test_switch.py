import contextlib
import csv
import io
import multiprocessing as mp
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

import SwitchTest as app
from switch_core import EchoLedger, ReceiveLedger, HDR, TAG_ECHO, TAG_FLOOD, TAG_END, TAG_ACK

ROOT = Path(__file__).parent


def crash_worker():
    os._exit(7)


class StateTests(unittest.TestCase):
    def test_missing_ids_across_bitmap_pages_and_late_delivery(self):
        ledger = ReceiveLedger()
        for seq in (1, 3, 8, 8192, 8194, 16385):
            ledger.add(seq)
        ledger.add(3)
        ledger.finish(16388)
        ledger.add(2)  # Delivered after END, before the final snapshot.
        expected = sorted(set(range(1, 16389)) - {1, 2, 3, 8, 8192, 8194, 16385})
        self.assertEqual(list(ledger.missing_sequences()), expected)
        self.assertEqual(len(expected), ledger.missing)
        self.assertEqual(list(ReceiveLedger().missing_sequences()), [])

    def test_missing_ids_without_end_and_zero_total(self):
        ledger = ReceiveLedger()
        ledger.add(3)
        self.assertEqual(list(ledger.missing_sequences()), [1, 2])
        ledger.finish(5)
        self.assertEqual(list(ledger.missing_sequences()), [1, 2, 4, 5])
        empty = ReceiveLedger()
        empty.finish(0)
        self.assertEqual(list(empty.missing_sequences()), [])

    def test_missing_export_metadata_session_isolation_and_no_overwrite(self):
        incomplete, complete, echo = ReceiveLedger(), ReceiveLedger(), ReceiveLedger()
        incomplete.add(3)
        complete.add(1)
        complete.finish(1)
        senders = {(TAG_FLOOD, ('127.0.0.1', 1001), 7): incomplete,
                   (TAG_FLOOD, ('127.0.0.1', 1002), 7): complete,
                   (TAG_ECHO, ('127.0.0.1', 1001), 8): echo}
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(io.StringIO()):
                files = app._export_missing(senders, str(Path(directory)/'中文_server.log'))
            self.assertEqual(len(files), 2)
            with files[0].open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([int(r['missing_seq']) for r in rows], [1, 2])
            self.assertEqual(rows[0]['session'], '00000007')
            self.assertEqual(rows[0]['basis'], 'observed_prefix_tail_unknown')
            self.assertEqual(rows[0]['expected_total'], '')
            with files[1].open(encoding='utf-8-sig', newline='') as f:
                self.assertEqual(list(csv.DictReader(f)), [])
            original = files[0].read_bytes()
            with self.assertRaises(FileExistsError):
                app._export_missing(senders, str(Path(directory)/'中文_server.log'))
            self.assertEqual(files[0].read_bytes(), original)

    def test_tail_loss_and_duplicates(self):
        ledger = ReceiveLedger()
        for seq in range(1, 901):
            ledger.add(seq)
        ledger.add(2)
        self.assertEqual(ledger.missing, 0)
        self.assertIsNone(ledger.expected)
        ledger.finish(1000)
        self.assertEqual(ledger.missing, 100)
        self.assertEqual(ledger.duplicates, 1)
        self.assertEqual(app._sender_loss({1: ledger}), (10, 100, 1, 0))
        self.assertFalse(ledger.add(1001))

    def test_reordering_after_end(self):
        ledger = ReceiveLedger()
        ledger.add(4)
        ledger.finish(4)
        for seq in (1, 2, 2):
            ledger.add(seq)
        self.assertEqual(ledger.missing, 1)
        ledger.add(3)
        self.assertEqual(ledger.missing, 0)
        with self.assertRaises(ValueError):
            ledger.finish(5)

    def test_timeout_same_in_active_and_drain(self):
        ledger = EchoLedger(50_000_000)
        ledger.sent(0, True)
        ledger.sent(10_000_000, True)
        ledger.expire(60_000_000)
        # First already expired, second is at the exact deadline.
        self.assertTrue(ledger.receive(1, 0, 80_000_000))
        self.assertTrue(ledger.receive(2, 10_000_000, 90_000_000))
        self.assertEqual(ledger.received, 0)
        self.assertEqual(ledger.late, {1: 80000, 2: 80000})
        self.assertFalse(ledger.receive(1, 0, 100_000_000))

    def test_timestamp_validation_and_failures(self):
        ledger = EchoLedger(50)
        ledger.sent(100, True)
        self.assertFalse(ledger.receive(1, 99, 120))
        self.assertTrue(ledger.receive(1, 100, 150))
        ledger.sent(200, False)
        self.assertFalse(ledger.receive(2, 200, 201))
        self.assertEqual(ledger.status, ['on_time', 'send_failed'])

    def test_actual_outage_time_bounds(self):
        ledger = EchoLedger(50)
        for stamp in (0, 2_000_000, 10_000_000, 20_000_000):
            ledger.sent(stamp, True)
        ledger.receive(1, 0, 10)
        ledger.receive(4, 20_000_000, 20_000_010)
        ledger.finish()
        self.assertEqual(ledger.outage_bounds(), [(2, 2, 8, 20, False)])

    def test_csv_statistics_order_and_incomplete_input(self):
        rows = [(3, 10), (1, 100), (2, 30)]
        self.assertEqual(app.analyze(rows)['jitter'], app.analyze(sorted(rows))['jitter'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.csv'
            path.write_text('worker,seq,rtt_us\n0,1,12\n0,3,10\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                app.load_csv(path)

    def test_worker_hard_crash_does_not_hang_parent(self):
        ctx = mp.get_context('spawn')
        q = ctx.Queue()
        proc = ctx.Process(target=crash_worker)
        proc.start()
        start = time.perf_counter()
        results = list(app._collect_workers([proc], q))
        self.assertLess(time.perf_counter()-start, 5)
        self.assertIn('exitcode=7', results[0][2])

    def test_bandwidth_units(self):
        args = SimpleNamespace(frame=64, bandwidth=1, host='127.0.0.1', port=1)
        result = dict(wid=0, seq=1000, sent_bytes=18000, dur=.672, session=123, confirmed=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            app._report_flood(args, [result], [])
        self.assertIn('估算线速 1.00 Mbps', output.getvalue())
        self.assertIn('达成 100.0%', output.getvalue())


class SocketTests(unittest.TestCase):
    def start_server(self, fast=False, duration=3):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        cmd = [sys.executable, str(ROOT/'SwitchTest.py'), 'server', '--bind', '127.0.0.1',
               '--port', str(port), '--duration', str(duration)]
        if fast:
            cmd.append('--fast')
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        self.addCleanup(cleanup)
        for _ in range(8):
            line = process.stdout.readline()
            if 'v2' in line and '启动' in line:
                break
        self.assertIn('v2', line)
        self.assertIn('启动', line)
        return process, port

    def cfg(self, port):
        return dict(host='127.0.0.1', port=port, frame=64, interval=.01, duration=.12,
                    timeout=.05, warmup=0, bind='127.0.0.1', wid=3)

    def test_echo_and_csv_roundtrip(self):
        process, port = self.start_server(fast=True)
        cfg = self.cfg(port)
        with contextlib.redirect_stdout(io.StringIO()):
            result = app._echo_core(cfg)
        self.assertGreater(result['sent'], 0)
        self.assertEqual(result['recv'], result['sent'])
        self.assertLess(result['dur'], .2)
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory)/'report.csv')
            args = SimpleNamespace(**cfg, jobs=1, out=output)
            with contextlib.redirect_stdout(io.StringIO()):
                app._report_echo(args, [result], [])
            self.assertEqual(app.load_csv(output), [result['records']])
            with open(output, encoding='utf-8-sig') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual({row['worker'] for row in rows}, {'3'})
            self.assertEqual({row['status'] for row in rows}, {'on_time'})

    def test_echo_progress_has_elapsed_and_cumulative_rtt(self):
        process, port = self.start_server()
        cfg = self.cfg(port)
        cfg['duration'] = 1.05
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = app._echo_core(cfg)
        self.assertGreater(result['recv'], 0)
        self.assertIn('已运行 1.0/1.05s', output.getvalue())
        self.assertIn('RTT 累计均', output.getvalue())

    def test_real_delayed_echo_has_no_on_time_tail(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        sock.settimeout(.005)
        stop = threading.Event()
        errors = []
        def respond():
            queued = []
            try:
                while not stop.is_set():
                    try:
                        data, peer = sock.recvfrom(65535)
                        queued.append((time.perf_counter()+.08, data, peer))
                    except socket.timeout:
                        pass
                    while queued and queued[0][0] <= time.perf_counter():
                        _, data, peer = queued.pop(0)
                        sock.sendto(data, peer)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=respond)
        thread.start()
        try:
            cfg = self.cfg(sock.getsockname()[1])
            with contextlib.redirect_stdout(io.StringIO()):
                result = app._echo_core(cfg)
            self.assertEqual(result['recv'], 0)
            self.assertEqual(len(result['late']), result['sent'])
            self.assertTrue(all(rtt >= 80000 for _, rtt in result['late']))
            self.assertLess(result['dur'], .2)
        finally:
            stop.set()
            thread.join(1)
            sock.close()
        self.assertFalse(errors)

    def test_real_tail_loss_session_isolation_and_end_ack(self):
        process, port = self.start_server(duration=.8)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(.5)
            addr = ('127.0.0.1', port)
            for session, seqs, total in ((101, [1, 2, 2, 4], 5), (102, [1, 2], 2)):
                for seq in seqs:
                    sock.sendto(HDR.pack(TAG_FLOOD, seq, session, 0), addr)
                sock.sendto(HDR.pack(TAG_END, total, session, ord('F')), addr)
                self.assertEqual(HDR.unpack(sock.recvfrom(65535)[0]), (TAG_ACK, total, session, ord('F')))
            sock.sendto(HDR.pack(TAG_FLOOD, 3, 101, 0), addr)
        stdout, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn('重复 1, 对账未到达 1/5', stdout)
        self.assertIn('重复 0, 对账未到达 0/2', stdout)
        paths = [Path(line.split('：', 1)[1].split('（', 1)[0])
                 for line in stdout.splitlines() if line.startswith('缺失包号清单：')]
        self.assertEqual(len(paths), 2)
        with paths[0].open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([int(r['missing_seq']) for r in rows], [5])
        self.assertEqual(rows[0]['expected_total'], '5')
        self.assertEqual(rows[0]['basis'], 'reconciled')
        with paths[1].open(encoding='utf-8-sig', newline='') as f:
            self.assertEqual(list(csv.DictReader(f)), [])

    def test_flood_end_metadata(self):
        process, port = self.start_server()
        cfg = self.cfg(port)
        cfg['bandwidth'] = .1
        with contextlib.redirect_stdout(io.StringIO()):
            result = app._flood_core(cfg)
        self.assertTrue(result['confirmed'])
        self.assertGreater(result['seq'], 0)
        self.assertEqual(result['sent_bytes'], result['seq'] * 18)

    def test_invalid_bind_aborts_echo_and_flood(self):
        cfg = self.cfg(5001)
        cfg['bind'] = '192.0.2.123'
        cfg['bandwidth'] = 1
        for func in (app._echo_core, app._flood_core):
            with self.assertRaises(OSError):
                func(cfg)

    def test_cli_invalid_numbers(self):
        for args in (['echo', '--host', '127.0.0.1', '--timeout', 'nan'],
                     ['echo', '--host', '127.0.0.1', '--duration', '0'],
                     ['flood', '--host', '127.0.0.1', '--port', '65536'],
                     ['bench', '--jobs', '1,100']):
            proc = subprocess.run([sys.executable, str(ROOT/'SwitchTest.py'), *args],
                                  capture_output=True, timeout=3)
            self.assertEqual(proc.returncode, 2)

    def test_spawn_multi_echo_and_flood_cli(self):
        process, port = self.start_server(duration=5)
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory)/'multi.csv')
            echo_log = str(Path(directory)/'multi.log')
            common = ['--host', '127.0.0.1', '--port', str(port), '--duration', '.12',
                      '--warmup', '0', '-j', '2']
            echo = subprocess.run([sys.executable, str(ROOT/'SwitchTest.py'), 'echo',
                                   *common, '--out', output, '--log', echo_log], capture_output=True,
                                  text=True, encoding='utf-8', timeout=5)
            self.assertEqual(echo.returncode, 0, echo.stderr + echo.stdout)
            with open(output, encoding='utf-8-sig') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual({row['worker'] for row in rows}, {'0', '1'})
            text = Path(echo_log).read_text(encoding='utf-8-sig')
            self.assertIn('[流0] v2 echo', text)
            self.assertIn('[流1] v2 echo', text)
            self.assertIn('明细已保存', text)
            flood = subprocess.run([sys.executable, str(ROOT/'SwitchTest.py'), 'flood',
                                    *common, '--bandwidth', '.1'], capture_output=True,
                                   text=True, encoding='utf-8', timeout=5)
            self.assertEqual(flood.returncode, 0, flood.stderr + flood.stdout)
            self.assertEqual(flood.stdout.count('结束总数确认：True'), 2)

    def test_failed_multi_cli_exits_nonzero(self):
        proc = subprocess.run([sys.executable, str(ROOT/'SwitchTest.py'), 'echo',
                               '--host', '127.0.0.1', '--bind', '192.0.2.123',
                               '--duration', '.1', '-j', '2'], capture_output=True,
                              text=True, encoding='utf-8', timeout=5)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('所有 echo 流均失败', proc.stderr)


if __name__ == '__main__':
    unittest.main()
