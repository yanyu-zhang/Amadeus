"""Read local meeting files without connecting to Discord or starting another bot."""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path


def snapshot():
    records = []
    for path in Path("data/meetings").glob("*/meeting.json"):
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            records.append((metadata["started_at"], path.parent, metadata))
        except (OSError, ValueError, KeyError):
            continue
    if not records:
        return "尚无本地会议记录。"
    lines = []
    labels = {
        "recording": "录音中",
        "processing": "转写/总结中",
        "complete": "已完成",
        "partial": "部分完成",
        "summary_failed": "总结失败",
        "publish_failed": "发布失败",
    }
    for _, directory, metadata in sorted(records, reverse=True)[:5]:
        audio = list(directory.glob("*.wav"))
        size = sum(path.stat().st_size for path in audio if path.exists())
        completed = sum(path.with_suffix(".json").exists() for path in audio)
        manifest = directory / "chunks.jsonl"
        saved = len(manifest.read_text(encoding="utf-8").splitlines()) if manifest.exists() else 0
        files = [path for path in directory.iterdir() if path.is_file()]
        last_update = max(
            (path.stat().st_mtime for path in files if path.exists()), default=time.time()
        )
        state = labels.get(metadata["status"], metadata["status"])
        lines.append(
            f"meeting={directory.name} 状态={state} 音频={size / 1024 / 1024:.1f}MB "
            f"转写={completed}/{saved} 当前音轨={max(0, len(audio) - saved)} "
            f"距文件更新={max(0, int(time.time() - last_update))}秒"
        )
        if (directory / "summary.md").exists():
            lines.append(f"  总结文件：{(directory / 'summary.md').resolve()}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="只读查看本地会议进度，不启动 bot")
    parser.add_argument("--once", action="store_true", help="只输出一次")
    args = parser.parse_args()
    try:
        while True:
            print(datetime.now().astimezone().strftime("%H:%M:%S"), snapshot(), flush=True)
            if args.once:
                return
            time.sleep(5)
    except KeyboardInterrupt:
        return
