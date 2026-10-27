"""Local-only controlled experiments; does not change production measurement code."""
import contextlib
import io
import json
import multiprocessing as mp
from pathlib import Path
import socket
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import SwitchTest as app
from switch_core import TAG_ECHO


def responder(conn, pause):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1', 0))
        sock.settimeout(.1)
        conn.send(sock.getsockname()[1])
        count = 0
        while not conn.poll():
            try:
                data, peer = sock.recvfrom(65535)
            except socket.timeout:
                continue
            if data[:1] == TAG_ECHO:
                count += 1
                if pause and count == 500:
                    time.sleep(.08)
            sock.sendto(data[:17], peer)


class PauseRead:
    def __init__(self, sock):
        self.sock = sock
        self.count = 0
        self.pause = False

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def sendto(self, data, peer):
        if data[:1] == TAG_ECHO:
            self.count += 1
            self.pause = self.count == 500
        return self.sock.sendto(data, peer)

    def recvfrom(self, size):
        if self.pause:
            self.pause = False
            time.sleep(.08)
        return self.sock.recvfrom(size)


class PauseOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.paused = False

    def write(self, text):
        if not self.paused and '已运行' in text:
            self.paused = True
            time.sleep(.08)
        return super().write(text)


def experiment(mode):
    parent, child = mp.Pipe()
    process = mp.Process(target=responder, args=(child, mode == 'server_pause'))
    process.start()
    original = app._open_sender
    try:
        if not parent.poll(10):
            raise RuntimeError('Local responder did not start')
        port = parent.recv()
        if mode == 'client_pause':
            def wrapped(cfg):
                sock, addr = original(cfg)
                return PauseRead(sock), addr
            app._open_sender = wrapped
        cfg = dict(host='127.0.0.1', bind='127.0.0.1', port=port,
                   frame=64, interval=.001, duration=3, timeout=.05,
                   warmup=0, drain=.5, wid=0)
        cpu_start = time.process_time()
        capture = PauseOutput() if mode == 'output_pause' else io.StringIO()
        with contextlib.redirect_stdout(capture):
            result = app._echo_core(cfg)
        cpu_seconds = time.process_time() - cpu_start
        gaps = [1000*(b-a) for a, b in zip(result['send_s'], result['send_s'][1:])]
        return dict(mode=mode, sent=result['sent'], on_time=result['recv'],
                    late=len(result['late']), unreturned=result['status'].count('unreturned'),
                    send_failed=result['snd_fail'], duration_s=result['dur'],
                    cpu_seconds=cpu_seconds, cpu_one_core_pct=cpu_seconds/result['dur']*100,
                    max_send_gap_ms=max(gaps), late_rtt_ms=[rtt/1000 for _, rtt in result['late']],
                    late_sequences=[seq for seq, _ in result['late']])
    finally:
        app._open_sender = original
        parent.send('stop')
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join()
        parent.close()
        child.close()


if __name__ == '__main__':
    mp.freeze_support()
    results = [experiment(mode) for mode in ('baseline', 'client_pause', 'server_pause', 'output_pause')]
    if results[0]['on_time'] == 0:
        raise RuntimeError('No loopback baseline replies: environment invalid; results not saved')
    output = ROOT / 'out' / 'endpoint_diagnosis_20261014'
    output.mkdir(exist_ok=True)
    (output / 'controlled_experiments.json').write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False, indent=2))
