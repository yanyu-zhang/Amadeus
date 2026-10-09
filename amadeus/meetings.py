import asyncio
import ctypes.util
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import voice_recv

from .capture import CHUNK_SECONDS, AudioChunk, MeetingSink
from .local_models import LocalModels

LOG = logging.getLogger(__name__)
MEETING_ID_FORMAT = "%Y-%m-%d_%H-%M-%S_UTC%z"


def valid_meeting_id(value):
    # Existing saved meetings remain accessible by their original IDs.
    if re.fullmatch(r"[0-9a-f]{12}", value):
        return True
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_UTC[+-]\d{4})(?:_[1-9]\d*)?", value)
    if match is None:
        return False
    try:
        datetime.strptime(match[1], "%Y-%m-%d_%H-%M-%S_UTC%z")
    except ValueError:
        return False
    return True


def load_opus():
    if discord.opus.is_loaded():
        return
    candidates = [
        os.getenv("OPUS_LIBRARY"),
        ctypes.util.find_library("opus"),
        "/opt/homebrew/lib/libopus.dylib",
        "/usr/local/lib/libopus.dylib",
    ]
    for candidate in candidates:
        if candidate:
            try:
                discord.opus.load_opus(candidate)
                return
            except OSError:
                continue
    raise RuntimeError("找不到 Opus。macOS 请运行 brew install opus；Linux 安装 libopus0。")


def save_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@dataclass
class Meeting:
    directory: Path
    metadata: dict
    voice: object = None
    sink: object = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker: object = None
    ticker: object = None
    finishing: object = None
    failures: list = field(default_factory=list)
    captured: int = 0
    transcribed: int = 0
    stopped: bool = False

    @property
    def id(self):
        return self.directory.name

    def persist(self):
        save_json(self.directory / "meeting.json", self.metadata)


class MeetingManager:
    def __init__(self, bot, root=None, models=None):
        self.bot = bot
        self.root = Path(root or "data/meetings")
        self.timezone = ZoneInfo(getattr(bot, "timezone", "America/Los_Angeles"))
        self.models = models or LocalModels()
        self.active = {}
        self.sessions = {}
        self.locks = {}
        self.summary_lock = asyncio.Lock()

    def guild_lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    def _create_directory(self, started_at):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        timestamp = started_at.astimezone(self.timezone).strftime(MEETING_ID_FORMAT)
        attempt = 1
        while True:
            name = timestamp if attempt == 1 else f"{timestamp}_{attempt}"
            directory = self.root / name
            try:
                directory.mkdir(mode=0o700)
                return directory
            except FileExistsError:
                # Simultaneous starts in different guilds must not share recordings.
                attempt += 1

    async def start(self, guild, voice_channel, text_channel, owner, title):
        async with self.guild_lock(guild.id):
            if guild.id in self.active:
                raise RuntimeError("这个服务器已有正在记录的会议。请先 /meeting stop。")
            if guild.voice_client:
                raise RuntimeError("我已连接到语音频道，请先结束当前语音连接。")
            try:
                max_mb = int(os.getenv("MAX_RECORDING_MB", "2048"))
                if max_mb <= 0:
                    raise ValueError
            except ValueError:
                raise RuntimeError("MAX_RECORDING_MB 必须为正整数。") from None
            load_opus()
            LOG.info(
                "准备开始会议 guild=%s voice_channel=%s，检查本地模型", guild.id, voice_channel.id
            )
            await self.models.ready(self.bot.http_session)
            started_at = datetime.now(UTC)
            directory = self._create_directory(started_at)
            metadata = {
                "id": directory.name,
                "guild_id": guild.id,
                "owner_id": owner.id,
                "voice_channel_id": voice_channel.id,
                "text_channel_id": text_channel.id,
                "title": title,
                "started_at": started_at.isoformat(),
                "timezone": self.timezone.key,
                "status": "connecting",
            }
            meeting = Meeting(directory, metadata)
            meeting.persist()
            loop = asyncio.get_running_loop()

            def enqueue(chunk):
                def put():
                    meeting.captured += 1
                    meeting.queue.put_nowait(chunk)
                    LOG.debug("meeting=%s 音频段入队 pending=%d", meeting.id, meeting.queue.qsize())

                loop.call_soon_threadsafe(put)

            def stop_with_reason(reason):
                def schedule():
                    if guild.id in self.active and not meeting.stopped:
                        task = asyncio.create_task(self.stop(guild.id, reason))
                        task.add_done_callback(self._task_done)

                loop.call_soon_threadsafe(schedule)

            def after(error):
                if error:
                    LOG.error("Voice receiver failed: %s", type(error).__name__)
                    stop_with_reason("语音接收中断，正在保存已收到的记录。")

            meeting.sink = MeetingSink(
                directory,
                enqueue,
                lambda: stop_with_reason("录音达到本机容量上限，已停止记录。"),
                max_mb * 1024 * 1024,
            )
            try:
                LOG.info("meeting=%s 正在连接语音频道 channel=%s", meeting.id, voice_channel.id)
                meeting.voice = await voice_channel.connect(
                    cls=voice_recv.VoiceRecvClient,
                    self_deaf=False,
                    self_mute=True,
                    timeout=30,
                    reconnect=True,
                )
                self.active[guild.id] = meeting
                self.sessions[meeting.id] = meeting
                meeting.worker = asyncio.create_task(self._transcribe_worker(meeting))
                meeting.voice.listen(meeting.sink, after=after)
                meeting.metadata["status"] = "recording"
                meeting.persist()
                meeting.ticker = asyncio.create_task(self._ticker(meeting))
                meeting.ticker.add_done_callback(self._task_done)
                LOG.info(
                    "meeting=%s 已开始收音，每 %d 秒分段，转写在后台运行", meeting.id, CHUNK_SECONDS
                )
            except Exception:
                LOG.exception("meeting=%s 语音连接失败", meeting.id)
                self.active.pop(guild.id, None)
                meeting.sink.cleanup()
                if meeting.voice:
                    await meeting.voice.disconnect(force=True)
                if meeting.worker:
                    meeting.worker.cancel()
                    await asyncio.gather(meeting.worker, return_exceptions=True)
                meeting.metadata["status"] = "connection_failed"
                meeting.persist()
                raise
            return meeting

    def _task_done(self, task):
        if not task.cancelled() and task.exception():
            LOG.error("Meeting task failed: %s", type(task.exception()).__name__)

    async def _ticker(self, meeting):
        last_report = time.monotonic()
        try:
            while not meeting.stopped:
                await asyncio.sleep(1)
                await asyncio.to_thread(meeting.sink.flush)
                if time.monotonic() - last_report >= 10:
                    sink = meeting.sink
                    LOG.info(
                        "meeting=%s 收音进度 packets=%d unknown_packets=%d audio_mb=%.1f chunks=%d transcribed=%d pending=%d failed=%d",
                        meeting.id,
                        sink.packets,
                        sink.unknown_packets,
                        sink.total_bytes / 1024 / 1024,
                        meeting.captured,
                        meeting.transcribed,
                        meeting.queue.qsize(),
                        len(meeting.failures),
                    )
                    if not sink.packets:
                        LOG.warning(
                            "meeting=%s 尚未收到可解码语音，请发言并检查 bot 是否被服务器设为耳聋",
                            meeting.id,
                        )
                    last_report = time.monotonic()
                if not meeting.voice.is_connected() or not meeting.voice.is_listening():
                    await self.stop(meeting.metadata["guild_id"], "语音连接或接收已中断。")
                    return
        except asyncio.CancelledError:
            return

    async def _process_chunk(self, meeting, chunk):
        output = chunk.path.with_suffix(".json")
        if output.exists():
            LOG.debug("meeting=%s 跳过已转写音频段 file=%s", meeting.id, chunk.path.name)
            return
        started = time.monotonic()
        LOG.info("meeting=%s 开始中文转写 file=%s", meeting.id, chunk.path.name)
        segments = await self.models.transcribe(chunk.path)
        save_json(
            output,
            [
                {
                    "time": chunk.offset + offset,
                    "speaker": chunk.speaker,
                    "user_id": chunk.user_id,
                    "text": text,
                }
                for offset, text in segments
            ],
        )
        LOG.info(
            "meeting=%s 转写完成 file=%s segments=%d elapsed=%.1fs",
            meeting.id,
            chunk.path.name,
            len(segments),
            time.monotonic() - started,
        )

    async def _transcribe_worker(self, meeting):
        while True:
            chunk = await meeting.queue.get()
            try:
                if chunk is None:
                    return
                await self._process_chunk(meeting, chunk)
                meeting.transcribed += 1
            except Exception:
                LOG.exception("Local transcription failed for meeting %s", meeting.id)
                meeting.failures.append(chunk.path.name)
            finally:
                meeting.queue.task_done()

    async def stop(self, guild_id, reason=None):
        async with self.guild_lock(guild_id):
            meeting = self.active.pop(guild_id, None)
            if meeting is None:
                raise RuntimeError("当前没有正在记录的会议。")
            meeting.stopped = True
            LOG.info("meeting=%s 正在停止收音并保存最后一段", meeting.id)
            if meeting.ticker and meeting.ticker is not asyncio.current_task():
                meeting.ticker.cancel()
                await asyncio.gather(meeting.ticker, return_exceptions=True)
            meeting.voice.stop_listening()
            await asyncio.to_thread(meeting.sink.cleanup)
            # Drain all thread callbacks before placing the worker's sentinel.
            await asyncio.sleep(0)
            try:
                await meeting.voice.disconnect(force=True)
            finally:
                meeting.metadata.update(
                    status="processing", stopped_at=datetime.now(UTC).isoformat()
                )
                if reason:
                    meeting.metadata["stop_reason"] = reason
                meeting.persist()
                meeting.queue.put_nowait(None)
                meeting.finishing = asyncio.create_task(self._finish(meeting))
                meeting.finishing.add_done_callback(self._task_done)
                LOG.info(
                    "meeting=%s 收音已结束，等待剩余转写 pending=%d",
                    meeting.id,
                    meeting.queue.qsize() - 1,
                )
            return meeting

    async def _notify(self, meeting, content, file=None):
        channel = self.bot.get_channel(meeting.metadata["text_channel_id"])
        if channel is None:
            channel = await self.bot.fetch_channel(meeting.metadata["text_channel_id"])
        kwargs = {"file": file} if file else {}
        await channel.send(content, allowed_mentions=discord.AllowedMentions.none(), **kwargs)

    async def _finish(self, meeting):
        try:
            if meeting.metadata.get("stop_reason"):
                await self._notify(meeting, meeting.metadata["stop_reason"])
        except discord.HTTPException:
            LOG.warning("Could not publish stop notice for %s", meeting.id)
        if meeting.worker:
            await meeting.worker
        LOG.info("meeting=%s 转写队列已处理完，开始整理总结", meeting.id)
        try:
            await self.generate_summary(meeting)
        except Exception:
            meeting.metadata["status"] = "summary_failed"
            meeting.persist()
            LOG.exception("Summary failed for %s", meeting.id)
            try:
                await self._notify(
                    meeting,
                    f"会议 `{meeting.id}` 的记录已保存，但总结未完成。"
                    "本地服务恢复后可用 /meeting summary 重试。",
                )
            except discord.HTTPException:
                LOG.warning("Could not publish failure notice for %s", meeting.id)

    def find(self, guild_id, meeting_id=None):
        if meeting_id is None:
            paths = self.root.glob("*/meeting.json")
        else:
            if not valid_meeting_id(meeting_id):
                raise RuntimeError("会议 ID 格式不正确。")
            paths = [self.root / meeting_id / "meeting.json"]
        candidates = []
        for path in paths:
            if not path.exists():
                continue
            metadata = json.loads(path.read_text(encoding="utf-8"))
            if metadata["guild_id"] == guild_id:
                candidates.append((metadata["started_at"], path, metadata))
        if candidates:
            _, path, metadata = max(candidates, key=lambda item: item[0])
            return self.sessions.get(path.parent.name) or Meeting(
                path.parent, metadata, stopped=True
            )
        raise RuntimeError("未找到这个服务器的会议记录。")

    async def generate_summary(self, meeting):
        LOG.info("meeting=%s 等待总结任务", meeting.id)
        async with self.summary_lock:
            if self.active.get(meeting.metadata["guild_id"]) is meeting:
                raise RuntimeError("请先 /meeting stop，再生成总结。")
            # Retry failed transcription from persisted chunks, including after a restart.
            chunks_path = meeting.directory / "chunks.jsonl"
            chunks = (
                [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines()]
                if chunks_path.exists()
                else []
            )
            failed = []
            for item in chunks:
                chunk = AudioChunk(
                    meeting.directory / item["file"],
                    item["user_id"],
                    item["speaker"],
                    item["offset"],
                )
                try:
                    await self._process_chunk(meeting, chunk)
                except Exception:
                    LOG.exception("Chunk retry failed for %s", meeting.id)
                    failed.append(chunk.path.name)
            rows = []
            for item in chunks:
                path = (meeting.directory / item["file"]).with_suffix(".json")
                if path.exists():
                    rows.extend(json.loads(path.read_text(encoding="utf-8")))
            rows.sort(key=lambda row: row["time"])
            transcript = "\n".join(
                f"[{int(row['time']) // 60:02d}:{int(row['time']) % 60:02d}] "
                f"{row['speaker']} ({row['user_id']}): {row['text']}"
                for row in rows
            )
            (meeting.directory / "transcript.md").write_text(transcript, encoding="utf-8")
            LOG.info(
                "meeting=%s 转写已保存 rows=%d chars=%d failed_chunks=%d",
                meeting.id,
                len(rows),
                len(transcript),
                len(failed),
            )
            warning = (
                f"\n\n注意：{len(failed)} 个音频片段转写失败，本总结不完整。" if failed else ""
            )
            if transcript:
                try:
                    body = await self.models.summarize(self.bot.http_session, transcript)
                finally:
                    save_json(
                        meeting.directory / "summary.metrics.json", self.models.last_summary_stats
                    )
                    save_json(
                        meeting.directory / "summary.evidence.json",
                        self.models.last_summary_evidence,
                    )
            else:
                body = "未识别到可用语音内容，无法生成会议总结。请检查发言音量、语音接收状态和本地转写模型。"
            if meeting.metadata.get("stop_reason"):
                warning += "\n\n记录结束原因：" + meeting.metadata["stop_reason"]
            summary = f"# {meeting.metadata['title']}\n\n会议 ID：{meeting.id}\n\n{body}{warning}\n"
            path = meeting.directory / "summary.md"
            path.write_text(summary, encoding="utf-8")
            LOG.info("meeting=%s 总结已保存 path=%s", meeting.id, path.resolve())
            meeting.metadata.update(
                status="complete" if not failed else "partial", failed_chunks=failed
            )
            meeting.persist()
            try:
                preview = (
                    summary if len(summary) <= 1800 else summary[:1700] + "\n\n完整总结见附件。"
                )
                with path.open("rb") as file:
                    await self._notify(
                        meeting, preview, discord.File(file, filename=f"meeting-{meeting.id}.md")
                    )
                LOG.info(
                    "meeting=%s 总结已发送到 Discord channel=%s",
                    meeting.id,
                    meeting.metadata["text_channel_id"],
                )
            except discord.HTTPException:
                meeting.metadata["status"] = "publish_failed"
                meeting.persist()
                raise
            return path

    async def close(self):
        # Flush audio on orderly shutdown; processing can be resumed with /meeting summary.
        for guild_id in list(self.active):
            try:
                await self.stop(guild_id, "Bot 正在重启，已停止记录。")
            except Exception:
                LOG.exception("Could not stop voice capture on shutdown")
        tasks = []
        for meeting in self.sessions.values():
            for task in (meeting.worker, meeting.finishing, meeting.ticker):
                if task and not task.done():
                    task.cancel()
                    tasks.append(task)
        await asyncio.gather(*tasks, return_exceptions=True)
        self.models.close()
