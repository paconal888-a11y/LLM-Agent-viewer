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

`-NoBrowser`는 브라우저를 열지 않고 앱을 시작하고, `-Port`는 앱 포트(기본 8765)를 바꿉니다. 종료 스크립트는 이 프로젝트가 시작한 프로세스를 정리합니다.

```powershell
.\Start-AgentLens.ps1 -NoBrowser
.\Stop-AgentLens.ps1
```

모델을 읽는 동안 실행 버튼이 비활성화됩니다. **실제 실행을 단계마다 멈추기**를 켜면 관찰 지점에서 작업이 정지합니다. 새로고침 뒤에도 화면 위의 **다음 단계·계속·중지**로 작업을 제어할 수 있습니다. 작성 중인 프롬프트와 설정, 선택한 기록 위치는 같은 브라우저에서 복원됩니다. 앱이 종료된 뒤 남은 기록은 재생만 가능합니다.

실행 정책 오류가 나면 `Start-AgentLens.cmd -SamplesOnly`처럼 `.cmd` 진입점을 쓰세요. 이 파일은 PC의 실행 정책을 바꾸지 않고 해당 호출에만 `-ExecutionPolicy Bypass`를 적용합니다. 브라우저로 내려받은 ZIP을 푼 경우에는 `Unblock-File -Path .\*.ps1, .\scripts\*.ps1`로 다운로드 표식을 지워도 됩니다. uv는 `--system-certs`를 지원하는 최근 버전(0.6 이상)이 필요하고, `-SamplesOnly`도 Python 3.12가 없으면 uv가 내려받으므로 첫 실행에는 네트워크가 필요합니다.

시작 스크립트는 포트에 떠 있는 것이 무엇인지 먼저 식별합니다. `/api/status`가 보고하는 프로젝트 경로·버전·모드가 이 폴더의 것과 같으면 그 앱을 그대로 사용하고, 이 폴더의 `.local/processes.json`에 기록된 프로세스(PID·실행 파일·시작 시각이 모두 일치)면 종료한 뒤 다시 시작합니다. 다른 프로젝트의 Agent Lens나 기록에 없는 프로세스, 다른 프로그램이 포트를 쓰고 있으면 종료하지 않고 PID와 이름을 표시한 뒤 멈춥니다. 그때는 `-Port 8766`처럼 다른 포트를 지정하거나 해당 프로그램을 직접 종료하세요. 새 앱은 `/api/status`가 이 프로젝트의 것으로 응답할 때까지 최대 90초 기다리며, 그 전에 종료되거나 응답이 없으면 `.local/app.stderr.log`의 마지막 20줄과 함께 실패를 알립니다. 모델 로딩은 응답 이후에도 계속될 수 있습니다.

이 프로젝트가 띄운 llama.cpp 엔진이 아직 모델을 읽는 중이면(`/health` 503) 두 번째 엔진을 띄우지 않고 최대 120초 기다립니다. 종료 스크립트는 기록된 PID의 실행 파일 경로와 시작 시각이 기록과 같을 때만 종료합니다. PID가 다른 프로세스에 재사용됐으면 종료하지 않고 그 사실을 표시합니다. llama.cpp 엔진 포트 8088은 바꾸지 않습니다.

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
