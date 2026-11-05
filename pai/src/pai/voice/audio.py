"""Работа со звуком через PyAV (FFmpeg внутри пакета — отдельно ставить ffmpeg не нужно)."""

from __future__ import annotations

import io

import av


def duration_seconds(data: bytes) -> float:
    """Длительность аудио. Для потоков без заголовка длительности — по сумме кадров."""
    with av.open(io.BytesIO(data)) as container:
        if container.duration:
            return float(container.duration / av.time_base)
        stream = container.streams.audio[0]
        total = 0.0
        for frame in container.decode(stream):
            if frame.duration and frame.time_base:
                total += float(frame.duration * frame.time_base)
            elif frame.sample_rate:
                total += frame.samples / frame.sample_rate
        return total


def is_ogg_opus(data: bytes) -> bool:
    try:
        with av.open(io.BytesIO(data)) as c:
            return c.format.name == "ogg" and c.streams.audio[0].codec_context.name == "opus"
    except (av.FFmpegError, IndexError):
        return False


def to_ogg_opus(data: bytes, bitrate: int = 48_000) -> bytes:
    """Перекодирует любой звук (mp3, wav…) в OGG/Opus — формат голосовых сообщений Telegram."""
    if is_ogg_opus(data):
        return data
    out = io.BytesIO()
    with av.open(io.BytesIO(data)) as src, av.open(out, "w", format="ogg") as dst:
        stream = dst.add_stream("libopus", rate=48_000)
        stream.bit_rate = bitrate
        stream.layout = "mono"
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48_000)
        for frame in src.decode(audio=0):
            for rf in resampler.resample(frame):
                for packet in stream.encode(rf):
                    dst.mux(packet)
        for rf in resampler.resample(None):
            for packet in stream.encode(rf):
                dst.mux(packet)
        for packet in stream.encode(None):
            dst.mux(packet)
    return out.getvalue()


def decode_pcm_16k(data: bytes):  # type: ignore[no-untyped-def]
    """Звук → моно 16 кГц float32 (numpy) для faster-whisper. Декодируем сами: faster-whisper 1.2
    вызывает av.open с параметром, которого нет в PyAV 16+."""
    import numpy as np

    chunks = []
    resampler = av.AudioResampler(format="flt", layout="mono", rate=16_000)
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            for rf in resampler.resample(frame):
                chunks.append(rf.to_ndarray().reshape(-1))
    for rf in resampler.resample(None):
        chunks.append(rf.to_ndarray().reshape(-1))
    return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, dtype=np.float32)


def ogg_chunks(
    src: bytes | str, *, chunk_seconds: float = 120.0, max_seconds: float | None = None
) -> list[tuple[float, float, bytes]]:
    """Звук (или звуковая дорожка видео) → части OGG/Opus по chunk_seconds: (начало, конец, данные).
    Для длинных записей: каждая часть распознаётся отдельно — так видно, где что сказано, и
    не упираемся в лимиты сервиса распознавания. Нет звука — пустой список."""
    source = io.BytesIO(src) if isinstance(src, bytes) else src
    chunks: list[tuple[float, float, bytes]] = []
    with av.open(source) as container:
        if not container.streams.audio:
            return []
        stream_in = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48_000)
        state: dict = {}

        def open_chunk(start: float) -> None:
            buf = io.BytesIO()
            out = av.open(buf, "w", format="ogg")
            st = out.add_stream("libopus", rate=48_000)
            st.bit_rate = 32_000
            st.layout = "mono"
            state.update(buf=buf, out=out, stream=st, start=start, samples=0)

        def close_chunk() -> None:
            if not state:
                return
            for packet in state["stream"].encode(None):
                state["out"].mux(packet)
            state["out"].close()
            seconds = state["samples"] / 48_000
            if seconds > 0.3:
                chunks.append((state["start"], state["start"] + seconds, state["buf"].getvalue()))
            state.clear()

        position = 0.0
        open_chunk(0.0)

        def feed(frames) -> bool:  # type: ignore[no-untyped-def]
            nonlocal position
            for rf in frames:
                if max_seconds is not None and position >= max_seconds:
                    return False
                if state["samples"] / 48_000 >= chunk_seconds:
                    close_chunk()
                    open_chunk(position)
                for packet in state["stream"].encode(rf):
                    state["out"].mux(packet)
                state["samples"] += rf.samples
                position += rf.samples / 48_000
            return True

        try:
            for frame in container.decode(stream_in):
                if not feed(resampler.resample(frame)):
                    break
            else:
                feed(resampler.resample(None))
        except av.FFmpegError:
            pass  # обрезанный/повреждённый хвост — используем то, что успели прочитать
        close_chunk()
    return chunks
