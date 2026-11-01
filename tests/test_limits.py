import pytest

from browser_mcp.config import LimitsConfig
from browser_mcp.limits import DAY, HOUR, LimitExceeded, Limits, site_of


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_site_is_the_registrable_domain():
    assert site_of("www.example.co.uk") == "example.co.uk"
    assert site_of("a.b.example.com") == "example.com"
    assert site_of("example.com.au") == "example.com.au"


def fill(limits, n, caller="c", host="www.example.com"):
    for _ in range(n):
        limits.check(caller, host)
        limits.record(caller, host)


def test_per_caller_per_hour_refuses_and_frees_up():
    clock = Clock()
    limits = Limits(LimitsConfig(per_caller_per_hour=3), clock=clock)
    fill(limits, 3)
    with pytest.raises(LimitExceeded, match=r"3 pages an hour for caller 'c': refused; it frees up at \S+ \(in 3601 s\)"):
        limits.check("c", "www.example.com")
    limits.check("other", "www.example.com")  # another caller isn't held up
    clock.t += HOUR + 1
    limits.check("c", "www.example.com")


def test_per_site_per_day_counts_the_registrable_domain():
    clock = Clock()
    limits = Limits(LimitsConfig(per_site_per_day=2, per_caller_per_hour=100), clock=clock)
    limits.check("a", "www.example.com"); limits.record("a", "www.example.com")
    clock.t += 10
    limits.check("b", "shop.example.com"); limits.record("b", "shop.example.com")
    with pytest.raises(LimitExceeded, match="2 pages a day on example.com") as e:
        limits.check("c", "example.com")
    assert "in 86391 s" in str(e.value) or "in 86390 s" in str(e.value)
    limits.check("c", "example.org")
    clock.t += DAY - 9
    limits.check("c", "example.com")  # the first has aged out


def test_per_day_in_all():
    clock = Clock()
    limits = Limits(LimitsConfig(per_day=3, per_site_per_day=100, per_caller_per_hour=100), clock=clock)
    for i, host in enumerate(["a.com", "b.com", "c.com"]):
        limits.check(f"c{i}", host); limits.record(f"c{i}", host)
    with pytest.raises(LimitExceeded, match="3 pages a day in all"):
        limits.check("new", "d.com")


def test_zero_cap_never_frees():
    limits = Limits(LimitsConfig(per_day=0))
    with pytest.raises(LimitExceeded, match="never"):
        limits.check("c", "a.com")


def test_counts_survive_a_restart(tmp_path):
    clock = Clock()
    state = tmp_path / "limits.json"
    fill(Limits(LimitsConfig(per_caller_per_hour=2), state, clock), 2)
    again = Limits(LimitsConfig(per_caller_per_hour=2), state, clock)
    with pytest.raises(LimitExceeded):
        again.check("c", "www.example.com")


async def test_pace_spaces_page_actions(monkeypatch):
    import time
    limits = Limits(LimitsConfig(delay_min=0.2, delay_max=0.3))
    await limits.pace()
    t0 = time.monotonic()
    await limits.pace()
    assert 0.19 <= time.monotonic() - t0 <= 0.5
