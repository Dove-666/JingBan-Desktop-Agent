"""Validated, cached RGBA clips; the pet's existing frame timer drives playback.

Images are decoded in a worker once per play. No file access, image decoding or
rescaling takes place in ``advance``. All frames retain the same full canvas;
their jointly normalized character placement must be prepared by the asset tool.
"""

from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import sys

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter

from .animation_controller import AnimationState
from .async_tasks import TaskRunner
from .config import PROJECT_ROOT
from .static_idle_smoothing import smooth_static_idle


ANIMATIONS = {
    "think_scratch_head": "思考 · 挠头",
    "snarl_cute": "趴下 · 奶凶呲牙",
    "idle_puff_cheeks": "待机 · 轻轻鼓腮",
    "idle_tiptoe_look": "待机 · 踮脚眺望",
    "idle_pinch_cheeks": "待机 · 轻轻捏脸",
    "idle_sleepy_nod": "待机 · 站着打盹",
    "idle_tail_blanket": "待机 · 尾巴小毯子",
    "idle_balloon_float": "待机 · 气球轻飘",
    "idle_proud_pose": "待机 · 得意挺胸",
    "idle_tsundere": "待机 · 傲娇抱臂",
}
MAX_FRAMES = 96  # At most 384 MiB of clip pixels, plus two bounded optional bridges.
MAX_TRANSITION_MS = 1000
TRANSITION_FPS = 30  # A bounded optional bridge, separate from the clip's FPS.


class FrameAssetError(ValueError):
    """An actionable, nonfatal asset validation error."""


@dataclass(frozen=True)
class ClipManifest:
    name: str
    paths: tuple[Path, ...]
    fps: float
    loop: bool
    completion: str
    anchor: tuple[float, float]
    transition_ms: int = 0
    static_pose_idle: bool = False

    @property
    def duration(self) -> float:
        return len(self.paths) / self.fps


@dataclass(frozen=True)
class LoadedClip:
    manifest: ClipManifest
    frames: tuple[QImage, ...]
    entry_frames: tuple[QImage, ...] = ()
    static_frame: QImage | None = None


def read_manifest(directory: Path, name: str) -> ClipManifest:
    """Read bounded metadata and reject any frame path escaping the clip folder."""
    if name not in ANIMATIONS:
        raise FrameAssetError("未知动画素材。")
    root = (Path(directory) / name).resolve()
    path = root / "animation.json"
    if not path.is_file():
        raise FrameAssetError("动画素材缺失，尚未制作或未就绪；需要真实视频分割后的 RGBA 序列帧。")
    try:
        if path.stat().st_size > 128 * 1024:
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
                or data.get("schema_version") != 1 or data.get("name") != name):
            raise ValueError
        if data.get("canvas") != {"width": 1024, "height": 1024}:
            raise ValueError
        count, fps = data["frame_count"], data["fps"]
        if type(count) is not int or not 2 <= count <= MAX_FRAMES:
            raise ValueError
        if type(fps) not in (int, float) or not math.isfinite(fps) or not 12 <= fps <= 24:
            raise ValueError
        if data.get("complete") is False or data.get("qa_passed") is False:
            raise FrameAssetError("动画尚未制作完成或未通过素材验收，不能播放。")
        origin = data.get("origin", {})
        if not isinstance(origin, dict) or origin.get("synthetic") is True:
            raise FrameAssetError("该素材为技术测试输出，不能作为真实角色动画播放。")
        loop, completion = data.get("loop", False), data.get("completion", "idle")
        if type(loop) is not bool or completion not in {"idle", "hold_last"}:
            raise ValueError
        transition_ms = data.get("transition_ms", 0)
        if type(transition_ms) is not int or not 0 <= transition_ms <= MAX_TRANSITION_MS:
            raise ValueError
        anchor = data.get("anchor", {"x": .5, "y": .9375})
        values = (anchor["x"], anchor["y"])
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
            raise ValueError
        if "duration_ms" in data and abs(float(data["duration_ms"]) - count / fps * 1000) > 2:
            raise ValueError
        relative = data.get("frames")
        if relative is None:
            # A deliberately narrow pattern avoids arbitrary format evaluation.
            pattern = data.get("frame_pattern", "")
            if pattern not in {"frames/{index:04d}.png", "{index:04d}.png"}:
                raise ValueError
            start = data.get("start_index", 1)
            if type(start) is not int or start < 0:
                raise ValueError
            relative = [pattern.format(index=i) for i in range(start, start + count)]
        if not isinstance(relative, list) or len(relative) != count:
            raise ValueError
        paths = []
        for item in relative:
            if not isinstance(item, str) or Path(item).is_absolute() or Path(item).suffix.lower() != ".png":
                raise ValueError
            frame = (root / item).resolve()
            if not frame.is_relative_to(root) or frame in paths:
                raise ValueError
            if not frame.is_file():
                raise FrameAssetError("动画帧不完整，尚未就绪；请补齐 manifest 列出的 PNG。")
            paths.append(frame)
        return ClipManifest(name, tuple(paths), float(fps), loop, completion, values, transition_ms,
                            origin.get("kind") == "static_pose_idle")
    except FrameAssetError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        raise FrameAssetError("animation.json 无效；请检查版本、1024 画布、帧列表、12～24 fps 和锚点。") from None


def load_clip(directory: Path, name: str) -> LoadedClip:
    manifest = read_manifest(directory, name)
    images = []
    for path in manifest.paths:
        reader = QImageReader(str(path), b"png")
        if reader.size().width() != 1024 or reader.size().height() != 1024:
            raise FrameAssetError("动画帧必须统一为 1024 × 1024，不能逐帧裁切。")
        image = reader.read()
        if image.isNull() or not image.hasAlphaChannel():
            raise FrameAssetError("动画 PNG 无法读取或缺少透明通道，尚未就绪。")
        if any(image.pixelColor(x, y).alpha() != 0 for x, y in ((0, 0), (1023, 0), (0, 1023), (1023, 1023))):
            raise FrameAssetError("动画画布四角必须透明，不能使用不透明色块伪装透明背景。")
        image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        if not any(image.constBits()[3 if sys.byteorder == "little" else 0::4]):
            raise FrameAssetError("动画帧完全透明，没有可显示的角色。")
        images.append(image)
    return LoadedClip(manifest, tuple(images))


def static_canvas(image: QImage) -> QImage:
    """Fit a detached static source onto the common canvas, once, before play."""
    result = QImage(1024, 1024, QImage.Format.Format_ARGB32_Premultiplied)
    result.fill(Qt.GlobalColor.transparent)
    fitted = image.scaled(1024, 1024, Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
    painter = QPainter(result)
    painter.drawImage((1024-fitted.width())//2, (1024-fitted.height())//2, fitted)
    painter.end()
    return result


def transition_frames(start: QImage, end: QImage, milliseconds: int) -> tuple[QImage, ...]:
    """Cache a premultiplied-alpha crossfade; never called by normal advance."""
    steps = max(1, math.ceil(milliseconds * TRANSITION_FPS / 1000))
    frames = [start]
    for index in range(1, steps):
        progress = index / steps
        weight = progress * progress * (3 - 2 * progress)
        frame = QImage(start.size(), QImage.Format.Format_ARGB32_Premultiplied)
        frame.fill(Qt.GlobalColor.transparent)
        painter = QPainter(frame)
        # SourceOver twice would lower opacity in overlapping opaque regions.
        # Add the two weighted premultiplied colors/alphas instead.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.setOpacity(1-weight)
        painter.drawImage(0, 0, start)
        painter.end()
        # Qt applies global opacity to Plus's result as coverage; weighting
        # Plus directly would attenuate the destination twice. Weight the
        # target on a separate transparent image, then add at full opacity.
        weighted_end = QImage(end.size(), QImage.Format.Format_ARGB32_Premultiplied)
        weighted_end.fill(Qt.GlobalColor.transparent)
        painter = QPainter(weighted_end)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.setOpacity(weight)
        painter.drawImage(0, 0, end)
        painter.end()
        painter = QPainter(frame)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        painter.drawImage(0, 0, weighted_end)
        painter.end()
        frames.append(frame)
    frames.append(end)
    return tuple(frames)


def prepare_transitions(clip: LoadedClip, source: QImage) -> LoadedClip:
    if not clip.manifest.transition_ms:
        return clip
    baseline = static_canvas(source)
    return replace(clip, static_frame=baseline,
                   entry_frames=(() if baseline == clip.frames[0] else
                                 transition_frames(baseline, clip.frames[0], clip.manifest.transition_ms)))


class ClipClock:
    """Deterministic elapsed-time clock, independent of Qt timers and disk I/O."""

    def __init__(self, manifest: ClipManifest):
        self.manifest = manifest
        self.elapsed = 0.0
        self.index = 0
        self.finished = False

    def advance(self, seconds: float, *, paused: bool = False) -> int:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("动画时间必须是非负有限数。")
        if paused or self.finished:
            return self.index
        self.elapsed += seconds
        count = len(self.manifest.paths)
        if self.manifest.loop:
            self.elapsed %= self.manifest.duration
            # Match frame selection's epsilon at a loop boundary, so frame 0
            # cannot retain almost a full cycle of elapsed time/fraction 1.
            if self.elapsed + 1e-10 >= self.manifest.duration:
                self.elapsed = 0.0
            self.index = int((self.elapsed + 1e-10) * self.manifest.fps) % count
        else:
            frame = int((self.elapsed + 1e-10) * self.manifest.fps)
            self.index = min(frame, count - 1)
            self.finished = frame >= count
        return self.index


class FrameAnimationPlayer(QObject):
    status_changed = Signal(str, str)

    def __init__(self, pet, directory: Path | None = None, parent=None):
        super().__init__(parent)
        self.pet = pet
        self.directory = directory or PROJECT_ROOT / "assets" / "character" / "animations"
        self.runner = TaskRunner(self)
        self._generation = 0
        self._closed = False
        self._clip = None
        self._clock = None
        self._installed = False
        self._loading = False
        self._loading_generation = None
        self._stop_pending = False
        self._pending_smooth_stop = False
        self._active_frames = ()
        self._shown_index = 0
        self._clip_offset = 0
        self._phase = "idle"
        self._transition_elapsed = 0.0
        self._frame_times = ()
        self._exit_home_index = None
        self._exit_home_moved = False
        self.name = ""
        self.status = "未播放素材动画；缺失素材不会用单帧假装动作。"
        pet.frame_tick.connect(self.advance)
        pet.closed.connect(self.shutdown)

    @property
    def loading(self) -> bool:
        """A decoder still owns its bounded frame allocation, even if cancelled."""
        return self._loading

    @property
    def playing(self) -> bool:
        """Include the entry/exit bridges and a deliberately held last frame."""
        return self._clip is not None

    @property
    def busy(self) -> bool:
        return self.loading or self.playing

    def catalog(self) -> list[tuple[str, str, str]]:
        result = []
        for name, label in ANIMATIONS.items():
            try:
                read_manifest(self.directory, name)
                status = "帧文件齐全，播放时后台验证透明画布；尚未验证。"
            except FrameAssetError as exc:
                status = str(exc)
            result.append((name, label, status))
        return result

    def _status(self, name, text):
        self.name, self.status = name, text
        self.status_changed.emit(name, text)

    def play(self, name: str, *, once: bool = False) -> bool:
        if self._closed:
            return False
        if self._loading:
            self._status(self.name, "上一组素材仍在后台验证，请稍候重试；不会重复排队占用内存。")
            return False
        if self.pet.animation.state in {AnimationState.DRAGGING, AnimationState.RETURNING}:
            self._status(self.name, "正在拖动或平滑恢复，素材播放保持暂停；角色恢复后可重试。")
            return False
        try:
            read_manifest(self.directory, name)
        except FrameAssetError as exc:
            self._status(name, str(exc))
            return False
        self.stop()
        self._generation += 1
        generation = self._generation
        self._loading = True
        self._loading_generation = generation
        self._status(name, "正在后台加载和验证 RGBA 序列帧…")
        # QPixmap belongs to the GUI thread; workers only receive a detached
        # QImage and paint into their own QImages.
        source = self.pet._source.toImage().copy()

        def prepare():
            clip = smooth_static_idle(load_clip(self.directory, name))
            if once:
                # Automatic idle actions finish once, without rewriting the
                # original loop/hold settings used by manual asset previews.
                clip = replace(clip, manifest=replace(clip.manifest, loop=False, completion="idle"))
            return prepare_transitions(clip, source)

        self.runner.submit(prepare, lambda result, error: self._loaded(generation, name, result, error))
        return True

    def _loaded(self, generation, name, result, error):
        if generation == self._loading_generation:
            self._loading = False
            self._loading_generation = None
        if self._closed or generation != self._generation:
            return
        if error:
            self._status(name, error)
            return
        if result.manifest.transition_ms and result.static_frame is None:
            result = prepare_transitions(result, self.pet._source.toImage())
        self._clip, self._clock = result, ClipClock(result.manifest)
        self._clip_offset = len(result.entry_frames)
        self._active_frames = result.entry_frames + result.frames
        self._phase = "enter" if result.entry_frames else "play"
        self._transition_elapsed = 0.0
        entry_duration = result.manifest.transition_ms / 1000 if result.entry_frames else 0
        count = len(result.entry_frames)
        self._frame_times = tuple(entry_duration * (i / max(1, count-1)) for i in range(count)) + tuple(
            entry_duration + i/result.manifest.fps for i in range(len(result.frames)))
        self.pet.animation.return_to_idle()
        self._status(name, "素材已验证；拖动期间冻结，释放后沿用平滑恢复。")
        self.advance(0)

    def advance(self, seconds):
        if self._clip is None or self._closed:
            return
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("动画时间必须是非负有限数。")
        paused = self.pet.animation.state in {AnimationState.DRAGGING, AnimationState.RETURNING}
        if self._stop_pending:
            if not paused:
                smooth = self._pending_smooth_stop
                self._stop_pending = self._pending_smooth_stop = False
                self.stop(smooth=smooth)
            return
        if paused:
            return
        if not self._installed:
            self.pet.set_frame_sequence(self._active_frames, self._clip.manifest.anchor,
                                        frame_times=self._frame_times)
            self._installed = True
            self._shown_index = 0
        reanchor_index = self.pet.consume_frame_reanchor()
        if reanchor_index is not None:
            if self._phase == "play":
                native_index = max(0, reanchor_index-self._clip_offset)
                self._clock.elapsed = native_index / self._clip.manifest.fps
                self._clock.index = native_index
            elif self._phase == "enter":
                self._transition_elapsed = (self._clip.manifest.transition_ms / 1000
                                            * (reanchor_index / max(1, self._clip_offset-1)))
        if self._phase in {"enter", "exit"}:
            duration = self._clip.manifest.transition_ms / 1000
            remaining = max(0.0, duration-self._transition_elapsed)
            consumed = min(seconds, remaining)
            self._transition_elapsed += consumed
            if self._transition_elapsed + 1e-10 < duration:
                count = self._clip_offset if self._phase == "enter" else len(self._active_frames)
                index = min(count-1, int(self._transition_elapsed / duration * (count-1)))
                if self._phase == "exit":
                    self._show_exit_index(index)
                else:
                    self._show_index(index)
                    self.pet.set_frame_progress(self._transition_elapsed / duration * (count-1) - index)
                return
            if self._phase == "exit":
                self._show_exit_index(len(self._active_frames)-1)
                self._clear_playback()
                self._status("", "素材动画已停止，恢复当前静态角色与待机。")
                return
            self._phase = "play"
            self._show_index(self._clip_offset)
            seconds -= consumed
        previous_elapsed = self._clock.elapsed
        index = self._clock.advance(seconds)
        if (self._clip.manifest.loop and seconds > 0
                and previous_elapsed + seconds + 1e-10 >= self._clip.manifest.duration):
            self.pet.restart_frame_cycle(self._clip_offset)
        self._show_index(self._clip_offset + index)
        fraction = max(0.0, min(1.0, self._clock.elapsed * self._clip.manifest.fps - index))
        self.pet.set_frame_progress(fraction)
        if self._clock.finished and self._clip.manifest.completion == "idle":
            # Prepared idles already finish on the exact static destination.
            # Do not append another neutral->neutral bridge or a window glide.
            # A one-pixel raster envelope difference needs no exit animation.
            at_static = (self._clip.static_frame is not None
                         and self._active_frames[self._shown_index] == self._clip.static_frame
                         and (self.pet.pos() - self.pet.frame_home_position).manhattanLength() <= 1)
            self.stop(smooth=bool(self._clip.manifest.transition_ms) and not at_static)
        elif self._clock.finished and "末帧" not in self.status:
            self._status(self.name, "动画已结束，保持末帧；点击停止可恢复静态角色。")

    def _show_index(self, index):
        if index != self._shown_index:
            self.pet.set_frame_index(index)
            self._shown_index = index

    def _show_exit_index(self, index):
        if (self._exit_home_index is not None and not self._exit_home_moved
                and index >= self._exit_home_index):
            self._show_index(self._exit_home_index)
            self.pet.place_frame_home()
            self._exit_home_moved = True
        self._show_index(index)

    def _clear_playback(self):
        self._stop_pending = self._pending_smooth_stop = False
        self._clip = self._clock = None
        self._active_frames = ()
        self._clip_offset = self._shown_index = 0
        self._phase = "idle"
        self._transition_elapsed = 0.0
        self._frame_times = ()
        self._exit_home_index = None
        self._exit_home_moved = False
        self._installed = False
        self.pet.clear_frame_sequence()
        if self.pet.animation.state != AnimationState.DRAGGING:
            self.pet.animation.return_to_idle()

    def stop(self, *, smooth=False):
        self._generation += 1
        if smooth and self._phase == "exit":
            return  # Repeated UI clicks must not restart the short exit.
        if self._installed and self.pet.animation.state in {AnimationState.DRAGGING, AnimationState.RETURNING}:
            self._stop_pending = True
            self._pending_smooth_stop = bool(smooth)
            self._status(self.name, "正在拖动，已取消后续播放；释放并平滑恢复后还原静态角色。")
            return
        if (smooth and self._installed and self._clip is not None
                and self._clip.manifest.transition_ms and self._clip.static_frame is not None):
            current = self._active_frames[self._shown_index]
            self._exit_home_index = None
            self._exit_home_moved = False
            if self.pet.pos() != self.pet.frame_home_position:
                # Interrupting an edge-avoiding pose must not drag its restored
                # static destination across the desktop. Fade out where it is,
                # relocate while fully transparent, then fade in at its home.
                blank = QImage(current.size(), QImage.Format.Format_ARGB32_Premultiplied)
                blank.fill(Qt.GlobalColor.transparent)
                half = self._clip.manifest.transition_ms // 2
                fade_out = transition_frames(current, blank, half)
                fade_in = transition_frames(blank, self._clip.static_frame,
                                             self._clip.manifest.transition_ms-half)
                self._active_frames = fade_out + fade_in[1:]
                self._exit_home_index = len(fade_out)-1
            else:
                self._active_frames = transition_frames(current, self._clip.static_frame,
                                                        self._clip.manifest.transition_ms)
            self._phase = "exit"
            self._transition_elapsed = 0.0
            self._stop_pending = self._pending_smooth_stop = False
            self.pet.set_frame_sequence(self._active_frames, self._clip.manifest.anchor, plan_motion=False)
            self._shown_index = 0
            self._status(self.name, "正在平滑恢复播放前的静态角色…")
            return
        self._clear_playback()
        self._status("", "素材动画已停止，恢复当前静态角色与待机。")

    def shutdown(self):
        self._closed = True
        self._generation += 1
        self._loading = False
        self._loading_generation = None
        self._clear_playback()
        self.pet._restore_sequence_position()
        self.runner.shutdown()
