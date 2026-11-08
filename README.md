# Agent Lens · LLM Agent Viewer

AGENTS.md를 읽는 코드부터 도구 결과가 다음 모델 입력에 들어가는 순간까지, 로컬에서 직접 실행하며 확인합니다. 규칙·스킬·도구를 고친 뒤 실제 입력과 행동이 어떻게 달라지는지 비교할 수 있습니다.

Local agent internals, one observable run at a time. Inspect real messages, file reads, tool calls, tokens, KV samples and conversation summaries, with the Python code that produced them. Runs stay on your PC.

![전체 구조 위에서 파일 로더와 실행 코드를 확인하는 화면](docs/images/architecture.jpg)

구조도는 그대로 있고 현재 위치와 데이터 경로가 바뀝니다. 구성 요소를 누르면 실제 입력·출력과 실행 당시 코드가 열립니다. 재생 중인 과거 기록과 지금 실행 중인 작업도 구분합니다.

## 한 요청에서 확인할 것

1. **파일이 모델 입력이 되는 과정.** `file.read`의 바이트 → `file.decode`의 문자열 → `rules.wrap`의 지침 → `model.request`의 system 메시지를 대조합니다. 파일을 읽는 주체가 누구인지 코드에서 확인합니다.
2. **모델 요청과 실제 행동의 차이.** 모델이 만든 도구 이름·인자를 하네스가 검사하고 함수를 실행합니다. 반환값을 messages에 추가한 뒤에야 모델을 다시 호출합니다.
3. **토큰과 계산 상태.** 교육용 실행기는 실제 임베딩·Q/K/V·attention·KV·다음 토큰 선택의 수치 표본을 제공합니다. 빠른 실행기는 서버가 노출한 통계까지만 표시합니다.
4. **기억과 요약의 실체.** 대화 원본 JSONL, 세션 JSON, 모델이 만든 요약 파일, 이번 요청의 messages를 나란히 봅니다. 원문에 있던 값이 요약과 현재 입력에도 남았는지 직접 대조합니다.

![실제 KV 캐시 표본과 실행 코드](docs/images/kv-cache.jpg)

![원문·세션·요약·이번 모델 입력을 구분한 대화 저장소](docs/images/conversation-memory.jpg)

위 화면은 포함된 실제 실행 기록입니다. [스크린샷으로 따라가기](docs/WALKTHROUGH.md)에서 요청과 각 단계의 확인 지점을 설명합니다.

## 설치 전에 기록부터 보기

```powershell
.\Start-AgentLens.ps1 -SamplesOnly
```

이 모드는 가중치를 받거나 모델을 불러오지 않습니다. 실제 실행 기록 일곱 개를 제공합니다. 파일 작업, 오류 지적과 후속 행동, 토큰·텐서·KV, 집중 점검, 두 하위 에이전트의 위임·결과 합류, 대화 요약과 재사용을 재생할 수 있습니다. 개인 경로만 익명화했고 기록된 값과 소스는 보존했습니다. 화면에 **저장 기록 재생**임을 표시합니다. Python과 uv는 필요합니다. 먼저 이 저장소를 복제하거나 Code → Download ZIP으로 받아 폴더를 여세요.

## 시작하기

Windows x64와 [uv](https://docs.astral.sh/uv/getting-started/installation/)가 필요합니다. Python 3.12는 uv가 준비합니다. 첫 실행에는 인터넷과 수 GB의 저장 공간이 필요합니다. 추론 중에는 외부 모델 API를 사용하지 않습니다.

```powershell
git clone https://github.com/paconal888-a11y/LLM-Agent-viewer.git
cd LLM-Agent-viewer
.\Start-AgentLens.ps1
```

화면: **http://127.0.0.1:8765**. 첫 모델 로딩 동안 실행 버튼이 비활성화됩니다. 종료는 `Stop-AgentLens.ps1`, 브라우저를 열지 않으려면 `Start-AgentLens.ps1 -NoBrowser`를 사용하세요.

단계 정지로 실행한 뒤 새로고침해도 화면 위에 **다음 단계 · 계속 · 중지**가 나타납니다. 이전 작업을 중지하면 새 요청을 실행할 수 있습니다. 앱 프로세스가 종료된 과거 기록은 제어 가능한 작업과 구분합니다. 작성 중이던 프롬프트·설정과 선택한 기록 위치도 같은 브라우저에서 복원합니다.

기본은 **교육용 Python 실행기 + Qwen3-0.6B**입니다. CPU에서 동작합니다. 실제 forward를 토큰마다 호출하고, 임베딩·선택 레이어의 Q/K/V projection·attention·28개 레이어의 KV shape와 메모리·샘플링 분포를 기록합니다. `model.generate()`를 사용하지 않습니다. 신경망 연산은 PyTorch/Transformers가 담당합니다.

작은 모델이 지시를 놓치거나 복잡한 파일 작업에 실패할 수 있습니다. 도구·스킬 왕복을 더 안정적으로 실습하려면 선택 설치하는 **llama.cpp + Qwen3-4B Q4_K_M**도 사용할 수 있습니다.

```powershell
.\Start-AgentLens.ps1 -Backend llama
```

| 실행기 | 주로 살펴볼 것 | 관측 한계 |
| --- | --- | --- |
| 교육용 Python / 0.6B | 실제 토큰별 forward, 수치 표본, 생성 중 KV 끄기, 실제 계산 정지 | CPU 연산·상세 기록 비용, 작은 모델의 능력 |
| llama.cpp / 4B | 하네스·파일·도구 왕복, 실제 HTTP/SSE, 실행기가 보고한 토큰·캐시 통계 | 내부 텐서 미노출, 하네스 정지가 외부 서버 계산 정지를 뜻하지 않음 |

4B 실행기는 Windows용 Vulkan 배포본을 사용합니다. Vulkan 드라이버·메모리에 따라 가속 여부와 속도가 다릅니다. 별도 LM Studio 설치는 필요하지 않습니다. 두 실행기 사이에 자동 대체는 없습니다.

PowerShell 실행 정책에 막히면 조직/PC 정책에 맞는 실행 방법을 사용하세요. 이 스크립트는 실행 정책을 변경하지 않습니다. 포트 8765 또는 8088을 다른 프로그램이 사용하면 해당 포트를 비우거나 아래 수동 실행에서 앱 포트를 바꾸세요.

## 화면에서 해볼 일

1. **계산 도구** 예제를 실행합니다. `model.response`의 도구 요청, `tool.validate`, 실제 `add_numbers`, `context.append`, 두 번째 `model.request`를 차례로 선택합니다. 모델은 요청을 생성하고 Python 함수가 연산한다는 차이를 확인하세요.
2. **실행을 단계마다 멈추기**를 켜고 다시 실행합니다. 정지는 실제 관찰 지점에서 일어납니다. 재생 막대는 이미 저장한 기록을 탐색하며 모델을 재실행하지 않습니다.
3. **AGENTS.md**를 왼쪽에서 열어 수정하고 저장합니다. 다음 실행에서 파일 원문 → system 지침 → 전체 messages → 답변을 확인하세요. 자연어 지침과 코드의 실행 권한 검사는 별개입니다.
4. **스킬 · 파일 읽기**를 실행합니다. Python이 검색에 읽은 스킬 본문과, 모델 입력에 먼저 넣는 이름·설명 목록을 구분하세요. `load_skill` 후에 본문이 도구 결과로 들어갑니다.
5. **토큰화** 단계를 선택해 현재 요청과 겹치는 토큰을 확인합니다. 조각·ID·바이트 위치를 고를 수 있습니다. 한글 한 글자가 여러 토큰의 바이트에 나뉘기도 합니다.
6. 교육용의 **cache.write / attention / sample**을 선택합니다. 실제 표본의 범위와 값, K/V shape, 후보 분포를 확인하세요. source 창의 강조 줄도 같은 이벤트를 가리킵니다.
7. **구성 · 비교**에서 도구 연결, 파일 근거, 요청 간 캐시, 생성 중 KV를 비교합니다. A와 B는 실제로 각각 실행하며 전체 기록을 열 수 있습니다.

모델을 더 잘 사용하기 위한 질문과 실습은 [학습 순서](docs/LEARNING.md), 구현을 바꾸는 방법은 [확장 안내](docs/EXTENDING.md)에 있습니다.

## 에이전트 모드도 실제로 바꿔 보기

요청 위의 **작업 방식**에서 한 에이전트, 계획만, 집중 점검, 하위 에이전트 위임을 선택합니다. 모드를 바꾸면 지침·도구 권한·호출 예산·대화 구조 중 무엇이 바뀌는지 agent.mode 이벤트에 기록합니다.

집중은 같은 대화에 추가 점검을 요청합니다. 위임은 주 모델의 실제 delegate_tasks 호출을 받아 별도 대화와 제한된 도구를 가진 하위 Run을 만들고, 결과를 주 대화의 tool 메시지로 돌려줍니다. 구조도 위 에이전트 선택에서 각 대화를 따로 보거나 전체 시각순으로 확인할 수 있습니다. 주 실행과 하위 실행의 호출·토큰을 합산해 표시합니다.

설정은 편집 가능한 `demo_workspace/agents.json`에 있습니다. 하위 작업은 기본 두 작업자 스레드로 진행하지만 같은 모델의 실제 계산은 직렬화하며 대기 시간을 기록합니다. [모드와 위임의 상세 경계](docs/MODES.md)를 참고하세요. 회사에서 본 OpenCode/OmO의 역할을 이해하는 실습이며 특정 제품·버전의 전체 복제는 아닙니다.

## 파일과 하네스 수정

왼쪽에서 실제 파일을 열어 **구문 검사 → 변경 확인 → 저장 → 다음 실행** 순서로 수정합니다. 저장 전 초안은 화면 이동 중 유지됩니다. 저장은 실행이 끝난 뒤 가능하며 충돌 검사·원문 백업·직전 저장 복원을 제공합니다. 브라우저를 닫은 저장 전 초안은 복원되지 않습니다.

편집 가능한 하네스는 `files.py`, `context.py`, `toolkit.py`, `policy.py`, `inference.py`, `agent_loop.py`, `harness.py`입니다. 다음 실행 때 버전별 namespace로 불러오므로 서버 재시작 없이 반영됩니다. 코드 적용 실패는 오류로 남으며 이전 기록의 소스가 바뀌지 않습니다. 구문 검사는 동작 정확성을 보증하지 않으므로 테스트도 실행하세요.

예제 버튼은 필요한 실제 도구 결과를 **완료 조건**으로 함께 지정합니다. 파일 수정 예제는 대상 경로의 read_file·write_file·통과한 check_file 결과를 요구합니다. 조건이 빠지면 `validation.requirements → context.repair`가 답변을 반려하고 실제 행동을 다시 요청합니다. 조건은 실행 설정의 JSON에서 수정할 수 있습니다. 입력을 직접 바꾸면 예제 조건을 해제합니다. 자유 요청의 의미·정답·작업 성공 전체를 자동 판정하는 기능은 아니므로, 필요한 조건을 명시하고 실제 기록을 확인하세요.

`reference_engine.py`·웹 서버·관찰 기록기 등 기반 코드는 화면에서 읽기 전용입니다. 외부 편집기로 수정한 뒤 앱을 재시작하세요. 도구 이름·schema·실행 함수를 일치시키고 검증기가 지원하는 인자 형식을 사용해야 합니다.

## 실제 구현 범위

| 구성 | 이 프로젝트의 동작 |
| --- | --- |
| 그라운드룰 | 작업 공간 AGENTS.md, 없으면 agent.md를 읽어 system에 포함 |
| 기억 | 완료 대화 원문 JSONL·세션 상태·resume.md, 최근 대화 선택, 지적 저장 목록과 실제 입력 대조 |
| 대화 요약 | 선택한 이전 원문을 실제 로컬 모델로 요약·파일 저장·다음 요청에 재사용. 원본 보존, 호출·토큰 비용 합산 |
| 스킬 | 메타데이터 목록을 먼저 공개하고 load_skill로 본문 전달 |
| /명령어 | commands/*.md의 `$ARGUMENTS`를 치환; 셸 실행 아님 |
| 도구 | 파일 목록·읽기·쓰기·검사, 스킬, 계산, search_text, 선택 가능한 set_plan·complete_plan_step |
| 검증 | 공개 도구·인자·경로 검사, 수정한 파일의 검사 통과 후 완료 |
| 완료 조건 | 명시한 도구·인자를 실제 성공 결과와 대조; 빠진 행동을 보완 요청 |
| 모드·하위 위임 | 계획의 쓰기 제한, 집중의 추가 점검, 독립 하위 대화와 제한 도구, 실제 결과 합류와 계산 차례 대기 |
| 추론 | 로컬 두 실행기, 템플릿·토큰 진단, 토큰별 직접 생성 또는 실제 SSE |
| MCP | 실제 stdio 서버 발견·도구 목록·schema 변환·tools/call, 원본 JSON-RPC 전송과 수신 |
| LSP | 실제 python-lsp-server 문서 동기화·진단·심볼·정의, 원본 Content-Length 메시지 |

이 앱의 로딩 규칙은 직접 작성한 구현입니다. 사내 OpenCode/CodeMate의 비공개 구현이 이와 같다고 단정하지 않습니다. 모델의 숨겨진 사고나 뉴런의 의미를 복원하는 도구도 아닙니다.

## 오류를 지적한 뒤 무엇이 달라졌나?

**오류 · 지적 · 기억**에서 확정하지 않은 모델 답변, 하네스가 추가한 보완 메시지, 다음 model.request에 같은 문자열이 포함된 사실, 후속 도구 결과를 한 줄기로 봅니다. 실제 이벤트를 눌러 전체 데이터와 코드로 이동할 수 있습니다.

완료한 대화의 지적은 세션 JSON의 feedback_ledger에 보관합니다. 최근 두 대화에서 지적이 빠져도 저장 목록에는 남을 수 있습니다. **동일 요청 지적 재포함**을 켜면 같은 요청 해시의 최근 세 지적을 추가 메시지로 전달합니다. 기억 대조 이벤트가 실제 messages 포함 여부를 보여줍니다. 실패한 실행의 지적은 실행 trace에서 확인하며, 완료 대화의 기억 저장과 구분합니다.

이 경로는 모델 가중치를 재학습하지 않습니다. 시간 자체가 기억을 지우는 것이 아니라 대화 선택·새 세션·기억 스위치·입력 구성 등이 달라집니다. 지적이 입력에 있어도 같은 실수를 할 수 있으므로 입력 포함 여부와 실제 행동을 따로 평가합니다.

## 대화는 어디에 남고, 언제 요약하나?

**대화 저장소**에서 실제 파일 경로·원문·요약·선택한 실행의 messages를 봅니다. 기본은 최근 두 대화를 선택합니다. 실행 설정의 **이전 대화 요약**을 켜면 새 요청 시작 시 `memory.json`의 기준을 검사합니다. 기준을 넘으면 오래된 대화를 별도 로컬 모델 호출로 요약하고, 요약과 최근 원문을 다음 입력에 넣습니다.

포함된 예제는 실제로 원문 2,488자를 73자로 요약했습니다. 원본 파일을 남긴 채 다음 요청에서 같은 요약을 재사용한 기록도 있습니다. 이 숫자는 문자 수이며 토큰 수와 다릅니다. 요약 호출의 비용도 전체 사용량에 포함됩니다. 이 구현은 완료한 대화를 다음 요청 전에 요약하며, 실행 중간의 모든 컨텍스트 초과 상황을 처리하지는 않습니다. [저장·요약 구현과 실습](docs/MEMORY.md)을 참고하세요.

## 외부 연결과 확장

**MCP 계산 / LSP 진단** 예제로 연결을 켜거나, `demo_workspace/integrations.json`의 command·args를 바꿉니다. `{python}`은 현재 가상환경의 Python입니다. 사용자가 연결을 켤 때 설정한 로컬 프로세스를 실행합니다. 모델은 이 설정 파일을 수정할 수 없습니다.

MCP는 현재 `2026-07-28`의 stdio 도구 경로를 구현하고 server/discover, tools/list, tools/call을 관찰합니다. 이전 initialize 방식에 대한 호환 경로도 있지만 모든 서버·버전·기능을 검증한 범용 SDK는 아닙니다. 현재는 단순 스칼라 인자의 도구 schema, 단일 프로필, complete 결과를 지원합니다. HTTP/OAuth·리소스·프롬프트·MRTR는 지원하지 않습니다. LSP는 Python 서버의 진단·문서 심볼·정의를 지원하며 파일을 요청할 때 문서 버전을 동기화합니다.

연결 전후 비교는 같은 기본 하네스에서 MCP 또는 LSP만 바꿉니다. 공개 도구 수, 원본 프로토콜 왕복, 실제 도구 실행, 누적 입력 토큰·시간을 비교합니다. 연결이 늘면 비용과 입력도 늘 수 있으며 무조건 품질이 좋아진다고 해석하지 않습니다.

[비슷한 제품과 참조한 하네스](docs/REFERENCES.md)에 공식 자료와 채택한 설계를 정리했습니다.

## 관측을 해석할 때

- 파일을 읽음, 모델 입력에 포함함, 모델이 실제로 따름은 각각 다른 사실입니다.
- 토큰 표는 실제 tokenizer 결과입니다. 교육용에서는 생성에 사용한 입력과 대조하며, 외부 실행기에서는 진단 API와 실제 생성 응답을 구분합니다.
- K/V heatmap은 선택한 레이어의 head 0, 마지막 최대 16위치 × 처음 16차원 표본입니다. Q/K projection은 정규화·RoPE 전, 캐시 K는 RoPE 후입니다. 전체 텐서를 저장하지 않습니다.
- attention은 선택 레이어·head 0의 마지막 query 일부입니다. 높은 가중치가 답변의 의미적 이유를 입증하지 않습니다.
- 대화 이력은 텍스트 데이터, KV는 계산 상태입니다. 요청 간 접두부 재사용과 한 답변 생성 중 KV 사용도 서로 다른 설정입니다.
- 시간에는 관찰 기록·진단 비용이 포함됩니다. 실제 정지 시간은 활동 시간에서 제외하고 순수 forward 시간도 따로 기록합니다. A→B 각 한 번의 비교는 일반 성능 순위를 증명하지 않습니다.
- 코드 강조는 이벤트가 기록된 관찰 지점과 실제 호출 스택입니다. 모든 줄의 실행을 추적하는 디버거는 아닙니다.
- Python 코드는 실제 프레임을, 브라우저 입력·표시는 계측한 JavaScript 함수와 브라우저의 보고를 연결합니다. `browser.render`는 DOM 본문 반영이며 사용자가 화면을 읽었다는 측정은 아닙니다. 활동 시간은 하네스 실행 시간이고 브라우저 보고 시각은 별도 이벤트입니다.

## 저장 위치와 로컬 접근

모델·실행기는 저장소 밖 `%LOCALAPPDATA%\AgentLens`, 실행 기록은 `.runs/traces`, 대화는 `.runs/sessions`, 편집 백업은 `.runs/edits`, 하네스 스냅샷은 `.runs/code`에 저장합니다. `.runs`·`.local`·가중치·가상환경은 Git에서 제외합니다.

앱은 `127.0.0.1`에만 바인딩합니다. 파일 도구는 `demo_workspace` 안에서 작업하며 임의 셸 실행 도구가 없습니다. 기본 실행은 읽기 권한이며 파일 수정 예제 또는 명시한 쓰기 스위치에서만 write_file을 공개합니다. 모델이 규칙·스킬·명령어·서버 실행 설정을 고치는 것은 정책 코드에서 제한하고, 사용자가 편집기로 수정하는 경로는 따로 둡니다. 편집한 Python 코드는 다음 실행 때 이 PC에서 실행되므로 신뢰하는 프로젝트 코드만 사용하세요. 이 앱은 적대적 코드를 격리하는 샌드박스가 아닙니다.

기록에는 프롬프트·파일 원문·로컬 경로가 포함됩니다. 공유할 JSON과 Git 파일 목록을 확인하세요. **화면을 인터넷에 공개하는 서버로 사용하지 마세요.**

## 개발과 검증

프런트엔드는 빌드 없는 HTML/CSS/JavaScript입니다. 하네스·HTTP 서버와 MCP 계산 예제는 Python 표준 라이브러리를 사용합니다. 교육용 추론은 reference, 실제 Python 언어 서버는 integrations 추가 패키지를 설치합니다. 의존성은 uv.lock에 고정합니다.

```powershell
uv sync --python 3.12 --extra reference --extra integrations --locked
uv run --extra reference --extra integrations --offline python -m unittest discover -s tests -v
node --test tests/architecture.test.cjs
```

Python 테스트 79개와 구조도 테스트 2개를 확인했습니다. Node는 구조도 개발 검사에만 필요합니다. 모의 모델을 사용한 하네스 경계와 작은 무작위 Qwen 신경망의 실제 KV 계산을 검사하며, 다운로드한 0.6B/4B의 실제 연결 검증과 구분합니다. 상세 결과·알려진 한계는 [검토 기록](docs/REVIEW.md)을 참고하세요.

Linux/macOS에서 교육용 실행기를 수동으로 준비할 수도 있습니다. 이 경로는 Windows 실행 스크립트와 별개이며 해당 OS의 브라우저·패키지 설치를 별도 확인해야 합니다.

```sh
uv sync --python 3.12 --extra reference --extra integrations --locked
uv run --extra reference --extra integrations python scripts/setup_reference.py
uv run --extra reference --extra integrations python -m local_agent_lab.web --backend reference --port 8765
```

이미 준비한 로컬 HTTP 서버를 연결하려면 `--backend llama --model-url http://127.0.0.1:1234 --model 실제모델ID`를 사용합니다. 도구 호출 형식과 선택 진단 API 지원 여부는 서버마다 다릅니다.

## 구성

```text
local_agent_lab/
  web.py               HTTP 수신과 파일 편집 API
  agent_loop.py        모델 ↔ 도구 반복과 완료
  context.py           규칙·스킬·커맨드·messages
  files.py             경로 검사와 파일 I/O
  toolkit.py           도구 정의와 실제 함수
  policy.py            실행·완료 조건
  harness.py           진입점과 대화 이력
  context_memory.py    대화 원본·로컬 모델 요약·저장·재사용
  inference.py         실행기 경계·템플릿·토큰·응답 조립
  reference_engine.py  실제 forward·샘플링·KV·텐서 관찰
  runtime.py           루프백 HTTP/SSE 어댑터
  trace.py             이벤트·실행 정지·소스 스냅샷
  project.py           편집·복원·버전별 코드 적용
  feedback.py          지적 저장·재포함·현재 입력 대조
  orchestration.py     모드·하위 실행·계산 차례·결과 합류
  integrations.py      MCP·LSP 도구 어댑터
  stdio_rpc.py         별도 프로세스의 실제 JSON-RPC 전송
  demo_mcp_server.py   학습용 MCP 계산 서버
web/                   실습 화면
demo_workspace/        공개 가능한 규칙·스킬·예제
tests/                 가중치 다운로드 없는 테스트
samples/               익명화한 실제 실행 기록 (gzip)
```

자체 코드: [MIT](LICENSE). 모델·엔진·라이브러리: [외부 고지](THIRD_PARTY_NOTICES.md).
