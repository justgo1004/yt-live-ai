"""
이벤트봇 · 참여 유도봇 — 라이브 채팅(chat_reader)을 보고 정해 둔 규칙대로 메시지를 보낸다.

    이벤트봇 (event)
      · 키워드 추첨: 시작 → 채팅에 키워드('참여')를 친 시청자 모집 → 마감 시 랜덤 당첨자 발표
      · 퀴즈: 문제 출제 → 정답을 가장 먼저 친 시청자 발표 / 시간 초과 시 정답 공개
    참여 유도봇 (engage)
      · 첫 채팅 환영: 오늘 처음 채팅한 시청자를 모아 일정 간격으로 한 번에 환영 (도배 방지)
      · 조용할 때 유도: 채팅이 일정 시간 없으면 한 번만 참여 유도 멘트
      · 종료 시 순위: 방송을 끝낼 때 채팅 참여 TOP N 감사 메시지

스트리머(채널 주인)와 봇 계정의 채팅은 환영·추첨·순위에서 제외한다.
메시지 앞에는 보내는 봇의 표기(예: "🎁 ")가 붙으므로, 기본 문구는 이모지로 시작하지 않는다.
설정은 community.json (앱의 [🎁 이벤트] 화면에서 편집).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from commands import normalize

log = logging.getLogger("live_stt.community")
chatlog = logging.getLogger("live_stt.chat_in")      # 시청자 채팅 (화면 기록용)

YT_MAX = 190


@dataclass
class Quiz:
    question: str
    answers: list[str]


@dataclass
class CommunityConfig:
    # 추첨
    raffle_keyword: str = "참여"
    raffle_winners: int = 1
    raffle_minutes: float = 0                 # 0 = 직접 마감할 때까지
    raffle_start_msg: str = "추첨 이벤트 시작! 채팅에 '{keyword}' 라고 쳐 주세요{deadline}"
    raffle_result_msg: str = "당첨: {winners}! (참여 {count}명) 축하드려요 🎉"
    # 퀴즈
    quizzes: list[Quiz] = field(default_factory=lambda: [
        Quiz("우리 방송 이름은?", ["히든픽토이", "히든 픽토이"]),
        Quiz("1 + 1 은?", ["2", "둘"]),
    ])
    quiz_seconds: float = 60
    # 참여 유도
    welcome_enabled: bool = True
    welcome_interval: float = 60              # 이 간격마다 모아서 한 번에 환영
    welcome_max_names: int = 5
    welcome_msg: str = "{names}님 어서 오세요! 편하게 채팅 남겨 주세요 😊"
    quiet_enabled: bool = True
    quiet_seconds: float = 180                # 이 시간 동안 채팅이 없으면
    quiet_min_gap: float = 600                # 유도 멘트 사이 최소 간격
    quiet_messages: list[str] = field(default_factory=lambda: [
        "채팅창이 조용하네요~ 지금 방송 어떠세요? 한마디씩 남겨 주세요!",
        "오늘 처음 오신 분들도 편하게 인사 남겨 주세요 👋",
    ])
    ranking_enabled: bool = True
    ranking_top: int = 3
    ranking_msg: str = "오늘 채팅 참여 TOP{n} 🏆 {list} 함께해 주셔서 고마워요!"

    @classmethod
    def from_dict(cls, d: dict) -> "CommunityConfig":
        d = dict(d or {})
        has_quizzes = "quizzes" in d
        raw_quizzes = d.pop("quizzes", None) or []
        cfg = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        if has_quizzes:                       # 빈 목록도 그대로 (기본 퀴즈로 되돌리지 않음)
            cfg.quizzes = [Quiz(str(q.get("question", "")).strip(),
                                [str(a).strip() for a in q.get("answers", []) if str(a).strip()])
                           for q in raw_quizzes if str(q.get("question", "")).strip()]
        cfg.quiet_messages = [str(m).strip() for m in cfg.quiet_messages if str(m).strip()]
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, path: str | Path) -> "CommunityConfig":
        p = Path(path)
        if not p.exists():
            cfg = cls()
            cfg.save(p)
            return cfg
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        p = Path(path)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def validate(self) -> list[str]:
        e = []
        if not normalize(self.raffle_keyword):
            e.append("추첨 키워드를 입력하세요.")
        if not 1 <= self.raffle_winners <= 10:
            e.append("당첨자 수는 1~10명이어야 합니다.")
        if not 0 <= self.raffle_minutes <= 120:
            e.append("추첨 마감 시간은 0~120분이어야 합니다.")
        if not 10 <= self.quiz_seconds <= 600:
            e.append("퀴즈 제한 시간은 10~600초여야 합니다.")
        for q in self.quizzes:
            if not q.answers:
                e.append(f"퀴즈 '{q.question}' 의 정답을 입력하세요.")
        if not 30 <= self.welcome_interval <= 600:
            e.append("환영 메시지 간격은 30~600초여야 합니다.")
        if not 1 <= self.welcome_max_names <= 10:
            e.append("환영할 이름 수는 1~10명이어야 합니다.")
        if not 60 <= self.quiet_seconds <= 3600:
            e.append("조용함 기준은 60~3600초여야 합니다.")
        if self.quiet_min_gap < 300:
            e.append("참여 유도 멘트 사이 간격은 300초 이상이어야 합니다.")
        if not 1 <= self.ranking_top <= 10:
            e.append("순위 인원은 1~10명이어야 합니다.")
        for name in ("raffle_start_msg", "raffle_result_msg", "welcome_msg", "ranking_msg"):
            if not getattr(self, name).strip():
                e.append("비어 있는 안내 문구가 있습니다.")
                break
        return e


@dataclass
class _Raffle:
    keyword: str
    started: float
    deadline: Optional[float]
    entries: dict[str, str] = field(default_factory=dict)     # channel_id → 이름


@dataclass
class _QuizRun:
    quiz: Quiz
    started: float
    deadline: float


class CommunityHub:
    """
    send(role, text): 역할('event' | 'engage')의 봇으로 메시지 1건 보내기 (즉시 반환, 실제 전송은 호출한 쪽이 처리)
    """

    def __init__(self, cfg: CommunityConfig, send: Callable[[str, str], None], state_path: str | Path,
                 reading: bool = True):
        self.cfg = cfg
        self.send = send
        self.reading = reading                               # 채팅 읽기가 켜져 있는지 (꺼져 있으면 모집·환영 불가)
        self.state_path = Path(state_path)
        self.counts: dict[str, int] = {}                     # 이번 방송 채팅 수 (channel_id)
        self.names: dict[str, str] = {}
        self.messages = 0
        self.last_activity = time.monotonic()
        self.last_quiet_prompt = -1e9
        self.quiet_prompted = False
        self.pending_welcome: list[str] = []
        self.last_welcome = time.monotonic()
        self.raffle: Optional[_Raffle] = None
        self.quiz: Optional[_QuizRun] = None
        self.quiz_index = 0
        self.ranking_done = False
        self._task: Optional[asyncio.Task] = None
        self.seen_today = self._load_seen()

    # ---------- 오늘 채팅한 사람 (재시작해도 다시 환영하지 않도록 저장) ----------
    def _load_seen(self) -> set[str]:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            if d.get("day") == date.today().isoformat():
                return set(d.get("seen", []))
        except (OSError, ValueError):
            pass
        return set()

    def _save_seen(self) -> None:
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"day": date.today().isoformat(), "seen": sorted(self.seen_today)}),
                           encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    # ---------- 채팅 입력 ----------
    def on_messages(self, msgs) -> None:
        new_people = False
        for m in msgs:
            chatlog.info("💭 %s: %s", m.author, m.text, extra={"author": m.author, "owner": m.is_owner})
            self.last_activity = time.monotonic()
            self.quiet_prompted = False
            if m.is_owner:
                continue                                     # 스트리머 본인은 집계 제외
            self.messages += 1
            cid = m.channel_id or m.author
            self.counts[cid] = self.counts.get(cid, 0) + 1
            self.names[cid] = m.author
            if cid not in self.seen_today:
                self.seen_today.add(cid)
                new_people = True
                if self.cfg.welcome_enabled:
                    self.pending_welcome.append(m.author)
            n = normalize(m.text)
            if self.raffle and normalize(self.raffle.keyword) in n and cid not in self.raffle.entries:
                self.raffle.entries[cid] = m.author
            if self.quiz and any(normalize(a) and normalize(a) in n for a in self.quiz.quiz.answers):
                q, self.quiz = self.quiz, None
                self._say("event", f"정답! {m.author}님 🎉 정답은 '{q.quiz.answers[0]}' 였습니다")
        if new_people:
            self._save_seen()

    # ---------- 이벤트 동작 (음성 명령·버튼) ----------
    def raffle_start(self, keyword: Optional[str] = None, minutes: Optional[float] = None) -> str:
        if self.raffle:
            return f"이미 추첨 진행 중입니다 (참여 {len(self.raffle.entries)}명)."
        kw = (keyword or self.cfg.raffle_keyword).strip()
        mins = self.cfg.raffle_minutes if minutes is None else minutes
        now = time.monotonic()
        self.raffle = _Raffle(kw, now, now + mins * 60 if mins else None)
        deadline = f" ({mins:g}분 뒤 마감)" if mins else ""
        self._say("event", self.cfg.raffle_start_msg.format(keyword=kw, deadline=deadline))
        warn = "" if self.reading else " — 채팅 읽기가 꺼져 있어 참여자를 모을 수 없습니다!"
        return f"추첨 시작 (키워드 '{kw}'){warn}"

    def raffle_end(self) -> str:
        r, self.raffle = self.raffle, None
        if not r:
            return "진행 중인 추첨이 없습니다."
        if not r.entries:
            self._say("event", "참여자가 없어 이번 추첨은 취소합니다. 다음 기회에!")
            return "참여자가 없어 취소했습니다."
        k = min(self.cfg.raffle_winners, len(r.entries))
        winners = random.sample(list(r.entries.values()), k)
        self._say("event", self.cfg.raffle_result_msg.format(winners=", ".join(f"{w}님" for w in winners),
                                                              count=len(r.entries)))
        return f"당첨: {', '.join(winners)} (참여 {len(r.entries)}명)"

    def quiz_start(self) -> str:
        if self.quiz:
            return "이미 퀴즈가 진행 중입니다."
        if not self.cfg.quizzes:
            return "등록된 퀴즈가 없습니다. [이벤트] 화면에서 추가하세요."
        q = self.cfg.quizzes[self.quiz_index % len(self.cfg.quizzes)]
        self.quiz_index += 1
        now = time.monotonic()
        self.quiz = _QuizRun(q, now, now + self.cfg.quiz_seconds)
        self._say("event", f"퀴즈! {q.question} (정답을 채팅에 쳐 주세요, {self.cfg.quiz_seconds:g}초)")
        warn = "" if self.reading else " — 채팅 읽기가 꺼져 있어 정답을 확인할 수 없습니다!"
        return f"퀴즈 출제: {q.question}{warn}"

    def ranking(self, final: bool = False) -> str:
        top = sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))[: self.cfg.ranking_top]
        if not top:
            return "아직 채팅 참여자가 없습니다."
        lst = " / ".join(f"{i}. {self.names.get(cid, '?')}({n})" for i, (cid, n) in enumerate(top, 1))
        msg = self.cfg.ranking_msg.format(n=len(top), list=lst)
        if len(msg) > YT_MAX:
            msg = msg[: YT_MAX - 1] + "…"
        self._say("engage", msg)
        if final:
            self.ranking_done = True
        return f"순위 발표: {lst}"

    # ---------- 주기 작업 ----------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="community")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self.tick()
            except Exception as e:
                log.warning("이벤트 처리 오류 (%s: %s)", type(e).__name__, e)

    def tick(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if self.raffle and self.raffle.deadline and now >= self.raffle.deadline:
            log.info("🎯 [추첨] %s", self.raffle_end())
        if self.quiz and now >= self.quiz.deadline:
            q, self.quiz = self.quiz, None
            self._say("event", f"시간 종료! 정답은 '{q.quiz.answers[0]}' 였습니다")
        if self.pending_welcome and now - self.last_welcome >= self.cfg.welcome_interval:
            names = self.pending_welcome
            self.pending_welcome = []
            self.last_welcome = now
            shown = names[: self.cfg.welcome_max_names]
            more = f" 외 {len(names) - len(shown)}명" if len(names) > len(shown) else ""
            self._say("engage", self.cfg.welcome_msg.format(names=", ".join(shown) + more))
        if (self.cfg.quiet_enabled and self.reading and self.cfg.quiet_messages and not self.quiet_prompted
                and now - self.last_activity >= self.cfg.quiet_seconds
                and now - self.last_quiet_prompt >= self.cfg.quiet_min_gap):
            self.quiet_prompted = True
            self.last_quiet_prompt = now
            self._say("engage", random.choice(self.cfg.quiet_messages))

    def _say(self, role: str, text: str) -> None:
        self.send(role, text[:YT_MAX])

    def snapshot(self) -> dict:
        now = time.monotonic()
        top = sorted(self.counts.items(), key=lambda kv: -kv[1])[:5]
        return {
            "participants": len(self.counts), "messages": self.messages, "reading": self.reading,
            "quiet_for": int(now - self.last_activity),
            "raffle": {"keyword": self.raffle.keyword, "entries": len(self.raffle.entries),
                       "remaining": int(self.raffle.deadline - now) if self.raffle.deadline else None}
            if self.raffle else None,
            "quiz": {"question": self.quiz.quiz.question, "remaining": int(self.quiz.deadline - now)}
            if self.quiz else None,
            "top": [{"name": self.names.get(c, "?"), "count": n} for c, n in top],
        }

    async def aclose(self, announce: bool = True) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if announce and self.raffle:                       # 마감 안 한 추첨: 참여자가 있으면 발표, 없으면 조용히 취소
            if self.raffle.entries:
                log.info("🎯 [방송 종료 — 추첨 자동 마감] %s", self.raffle_end())
            else:
                self.raffle = None
        if announce and self.cfg.ranking_enabled and not self.ranking_done and self.counts:
            log.info("🎯 [방송 종료] %s", self.ranking(final=True))
        log.info("이벤트·참여 — 채팅 참여 %d명 | 메시지 %d개", len(self.counts), self.messages)
