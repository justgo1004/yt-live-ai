#!/usr/bin/env python3
"""
YouTube Live AI 리액션 — 메인 오케스트레이터

    [Phase 1] live_stt.py     yt-dlp + ffmpeg → 3~5초 청크 → Groq/Deepgram STT
         │ on_transcript(offset, text, dbfs)   (동기 콜백, 즉시 반환)
         ▼
    [Phase 2] reaction.py     최근 10개 발언 버퍼 → 트리거 판정 → Claude 리액션 (백그라운드 태스크)
         │ on_reaction(text)
         ▼
    [Phase 3] chat_sender.py  쿨다운·횟수 제한·AI 표기 → console(dry-run) / YouTube Live Chat API

사용 예:
    python main.py "https://www.youtube.com/watch?v=XXXXXXXXXXX"
    python main.py URL --chat youtube-api -o log.txt
    python main.py --input sample.mp3 --chat console        # 로컬 파일로 전체 흐름 시험
    python main.py --check                                   # 설정/의존성 점검만

종료: Ctrl+C 1번 → 정상 종료(진행 중인 리액션/전송 마무리, ffmpeg 종료) / 2번 → 즉시 강제 종료
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

import live_stt
from live_stt import Config as SttConfig, run_pipeline, setup_logging

log = logging.getLogger("live_stt.main")

EXIT_OK, EXIT_ERROR, EXIT_INTERRUPTED = 0, 1, 130


# ─────────────────────────────────────────────────────────────────────────────
# 설정 (.env → 환경 변수 → 기본값 순으로 로드, 타입·범위 검증)
# ─────────────────────────────────────────────────────────────────────────────
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore", case_sensitive=False)

    # --- API 키 ---
    groq_api_key: Optional[SecretStr] = None
    deepgram_api_key: Optional[SecretStr] = None
    anthropic_api_key: Optional[SecretStr] = None

    # --- STT ---
    stt_provider: Literal["groq", "deepgram"] = "groq"
    stt_language: str = "ko"
    groq_model: str = "whisper-large-v3"
    deepgram_model: str = "nova-2"
    chunk_seconds: float = Field(4.0, ge=3.0, le=5.0)
    stt_concurrency: int = Field(2, ge=1, le=8)
    stt_timeout: float = Field(20.0, gt=0)
    stt_cooldown: float = Field(30.0, ge=0)
    max_lag_seconds: float = Field(15.0, gt=0)
    silence_dbfs: float = -50.0
    ytdlp_format: str = "bestaudio/93/best"
    ytdlp_cookies: str = ""

    # --- 리액션 (Phase 2) ---
    reactions: bool = True
    reaction_model: str = "claude-haiku-5-5"
    reaction_effort: Literal["low", "medium", "high"] = "low"
    reaction_timeout: float = Field(10.0, gt=0)
    reaction_memory_size: int = Field(10, ge=1, le=50)
    reaction_threshold: float = Field(2.0, gt=0)
    reaction_cooldown: float = Field(12.0, ge=0)
    reaction_question_cooldown: float = Field(3.0, ge=0)
    reaction_max_age: float = Field(8.0, gt=0)

    # --- 스트리머 맞춤 페르소나 ---
    persona_streamer_name: str = ""     # 예: "철수님" — 비우면 호칭을 쓰지 않음
    persona_channel_topic: str = ""     # 예: "소울라이크 게임 공략, 차분하고 진지한 분위기"
    persona_viewer_style: str = ""      # 예: "게임 고인물, 드립 좋아함"
    persona_extra: str = ""             # 예: "스포일러 금지, 다른 스트리머 언급 금지"

    # --- 채팅 전송 (Phase 3) ---
    chat_sender: Literal["off", "console", "youtube-api"] = "console"
    chat_label: str = Field("🤖AI: ", min_length=1)
    chat_cooldown_min: float = Field(45.0, ge=30.0)
    chat_cooldown_max: float = Field(90.0, ge=30.0)
    chat_max_per_stream: int = Field(20, ge=1, le=200)
    chat_max_per_day: int = Field(60, ge=1, le=200)
    chat_state_file: str = ".chat_state.json"
    chat_video_id: str = ""
    yt_client_secret: str = "client_secret.json"
    yt_token_file: str = "token.json"

    # --- 음성 명령 ---
    commands_enabled: bool = True
    commands_file: str = "commands.json"
    command_label: str = ""             # 명령 메시지 앞 표기 (선택, 예: "📢 ")
    command_max_per_day: int = Field(100, ge=1, le=150)

    @field_validator("groq_api_key", "deepgram_api_key", "anthropic_api_key", mode="after")
    @classmethod
    def _no_comment_as_key(cls, v: Optional[SecretStr]) -> Optional[SecretStr]:
        if v and v.get_secret_value().lstrip().startswith("#"):
            raise ValueError("값 자리에 주석(#...)이 읽혔습니다. 빈 값 뒤에 주석을 달지 말고 주석은 윗줄에 쓰세요")
        return v

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if not self.chat_label.strip():
            raise ValueError("CHAT_LABEL 은 비워 둘 수 없습니다 (자동 메시지에는 AI 표기가 필요합니다)")
        if self.chat_cooldown_min > self.chat_cooldown_max:
            raise ValueError("CHAT_COOLDOWN_MIN 이 CHAT_COOLDOWN_MAX 보다 큽니다")
        return self

    def has_stt_key(self) -> bool:
        return bool(self.secret(self.groq_api_key) or self.secret(self.deepgram_api_key))

    @staticmethod
    def secret(v: Optional[SecretStr]) -> str:
        return v.get_secret_value().strip() if v else ""


def load_settings(env_file: str) -> Settings:
    if env_file != ".env" and not Path(env_file).exists():
        raise SystemExit(f"설정 파일을 찾을 수 없습니다: {env_file}")
    try:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as e:
        lines = [f"  - {'.'.join(map(str, err['loc'])) or '설정'}: {err['msg']}" for err in e.errors()]
        raise SystemExit("설정 오류 (.env 확인):\n" + "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# 오케스트레이터
# ─────────────────────────────────────────────────────────────────────────────
class Orchestrator:
    """세 모듈을 하나의 이벤트 루프에서 실행하고, 종료 시 역순으로 정리한다."""

    SHUTDOWN_TIMEOUT = 15.0

    def __init__(self, s: Settings, args: argparse.Namespace, install_signals: bool = True):
        self.s, self.args = s, args
        self.install_signals = install_signals   # GUI 등 다른 곳에서 종료를 제어할 때는 False
        self.phase = "starting"   # starting → running → stopping → done (UI 표시용)
        self.stop = asyncio.Event()
        self.reason = ""
        self.signals = 0
        self.force = False        # 두 번째 Ctrl+C → 정리 단계 타임아웃을 0.5초로 단축
        self.engine = None        # reaction.ReactionEngine
        self.publisher = None     # chat_sender.SafeChatPublisher
        self.commands = None      # commands.CommandRunner
        self.vocab_hint = ""
        self.pipeline: Optional[asyncio.Task] = None
        self._run_task: Optional[asyncio.Task] = None
        self._t0 = time.monotonic()

    # ---------- 설정 → 각 모듈 Config ----------
    def _stt_config(self) -> SttConfig:
        s, a = self.s, self.args
        cfg = SttConfig(
            url=a.url, input=a.input, provider=s.stt_provider,
            language=None if s.stt_language.lower() in ("", "auto") else s.stt_language,
            chunk_seconds=s.chunk_seconds, concurrency=s.stt_concurrency, timeout=s.stt_timeout,
            cooldown=s.stt_cooldown, max_lag=s.max_lag_seconds, silence_dbfs=s.silence_dbfs,
            allow_vod=a.allow_vod, output=a.output, verbose=a.verbose,
        )
        cfg.groq_key, cfg.deepgram_key = s.secret(s.groq_api_key), s.secret(s.deepgram_api_key)
        cfg.groq_model, cfg.deepgram_model = s.groq_model, s.deepgram_model
        cfg.ytdlp_format, cfg.ytdlp_cookies = s.ytdlp_format, s.ytdlp_cookies
        cfg.vocab_hint = self.vocab_hint
        return cfg

    async def _start_chat(self) -> None:
        """채팅 전송기 (리액션·음성 명령이 함께 사용). 실패하면 전송만 끄고 나머지는 계속."""
        s = self.s
        if s.chat_sender == "off":
            return
        from chat_sender import ChatPolicy, ConsoleSender, SafeChatPublisher, YouTubeApiSender
        policy = ChatPolicy(cooldown_min=s.chat_cooldown_min, cooldown_max=s.chat_cooldown_max,
                            max_per_stream=s.chat_max_per_stream, max_per_day=s.chat_max_per_day,
                            label=s.chat_label, state_file=s.chat_state_file)
        if s.chat_sender == "console":
            sender = ConsoleSender()
        else:
            video = s.chat_video_id or self.args.url
            if not video:
                raise SystemExit("youtube-api 전송에는 유튜브 URL 또는 CHAT_VIDEO_ID 가 필요합니다")
            sender = YouTubeApiSender(video, s.yt_client_secret, s.yt_token_file)
        self.publisher = SafeChatPublisher(sender, policy)
        if not await self.publisher.start():
            self.publisher = None

    async def _start_reactions(self) -> None:
        from reaction import ReactionConfig, ReactionEngine, build_persona, setup_reaction_logging
        s = self.s
        persona = build_persona(s.persona_streamer_name, s.persona_channel_topic,
                                s.persona_viewer_style, s.persona_extra)
        rcfg = ReactionConfig(
            model=s.reaction_model, effort=s.reaction_effort, timeout=s.reaction_timeout,
            memory_size=s.reaction_memory_size, threshold=s.reaction_threshold,
            cooldown=s.reaction_cooldown, question_cooldown=s.reaction_question_cooldown,
            max_age=s.reaction_max_age, persona=persona, api_key=s.secret(s.anthropic_api_key) or None,
        )
        setup_reaction_logging(self.args.output)
        self.engine = ReactionEngine(rcfg)
        if self.publisher:
            self.engine.on_reaction = self.publisher.offer
        log.info("리액션 켜짐 — %s / effort=%s / 페르소나: %s", rcfg.model, rcfg.effort,
                 persona.replace("\n", " | ") if persona else "(기본)")

    def _start_commands(self) -> None:
        from commands import CommandRunner, CommandSet
        from chat_sender import DailyCounter
        s = self.s
        cs = CommandSet.load(s.commands_file)
        errs = cs.validate()
        if not cs.enabled or not cs.commands or errs:
            if errs:
                log.warning("음성 명령 설정 오류로 끕니다: %s", " / ".join(errs))
            return
        real = bool(self.publisher and s.chat_sender == "youtube-api")
        self.commands = CommandRunner(
            cs, self.publisher, real=real, label=s.command_label,
            counter=DailyCounter(s.chat_state_file + ".commands"), max_per_day=s.command_max_per_day)
        self.vocab_hint = cs.vocab_hint()
        log.info("음성 명령 켜짐 — 호출어: %s | 명령 %d개 (%s)%s", ", ".join(cs.wake_words), len(cs.commands),
                 ", ".join(c.name for c in cs.commands), "" if real else " | 화면 모드: 전송하지 않음")

    # ---------- 콜백 ----------
    def _on_transcript(self, offset: float, text: str, dbfs: float) -> None:
        if self.stop.is_set():
            return
        handled = self.commands.on_utterance(offset, text) if self.commands else False
        if self.engine and not handled:        # 명령 문장("히든아 메뉴판…")에는 AI 리액션을 붙이지 않음
            self.engine.on_utterance(offset, text, dbfs)

    async def _pipeline(self) -> None:
        try:
            await run_pipeline(self._stt_config(), self._on_transcript)
        except SystemExit as e:   # build_stt 등의 SystemExit 가 이벤트 루프 밖으로 새지 않게 변환
            raise RuntimeError(str(e)) from None

    # ---------- 시그널 ----------
    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError):   # Windows: loop 시그널 핸들러 미지원
                signal.signal(sig, lambda n, f: loop.call_soon_threadsafe(self._on_signal, n))

    def request_stop(self) -> None:
        """외부(UI)에서 종료 요청. 시그널과 같은 규칙: 1번째 정상 종료, 2번째 강제."""
        self._on_signal(signal.SIGINT)

    def _on_signal(self, sig) -> None:
        self.signals += 1
        name = signal.Signals(sig).name if isinstance(sig, int) else str(sig)
        if self.signals == 1:
            print(f"\n{name} 수신 — 정리 중입니다… (한 번 더 누르면 강제 종료)", file=sys.stderr)
            self.reason = "사용자 중단"
            self.stop.set()
        elif self.signals == 2:
            print("강제 종료 중… 남은 작업을 즉시 취소합니다. (한 번 더 누르면 즉시 프로세스 종료)",
                  file=sys.stderr)
            self.force = True
            if self.install_signals:            # 단독 실행: 루프 전체가 이 프로그램 소유
                targets = asyncio.all_tasks()
            else:                               # GUI 등에 내장: 내 작업만 취소 (화면 서버는 유지)
                targets = {t for t in (self._run_task, self.pipeline) if t}
            for t in targets:                   # 진행 중인 정리 단계(현재 await)도 즉시 깨운다
                t.cancel()
        else:
            os._exit(EXIT_INTERRUPTED)

    # ---------- 실행 ----------
    async def run(self) -> int:
        self._run_task = asyncio.current_task()
        if self.install_signals:
            self._install_signal_handlers()
        code = EXIT_OK
        try:
            try:
                await self._start_chat()
                if self.s.reactions:
                    await self._start_reactions()
                if self.s.commands_enabled:
                    self._start_commands()
            except ImportError as e:
                log.error("추가 모듈을 불러올 수 없습니다 (%s) — 자막만 실행합니다", e)
            except (OSError, ValueError) as e:
                log.error("음성 명령/채팅 설정을 읽지 못했습니다 (%s)", e)
            if self.stop.is_set():          # 초기화(OAuth 등) 도중 Ctrl+C
                return EXIT_INTERRUPTED
            self.pipeline = asyncio.create_task(self._pipeline(), name="stt-pipeline")
            self.phase = "running"
            stopper = asyncio.create_task(self.stop.wait(), name="stop-wait")
            done, _ = await asyncio.wait({self.pipeline, stopper}, return_when=asyncio.FIRST_COMPLETED)
            stopper.cancel()
            if self.pipeline in done:
                exc = self.pipeline.exception() if not self.pipeline.cancelled() else None
                if exc:
                    log.error("파이프라인 오류: %s: %s", type(exc).__name__, exc)
                    self.reason, code = "오류", EXIT_ERROR
                else:
                    self.reason = self.reason or "스트림 종료"
            else:
                code = EXIT_INTERRUPTED
        except asyncio.CancelledError:      # 두 번째 Ctrl+C 로 run() 자체가 취소된 경우
            self.reason, code = "강제 종료", EXIT_INTERRUPTED
        finally:
            await self._shutdown()
        return code

    async def _step(self, name: str, coro, timeout: float) -> None:
        """정리 단계 하나를 실행. 실패·지연해도 다음 단계는 반드시 진행한다."""
        if self.force:
            timeout = min(timeout, 0.5)
        try:
            await asyncio.wait_for(coro, timeout=timeout)
            log.info("  ✔ %s", name)
        except asyncio.TimeoutError:
            log.warning("  ✘ %s — %.0fs 초과, 건너뜀", name, timeout)
        except asyncio.CancelledError:
            log.warning("  ✘ %s — 강제 취소됨", name)
        except Exception as e:
            log.warning("  ✘ %s — %s: %s", name, type(e).__name__, e)

    async def _shutdown(self) -> None:
        self.phase = "stopping"
        log.info("종료 시작 (%s) — 실행 시간 %s", self.reason or "정상",
                 live_stt._hms(time.monotonic() - self._t0))
        if self.engine:
            self.engine.on_reaction = None   # 정리 중 새 채팅 전송 차단
        commands, self.commands = self.commands, None   # 정리 중 새 음성 명령 차단

        # 1) 오디오 수집 + STT: 태스크 취소 → run_pipeline 의 finally 가 ffmpeg/yt-dlp 종료, STT 클라이언트 닫기
        if self.pipeline and not self.pipeline.done():
            self.pipeline.cancel()

            async def _await_pipeline():
                try:
                    await self.pipeline
                except asyncio.CancelledError:
                    pass
            await self._step("오디오 스트림(ffmpeg/yt-dlp) 및 STT 정리", _await_pipeline(), 8.0)

        # 2) 리액션: 진행 중인 생성을 잠깐 기다린 뒤 Claude 클라이언트 닫기
        if self.engine:
            await self._step("리액션 엔진 정리", self.engine.aclose(wait=3.0), 5.0)
        if commands:
            await self._step("음성 명령 정리", commands.aclose(wait=4.0), 5.0)
        # 3) 채팅: 진행 중인 전송 1건만 마무리 후 HTTP 클라이언트 닫기
        if self.publisher:
            await self._step("채팅 전송기 정리", self.publisher.close(wait=5.0), 7.0)

        # 4) 남은 백그라운드 태스크 일괄 취소
        rest = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
                and self.install_signals]   # GUI 내장 시 다른 태스크는 건드리지 않음
        if rest:
            for t in rest:
                t.cancel()
            await self._step(f"남은 태스크 {len(rest)}개 취소",
                             asyncio.gather(*rest, return_exceptions=True), 3.0)
        log.info("종료 완료")
        self.phase = "done"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="YouTube Live 실시간 자막 + AI 시청자 리액션 + 채팅 전송")
    p.add_argument("url", nargs="?", help="유튜브 라이브 URL")
    p.add_argument("--input", help="yt-dlp 대신 로컬 파일/직접 미디어 URL 사용 (테스트용)")
    p.add_argument("--chat", choices=["off", "console", "youtube-api"], help="채팅 전송 방식 (.env CHAT_SENDER 보다 우선)")
    p.add_argument("--no-reactions", action="store_true", help="리액션 생성 끄기 (자막만)")
    p.add_argument("--allow-vod", action="store_true", help="라이브가 아닌 영상도 허용 (실시간 속도로 재생)")
    p.add_argument("--env-file", default=".env", help="설정 파일 경로 (기본 .env)")
    p.add_argument("-o", "--output", help="자막·리액션을 저장할 텍스트 파일")
    p.add_argument("-v", "--verbose", action="store_true", help="디버그 로그")
    p.add_argument("--check", action="store_true", help="설정/의존성 점검 후 종료")
    a = p.parse_args(argv)
    if not a.check and not a.url and not a.input:
        p.error("유튜브 URL 또는 --input 이 필요합니다.")
    return a


def check(s: Settings) -> int:
    # live_stt.check_environment 는 환경 변수를 보므로 .env 에서 읽은 키를 전달 (출력은 설정 여부만)
    for name, val in (("GROQ_API_KEY", s.groq_api_key), ("DEEPGRAM_API_KEY", s.deepgram_api_key),
                      ("ANTHROPIC_API_KEY", s.anthropic_api_key)):
        if s.secret(val):
            os.environ.setdefault(name, s.secret(val))
    code = live_stt.check_environment()
    print("\n설정 요약")
    print(f"  STT          {s.stt_provider} ({s.stt_language}), 청크 {s.chunk_seconds}s")
    print(f"  리액션       {'켜짐 — ' + s.reaction_model if s.reactions else '꺼짐'}")
    print(f"  채팅 전송    {s.chat_sender} | 쿨다운 {s.chat_cooldown_min:.0f}~{s.chat_cooldown_max:.0f}s | "
          f"방송당 {s.chat_max_per_stream} / 하루 {s.chat_max_per_day} | 표기 '{s.chat_label}'")
    if s.chat_sender == "youtube-api":
        for f in (s.yt_client_secret, s.yt_token_file):
            print(f"  [{'OK' if Path(f).exists() else '!!'}] {f}")
        if not Path(s.yt_token_file).exists():
            print("      → `python chat_sender.py auth` 로 먼저 로그인하세요")
            code = 1
    return code


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    s = load_settings(args.env_file)
    if args.chat:
        s.chat_sender = args.chat
    if args.no_reactions:
        s.reactions = False
    if args.check:
        raise SystemExit(check(s))
    if not s.has_stt_key():
        raise SystemExit("GROQ_API_KEY 또는 DEEPGRAM_API_KEY 중 하나는 .env 에 설정해야 합니다")
    setup_logging(args.verbose, args.output)
    if not live_stt.shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg 를 찾을 수 없습니다 — `python main.py --check` 와 README 참고")

    try:
        code = asyncio.run(Orchestrator(s, args).run())
    except KeyboardInterrupt:      # 시그널 핸들러 설치 전(아주 초기)에 눌린 경우
        code = EXIT_INTERRUPTED
    sys.exit(code)


if __name__ == "__main__":
    main()
