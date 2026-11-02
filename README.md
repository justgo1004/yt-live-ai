# YouTube Live AI 리액션

유튜브 라이브 방송의 음성을 실시간으로 자막으로 바꾸고, 반응할 만한 순간에 Claude가 시청자 리액션을 만들어 (선택 시) 라이브 채팅에 **AI 표기와 함께** 올립니다.

```
                         main.py (오케스트레이터, asyncio 이벤트 루프 1개)
 ┌──────────────────────────────────────────────────────────────────────────────────┐
 │ [Phase 1] live_stt.py                                                            │
 │   yt-dlp → ffmpeg(16kHz) → 3~5초 청크 → Groq Whisper ⇄ Deepgram → 자막 출력        │
 │        │ on_transcript(위치, 텍스트, 음량)                                         │
 │        ▼                                                                         │
 │ [Phase 2] reaction.py                                                            │
 │   최근 10개 발언(deque) → 트리거 판정(질문/신남/당황/진지/후원) → Claude 리액션       │
 │        │ on_reaction(텍스트)                                                      │
 │        ▼                                                                         │
 │ [Phase 3] chat_sender.py                                                         │
 │   쿨다운 45~90초 · 방송당/하루 한도 · AI 표기 → console(dry-run) / YouTube API       │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

## 🖥 앱으로 쓰기 (추천)

명령어 없이 화면에서 버튼으로 모두 할 수 있는 **무설치 실행 파일**입니다.

1. **`build_exe.bat`을 더블클릭**합니다 (처음 한 번, 5~10분).
   - exe를 만들고 ffmpeg.exe를 자동으로 함께 넣습니다.
   - 지금 쓰는 설정(API 키, 유튜브 로그인)도 옮겨 줍니다.
2. 열린 `release\YouTubeLiveAI` 폴더에서 **`YouTubeLiveAI.exe`를 더블클릭**하면 앱 창이 뜹니다.
3. 이 폴더(`YouTubeLiveAI.exe` + `ffmpeg.exe`)는 Python이 없는 다른 PC에 복사해도 그대로 실행됩니다.

앱 화면 구성:

| 메뉴 | 할 수 있는 일 |
|---|---|
| ▶ 방송 | 주소 붙여넣기 → 모드 선택(화면에만 표시 / 채팅에 실제 전송) → 시작·중지. 자막·리액션·전송 기록을 실시간으로 보여 주고, 전송 횟수와 다음 전송 가능 시간도 표시 |
| 🎤 음성 명령 | 호출어와 명령 목록(인식할 말 → 채팅에 올릴 내용)을 편집하고, "말해 보기"로 인식 여부를 바로 확인 |
| 💬 리액션 미리보기 | 문장을 넣으면 감지된 분위기, 반응 점수, 실제 리액션을 바로 확인 |
| ⚙ 설정 | API 키, 페르소나, 리액션 빈도(적게/보통/자주), 전송 안전장치. 저장할 때 값을 검사 |
| 🔗 유튜브 연결 | Google Cloud 안내 링크 → JSON 파일 끌어다 놓기 → 구글 로그인 → 테스트 전송 |

- 설정·로그인 정보·로그(`logs\날짜.txt`)는 exe와 같은 폴더에 저장됩니다.
- 앱 창을 닫으면 30초 뒤 방송 처리를 정리하고 프로그램이 종료됩니다.
- Windows가 "PC 보호" 경고를 띄우면 **추가 정보 → 실행**을 누르세요. 서명되지 않은 exe라서 나오는 정상적인 화면입니다.
- 백신이 exe를 의심하면 `release` 폴더를 예외로 등록하세요.
- 개발 중에는 `python app.py`로 같은 화면을 띄울 수 있습니다.

## 🔄 자동 업데이트

**처음 설치**: [Releases](https://github.com/justgo1004/yt-live-ai/releases/latest)에서 `YouTubeLiveAI_full.zip`을 내려받아 압축을 풀고 `YouTubeLiveAI.exe`를 실행합니다. exe와 ffmpeg가 함께 들어 있습니다.

**업데이트**: 새 버전이 나오면 앱 상단에 알림이 뜹니다. **지금 업데이트**를 누르면 다음 순서로 진행됩니다. `.env`, `token.json`, `commands.json` 같은 설정 파일은 그대로 유지됩니다.

1. 새 exe를 내려받고 SHA-256으로 검증합니다.
2. 프로그램이 잠시 꺼집니다.
3. exe를 교체한 뒤 자동으로 다시 실행되고, 기존 창이 다시 연결됩니다.

- 방송 처리 중에는 업데이트할 수 없습니다 (중지 후 가능).
- 교체에 실패하면 이전 버전으로 되돌립니다. 이전 exe는 `update\YouTubeLiveAI.old.exe`에 한 개 보관합니다.
- 앱은 시작할 때와 6시간마다 새 버전을 확인합니다. 왼쪽 아래 **업데이트 확인**으로 바로 확인할 수도 있습니다.

### 새 버전 내기 (개발자용)

```bash
# 1) version.py 와 RELEASE_NOTES.md 수정 (변경 내용은 앱의 업데이트 알림에 그대로 표시됨) → main 에 push
# 2) GitHub 에서 릴리스 발행 (태그 v1.2.0) → GitHub Actions 가 윈도우에서 exe 를 빌드·시험해 그 릴리스에 올림 (약 10분)
gh release create v1.2.0 --target main --title v1.2.0 --notes-file RELEASE_NOTES.md
```

빌드 과정은 `.github/workflows/release.yml`에 있습니다. 릴리스 태그 이름이 그대로 앱 버전(`version.py`)이 됩니다.

## 🎤 음성 명령 (나이트봇 명령어를 목소리로)

방송 중에 **"히든아, 메뉴판 올려줘"**라고 말하면, 미리 써 둔 메뉴판을 채팅에 올립니다.

- **호출어 + 명령어**: 호출어(기본 `히든아`) 뒤에 명령어(예: `메뉴판`, `메뉴`)가 들리면 실행합니다.
  - "히든아…(잠시 후) 메뉴판 올려줘"처럼 나눠 말해도 8초 안이면 됩니다.
  - 음성 인식이 "이든아", "히든야"로 잘못 들을 수 있어 기본 호출어에 함께 넣어 두었습니다. 띄어쓰기("히든 아")는 무시합니다.
  - 인식 모델에 호출어와 명령어를 미리 알려 줘서 정확도를 높입니다.
- **전송**
  - 리액션용 45~90초 대기와 상관없이 **바로** 올라갑니다.
  - 같은 명령은 30초 안에 다시 실행되지 않습니다 (명령별로 조정 가능).
  - 190자가 넘으면 자동으로 나눠 1.5초 간격으로 올립니다. 여러 줄은 ` / `로 이어서 올라갑니다.
  - 하루 최대 100건이며, 리액션 한도와는 따로 셉니다.
- **명령 문장에는 AI 리액션이 붙지 않습니다.**
- **빠른 전송 버튼**: 방송 화면에 명령마다 버튼이 생겨, 말하지 않고 클릭으로도 올릴 수 있습니다.
- **모드**: "화면에만 표시" 모드에서는 올라갈 메시지만 보여 주고, "채팅에 실제 전송" 모드에서 실제로 올립니다.
- 명령 앞 표기는 [설정 → 채팅 전송 안전장치]의 "음성 명령 메시지 앞 표기"로 정합니다 (선택).
- 명령 목록은 `commands.json`에 저장됩니다.

## 프로젝트 구조

```
yt-live-stt/
├── app.py               # ★ 데스크톱 앱 진입점 (exe): 화면 서버 + 버튼으로 시작/중지·설정·유튜브 로그인
├── ui/index.html        # 앱 화면 (인터넷 없이 동작)
├── build_exe.bat        # 무설치 exe 만들기 (ffmpeg 포함)
├── main.py              # 명령어 실행 진입점 + 오케스트레이터 (설정 로드, 세 모듈 연결, 순서대로 종료)
├── live_stt.py          # Phase 1: 오디오 수집(yt-dlp/ffmpeg) + STT(Groq/Deepgram 폴백) + 자막 출력
├── reaction.py          # Phase 2: 대화 버퍼, 트리거 감지, Claude 리액션 생성, 페르소나
├── updater.py           # 자동 업데이트 (GitHub 릴리스 확인·다운로드·검증·교체)
├── version.py           # 앱 버전
├── .github/workflows/release.yml  # 태그 → 윈도우 exe 자동 빌드·릴리스
├── commands.py          # 음성 명령: 호출어·명령어 인식, 메시지 분할, 전송
├── chat_sender.py       # Phase 3: 전송 인터페이스(ChatSender), 가드레일, YouTube Live Chat API
├── setup.bat            # Windows 원클릭 설치 (더블클릭)
├── run.bat              # Windows 실행 메뉴 (데모 / 라이브 / 점검)
├── requirements.txt     # 파이썬 의존성
├── .env.example         # 설정 템플릿 → .env 로 복사해서 사용
├── .gitignore           # .env, token.json, client_secret*.json, .chat_state.json 제외
└── README.md

실행 중 생성되는 파일 (git 제외)
├── .env                 # API 키와 설정
├── client_secret.json   # Google Cloud OAuth 클라이언트 (youtube-api 전송 시)
├── token.json           # OAuth 토큰, 자동 갱신 (python chat_sender.py auth 로 생성)
└── .chat_state.json     # 날짜별 채팅 전송 횟수 (하루 한도용)
```

---

## 1. 준비물

| 항목 | 확인 방법 |
|---|---|
| Python **3.10 이상** | `python3 --version` |
| ffmpeg | `ffmpeg -version` |
| Groq API 키 (필수) | https://console.groq.com/keys |
| Anthropic API 키 (리액션용) | https://console.anthropic.com |
| Deepgram API 키 (선택, STT 폴백) | https://console.deepgram.com |
| Google Cloud OAuth 클라이언트 (선택, 실제 채팅 전송) | 아래 5단계 |

ffmpeg 설치:

| OS | 명령 |
|---|---|
| macOS | `brew install ffmpeg` |
| Ubuntu / Debian | `sudo apt update && sudo apt install -y ffmpeg` |
| Windows | `winget install Gyan.FFmpeg` (설치 후 **새 터미널**에서 확인) |
| Conda | `conda install -c conda-forge ffmpeg` |

## 빠른 시작 (Windows, 더블클릭)

1. **`setup.bat`**을 더블클릭합니다. Python 확인, ffmpeg 설치, 가상환경 생성, 패키지 설치, `.env` 생성이 자동으로 진행됩니다. 메모장이 열리면 API 키를 넣고 저장하세요. 마지막에 점검 결과가 나옵니다.
   - ffmpeg가 새로 설치된 경우에는 창을 닫고 `setup.bat`을 한 번 더 실행합니다.
2. **`run.bat`**을 더블클릭하고 번호를 고릅니다: 1 데모 / 2 라이브 실행 / 3 점검.

아래 2~4단계는 이 과정을 직접 할 때의 설명입니다.

## 2. 가상환경과 의존성 설치

macOS / Linux:

```bash
cd yt-live-stt
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
cd yt-live-stt
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -U pip
pip install -r requirements.txt
```

## 3. 설정 파일 만들기

```bash
cp .env.example .env          # Windows: copy .env.example .env
```

`.env`를 열어 최소한 아래 두 개를 채우세요.

```ini
GROQ_API_KEY=gsk_...
ANTHROPIC_API_KEY=sk-ant-...
```

> 빈 값 뒤에 주석을 달면(`KEY=   # 설명`) 주석이 값으로 읽힙니다. 주석은 항상 윗줄에 쓰세요. 이렇게 잘못 적으면 `main.py`가 알려 줍니다.

스트리머에 맞춘 페르소나 (선택):

```ini
PERSONA_STREAMER_NAME="철수님"
PERSONA_CHANNEL_TOPIC="소울라이크 게임 공략 방송, 차분한 분위기"
PERSONA_VIEWER_STYLE="이 게임 오래 한 고인물, 가벼운 드립 좋아함"
PERSONA_EXTRA="스포일러 금지, 다른 스트리머 언급 금지"
```

호칭을 비워 두면 리액션에 호칭(형·누나 등)을 쓰지 않습니다. 모든 설정 항목과 기본값은 `.env.example`에 있습니다. 값이 범위를 벗어나면(예: `CHUNK_SECONDS=8`, `CHAT_COOLDOWN_MIN=10`) 시작할 때 오류로 알려 줍니다.

## 4. 점검과 시험 실행

```bash
python main.py --check                                  # ffmpeg, yt-dlp, 패키지, 키, 채팅 설정 점검
python reaction.py --demo --dry-run -v                  # API 호출 없이 트리거 판정만 확인
python reaction.py --demo                               # 샘플 대사로 실제 리액션 생성
python main.py --input sample.mp3 --chat console        # 로컬 파일로 전체 흐름 시험 (실시간 속도)
```

## 5. (선택) 실제 채팅 전송 준비: YouTube Live Chat API

기본값 `CHAT_SENDER=console`은 **아무것도 전송하지 않고** 보낼 메시지만 화면에 표시합니다. 실제로 전송하려면 다음을 준비하세요.

1. https://console.cloud.google.com 에서 프로젝트를 만들고 **YouTube Data API v3**를 사용 설정합니다.
2. **OAuth 동의 화면**을 구성합니다. 테스트 모드라면 "테스트 사용자"에 전송할 구글 계정을 추가합니다.
3. **사용자 인증 정보 → OAuth 클라이언트 ID → 데스크톱 앱**을 만들고, JSON을 받아 `client_secret.json`으로 저장합니다.
4. 한 번만 로그인합니다. 브라우저가 열리고 `token.json`이 저장되며, 이후에는 자동으로 갱신됩니다.

   ```bash
   python chat_sender.py auth
   ```

5. 1건 전송으로 확인합니다.

   ```bash
   python chat_sender.py test "라이브 URL" "테스트"
   ```

## 6. 라이브 구동

```bash
python main.py "https://www.youtube.com/watch?v=라이브ID"                     # 자막 + 리액션 + dry-run 전송
python main.py "https://www.youtube.com/watch?v=라이브ID" --chat youtube-api  # 실제 채팅 전송
python main.py URL --no-reactions                                             # 자막만
python main.py URL -o log.txt -v                                              # 파일 저장 + 디버그 로그
python main.py URL --env-file .env.streamerA                                  # 스트리머별 설정 파일
```

| 옵션 | 설명 |
|---|---|
| `URL` | 유튜브 라이브 URL |
| `--input FILE` | yt-dlp 대신 로컬 파일 사용 (시험용) |
| `--chat off\|console\|youtube-api` | `.env`의 `CHAT_SENDER`보다 우선 |
| `--no-reactions` | 리액션 끄기 |
| `--allow-vod` | 라이브가 아닌 영상도 허용 (실시간 속도로 재생) |
| `--env-file` | 설정 파일 경로 (기본 `.env`) |
| `-o FILE` | 자막·리액션을 파일로 저장 |
| `-v` | 디버그 로그 (트리거 점수, 쿨다운 사유 등) |
| `--check` | 점검만 하고 종료 |

출력 예시:

```
14:03:08 INFO    리액션 켜짐 — claude-haiku-5-5 / effort=low / 페르소나: 스트리머 호칭: 철수님 …
14:03:08 INFO    채팅 전송 켜짐 [youtube-api] — 쿨다운 45~90s | 방송당 20건 | 하루 60건 (오늘 3건 사용)
14:03:12 │ ▶ 00:00:12 │ groq       540ms │ 여러분 무기 뭐 들고 갈까요? 대검이랑 쌍검 중에
14:03:13 │ 💬 질문 ↳00:00:12 │   820ms │ 철수님 대검 ㄱㄱ
14:03:13 INFO    📤 채팅 전송 (1/20, 오늘 4/60, 다음 전송까지 ≥71s): 🤖AI: 철수님 대검 ㄱㄱ
```

## 7. 종료 (Ctrl+C)

| 입력 | 동작 |
|---|---|
| **Ctrl+C 1번** | 정상 종료. 아래 순서로 정리합니다 (단계마다 시간 제한이 있고, 한 단계가 실패해도 다음 단계는 진행) |
| Ctrl+C 2번 | 남은 정리 단계를 0.5초 안에 끝내고 종료 |
| Ctrl+C 3번 | 프로세스 즉시 종료 |

정상 종료 순서:

1. **오디오·STT**: 파이프라인 태스크를 취소합니다. ffmpeg와 yt-dlp를 종료(필요하면 강제 종료)하고, 진행 중인 STT 요청을 취소한 뒤 STT 클라이언트를 닫습니다.
2. **리액션**: 생성 중인 리액션을 최대 3초 기다린 뒤 Claude 클라이언트를 닫습니다. 이 단계부터는 새 채팅 전송이 차단되어 있습니다.
3. **채팅**: 전송 중인 1건을 최대 5초 기다린 뒤 HTTP 클라이언트를 닫습니다.
4. **남은 태스크**: 일괄 취소합니다.

ffmpeg와 yt-dlp는 별도 프로세스 그룹에서 실행됩니다. 그래서 터미널의 Ctrl+C가 이들에게 직접 전달되지 않고, 파이썬이 위 순서대로 직접 정리합니다. `SIGTERM`(예: `kill`, docker stop)도 같은 방식으로 처리합니다.

종료 코드: `0` 방송 종료·파일 끝, `130` 사용자 중단, `1` 오류.

---

## 동작 상세

### Phase 1: 오디오와 STT (`live_stt.py`)
- yt-dlp로 HLS 주소와 라이브 여부를 확인하고, ffmpeg로 16kHz mono PCM을 받아 정확히 N초씩 잘라 WAV로 감쌉니다.
- 청크마다 태스크를 만들어 동시에 처리하되, 출력 순서는 항상 보장합니다. 15초 넘게 밀린 청크는 버려 실시간성을 유지합니다.
- Groq가 실패하면 Deepgram으로 즉시 폴백합니다. 실패한 공급자는 30초 쿨다운 후 자동으로 복귀합니다.
- 무음 청크는 STT를 호출하지 않습니다. Whisper가 무음 구간에서 지어내는 문장("시청해주셔서 감사합니다" 등)도 걸러냅니다.
- 스트림이 끊기면 지수 백오프로 재연결하고, 방송이 끝나면(`was_live`) 자동으로 종료합니다.
- `python live_stt.py URL`로 자막만 단독 실행할 수도 있습니다.

### Phase 2: 리액션 (`reaction.py`)
- **트리거 조건**: 질문(시청자 호명·의문형), 신남, 당황, 진지, 후원 규칙 점수가 2.0 이상일 때만 Claude를 호출합니다. 평소보다 6dB 이상 큰 목소리는 감정 고조로 가산합니다.
- **쿨다운**: 일반 12초, 강한 감정 6초, 질문 3초입니다. 생성은 한 번에 1건만 하고, 8초 넘게 걸린 리액션은 버립니다.
- **모델**: `claude-haiku-5-5`를 effort `low`로 씁니다. `claude-3-5-haiku`는 2026년 2월에 은퇴했습니다.
- **말투**: 짧은 구어체·초성을 쓰고, "~하셨군요" 같은 비서 말투는 금지합니다. 금지 말투가 나오면 한 번만 다시 생성하고, 또 나오면 버립니다.
- 오류 처리: 키 오류나 모델 없음이면 리액션만 끄고, 일시적인 오류는 해당 건만 건너뜁니다.

### Phase 3: 채팅 전송 (`chat_sender.py`)

| 가드레일 | 기본값 |
|---|---|
| 랜덤 쿨다운 | 45~90초. 쿨다운 중인 리액션은 줄 세우지 않고 버림 |
| 방송당 / 하루 한도 | 20건 / 60건. 하루 횟수는 파일에 저장되어 재시작해도 유지 |
| 중복 차단 | 최근 10건 |
| 길이 | 200자 |
| AI 표기 | `🤖AI: ` (문구 변경은 가능, 비워 둘 수 없음) |
| 오류 처리 | 할당량 소진·채팅 종료·권한 오류는 전송만 중단하고 자막·리액션은 계속 |

- YouTube API 기본 할당량은 하루 10,000 units이고 1건 전송에 약 50 units가 들어, 하루 약 200건까지 가능합니다.
- 다른 전송 방식은 `ChatSender`를 상속해 `start` / `send` / `close`만 구현하면 됩니다. 가드레일은 그대로 적용됩니다.

## 운영 시 유의사항
- **본인 방송 또는 스트리머가 허락한 방송**에서만, 봇임을 알 수 있는 이름의 계정으로 사용하세요. 유튜브 정책상 자동 메시지로 실제 시청자인 척 반응을 부풀리는 것은 금지되어 있습니다.
- `.env`, `client_secret.json`, `token.json`은 비밀번호처럼 관리하세요. 모두 `.gitignore`에 등록되어 있습니다.
- 유튜브가 자주 바뀌므로 추출 오류가 나면 `pip install -U yt-dlp`로 업데이트하세요. "Sign in to confirm you're not a bot" 오류가 나면 `YTDLP_COOKIES`를 설정하세요.

## 문제 해결

| 증상 | 해결 |
|---|---|
| `설정 오류 (.env 확인)` | 메시지에 나온 항목의 값과 범위 확인 |
| `GROQ_API_KEY 또는 DEEPGRAM_API_KEY …` | `.env`에 STT 키 입력 |
| `리액션 기능을 끕니다: 인증/권한 오류` | `ANTHROPIC_API_KEY` 확인 |
| `유효한 OAuth 토큰이 없습니다` | `python chat_sender.py auth` 실행 |
| `활성화된 라이브 채팅이 없습니다` | 방송 중인지, 채팅이 켜져 있는지 확인 |
| `처리 지연 … 청크 폐기` 경고가 계속 남 | `STT_CONCURRENCY` 올리기 또는 `GROQ_MODEL=whisper-large-v3-turbo` |
| 리액션이 너무 많음 / 적음 | `REACTION_THRESHOLD`, `REACTION_COOLDOWN` 조정 (`-v`로 점수 확인) |
