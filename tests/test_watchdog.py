from cams_record.watchdog import Backoff, RestartCounter, Watchdog


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def test_backoff_doubles_and_caps():
    b = Backoff(1, 60)
    assert [b.next() for _ in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    b.reset()
    assert b.next() == 1


def test_watchdog_stall_after_three_segment_times():
    clk = FakeClock()
    w = Watchdog(60, 3, clock=clk)
    clk.advance(179)
    assert not w.stalled()
    clk.advance(2)
    assert w.stalled()


def test_watchdog_segment_resets():
    clk = FakeClock()
    w = Watchdog(60, 3, clock=clk)
    for _ in range(10):  # a segment every 60 s keeps it healthy indefinitely
        clk.advance(60)
        w.segment()
        assert not w.stalled()
    clk.advance(181)
    assert w.stalled()
    w.started()  # restart
    assert not w.stalled()
    assert w.idle_for() == 0


def test_restart_counter_24h_window():
    clk = FakeClock(0)
    c = RestartCounter(clock=clk)
    c.add()
    clk.advance(3600)
    c.add()
    assert c.last_24h() == 2
    clk.advance(23 * 3600 + 1)
    assert c.last_24h() == 1
    assert c.total == 2
