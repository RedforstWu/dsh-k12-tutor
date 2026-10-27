"""Bounded Microsoft online synthesis. JSON stdin, MP3 stdout."""
from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path

VOICES = {voice["id"] for voice in json.loads(Path(__file__).with_name("online_voices.json").read_text(encoding="utf-8"))}
MAX_AUDIO = 2 * 1024 * 1024


def validate(value):
    if not isinstance(value, dict) or set(value) != {"voice", "text", "rate"}:
        raise ValueError("invalid speech request")
    if not isinstance(value["voice"], str) or value["voice"] not in VOICES or not isinstance(value["text"], str) or not 0 < len(value["text"].strip()) <= 2000:
        raise ValueError("invalid speech request")
    rate = value["rate"]
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0.5 <= rate <= 2:
        raise ValueError("invalid speech rate")
    return value["text"], f"{round((rate - 1) * 100):+d}%"


async def synthesize(value, communicate=None):
    text, rate = validate(value)
    if communicate is None:
        from edge_tts import Communicate
        communicate = Communicate
    audio = bytearray()
    async with asyncio.timeout(45):
        async for item in communicate(text, value["voice"], rate=rate).stream():
            if item["type"] == "audio":
                audio.extend(item["data"])
                if len(audio) > MAX_AUDIO:
                    raise ValueError("speech audio too large")
    if not audio:
        raise ValueError("empty speech audio")
    return bytes(audio)


def main():
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError("request too large")
        audio = asyncio.run(synthesize(json.loads(raw)))
        sys.stdout.buffer.write(audio)
    except Exception:
        print("online speech unavailable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
