#!/usr/bin/env python3
"""
YouTube Live → 실시간 STT 파이프라인

    yt-dlp (HLS URL 추출) ─▶ ffmpeg (16kHz mono PCM) ─▶ 3~5초 청크(WAV)
        ─▶ asyncio 워커 (Groq Whisper ⇄ Deepgram 폴백) ─▶ 순서 보장 콘솔 출력

사용 예:
    python live_stt.py "https://www.youtube.com/watch?v=XXXXXXXXXXX"
    python live_stt.py URL --provider deepgram --chunk-seconds 3 -o transcript.txt
    python live_stt.py --input sample.mp3          # 로컬 파일로 실시간 시뮬레이션
    python live_stt.py --check                     # 의존성/키 점검
"""
from __future__ import annotations

import argparse
import array
import asyncio
import importlib.util
import io
import logging
import math
import os
import shutil
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Callable, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv 는 선택 사항
    load_dotenv = None

# ─────────────────────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16_000          # Whisper / Deepgram 모두 16kHz mono 면 충분
CHANNELS = 1
SAMPLE_WIDTH = 2              # s16le
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
MIN_CHUNK_SEC, MAX_CHUNK_SEC = 3.0, 5.0

# 무음/배경음악 구간에서 Whisper 가 자주 만들어내는 '환각' 문장 (전체 일치 시에만 제거)
HALLUCINATIONS = {
    "시청해주셔서 감사합니다", "시청해 주셔서 감사합니다", "구독과 좋아요 부탁드립니다",
    "구독과 좋아요", "감사합니다", "mbc 뉴스 이덕영입니다", "thank you for watching",
    "thanks for watching", "thank you", "you", "자막 제공 및 자막 편집",
}

log = logging.getLogger("live_stt")
tlog = logging.getLogger("live_stt.transcript")


# ─────────────────────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Config:
    url: Optional[str]
    input: Optional[str]
    provider: str
    language: Optional[str]
    chunk_seconds: float
    concurrency: int
    timeout: float
    cooldown: float
    max_lag: float
    silence_dbfs: float
    allow_vod: bool
    output: Optional[str]
    verbose: bool
    groq_key: str = field(default_factory=lambda: _env("GROQ_API_KEY"))
    groq_model: str = field(default_factory=lambda: _env("GROQ_MODEL", "whisper-large-v3"))
    deepgram_key: str = field(default_factory=lambda: _env("DEEPGRAM_API_KEY"))
    deepgram_model: str = field(default_factory=lambda: _env("DEEPGRAM_MODEL", "nova-2"))
    ytdlp_format: str = field(default_factory=lambda: _env("YTDLP_FORMAT", "bestaudio/93/best"))
    ytdlp_cookies: str = field(default_factory=lambda: _env("YTDLP_COOKIES"))
    context_prompt: bool = field(default_factory=lambda: _env("WHISPER_CONTEXT_PROMPT", "0") == "1")
    vocab_hint: str = ""     # 음성 명령 호출어·명령어 — Whisper 에 미리 알려 인식률 향상


# ─────────────────────────────────────────────────────────────────────────────
# 로깅: 시스템 로그는 stderr, 인식 결과는 stdout (+ 선택적으로 파일)
# ─────────────────────────────────────────────────────────────────────────────
class C:
    RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
    CYAN, GREEN, YELLOW, RED, MAGENTA = "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[35m"


def _hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class SystemFormatter(logging.Formatter):
    LEVEL_COLORS = {"DEBUG": C.DIM, "INFO": C.CYAN, "WARNING": C.YELLOW, "ERROR": C.RED, "CRITICAL": C.RED}

    def __init__(self, color: bool):
        super().__init__()
        self.color = color

    def format(self, r: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(r.created).strftime("%H:%M:%S")
        lvl = f"{r.levelname:<7}"
        msg = r.getMessage()
        if r.exc_info and log.isEnabledFor(logging.DEBUG):
            msg += "\n" + self.formatException(r.exc_info)
        if self.color:
            c = self.LEVEL_COLORS.get(r.levelname, "")
            return f"{C.DIM}{ts}{C.RESET} {c}{lvl}{C.RESET} {C.DIM}{msg}{C.RESET}" if r.levelno <= logging.INFO \
                else f"{C.DIM}{ts}{C.RESET} {c}{lvl} {msg}{C.RESET}"
        return f"{ts} {lvl} {msg}"


class TranscriptFormatter(logging.Formatter):
    """  14:03:12 │ ▶ 00:01:24 │ groq      612ms │ 인식된 문장  """

    def __init__(self, color: bool, plain_file: bool = False):
        super().__init__()
        self.color, self.plain_file = color, plain_file

    def format(self, r: logging.LogRecord) -> str:
        wall = datetime.fromtimestamp(r.created)
        stream_ts = _hms(getattr(r, "stream_ts", 0.0))
        text = r.getMessage()
        if self.plain_file:
            return f"[{wall:%Y-%m-%d %H:%M:%S}] [{stream_ts}] {text}"
        meta = f"{getattr(r, 'provider', '?'):<9}{getattr(r, 'latency_ms', 0):>5.0f}ms"
        if self.color:
            return (f"{C.DIM}{wall:%H:%M:%S}{C.RESET} │ {C.MAGENTA}▶ {stream_ts}{C.RESET} │ "
                    f"{C.DIM}{meta}{C.RESET} │ {C.BOLD}{C.GREEN}{text}{C.RESET}")
        return f"{wall:%H:%M:%S} │ ▶ {stream_ts} │ {meta} │ {text}"


def setup_logging(verbose: bool, output: Optional[str]) -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows 콘솔에서 한글 깨짐 방지
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    if os.name == "nt":
        os.system("")  # Windows 10+ 콘솔 ANSI 색상 활성화
    color = not os.environ.get("NO_COLOR")

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(SystemFormatter(color and sys.stderr.isatty()))
    log.addHandler(sh)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)

    tlog.propagate = False  # 인식 결과는 시스템 로그(stderr)로 중복 출력하지 않음
    tlog.setLevel(logging.INFO)
    th = logging.StreamHandler(sys.stdout)
    th.setFormatter(TranscriptFormatter(color and sys.stdout.isatty()))
    tlog.addHandler(th)
    if output:
        fh = logging.FileHandler(output, encoding="utf-8")
        fh.setFormatter(TranscriptFormatter(False, plain_file=True))
        tlog.addHandler(fh)


def _short(exc: BaseException, limit: int = 160) -> str:
    s = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


# ─────────────────────────────────────────────────────────────────────────────
# 1) 오디오 수집: yt-dlp 로 스트림 URL 해석 → ffmpeg 로 PCM 디코딩
# ─────────────────────────────────────────────────────────────────────────────
def subprocess_isolation() -> dict:
    """자식 프로세스를 별도 프로세스 그룹으로 실행.
    터미널의 Ctrl+C 는 프로세스 그룹 전체에 전달되므로, 분리하지 않으면 ffmpeg/yt-dlp 가 먼저 죽어
    '스트림 끊김 → 재연결' 로 오인된다. 분리해 두고 종료는 파이썬이 순서대로 직접 처리한다."""
    if os.name == "nt":
        # 새 프로세스 그룹 + 콘솔 창 숨김 (exe 실행 시 ffmpeg 검은 창이 뜨지 않도록)
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


class StreamEnded(Exception):
    """라이브 방송이 종료되었거나 라이브가 아님."""


def ytdlp_base_cmd() -> list[str]:
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]   # pip 로 설치한 버전 우선
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    raise SystemExit("yt-dlp 가 없습니다:  pip install -U yt-dlp")


IS_FROZEN = getattr(sys, "frozen", False)   # PyInstaller exe 로 실행 중인지


async def _probe_subprocess(cfg: Config) -> tuple[str, str]:
    """yt-dlp 를 별도 프로세스로 실행 (일반 파이썬 실행 시). 반환: (live_status, media_url)"""
    cmd = ytdlp_base_cmd() + [
        "--no-playlist", "--no-warnings", "-f", cfg.ytdlp_format,
        "--print", "live_status", "--print", "urls",
    ]
    if cfg.ytdlp_cookies:
        cmd += ["--cookies", cfg.ytdlp_cookies]
    cmd.append(cfg.url or "")

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **subprocess_isolation())
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        raise RuntimeError("yt-dlp 응답 시간 초과(60s)")
    finally:
        if proc.returncode is None:   # 타임아웃·취소(Ctrl+C) 시 yt-dlp 가 남지 않도록
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        msg = err.decode(errors="replace").strip().splitlines()
        text = msg[-1] if msg else f"exit code {proc.returncode}"
        if "has ended" in text.lower():
            raise StreamEnded(text)
        raise RuntimeError(text)
    lines = [ln.strip() for ln in out.decode(errors="replace").splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"yt-dlp 출력 해석 실패: {lines!r}")
    return lines[0], lines[1]


def _probe_api_sync(url: str, fmt: str, cookies: str) -> tuple[str, str]:
    import yt_dlp
    opts = {"format": fmt, "quiet": True, "no_warnings": True, "noplaylist": True,
            "skip_download": True, "socket_timeout": 20}
    if cookies:
        opts["cookiefile"] = cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        text = str(e).replace("ERROR: ", "")
        if "has ended" in text.lower():
            raise StreamEnded(text) from None
        raise RuntimeError(text) from None
    media = info.get("url") or next((f.get("url") for f in info.get("requested_formats") or [] if f.get("url")), None)
    if not media:
        raise RuntimeError("스트림 주소를 찾지 못했습니다")
    return str(info.get("live_status") or ""), media


async def _probe_api(cfg: Config) -> tuple[str, str]:
    """yt-dlp 파이썬 API 로 직접 호출 (exe 실행 시: sys.executable 이 exe 자신이라 -m yt_dlp 불가)."""
    return await asyncio.wait_for(
        asyncio.to_thread(_probe_api_sync, cfg.url or "", cfg.ytdlp_format, cfg.ytdlp_cookies), timeout=60)


async def resolve_stream(cfg: Config) -> tuple[str, bool]:
    """(미디어 URL, is_live) 반환."""
    use_api = IS_FROZEN and importlib.util.find_spec("yt_dlp") is not None
    live_status, media_url = await (_probe_api(cfg) if use_api else _probe_subprocess(cfg))
    is_live = live_status == "is_live"
    if not is_live and not cfg.allow_vod:
        if live_status in ("was_live", "post_live"):
            raise StreamEnded(f"라이브가 종료되었습니다 (live_status={live_status})")
        if live_status == "is_upcoming":
            raise RuntimeError("아직 시작 전인 예약 라이브입니다 — 대기 후 재시도")
        raise StreamEnded(f"라이브 방송이 아닙니다 (live_status={live_status}). VOD 는 --allow-vod 사용")
    return media_url, is_live


def ffmpeg_cmd(src: str, realtime: bool) -> list[str]:
    cmd = [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if src.startswith(("http://", "https://")):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-rw_timeout", "15000000"]  # 15초 동안 데이터가 없으면 종료 → 상위에서 재연결
    if realtime:
        cmd += ["-re"]                       # VOD/로컬 파일을 실제 재생 속도로 읽어 라이브처럼 시뮬레이션
    cmd += ["-i", src,
            "-vn", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
            "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"]
    return cmd


@dataclass
class AudioChunk:
    seq: int
    offset: float          # 스트림 시작 기준 위치(초)
    duration: float
    wav: bytes
    dbfs: float
    captured_at: float     # time.monotonic() — 큐 지연(lag) 계산용


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def rms_dbfs(pcm: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return -math.inf
    ms = sum(s * s for s in samples) / len(samples)
    return 20 * math.log10(math.sqrt(ms) / 32768) if ms > 0 else -math.inf


async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
    assert proc.stderr
    async for line in proc.stderr:
        if text := line.decode(errors="replace").strip():
            log.warning("ffmpeg: %s", text)


async def audio_chunks(cfg: Config) -> AsyncIterator[AudioChunk]:
    """ffmpeg 출력(PCM)을 chunk_seconds 단위로 잘라 WAV 청크를 생성. 끊기면 자동 재연결."""
    chunk_bytes = int(cfg.chunk_seconds * BYTES_PER_SEC)
    chunk_bytes -= chunk_bytes % SAMPLE_WIDTH
    seq, offset, failures = 0, 0.0, 0

    while True:
        # --- 소스 결정 ---
        if cfg.input:
            src, realtime = cfg.input, not cfg.input.startswith(("http://", "https://"))
        else:
            try:
                log.info("yt-dlp 로 스트림 주소를 가져오는 중…")
                src, is_live = await resolve_stream(cfg)
                realtime = not is_live
                log.info("스트림 확인 (%s)", "LIVE" if is_live else "VOD → 실시간 속도로 재생")
            except StreamEnded as e:
                log.warning("%s — 수집을 종료합니다.", e)
                return
            except Exception as e:
                failures += 1
                delay = min(30, 2 ** failures)
                log.error("스트림 주소 해석 실패 (%s) — %ds 후 재시도", _short(e), delay)
                await asyncio.sleep(delay)
                continue

        # --- ffmpeg 실행 & 청크 분할 ---
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd(src, realtime), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **subprocess_isolation())
        stderr_task = asyncio.create_task(_drain_stderr(proc))
        log.info("오디오 수신 시작 (청크 %.1fs, 16kHz mono)", cfg.chunk_seconds)
        try:
            assert proc.stdout
            while True:
                try:
                    pcm = await proc.stdout.readexactly(chunk_bytes)
                except asyncio.IncompleteReadError as e:
                    pcm = e.partial  # 스트림 끝: 1초 이상 남은 꼬리는 처리
                    if len(pcm) < BYTES_PER_SEC:
                        break
                failures = 0
                dur = len(pcm) / BYTES_PER_SEC
                yield AudioChunk(seq, offset, dur, pcm_to_wav(pcm), rms_dbfs(pcm), time.monotonic())
                seq += 1
                offset += dur
                if len(pcm) < chunk_bytes:
                    break
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
            stderr_task.cancel()

        if cfg.input:
            log.info("입력 파일 처리가 끝났습니다.")
            return
        failures += 1
        delay = min(30, 2 ** failures)
        log.warning("오디오 스트림이 끊겼습니다 (ffmpeg exit=%s) — %ds 후 재연결", proc.returncode, delay)
        await asyncio.sleep(delay)


# ─────────────────────────────────────────────────────────────────────────────
# 2) STT 공급자 + 폴백
# ─────────────────────────────────────────────────────────────────────────────
class STTError(Exception):
    pass


@dataclass
class STTResult:
    text: str
    provider: str
    latency_ms: float


class BaseSTT:
    name = "base"

    async def transcribe(self, wav: bytes, prompt: Optional[str] = None) -> str:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class GroqWhisperSTT(BaseSTT):
    name = "groq"

    def __init__(self, api_key: str, model: str, language: Optional[str], timeout: float):
        from groq import AsyncGroq
        self.client = AsyncGroq(api_key=api_key, timeout=timeout, max_retries=1)
        self.model, self.language = model, language

    async def transcribe(self, wav: bytes, prompt: Optional[str] = None) -> str:
        kwargs: dict = dict(file=("chunk.wav", wav), model=self.model,
                            response_format="verbose_json", temperature=0.0)
        if self.language:
            kwargs["language"] = self.language
        if prompt:
            kwargs["prompt"] = prompt[-120:]   # 직전 문맥 + 명령어 힌트 (Groq prompt 한도: 224 토큰)
        resp = await self.client.audio.transcriptions.create(**kwargs)

        # verbose_json 의 세그먼트별 no_speech_prob 로 무음 환각 제거
        segments = getattr(resp, "segments", None)
        if segments:
            parts = []
            for s in segments:
                get = s.get if isinstance(s, dict) else (lambda k, d=None, _s=s: getattr(_s, k, d))
                if (get("no_speech_prob", 0.0) or 0.0) < 0.6:
                    parts.append((get("text", "") or "").strip())
            return " ".join(p for p in parts if p)
        return getattr(resp, "text", "") or ""

    async def aclose(self) -> None:
        await self.client.close()


class DeepgramSTT(BaseSTT):
    name = "deepgram"

    def __init__(self, api_key: str, model: str, language: Optional[str], timeout: float):
        from deepgram import DeepgramClient, PrerecordedOptions
        import httpx
        self.client = DeepgramClient(api_key)
        opts: dict = dict(model=model, smart_format=True, punctuate=True)
        if language:
            opts["language"] = language
        else:
            opts["detect_language"] = True
        self.options = PrerecordedOptions(**opts)
        self.timeout = httpx.Timeout(timeout, connect=10.0)

    async def transcribe(self, wav: bytes, prompt: Optional[str] = None) -> str:
        resp = await self.client.listen.asyncrest.v("1").transcribe_file(
            {"buffer": wav}, self.options, timeout=self.timeout)
        channels = resp.results.channels
        if not channels or not channels[0].alternatives:
            return ""
        return channels[0].alternatives[0].transcript or ""


class FallbackSTT:
    """우선순위대로 시도. 실패한 공급자는 cooldown 동안 건너뛰었다가 자동으로 다시 시도한다."""

    def __init__(self, providers: list[BaseSTT], cooldown: float):
        self.providers = providers
        self.cooldown = cooldown
        self._down_until = {p.name: 0.0 for p in providers}

    @property
    def names(self) -> str:
        return " → ".join(p.name for p in self.providers)

    async def transcribe(self, wav: bytes, prompt: Optional[str] = None) -> STTResult:
        now = time.monotonic()
        healthy = [p for p in self.providers if self._down_until[p.name] <= now]
        candidates = healthy or list(self.providers)   # 전부 쿨다운이면 그래도 전부 시도
        last_exc: Optional[BaseException] = None
        for p in candidates:
            t0 = time.perf_counter()
            try:
                text = await p.transcribe(wav, prompt)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_exc = e
                self._down_until[p.name] = time.monotonic() + self.cooldown
                idx = candidates.index(p)
                nxt = candidates[idx + 1].name if idx + 1 < len(candidates) else None
                log.warning("[%s] 실패: %s%s", p.name, _short(e), f" → {nxt} 로 폴백" if nxt else "")
                continue
            if self._down_until[p.name]:
                log.info("[%s] 복구됨", p.name)
                self._down_until[p.name] = 0.0
            return STTResult(text.strip(), p.name, (time.perf_counter() - t0) * 1000)
        raise STTError(f"모든 STT 공급자 실패: {_short(last_exc) if last_exc else '없음'}")

    async def aclose(self) -> None:
        for p in self.providers:
            try:
                await p.aclose()
            except Exception:
                pass


def build_stt(cfg: Config) -> FallbackSTT:
    factories = {
        "groq": (cfg.groq_key, "groq", "GROQ_API_KEY",
                 lambda: GroqWhisperSTT(cfg.groq_key, cfg.groq_model, cfg.language, cfg.timeout)),
        "deepgram": (cfg.deepgram_key, "deepgram", "DEEPGRAM_API_KEY",
                     lambda: DeepgramSTT(cfg.deepgram_key, cfg.deepgram_model, cfg.language, cfg.timeout)),
    }
    order = [cfg.provider] + [n for n in factories if n != cfg.provider]
    providers: list[BaseSTT] = []
    for name in order:
        key, module, env_name, make = factories[name]
        if not key:
            log.debug("[%s] %s 미설정 → 건너뜀", name, env_name)
            continue
        if not importlib.util.find_spec(module):
            log.warning("[%s] 키는 있으나 패키지 미설치 → pip install -r requirements.txt", name)
            continue
        try:
            providers.append(make())
        except Exception as e:
            log.warning("[%s] 초기화 실패: %s", name, _short(e))
    if not providers:
        raise SystemExit("사용 가능한 STT 공급자가 없습니다. .env 에 GROQ_API_KEY 또는 DEEPGRAM_API_KEY 를 설정하세요.")
    if providers[0].name != cfg.provider:
        log.warning("기본 공급자 '%s' 를 사용할 수 없어 '%s' 로 시작합니다.", cfg.provider, providers[0].name)
    return FallbackSTT(providers, cfg.cooldown)


# ─────────────────────────────────────────────────────────────────────────────
# 3) 비동기 파이프라인: 청크마다 태스크 생성(동시 처리) + 순서 보장 출력
# ─────────────────────────────────────────────────────────────────────────────
def _is_hallucination(text: str) -> bool:
    norm = text.strip().strip(".!?。…~ ").lower()
    return not norm or norm in HALLUCINATIONS


@dataclass
class Stats:
    chunks: int = 0
    printed: int = 0
    silent: int = 0
    stale: int = 0
    errors: int = 0
    latencies: list[float] = field(default_factory=list)


TranscriptCallback = Callable[[float, str, float], None]   # (방송 내 위치, 텍스트, 음량 dBFS)


async def run_pipeline(cfg: Config, on_transcript: Optional[TranscriptCallback] = None) -> None:
    """STT 파이프라인. 인식 결과가 출력될 때마다 on_transcript 를 (동기, 즉시 반환) 호출한다."""
    stt = build_stt(cfg)
    log.info("STT 공급자: %s | 언어: %s | 청크: %.1fs | 동시성: %d",
             stt.names, cfg.language or "auto", cfg.chunk_seconds, cfg.concurrency)

    sem = asyncio.Semaphore(cfg.concurrency)
    ordered: asyncio.Queue = asyncio.Queue()   # (chunk, task) 를 '생성 순서'대로 보관
    stats = Stats()
    last_text = ""

    async def process(chunk: AudioChunk) -> Optional[STTResult]:
        if chunk.dbfs < cfg.silence_dbfs:
            stats.silent += 1
            log.debug("#%d 무음 (%.1f dBFS) → 건너뜀", chunk.seq, chunk.dbfs)
            return None
        async with sem:
            lag = time.monotonic() - chunk.captured_at
            if lag > cfg.max_lag:                   # STT 가 밀리면 오래된 청크를 버려 실시간성 유지
                stats.stale += 1
                log.warning("#%d 처리 지연 %.1fs > %.0fs → 청크 폐기", chunk.seq, lag, cfg.max_lag)
                return None
            prompt = " ".join(p for p in ((last_text if cfg.context_prompt else ""), cfg.vocab_hint) if p)
            return await stt.transcribe(chunk.wav, prompt or None)

    async def printer() -> None:
        nonlocal last_text
        while True:
            item = await ordered.get()
            if item is None:
                return
            chunk, task = item
            try:
                res = await task
            except STTError as e:
                stats.errors += 1
                log.error("#%d [%s] %s", chunk.seq, _hms(chunk.offset), e)
                continue
            if res is None:
                continue
            stats.latencies.append(res.latency_ms)
            if _is_hallucination(res.text):
                log.debug("#%d 빈 결과/환각 필터: %r", chunk.seq, res.text)
                continue
            stats.printed += 1
            last_text = res.text
            tlog.info(res.text, extra={"stream_ts": chunk.offset,
                                       "provider": res.provider, "latency_ms": res.latency_ms})
            if on_transcript:   # 즉시 반환해야 함 — 무거운 작업은 콜백 쪽에서 백그라운드 태스크로
                try:
                    on_transcript(chunk.offset, res.text, chunk.dbfs)
                except Exception as e:  # 후속 모듈 오류가 STT 출력을 멈추지 않도록 격리
                    log.warning("후처리 콜백 오류 (%s: %s)", type(e).__name__, e)

    printer_task = asyncio.create_task(printer())
    pending: set[asyncio.Task] = set()
    try:
        async for chunk in audio_chunks(cfg):
            stats.chunks += 1
            log.debug("#%d 청크 %.1fs @%s (%.1f dBFS)", chunk.seq, chunk.duration, _hms(chunk.offset), chunk.dbfs)
            task = asyncio.create_task(process(chunk))
            pending.add(task)
            task.add_done_callback(pending.discard)
            await ordered.put((chunk, task))
        await ordered.put(None)
        await printer_task                            # 남은 결과까지 모두 출력 후 종료
    finally:
        for t in (*pending, printer_task):
            t.cancel()
        await asyncio.gather(*pending, printer_task, return_exceptions=True)
        await stt.aclose()
        avg = sum(stats.latencies) / len(stats.latencies) if stats.latencies else 0
        log.info("종료 — 청크 %d | 출력 %d | 무음 %d | 폐기 %d | 오류 %d | 평균 STT 지연 %.0fms",
                 stats.chunks, stats.printed, stats.silent, stats.stale, stats.errors, avg)


# ─────────────────────────────────────────────────────────────────────────────
# 의존성 점검 & CLI
# ─────────────────────────────────────────────────────────────────────────────
def check_environment() -> int:
    ok = True

    def line(status: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok &= status
        print(f"  [{'OK' if status else '!!'}] {label:<18} {detail}")

    print("의존성 점검")
    ff = shutil.which("ffmpeg")
    if ff:
        ver = subprocess.run([ff, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
        line(True, "ffmpeg", ver)
    else:
        line(False, "ffmpeg", "PATH 에서 찾을 수 없음 → README.md 의 설치 안내 참고")
    try:
        cmd = ytdlp_base_cmd() + ["--version"]
        line(True, "yt-dlp", subprocess.run(cmd, capture_output=True, text=True).stdout.strip())
    except SystemExit as e:
        line(False, "yt-dlp", str(e))
    for mod, pkg in (("groq", "groq"), ("deepgram", "deepgram-sdk"), ("anthropic", "anthropic"),
                     ("dotenv", "python-dotenv")):
        found = importlib.util.find_spec(mod) is not None
        print(f"  [{'OK' if found else '--'}] {pkg:<18} {'설치됨' if found else '미설치'}")
    keys = {k: bool(_env(k)) for k in ("GROQ_API_KEY", "DEEPGRAM_API_KEY")}
    for k, v in keys.items():
        print(f"  [{'OK' if v else '--'}] {k:<18} {'설정됨' if v else '미설정'}")
    if not any(keys.values()):
        line(False, "STT 키", "GROQ_API_KEY 또는 DEEPGRAM_API_KEY 중 하나는 필요")
    has_claude = bool(_env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN"))
    print(f"  [{'OK' if has_claude else '--'}] {'ANTHROPIC_API_KEY':<18} "
          f"{'설정됨' if has_claude else '미설정 (리액션 생성 시 필요, `ant auth login` 프로필도 가능)'}")
    print("결과:", "준비 완료" if ok else "누락된 항목을 설치/설정하세요")
    return 0 if ok else 1


def parse_args(argv: Optional[list[str]] = None) -> Config:
    p = argparse.ArgumentParser(description="YouTube Live 실시간 STT (yt-dlp + ffmpeg + Groq/Deepgram)")
    p.add_argument("url", nargs="?", help="유튜브 라이브 URL")
    p.add_argument("--input", help="yt-dlp 대신 로컬 파일/직접 미디어 URL 사용 (테스트용)")
    p.add_argument("--provider", choices=["groq", "deepgram"], default=_env("STT_PROVIDER", "groq").lower())
    p.add_argument("--language", default=_env("STT_LANGUAGE", "ko"), help="예: ko, en, ja / 'auto' 는 자동 감지")
    p.add_argument("--chunk-seconds", type=float, default=float(_env("CHUNK_SECONDS", "4")))
    p.add_argument("--concurrency", type=int, default=int(_env("STT_CONCURRENCY", "2")))
    p.add_argument("--timeout", type=float, default=float(_env("STT_TIMEOUT", "20")))
    p.add_argument("--cooldown", type=float, default=float(_env("STT_COOLDOWN", "30")))
    p.add_argument("--max-lag", type=float, default=float(_env("MAX_LAG_SECONDS", "15")))
    p.add_argument("--silence-dbfs", type=float, default=float(_env("SILENCE_DBFS", "-50")))
    p.add_argument("--allow-vod", action="store_true", help="라이브가 아닌 일반 영상도 허용 (실시간 속도로 재생)")
    p.add_argument("-o", "--output", help="인식 결과를 저장할 텍스트 파일")
    p.add_argument("-v", "--verbose", action="store_true", help="디버그 로그 출력")
    p.add_argument("--check", action="store_true", help="ffmpeg / yt-dlp / API 키 점검 후 종료")
    a = p.parse_args(argv)

    if a.check:
        raise SystemExit(check_environment())
    if not a.url and not a.input:
        p.error("유튜브 URL 또는 --input 이 필요합니다.")

    chunk = min(max(a.chunk_seconds, MIN_CHUNK_SEC), MAX_CHUNK_SEC)
    cfg = Config(
        url=a.url, input=a.input, provider=a.provider,
        language=None if a.language.lower() in ("", "auto") else a.language,
        chunk_seconds=chunk, concurrency=max(1, a.concurrency), timeout=a.timeout,
        cooldown=a.cooldown, max_lag=a.max_lag, silence_dbfs=a.silence_dbfs,
        allow_vod=a.allow_vod, output=a.output, verbose=a.verbose,
    )
    cfg._chunk_clamped = chunk != a.chunk_seconds  # type: ignore[attr-defined]
    return cfg


def main(argv: Optional[list[str]] = None) -> None:
    if load_dotenv:
        load_dotenv()
    cfg = parse_args(argv)
    setup_logging(cfg.verbose, cfg.output)
    if getattr(cfg, "_chunk_clamped", False):
        log.warning("청크 길이는 %.0f~%.0f초로 제한됩니다 → %.1fs 사용", MIN_CHUNK_SEC, MAX_CHUNK_SEC, cfg.chunk_seconds)
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg 를 찾을 수 없습니다. `python live_stt.py --check` 와 README.md 를 참고하세요.")
    try:
        asyncio.run(run_pipeline(cfg))
    except KeyboardInterrupt:
        print("\n중단됨 (Ctrl+C)", file=sys.stderr)


if __name__ == "__main__":
    main()
