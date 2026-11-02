#!/usr/bin/env python3
"""
라이브 AI 리액션 — 데스크톱 앱 (무설치 exe 의 진입점)

    YouTubeLiveAI.exe ─▶ 내 PC 안에서만 동작하는 작은 웹 서버 (127.0.0.1)
                      ─▶ Edge '앱 모드' 창으로 화면 표시 (주소창 없는 일반 프로그램 창)
                      ─▶ 화면의 버튼이 main.Orchestrator 를 같은 이벤트 루프에서 시작/중지

설정·로그인 정보는 exe 와 같은 폴더에 저장된다 (.env, client_secret.json, token.json, logs/).
개발 중에는 `python app.py` 로 같은 화면을 띄울 수 있다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque
from datetime import date
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# 경로 / 실행 환경
# ─────────────────────────────────────────────────────────────────────────────
FROZEN = getattr(sys, "frozen", False)
BASE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES = Path(getattr(sys, "_MEIPASS", BASE))          # exe 안에 묶인 리소스 (ui/)
ENV_FILE = BASE / ".env"
CLIENT_SECRET = BASE / "client_secret.json"
TOKEN = BASE / "token.json"
CHAT_STATE = BASE / ".chat_state.json"
COMMANDS_FILE = BASE / "commands.json"
BOTS_FILE = BASE / "bots.json"
COMMUNITY_FILE = BASE / "community.json"
COMMUNITY_STATE = BASE / ".community_state.json"
QUOTA_FILE = BASE / ".quota.json"
LOG_DIR = BASE / "logs"
APP_ID = "yt-live-ai"
DEFAULT_PORT = int(os.environ.get("LIVEAI_PORT", "8765"))

# --windowed exe 에서는 stdout/stderr 가 None → print/로그가 터지지 않도록 막아 둔다
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
os.chdir(BASE)
os.environ["PATH"] = str(BASE) + os.pathsep + os.environ.get("PATH", "")   # 같은 폴더의 ffmpeg.exe 우선

from aiohttp import WSMsgType, web           # noqa: E402
from pydantic import ValidationError         # noqa: E402

import chat_sender                            # noqa: E402
import commands as voice_cmd                  # noqa: E402
import updater                                # noqa: E402
import bots as bots_mod                       # noqa: E402
import community as community_mod             # noqa: E402
import quota as quota_mod                     # noqa: E402
from dataclasses import asdict                # noqa: E402
from version import __version__               # noqa: E402
import live_stt                               # noqa: E402
import main as core                           # noqa: E402
import reaction                               # noqa: E402

log = logging.getLogger("live_stt.app")

SECRET_FIELDS = ("groq_api_key", "deepgram_api_key", "anthropic_api_key")
FIELD_LABELS = {
    "groq_api_key": "Groq API 키", "anthropic_api_key": "Anthropic API 키", "deepgram_api_key": "Deepgram API 키",
    "chunk_seconds": "청크 길이", "reaction_threshold": "리액션 빈도", "reaction_cooldown": "리액션 간격",
    "chat_cooldown_min": "채팅 최소 간격", "chat_cooldown_max": "채팅 최대 간격",
    "chat_max_per_stream": "방송당 최대 전송", "chat_max_per_day": "하루 최대 전송", "chat_label": "AI 표기",
    "reaction_effort": "리액션 생성 속도", "stt_language": "인식 언어",
}


# ─────────────────────────────────────────────────────────────────────────────
# .env 읽기/쓰기 + 검증
# ─────────────────────────────────────────────────────────────────────────────
def read_env() -> dict[str, str]:
    from dotenv import dotenv_values
    if not ENV_FILE.exists():
        return {}
    return {k.upper(): (v or "") for k, v in dotenv_values(ENV_FILE).items()}


def _quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def write_env(values: dict[str, str]) -> None:
    lines = ["# 라이브 AI 리액션 설정 — 앱의 [설정] 화면에서 자동 저장됩니다.",
             "# 이 파일에는 API 키가 들어 있으니 다른 사람과 공유하지 마세요.", ""]
    known = [n.upper() for n in core.Settings.model_fields]
    for key in known + sorted(k for k in values if k not in known):
        if key in values:
            lines.append(f"{key}={_quote(str(values[key]))}")
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(ENV_FILE)


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _korean_error(err: dict) -> str:
    t, ctx = err.get("type", ""), {k: _num(v) for k, v in (err.get("ctx") or {}).items()}
    if t == "greater_than_equal":
        return f"{ctx.get('ge')} 이상이어야 합니다"
    if t == "greater_than":
        return f"{ctx.get('gt')} 보다 커야 합니다"
    if t == "less_than_equal":
        return f"{ctx.get('le')} 이하여야 합니다"
    if t in ("float_parsing", "int_parsing", "float_type", "int_type", "int_from_float"):
        return "숫자를 입력하세요"
    if t == "string_too_short":
        return "비워 둘 수 없습니다"
    if t == "literal_error":
        return f"다음 중 하나여야 합니다: {ctx.get('expected')}"
    if t == "bool_parsing":
        return "true 또는 false 여야 합니다"
    return str(err.get("msg", "")).removeprefix("Value error, ")


def build_settings(values: dict[str, str]) -> tuple[Optional[core.Settings], dict[str, str]]:
    """.env 값(dict) → Settings. 오류는 {필드: 한국어 메시지}."""
    kwargs = {k.lower(): v for k, v in values.items() if k.lower() in core.Settings.model_fields}
    try:
        return core.Settings(_env_file=None, **kwargs), {}  # type: ignore[call-arg]
    except ValidationError as e:
        errors = {}
        for err in e.errors():
            field = str(err["loc"][0]) if err.get("loc") else "_"
            errors[field] = _korean_error(err)
        return None, errors


def _with_paths(s: core.Settings) -> core.Settings:
    s.chat_state_file, s.yt_client_secret, s.yt_token_file = str(CHAT_STATE), str(CLIENT_SECRET), str(TOKEN)
    s.commands_file = str(COMMANDS_FILE)
    s.bots_file, s.community_file = str(BOTS_FILE), str(COMMUNITY_FILE)
    s.community_state_file, s.quota_file = str(COMMUNITY_STATE), str(QUOTA_FILE)
    return s


# ─────────────────────────────────────────────────────────────────────────────
# 이벤트 허브: 로그 → 화면 (웹소켓)
# ─────────────────────────────────────────────────────────────────────────────
class Hub:
    def __init__(self) -> None:
        self.clients: set[asyncio.Queue] = set()
        self.history: deque[dict] = deque(maxlen=500)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_thread: Optional[int] = None
        self.last_client_seen = time.monotonic()
        self.ever_connected = False

    def publish(self, ev: dict, keep: bool = True) -> None:
        ev.setdefault("ts", time.time())
        if self.loop is None:
            return
        if threading.get_ident() != self.loop_thread:
            self.loop.call_soon_threadsafe(self._publish, ev, keep)
        else:
            self._publish(ev, keep)

    def _publish(self, ev: dict, keep: bool) -> None:
        if keep:
            self.history.append(ev)
        for q in list(self.clients):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass


hub = Hub()


class AppState:
    def __init__(self) -> None:
        self.orch: Optional[core.Orchestrator] = None
        self.task: Optional[asyncio.Task] = None
        self.mode = "console"
        self.url = ""
        self.detail = ""            # connecting / receiving / reconnecting
        self.started_at: Optional[float] = None
        self.counts = {"stt": 0, "reactions": 0}
        self.login_busy = False
        self.quit = asyncio.Event()
        self.update = {"current": __version__, "latest": None, "available": False, "notes": "", "page": "",
                       "status": "idle", "progress": 0.0, "error": None, "can_install": FROZEN and os.name == "nt"}
        self.update_info: Optional[updater.UpdateInfo] = None

    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())


state: AppState  # serve() 에서 생성 (이벤트 루프 안에서 만들어야 함)


_BOT_RE = re.compile(r"📣 \[(.+?)\] (전송됨|\(화면 모드 — 전송 안 함\)|전송 실패|\(화면에만 표시\)) :: (.*)$", re.S)
_CMD_RE = re.compile(r"📢 \[(.+?)\] (\d+)/(\d+) (전송됨|\(화면 모드 — 전송 안 함\)) ← (.*?) :: (.*)$", re.S)


class UiLogHandler(logging.Handler):
    """로그 레코드를 화면용 이벤트로 변환."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            name = record.name
            ev: dict = {"type": "log", "text": msg}
            if name == "live_stt.transcript":
                state.counts["stt"] += 1
                state.detail = "receiving"
                ev.update(kind="stt", pos=live_stt._hms(getattr(record, "stream_ts", 0.0)),
                          provider=getattr(record, "provider", ""), ms=round(getattr(record, "latency_ms", 0)))
            elif name == "live_stt_reaction":
                state.counts["reactions"] += 1
                ev.update(kind="reaction", mood=getattr(record, "label", ""),
                          pos=live_stt._hms(getattr(record, "src_offset", 0.0)))
            elif name == "live_stt.chat_in":
                ev.update(kind="chat", author=getattr(record, "author", ""), owner=bool(getattr(record, "owner", False)),
                          text=msg.split(": ", 1)[-1])
            elif name == "live_stt.main" and msg.startswith("📣"):
                m = _BOT_RE.match(msg)
                if m:
                    ev.update(kind="botmsg", bot=m[1], real=m[2] == "전송됨", note=m[2], text=m[3])
                else:
                    ev["kind"] = "info"
            elif name == "live_stt.command" and msg.startswith("🎯"):
                ev.update(kind="action")
            elif name == "live_stt.command":
                m = _CMD_RE.match(msg)
                if m:
                    ev.update(kind="command", name=m[1], part=f"{m[2]}/{m[3]}", real=m[4] == "전송됨",
                              source=m[5], text=m[6])
                elif msg.startswith("🎤"):
                    ev.update(kind="wake")
                else:
                    ev["kind"] = "warn" if record.levelno >= logging.WARNING else "info"
            elif name.startswith("live_stt.chat") and "[dry-run]" in msg:
                return                                      # 아래 📤 줄과 중복
            elif name.startswith("live_stt.chat") and msg.startswith("📤"):
                text = msg.split("): ", 1)[-1]
                bm = re.match(r"📤 \[(.+?)\]", msg)
                ev.update(kind="sent", text=text, real=state.mode == "youtube-api", bot=bm[1] if bm else "")
            else:
                ev["kind"] = "error" if record.levelno >= logging.ERROR else \
                    "warn" if record.levelno >= logging.WARNING else "info"
                if "스트림 주소를 가져오는" in msg:
                    state.detail = "connecting"
                elif "오디오 수신 시작" in msg:
                    state.detail = "receiving"
                elif "끊겼습니다" in msg:
                    state.detail = "reconnecting"
            hub.publish(ev)
        except Exception:
            pass


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    day = LOG_DIR / f"{date.today().isoformat()}.txt"
    ui = UiLogHandler()

    def file_handler(fmt: logging.Formatter) -> logging.Handler:
        h = logging.FileHandler(day, encoding="utf-8")
        h.setFormatter(fmt)
        return h

    root = logging.getLogger("live_stt")
    root.setLevel(logging.INFO)
    root.addHandler(ui)
    root.addHandler(file_handler(live_stt.SystemFormatter(False)))

    t = logging.getLogger("live_stt.transcript")
    t.propagate = False
    t.setLevel(logging.INFO)
    t.addHandler(ui)
    t.addHandler(file_handler(live_stt.TranscriptFormatter(False, plain_file=True)))

    r = logging.getLogger("live_stt_reaction")       # reaction.setup_reaction_logging 은 이후 건너뜀
    r.propagate = False
    r.setLevel(logging.INFO)
    r.addHandler(ui)
    r.addHandler(file_handler(reaction.ReactionFormatter(False, plain_file=True)))

    app_log = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
    app_log.setLevel(logging.WARNING)
    app_log.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(app_log)


# ─────────────────────────────────────────────────────────────────────────────
# 상태 스냅샷
# ─────────────────────────────────────────────────────────────────────────────
def snapshot() -> dict:
    env = read_env()
    s, errors = build_settings(env)
    bots = bots_snapshot()
    pub = state.orch.publisher if state.orch else None
    today = pub.daily.value() if pub else chat_sender.DailyCounter(str(CHAT_STATE)).value()
    next_in = max(0.0, pub._next_allowed - time.monotonic()) if pub else 0.0
    phase = "idle"
    if state.running and state.orch:
        phase = {"starting": "starting", "stopping": "stopping"}.get(state.orch.phase, state.detail or "starting")
        if state.orch.signals >= 1 and phase != "stopping":
            phase = "stopping"
    return {
        "type": "state",
        "running": state.running, "phase": phase, "mode": state.mode, "url": state.url,
        "elapsed": int(time.monotonic() - state.started_at) if state.started_at and state.running else 0,
        "counts": state.counts,
        "chat": {
            "active": bool(pub and pub.enabled),
            "sent": pub.sent_this_stream if pub else 0,
            "max_stream": s.chat_max_per_stream if s else 20,
            "today": today, "max_day": s.chat_max_per_day if s else 60,
            "next_in": round(next_in),
        },
        "setup": {
            "groq": bool(env.get("GROQ_API_KEY") or env.get("DEEPGRAM_API_KEY")),
            "anthropic": bool(env.get("ANTHROPIC_API_KEY")),
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "token": bots["any_ready"],
            "login_busy": state.login_busy,
        },
        "bots": bots,
        "pubs": [{"bot": p.bot_name, "real": p.sender.name != "console", "enabled": p.enabled,
                  "sent": p.sent_this_stream, "direct": p.stats.get("direct", 0),
                  "next_in": round(max(0.0, p._next_allowed - time.monotonic()))}
                 for p in (state.orch.publishers.values() if state.running and state.orch else [])],
        "community": state.orch.community.snapshot() if state.running and state.orch and state.orch.community else None,
        "reader": (state.orch.reader.status if getattr(state.orch.reader, "status", None) else "running")
        if state.running and state.orch and state.orch.reader else None,
        "settings_errors": errors,
        "update": state.update,
        "commands": {
            "active": bool(state.running and state.orch and state.orch.commands),
            "names": _command_names(),
        },
    }


def _command_names() -> list[str]:
    try:
        cs = voice_cmd.CommandSet.load(COMMANDS_FILE)
        return [c.name for c in cs.commands] if cs.enabled else []
    except Exception:
        return []


async def ticker() -> None:
    while True:
        await asyncio.sleep(1.0)
        if hub.clients:
            hub.publish(snapshot(), keep=False)


async def watchdog() -> None:
    """앱 창이 닫히면(웹소켓 연결이 모두 끊기고 일정 시간 지나면) 방송을 정리하고 종료."""
    while True:
        await asyncio.sleep(5)
        idle = time.monotonic() - hub.last_client_seen
        if not hub.clients and ((hub.ever_connected and idle > 30) or (not hub.ever_connected and idle > 180)):
            log.info("앱 창이 닫혀 종료합니다")
            state.quit.set()
            return


# ─────────────────────────────────────────────────────────────────────────────
# API 핸들러
# ─────────────────────────────────────────────────────────────────────────────
def ok(**kw) -> web.Response:
    return web.json_response({"ok": True, **kw})


def fail(message: str, status: int = 400, **kw) -> web.Response:
    return web.json_response({"ok": False, "error": message, **kw}, status=status)


async def h_index(_req) -> web.StreamResponse:
    return web.FileResponse(RES / "ui" / "index.html", headers={"Cache-Control": "no-store"})


async def h_ping(_req) -> web.Response:
    return web.json_response({"app": APP_ID})


async def h_state(_req) -> web.Response:
    return web.json_response(snapshot())


async def h_get_settings(_req) -> web.Response:
    env = read_env()
    defaults = {n: (f.default if not callable(f.default) else None) for n, f in core.Settings.model_fields.items()}
    values = {}
    for name in core.Settings.model_fields:
        raw = env.get(name.upper())
        if name in SECRET_FIELDS:
            values[name] = {"set": bool(raw), "hint": ("••••" + raw[-4:]) if raw and len(raw) > 8 else ("설정됨" if raw else "")}
        else:
            d = defaults.get(name)
            values[name] = raw if raw is not None else ("" if d is None else
                                                        (str(d).lower() if isinstance(d, bool) else str(d)))
    _, errors = build_settings(env)
    return web.json_response({"values": values, "errors": errors, "labels": FIELD_LABELS})


async def h_save_settings(req) -> web.Response:
    body = await req.json()
    env = read_env()
    for name, val in body.items():
        if name not in core.Settings.model_fields:
            continue
        key = name.upper()
        val = "" if val is None else str(val)
        val = val.replace("\n", " ") if name in ("chat_label", "command_label") else val.strip()  # 표기는 끝 공백 유지
        if name in SECRET_FIELDS:
            if val == "":
                continue                       # 빈칸 = 기존 키 유지
            if val == "__clear__":
                val = ""
        env[key] = val
    s, errors = build_settings(env)
    if errors:
        msgs = [f"{FIELD_LABELS.get(k, k)}: {v}" for k, v in errors.items()]
        return fail("저장하지 못했습니다 — " + " / ".join(msgs), errors=errors)
    write_env(env)
    if state.running:
        return ok(message="저장했습니다. 실행 중인 방송에는 다음 시작부터 적용됩니다.")
    return ok(message="저장했습니다.")


YOUTUBE_URL = re.compile(r"(youtube\.com/(watch\?|live/|shorts/)|youtu\.be/)")


async def h_start(req) -> web.Response:
    if state.running:
        return fail("이미 실행 중입니다.", 409)
    body = await req.json()
    url = (body.get("url") or "").strip()
    mode = body.get("mode") or "console"
    inp = (body.get("input") or "").strip()                     # 개발/시험용 (화면에는 없음)
    s, errors = build_settings(read_env())
    if errors or s is None:
        return fail("설정에 오류가 있습니다. [설정] 화면을 확인하세요.", errors=errors)
    if not s.has_stt_key():
        return fail("Groq API 키가 없습니다. [설정]에서 먼저 입력하세요.")
    if not inp:
        if not url:
            return fail("유튜브 라이브 주소를 입력하세요.")
        if not YOUTUBE_URL.search(url):
            return fail("유튜브 주소가 아닌 것 같습니다. 예: https://www.youtube.com/watch?v=...")
    if not shutil.which("ffmpeg"):
        return fail("ffmpeg.exe 를 찾을 수 없습니다. YouTubeLiveAI.exe 와 같은 폴더에 ffmpeg.exe 가 있어야 합니다.")
    if mode == "youtube-api" and not any(b.ready(BASE) for b in load_registry().bots):
        return fail("실제 전송을 하려면 먼저 [봇 관리]에서 봇 계정으로 로그인하세요.")
    if mode not in ("console", "youtube-api"):
        mode = "console"
    if body.get("reactions") is False:
        s.reactions = False
    if body.get("commands") is False:
        s.commands_enabled = False
    if s.reactions and not s.secret(s.anthropic_api_key):
        s.reactions = False
        hub.publish({"type": "log", "kind": "warn",
                     "text": "Anthropic API 키가 없어 리액션 없이 자막만 실행합니다. [설정]에서 키를 넣으세요."})
    s.chat_sender = mode
    _with_paths(s)
    args = argparse.Namespace(url=url or None, input=inp or None, allow_vod=bool(body.get("allow_vod")),
                              output=None, verbose=False)
    state.orch = core.Orchestrator(s, args, install_signals=False)
    state.mode, state.url, state.detail = mode, url, ""
    state.counts = {"stt": 0, "reactions": 0}
    state.started_at = time.monotonic()
    hub.publish({"type": "log", "kind": "start",
                 "text": f"시작 — {'채팅 실제 전송' if mode == 'youtube-api' else '화면에만 표시 (채팅 전송 안 함)'}"})

    async def runner() -> None:
        try:
            code = await state.orch.run()
            hub.publish({"type": "log", "kind": "end", "text": {0: "방송이 끝나 종료했습니다.", 130: "중지했습니다."}
                         .get(code, "오류로 종료했습니다. 위의 빨간 메시지를 확인하세요.")})
        except Exception as e:  # 예기치 못한 오류도 화면에 표시
            logging.getLogger().exception("orchestrator crashed")
            hub.publish({"type": "log", "kind": "error", "text": f"예기치 못한 오류: {type(e).__name__}: {e}"})
        finally:
            hub.publish(snapshot(), keep=False)

    state.task = asyncio.create_task(runner(), name="orchestrator")
    return ok()


async def h_stop(_req) -> web.Response:
    if not state.running or not state.orch:
        return ok(message="실행 중이 아닙니다.")
    state.orch.request_stop()
    return ok(force=state.orch.signals >= 2)


# ─────────────────────────────────────────────────────────────────────────────
# 봇 관리 (여러 유튜브 계정)
# ─────────────────────────────────────────────────────────────────────────────
def load_registry() -> bots_mod.BotRegistry:
    env = read_env()
    return bots_mod.BotRegistry.load(BOTS_FILE, "client_secret.json", "token.json",
                                     env.get("CHAT_LABEL", "🤖AI: ") or "🤖AI: ")


def _bot_view(reg: bots_mod.BotRegistry, b: bots_mod.Bot) -> dict:
    cs = b.cs_path(BASE)
    proj = quota_mod.project_key(cs) if cs.exists() else ""
    return {**asdict(b), "has_secret": cs.exists(), "has_token": b.token_path(BASE).exists(),
            "ready": b.ready(BASE), "project": proj[-6:] if proj else "",
            "quota_used": quota_mod.meter(QUOTA_FILE).get(proj) if proj else 0}


def bots_snapshot() -> dict:
    try:
        reg = load_registry()
    except (OSError, ValueError) as e:
        return {"list": [], "error": str(e)}
    views = [_bot_view(reg, b) for b in reg.bots]
    projects: dict[str, dict] = {}
    for v in views:
        if v["project"]:
            p = projects.setdefault(v["project"], {"used": v["quota_used"], "bots": []})
            p["bots"].append(v["name"])
    rb = reg.reader()
    return {"list": views, "reader_bot": reg.reader_bot, "read_interval": reg.read_interval,
            "reader_name": rb.name if rb else None, "projects": projects,
            "limit": quota_mod.DAILY_LIMIT, "any_ready": any(v["ready"] for v in views),
            "login_busy": state.login_busy}


def _find_bot(req) -> tuple[bots_mod.BotRegistry, Optional[bots_mod.Bot]]:
    reg = load_registry()
    return reg, reg.get(req.match_info["bot_id"])


async def h_bots(_req) -> web.Response:
    return web.json_response({"ok": True, **bots_snapshot(), "roles": bots_mod.ROLES})


async def h_bots_save(req) -> web.Response:
    """이름·역할·표기·사용 여부·채팅 읽기 설정 저장 (OAuth 파일·로그인은 각 버튼으로 따로)."""
    if state.running:
        return fail("방송 처리 중에는 봇 설정을 바꿀 수 없습니다. 먼저 중지하세요.")
    body = await req.json()
    reg = load_registry()
    by_id = {b["id"]: b for b in body.get("bots", [])}
    for b in reg.bots:
        d = by_id.get(b.id)
        if d:
            b.name = str(d.get("name", b.name)).strip()
            b.roles = [r for r in d.get("roles", []) if r in bots_mod.ROLES]
            b.label = str(d.get("label", b.label)).replace("\n", " ")
            b.enabled = bool(d.get("enabled", True))
    reg.reader_bot = str(body.get("reader_bot", reg.reader_bot) or "")
    try:
        reg.read_interval = float(body.get("read_interval", reg.read_interval))
    except (TypeError, ValueError):
        return fail("채팅 읽기 간격은 숫자로 입력하세요.")
    errs = reg.validate()
    if errs:
        return fail(" / ".join(errs))
    reg.save()
    return ok(message="봇 설정을 저장했습니다.")


async def h_bot_add(_req) -> web.Response:
    reg = load_registry()
    if len(reg.bots) >= 6:
        return fail("봇은 최대 6개까지 만들 수 있습니다.")
    bid = reg.new_id()
    used = {r for b in reg.bots for r in b.roles}
    free = [r for r in bots_mod.ROLES if r not in used][:1]
    reg.bots.append(bots_mod.Bot(bid, f"새 봇 {bid[3:]}", free, "🤖 "))
    (BASE / "bots" / bid).mkdir(parents=True, exist_ok=True)
    reg.save()
    return ok(id=bid)


async def h_bot_delete(req) -> web.Response:
    if state.running:
        return fail("방송 처리 중에는 봇을 삭제할 수 없습니다.")
    reg, b = _find_bot(req)
    if not b:
        return fail("없는 봇입니다.", 404)
    if b.token_path(BASE).exists() and b.token_path(BASE).parent.name == b.id:
        b.token_path(BASE).unlink(missing_ok=True)            # 로그인 정보만 지움 (OAuth 파일은 남김)
    reg.bots.remove(b)
    if reg.reader_bot == b.id:
        reg.reader_bot = ""
    reg.save()
    return ok(message=f"'{b.name}' 을(를) 삭제했습니다.")


def _validate_secret(content: str) -> tuple[Optional[dict], Optional[str]]:
    try:
        data = json.loads(content or "")
    except json.JSONDecodeError:
        return None, "JSON 파일이 아닙니다. Google Cloud 에서 받은 파일을 그대로 올려 주세요."
    if "web" in data and "installed" not in data:
        return None, "'웹 애플리케이션' 유형의 파일입니다. OAuth 클라이언트를 '데스크톱 앱' 유형으로 다시 만들어 주세요."
    if "installed" not in data or "client_id" not in data.get("installed", {}):
        return None, "OAuth 클라이언트 파일이 아닙니다. '사용자 인증 정보 → OAuth 클라이언트 ID → 데스크톱 앱'의 JSON 을 올려 주세요."
    return data, None


async def h_bot_secret(req) -> web.Response:
    reg, b = _find_bot(req)
    if not b:
        return fail("없는 봇입니다.", 404)
    body = await req.json()
    if body.get("copy_from"):                                 # 다른 봇과 같은 프로젝트(할당량 공유)
        src = reg.get(body["copy_from"])
        if not src or not src.cs_path(BASE).exists():
            return fail("복사할 OAuth 파일이 없습니다.")
        content = src.cs_path(BASE).read_text(encoding="utf-8")
    else:
        content = body.get("content") or ""
    data, err = _validate_secret(content)
    if err:
        return fail(err)
    path = BASE / "bots" / b.id / "client_secret.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    old_project = quota_mod.project_key(b.cs_path(BASE)) if b.cs_path(BASE).exists() else None
    path.write_text(json.dumps(data), encoding="utf-8")
    b.client_secret = f"bots/{b.id}/client_secret.json"
    if old_project and old_project != quota_mod.project_key(path):
        b.token_path(BASE).unlink(missing_ok=True)            # 프로젝트가 바뀌면 다시 로그인 필요
        b.channel_title = b.channel_id = ""
    reg.save()
    shared = [x.name for x in reg.bots if x is not b and x.cs_path(BASE).exists()
              and quota_mod.project_key(x.cs_path(BASE)) == quota_mod.project_key(path)]
    note = f" ({', '.join(shared)} 와(과) 같은 프로젝트 — 하루 할당량을 함께 씁니다)" if shared else ""
    return ok(message="OAuth 파일을 저장했습니다. 이제 이 봇으로 구글 로그인을 하세요." + note)


async def _refresh_bot_channel(bot_id: str) -> Optional[str]:
    reg = load_registry()
    b = reg.get(bot_id)
    if not b or not b.ready(BASE):
        return "로그인되어 있지 않습니다."
    try:
        title, cid = await asyncio.to_thread(chat_sender.fetch_channel_info, str(b.cs_path(BASE)),
                                             str(b.token_path(BASE)), quota_mod.meter(QUOTA_FILE))
    except Exception as e:
        return str(e)
    reg = load_registry()                                      # 그 사이 바뀌었을 수 있어 다시 읽음
    b = reg.get(bot_id)
    if b:
        b.channel_title, b.channel_id = title, cid
        reg.save()
    return None


async def h_bot_login(req) -> web.Response:
    reg, b = _find_bot(req)
    if not b:
        return fail("없는 봇입니다.", 404)
    if not b.cs_path(BASE).exists():
        return fail("먼저 이 봇의 OAuth 파일을 올려 주세요.")
    if state.login_busy:
        return fail("다른 로그인 창이 열려 있습니다. 브라우저에서 로그인을 마쳐 주세요.", 409)
    state.login_busy = True
    tok = BASE / "bots" / b.id / "token.json"
    try:
        tok.parent.mkdir(parents=True, exist_ok=True)
        tok.unlink(missing_ok=True)                            # 다른 계정으로 다시 로그인할 수 있게
        await asyncio.to_thread(chat_sender.load_credentials, str(b.cs_path(BASE)), str(tok), True)
    except Exception as e:
        return fail(f"로그인에 실패했습니다: {e}")
    finally:
        state.login_busy = False
    reg = load_registry()
    b = reg.get(b.id)
    b.token = f"bots/{b.id}/token.json"
    reg.save()
    err = await _refresh_bot_channel(b.id)
    if err:
        return fail(f"로그인은 됐지만 채널 정보를 가져오지 못했습니다: {err}")
    b = load_registry().get(b.id)
    others = [x.name for x in load_registry().bots if x.id != b.id and x.channel_id and x.channel_id == b.channel_id]
    if others:
        return ok(channel=b.channel_title, message=f"주의: {', '.join(others)} 와(과) 같은 유튜브 계정입니다. "
                                                   "역할별로 다른 이름을 보이려면 봇마다 다른 계정으로 로그인하세요.")
    return ok(channel=b.channel_title)


async def h_bot_logout(req) -> web.Response:
    if state.running and state.mode == "youtube-api":
        return fail("실제 전송 중에는 로그아웃할 수 없습니다. 먼저 중지하세요.")
    reg, b = _find_bot(req)
    if not b:
        return fail("없는 봇입니다.", 404)
    b.token_path(BASE).unlink(missing_ok=True)
    b.channel_title = b.channel_id = ""
    reg.save()
    return ok(message=f"'{b.name}' 로그아웃했습니다.")


async def h_bot_test(req) -> web.Response:
    reg, b = _find_bot(req)
    if not b:
        return fail("없는 봇입니다.", 404)
    body = await req.json()
    url, text = (body.get("url") or "").strip(), (body.get("text") or "").strip() or "테스트 메시지입니다"
    if not b.ready(BASE):
        return fail("먼저 이 봇으로 구글 로그인을 하세요.")
    if not YOUTUBE_URL.search(url):
        return fail("유튜브 라이브 주소를 입력하세요.")
    s, _ = build_settings(read_env())
    if not s:
        return fail("설정에 오류가 있습니다. [설정] 화면을 확인하세요.")
    daily = chat_sender.DailyCounter(str(b.state_path(BASE)) + ".direct")
    if daily.value() >= chat_sender.SafeChatPublisher.DIRECT_MAX_PER_DAY:
        return fail("이 봇의 오늘 전송 한도에 도달했습니다.")
    b.state_path(BASE).parent.mkdir(parents=True, exist_ok=True)
    sender = chat_sender.YouTubeApiSender(url, str(b.cs_path(BASE)), str(b.token_path(BASE)),
                                          quota=quota_mod.meter(QUOTA_FILE))
    msg = (b.label + text)[: chat_sender.YT_MAX_LEN]
    try:
        await sender.start()
        await sender.send(msg)
    except chat_sender.SendError as e:
        return fail(f"전송 실패: {e}")
    finally:
        await sender.close()
    daily.increment()
    hub.publish({"type": "log", "kind": "botmsg", "bot": b.name, "text": msg, "real": True})
    return ok(message=f"[{b.name}] 전송했습니다: {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# 이벤트 · 참여 유도
# ─────────────────────────────────────────────────────────────────────────────
async def h_get_community(_req) -> web.Response:
    try:
        cfg = community_mod.CommunityConfig.load(COMMUNITY_FILE)
    except (OSError, ValueError) as e:
        return fail(f"community.json 을 읽지 못했습니다: {e}")
    return web.json_response({"ok": True, "config": cfg.to_dict()})


async def h_save_community(req) -> web.Response:
    body = await req.json()
    try:
        cfg = community_mod.CommunityConfig.from_dict(body)
    except (TypeError, ValueError) as e:
        return fail(f"입력값을 확인하세요: {e}")
    errs = cfg.validate()
    if errs:
        return fail(" / ".join(errs))
    cfg.save(COMMUNITY_FILE)
    if state.running and state.orch and state.orch.community:
        state.orch.community.cfg = cfg                         # 진행 중인 방송에도 바로 적용
        return ok(message="저장했습니다. 진행 중인 방송에도 바로 적용됩니다.")
    return ok(message="저장했습니다.")


async def h_community_action(req) -> web.Response:
    hubc = state.orch.community if state.running and state.orch else None
    if not hubc:
        return fail("방송을 시작한 뒤에 사용할 수 있습니다.")
    action = req.match_info["action"]
    fn = {"raffle_start": hubc.raffle_start, "raffle_end": hubc.raffle_end,
          "quiz": hubc.quiz_start, "ranking": hubc.ranking}.get(action)
    if not fn:
        return fail("알 수 없는 동작입니다.", 404)
    result = fn()
    logging.getLogger("live_stt.command").info("🎯 [%s] %s ← 버튼", commands_label(action), result)
    return ok(message=result)


def commands_label(action: str) -> str:
    return voice_cmd.ACTIONS.get(action, action)


async def h_preview(req) -> web.Response:
    body = await req.json()
    text = (body.get("text") or "").strip()
    if not text:
        return fail("스트리머가 했을 법한 말을 입력하세요.")
    s, errors = build_settings(read_env())
    if not s:
        return fail("설정에 오류가 있습니다. [설정] 화면을 확인하세요.")
    det = reaction.TriggerDetector(threshold=s.reaction_threshold)
    u = reaction.Utterance(0.0, text)
    trig = det.score(u)
    result = {"mood": trig.label if trig else "일반", "score": trig.score if trig else 0.0,
              "threshold": s.reaction_threshold, "reasons": trig.reasons if trig else [],
              "would_trigger": bool(trig and trig.score >= s.reaction_threshold), "reaction": None}
    key = s.secret(s.anthropic_api_key)
    if not key:
        return fail("Anthropic API 키가 없습니다. [설정]에서 먼저 입력하세요.", result=result)
    persona = reaction.build_persona(s.persona_streamer_name, s.persona_channel_topic,
                                     s.persona_viewer_style, s.persona_extra)
    gen = reaction.ReactionGenerator(s.reaction_model, s.reaction_effort, s.reaction_timeout, persona, key)
    mem = reaction.ConversationMemory(s.reaction_memory_size)
    for i, line in enumerate(body.get("context") or []):
        if str(line).strip():
            mem.add(reaction.Utterance(float(i), str(line).strip()))
    mem.add(u)
    t = trig or reaction.Trigger("일반", 0.0, ["특별한 신호 없음"])
    t0 = time.perf_counter()
    try:
        result["reaction"] = await gen.generate(mem, t, [])
    except reaction.ReactionFatal as e:
        return fail(str(e), result=result)
    finally:
        await gen.aclose()
    result["ms"] = round((time.perf_counter() - t0) * 1000)
    if not result["reaction"]:
        result["note"] = "이번에는 리액션이 만들어지지 않았습니다 (필터링 또는 일시 오류). 다시 시도해 보세요."
    return ok(result=result)


async def h_get_commands(_req) -> web.Response:
    try:
        cs = voice_cmd.CommandSet.load(COMMANDS_FILE)
    except (OSError, ValueError) as e:
        return fail(f"commands.json 을 읽지 못했습니다: {e}")
    return web.json_response({"ok": True, **cs.to_dict(), "max_len": voice_cmd.YT_MAX_LEN})


async def h_save_commands(req) -> web.Response:
    body = await req.json()
    cs = voice_cmd.CommandSet.from_dict(body)
    dropped = len(body.get("commands") or []) - len(cs.commands)
    errs = cs.validate()
    if errs:
        return fail(" / ".join(errs))
    cs.save(COMMANDS_FILE)
    note = f" (이름이나 내용이 빈 명령 {dropped}개는 제외했습니다)" if dropped > 0 else ""
    if state.running:
        return ok(message="저장했습니다. 실행 중인 방송에는 다음 시작부터 적용됩니다." + note)
    return ok(message="저장했습니다." + note)


async def h_test_command(req) -> web.Response:
    body = await req.json()
    cs = voice_cmd.CommandSet.from_dict(body.get("config") or {}) if body.get("config") \
        else voice_cmd.CommandSet.load(COMMANDS_FILE)
    s, _ = build_settings(read_env())
    res = voice_cmd.CommandMatcher(cs).test(body.get("text") or "")
    if res.get("command"):
        cmd = next(c for c in cs.commands if c.name == res["command"])
        res["parts"] = cmd.parts(s.command_label if s else "")
    return ok(result=res)


async def h_send_command(req) -> web.Response:
    body = await req.json()
    if not (state.running and state.orch and state.orch.commands):
        return fail("방송을 시작한 뒤에 사용할 수 있습니다.")
    msg = state.orch.commands.trigger(str(body.get("name", "")), source="버튼")
    return ok() if msg == "ok" else fail(msg)


async def check_update(quiet: bool = True) -> None:
    u = state.update
    if u["status"] in ("downloading", "restarting"):
        return
    u.update(status="checking", error=None)
    try:
        info = await asyncio.to_thread(updater.check)
    except updater.UpdateError as e:
        u.update(status="error" if not quiet else "idle", error=None if quiet else str(e))
        return
    state.update_info = info
    u.update(status="idle", latest=info.version if info else None, available=bool(info and info.newer),
             notes=info.notes if info else "", page=info.page_url if info else "")
    if info and info.newer:
        log.info("새 버전 v%s 이(가) 있습니다 (현재 v%s)", info.version, __version__)


async def update_loop() -> None:
    await asyncio.sleep(3)
    while True:
        await check_update(quiet=True)
        await asyncio.sleep(6 * 3600)


async def h_update_check(_req) -> web.Response:
    await check_update(quiet=False)
    u = state.update
    if u["error"]:
        return fail(u["error"])
    if u["available"]:
        return ok(message=f"새 버전 v{u['latest']} 이(가) 있습니다.")
    return ok(message=f"최신 버전입니다 (v{__version__}).")


async def h_update_install(_req) -> web.Response:
    u, info = state.update, state.update_info
    if not u["can_install"]:
        return fail("자동 업데이트는 윈도우용 YouTubeLiveAI.exe 에서만 동작합니다. (개발 모드에서는 git pull)")
    if state.running:
        return fail("방송 처리 중에는 업데이트할 수 없습니다. 먼저 중지하세요.")
    if not info or not info.newer:
        return fail("설치할 새 버전이 없습니다.")
    if u["status"] in ("downloading", "restarting"):
        return fail("이미 업데이트 중입니다.", 409)
    u.update(status="downloading", progress=0.0, error=None)
    loop = asyncio.get_running_loop()

    def prog(p: float) -> None:
        loop.call_soon_threadsafe(u.__setitem__, "progress", round(p, 3))
    try:
        new_exe = await asyncio.to_thread(updater.download, info, BASE / "update", prog)
        u.update(status="restarting", progress=1.0)
        hub.publish(snapshot(), keep=False)
        await asyncio.sleep(0.5)
        updater.apply(new_exe, BASE)
    except (updater.UpdateError, OSError) as e:
        u.update(status="error", error=str(e))
        return fail(f"업데이트 실패: {e}")
    log.info("v%s 로 업데이트합니다 — 잠시 후 자동으로 다시 실행됩니다", info.version)
    loop.call_later(0.8, state.quit.set)
    return ok(message="업데이트를 적용합니다. 잠시 후 자동으로 다시 실행됩니다.")


async def h_quit(_req) -> web.Response:
    state.quit.set()
    return ok(message="종료합니다.")


async def h_open_folder(_req) -> web.Response:
    target = str(BASE)
    try:
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception as e:
        return fail(f"폴더를 열 수 없습니다: {e}")
    return ok()


async def h_ws(req) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    hub.clients.add(q)
    hub.ever_connected = True
    await ws.send_json({"type": "history", "events": list(hub.history)})
    await ws.send_json(snapshot())

    async def writer() -> None:
        while True:
            await ws.send_json(await q.get())

    wt = asyncio.create_task(writer())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        wt.cancel()
        hub.clients.discard(q)
        hub.last_client_seen = time.monotonic()
    return ws


# ─────────────────────────────────────────────────────────────────────────────
# 창 열기 / 서버 실행
# ─────────────────────────────────────────────────────────────────────────────
def open_window(url: str) -> None:
    if os.environ.get("LIVEAI_NO_BROWSER") == "1":
        return
    if os.name == "nt":
        candidates = [
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
            r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        ]
        for c in candidates:
            p = os.path.expandvars(c)
            if os.path.exists(p):
                subprocess.Popen([p, f"--app={url}", "--window-size=1280,860"])
                return
    webbrowser.open(url)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _already_running(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return json.loads(r.read()).get("app") == APP_ID
    except Exception:
        return False


async def serve() -> None:
    global state
    hub.loop, hub.loop_thread = asyncio.get_running_loop(), threading.get_ident()
    state = AppState()
    setup_logging()

    port = DEFAULT_PORT
    if not _port_free(port):
        if _already_running(port):                  # 이미 켜져 있으면 창만 다시 연다
            open_window(f"http://127.0.0.1:{port}/")
            return
        with socket.socket() as s:                  # 다른 프로그램이 쓰는 포트 → 빈 포트 사용
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.add_routes([
        web.get("/", h_index), web.get("/api/ping", h_ping), web.get("/api/state", h_state),
        web.get("/api/settings", h_get_settings), web.post("/api/settings", h_save_settings),
        web.post("/api/start", h_start), web.post("/api/stop", h_stop),
        web.get("/api/bots", h_bots), web.post("/api/bots", h_bots_save), web.post("/api/bots/add", h_bot_add),
        web.post("/api/bots/{bot_id}/delete", h_bot_delete), web.post("/api/bots/{bot_id}/secret", h_bot_secret),
        web.post("/api/bots/{bot_id}/login", h_bot_login), web.post("/api/bots/{bot_id}/logout", h_bot_logout),
        web.post("/api/bots/{bot_id}/test", h_bot_test),
        web.get("/api/community", h_get_community), web.post("/api/community", h_save_community),
        web.post("/api/community/{action}", h_community_action),
        web.post("/api/preview", h_preview), web.post("/api/quit", h_quit),
        web.post("/api/open-folder", h_open_folder), web.get("/ws", h_ws),
        web.post("/api/update/check", h_update_check), web.post("/api/update/install", h_update_install),
        web.get("/api/commands", h_get_commands), web.post("/api/commands", h_save_commands),
        web.post("/api/commands/test", h_test_command), web.post("/api/commands/send", h_send_command),
    ])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()     # 내 PC 에서만 접속 가능
    url = f"http://127.0.0.1:{port}/"
    log.info("앱 시작 v%s — %s (데이터 폴더: %s)", __version__, url, BASE)
    bg = [asyncio.create_task(ticker()), asyncio.create_task(watchdog()), asyncio.create_task(update_loop())]
    try:                                                       # 채널 이름이 비어 있는 로그인된 봇 → 채워 넣기
        for b in load_registry().bots:
            if b.ready(BASE) and not b.channel_title:
                bg.append(asyncio.create_task(_refresh_bot_channel(b.id)))
    except (OSError, ValueError) as e:
        log.warning("bots.json 을 읽지 못했습니다: %s", e)
    if "--after-update" in sys.argv:
        # 업데이트 직후: 기존 창이 자동으로 다시 연결된다. 15초 안에 연결이 없으면 새 창을 연다.
        async def fallback_window() -> None:
            await asyncio.sleep(15)
            if not hub.ever_connected:
                open_window(url)
        bg.append(asyncio.create_task(fallback_window()))
        log.info("v%s 로 업데이트되었습니다", __version__)
    else:
        open_window(url)

    try:
        await state.quit.wait()
    finally:
        if state.running and state.orch:          # 방송 중이면 정상 종료 절차 → 최대 20초
            state.orch.request_stop()
            try:
                await asyncio.wait_for(asyncio.shield(state.task), timeout=20)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                state.orch.request_stop()           # 강제
                await asyncio.sleep(1)
        for t in bg:
            t.cancel()
        await runner.cleanup()


def _fatal(msg: str) -> None:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        (LOG_DIR / "app.log").open("a", encoding="utf-8").write(f"\n[{time.ctime()}]\n{msg}\n")
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg[-1500:], "라이브 AI 리액션 — 오류", 0x10)
            return
        except Exception:
            pass
    print(msg, file=sys.stderr)


def run() -> None:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except Exception:
        _fatal("프로그램을 시작하지 못했습니다.\n\n" + traceback.format_exc())


if __name__ == "__main__":
    run()
