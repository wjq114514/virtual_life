from virtual_life_linux.lease import LeaseAuthorization
from virtual_life_linux.render_guard import RenderGuard


class FakeClock:
    def __init__(self, now_ns: int):
        self.value = now_ns

    def now_ns(self) -> int:
        return self.value


def guard(now_ns: int, deadline_ns: int, generation: str = "g1") -> RenderGuard:
    return RenderGuard(
        generation=generation,
        clock=FakeClock(now_ns),
        authorization=LeaseAuthorization(request_started_ns=0, deadline_ns=deadline_ns),
    )


def test_accepts_current_generation_before_deadline():
    instance = guard(now_ns=100, deadline_ns=200)
    assert instance.accept("g1") is True
    assert instance.remaining_ns() == 100


def test_rejects_stale_generation():
    instance = guard(now_ns=100, deadline_ns=200, generation="g2")
    assert instance.accept("g1") is False


def test_rejects_after_deadline():
    instance = guard(now_ns=250, deadline_ns=200)
    assert instance.accept("g1") is False
    assert instance.remaining_ns() == 0


def test_rejects_without_authorization():
    instance = RenderGuard(generation="g1", clock=FakeClock(0), authorization=None)
    assert instance.accept("g1") is False


def test_rebind_replaces_generation_and_deadline():
    instance = guard(now_ns=100, deadline_ns=150, generation="old")
    instance.bind("new", LeaseAuthorization(request_started_ns=0, deadline_ns=500))
    assert instance.accept("old") is False
    assert instance.accept("new") is True
