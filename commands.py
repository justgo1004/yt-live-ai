#!/usr/bin/env python3
"""
음성 명령 — 스트리머가 "히든아, 메뉴판 올려줘" 라고 말하면 미리 써 둔 메뉴판을 채팅에 올린다.

    STT 텍스트 ─▶ CommandMatcher
                   · 호출어(예: 히든아) 탐지 — 음성 인식 오차를 고려해 자모 단위로 비슷한 발음도 허용
                   · 호출어 뒤(또는 8초 안의 다음 문장)에서 명령어(예: 메뉴판) 탐지
              ─▶ CommandRunner
                   · 명령별 재사용 대기(같은 명령이 연달아 두 번 나가지 않게)
                   · 200자 넘는 내용은 자동으로 나눠 1.5초 간격 연속 전송
                   · 화면 모드면 '보냈다면 이렇게 나갔을 메시지'만 표시

명령 목록은 commands.json 에 저장된다 (앱의 [🎤 음성 명령] 화면에서 편집).
단독 시험:  python commands.py "히든아 메뉴판 올려줘"
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

log = logging.getLogger("live_stt.command")

YT_MAX_LEN = 190             # 유튜브 한도 200자 — 이모지가 2글자로 세어질 수 있어 여유를 둠
PART_GAP_SECONDS = 1.5      # 나눠 보낼 때 메시지 사이 간격
ARM_SECONDS = 8.0           # 호출어만 들렸을 때 다음 말을 기다리는 시간 (방송 시간 기준)

ACTIONS = {
    "message": "내용 전송",
    "raffle_start": "추첨 시작",
    "raffle_end": "추첨 마감·발표",
    "quiz": "퀴즈 내기",
    "ranking": "채팅 순위 발표",
}
ACTION_COMMANDS = [
    {"name": "추첨 시작", "triggers": ["추첨 시작", "이벤트 시작"], "cooldown": 10, "action": "raffle_start"},
    {"name": "추첨 마감", "triggers": ["추첨 마감", "이벤트 마감", "당첨자 발표"], "cooldown": 10, "action": "raffle_end"},
    {"name": "퀴즈", "triggers": ["퀴즈", "퀴즈 내줘"], "cooldown": 10, "action": "quiz"},
    {"name": "순위", "triggers": ["채팅 순위", "순위 발표"], "cooldown": 60, "action": "ranking"},
]

DEFAULT_COMMANDS = {
    "version": 2,
    "enabled": True,
    "wake_words": ["히든아", "이든아", "히든야"],   # 자주 잘못 들리는 발음도 함께 등록
    "commands": [
        {"name": "메뉴판", "triggers": ["메뉴판", "메뉴"], "cooldown": 30,
         "message": "📋 오늘의 메뉴판\n1. 노래 신청 — 1,000원\n2. 게임 같이하기 — 5,000원\n3. 소원 들어주기 — 10,000원"},
        {"name": "공지", "triggers": ["공지", "공지사항"], "cooldown": 30,
         "message": "📢 방송 시간: 매주 월·수·금 저녁 8시 / 문의는 커뮤니티 탭으로!"},
        *[{**d, "message": ""} for d in ACTION_COMMANDS],
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# 한글 정규화 · 자모 분해 (음성 인식 오차에 강한 비교용)
# ─────────────────────────────────────────────────────────────────────────────
_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"


def normalize(text: str) -> str:
    """공백·문장부호 제거 + 소문자. '히든 아,' → '히든아'"""
    return re.sub(r"[^\w]", "", text).replace("_", "").lower()


def jamo(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out.append(_CHO[code // 588])
            out.append(_JUNG[(code % 588) // 28])
            if code % 28:
                out.append(_JONG[code % 28])
        else:
            out.append(ch)
    return "".join(out)


def _best_window(hay: str, needle: str) -> tuple[float, int]:
    """hay(정규화 문자열)에서 needle 과 가장 비슷한 같은 길이 구간 → (유사도, 끝 위치)."""
    if not needle or not hay:
        return 0.0, -1
    idx = hay.find(needle)
    if idx >= 0:
        return 1.0, idx + len(needle)
    best, end = 0.0, -1
    nj = jamo(needle)
    n = len(needle)
    for size in {n - 1, n, n + 1}:          # 한 글자 빠지거나 더 붙은 경우도 허용
        if size <= 0:
            continue
        for i in range(0, max(1, len(hay) - size + 1)):
            r = SequenceMatcher(None, jamo(hay[i:i + size]), nj).ratio()
            if r > best:
                best, end = r, i + size
    return best, end


# ─────────────────────────────────────────────────────────────────────────────
# 명령 정의 · 저장
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Command:
    name: str
    triggers: list[str]
    message: str
    cooldown: float = 30.0
    action: str = "message"        # message(내용 전송) | raffle_start | raffle_end | quiz | ranking

    def parts(self, label: str = "") -> list[str]:
        return split_message(self.message, label)


@dataclass
class CommandSet:
    enabled: bool = True
    wake_words: list[str] = field(default_factory=lambda: ["히든아"])
    commands: list[Command] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "CommandSet":
        cmds = []
        for c in d.get("commands") or []:
            name = str(c.get("name", "")).strip()
            triggers = [t.strip() for t in (c.get("triggers") or []) if str(t).strip()]
            msg = str(c.get("message", "")).strip()
            action = str(c.get("action") or "message")
            if action not in ACTIONS:
                action = "message"
            if name and (msg or action != "message"):
                cmds.append(Command(name, triggers or [name], msg, float(c.get("cooldown", 30) or 30), action))
        wakes = [w.strip() for w in (d.get("wake_words") or []) if str(w).strip()]
        cs = cls(bool(d.get("enabled", True)), wakes, cmds)
        cs.version = int(d.get("version", 1) or 1)
        return cs

    version: int = 2

    def to_dict(self) -> dict:
        return {"version": 2, "enabled": self.enabled, "wake_words": self.wake_words,
                "commands": [asdict(c) for c in self.commands]}

    @classmethod
    def load(cls, path: str | Path, create_default: bool = True) -> "CommandSet":
        p = Path(path)
        if not p.exists():
            if create_default:
                p.write_text(json.dumps(DEFAULT_COMMANDS, ensure_ascii=False, indent=2), encoding="utf-8")
                return cls.from_dict(DEFAULT_COMMANDS)
            return cls()
        cs = cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        if cs.version < 2:                      # 이전 버전 파일 → 이벤트·순위 명령을 한 번만 추가
            names = {c.name for c in cs.commands}
            for d in ACTION_COMMANDS:
                if d["name"] not in names:
                    cs.commands.append(Command(d["name"], d["triggers"], "", d["cooldown"], d["action"]))
            cs.version = 2
            cs.save(p)
        return cs

    def save(self, path: str | Path) -> None:
        p = Path(path)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def validate(self) -> list[str]:
        errs = []
        if self.enabled and not self.wake_words:
            errs.append("호출어를 하나 이상 입력하세요.")
        for w in self.wake_words:
            if len(normalize(w)) < 2:
                errs.append(f"호출어 '{w}' 가 너무 짧습니다 (2글자 이상).")
        seen = set()
        for c in self.commands:
            if c.name in seen:
                errs.append(f"명령 이름 '{c.name}' 이 중복되었습니다.")
            seen.add(c.name)
            if c.cooldown < 5:
                errs.append(f"'{c.name}' 의 재사용 대기는 5초 이상이어야 합니다.")
            if len(c.message) > 1000:
                errs.append(f"'{c.name}' 의 내용이 너무 깁니다 (1000자 이하).")
        return errs

    def vocab_hint(self) -> str:
        """음성 인식 모델에 미리 알려 줄 단어 (Whisper prompt) — 인식 정확도 향상."""
        words = list(dict.fromkeys(self.wake_words + [t for c in self.commands for t in c.triggers]))
        return ", ".join(words)[:80]


def split_message(message: str, label: str = "", max_len: int = YT_MAX_LEN) -> list[str]:
    """줄 단위로 최대한 묶어서 200자 이하 메시지 목록으로. 유튜브 채팅은 줄바꿈이 없어 ' / ' 로 잇는다."""
    room = max_len - len(label)
    lines = [ln.strip() for ln in message.splitlines() if ln.strip()]
    pieces: list[str] = []
    for ln in lines:                             # 한 줄이 너무 길면 단어 단위로 자름
        while len(ln) > room:
            cut = ln.rfind(" ", 0, room)
            cut = cut if cut > room // 2 else room
            pieces.append(ln[:cut].strip())
            ln = ln[cut:].strip()
        if ln:
            pieces.append(ln)
    out, cur = [], ""
    for p in pieces:
        cand = f"{cur} / {p}" if cur else p
        if len(cand) <= room:
            cur = cand
        else:
            out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return [label + m for m in out]


# ─────────────────────────────────────────────────────────────────────────────
# 매칭
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class MatchResult:
    kind: str                               # "command" | "wake" (호출어만 들림)
    command: Optional[Command] = None
    wake: str = ""
    score: float = 0.0


class CommandMatcher:
    WAKE_THRESHOLD = 0.88        # 자모 유사도. 히든아↔히든카(0.86)는 거부 — 비슷한 발음은 호출어 목록에 직접 추가
    TRIGGER_THRESHOLD = 0.8

    def __init__(self, cs: CommandSet, arm_seconds: float = ARM_SECONDS):
        self.cs = cs
        self.arm_seconds = arm_seconds
        self._armed_until = -1e9

    def _find_wake(self, n: str) -> tuple[Optional[str], int]:
        best_w, best_end, best_r = None, -1, 0.0
        for w in self.cs.wake_words:
            r, end = _best_window(n, normalize(w))
            if r > best_r:
                best_w, best_end, best_r = w, end, r
        return (best_w, best_end) if best_r >= self.WAKE_THRESHOLD else (None, -1)

    def _find_command(self, tail: str) -> tuple[Optional[Command], float]:
        best, best_r = None, 0.0
        for c in self.cs.commands:
            for t in c.triggers:
                nt = normalize(t)
                if len(nt) < 2:
                    continue
                r, _ = _best_window(tail, nt)
                # 같은 점수면 더 긴(구체적인) 명령어 우선: '공지사항' > '공지'
                if r > best_r or (r == best_r and best and len(nt) > max(len(normalize(x)) for x in best.triggers)):
                    best, best_r = c, r
        return (best, best_r) if best_r >= self.TRIGGER_THRESHOLD else (None, best_r)

    def feed(self, offset: float, text: str) -> Optional[MatchResult]:
        if not self.cs.enabled or not self.cs.commands:
            return None
        n = normalize(text)
        wake, end = self._find_wake(n)
        if wake is not None:
            tail = n[end:]
            self._armed_until = offset + self.arm_seconds
        elif offset <= self._armed_until:
            tail = n                                     # 직전에 호출어만 들렸음 → 이번 문장 전체가 명령
        else:
            return None
        cmd, score = self._find_command(tail)
        if cmd:
            self._armed_until = -1e9
            return MatchResult("command", cmd, wake or "(이어서)", score)
        return MatchResult("wake", None, wake or "", 0.0) if wake else None

    def test(self, text: str) -> dict:
        """앱의 '말해 보기' — 상태를 바꾸지 않고 결과만 설명."""
        n = normalize(text)
        wake, end = self._find_wake(n)
        if wake is None:
            return {"wake": None, "command": None,
                    "note": f"호출어({', '.join(self.cs.wake_words)})가 들리지 않았습니다."}
        cmd, score = self._find_command(n[end:])
        if not cmd:
            return {"wake": wake, "command": None,
                    "note": "호출어는 인식했지만 해당하는 명령어가 없습니다. 8초 안에 명령어를 말하면 실행됩니다."}
        return {"wake": wake, "command": cmd.name, "score": round(score, 2), "parts": cmd.parts()}


# ─────────────────────────────────────────────────────────────────────────────
# 실행 (전송)
# ─────────────────────────────────────────────────────────────────────────────
class CommandRunner:
    """
    매칭된 명령을 전송한다. publisher 가 None 이거나 화면 모드면 표시만.
    publisher: chat_sender.SafeChatPublisher (send_direct 사용 — 리액션용 45~90초 쿨다운과는 별개)
    """

    def __init__(self, cs: CommandSet, publisher=None, real: bool = False, label: str = "",
                 counter=None, max_per_day: int = 100, actions: Optional[dict] = None):
        self.cs = cs
        self.actions = actions or {}        # 동작 이름 → 실행 함수 (반환: 화면에 보여 줄 결과 문구)
        self.matcher = CommandMatcher(cs)
        self.publisher, self.real, self.label = publisher, real, label
        self.counter, self.max_per_day = counter, max_per_day
        self._last_fired: dict[str, float] = {}      # 명령 이름 → time.monotonic()
        self._tasks: set[asyncio.Task] = set()
        self.stats = {"heard": 0, "sent": 0, "skipped": 0}

    def on_utterance(self, offset: float, text: str) -> bool:
        """True 를 반환하면 이 발언은 명령으로 처리된 것 (AI 리액션 대상에서 제외)."""
        m = self.matcher.feed(offset, text)
        if m is None:
            return False
        if m.kind == "wake":
            log.info("🎤 호출어 '%s' 인식 — %.0f초 안에 명령어를 말하세요", m.wake, ARM_SECONDS)
            return True
        self.trigger(m.command.name, source=f"음성: \"{text}\"")
        return True

    def trigger(self, name: str, source: str = "버튼") -> str:
        cmd = next((c for c in self.cs.commands if c.name == name), None)
        if cmd is None:
            return "없는 명령입니다."
        self.stats["heard"] += 1
        now = time.monotonic()
        wait = cmd.cooldown - (now - self._last_fired.get(name, -1e9))
        if wait > 0:
            self.stats["skipped"] += 1
            log.warning("📢 [%s] 방금 보냈습니다 — %.0f초 뒤에 다시 보낼 수 있습니다", name, wait)
            return f"방금 보냈습니다. {wait:.0f}초 뒤에 다시 보낼 수 있습니다."
        if cmd.action != "message":
            fn = self.actions.get(cmd.action)
            if fn is None:
                log.warning("📢 [%s] '%s' 기능이 꺼져 있습니다 (이벤트 설정 확인)", name, ACTIONS.get(cmd.action))
                return f"'{ACTIONS.get(cmd.action)}' 기능을 쓸 수 없습니다."
            self._last_fired[name] = now
            result = fn()
            log.info("🎯 [%s] %s ← %s", name, result, source)
            return "ok"
        parts = cmd.parts(self.label)
        if self.real and self.counter is not None and self.counter.value() + len(parts) > self.max_per_day:
            self.stats["skipped"] += 1
            log.warning("📢 [%s] 오늘 명령 전송 한도(%d건)에 도달했습니다", name, self.max_per_day)
            return "오늘 명령 전송 한도에 도달했습니다."
        self._last_fired[name] = now
        t = asyncio.create_task(self._send(cmd, parts, source))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return "ok"

    async def _send(self, cmd: Command, parts: list[str], source: str) -> None:
        if not (self.real and self.publisher and self.publisher.enabled):
            for i, p in enumerate(parts, 1):
                log.info("📢 [%s] %d/%d (화면 모드 — 전송 안 함) ← %s :: %s", cmd.name, i, len(parts), source, p)
            return
        for i, p in enumerate(parts, 1):
            ok = await self.publisher.send_direct(p, labeled=False)   # 표기는 parts 에 이미 포함
            if not ok:
                log.error("📢 [%s] %d/%d 전송 실패 — 나머지는 보내지 않습니다", cmd.name, i, len(parts))
                return
            if self.counter is not None:
                self.counter.increment()
            log.info("📢 [%s] %d/%d 전송됨 ← %s :: %s", cmd.name, i, len(parts), source, p)
            if i < len(parts):
                await asyncio.sleep(PART_GAP_SECONDS)
        self.stats["sent"] += 1

    async def aclose(self, wait: float = 5.0) -> None:
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=wait)
            for t in self._tasks:
                t.cancel()
        log.info("음성 명령 — 인식 %d | 전송 %d | 건너뜀 %d",
                 self.stats["heard"], self.stats["sent"], self.stats["skipped"])


if __name__ == "__main__":
    cs = CommandSet.from_dict(DEFAULT_COMMANDS)
    m = CommandMatcher(cs)
    for s in (sys.argv[1:] or ["히든아 메뉴판 올려줘", "이든아, 메뉴 좀 보여줘", "히든 아 공지사항 올려 줘",
                               "히든카드 꺼낼게요", "오늘 메뉴는 뭐 먹지", "히든아"]):
        print(f"{s!r:30} → {m.test(s)}")
