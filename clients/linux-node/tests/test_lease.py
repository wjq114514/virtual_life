from virtual_life_linux.lease import BootTimeClock, LeaseGate


class FakeClock:
    def __init__(self, now_ns: int):
        self.value = now_ns

    def now_ns(self) -> int:
        return self.value


def test_deadline_uses_request_start_not_response_time():
    gate = LeaseGate(safety_margin_ms=250, clock=FakeClock(0))
    start = 1_000 * 1_000_000  # 1s
    auth = gate.authorize(
        start,
        expires_at="2026-01-01T00:00:10Z",
        server_time="2026-01-01T00:00:00Z",
        ttl_seconds=15,
    )
    assert auth is not None
    # 10s of server lease minus 250ms margin, anchored at request start.
    assert auth.deadline_ns == start + 10_000_000_000 - 250_000_000


def test_ttl_caps_server_header():
    gate = LeaseGate(safety_margin_ms=250, clock=FakeClock(0))
    start = 0
    auth = gate.authorize(
        start,
        expires_at="2026-01-01T00:00:30Z",
        server_time="2026-01-01T00:00:00Z",
        ttl_seconds=5,
    )
    assert auth is not None
    assert auth.deadline_ns == 5_000_000_000 - 250_000_000


def test_reject_when_remaining_within_margin():
    gate = LeaseGate(safety_margin_ms=250, clock=FakeClock(0))
    assert (
        gate.authorize(
            0,
            expires_at="2026-01-01T00:00:00.100Z",
            server_time="2026-01-01T00:00:00Z",
            ttl_seconds=1,
        )
        is None
    )


def test_reject_when_already_expired_locally():
    gate = LeaseGate(safety_margin_ms=250, clock=FakeClock(0))
    start = 0
    assert (
        gate.authorize(
            start,
            expires_at="2026-01-01T00:00:10Z",
            server_time="2026-01-01T00:00:00Z",
            ttl_seconds=15,
            clock_now_ns=10_000_000_000,
        )
        is None
    )


def test_local_max_is_applied():
    gate = LeaseGate(safety_margin_ms=250, clock=FakeClock(0), local_max_seconds=3)
    auth = gate.authorize(
        0,
        expires_at="2026-01-01T00:00:30Z",
        server_time="2026-01-01T00:00:00Z",
        ttl_seconds=15,
    )
    assert auth is not None
    assert auth.deadline_ns == 3_000_000_000 - 250_000_000


def test_valid_uses_strict_inequality():
    gate = LeaseGate(safety_margin_ms=250, clock=FakeClock(0))
    auth = gate.authorize(
        0,
        expires_at="2026-01-01T00:00:10Z",
        server_time="2026-01-01T00:00:00Z",
        ttl_seconds=15,
    )
    assert auth is not None
    assert auth.valid(auth.deadline_ns - 1)
    assert not auth.valid(auth.deadline_ns)


def test_boot_time_clock_is_monotonic():
    clock = BootTimeClock()
    first = clock.now_ns()
    second = clock.now_ns()
    assert second >= first
