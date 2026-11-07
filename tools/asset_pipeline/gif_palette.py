"""Stable GIF colors trained from the full animation, including brief accents.

GIF has only 256 colors. A palette learned from a closed-mouth frame can map a
later red mouth to dark gray. Every frame contributes here, and optional small
focus boxes give transient face/prop colors enough representation. This does
not alter the source images, alpha, frame order, geometry, or timing.
"""

from __future__ import annotations

from collections.abc import Sequence

from PIL import Image


def quantize_animation(
    frames: Sequence[Image.Image],
    *,
    focus_boxes: Sequence[tuple[int, int, int, int]] = (),
    colors: int = 256,
) -> list[Image.Image]:
    """Return indexed frames sharing one palette sampled from the whole clip.

    Inputs must already be RGB viewing copies on their intended background.
    Focus boxes use those viewing copies' pixel coordinates, not runtime or
    source-video coordinates. Each whole frame and each focus box contribute an
    equal 96x96 sample, so a small, briefly visible mouth is not swamped by hair
    and background. Nearest sampling retains actual colors rather than creating
    new averages that can erase tiny accents before quantization.
    """
    if not frames:
        raise ValueError("Need at least one RGB animation frame")
    if type(colors) is not int or not 2 <= colors <= 256:
        raise ValueError("GIF palette size must be an integer from 2 to 256")
    size = frames[0].size
    if any(frame.mode != "RGB" or frame.size != size for frame in frames):
        raise ValueError("Viewing frames must be RGB and share one canvas")
    boxes = tuple(focus_boxes)
    for box in boxes:
        if (len(box) != 4 or any(type(value) is not int for value in box)
                or not 0 <= box[0] < box[2] <= size[0]
                or not 0 <= box[1] < box[3] <= size[1]):
            raise ValueError("Focus boxes must fit the viewing-copy canvas")

    side = 96
    samples = len(frames) * (1 + len(boxes))
    training = Image.new("RGB", (side, side * samples))
    offset = 0
    for frame in frames:
        for crop in (None, *boxes):
            source = frame if crop is None else frame.crop(crop)
            sample = source.resize((side, side), Image.Resampling.NEAREST)
            training.paste(sample, (0, offset))
            offset += side
    trained = training.quantize(colors=colors, method=Image.Quantize.MEDIANCUT,
                                dither=Image.Dither.NONE)
    palette = Image.new("P", (1, 1))
    palette.putpalette(trained.getpalette())
    return [frame.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)
            for frame in frames]
