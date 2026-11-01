"""Bounded-speed visibility planning for a cached sprite sequence, without Qt.

Every interval describes where the window can be placed for that frame. The
future/past envelopes anticipate a large prop and finish returning before the
last frame, instead of moving an already restored static character afterward.
Temporary gaps in prop clearance do not trigger extra trips back toward home.
One-sided clearances use a slow-start, slow-stop curve when its bounds and
speed permit it; tight or conflicting layouts retain the feasible fallback.
"""
import math


def _fill_valleys(values):
    """Hold one excursion across local gaps, retaining both endpoint values."""
    prefix, suffix = [], [0.0] * len(values)
    maximum = 0.0
    for value in values:
        maximum = max(maximum, value)
        prefix.append(maximum)
    maximum = 0.0
    for index in range(len(values)-1, -1, -1):
        maximum = max(maximum, values[index])
        suffix[index] = maximum
    return [min(before, after) for before, after in zip(prefix, suffix)]


def _ease(progress):
    """Quintic smoothstep, with zero velocity and acceleration at either end."""
    progress = max(0.0, min(1.0, progress))
    return progress**3 * (10 + progress * (-15 + 6 * progress))


def _inverse_ease(value):
    if value <= 0:
        return 0.0
    if value >= 1:
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(48):
        middle = (low + high) / 2
        if _ease(middle) < value:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _eased_excursion(times, lower, upper, home, rate):
    """Use the same minimum clearance peak, with no sudden departure or stop.

    Each required clearance limits the longest permissible easing duration.
    A flat peak separates departure from return. The caller's pinned endpoint
    intervals, including custom starts/destinations, must admit home first.
    """
    if not (lower[0] <= home <= upper[0] and lower[-1] <= home <= upper[-1]):
        return None
    above = [max(0.0, value - home) for value in lower]
    below = [max(0.0, home - value) for value in upper]
    if max(above) and max(below):
        return None
    direction = 1 if max(above) else -1
    required = above if direction > 0 else below
    peak = max(required)
    if not peak:
        return tuple(home for _ in times)
    first, last = times[0], times[-1]
    departure, returning = last - first, last - first
    for stamp, distance in zip(times, required):
        if distance:
            weight = _inverse_ease(distance / peak)
            departure = min(departure, (stamp - first) / weight)
            returning = min(returning, (last - stamp) / weight)
    if (departure <= 0 or returning <= 0 or departure + returning > last - first + 1e-7
            or 1.875 * peak / min(departure, returning) > rate + 1e-7):
        return None
    path = tuple(home + direction * peak * min(
        _ease((stamp - first) / departure), _ease((last - stamp) / returning))
                 for stamp in times)
    if (all(low - 1e-7 <= value <= high + 1e-7 for value, low, high in zip(path, lower, upper))
            and all(abs(b - a) <= rate * (tb - ta) + 1e-5
                    for a, b, ta, tb in zip(path, path[1:], times, times[1:]))):
        # Inverting the easing curve can leave a sub-ulp boundary error.
        # A duplicated bridge/native timestamp must use one common clamp,
        # otherwise different intervals can introduce tiny unequal positions.
        intervals = {}
        for stamp, low, high in zip(times, lower, upper):
            if stamp in intervals:
                previous_low, previous_high = intervals[stamp]
                low, high = max(low, previous_low), min(high, previous_high)
            if low > high:
                return None
            intervals[stamp] = (low, high)
        bounded = tuple(max(intervals[stamp][0], min(value, intervals[stamp][1]))
                        for stamp, value in zip(times, path))
        if all(abs(b - a) <= rate * (tb - ta) + 1e-5
               for a, b, ta, tb in zip(bounded, bounded[1:], times, times[1:])):
            return bounded
    return None


def plan_axis(times, lower, upper, home, *, start=None, end=None, speed=120.0):
    times, low, high = tuple(times), list(lower), list(upper)
    if not times or len(times) != len(low) or len(low) != len(high):
        raise ValueError("One placement interval is required per frame")
    if (any(not math.isfinite(v) for v in (*times, *low, *high, home, speed))
            or any(value is not None and not math.isfinite(value) for value in (start, end))
            or speed <= 0 or any(b < a for a, b in zip(times, times[1:]))
            or any(a > b for a, b in zip(low, high))):
        raise ValueError("Invalid frame times or placement intervals")
    if start is not None:
        low[0] = high[0] = max(low[0], min(start, high[0]))
    if end is not None:
        low[-1] = high[-1] = max(low[-1], min(end, high[-1]))
    # The endpoint constraints may require faster travel for an unusually
    # abrupt clip; find the minimum feasible rate rather than introducing a
    # one-frame jump. At most 127 cached image/bridge frames are considered.
    rate = speed
    for i in range(len(times)):
        for j in range(i + 1, len(times)):
            elapsed = times[j] - times[i]
            required = max(low[j] - high[i], low[i] - high[j], 0.0)
            if elapsed <= 1e-9:
                if required > 1e-7:
                    raise ValueError("Simultaneous frames have incompatible bounds")
            else:
                rate = max(rate, required / elapsed)
    rate += 1e-7
    eased = _eased_excursion(times, low, high, home, rate)
    if eased is not None:
        return eased
    result = []
    for stamp in times:
        earliest = max(a - rate * abs(stamp - other) for a, other in zip(low, times))
        latest = min(b + rate * abs(stamp - other) for b, other in zip(high, times))
        if earliest > latest + 1e-5:
            raise ValueError("No visible placement path exists")
        result.append(max(earliest, min(home, latest)))
    # Staying closest to home independently at every instant follows every
    # notch in a moving silhouette: return, move aside again, return again.
    # Fill those local valleys separately on each side of home. For a
    # one-sided edge this gives one departure, a stable hold, and one return.
    # Running maxima and their minimum retain the original Lipschitz bound.
    above = _fill_valleys([max(0.0, value-home) for value in result])
    below = _fill_valleys([max(0.0, home-value) for value in result])
    stable = tuple(home + positive - negative for positive, negative in zip(above, below))
    if (all(a-1e-7 <= value <= b+1e-7 for value, a, b in zip(stable, low, high))
            and all(abs(b-a) <= rate*(tb-ta)+1e-5
                    for a, b, ta, tb in zip(stable, stable[1:], times, times[1:]))):
        return stable
    # Opposite edge constraints can make a held clearance impossible. Keep
    # the original feasible path rather than clipping art or breaking pins.
    return tuple(result)
