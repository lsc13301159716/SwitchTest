"""Protocol and measurement state, independent of CLI and socket I/O."""
import struct
import secrets

# Version 2: 17 bytes still fit the 18-byte UDP payload of a 64-byte frame.
HDR = struct.Struct("!cIIQ")
TAG_ECHO, TAG_FLOOD, TAG_WARM = b"S", b"F", b"W"
TAG_END, TAG_ACK = b"E", b"A"


def new_session():
    return secrets.randbits(32)


class ReceiveLedger:
    """Sparse bitmap: duplicates cannot conceal loss; one bit per sequence."""
    def __init__(self):
        self.pages = {}
        self.maximum = self.unique = self.duplicates = 0
        self.expected = None

    def add(self, seq):
        if seq < 1 or (self.expected is not None and seq > self.expected):
            return False
        page, bit = divmod(seq - 1, 8192)
        buf = self.pages.get(page)
        if buf is None:
            buf = self.pages[page] = bytearray(1024)
        byte, offset = divmod(bit, 8)
        mask = 1 << offset
        if buf[byte] & mask:
            self.duplicates += 1
            return False
        buf[byte] |= mask
        self.unique += 1
        self.maximum = max(seq, self.maximum)
        return True

    def finish(self, expected):
        if expected < self.maximum:
            raise ValueError("final count is smaller than observed sequence")
        if self.expected is not None and expected != self.expected:
            raise ValueError("conflicting final count")
        self.expected = expected

    @property
    def missing(self):
        return (self.expected if self.expected is not None else self.maximum) - self.unique

    def missing_sequences(self):
        """Yield missing IDs at the final snapshot, including reconciled tail loss.

        Without END, only the observed prefix is knowable. Iterate the existing
        bitmap without allocating a set proportional to the sequence space.
        """
        limit = self.expected if self.expected is not None else self.maximum
        for base in range(0, limit, 8192):
            page = self.pages.get(base // 8192)
            count = min(8192, limit - base)
            if page is None:
                yield from range(base + 1, base + count + 1)
                continue
            for offset in range(0, count, 8):
                bits = page[offset // 8]
                if bits == 255:
                    continue
                for bit in range(min(8, count - offset)):
                    if not bits & (1 << bit):
                        yield base + offset + bit + 1


class EchoLedger:
    """Single deadline rule for active measurement and final drain."""
    def __init__(self, timeout_ns):
        self.timeout_ns = timeout_ns
        self.records = []
        self.sent_ns = []
        self.status = []
        self.pending = {}  # insertion order = send order
        self.late = {}
        self.rtt_sum = 0.0
        self.received = 0

    def sent(self, timestamp, success):
        seq = len(self.records) + 1
        self.records.append([seq, None])
        self.sent_ns.append(timestamp)
        self.status.append("pending" if success else "send_failed")
        if success:
            self.pending[seq] = timestamp
        return seq

    def expire(self, now):
        while self.pending:
            seq = next(iter(self.pending))
            if now - self.pending[seq] <= self.timeout_ns:
                break
            self.pending.pop(seq)
            self.status[seq - 1] = "unreturned"

    def receive(self, seq, timestamp, now):
        if not 1 <= seq <= len(self.records):
            return False
        i = seq - 1
        if timestamp != self.sent_ns[i] or self.status[i] not in ("pending", "unreturned"):
            return False
        self.pending.pop(seq, None)
        rtt = (now - timestamp) / 1000.0
        if now - timestamp > self.timeout_ns:
            self.status[i] = "late"
            self.late[seq] = rtt
        else:
            self.status[i] = "on_time"
            self.records[i][1] = rtt
            self.received += 1
            self.rtt_sum += rtt
        return True

    def finish(self):
        for seq in self.pending:
            self.status[seq - 1] = "unreturned"
        self.pending.clear()

    def outage_bounds(self):
        """Probe failure runs, bounded by actual adjacent send timestamps."""
        result = []
        i = 0
        while i < len(self.records):
            if self.records[i][1] is not None:
                i += 1
                continue
            start = i
            while i < len(self.records) and self.records[i][1] is None:
                i += 1
            if i - start < 2:
                continue
            lower = (self.sent_ns[i - 1] - self.sent_ns[start]) / 1e6
            upper = None
            if start > 0 and i < len(self.records):
                upper = (self.sent_ns[i] - self.sent_ns[start - 1]) / 1e6
            result.append((start + 1, i - start, lower, upper, i == len(self.records)))
        return result
