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
