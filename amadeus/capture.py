"""Write bounded per-speaker WAV chunks from the receiver's audio thread."""

import json
import logging
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from discord.ext import voice_recv

RATE = 48000
CHANNELS = 2
FRAME_BYTES = 4  # 16-bit stereo
CHUNK_SECONDS = 60
LOG = logging.getLogger(__name__)


@dataclass
class AudioChunk:
    path: Path
    user_id: int
    speaker: str
    offset: float


class MeetingSink(voice_recv.AudioSink):
    def __init__(self, directory, on_chunk, on_limit, max_bytes):
        super().__init__()
        self.directory = Path(directory)
        self.on_chunk = on_chunk
        self.on_limit = on_limit
        self.max_bytes = max_bytes
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.streams = {}
        self.total_bytes = 0
        self.packets = 0
        self.unknown_packets = 0
        self.closed = False
        self.limited = False

    def wants_opus(self):
        return False

    def write(self, user, data):
        if not data.pcm:
            return
        with self.lock:
            if self.closed or self.limited:
                return
            if user is None:
                self.unknown_packets += 1
                return
            if user.bot:
                return
            offset = max(0, time.monotonic() - self.started)
            index = int(offset // CHUNK_SECONDS)
            current = self.streams.get(user.id)
            if current and current[0] != index:
                self._finish(user.id)
                current = None
            if current is None:
                path = self.directory / f"{index:08d}-{user.id}.wav"
                # Streams span callbacks and are closed on rotation or cleanup.
                stream = wave.open(str(path), "wb")  # noqa: SIM115
                stream.setnchannels(CHANNELS)
                stream.setsampwidth(2)
                stream.setframerate(RATE)
                chunk = AudioChunk(path, user.id, user.display_name, index * CHUNK_SECONDS)
                current = (index, stream, chunk)
                self.streams[user.id] = current
            _, stream, _ = current
            # Preserve pauses and the relative timing of overlapping speakers.
            target_frame = int((offset - index * CHUNK_SECONDS) * RATE)
            gap = max(0, target_frame - stream.getnframes())
            pcm = bytes(data.pcm)
            growth = gap * FRAME_BYTES + len(pcm)
            if self.total_bytes + growth > self.max_bytes:
                self.limited = True
                LOG.warning(
                    "meeting=%s 录音达到容量上限 bytes=%d", self.directory.name, self.total_bytes
                )
                self.on_limit()
                return
            if gap:
                stream.writeframesraw(b"\0" * (gap * FRAME_BYTES))
            stream.writeframesraw(pcm)
            self.total_bytes += growth
            self.packets += 1

    def _finish(self, user_id):
        _, stream, chunk = self.streams.pop(user_id)
        frames = stream.getnframes()
        stream.close()
        if not frames:
            chunk.path.unlink(missing_ok=True)
            return
        # Persist the queue before notifying the asyncio worker, for restart recovery.
        with (self.directory / "chunks.jsonl").open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "file": chunk.path.name,
                        "user_id": chunk.user_id,
                        "speaker": chunk.speaker,
                        "offset": chunk.offset,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        self.on_chunk(chunk)
        LOG.info(
            "meeting=%s 音频段已保存 file=%s speaker_id=%s offset=%.1fs duration=%.1fs",
            self.directory.name,
            chunk.path.name,
            chunk.user_id,
            chunk.offset,
            frames / RATE,
        )

    def flush(self):
        with self.lock:
            current_index = int((time.monotonic() - self.started) // CHUNK_SECONDS)
            for user_id, (index, _, _) in list(self.streams.items()):
                if index < current_index:
                    self._finish(user_id)

    def cleanup(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for user_id in list(self.streams):
                self._finish(user_id)
