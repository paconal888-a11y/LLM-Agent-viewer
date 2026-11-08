# 설치와 실행

## Windows

Windows x64, Git, [uv](https://docs.astral.sh/uv/getting-started/installation/)를 준비합니다. 시작 스크립트가 Python 3.12와 선택한 실행기의 의존성을 설치합니다.

```powershell
git clone https://github.com/paconal888-a11y/LLM-Agent-viewer.git
cd LLM-Agent-viewer
.\Start-AgentLens.ps1 -SamplesOnly
```

브라우저에서 [http://127.0.0.1:8765](http://127.0.0.1:8765)를 엽니다. `-SamplesOnly`는 모델을 다운로드하거나 실행하지 않고 [포함된 기록](../samples/README.md)을 재생합니다.

### 실행기 선택

| 실행기 | 모델 | 실행 명령 | 특징 |
| --- | --- | --- | --- |
| Python | Qwen3-0.6B | `.\Start-AgentLens.ps1` | CPU, 토큰별 forward·샘플링·KV·텐서 표본 기록 |
| llama.cpp | Qwen3-4B Q4_K_M | `.\Start-AgentLens.ps1 -Backend llama` | Vulkan, HTTP/SSE 응답과 캐시 통계 기록 |

첫 실행에는 인터넷 연결과 수 GB의 저장 공간이 필요합니다. 작은 모델은 복잡한 도구 요청을 놓칠 수 있습니다. 하네스와 파일 작업은 4B 모델로, 신경망 연산은 Python 실행기로 확인할 수 있습니다.

llama.cpp는 Windows용 Vulkan 배포본을 사용합니다. 드라이버·메모리에 따라 GPU 사용 여부와 속도가 달라집니다. Python 실행기는 PyTorch/Transformers의 신경망을 직접 호출합니다. 두 실행기 사이에 자동 전환은 없습니다.

### 실행과 종료

`-NoBrowser`는 브라우저를 열지 않고 앱을 시작합니다. 종료 스크립트는 이 프로젝트가 시작한 프로세스를 정리합니다.

```powershell
.\Start-AgentLens.ps1 -NoBrowser
.\Stop-AgentLens.ps1
```

모델을 읽는 동안 실행 버튼이 비활성화됩니다. **실제 실행을 단계마다 멈추기**를 켜면 관찰 지점에서 작업이 정지합니다. 새로고침 뒤에도 화면 위의 **다음 단계·계속·중지**로 작업을 제어할 수 있습니다. 작성 중인 프롬프트와 설정, 선택한 기록 위치는 같은 브라우저에서 복원됩니다. 앱이 종료된 뒤 남은 기록은 재생만 가능합니다.

실행 정책 오류가 나면 PC·조직의 PowerShell 정책을 확인하세요. 시작 스크립트는 실행 정책을 변경하지 않습니다. 포트 8765 또는 8088이 사용 중이면 사용 중인 프로그램을 확인하거나 수동 실행의 앱 포트를 바꾸세요.

## Linux/macOS 수동 실행

교육용 Python 실행기는 다음 명령으로 준비합니다. Windows 시작 스크립트와 별개이며, 해당 OS의 설치·브라우저 동작은 별도 검증이 필요합니다.

```sh
uv sync --python 3.12 --extra reference --extra integrations --locked
uv run --extra reference --extra integrations python scripts/setup_reference.py
uv run --extra reference --extra integrations python -m local_agent_lab.web --backend reference --port 8765
```

가중치 없이 기록만 보려면 다음을 사용합니다.

```sh
uv sync --python 3.12 --locked
uv run python -m local_agent_lab.web --samples-only --port 8765
```

## 기존 로컬 서버 연결

```powershell
uv run --extra integrations python -m local_agent_lab.web --backend llama --model-url http://127.0.0.1:1234 --model MODEL_ID
```

`MODEL_ID`를 서버가 제공하는 모델 ID로 바꿉니다. 도구 호출 형식과 템플릿·토큰 진단 API의 지원 여부는 서버마다 다릅니다. 이 경로는 서버 내부 텐서를 수집하지 않습니다.

## 저장 위치

| 경로 | 내용 |
| --- | --- |
| `%LOCALAPPDATA%\AgentLens` | 모델과 Windows 추론 엔진 |
| `.runs/traces` | 실행 기록과 소스 스냅샷 참조 |
| `.runs/conversations` | 완료한 대화 원문 JSONL |
| `.runs/sessions` | 최근 대화, 요약, 지적 목록 등 세션 상태 |
| `.runs/summaries` | 모델이 생성한 요약과 원문 범위 |
| `.runs/edits` | 편집 백업 |
| `.runs/code` | 버전별 하네스 소스 |
| `.local` | 앱 로그와 프로세스 정보 |

`.runs`, `.local`, 가상환경과 모델 파일은 Git에서 제외합니다. 실행 기록을 내보내 공유할 때는 프롬프트·파일 원문·로컬 경로를 확인하세요.
