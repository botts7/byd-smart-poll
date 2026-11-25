"""Print what the charge tracker does in the simulated scenarios (manual diagnostics)."""

import importlib
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
importlib.import_module("conftest")  # registers the bsp stub package
from test_charge_tracker import simulate  # noqa: E402

cases = {
    "solar 1.9kW, prior 0.6": dict(true_kpp=0.82, prior=0.6, power_kw=lambda t: 1.9, hours=6),
    "clouds, prior 0.6": dict(
        true_kpp=0.82,
        prior=0.6,
        power_kw=lambda t: 2.9 + 1.7 * math.sin(t / 600 * math.pi),
        hours=5,
    ),
    "dolphin 7kW prior 1.2": dict(true_kpp=0.45, prior=1.2, power_kw=lambda t: 7.0, hours=2),
    "7.2kW prior 0.82": dict(true_kpp=0.82, prior=0.82, power_kw=lambda t: 7.2, hours=3),
}
for name, kw in cases.items():
    tr, polls, ticks, stale = simulate(**kw)
    print(
        f"{name}: polls={polls} ticks={ticks} ratio={polls / ticks:.2f} est={tr.session_estimate}"
    )
    print("   stale s:", stale)

# Baseline: what the BYD integration does today, a fixed 300 s poll.
print("\nbaseline fixed 300 s polling:")
for name, kw in cases.items():
    import statistics

    from bsp.charge_tracker import ChargeTracker

    class Fixed(ChargeTracker):
        def poll_due(self, ts):
            self.advance(ts)
            return ts % 300 == 0

    import test_charge_tracker as t

    orig = t.ChargeTracker
    t.ChargeTracker = Fixed
    tr, polls, ticks, stale = simulate(**kw)
    t.ChargeTracker = orig
    v = stale[3:]
    print(f"{name}: ratio={polls / ticks:.2f} stale mean {statistics.mean(v):.0f}s max {max(v)}s")
