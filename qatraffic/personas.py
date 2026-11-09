"""The simulated user population: who each account is, where it lives, how it behaves.

Everything here is a pure function of (entropy, account count), so the same seed always gives the
same population: same personas, time zones, habits and friends.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import asdict, dataclass, field

# Where the users live (IANA zones; DST handled by zoneinfo). Weights approximate a global crypto
# user base, so the app is busy somewhere at every hour of the day.
ZONES = [
    ("America/Los_Angeles", 6), ("America/Denver", 2), ("America/Chicago", 5), ("America/New_York", 9),
    ("America/Mexico_City", 3), ("America/Bogota", 2), ("America/Sao_Paulo", 5), ("America/Argentina/Buenos_Aires", 2),
    ("Europe/London", 6), ("Europe/Lisbon", 2), ("Europe/Berlin", 7), ("Europe/Paris", 4), ("Europe/Warsaw", 2),
    ("Europe/Kyiv", 2), ("Europe/Istanbul", 3),
    ("Africa/Lagos", 6), ("Africa/Accra", 2), ("Africa/Nairobi", 3), ("Africa/Johannesburg", 2), ("Africa/Cairo", 2),
    ("Asia/Dubai", 2), ("Asia/Kolkata", 7), ("Asia/Bangkok", 2), ("Asia/Jakarta", 3), ("Asia/Singapore", 3),
    ("Asia/Manila", 2), ("Asia/Shanghai", 3), ("Asia/Seoul", 3), ("Asia/Tokyo", 3),
    ("Australia/Sydney", 3), ("Pacific/Auckland", 1),
]

# Session intents. Each one maps to concrete steps in behaviour.py, decided at run time from the
# account's balances.
INTENT_INPUTS = {"topup": 1, "pay": 1, "pay2": 2, "topup_pay": 2, "cashout": 1, "pay_cashout": 2, "check": 0}


@dataclass(frozen=True)
class PersonaSpec:
    share: float  # fraction of the population
    rate: float  # relative sessions per active day
    intents: dict  # intent -> weight
    amount_median: float  # USDC, for deposits
    amount_sigma: float
    dormancy: float  # daily chance to go quiet for a few days
    friends: tuple  # (min, max)


PERSONAS = {
    "power": PersonaSpec(0.10, 5.0, {"pay": 0.30, "pay2": 0.12, "topup": 0.17, "topup_pay": 0.12, "cashout": 0.14,
                                      "pay_cashout": 0.08, "check": 0.07}, 80, 0.8, 0.0, (4, 6)),
    "regular": PersonaSpec(0.32, 1.8, {"pay": 0.27, "pay2": 0.04, "topup": 0.24, "topup_pay": 0.12, "cashout": 0.19,
                                        "pay_cashout": 0.06, "check": 0.08}, 45, 0.7, 0.01, (2, 5)),
    "casual": PersonaSpec(0.44, 0.6, {"pay": 0.21, "topup": 0.31, "topup_pay": 0.08, "cashout": 0.25,
                                       "pay_cashout": 0.04, "check": 0.11}, 20, 0.7, 0.04, (1, 3)),
    "merchant": PersonaSpec(0.10, 1.2, {"cashout": 0.46, "pay": 0.18, "topup": 0.08, "check": 0.28}, 60, 0.6, 0.0, (2, 4)),
    "whale": PersonaSpec(0.04, 0.35, {"topup": 0.40, "cashout": 0.33, "pay": 0.20, "check": 0.07}, 2500, 0.6, 0.02, (1, 3)),
}

# Local-hour activity curve (hour 0..23), a typical consumer-app shape: quiet before dawn, a
# morning rise, steady day, evening peak.
DIURNAL = [0.30, 0.18, 0.10, 0.06, 0.05, 0.09, 0.25, 0.55, 0.80, 0.95, 1.00, 1.00,
           1.08, 1.00, 0.94, 0.94, 1.00, 1.10, 1.22, 1.32, 1.36, 1.22, 0.92, 0.56]


@dataclass
class Profile:
    slot: int
    persona: str
    tz: str
    chronotype: float  # hours: + means a late riser, - an early bird
    weekend_factor: float
    tip_gwei: float
    approve_habit: str  # exact | max | buffer
    l1_route_bias: float  # chance a transfer/withdrawal goes straight to L1 instead of the sequencer
    exec_profile: str  # prompt | normal | lazy (voucher execution delay)
    onboard_day: int  # day index (0 = first day of the run) when the user starts
    friends: list[int] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def rng_for(seed: bytes, *labels) -> random.Random:
    h = hashlib.sha256(seed + b"|" + "|".join(str(x) for x in labels).encode()).digest()
    return random.Random(int.from_bytes(h, "big"))


def _weighted(rng: random.Random, items):
    total = sum(w for _, w in items)
    x = rng.random() * total
    for item, w in items:
        x -= w
        if x <= 0:
            return item
    return items[-1][0]


def build_population(seed: bytes, count: int, *, initial_active_share: float, onboarding_days: int,
                     tip_min_gwei: float, tip_max_gwei: float, l1_share: float) -> list[Profile]:
    rng = rng_for(seed, "population", count)
    # personas: exact shares (largest remainder) so small populations still get every type
    names = list(PERSONAS)
    raw = {n: PERSONAS[n].share * count for n in names}
    alloc = {n: int(math.floor(v)) for n, v in raw.items()}
    for n in sorted(names, key=lambda n: raw[n] - alloc[n], reverse=True)[: count - sum(alloc.values())]:
        alloc[n] += 1
    if count >= 10 and alloc["whale"] == 0:
        alloc["whale"], alloc["casual"] = 1, alloc["casual"] - 1
    personas = [n for n in names for _ in range(alloc[n])]
    rng.shuffle(personas)

    profiles = []
    n_initial = max(1, round(count * initial_active_share))
    order = list(range(count))
    rng.shuffle(order)
    initial = set(order[:n_initial])
    # make sure the first day has at least one power user and one merchant if they exist
    for need in ("power", "merchant"):
        if not any(personas[s] == need for s in initial):
            cand = [s for s in range(count) if personas[s] == need]
            if cand:
                drop = next((s for s in initial if personas[s] == "casual"), None)
                if drop is not None:
                    initial.discard(drop)
                initial.add(cand[0])
    log_lo, log_hi = math.log(tip_min_gwei), math.log(tip_max_gwei)
    for slot in range(count):
        p = personas[slot]
        late = slot not in initial
        # later joiners arrive with a decaying rate over the onboarding window
        onboard = 0 if not late else 1 + min(onboarding_days - 1, int(rng.expovariate(1 / max(1, onboarding_days / 3))))
        habit = _weighted(rng, [("exact", 55), ("max", 30), ("buffer", 15)])
        profiles.append(Profile(
            slot=slot,
            persona=p,
            tz=_weighted(rng, ZONES),
            chronotype=max(-3.0, min(3.0, rng.gauss(0.0, 1.2))),
            weekend_factor=rng.uniform(0.6, 1.4),
            tip_gwei=round(math.exp(rng.uniform(log_lo, log_hi)), 6),
            approve_habit=habit,
            l1_route_bias=_l1_bias(rng, l1_share),
            exec_profile=_weighted(rng, [("prompt", 40), ("normal", 40), ("lazy", 20)]),
            onboard_day=onboard,
        ))
    # friend graph: mostly people in nearby time zones, plus merchants
    merchants = [q.slot for q in profiles if q.persona == "merchant"]
    for prof in profiles:
        lo, hi = PERSONAS[prof.persona].friends
        want = min(count - 1, rng.randint(lo, hi))
        chosen: list[int] = []
        guard = 0
        while len(chosen) < want and guard < 200:
            guard += 1
            r = rng.random()
            if merchants and r < 0.22 and prof.persona != "merchant":
                cand = rng.choice(merchants)
            elif r < 0.65:
                same = [q.slot for q in profiles if q.tz.split("/")[0] == prof.tz.split("/")[0] and q.slot != prof.slot]
                cand = rng.choice(same) if same else rng.randrange(count)
            else:
                cand = rng.randrange(count)
            if cand != prof.slot and cand not in chosen:
                chosen.append(cand)
        prof.friends = chosen
    return profiles


def _l1_bias(rng: random.Random, share: float) -> float:
    """Most people use the sequencer (fast, no gas); a few prefer the L1 path for everything."""
    if share <= 0:
        return 0.0
    if rng.random() < min(0.5, share / 2):
        return min(1.0, rng.uniform(0.5, 0.95))
    return max(0.0, min(1.0, rng.gauss(share / 2, share / 4)))


def profile_from_json(d: dict) -> Profile:
    return Profile(**d)
