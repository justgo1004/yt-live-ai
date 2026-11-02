"""
앱 버전과 업데이트 저장소.

새 버전 내기: 아래 __version__ 숫자를 올리고 RELEASE_NOTES.md 를 고쳐 main 에 push 하면
GitHub Actions(.github/workflows/release.yml)가 윈도우 exe 를 빌드·시험해 릴리스 v<버전> 을 발행한다.
"""
__version__ = "1.2.0"
UPDATE_REPO = "justgo1004/yt-live-ai"      # 릴리스(자동 업데이트 파일)를 내려받을 GitHub 저장소
EXE_NAME = "YouTubeLiveAI.exe"
