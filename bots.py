"""
여러 봇 계정 관리 — 역할별로 다른 봇이 채팅을 보낸다.

    역할                         예시 봇 이름          하는 일
    reaction  소통               소통봇               AI 리액션
    guide     안내               메뉴·방송안내 봇      음성 명령(메뉴판·공지)
    event     이벤트             이벤트봇             키워드 추첨, 퀴즈
    engage    참여 유도          참여봇               첫 채팅 환영, 조용할 때 유도, 종료 시 순위

bots.json 에 저장. 봇마다 OAuth 파일·로그인 토큰·전송 기록은 bots/<id>/ 폴더에 따로 둔다.
같은 OAuth 파일(같은 Google Cloud 프로젝트)을 쓰는 봇들은 유튜브 API 하루 할당량을 함께 쓴다.

각 봇이 보내는 모든 메시지 앞에는 그 봇의 표기(label)가 붙는다.
AI 리액션을 맡은 봇은 표기를 비울 수 없다 (자동 생성 메시지임을 알리기 위해).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

ROLES = {
    "reaction": "소통 (AI 리액션)",
    "guide": "안내 (음성 명령·메뉴)",
    "event": "이벤트 (추첨·퀴즈)",
    "engage": "참여 유도 (환영·순위)",
}


@dataclass
class Bot:
    id: str
    name: str
    roles: list[str] = field(default_factory=list)
    label: str = "🤖 "
    client_secret: str = ""        # BASE 기준 상대 경로
    token: str = ""
    enabled: bool = True
    channel_title: str = ""        # 로그인 후 채워짐 (표시용)
    channel_id: str = ""           # 봇이 보낸 메시지를 채팅 읽기에서 제외할 때 사용

    def cs_path(self, base: Path) -> Path:
        return base / (self.client_secret or f"bots/{self.id}/client_secret.json")

    def token_path(self, base: Path) -> Path:
        return base / (self.token or f"bots/{self.id}/token.json")

    def state_path(self, base: Path) -> Path:
        return base / f"bots/{self.id}/.chat_state.json"

    def ready(self, base: Path) -> bool:
        return self.enabled and self.cs_path(base).exists() and self.token_path(base).exists()


@dataclass
class BotRegistry:
    bots: list[Bot] = field(default_factory=list)
    reader_bot: str = ""           # 채팅 읽기에 쓸 봇 (비우면 로그인된 첫 봇)
    read_interval: float = 10.0    # 채팅 읽기 간격(초) — 1회 약 5 단위
    path: Optional[Path] = None

    # ---------- 저장 ----------
    @classmethod
    def load(cls, path: str | Path, legacy_secret: str = "client_secret.json",
             legacy_token: str = "token.json", legacy_label: str = "🤖AI: ") -> "BotRegistry":
        p = Path(path)
        base = p.parent
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            bots = [Bot(**{k: v for k, v in b.items() if k in Bot.__dataclass_fields__}) for b in d.get("bots", [])]
            return cls(bots, d.get("reader_bot", ""), float(d.get("read_interval", 10) or 10), p)
        # 처음 실행: 기존 단일 봇 설정(client_secret.json / token.json)을 '기본 봇'으로 이전
        reg = cls(path=p)
        if (base / legacy_secret).exists() or (base / legacy_token).exists():
            reg.bots.append(Bot("bot1", "기본 봇", list(ROLES), legacy_label or "🤖AI: ",
                                legacy_secret, legacy_token))
        reg.save()
        return reg

    def save(self) -> None:
        assert self.path
        d = {"bots": [asdict(b) for b in self.bots], "reader_bot": self.reader_bot,
             "read_interval": self.read_interval}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def base(self) -> Path:
        return self.path.parent if self.path else Path(".")

    # ---------- 조회 ----------
    def get(self, bot_id: str) -> Optional[Bot]:
        return next((b for b in self.bots if b.id == bot_id), None)

    def for_role(self, role: str, ready_only: bool = True) -> Optional[Bot]:
        for b in self.bots:
            if b.enabled and role in b.roles and (not ready_only or b.ready(self.base)):
                return b
        return None

    def reader(self) -> Optional[Bot]:
        b = self.get(self.reader_bot)
        if b and b.ready(self.base):
            return b
        return next((b for b in self.bots if b.ready(self.base)), None)

    def bot_channel_ids(self) -> set[str]:
        return {b.channel_id for b in self.bots if b.channel_id}

    def new_id(self) -> str:
        n = 1
        while self.get(f"bot{n}"):
            n += 1
        return f"bot{n}"

    def validate(self) -> list[str]:
        errs = []
        names = [b.name.strip() for b in self.bots]
        if any(not n for n in names):
            errs.append("봇 이름을 입력하세요.")
        if len(set(names)) != len(names):
            errs.append("봇 이름이 겹칩니다.")
        for b in self.bots:
            if "reaction" in b.roles and not b.label.strip():
                errs.append(f"'{b.name}' 은(는) AI 리액션을 맡으므로 표기를 비울 수 없습니다.")
            if any(r not in ROLES for r in b.roles):
                errs.append(f"'{b.name}' 에 알 수 없는 역할이 있습니다.")
            if not re.fullmatch(r"bot\d+", b.id):
                errs.append(f"잘못된 봇 ID: {b.id}")
        for role, title in ROLES.items():
            owners = [b.name for b in self.bots if b.enabled and role in b.roles]
            if len(owners) > 1:
                errs.append(f"'{title}' 역할을 여러 봇({', '.join(owners)})이 맡고 있습니다. 한 봇만 지정하세요.")
        if not 3 <= self.read_interval <= 120:
            errs.append("채팅 읽기 간격은 3~120초 사이여야 합니다.")
        return errs
