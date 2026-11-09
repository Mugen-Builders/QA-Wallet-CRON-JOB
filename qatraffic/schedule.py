"""When things happen: a virtual clock and the per-day session plan.

There is no fixed interval. Session start times are drawn from a non-homogeneous Poisson-like
process: the sum, over every active user, of that user's local-time activity curve, plus a small
uniform floor. Users live in ~30 time zones, so the app gets traffic at every hour. A day's plan is
a pure function of (seed, date, population, run start), so any day can be regenerated exactly.
"""
from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .personas import DIURNAL, INTENT_INPUTS, PERSONAS, Profile, rng_for, _weighted

BIN_S = 300
BINS = 86400 // BIN_S
FLOOR_SHARE = 0.06  # uniform share of the day's sessions, so no hour is ever empty
MIN_GAP_S = 20 * 60  # one user does not open two sessions within 20 minutes


class Clock:
    """Virtual time = run start + (wall time - run start) x scale. scale 1 in production; tests
    and the local devnet rehearsal compress days into minutes."""

    def __init__(self, start_real: float, scale: float = 1.0):
        self.start = start_real
        self.scale = scale

    def now(self) -> float:
        return self.start + (time.time() - self.start) * self.scale

    def real_delay(self, virtual_seconds: float) -> float:
        return virtual_seconds / self.scale

    def to_real(self, virtual_ts: float) -> float:
        return self.start + (virtual_ts - self.start) / self.scale


def utc_date(ts: float) -> date:
    return datetime.fromtimestamp(ts, timezone.utc).date()


def day_start(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


@dataclass
class PlannedSession:
    planned_at: float
    slot: int
    intent: str
    counterparty: int | None
    planned_inputs: int
    origin: str


def day_target(cfg, seed: bytes, d: date, day_number: int) -> tuple[int, float, str]:
    """Number of app inputs the day aims for, never below DAILY_MIN."""
    rng = rng_for(seed, "day-target", d.isoformat())
    arc = 1.0 + 0.18 * math.exp(-day_number / 6) - 0.05 * math.sin(math.pi * min(day_number, 30) / 30)
    week = (1.0, 1.04, 1.06, 1.03, 0.98, 0.9, 0.88)[d.weekday()]
    noise = math.exp(rng.gauss(0, 0.09))
    lo, hi = cfg.daily_min, cfg.daily_max
    factor = arc * week * noise
    target = (lo + hi) / 2 * factor
    note = ""
    r = rng.random()
    if r < cfg.busy_day_prob:
        target = rng.uniform(hi, cfg.busy_day_max)
        note = "busy day"
    elif r < cfg.busy_day_prob + 0.05:
        target = rng.uniform(lo, lo + 0.15 * (hi - lo))
        note = "quiet day"
    target = int(round(max(lo, min(cfg.busy_day_max, target))))
    return target, round(factor, 4), note


def dormant(seed: bytes, prof: Profile, d: date, run_start: date) -> bool:
    """Casual users go quiet for a few days now and then. Deterministic per (user, day): a spell
    starts on day k with probability p and lasts 3-10 days."""
    p = PERSONAS[prof.persona].dormancy
    if p <= 0:
        return False
    days = (d - run_start).days
    for back in range(0, 11):
        k = days - back
        if k < 1:
            break
        rng = rng_for(seed, "dormancy", prof.slot, k)
        if rng.random() < p and back < rng.randint(3, 10):
            return True
    return False


def _local_weight(prof: Profile, ts: float) -> float:
    local = datetime.fromtimestamp(ts, ZoneInfo(prof.tz))
    h = (local.hour + local.minute / 60 - prof.chronotype) % 24
    i = int(h)
    frac = h - i
    w = DIURNAL[i] * (1 - frac) + DIURNAL[(i + 1) % 24] * frac
    if local.weekday() >= 5:
        w *= prof.weekend_factor
    return w


def plan_day(cfg, seed: bytes, d: date, run_start: date, profiles: list[Profile], target: int,
             busy_until: dict[int, float] | None = None) -> list[PlannedSession]:
    """Sessions for UTC day `d` whose planned inputs add up to `target`."""
    rng = rng_for(seed, "plan", d.isoformat())
    day_no = (d - run_start).days
    t0 = day_start(d)
    active = [p for p in profiles if p.onboard_day <= day_no and not dormant(seed, p, d, run_start)]
    if not active:
        active = [min(profiles, key=lambda p: p.onboard_day)]
    rates = {p.slot: PERSONAS[p.persona].rate * (1.25 if p.onboard_day == day_no and day_no > 0 else 1.0)
             for p in active}
    # per-bin intensity for each active user
    lam = {p.slot: [rates[p.slot] * _local_weight(p, t0 + (b + 0.5) * BIN_S) for b in range(BINS)] for p in active}
    total = [sum(lam[s][b] for s in lam) for b in range(BINS)]
    tsum = sum(total) or 1.0
    density = [(1 - FLOOR_SHARE) * total[b] / tsum + FLOOR_SHARE / BINS for b in range(BINS)]
    cdf, acc = [], 0.0
    for x in density:
        acc += x
        cdf.append(acc)
    by_slot = {p.slot: p for p in active}
    last_at: dict[int, list[float]] = {s: [] for s in lam}
    if busy_until:
        for s, t in busy_until.items():
            if s in last_at:
                last_at[s].append(t)
    sessions: list[PlannedSession] = []
    planned = 0
    guard = 0
    while planned < target and guard < target * 40:
        guard += 1
        b = min(BINS - 1, bisect.bisect_left(cdf, rng.random() * cdf[-1]))
        t = t0 + b * BIN_S + rng.random() * BIN_S
        weights = [(s, lam[s][b]) for s in lam if lam[s][b] > 0]
        slot = _weighted(rng, weights) if weights else rng.choice(list(lam))
        if any(abs(t - x) < MIN_GAP_S for x in last_at[slot]):
            continue
        prof = by_slot[slot]
        intent = _weighted(rng, list(PERSONAS[prof.persona].intents.items()))
        cp = rng.choice(prof.friends) if prof.friends and intent in ("pay", "pay2", "topup_pay", "pay_cashout") else None
        n = INTENT_INPUTS[intent]
        sessions.append(PlannedSession(t, slot, intent, cp, n, "plan"))
        last_at[slot].append(t)
        planned += n
        # a payment often makes the receiver act soon after (moves it on, or cashes it out)
        if cp is not None and cp in by_slot and rng.random() < 0.22 and planned < target:
            ft = t + min(4 * 3600, max(240, rng.lognormvariate(math.log(25 * 60), 0.8)))
            if ft < t0 + 86400 and not any(abs(ft - x) < MIN_GAP_S for x in last_at[cp]):
                rprof = by_slot[cp]
                fint = _weighted(rng, [("pay", 3), ("cashout", 4), ("check", 2)])
                fcp = rng.choice(rprof.friends) if rprof.friends and fint == "pay" else None
                sessions.append(PlannedSession(ft, cp, fint, fcp, INTENT_INPUTS[fint], "followup"))
                last_at[cp].append(ft)
                planned += INTENT_INPUTS[fint]
    sessions.sort(key=lambda s: s.planned_at)
    return sessions


def think_time(rng, median_s: float = 70.0, sigma: float = 0.8, lo: float = 12.0, hi: float = 600.0) -> float:
    return max(lo, min(hi, rng.lognormvariate(math.log(median_s), sigma)))


EXEC_BUCKETS = {
    # (low, high) seconds after the voucher becomes executable, with weights
    "prompt": [((120, 1800), 75), ((1800, 3 * 3600), 20), ((3 * 3600, 20 * 3600), 5)],
    "normal": [((120, 1800), 52), ((1800, 3 * 3600), 33), ((3 * 3600, 20 * 3600), 12), ((20 * 3600, 23.5 * 3600), 3)],
    "lazy": [((120, 1800), 25), ((1800, 3 * 3600), 40), ((3 * 3600, 20 * 3600), 27), ((20 * 3600, 23.5 * 3600), 8)],
}


def exec_delay(rng, prof: Profile, due_virtual: float, deadline_hours: float) -> float:
    """Seconds (virtual) after due when the owner executes. Users asleep at due time act after
    they wake up, within the hard deadline."""
    (lo, hi) = _weighted(rng, EXEC_BUCKETS[prof.exec_profile])
    delay = rng.uniform(lo, hi)
    at = due_virtual + delay
    local = datetime.fromtimestamp(at, ZoneInfo(prof.tz))
    hour = (local.hour - prof.chronotype) % 24
    if hour < 6.5 and delay < 6 * 3600:
        wake = local.replace(hour=7, minute=0, second=0, microsecond=0) + timedelta(minutes=rng.uniform(0, 150))
        if wake.timestamp() < at:
            wake += timedelta(days=1)
        delay = wake.timestamp() - due_virtual
    return min(delay, deadline_hours * 3600 - 600)
