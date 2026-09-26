"""Battery telemetry from the Stick, and a rough current estimate from it.

The StickS3's PMIC reports battery voltage but not current, so consumption is inferred: voltage over time,
mapped through a typical 1-cell LiPo open-circuit curve to state of charge, times the 250 mAh capacity.
Good to roughly +/-20% over windows of 20+ minutes in one steady state; useless for short bursts.

    uv run kai-power                 # summarise data/power.csv
    uv run kai-power --since 2h
"""

import argparse
import csv
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CAPACITY_MAH = 250
FIELDS = ["ts", "device", "kind", "mode", "mv", "usb", "bright", "wifi_ps", "rssi", "up_s", "loop_hz",
          "render_pct", "slept_s", "mv_before", "mv_after", "mhz", "enc_us", "dec_us"]

# Typical 1-cell LiPo open-circuit voltage (mV) -> state of charge (%), light load.
OCV = [(3270, 0), (3610, 5), (3690, 10), (3710, 15), (3730, 20), (3750, 25), (3770, 30), (3790, 35),
       (3800, 40), (3820, 45), (3840, 50), (3850, 55), (3870, 60), (3910, 65), (3950, 70), (3980, 75),
       (4020, 80), (4080, 85), (4110, 90), (4150, 95), (4200, 100)]


def soc(mv: float) -> float:
    if mv <= OCV[0][0]:
        return 0.0
    for (v0, s0), (v1, s1) in zip(OCV, OCV[1:]):
        if mv <= v1:
            return s0 + (s1 - s0) * (mv - v0) / (v1 - v0)
    return 100.0


def mah_between(mv_start: float, mv_end: float) -> float:
    return (soc(mv_start) - soc(mv_end)) / 100 * CAPACITY_MAH


class PowerLog:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "power.csv"
        data_dir.mkdir(parents=True, exist_ok=True)
        # Columns changed since this file was started: keep the old one aside rather than misalign rows.
        if self.path.exists():
            with self.path.open() as f:
                header = f.readline().strip().split(",")
            if header != FIELDS:
                self.path.rename(self.path.with_name(f"power-{int(self.path.stat().st_mtime)}.csv"))

    def write(self, device: str, kind: str, **values) -> None:
        new = not self.path.exists()
        with self.path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow({"ts": f"{time.time():.0f}", "device": device, "kind": kind, **values})


def sleep_estimate_ma(slept_s: float, mv_before: float, mv_after: float) -> float | None:
    """Average deep-sleep current, or None when the drop is below the voltage resolution."""
    if slept_s < 3600 or mv_before - mv_after < 3:
        return None
    return mah_between(mv_before, mv_after) / (slept_s / 3600)


@dataclass
class Segment:
    key: tuple
    t: list
    mv: list

    def estimate(self) -> tuple[float, float] | None:
        """(minutes, mA) from a least-squares voltage slope."""
        n = len(self.t)
        minutes = (self.t[-1] - self.t[0]) / 60
        if n < 5 or minutes < 10:
            return None
        tm, vm = sum(self.t) / n, sum(self.mv) / n
        slope = sum((t - tm) * (v - vm) for t, v in zip(self.t, self.mv)) / sum((t - tm) ** 2 for t in self.t)
        v0 = vm + slope * (self.t[0] - tm)
        v1 = vm + slope * (self.t[-1] - tm)
        return minutes, mah_between(v0, v1) / (minutes / 60)


def parse_since(value: str | None) -> float:
    if not value:
        return 0
    m = re.fullmatch(r"(\d+)([mhd])", value)
    if not m:
        raise SystemExit("--since takes e.g. 30m, 2h, 1d")
    return time.time() - int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def main() -> None:
    ap = argparse.ArgumentParser(description="Estimate Kai's battery current per state from data/power.csv")
    ap.add_argument("--csv", default="data/power.csv")
    ap.add_argument("--since", help="only rows newer than this, e.g. 2h")
    args = ap.parse_args()
    since = parse_since(args.since)

    rows = [r for r in csv.DictReader(open(args.csv)) if float(r["ts"]) >= since]
    if not rows:
        raise SystemExit("no telemetry yet")

    print("Deep sleep (from wake reports):")
    for r in (r for r in rows if r["kind"] == "sleep"):
        ma = sleep_estimate_ma(float(r["slept_s"]), float(r["mv_before"]), float(r["mv_after"]))
        when = datetime.fromtimestamp(float(r["ts"])).strftime("%m-%d %H:%M")
        est = f"{ma:.2f} mA" if ma is not None else "too short/small to resolve"
        print(f"  {when}  slept {float(r['slept_s']) / 3600:.1f} h, {r['mv_before']} -> {r['mv_after']} mV: {est}")

    # Steady on-battery stretches: consecutive reports with the same mode, brightness and radio setting.
    segments: list[Segment] = []
    for r in (r for r in rows if r["kind"] == "report" and r["usb"] == "False"):
        key = (r["device"], r["mode"], r["bright"], r["wifi_ps"])
        t, mv = float(r["ts"]), float(r["mv"])
        if segments and segments[-1].key == key and t - segments[-1].t[-1] < 120:
            segments[-1].t.append(t)
            segments[-1].mv.append(mv)
        else:
            segments.append(Segment(key, [t], [mv]))

    print("\nSteady states on battery (>= 10 min):")
    by_state = defaultdict(list)
    for seg in segments:
        est = seg.estimate()
        if est:
            by_state[seg.key[1:]].append(est)
    if not by_state:
        print("  none yet: unplug USB and hold one state for 10+ minutes (see docs/POWER.md)")
    for (mode, bright, wifi_ps), ests in sorted(by_state.items()):
        minutes = sum(m for m, _ in ests)
        ma = sum(m * a for m, a in ests) / minutes
        print(f"  {mode:10} bright={bright:>3} wifi_ps={wifi_ps:5}  {minutes:5.0f} min  ~{ma:5.1f} mA"
              f"  -> {CAPACITY_MAH * 0.9 / ma:4.1f} h on a full charge")

    reports = [r for r in rows if r["kind"] == "report"]
    if reports:
        hz = sorted(int(r["loop_hz"]) for r in reports)
        rp = sorted(int(r["render_pct"]) for r in reports)
        print(f"\nFirmware loop: median {hz[len(hz) // 2]} loops/s, drawing {rp[len(rp) // 2]}% of the time")


if __name__ == "__main__":
    main()
