"""Writing the BYD poll interval numbers, and noticing when a write did not take."""

from bsp.modes import MAX_WRITE_MISSES, Intervals, IntervalWrites

DRIVING = Intervals(60, 120)
PARKED = Intervals(3600, 28800)


def test_first_target_is_due_and_applied_when_read_back():
    writes = IntervalWrites()
    assert writes.due(DRIVING)
    assert writes.written(DRIVING, DRIVING)
    assert writes.applied == DRIVING
    assert not writes.due(DRIVING)


def test_write_to_unavailable_numbers_is_not_applied():
    # number.set_value skips an unavailable entity without an error, so nothing reads back.
    writes = IntervalWrites()
    assert not writes.written(DRIVING, None)
    assert writes.applied is None
    assert writes.due(DRIVING)


def test_retries_stop_after_max_misses_until_target_changes():
    writes = IntervalWrites()
    for _ in range(MAX_WRITE_MISSES):
        assert writes.due(DRIVING)
        writes.written(DRIVING, PARKED)
    assert not writes.due(DRIVING)
    # A new target is always tried.
    assert writes.due(PARKED)
    writes.written(PARKED, PARKED)
    assert writes.misses == 0


def test_reload_mid_drive_reapplies_when_numbers_come_back():
    writes = IntervalWrites()
    writes.written(PARKED, PARKED)
    writes.written(DRIVING, DRIVING)
    # BYD reloads: the numbers go unavailable.
    assert not writes.numbers_changed(None, came_back=False)
    assert writes.applied is None
    # They come back with the value BYD saved before, not ours.
    assert writes.numbers_changed(PARKED, came_back=True)
    assert writes.due(DRIVING)


def test_numbers_back_with_our_value_need_no_write():
    writes = IntervalWrites()
    writes.written(DRIVING, DRIVING)
    writes.numbers_changed(None, came_back=False)
    assert not writes.numbers_changed(DRIVING, came_back=True)
    assert writes.applied == DRIVING
    assert not writes.due(DRIVING)


def test_late_read_back_confirms_a_write():
    # The write read back nothing, then the numbers showed the target a moment later.
    writes = IntervalWrites()
    writes.written(DRIVING, None)
    assert not writes.numbers_changed(DRIVING, came_back=True)
    assert writes.applied == DRIVING
    assert writes.misses == 0


def test_manual_edit_is_written_over():
    writes = IntervalWrites()
    writes.written(PARKED, PARKED)
    assert writes.numbers_changed(Intervals(300, 300), came_back=False)
    assert writes.due(PARKED)


def test_numbers_that_keep_another_value_do_not_loop():
    # BYD keeps a different value than written (for example clamped to its range).
    writes = IntervalWrites()
    kept = Intervals(60, 300)
    rewrites = 0
    while writes.due(DRIVING) and rewrites < 10:
        writes.written(DRIVING, kept)
        rewrites += 1
        # The state change from that write, seen after it finished.
        if not writes.numbers_changed(kept, came_back=False):
            break
    assert rewrites == MAX_WRITE_MISSES
    # Coming back from unavailable gives it another go.
    assert writes.numbers_changed(kept, came_back=True)


def test_nothing_written_yet_needs_nothing():
    writes = IntervalWrites()
    assert not writes.numbers_changed(PARKED, came_back=True)


def test_reset_forgets_everything():
    writes = IntervalWrites()
    writes.written(DRIVING, DRIVING)
    writes.reset()
    assert writes.applied is None and writes.target is None and writes.misses == 0
    assert writes.due(DRIVING)


def test_another_writer_is_not_fought_for_good():
    # Our write holds each time, then something else (an old automation) sets its own value.
    writes = IntervalWrites()
    theirs = Intervals(300, 300)
    rounds = 0
    while writes.due(DRIVING) and rounds < 10:
        writes.written(DRIVING, DRIVING)
        rounds += 1
        if not writes.numbers_changed(theirs, came_back=False):
            break
    assert rounds == MAX_WRITE_MISSES
    assert writes.gave_up
    assert not writes.due(DRIVING)
    # A new target is written again, with a fresh count.
    assert writes.due(PARKED)
    writes.written(PARKED, PARKED)
    assert not writes.gave_up


def test_numbers_coming_back_end_the_overwrite_count():
    writes = IntervalWrites()
    for _ in range(MAX_WRITE_MISSES):
        writes.written(DRIVING, DRIVING)
        writes.numbers_changed(Intervals(300, 300), came_back=False)
    assert writes.gave_up
    assert writes.numbers_changed(Intervals(300, 300), came_back=True)
    assert not writes.gave_up
