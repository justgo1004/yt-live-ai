#!/usr/bin/env python3
"""
Phase 2 — 스트리머 발언 맥락 기반 '시청자 리액션' 생성기

    STT 텍스트 ─▶ ConversationMemory (최근 10개, deque 슬라이딩 윈도우)
              ─▶ TriggerDetector (질문 / 신남 / 당황 / 진지 / 후원 + 음량 급상승)
              ─▶ (트리거 + 쿨다운 통과 시에만) ReactionGenerator: Claude → 채팅 한 줄

main.py 에서 자동으로 연결되며 (REACTIONS=true), 단독으로 시험할 수도 있다:
    python reaction.py --demo              # 내장 샘플 대사로 트리거 + 실제 리액션 생성
    python reaction.py --demo --dry-run    # API 호출 없이 트리거 판정만 확인
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

log = logging.getLogger("live_stt.reaction.sys")   # 상태 로그 (live_stt 의 시스템 로그로 전파)
rlog = logging.getLogger("live_stt_reaction")      # 생성된 리액션 출력 전용

DEFAULT_MODEL = "claude-haiku-5-5"   # claude-3-5-haiku 는 2026-02 은퇴 → 현행 Haiku 사용


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# 1) 메모리 매니저: 최근 N개 발언 슬라이딩 윈도우
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Utterance:
    offset: float          # 방송 내 위치(초)
    text: str
    dbfs: float = -30.0    # 해당 청크 음량 (감정 고조 판단용)
    wall: datetime = field(default_factory=datetime.now)
    mono: float = field(default_factory=time.monotonic)   # 리액션 지연 계산용


class ConversationMemory:
    """deque(maxlen=N) 기반. 새 발언이 들어오면 가장 오래된 발언이 자동으로 밀려난다."""

    def __init__(self, size: int = 10):
        self._buf: deque[Utterance] = deque(maxlen=size)

    def add(self, u: Utterance) -> None:
        self._buf.append(u)

    def recent(self, n: Optional[int] = None) -> list[Utterance]:
        items = list(self._buf)
        return items if n is None else items[-n:]

    @property
    def latest(self) -> Optional[Utterance]:
        return self._buf[-1] if self._buf else None

    def as_transcript(self) -> str:
        return "\n".join(f"[{_hms(u.offset)}] {u.text}" for u in self._buf)

    def __len__(self) -> int:
        return len(self._buf)


# ─────────────────────────────────────────────────────────────────────────────
# 2) 트리거 감지: 규칙 기반 점수 + 쿨다운
# ─────────────────────────────────────────────────────────────────────────────
KIND_LABEL = {"question": "질문", "excited": "신남", "flustered": "당황", "serious": "진지", "event": "후원"}

_P = lambda s: re.compile(s, re.IGNORECASE)  # noqa: E731

# (패턴, 가중치, 근거 라벨)
RULES: dict[str, list[tuple[re.Pattern, float, str]]] = {
    "question": [
        (_P(r"(여러분|채팅창?|시청자|님들|구독자분?들?|댓글)"), 1.2, "시청자 호명"),
        (_P(r"(골라\s*주|투표|알려\s*주|말해\s*주|적어\s*주|추천\s*(해\s*)?주|어떻게\s*생각)"), 1.5, "의견 요청"),
        (_P(r"(까요|나요|래요|할래|을래|어때요?|뭐가\s*(좋|나)|맞죠|맞지|할까|볼까|갈까|먹을까|살까|인가요|있어요|없어요)"
            r"\s*[?？]"), 1.5, "의문형 어미"),
        (_P(r"[?？]"), 0.8, "물음표"),
        (_P(r"(뭐|어디|언제|누가|누구|왜|몇|어떤)\S*\s*[?？]"), 0.5, "의문사"),
    ],
    "excited": [
        (_P(r"(대박|미쳤|미친|레전드|개쩐|쩐다|지렸|가즈아|드디어|최고|역대급)"), 1.5, "감탄어"),
        (_P(r"(됐다|성공|이겼|클리어|해냈|잡았)"), 1.5, "성공"),
        (_P(r"(우와|와아+|오오+|와\s*[!~]|예스|yes)"), 1.0, "환호"),
        (_P(r"(ㅋㅋ|하하|크크|웃음|ㅎㅎ)"), 0.8, "웃음"),
        (_P(r"!.*!"), 0.5, "느낌표 반복"),
    ],
    "flustered": [
        (_P(r"(어어+|잠깐만?|헐|으악|악+[!~]|깜짝|어떡해|어떻게\s*해|망했|죽었|큰일|왜\s*이래|뭐야\s*이거)"), 1.5, "당황 표현"),
        (_P(r"(실수|잘못\s*눌|아니\s*잠|아\s*진짜|미안\s*미안)"), 1.2, "실수"),
    ],
    "serious": [
        (_P(r"(죄송|사과|슬프|슬퍼|힘들|눈물|울컥|걱정|고민|아팠|아파)"), 1.2, "감정 고백"),
        (_P(r"(진지하게|고백|사실은|솔직히|중요한\s*얘기|마지막\s*방송|공지)"), 1.3, "진지한 전환"),
    ],
    "event": [
        (_P(r"(후원|도네|슈퍼\s*챗|슈챗|멤버십|멤버쉽|선물|당첨|이벤트|구독\s*(해\s*주셔서|감사))"), 2.0, "후원/이벤트"),
        (_P(r"님\s*(\S+\s*)?(감사|고마)"), 1.0, "닉네임 감사"),
    ],
}


@dataclass
class Trigger:
    kind: str
    score: float
    reasons: list[str]

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)


class TriggerDetector:
    """
    발언 하나를 점수화해 '반응할 만한 순간'만 통과시킨다.
      - 카테고리별 규칙 점수 합 → 최고 점수 카테고리 선택
      - 음량 급상승(최근 평균 대비 +loud_jump_db 이상)은 신남/당황 점수에 가산 → 텍스트만으로 놓치는 감정 변화 포착
      - 쿨다운(방송 시간 기준): 일반 cooldown 초 / 강한 감정(임계값×1.5 이상)은 절반 / 질문은 question_cooldown 초
    """

    def __init__(self, threshold: float = 2.0, cooldown: float = 12.0, question_cooldown: float = 3.0,
                 loud_jump_db: float = 6.0):
        self.threshold = threshold
        self.cooldown = cooldown
        self.question_cooldown = question_cooldown
        self.loud_jump_db = loud_jump_db
        self._ema_db: Optional[float] = None
        self._last_fire: float = -1e9

    def score(self, u: Utterance) -> Optional[Trigger]:
        scores: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        for kind, rules in RULES.items():
            for pat, w, why in rules:
                hits = len(pat.findall(u.text))
                if hits:   # 같은 계열 표현이 여러 번 나오면 강도 가산 (최대 2배)
                    scores[kind] = scores.get(kind, 0.0) + w * min(hits, 2)
                    reasons.setdefault(kind, []).append(why if hits == 1 else f"{why}×{hits}")

        # 음량 기반 감정 고조 감지 (EMA 로 '평소 목소리 크기'를 추적)
        if self._ema_db is not None and u.dbfs - self._ema_db >= self.loud_jump_db:
            bump = f"음량 +{u.dbfs - self._ema_db:.0f}dB"
            target = "flustered" if scores.get("flustered", 0) > scores.get("excited", 0) else "excited"
            scores[target] = scores.get(target, 0.0) + 1.2
            reasons.setdefault(target, []).append(bump)
        if u.dbfs > -90:
            self._ema_db = u.dbfs if self._ema_db is None else 0.8 * self._ema_db + 0.2 * u.dbfs

        if not scores:
            return None
        kind = max(scores, key=lambda k: scores[k])
        return Trigger(kind, round(scores[kind], 2), reasons[kind])

    def check(self, u: Utterance) -> Optional[Trigger]:
        """임계값 + 쿨다운까지 통과한 경우에만 Trigger 반환."""
        t = self.score(u)
        if t is None or t.score < self.threshold:
            if t:
                log.debug("트리거 미달 %.1f<%.1f [%s] %s", t.score, self.threshold, t.label, u.text)
            return None
        if t.kind == "question":
            gap = self.question_cooldown          # 시청자에게 던진 질문은 거의 항상 답한다
        elif t.score >= self.threshold * 1.5:
            gap = self.cooldown / 2               # 강한 감정 변화는 쿨다운 절반만 적용
        else:
            gap = self.cooldown
        if u.offset - self._last_fire < gap:
            log.debug("쿨다운 중 (%.0fs < %.0fs) [%s]", u.offset - self._last_fire, gap, t.label)
            return None
        self._last_fire = u.offset
        return t


# ─────────────────────────────────────────────────────────────────────────────
# 3) Claude 리액션 생성
# ─────────────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
너는 지금 한국 유튜브 라이브 방송을 보고 있는 평범한 시청자다. 스트리머가 방금까지 한 말(음성 인식 결과)이 시간순으로 주어지면, 채팅창에 실제로 칠 법한 채팅 한 줄을 쓴다.

이 채팅은 방송 채팅창에 다른 시청자들 채팅과 섞여 올라간다. 그래서 채팅창 분위기에 맞는 짧고 가벼운 말투여야 하고, 정리된 문장이나 비서 같은 말투는 흐름을 깬다.

<tone>
- 1~2문장, 대부분 20자 안팎. 길어도 40자를 넘기지 않는다.
- 반말 위주의 가벼운 구어체. 친근한 "~요" 정도는 괜찮다.
- ㅋㅋㅋ, ㄷㄷ, ㅠㅠ, ㅇㅇ, ㄹㅇ, ㄱㄱ, ㅁㅊ 같은 초성이나 "미쳤다", "레전드", "실화냐" 같은 흔한 표현을 자연스럽게 섞는다. 같은 표현만 반복하지 말고, 초성 없이 말로만 쳐도 된다.
- 마침표는 거의 안 찍고, 띄어쓰기는 좀 대충이어도 된다.
- <persona> 에 스트리머 호칭이 주어지면 그 호칭만 쓴다. 주어지지 않았다면 성별이나 이름을 모르니 "형", "누나", "언니" 같은 호칭은 쓰지 않는다.
</tone>

<avoid>
아래는 사람이 친 채팅이 아니라 상담원·비서·기자처럼 보이게 만드는 말투라서 쓰지 않는다.
- "~하셨군요", "~하셨네요", "좋은 정보 감사합니다", "도움이 되었어요", "말씀하신 것처럼", "궁금하시군요", "응원하겠습니다!"
- 방송 내용 요약, 설명, 조언, 질문을 그대로 되풀이하기
- "~습니다"로 끝나는 격식체 문장
- 따옴표, 해시태그, 줄바꿈, "시청자:" 같은 접두어, 이모지 여러 개
- 음성 인식이 틀린 것 같은 단어는 지적하지 말고 문맥으로 알아듣는다
</avoid>

<by_mood>
감지된 분위기 힌트가 함께 주어진다. 힌트는 참고만 하고, 실제 대사가 다르면 대사를 따른다.
- 질문: 스트리머가 시청자에게 물은 것이다. 진짜로 대답한다. 선택지면 하나를 고르고, 의견을 물으면 짧게 자기 생각을 말한다. 예: "2번 ㄱㄱ", "ㅇㅇ 해줘요 ㅋㅋ", "전 매운맛이요"
- 신남: 같이 흥분한다. 예: "ㅋㅋㅋㅋ 미쳤다", "와 이걸 하네 ㄷㄷ"
- 당황: 가볍게 놀리거나 같이 당황한다. 예: "ㅋㅋㅋ 아 그걸 누르네", "헐 괜찮아요?"
- 진지: 장난을 빼고 짧게 공감한다. 예: "ㅠㅠ 고생 많았어요", "천천히 해도 돼요"
- 후원: 같이 축하하거나 부러워한다. 예: "ㅋㅋ 부자다", "오 축하해요"
</by_mood>

가장 마지막 발언에 반응하되 앞의 흐름과 이어지게 쓴다. 출력은 채팅 한 줄 그 자체만 쓴다."""

# 생성 결과에서 걸러낼 '비서 말투' (시스템 프롬프트를 어긴 경우의 안전망)
BANNED = re.compile(
    r"(하셨군요|하셨네요|좋은\s*정보|정보\s*감사|도움이\s*되|말씀하신|말씀하셨|궁금하시|궁금하신|"
    r"응원하겠습니다|습니다[.!]?\s*$|AI|어시스턴트|시청자로서)")


@dataclass
class Reaction:
    text: str
    trigger: Trigger
    source: Utterance
    latency_ms: float


class ReactionFatal(Exception):
    """복구 불가능한 설정 오류 (키 없음/권한 없음/모델 없음) → 리액션 기능 비활성화."""


def build_persona(streamer_name: str = "", channel_topic: str = "", viewer_style: str = "",
                  extra: str = "") -> str:
    """스트리머 맞춤 페르소나 블록 텍스트. 비어 있는 항목은 생략한다."""
    lines = []
    if streamer_name:
        lines.append(f"스트리머 호칭: {streamer_name} (채팅에서 부를 때 이 호칭을 쓴다)")
    if channel_topic:
        lines.append(f"방송 주제/분위기: {channel_topic}")
    if viewer_style:
        lines.append(f"내(시청자) 성격과 말투: {viewer_style}")
    if extra:
        lines.append(f"추가 지침: {extra}")
    return "\n".join(lines)


class ReactionGenerator:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "low", timeout: float = 10.0,
                 persona: str = "", api_key: Optional[str] = None):
        import anthropic  # 리액션 기능을 켤 때만 필요
        self._anthropic = anthropic
        # api_key 가 없으면 SDK 가 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login` 프로필 순으로 찾는다
        kwargs = {"api_key": api_key} if api_key else {}
        self.client = anthropic.AsyncAnthropic(timeout=timeout, max_retries=1, **kwargs)
        self.model, self.effort = model, effort
        # 페르소나는 방송 내내 고정 → 시스템 프롬프트 뒤에 한 번만 붙인다
        self.system = SYSTEM_PROMPT + (f"\n\n<persona>\n{persona.strip()}\n</persona>" if persona.strip() else "")

    @staticmethod
    def _build_prompt(memory: ConversationMemory, trigger: Trigger, my_recent: list[str],
                      retry_note: Optional[str]) -> str:
        latest = memory.latest
        parts = [
            "<recent_transcript>", memory.as_transcript(), "</recent_transcript>",
            f"<latest>[{_hms(latest.offset)}] {latest.text}</latest>" if latest else "",
            f"<mood_hint>{trigger.label} (근거: {', '.join(trigger.reasons)})</mood_hint>",
        ]
        if my_recent:
            parts += ["<my_previous_chats>", *my_recent, "</my_previous_chats>",
                      "위는 내가 이미 친 채팅이다. 같은 표현은 피한다."]
        if retry_note:
            parts.append(retry_note)
        parts.append("지금 칠 채팅 한 줄:")
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _clean(text: str) -> str:
        line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        line = re.sub(r"^(채팅|시청자|나|me|chat)\s*[:：]\s*", "", line, flags=re.IGNORECASE)
        line = line.strip("\"'“”‘’「」 ")
        return line[:80]

    async def _call(self, prompt: str) -> Optional[str]:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=1024,                          # 짧은 답이지만 thinking 여유분 포함
            output_config={"effort": self.effort},    # low: 지연 최소화 (단순한 요청은 thinking 생략)
            system=self.system,
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            log.debug("리액션 생성 거절됨 (stop_details=%s)", getattr(resp, "stop_details", None))
            return None
        text = "".join(b.text for b in resp.content if b.type == "text")
        return self._clean(text) or None

    async def generate(self, memory: ConversationMemory, trigger: Trigger,
                       my_recent: list[str]) -> Optional[str]:
        """
        한 줄 리액션 반환. 실패하면 None (STT 파이프라인을 멈추지 않도록 예외를 밖으로 내지 않음).
        키/권한/모델 오류는 복구 불가이므로 ReactionFatal 로 던져 엔진이 기능을 끄게 한다.
        """
        a = self._anthropic
        retry_note = None
        for attempt in range(2):
            try:
                line = await self._call(self._build_prompt(memory, trigger, my_recent, retry_note))
            except (a.AuthenticationError, a.PermissionDeniedError) as e:
                raise ReactionFatal(f"인증/권한 오류 ({type(e).__name__}) — ANTHROPIC_API_KEY 확인") from e
            except a.NotFoundError as e:
                raise ReactionFatal(f"모델 '{self.model}' 을(를) 찾을 수 없음 — REACTION_MODEL 확인") from e
            except a.RateLimitError:
                log.warning("Claude 요청 한도 초과 — 이번 리액션은 건너뜀")
                return None
            except a.APIStatusError as e:
                log.warning("Claude API 오류 %s — 리액션 건너뜀", e.status_code)
                return None
            except a.APIConnectionError as e:   # 타임아웃 포함
                log.warning("Claude 연결 실패 (%s) — 리액션 건너뜀", type(e).__name__)
                return None
            except a.AnthropicError as e:       # 자격 증명 미설정 등 SDK 자체 오류
                raise ReactionFatal(f"{type(e).__name__}: {e}") from e
            if line is None:                    # 거절(refusal) 또는 빈 응답 → 재시도하지 않음
                return None
            if not BANNED.search(line):
                return line
            log.debug("비서 말투/빈 응답 필터: %r (시도 %d)", line, attempt + 1)
            retry_note = f'방금 쓴 "{line}" 는 너무 딱딱하다. 실제 채팅처럼 더 짧고 가볍게 다시.'
        return None

    async def aclose(self) -> None:
        await self.client.close()


# ─────────────────────────────────────────────────────────────────────────────
# 4) 엔진: 메모리 + 트리거 + 생성기를 묶고, STT 출력 흐름을 막지 않도록 백그라운드로 실행
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ReactionConfig:
    model: str = field(default_factory=lambda: _env("REACTION_MODEL", DEFAULT_MODEL))
    effort: str = field(default_factory=lambda: _env("REACTION_EFFORT", "low"))
    timeout: float = field(default_factory=lambda: float(_env("REACTION_TIMEOUT", "10")))
    memory_size: int = field(default_factory=lambda: int(_env("REACTION_MEMORY_SIZE", "10")))
    threshold: float = field(default_factory=lambda: float(_env("REACTION_THRESHOLD", "2.0")))
    cooldown: float = field(default_factory=lambda: float(_env("REACTION_COOLDOWN", "12")))
    question_cooldown: float = field(default_factory=lambda: float(_env("REACTION_QUESTION_COOLDOWN", "3")))
    max_age: float = field(default_factory=lambda: float(_env("REACTION_MAX_AGE", "8")))
    persona: str = ""                   # build_persona() 결과 (스트리머 맞춤 설정)
    api_key: Optional[str] = None       # None 이면 SDK 기본 자격 증명 탐색
    dry_run: bool = False


class ReactionEngine:
    def __init__(self, cfg: Optional[ReactionConfig] = None,
                 on_reaction: Optional[Callable[[str], object]] = None):
        self.cfg = cfg or ReactionConfig()
        self.on_reaction = on_reaction      # 생성된 리액션을 받을 콜백 (예: SafeChatPublisher.offer)
        self.memory = ConversationMemory(self.cfg.memory_size)
        self.detector = TriggerDetector(self.cfg.threshold, self.cfg.cooldown, self.cfg.question_cooldown)
        self.generator: Optional[ReactionGenerator] = None
        if not self.cfg.dry_run:
            self.generator = ReactionGenerator(self.cfg.model, self.cfg.effort, self.cfg.timeout,
                                               self.cfg.persona, self.cfg.api_key)
        self._my_recent: deque[str] = deque(maxlen=5)
        self._inflight: Optional[asyncio.Task] = None
        self.enabled = True
        self.stats = {"utterances": 0, "triggers": 0, "busy_skips": 0, "reactions": 0, "failed": 0}

    def on_utterance(self, offset: float, text: str, dbfs: float = -30.0) -> Optional[Trigger]:
        """STT 결과 1건 입력. 즉시 반환하며, 생성은 백그라운드 태스크로 진행된다."""
        u = Utterance(offset, text, dbfs)
        self.memory.add(u)
        self.stats["utterances"] += 1
        if not self.enabled:
            return None
        trig = self.detector.check(u)
        if trig is None:
            return None
        self.stats["triggers"] += 1
        log.debug("트리거 [%s %.1f] %s ← %s", trig.label, trig.score, ", ".join(trig.reasons), text)
        if self._inflight and not self._inflight.done():
            self.stats["busy_skips"] += 1      # 이전 리액션 생성 중 → 겹치지 않게 건너뜀
            return trig
        if self.generator is None:            # dry-run
            _emit(f"(dry-run) {trig.label} 트리거", trig, u, 0.0)
            return trig
        self._inflight = asyncio.create_task(self._run(trig, u))
        return trig

    async def _run(self, trig: Trigger, u: Utterance) -> None:
        assert self.generator
        t0 = time.perf_counter()
        try:
            line = await self.generator.generate(self.memory, trig, list(self._my_recent))
        except ReactionFatal as e:
            self.enabled = False
            log.error("리액션 기능을 끕니다: %s", e)
            return
        except Exception as e:               # 예기치 못한 오류: 이번 건만 버리고 계속
            self.stats["failed"] += 1
            log.warning("리액션 생성 중 오류 (%s: %s) — 건너뜀", type(e).__name__, e)
            return
        if line is None:
            self.stats["failed"] += 1
            return
        age = time.monotonic() - u.mono
        if age > self.cfg.max_age:            # 너무 늦게 나온 리액션은 맥락이 지나가 어색하므로 버림
            log.debug("리액션 지연 %.1fs > %.0fs → 폐기: %s", age, self.cfg.max_age, line)
            self.stats["failed"] += 1
            return
        self._my_recent.append(line)
        self.stats["reactions"] += 1
        _emit(line, trig, u, (time.perf_counter() - t0) * 1000)
        if self.on_reaction:
            self.on_reaction(line)

    async def aclose(self, wait: float = 5.0) -> None:
        if self._inflight and not self._inflight.done():
            try:
                await asyncio.wait_for(self._inflight, timeout=wait)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self.generator:
            await self.generator.aclose()
        s = self.stats
        log.info("리액션 — 발언 %d | 트리거 %d | 생성 %d | 건너뜀(생성중) %d | 실패/폐기 %d",
                 s["utterances"], s["triggers"], s["reactions"], s["busy_skips"], s["failed"])


# ─────────────────────────────────────────────────────────────────────────────
# 출력 포맷
# ─────────────────────────────────────────────────────────────────────────────
class ReactionFormatter(logging.Formatter):
    """  14:03:15 │ 💬 질문 ↳00:01:24 │  820ms │ 2번 ㄱㄱ  """

    def __init__(self, color: bool, plain_file: bool = False):
        super().__init__()
        self.color, self.plain_file = color, plain_file

    def format(self, r: logging.LogRecord) -> str:
        wall = datetime.fromtimestamp(r.created)
        label, src = getattr(r, "label", "?"), _hms(getattr(r, "src_offset", 0.0))
        text = r.getMessage()
        if self.plain_file:
            return f"[{wall:%Y-%m-%d %H:%M:%S}] [{src}] 💬({label}) {text}"
        meta = f"{getattr(r, 'latency_ms', 0):>5.0f}ms"
        if self.color:
            return (f"\033[2m{wall:%H:%M:%S}\033[0m │ \033[33m💬 {label} ↳{src}\033[0m │ "
                    f"\033[2m{meta}\033[0m │ \033[1;33m{text}\033[0m")
        return f"{wall:%H:%M:%S} │ 💬 {label} ↳{src} │ {meta} │ {text}"


def setup_reaction_logging(output: Optional[str] = None) -> None:
    if rlog.handlers:
        return
    color = not os.environ.get("NO_COLOR") and sys.stdout.isatty()
    rlog.propagate = False
    rlog.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(ReactionFormatter(color))
    rlog.addHandler(h)
    if output:
        fh = logging.FileHandler(output, encoding="utf-8")
        fh.setFormatter(ReactionFormatter(False, plain_file=True))
        rlog.addHandler(fh)


def _emit(text: str, trig: Trigger, u: Utterance, latency_ms: float) -> None:
    rlog.info(text, extra={"label": trig.label, "src_offset": u.offset, "latency_ms": latency_ms})


# ─────────────────────────────────────────────────────────────────────────────
# 단독 데모
# ─────────────────────────────────────────────────────────────────────────────
DEMO_SCRIPT: list[tuple[float, str, float]] = [
    (0, "자 안녕하세요 오늘도 방송 시작하겠습니다", -28),
    (4, "오늘은 지난번에 못 깬 보스 다시 가 볼 건데요", -29),
    (8, "아 그 전에 장비부터 좀 정리하고", -30),
    (12, "여러분 무기 뭐 들고 갈까요? 대검이랑 쌍검 중에", -27),
    (16, "음 일단 들어가 볼게요", -30),
    (20, "어어 잠깐만 이거 왜 이래 어떡해", -20),
    (24, "아 죽었다 아 진짜", -24),
    (28, "다시 가 봅니다 이번엔 진짜 간다", -28),
    (36, "됐다 됐다 와 미쳤다 드디어 깼어요!!", -16),
    (40, "와 이거 세 시간 걸렸어요", -27),
    (48, "아 그리고 철수님 후원 감사합니다", -28),
    (60, "솔직히 요즘 좀 힘들었는데 여러분 덕분에 버텼어요", -32),
    (64, "다음 방송 때 뭐 하면 좋을지 채팅으로 알려 주세요", -29),
]


async def _demo(dry_run: bool) -> None:
    cfg = ReactionConfig(dry_run=dry_run)
    engine = ReactionEngine(cfg)
    print(f"모델: {cfg.model} | effort: {cfg.effort} | 임계값: {cfg.threshold} | 메모리: {cfg.memory_size}개\n")
    try:
        prev = 0.0
        for offset, text, db in DEMO_SCRIPT:
            await asyncio.sleep(min(1.5, (offset - prev) / 4))   # 4배속 재생
            prev = offset
            print(f"\033[2m[{_hms(offset)}] 🎙 {text}\033[0m" if sys.stdout.isatty() else f"[{_hms(offset)}] 🎙 {text}")
            engine.on_utterance(offset, text, db)   # 트리거 미달/쿨다운 사유는 -v 로 확인
    finally:
        await engine.aclose(wait=15)


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    p = argparse.ArgumentParser(description="시청자 리액션 생성기 단독 테스트")
    p.add_argument("--demo", action="store_true", help="내장 샘플 대사로 실행")
    p.add_argument("--dry-run", action="store_true", help="API 호출 없이 트리거만 확인")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    if not a.demo:
        p.error("현재는 --demo 만 지원합니다. 실방송은 `python main.py URL` 을 사용하세요.")
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    setup_reaction_logging()
    asyncio.run(_demo(a.dry_run))


if __name__ == "__main__":
    main()
