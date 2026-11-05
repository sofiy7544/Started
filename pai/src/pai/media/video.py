"""Video Processor: длительность, кадры по времени и звуковая дорожка — через PyAV (FFmpeg внутри
пакета). Видео целиком модели не отправляется: это дорого, а большинство моделей видео не
принимают. Вместо этого — равномерные кадры с отметками времени + расшифровка речи по минутам;
при вопросе о конкретном моменте модель запрашивает кадры этого отрезка.

Файл читается с диска с перемоткой (seek) — большое видео не загружается в память целиком.
"""

from __future__ import annotations

from dataclasses import dataclass

import av

from pai.media.images import pil_to_jpeg
from pai.media.safety import UnsafeFile


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


def probe(path: str) -> VideoInfo:
    try:
        with av.open(path) as c:
            if not c.streams.video:
                raise UnsafeFile("в файле нет видеодорожки")
            vs = c.streams.video[0]
            duration = 0.0
            if c.duration:
                duration = float(c.duration / av.time_base)
            elif vs.duration and vs.time_base:
                duration = float(vs.duration * vs.time_base)
            return VideoInfo(duration, vs.codec_context.width or 0, vs.codec_context.height or 0,
                             bool(c.streams.audio))
    except av.FFmpegError as exc:
        raise UnsafeFile("видео повреждено или в неподдерживаемом формате") from exc


def sample_times(duration: float, count: int, start: float = 0.0, end: float | None = None) -> list[float]:
    """Равномерные моменты внутри [start, end] (середины отрезков — без чёрных первых кадров)."""
    end = duration if end is None else min(end, duration)
    start = max(0.0, min(start, end))
    span = max(end - start, 0.0)
    if count <= 0:
        return []
    if span < 0.5:
        return [start]
    return [round(start + span * (i + 0.5) / count, 2) for i in range(count)]


def frames_at(path: str, times: list[float], *, max_side: int = 1024) -> list[tuple[float, bytes]]:
    """Кадр для каждого момента (ближайший ключевой кадр + декодирование до нужного времени)."""
    out: list[tuple[float, bytes]] = []
    try:
        with av.open(path) as c:
            vs = c.streams.video[0]
            vs.thread_type = "AUTO"
            tb = vs.time_base
            for t in times:
                try:
                    c.seek(int(t / tb) + (vs.start_time or 0), stream=vs, backward=True, any_frame=False)
                except av.FFmpegError:
                    continue
                picked = None
                for frame in c.decode(vs):
                    picked = frame
                    if frame.pts is not None and float((frame.pts - (vs.start_time or 0)) * tb) >= t - 0.04:
                        break
                if picked is not None:
                    out.append((t, pil_to_jpeg(picked.to_image(), max_side)))
    except av.FFmpegError as exc:
        if not out:
            raise UnsafeFile("не удалось прочитать кадры видео") from exc
    return out


def mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"
