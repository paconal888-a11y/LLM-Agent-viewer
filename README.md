# Agent Lens

[![Tests](https://github.com/paconal888-a11y/LLM-Agent-viewer/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/paconal888-a11y/LLM-Agent-viewer/actions/workflows/tests.yml)
[![Release](https://img.shields.io/github/v/release/paconal888-a11y/LLM-Agent-viewer)](https://github.com/paconal888-a11y/LLM-Agent-viewer/releases)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

로컬 LLM 에이전트가 실제로 어떻게 동작하는지 관찰하고, 하네스를 고친 효과를 측정하는 도구입니다. 에이전트가 읽은 파일, 모델에 보낸 메시지, 도구 호출과 결과를 실행 코드와 함께 보여줍니다. 각 단계를 코드가 정했는지, 모델이 골랐는지, 설정으로 바꿀 수 있는지 구분합니다.

**관찰 → 원인 층 좁히기 → 한 조건 수정 → 반복 측정.** 실행 기록을 보는 것과 원인을 아는 것은 다릅니다. 이 도구는 기록에서 셀 수 있는 사실만 보여 주고, 고친 것이 나아졌는지는 같은 조건을 반복 실행한 통과율로 확인하게 합니다.

![파일 로딩 단계와 실행 코드](docs/images/architecture.jpg)

[시작하기](#시작하기) · [사용법](docs/WALKTHROUGH.md) · [구조](docs/ARCHITECTURE.md) · [하네스 수정](docs/EXTENDING.md) · [실제 에이전트로 옮기기](docs/TRANSFER.md)

## 시작하기

Windows x64, Git, [uv](https://docs.astral.sh/uv/getting-started/installation/)가 필요합니다. Python 3.12는 uv가 준비합니다.

```powershell
git clone https://github.com/paconal888-a11y/LLM-Agent-viewer.git
cd LLM-Agent-viewer
.\Start-AgentLens.ps1 -SamplesOnly
```

실행 정책 때문에 `.ps1`이 막히면 `.\Start-AgentLens.cmd -SamplesOnly`를 쓰세요. [http://127.0.0.1:8765](http://127.0.0.1:8765)에서 왼쪽 **예제 기록**을 선택하세요. 모델 설치 없이 파일 작업, 토큰·KV 캐시, 하위 에이전트, 대화 요약과 반복 응답 중단 등 아홉 개의 실행 기록을 재생할 수 있습니다.

### 로컬 모델로 실행

직접 프롬프트를 입력하려면 다음 중 하나로 실행합니다. 첫 실행에 모델과 의존성을 다운로드하며, 추론에는 외부 모델 API를 사용하지 않습니다.

| 실행기 | 명령 | 용도 |
| --- | --- | --- |
| Python · Qwen3-0.6B | `.\Start-AgentLens.ps1` | CPU에서 토큰별 연산과 텐서 관찰 |
| llama.cpp · Qwen3-4B | `.\Start-AgentLens.ps1 -Backend llama` | 파일 작업과 도구 호출, Vulkan 가속 |

종료는 `.\Stop-AgentLens.ps1`입니다. 설치 옵션과 Linux/macOS 수동 실행은 [설치 안내](docs/SETUP.md)를 참고하세요.

## 기능

- **실행 추적** — 파일 읽기, 메시지 조립, 모델 호출, 도구 실행을 구조도에서 따라가며 입력·출력과 코드 확인
- **결정 주체 구분** — 단계마다 코드가 결정 / 모델이 선택 / 설정·파일에서 옴 / 실행기 계산을 표시하고, 모델이 고른 단계만 걸러 보기
- **원인 층 구분** — 실패한 실행을 실행기·설정 → 도구·하네스 → 모델 입력 → 모델의 선택 순서로 좁히고, 완료한 실행은 코드가 확인한 범위와 확인하지 않은 범위를 표시
- **반복 측정** — 같은 요청을 조건별로 최대 10회 실행해 코드로 확인한 통과 수·실패 층·호출·토큰을 비교. 스위치 하나만 바꾸거나 기준선만 측정
- **입력 비교** — 파일 원문이 포함된 메시지로 이동하고, 모델 호출 사이에 추가·변경된 메시지와 도구 정의 확인
- **하네스 편집** — AGENTS.md, 스킬, 커맨드와 Python 도구 함수를 수정하고 다음 실행에 반영
- **추론 관찰** — 토큰 ID, 임베딩, Q/K/V, attention, KV 캐시, 다음 토큰 선택의 수치 표본
- **대화 관리** — 원문·요약·현재 모델 입력을 비교하고 저장된 요약의 재사용 확인
- **에이전트 모드** — 단일 실행, 계획, 추가 점검, 하위 에이전트별 대화와 도구 권한 비교
- **MCP·LSP 연결** — 외부 서버의 도구 목록·호출·응답과 연결 전후의 토큰·시간 비교
- **본문 검색·공유용 내보내기** — 모델 입력 messages·도구 결과·파일 원문 안의 문자열을 찾아 해당 기록으로 이동, 경로·사용자 이름·비밀값을 치환한 공유용 사본을 치환 횟수 미리보기와 함께 저장(원본 기록은 유지)
- **학습 흐름** — 단계 상세의 ‘학습 흐름’ 탭에서 예측 → 증거 확인 → 원인 설명 → 한 조건 수정 → 재검증 순서로 기록을 살펴보는 안내

<details>
<summary>모델 입력, KV 캐시와 대화 저장소 화면</summary>

**모델 입력** — 요청 사이의 메시지 변경과 도구 정의를 비교합니다.

![모델 입력과 이전 요청 비교](docs/images/input-diff.jpg)

**KV 캐시** — 처리한 위치 수, 메모리 사용량, 선택한 레이어의 K/V 표본을 표시합니다.

![KV 캐시 통계와 K 텐서 표본](docs/images/kv-cache.jpg)

**대화 저장소** — 원문 파일, 세션 상태, 모델 요약, 요청에 포함된 메시지를 구분합니다.

![대화 원문과 요약, 모델 입력](docs/images/conversation-memory.jpg)

</details>

## 문서

| 문서 | 내용 |
| --- | --- |
| [설치 안내](docs/SETUP.md) | 실행기 선택, 수동 설치, 저장 경로, 문제 해결 |
| [사용법](docs/WALKTHROUGH.md) | 예제 실행과 화면 조작 |
| [구조](docs/ARCHITECTURE.md) | 처리 흐름, 주요 모듈, 편집 범위 |
| [하네스 수정](docs/EXTENDING.md) | 도구 추가, 규칙·스킬 변경, MCP·LSP 설정 |
| [에이전트 모드](docs/MODES.md) | 역할·권한·호출 예산과 하위 작업 처리 |
| [대화 저장과 요약](docs/MEMORY.md) | 원문 보관, 요약 생성, 다음 입력 선택 |
| [실습](docs/LEARNING.md) | 구성을 바꾸며 확인할 항목, 개선 측정 절차 |
| [실제 에이전트로 옮기기](docs/TRANSFER.md) | 교육용 하네스와 실제 도구의 차이, 확인 방법, 고정된 모델에서 고를 수단 |
| [테스트·측정 결과](docs/REVIEW.md) | 검증 환경, 실행 결과, 알려진 제한 |
| [관련 프로젝트와 명세](docs/REFERENCES.md) | 설계 참고 자료와 프로토콜 문서 |
| [유사 제품 비교](docs/COMPARISON.md) | 비교 대상, 반영한 개선, 남은 차이 |
| [변경 기록](CHANGELOG.md) | 버전별 변경 사항 |

## 개발

프런트엔드는 빌드 없는 HTML/CSS/JavaScript, 하네스와 HTTP 서버는 Python입니다. 교육용 추론에는 PyTorch/Transformers, LSP에는 python-lsp-server를 사용합니다. 의존성은 `uv.lock`에 고정합니다.

```powershell
uv sync --python 3.12 --extra reference --extra integrations --locked
uv run --extra reference --extra integrations --offline python -m unittest discover -s tests -v
node --test tests/architecture.test.cjs tests/inspection.test.cjs tests/editor.test.cjs tests/settings.test.cjs tests/export_search.test.cjs
```

Node.js는 구조도·입력 검사·편집기·설정 복원·내보내기/본문 검색 화면 테스트에만 필요합니다. [이슈](https://github.com/paconal888-a11y/LLM-Agent-viewer/issues)에 재현 절차와 실행기 정보를 남기거나, 변경한 기능의 테스트와 함께 PR을 보내 주세요.

## 지원 범위

텐서 관찰은 Python 실행기의 선택 레이어·위치 표본을 제공합니다. llama.cpp는 서버가 노출하는 응답과 통계를 표시합니다. 대화 요약은 완료한 대화를 다음 요청 전에 처리합니다. MCP는 stdio 도구 연결, LSP는 Python 진단·심볼·정의를 지원합니다.

앱은 로컬 접속용입니다. 편집한 Python 코드와 연결한 도구 서버는 이 PC에서 실행되며, 실행 기록에는 프롬프트와 파일 내용이 포함됩니다. 자세한 관측 범위와 제한은 [테스트·측정 결과](docs/REVIEW.md)에 정리했습니다.

## 라이선스

[MIT](LICENSE). 모델·엔진·라이브러리의 라이선스는 [외부 고지](THIRD_PARTY_NOTICES.md)를 참고하세요.
