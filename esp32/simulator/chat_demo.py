#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Conversation demo for the UI simulator.

Type a message in the terminal and the simulator window plays a voice turn the
way the firmware does (muse_voice.c): listening, thinking with the transcript,
then speaking the reply a page at a time, and back to idle. Nothing is sent to
Muse.

By default replies come from a few built-in rules. With --api-url, they come
from a chat model behind any OpenAI-compatible endpoint, such as a local proxy:

    python3 esp32/simulator/chat_demo.py
    python3 esp32/simulator/chat_demo.py --api-url http://127.0.0.1:8045/v1
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import random
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_SIM = Path(__file__).resolve().parent / "build" / "muse_simulator"
FRAME_S = 0.05
# The heard reply page on the simulator's 412 px screen ("reply pages: 18 x 3
# heard" in its log). The voice task pages replies to this.
PAGE_COLS, PAGE_LINES = 18, 3
DEFAULT_MODEL = "claude-sonnet-4-6"
SYSTEM_PROMPT = (
    "You are Muse, a friendly voice assistant living on a small round-screen "
    "gadget. Your words are spoken aloud and shown a few lines at a time, so "
    "answer in one to three short, natural sentences. No markdown, lists, "
    "emoji or code. Reply in the language the user writes in."
)


def ascii_text(text: str) -> str:
    """The caption font is ASCII-only: drop accents (and map d-stroke)."""
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    plain = " ".join(plain.split())
    return "".join(c for c in plain if 32 <= ord(c) < 127)


def is_vietnamese(text: str) -> bool:
    return ascii_text(text) != text or any(
        w in ascii_text(text).lower().split()
        for w in ("toi", "ban", "gio", "ngay", "chao", "khong", "gi", "nhe", "oi")
    )


def machine_status(vi: bool) -> str:
    parts = []
    try:
        load = os.getloadavg()[0]
        parts.append(f"tải CPU {load:.2f}" if vi else f"CPU load {load:.2f}")
    except OSError:
        pass
    try:
        info = dict(line.split(":", 1) for line in open("/proc/meminfo"))
        total = int(info["MemTotal"].split()[0]) // 1024
        free = int(info["MemAvailable"].split()[0]) // 1024
        parts.append(f"RAM trống {free} / {total} MB" if vi else f"{free} of {total} MB RAM free")
    except (OSError, KeyError, ValueError):
        pass
    try:
        temp = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
        parts.append(f"nhiệt độ {temp:.0f}°C" if vi else f"{temp:.0f}°C")
    except (OSError, ValueError):
        pass
    if not parts:
        return "Mình không đọc được thông số máy." if vi else "I can't read this machine's stats."
    return ("Máy đang ổn: " if vi else "This machine: ") + ", ".join(parts) + "."


JOKES = {
    True: [
        "Tại sao lập trình viên thích trời tối? Vì ánh sáng thu hút bug.",
        "Có 10 loại người: người hiểu nhị phân và người không.",
    ],
    False: [
        "Why do programmers prefer dark mode? Because light attracts bugs.",
        "There are 10 kinds of people: those who know binary and those who don't.",
    ],
}


def reply_for(message: str) -> tuple[str, str]:
    """Returns (reply, face at the end) for a message."""
    vi = is_vietnamese(message)
    words = ascii_text(message).lower()
    now = datetime.datetime.now()

    def has(*keys: str) -> bool:
        return any(k in words for k in keys)

    if has("cam on", "thank"):
        return ("Không có gì, rất vui được giúp bạn!" if vi else "Any time, happy to help!"), "happy"
    if has("chao", "hello", "hi ", "hey") or words.strip() in ("hi", "hey"):
        return ("Xin chào! Mình là Muse bản giả lập. Bạn cần gì nào?" if vi
                else "Hi! I'm the simulated Muse. What can I do for you?"), "happy"
    if has("may gio", "gio", "time"):
        return (f"Bây giờ là {now:%H:%M}." if vi else f"It's {now:%H:%M}."), "idle"
    if has("ngay", "hom nay", "date", "today"):
        return (f"Hôm nay là ngày {now:%d/%m/%Y}." if vi else f"Today is {now:%A, %B %d}."), "idle"
    if has("may tinh", "cpu", "ram", "nhiet", "status", "health", "tinh trang"):
        return machine_status(vi), "idle"
    if has("dua", "cuoi", "joke", "funny"):
        return random.choice(JOKES[vi]), "happy"
    if has("ban la ai", "ten", "who are you", "your name"):
        return ("Mình là Muse, chạy trong bộ giả lập giao diện. Ở thiết bị thật, mình sẽ "
                "nghe giọng nói của bạn và trả lời qua loa." if vi else
                "I'm Muse, running in the UI simulator. On a real gadget I'd hear your "
                "voice and answer through the speaker."), "idle"
    if has("bao loi", "show error"):
        return ("CAN'T REACH MUSE"), "error"
    return ("Đây là bản demo offline nên mình chưa trả lời được câu đó. Thử hỏi mình mấy "
            "giờ, hôm nay ngày gì, tình trạng máy, hoặc kể chuyện cười nhé." if vi else
            "This is an offline demo, so I can't answer that one. Try asking the time, "
            "the date, this machine's status, or for a joke."), "idle"


class ChatModel:
    """A chat model behind an OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, api_url: str, model: str, api_key: str) -> None:
        self.url = api_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]

    def reply(self, message: str) -> str:
        messages = self.history + [{"role": "user", "content": message}]
        body = json.dumps({"model": self.model, "max_tokens": 400, "messages": messages})
        request = urllib.request.Request(self.url, data=body.encode(), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        })
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc
        try:
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected response: {str(data)[:300]}") from exc
        if not text:
            raise RuntimeError("empty reply")
        # Keep the turn only once it succeeded, so a failure can be retried.
        self.history = messages + [{"role": "assistant", "content": text}]
        return text


def end_face_for(message: str) -> str:
    words = ascii_text(message).lower()
    return "happy" if any(k in words for k in ("cam on", "thank", "chao", "hello")) else "idle"


def pages(text: str) -> list[str]:
    lines = textwrap.wrap(ascii_text(text), PAGE_COLS) or [""]
    return [" ".join(lines[i:i + PAGE_LINES]) for i in range(0, len(lines), PAGE_LINES)]


class Simulator:
    def __init__(self, path: Path) -> None:
        self.proc = subprocess.Popen(
            [str(path), "--stdin"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            text=True, bufsize=1,
        )

    def send(self, **settings) -> None:
        if self.proc.poll() is not None:
            raise BrokenPipeError
        for key, value in settings.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, float):
                value = f"{value:.3f}"
            self.proc.stdin.write(f"{key}={value}\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.terminate()


def play_turn(sim: Simulator, message: str, model: ChatModel | None) -> None:
    # Ask the model while the window listens and thinks; its latency shows as thinking.
    result: dict = {}
    if model:
        def ask() -> None:
            try:
                result["reply"] = model.reply(message)
            except RuntimeError as exc:
                result["error"] = str(exc)
        worker = threading.Thread(target=ask, daemon=True)
        worker.start()

    # Listening: a voice-like level and the recording's length.
    sim.send(face="listening", progress=0.0, caption="LISTENING...")
    listen_s = min(3.0, max(1.0, 0.6 + 0.05 * len(message)))
    started = time.monotonic()
    while (elapsed := time.monotonic() - started) < listen_s:
        level = max(0.0, min(1.0, 0.35 + 0.3 * math.sin(elapsed * 9) + random.uniform(-0.2, 0.2)))
        sim.send(level=level, progress=elapsed / 30, caption=f"LISTENING {elapsed:.1f}s")
        time.sleep(FRAME_S)
    sim.send(level=0.0)

    # Thinking: sending the note, then what Muse heard.
    sim.send(face="thinking", caption="SENDING VOICE NOTE")
    time.sleep(0.6)
    sim.send(caption=f'"{ascii_text(message)[:90]}"')
    time.sleep(1.2)
    if model:
        if worker.is_alive():
            sim.send(caption="NOTE SENT - WAITING FOR MUSE")
        while worker.is_alive():
            time.sleep(FRAME_S)
        if "error" in result:
            print(f"(model error: {result['error']})", file=sys.stderr)
            reply, end_face = "CAN'T REACH MUSE", "error"
        else:
            reply, end_face = result["reply"], end_face_for(message)
    else:
        reply, end_face = reply_for(message)

    if end_face == "error":
        sim.send(face="error", caption=reply)
        print(f"Muse: {reply}")
        time.sleep(2.5)
        sim.send(face="idle", caption="", progress=0.0)
        return

    # Speaking: one page at a time, the mouth following a syllable rhythm.
    print(f"Muse: {reply}")
    sim.send(face="speaking")
    chunks = pages(reply)
    durations = [max(1.4, 0.075 * len(p)) for p in chunks]
    total = sum(durations)
    spoken = 0.0
    for page, page_s in zip(chunks, durations):
        sim.send(caption=page)
        started = time.monotonic()
        while (elapsed := time.monotonic() - started) < page_s:
            syllable = abs(math.sin(elapsed * 11))
            sim.send(level=0.15 + 0.75 * syllable * random.uniform(0.6, 1.0),
                     progress=min(1.0, (spoken + elapsed) / total))
            time.sleep(FRAME_S)
        spoken += page_s
    sim.send(level=0.0, progress=0.0, face=end_face, caption="")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sim", type=Path, default=DEFAULT_SIM,
                        help=f"simulator binary (default: {DEFAULT_SIM})")
    parser.add_argument("--api-url", default=os.environ.get("MUSE_DEMO_API_URL"),
                        help="OpenAI-compatible base URL for replies, e.g. "
                             "http://127.0.0.1:8045/v1 (default: $MUSE_DEMO_API_URL, "
                             "or the built-in rules)")
    parser.add_argument("--model", default=os.environ.get("MUSE_DEMO_MODEL", DEFAULT_MODEL),
                        help=f"model name (default: $MUSE_DEMO_MODEL or {DEFAULT_MODEL})")
    args = parser.parse_args()
    model = None
    if args.api_url:
        model = ChatModel(args.api_url, args.model, os.environ.get("MUSE_DEMO_API_KEY", "local"))
    if not args.sim.exists():
        print(f"{args.sim} not found. Build the simulator first (see esp32/simulator/README.md).",
              file=sys.stderr)
        return 2

    sim = Simulator(args.sim)
    try:
        sim.send(wifi="connected", ble="connected", paired=True, link="online", face="boot")
        time.sleep(1.5)
        sim.send(face="idle")
        print("Muse simulator demo. Type a message and press Enter; 'exit' or Ctrl+D quits.")
        if model:
            print(f"Replies from {model.model} at {args.api_url}.")
        else:
            print("Try: 'xin chào', 'mấy giờ rồi', 'tình trạng máy', 'kể chuyện cười', "
                  "'what's the date'.")
        while True:
            try:
                message = input("You: ").strip()
            except EOFError:
                break
            if message.lower() in ("exit", "quit", "thoat", "thoát"):
                break
            if message:
                play_turn(sim, message, model)
    except BrokenPipeError:
        print("The simulator window closed.", file=sys.stderr)
    except KeyboardInterrupt:
        print()
    finally:
        sim.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
