# 하네스 수정과 확장

## 먼저 작은 변경으로 확인하기

AGENTS.md에 출력 형식을 한 줄 추가하고 다음 실행의 `file.decode`·`context.assemble`에서 실제 반영을 확인하세요. 원래 내용은 직전 저장 복원으로 되돌릴 수 있습니다. 저장 전후 차이, 실행 때의 하네스 버전, 당시 소스를 함께 보존합니다.

## 도구 추가

`local_agent_lab/toolkit.py`의 `TOOLS`에 function schema를 추가하고 같은 이름의 함수를 `IMPLEMENTATIONS`에 연결합니다. 기본 도구인 add_numbers가 가장 작은 예제입니다.

```python
def multiply(ctx, a, b):
    return ctx.finish({"a": a, "b": b, "result": a * b})
```

schema는 `function_tool("multiply", "두 정수를 곱합니다.", {"a": {"type": "integer"}, "b": {"type": "integer"}}, ["a", "b"])` 형식으로 TOOLS에 추가합니다. 함수도 IMPLEMENTATIONS에 포함하세요. schema와 함수 signature는 같아야 합니다. 현재 검증기는 string/integer/number/boolean만 지원합니다. 새로운 형식이 필요하면 policy.py의 검증부터 확장하세요.

`ctx.finish()`는 함수의 실제 반환값과 소스를 기록합니다. `ctx.read()`는 경로와 실제 파일 I/O를 기록합니다. 도구가 읽을 파일을 직접 `Path`로 열면 해당 I/O는 기존 관찰 함수의 기록에 포함되지 않습니다. 도구 요청 수신 → 검사 → 함수 → 결과 → tool 메시지 추가는 각각 독립적인 경계입니다.

에이전트 루프는 파일 변경의 완료 조건을 추적하기 위해 write_file과 check_file의 결과를 사용합니다. 새로운 쓰기 도구를 추가하면 agent_loop.py의 dirty/checked 추적도 함께 확장해야 합니다. schema만 추가했다고 실행 권한 검사가 충분한 것은 아닙니다.

## 설정과 규칙의 층

`agent-lens.json`은 코드 기본값 위에 사용자 층(`demo_workspace/.user-home/`), 프로젝트 층(`demo_workspace/`) 순서로 덮어씁니다. 프로젝트 밖의 실제 사용자 폴더는 읽지 않으며, `.user-home`은 전역 설정 위치를 흉내 낸 실습 폴더입니다. 허용 범위를 벗어난 값이나 없는 키가 있으면 실행 전에 거부합니다. 모델의 파일 도구는 이 설정 파일과 `.user-home`을 수정할 수 없습니다.

```json
{
  "rules": { "file_names": ["AGENTS.md", "agent.md"], "directory_rules": true },
  "loop": { "max_model_calls": 8 },
  "context": {
    "compact_at_ratio": 0.8,
    "compact_at_tokens": 0,
    "keep_recent_messages": 4,
    "prune_tool_characters": 1500,
    "max_summaries": 2
  }
}
```

위 값이 코드 기본값입니다. `compact_at_tokens`가 0이면 컨텍스트 창의 `compact_at_ratio` 비율을 기준으로 씁니다. 어떤 층의 값이 적용됐는지는 `config.resolve` 기록의 표에서 확인합니다.

규칙 파일이 둘 이상이면 사용자 층에 1번, 프로젝트 층에 2번을 붙여 system 메시지에 넣고, 충돌하면 번호가 큰 쪽을 따르라는 문장을 더합니다. 코드가 정하는 것은 순서와 문장뿐이며 모델이 따르는지는 강제하지 않습니다. 하위 폴더의 AGENTS.md는 시작할 때 읽지 않고, 파일 도구가 그 폴더의 파일을 다룬 뒤 처음 한 번만 추가합니다.

## 규칙·스킬·커맨드 바꾸기

- context.py: 탐색할 파일명, 지침 감싸기, 메시지 배치, 도구 목록.
- compaction.py: 입력을 줄이는 시점과 방법, 요약 지시문. 대화 기록 원본은 건드리지 않습니다.
- checkpoint.py: 저장할 진행 상태. 모델 내부 상태는 저장하지 않습니다.
- demo_workspace/skills/*/SKILL.md: 이름·설명과 실제 절차. 검색 결과와 본문 로드를 구분.
  - 지원 형식(`skill-front-matter/1`): 첫 줄 `---`부터 다음 단독 `---` 줄까지의 front matter에서 줄 시작의 `name:`과 `description:`만 읽습니다. 값은 한 줄 스칼라이며 따옴표 없는 평문(` #` 뒤는 주석), `"..."`, `'...'`를 지원합니다. CRLF와 UTF-8 BOM, 다른 키(무시하고 기록)는 허용합니다. name은 64자, description은 500자까지입니다.
  - 지원하지 않는 표현은 다른 값으로 바꾸지 않습니다. 여러 줄 값(`>`, `|`), 흐름 컬렉션(`[ ]`, `{ }`), anchor·alias·tag(`&`, `*`, `!`), 들여쓴 키, 중복 키, 빈 값, 닫히지 않은 따옴표가 있으면 그 스킬을 목록에서 제외하고 `skills.metadata` 이벤트(status=warning)와 `skills.catalog`의 `skipped`에 파일 경로·줄 번호·사유를 남깁니다. 일반 YAML 파서가 아니며 전체 YAML을 지원한다고 주장하지 않습니다.
- demo_workspace/commands/*.md: `$ARGUMENTS`를 치환하는 프롬프트 템플릿.
- harness.py: 복원할 이력과 저장 정책. 텍스트 이력을 모델 가중치나 KV와 혼동하지 않기.
- policy.py: 공개 도구·인자·경로·완료 조건. LLM이 반환한 요청을 신뢰하지 않는 검사 위치.
  - 완료 조건의 값 비교: 숫자는 값으로 비교합니다(3과 3.0은 같음). 숫자와 불리언, 숫자와 문자열은 다른 값입니다(1과 true, 1과 "1"). 배열은 순서와 길이까지 맞아야 하고, 결과 객체는 조건에 적은 키만 확인합니다. 문자열의 유니코드 정규화(NFC/NFD)는 하지 않습니다.

## 추론 실행기 바꾸기

inference.py가 하네스와 실행기 사이의 경계입니다. 외부 실행기는 runtime.py의 request/observe/stream_json을, 교육용은 reference_engine.py의 generate_observed를 사용합니다. 템플릿·토큰 진단을 지원하지 않는 서버는 미지원 이벤트를 남기며 수치를 추정하지 않습니다.

교육용은 모델 로딩·토큰화·forward·KV·샘플링을 구분합니다. 다른 모델 구조를 쓴다면 hook 경로, 캐시 API, head 구분, 도구 표현 파서도 맞춰야 합니다. reference_engine.py는 Qwen3 구조 전용이며 모든 Transformers 모델에 범용으로 동작하지 않습니다.

## 인터넷 검색 바꾸기

`web_search` 도구의 네트워크 부분은 `local_agent_lab/websearch.py` 하나에 있습니다. 요청 주소는 이 파일에 고정되어 있고 모델은 검색어만 정합니다. 다른 검색 서비스를 쓰려면 `search()`가 같은 형식의 결과를 돌려주도록 고치세요. 호출 한도와 검색어 길이도 이 파일의 상수로 바꿉니다. 도구는 실행 설정이나 입력란의 **인터넷 검색**을 켠 실행에서만 모델에게 공개되고, 꺼져 있으면 모델이 호출해도 코드가 거부합니다.

## MCP와 LSP 연결을 교체하기

실제 어댑터는 integrations.py, framing과 프로세스 수명은 stdio_rpc.py에 있습니다. demo_workspace/integrations.json에서 로컬 서버 실행 프로필을 바꾸세요. command는 실행 파일, args는 인자 목록입니다. shell 문자열을 실행하지 않습니다. UI의 MCP·LSP 스위치를 켠 다음 실행부터 설정을 읽습니다. 외부 서버는 이 PC의 별도 프로세스로 실행하므로 신뢰하는 서버를 연결하세요.

MCP는 2026-07-28의 server/discover → tools/list → 모델용 schema → tools/call → 구조화 결과 정규화 경로를 사용합니다. tools/call이 JSON-RPC 오류나 시간 초과로 끝나면 실행 전체가 아니라 그 도구 호출의 실패로 처리해 `error`·`executed_successfully=false`를 tool 메시지로 모델에 돌려줍니다(시간 초과는 서버 쪽 부수효과가 이미 일어났을 수 있어 `side_effects: unknown`으로 표시하고 같은 요청을 다시 보내지 않습니다). 서버가 stdout에 JSON-RPC 외 출력을 쓰거나 2 MiB를 넘는 메시지를 보내면 그 연결은 더 쓰지 않고 다음 실행에서 프로세스를 다시 시작합니다(`mcp.process.restart`). 같은 이름의 도구를 두 번 광고하는 서버는 거부합니다. 모든 요청의 _meta와 응답 ID를 기록합니다. 도구에는 mcp_서버명_도구명 접두부를 붙입니다. 이전 initialize 방식의 호환 경로와 최신 경로를 같은 초기화라고 표시하지 않습니다. 현재는 단일 stdio 프로필과 단순 스칼라 인자를 지원합니다. 복합 schema를 쓰려면 policy.py 검증기를 함께 확장하세요. HTTP/OAuth와 전체 MCP 기능을 구현한 SDK는 아닙니다.

MCP 프로필의 allow_tools는 서버가 광고한 도구 중 공개할 원본 이름 목록입니다. 기본은 ["add"]이며 빈 목록이면 어떤 도구도 모델에 공개하지 않습니다. allow_tools 키가 아예 없으면 기본 거부입니다. 어떤 도구도 공개하지 않고 `mcp.policy` 이벤트로 광고된 도구 이름과 함께 그 사실을 기록하며, 실행 요청도 거부합니다. 서버의 도구 목록이 바뀌었다는 사실만으로 실행 권한이 늘어나지 않습니다. 연결 프로필 변경은 다음 연결 준비에서 새 설정으로 구별합니다.

인자 검사는 이름이 `path`인 인자에만 작업 공간 제한을 적용합니다. MCP 도구도 같습니다. `file`처럼 다른 이름의 경로 인자는 하네스가 알 수 없으므로 서버가 직접 제한해야 합니다.

쓰기 권한이 꺼진 실행(계획 모드와 `writes=false`인 모든 모드)에서는 호스트의 `read_only_tools`에도 이름을 등록한 도구만 공개·실행합니다. 기본 계산 도구는 `["add"]`입니다. 미분류 외부 도구와 서버의 `readOnlyHint`만 있는 도구는 거부하고, 제외한 도구마다 `mcp.tool.filtered` 이벤트에 이유(`plan_requires_host_read_only` 또는 `writes_off_requires_host_read_only`)를 남깁니다. 내장 write_file만 숨기고 외부 쓰기 도구는 노출하던 불일치를 없앤 규칙입니다. 이 목록은 신뢰하는 사용자가 서버 구현을 확인해 지정하는 정책이며, 외부 프로세스의 임의 부작용을 차단하는 OS 샌드박스는 아닙니다.

LSP는 실제 python-lsp-server에 initialize → initialized → 문서 didOpen/didChange를 보내고 진단 통지·심볼·정의 응답을 받습니다. 문서 URI·버전·원문과 Content-Length 바이트 경계를 확인하세요. 원본 range는 0부터 시작하며 모델용 locations는 1부터 시작하는 행 번호를 별도 필드로 계산합니다. ast.parse/compile 검사와 언어 서버의 pyflakes 진단은 다른 증거입니다.

모델에 공개하는 도구는 Run.extra_tools, 실행 어댑터는 Run.tool_handlers에 연결합니다. 전송 자체가 도구 성공은 아니며, 서버의 오류와 도구 결과를 정규화한 다음 tool 메시지로 추가합니다. 서버의 self-reported 이름이나 설명을 실행 권한의 근거로 사용하지 않습니다. 기본 어댑터는 서버 통신까지만 관찰하며 외부 서버의 모든 내부 함수·파일 I/O를 추적하지 않습니다.

## 계획과 검색

search_text는 실제 파일을 읽어 경로와 행을 반환합니다. set_plan과 complete_plan_step은 planning 스위치에서 공개하며 실행 상태의 계획 표를 갱신합니다. 계획을 done으로 표시한 것과 검증한 작업 결과는 별개입니다. 새로운 도구도 TOOLS의 schema와 IMPLEMENTATIONS의 함수를 함께 추가하세요.

## 지적과 기억 정책

feedback.py가 context.repair에서 보완 문자열을 추출합니다. 완료한 대화의 지적은 세션 JSON에 보관하고 최근 두 대화와 따로 비교합니다. 동일 요청 지적 재포함은 요청 해시가 같은 항목의 최근 세 개를 추가 메시지로 넣는 명시적 정책입니다. 다른 질문의 과거 지적을 무조건 영구 규칙으로 승격하지 않습니다. memory.feedback.audit의 message_indices와 실제 model.request를 대조하세요.

## 모드와 하위 역할

agents.json에서 집중 모드의 호출 예산·점검 지시와 하위 역할별 지침·읽기 도구 허용 목록을 바꿉니다. 선택 모드에 따라 실제로 읽고 적용합니다. 실행·대화 분리·결과 합류 구현은 orchestration.py입니다. 이 기반 모듈은 화면에서는 읽기 전용이므로 외부 편집 후 앱을 재시작하세요. 한 실행에서 위임 한 번, 하위 작업 최대 세 개, 제한된 읽기 도구라는 경계를 함께 확인합니다. [모드 설명](MODES.md)에 입력 전달·미상속 항목·계산 직렬화를 정리했습니다.

화면에서 편집하는 9개 모듈도 이미 서버의 Run 등이 참조 중인 기반 설정 함수를 바꾸는 경우에는 재시작이 필요할 수 있습니다. 다음 실행의 적용 소스와 실제 호출 이벤트를 함께 확인하세요.

## 검증 원칙

```powershell
uv run --extra reference --extra integrations --offline python -m unittest discover -s tests -v
```

모의 모델 테스트는 하네스의 분기와 권한 경계를 검사합니다. 작은 무작위 신경망 테스트는 실제 KV와 전체 재계산의 동등성을 검사합니다. 설치된 실제 모델에서 도구 요청·모델 재호출·캐시 수치를 따로 확인해야 합니다. 테스트 성공과 실제 작업 성공을 같은 뜻으로 쓰지 않습니다.

실행 중 편집 저장은 차단됩니다. 하네스 9개 모듈은 다음 실행에 함께 불러오고, import 실패 시 해당 실행을 실패로 기록합니다. 다른 새 Python 모듈을 만들거나 서버·추론 기반을 바꾸는 작업은 외부 편집기와 앱 재시작을 사용하세요. 모든 Python 코드를 격리된 안전한 샌드박스에서 실행하는 기능은 아닙니다.
