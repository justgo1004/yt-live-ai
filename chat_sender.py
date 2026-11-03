#!/usr/bin/env python3
"""
Phase 3 — 리액션을 유튜브 라이브 채팅으로 전송

    ReactionEngine ─(on_reaction)─▶ SafeChatPublisher ─▶ ChatSender (교체 가능)
                                     · 랜덤 쿨다운 45~90초          ├─ ConsoleSender      (dry-run, 기본)
                                     · 방송당 / 하루 최대 횟수       └─ YouTubeApiSender   (공식 Live Chat API, OAuth2)
                                     · 중복·길이 검사, AI 표기

전송 방식은 공식 YouTube Data API v3(liveChatMessages.insert)만 지원한다. 모든 메시지 앞에는
AI 표기(label)가 붙는다. 채팅을 보는 사람이 사람이 아닌 봇 메시지라는 것을 알 수 있게 하기 위해서다.

사용 예:
    python chat_sender.py auth                                  # 최초 1회 OAuth 로그인 → token.json 저장
    python chat_sender.py test "URL" "테스트 메시지"              # 실제로 1건 전송
    python main.py "URL" --chat youtube-api
"""
from __future__ import annotations

import abc
import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger("live_stt.chat")

API_BASE = "https://www.googleapis.com/youtube/v3"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
YT_MAX_LEN = 200   # 유튜브 라이브 채팅 메시지 최대 길이


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def extract_video_id(url_or_id: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url_or_id)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_id):
        return url_or_id
    raise ValueError(f"유튜브 영상 ID 를 찾을 수 없습니다: {url_or_id}")


# ─────────────────────────────────────────────────────────────────────────────
# 1) 전송 방식 추상화
# ─────────────────────────────────────────────────────────────────────────────
class SendError(Exception):
    """일시적 실패 — 이번 메시지만 실패, 이후 전송은 계속."""


class SendFatal(SendError):
    """계속 시도해도 소용없는 실패 (인증, 할당량 소진, 채팅 종료 등) → 전송 기능 중단."""


class ChatSender(abc.ABC):
    """새 전송 방식은 이 클래스를 상속해 start / send / close 만 구현하면 된다."""

    name = "base"

    async def start(self) -> None:
        """연결 준비 (인증, 채팅방 ID 확인 등). 실패 시 SendFatal."""

    @abc.abstractmethod
    async def send(self, text: str) -> None:
        """메시지 1건 전송. 실패 시 SendError / SendFatal."""

    async def close(self) -> None:
        pass


class ConsoleSender(ChatSender):
    """실제로 전송하지 않고 '전송될 메시지'만 로그로 보여 주는 dry-run 전송기."""

    name = "console"

    def __init__(self, bot_name: str = ""):
        self.bot_name = bot_name

    async def send(self, text: str) -> None:
        log.debug("[dry-run]%s 채팅 전송 예정: %s", f"[{self.bot_name}]" if self.bot_name else "", text)


class YouTubeApiSender(ChatSender):
    """
    공식 YouTube Data API v3 사용.
      - videos.list(part=liveStreamingDetails) → activeLiveChatId   (할당량 1)
      - liveChatMessages.insert                                      (할당량 약 50 / 건, 기본 일일 한도 10,000)
    인증: OAuth2 설치형 앱 흐름. 최초 1회 브라우저 로그인 후 token.json 의 refresh token 으로 자동 갱신.
    """

    name = "youtube-api"

    def __init__(self, video: str, client_secret: str = "client_secret.json",
                 token_path: str = "token.json", timeout: float = 10.0, quota=None):
        self.video_id = extract_video_id(video)
        self.client_secret, self.token_path, self.timeout = client_secret, token_path, timeout
        self.creds = None
        self.http = None
        self.live_chat_id: Optional[str] = None
        self.quota = quota                      # quota.QuotaMeter (선택) — 프로젝트별 하루 사용량 기록
        from quota import project_key
        self.project = project_key(client_secret)

    async def start(self) -> None:
        import httpx
        try:
            self.creds = await asyncio.to_thread(load_credentials, self.client_secret, self.token_path,
                                                 False)
        except FileNotFoundError as e:
            raise SendFatal(f"{e} — 먼저 `python chat_sender.py auth` 를 실행하세요") from e
        self.http = httpx.AsyncClient(base_url=API_BASE, timeout=self.timeout)
        r = await self._request("GET", "/videos", units=1,
                                params={"part": "liveStreamingDetails", "id": self.video_id})
        items = r.json().get("items", [])
        chat_id = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId") if items else None
        if not chat_id:
            raise SendFatal(f"영상 {self.video_id} 에 활성화된 라이브 채팅이 없습니다 (종료됐거나 채팅 꺼짐)")
        self.live_chat_id = chat_id
        log.info("YouTube Live Chat 연결됨 (video=%s)", self.video_id)

    async def _auth_header(self) -> dict:
        if not self.creds.valid:
            await asyncio.to_thread(_refresh_and_save, self.creds, self.token_path)
        return {"Authorization": f"Bearer {self.creds.token}"}

    async def _request(self, method: str, path: str, units: int = 1, **kw):
        import httpx
        for attempt in range(2):
            try:
                r = await self.http.request(method, path, headers=await self._auth_header(), **kw)
            except httpx.HTTPError as e:
                raise SendError(f"네트워크 오류: {type(e).__name__}") from e
            if self.quota is not None and r.status_code != 401:
                self.quota.add(self.project, units)
            if r.status_code == 401 and attempt == 0:      # 토큰 만료 직후 → 한 번 갱신 후 재시도
                await asyncio.to_thread(_refresh_and_save, self.creds, self.token_path)
                continue
            if r.is_success:
                return r
            raise _classify_error(r)
        raise SendFatal("인증 실패 (토큰 갱신 후에도 401) — token.json 삭제 후 다시 auth")

    async def send(self, text: str) -> None:
        body = {"snippet": {"liveChatId": self.live_chat_id, "type": "textMessageEvent",
                            "textMessageDetails": {"messageText": text}}}
        await self._request("POST", "/liveChat/messages", units=50, params={"part": "snippet"}, json=body)

    async def close(self) -> None:
        if self.http:
            await self.http.aclose()


def fetch_channel_info(client_secret: str, token_path: str, quota=None) -> tuple[str, str]:
    """로그인된 계정의 (채널 이름, 채널 ID). 할당량 1. 동기 함수 — 스레드에서 호출."""
    import httpx
    creds = load_credentials(client_secret, token_path, interactive=False)
    r = httpx.get(f"{API_BASE}/channels", params={"part": "snippet", "mine": "true"},
                  headers={"Authorization": f"Bearer {creds.token}"}, timeout=10)
    if quota is not None:
        from quota import project_key
        quota.add(project_key(client_secret), 1)
    if not r.is_success:
        raise _classify_error(r)
    items = r.json().get("items") or []
    if not items:
        return "(채널 없음 — 유튜브 채널을 먼저 만드세요)", ""
    return items[0]["snippet"]["title"], items[0]["id"]


def fetch_channel_title(client_secret: str, token_path: str) -> str:
    return fetch_channel_info(client_secret, token_path)[0]


def _classify_error(r) -> SendError:
    try:
        err = r.json().get("error", {})
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        msg = err.get("message", "")
    except ValueError:
        reason, msg = "", r.text[:200]
    fatal_reasons = {"quotaExceeded", "dailyLimitExceeded", "liveChatEnded", "liveChatDisabled",
                     "liveChatNotFound", "forbidden", "insufficientPermissions", "videoNotFound",
                     "authError", "accessNotConfigured"}
    detail = f"HTTP {r.status_code} {reason or ''} {msg}".strip()
    if reason in fatal_reasons or r.status_code in (401, 404):
        return SendFatal(detail)
    return SendError(detail)            # rateLimitExceeded, 5xx 등은 일시적


def load_credentials(client_secret: str, token_path: str, interactive: bool = True):
    """token.json 이 있으면 재사용(필요 시 갱신), 없으면 interactive=True 일 때만 브라우저 로그인."""
    from google.oauth2.credentials import Credentials
    creds = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        _refresh_and_save(creds, token_path)
        return creds
    if not interactive:
        raise FileNotFoundError(f"유효한 OAuth 토큰({token_path})이 없습니다")
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not Path(client_secret).exists():
        raise FileNotFoundError(f"OAuth 클라이언트 파일({client_secret})이 없습니다 — README 의 Google Cloud 설정 참고")
    flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
    creds = flow.run_local_server(
        port=0, prompt="consent", timeout_seconds=180,     # 3분 안에 로그인하지 않으면 취소
        success_message="로그인 완료! 이 창을 닫고 라이브 AI 리액션 앱으로 돌아가세요.")
    _save_token(creds, token_path)
    return creds


def _refresh_and_save(creds, token_path: str) -> None:
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    try:
        creds.refresh(Request())
    except RefreshError as e:
        raise SendFatal(f"토큰 갱신 실패 ({e}) — token.json 삭제 후 `python chat_sender.py auth`") from e
    _save_token(creds, token_path)


def _save_token(creds, token_path: str) -> None:
    p = Path(token_path)
    p.write_text(creds.to_json(), encoding="utf-8")
    try:
        p.chmod(0o600)                  # refresh token 은 비밀번호급 정보
    except OSError:
        pass


def build_sender(kind: str, video: Optional[str]) -> ChatSender:
    if kind == "console":
        return ConsoleSender()
    if kind == "youtube-api":
        if not video:
            raise ValueError("youtube-api 전송에는 유튜브 URL(또는 CHAT_VIDEO_ID)이 필요합니다")
        return YouTubeApiSender(video, _env("YT_CLIENT_SECRET", "client_secret.json"),
                                _env("YT_TOKEN_FILE", "token.json"))
    raise ValueError(f"알 수 없는 전송 방식: {kind}")


# ─────────────────────────────────────────────────────────────────────────────
# 2) 가드레일: 쿨다운 / 횟수 제한 / 중복 / AI 표기
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ChatPolicy:
    cooldown_min: float = field(default_factory=lambda: float(_env("CHAT_COOLDOWN_MIN", "45")))
    cooldown_max: float = field(default_factory=lambda: float(_env("CHAT_COOLDOWN_MAX", "90")))
    max_per_stream: int = field(default_factory=lambda: int(_env("CHAT_MAX_PER_STREAM", "20")))
    max_per_day: int = field(default_factory=lambda: int(_env("CHAT_MAX_PER_DAY", "60")))
    label: str = field(default_factory=lambda: os.environ.get("CHAT_LABEL", "🤖AI: "))
    state_file: str = field(default_factory=lambda: _env("CHAT_STATE_FILE", ".chat_state.json"))
    dedupe_window: int = 10

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("CHAT_LABEL 은 비워 둘 수 없습니다: 자동 생성 메시지는 AI 표기를 붙여 전송합니다.")
        if not 0 < self.cooldown_min <= self.cooldown_max:
            raise ValueError("CHAT_COOLDOWN_MIN/MAX 값이 잘못되었습니다")


class DailyCounter:
    """날짜별 전송 횟수를 파일에 저장 → 프로그램을 재시작해도 하루 한도가 유지된다."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.day, self.count = date.today().isoformat(), 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("date") == self.day:
                self.count = int(data.get("count", 0))
        except (FileNotFoundError, ValueError):
            pass

    def _rollover(self) -> None:
        today = date.today().isoformat()
        if today != self.day:
            self.day, self.count = today, 0

    def value(self) -> int:
        self._rollover()
        return self.count

    def increment(self) -> None:
        self._rollover()
        self.count += 1
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"date": self.day, "count": self.count}), encoding="utf-8")
        tmp.replace(self.path)          # 원자적 교체


class SafeChatPublisher:
    """
    offer(text) 는 즉시 반환한다. 조건을 통과하면 백그라운드로 전송하고, 아니면 사유와 함께 버린다.
    리액션은 그 순간에만 의미가 있으므로 쿨다운 중인 메시지를 줄 세워 나중에 보내지 않는다.
    """

    DIRECT_GAP = 2.0           # 직접 전송(명령·이벤트) 사이 최소 간격(초) — 연속 도배 방지
    DIRECT_MAX_PER_DAY = 150   # 봇 하나의 직접 전송 하루 한도

    def __init__(self, sender: ChatSender, policy: Optional[ChatPolicy] = None, bot_name: str = ""):
        self.sender = sender
        self.policy = policy or ChatPolicy()
        self.bot_name = bot_name
        self._tag = f"[{bot_name}] " if bot_name else ""
        # dry-run 은 실제 하루 한도를 소모하지 않도록 별도 파일에 기록
        state = self.policy.state_file + (".dryrun" if sender.name == "console" else "")
        self.daily = DailyCounter(state)
        self.sent_this_stream = 0
        self._next_allowed = 0.0                  # time.monotonic() 기준
        self._recent: deque[str] = deque(maxlen=self.policy.dedupe_window)
        self._inflight: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()               # 리액션·음성 명령 전송이 겹치지 않게 순서대로
        self.enabled = False
        self.stats = {"offered": 0, "sent": 0, "cooldown": 0, "limit": 0, "duplicate": 0, "busy": 0, "failed": 0,
                      "direct": 0}
        self.direct_daily = DailyCounter(state + ".direct")

    async def start(self) -> bool:
        try:
            await self.sender.start()
        except SendError as e:
            log.error("채팅 전송 비활성화: %s", e)
            return False
        self.enabled = True
        p = self.policy
        log.info("채팅 전송 켜짐 %s[%s] — 표기 '%s' | 리액션 쿨다운 %.0f~%.0fs | 방송당 %d건 | 하루 %d건 (오늘 %d건 사용)",
                 self._tag, self.sender.name, p.label.strip(), p.cooldown_min, p.cooldown_max,
                 p.max_per_stream, p.max_per_day, self.daily.value())
        return True

    def _format(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        return (self.policy.label + text)[:YT_MAX_LEN]

    def offer(self, text: str) -> str:
        """전송 시도. 반환값은 처리 결과 사유 (sent-queued / cooldown / limit / ...)."""
        self.stats["offered"] += 1
        if not self.enabled:
            return "disabled"
        now = time.monotonic()
        if self.sent_this_stream >= self.policy.max_per_stream or self.daily.value() >= self.policy.max_per_day:
            self.stats["limit"] += 1
            if self.stats["limit"] == 1:
                log.warning("채팅 전송 한도 도달 (방송 %d/%d, 오늘 %d/%d) — 이후 리액션은 화면에만 표시",
                            self.sent_this_stream, self.policy.max_per_stream,
                            self.daily.value(), self.policy.max_per_day)
            return "limit"
        if self._inflight and not self._inflight.done():
            self.stats["busy"] += 1
            return "busy"
        if now < self._next_allowed:
            self.stats["cooldown"] += 1
            log.debug("채팅 쿨다운 %.0fs 남음 → 건너뜀: %s", self._next_allowed - now, text)
            return "cooldown"
        key = re.sub(r"[^\w]", "", text)
        if key in self._recent:
            self.stats["duplicate"] += 1
            return "duplicate"
        self._recent.append(key)
        # 전송 시도 시점부터 쿨다운 시작 (실패해도 바로 재시도하지 않음)
        self._next_allowed = now + random.uniform(self.policy.cooldown_min, self.policy.cooldown_max)
        self._inflight = asyncio.create_task(self._send(self._format(text)))
        return "queued"

    async def _send(self, msg: str) -> None:
        try:
            async with self._lock:
                await self.sender.send(msg)
        except SendFatal as e:
            self.enabled = False
            self.stats["failed"] += 1
            log.error("채팅 전송 중단: %s", e)
            return
        except SendError as e:
            self.stats["failed"] += 1
            log.warning("채팅 전송 실패 (이번 건만): %s", e)
            return
        self.sent_this_stream += 1
        self.daily.increment()
        self.stats["sent"] += 1
        log.info("📤 %s채팅 전송 (%d/%d, 오늘 %d/%d, 다음 전송까지 ≥%.0fs): %s", self._tag,
                 self.sent_this_stream, self.policy.max_per_stream, self.daily.value(),
                 self.policy.max_per_day, max(0.0, self._next_allowed - time.monotonic()), msg)

    async def send_direct(self, msg: str, labeled: bool = True) -> bool:
        """스트리머가 지시했거나(음성 명령) 정해 둔 규칙(이벤트·참여 유도)으로 보내는 메시지.
        리액션용 랜덤 쿨다운·중복 검사는 없고, 봇 표기·2초 간격·하루 한도는 적용한다."""
        if not self.enabled:
            return False
        if self.direct_daily.value() >= self.DIRECT_MAX_PER_DAY:
            log.warning("%s오늘 직접 전송 한도(%d건)에 도달했습니다", self._tag, self.DIRECT_MAX_PER_DAY)
            return False
        if labeled and not msg.startswith(self.policy.label):
            msg = self.policy.label + msg
        try:
            async with self._lock:
                await self.sender.send(msg[:YT_MAX_LEN])
                self.direct_daily.increment()
                self.stats["direct"] += 1
                await asyncio.sleep(self.DIRECT_GAP)
        except SendFatal as e:
            self.enabled = False
            log.error("채팅 전송 중단: %s", e)
            return False
        except SendError as e:
            log.warning("명령 메시지 전송 실패: %s", e)
            return False
        return True

    async def close(self, wait: float = 10.0) -> None:
        self.enabled = False                # 종료 중에는 새 전송을 받지 않음
        if self._inflight and not self._inflight.done():
            try:
                await asyncio.wait_for(self._inflight, timeout=wait)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        await self.sender.close()
        s = self.stats
        log.info("채팅 %s— 리액션 후보 %d | 리액션 전송 %d | 쿨다운 %d | 한도 %d | 중복 %d | 실패 %d | 명령·이벤트 전송 %d",
                 self._tag, s["offered"], s["sent"], s["cooldown"], s["limit"], s["duplicate"], s["failed"],
                 s["direct"])


# ─────────────────────────────────────────────────────────────────────────────
# CLI: OAuth 최초 로그인 / 1건 전송 테스트
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    p = argparse.ArgumentParser(description="YouTube Live Chat 전송 도구")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="OAuth 로그인 후 token.json 저장 (최초 1회)")
    t = sub.add_parser("test", help="라이브 채팅에 메시지 1건 전송 (AI 표기 포함)")
    t.add_argument("url")
    t.add_argument("text")
    a = p.parse_args()

    secret, token = _env("YT_CLIENT_SECRET", "client_secret.json"), _env("YT_TOKEN_FILE", "token.json")
    if a.cmd == "auth":
        try:
            load_credentials(secret, token, interactive=True)
        except (FileNotFoundError, SendFatal) as e:
            sys.exit(str(e))
        print(f"로그인 완료 → {token} 저장됨 (이 파일은 비밀번호처럼 관리하세요)")
        return

    async def run_test() -> None:
        pub = SafeChatPublisher(YouTubeApiSender(a.url, secret, token))
        if await pub.start():
            pub.offer(a.text)
        await pub.close()

    asyncio.run(run_test())


if __name__ == "__main__":
    main()
