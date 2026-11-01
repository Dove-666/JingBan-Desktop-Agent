"""Finer runtime fades for static idle poses, without changing real footage."""
from dataclasses import replace
from typing import TYPE_CHECKING

from PySide6.QtGui import QImage

if TYPE_CHECKING:
    from .frame_animation import LoadedClip

_MAX_FRAMES = 96
_CHUNK = 16384
_EVEN_CHANNEL_BITS = int.from_bytes(b"\xfe" * _CHUNK, "little")
_LOW_CHANNEL_BITS = int.from_bytes(b"\x01" * _CHUNK, "little")


def _midpoint(start: QImage, end: QImage) -> QImage:
    """Average premultiplied channels with rounding, including exact alpha=255.

    Chunked integer arithmetic performs independent byte-channel averages in C
    without an extra package. Each channel is at most 127+127+1, so additions
    cannot carry into the next channel. Two painter half-opacity passes would
    round overlapping opaque alpha down to 254.
    """
    first = start.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    second = end.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    result = QImage(start.size(), QImage.Format.Format_ARGB32_Premultiplied)
    result.setDevicePixelRatio(start.devicePixelRatio())
    result.setColorSpace(start.colorSpace())
    a, b, target = first.constBits(), second.constBits(), result.bits()
    for offset in range(0, result.sizeInBytes(), _CHUNK):
        count = min(_CHUNK, result.sizeInBytes() - offset)
        x = int.from_bytes(a[offset:offset + count], "little")
        y = int.from_bytes(b[offset:offset + count], "little")
        average = ((x & _EVEN_CHANNEL_BITS) >> 1) + ((y & _EVEN_CHANNEL_BITS) >> 1) + ((x | y) & _LOW_CHANNEL_BITS)
        target[offset:offset + count] = average.to_bytes(count, "little")
    return result


def _uniform_endpoint_fades(frames, pose_start, pose_stop):
    """Rebuild the trusted three-fade/pose/three-fade layout symmetrically.

    Five retained weights .125/.3125/.5/.6875/.875 distribute the pose change
    evenly at 24 FPS. The entry/exit share them; temporary quarter images are
    released after construction. Source artwork and exact pose hold remain.
    """
    neutral, pose = frames[0], frames[pose_start]
    half = _midpoint(neutral, pose)
    quarter = _midpoint(neutral, half)
    three_quarters = _midpoint(half, pose)
    low = _midpoint(neutral, quarter)
    lower_middle = _midpoint(quarter, _midpoint(quarter, half))
    upper_middle = _midpoint(_midpoint(half, three_quarters), three_quarters)
    high = _midpoint(three_quarters, pose)
    entry = (neutral, low, lower_middle, half, upper_middle, high)
    hold = (pose,) * ((pose_stop - pose_start) * 2)
    exit_frames = (high, upper_middle, half, lower_middle, low, frames[-1])
    return entry + hold + exit_frames


def smooth_static_idle(clip: "LoadedClip") -> "LoadedClip":
    """Double only 12 FPS static-pose slots, retaining duration and pose hold.

    Identical neighbors reuse their original QImage. The final slot of the
    longest internal repeated pose also remains unchanged, preserving its exact
    hold duration rather than borrowing half a hold slot for the outgoing fade.
    """
    manifest, frames = clip.manifest, clip.frames
    count = len(frames)
    if (getattr(manifest, "static_pose_idle", False) is not True
            or manifest.fps != 12 or count < 2 or count * 2 > _MAX_FRAMES
            or len(manifest.paths) != count
            or any(frame.isNull() or frame.size() != frames[0].size() for frame in frames)):
        return clip
    same = tuple(a == b for a, b in zip(frames, frames[1:]))
    held_start, held_end, longest = -1, -1, 1
    run_start = 0
    for stop in range(1, count + 1):
        if stop < count and same[stop - 1]:
            continue
        length = stop - run_start
        if run_start > 0 and stop < count and length > longest:
            held_start, held_end, longest = run_start, stop - 1, length
        run_start = stop
    if held_start == 3 and held_end == count - 4 and frames[0] == frames[-1]:
        output = _uniform_endpoint_fades(frames, held_start, held_end + 1)
    else:
        output = []
        for index, frame in enumerate(frames):
            output.append(frame)
            if index == count - 1 or index == held_end or same[index]:
                output.append(frame)
            else:
                output.append(_midpoint(frame, frames[index + 1]))
    return replace(clip, manifest=replace(manifest,
                   paths=tuple(path for path in manifest.paths for _ in range(2)),
                   fps=manifest.fps * 2), frames=tuple(output))
