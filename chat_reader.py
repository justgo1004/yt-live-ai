"""
라이브 채팅 읽기 — 공식 API liveChatMessages.list 를 주기적으로 호출 (1회 약 5 단위).

    · 시작 시점 이전 메시지(백로그)는 건너뛴다 → 이미 지난 채팅으로 환영·추첨이 일어나지 않게
    · 봇 계정들이 보낸 메시지는 제외 (봇끼리 반응하는 일 방지)
    · 할당량 사용률 80% 이상이면 읽기 간격 3배, 95% 이상이면 읽기 중단
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from chat_sender import SendError, SendFatal

log = logging.getLogger("live_stt.reader")

READ_COST = 5
COUNTED_TYPES = {"textMessageEvent", "superChatEvent", "superStickerEvent", "memberMilestoneChatEvent"}


@dataclass
class ChatMessage:
    author: str
    channel_id: str
    text: str
    ts: float
    is_owner: bool = False
    is_mod: bool = False
    kind: str = "textMessageEvent"


def parse_items(items: Iterable[dict]) -> list[ChatMessage]:
    out = []
    for it in items:
        sn, au = it.get("snippet") or {}, it.get("authorDetails") or {}
        if sn.get("type") not in COUNTED_TYPES:
            continue
        out.append(ChatMessage(
            author=au.get("displayName", "?"), channel_id=au.get("channelId", ""),
            text=sn.get("displayMessage", "") or "", ts=time.time(),
            is_owner=bool(au.get("isChatOwner")), is_mod=bool(au.get("isChatModerator")),
            kind=sn.get("type", ""),
        ))
    return out


class ChatReader:
    """
    api: 이미 start() 된 chat_sender.YouTubeApiSender (live_chat_id·인증 재사용).
    on_messages: 새 메시지 목록을 받는 동기 콜백.
    """

    def __init__(self, api, on_messages: Callable[[list[ChatMessage]], None],
                 min_interval: float = 10.0, exclude_ids: Optional[set[str]] = None, quota=None):
        self.api = api
        self.on_messages = on_messages
        self.min_interval = max(3.0, min_interval)
        self.exclude_ids = exclude_ids or set()
        self.quota = quota
        self.stats = {"polls": 0, "messages": 0}
        self.status = "starting"        # running / slow / stopped
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="chat-reader")

    async def _run(self) -> None:
        token: Optional[str] = None
        server_ms = 5000
        first = True
        failures = 0
        log.info("채팅 읽기 시작 — %.0f초 간격 (1회 약 %d 단위, 시간당 약 %d 단위)",
                 self.min_interval, READ_COST, int(3600 / self.min_interval * READ_COST))
        while True:
            ratio = self.quota.ratio(self.api.project) if self.quota else 0.0
            if ratio >= 0.95:
                self.status = "stopped"
                log.warning("오늘 유튜브 API 할당량을 95% 이상 써서 채팅 읽기를 멈춥니다 (전송용으로 남겨 둠)")
                return
            slow = ratio >= 0.8
            if slow and self.status != "slow":
                log.warning("할당량 80% 이상 사용 — 채팅 읽기 간격을 3배로 늘립니다")
            self.status = "slow" if slow else "running"
            params = {"liveChatId": self.api.live_chat_id, "part": "snippet,authorDetails", "maxResults": 200}
            if token:
                params["pageToken"] = token
            try:
                r = await self.api._request("GET", "/liveChat/messages", units=READ_COST, params=params)
            except SendFatal as e:
                self.status = "stopped"
                log.error("채팅 읽기 중단: %s", e)
                return
            except SendError as e:
                failures += 1
                log.warning("채팅 읽기 실패 (%s) — 잠시 후 다시 시도", e)
                await asyncio.sleep(min(60, 10 * failures))
                continue
            failures = 0
            data = r.json()
            token = data.get("nextPageToken") or token
            server_ms = int(data.get("pollingIntervalMillis") or server_ms)
            self.stats["polls"] += 1
            msgs = [m for m in parse_items(data.get("items") or []) if m.channel_id not in self.exclude_ids]
            if first:
                first = False
                log.info("채팅 연결됨 — 이전 메시지 %d개는 건너뜁니다", len(msgs))
            elif msgs:
                self.stats["messages"] += len(msgs)
                try:
                    self.on_messages(msgs)
                except Exception as e:
                    log.warning("채팅 처리 오류 (%s: %s)", type(e).__name__, e)
            await asyncio.sleep(max(self.min_interval, server_ms / 1000) * (3 if slow else 1))

    async def aclose(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.status = "stopped"
        log.info("채팅 읽기 — 조회 %d회 | 메시지 %d개", self.stats["polls"], self.stats["messages"])
