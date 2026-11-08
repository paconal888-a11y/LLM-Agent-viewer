"""구성 설명. 각 문장은 이 프로젝트의 실제 구현 범위를 설명한다."""

EXPLANATIONS = {
    "compaction": {
        "purpose": "오래된 대화 원문 일부를 로컬 모델로 요약해 다음 입력에 넣습니다.",
        "when": "대화 요약을 켜면 새 요청을 시작할 때 memory.json의 문자 기준을 검사합니다. 요약은 별도 실제 모델 호출이며 원문은 삭제하지 않습니다.",
        "edit_path": "demo_workspace/memory.json",
        "exercise": "대화 저장소에서 원문·요약·실제 messages에 같은 문자열이 남았는지 대조하세요.",
        "decided": {
            "code": "요약 여부는 memory.json의 문자 기준으로 판단하고 요약 범위와 원문 보존도 코드가 정합니다.",
            "model": "요약 문장의 내용",
            "change": "이전 대화 요약 스위치, demo_workspace/memory.json",
        },
    },
    "agents": {
        "purpose": "모드에 따라 실제 역할 지침·권한·예산·대화 구조를 바꿉니다.",
        "when": "계획은 쓰기를 막고, 집중은 같은 대화에서 추가 점검하며, 위임은 별도 작업자와 messages를 생성합니다. 같은 가중치를 사용해도 같은 대화는 아닙니다.",
        "edit_path": "demo_workspace/agents.json",
        "exercise": "같은 요청을 단일·집중·위임으로 비교하세요. agent.mode·spawn·queue·join과 주 대화에 들어간 결과를 확인합니다. 작업자 동시 실행과 모델 동시 추론을 구분하세요.",
        "decided": {
            "code": "모드별 권한·호출 예산, 하위 대화 격리, 보고 수집",
            "model": "delegate_tasks로 위임할지와 작업 문구, 각 에이전트의 답변",
            "change": "에이전트 모드, demo_workspace/agents.json",
        },
    },
    "search": {
        "purpose": "작업 공간의 텍스트 파일을 실제로 읽고 검색어가 있는 경로·행을 반환합니다.",
        "when": "읽기 도구가 켜져 있으면 공개합니다. 파일 50개·512 KiB·결과 30개 한도로 검색하며 제한 여부도 기록합니다.",
        "edit_path": "local_agent_lab/toolkit.py",
        "exercise": "검색 결과의 경로와 행 번호를 실제 파일 I/O 기록과 대조하세요.",
        "decided": {
            "code": "파일 50개·512 KiB·결과 30개 한도와 경로 제한",
            "model": "검색할지, 무엇을 어느 범위에서 찾을지",
            "change": "local_agent_lab/toolkit.py",
        },
    },
    "planning": {
        "purpose": "모델이 명시한 계획과 항목 상태를 실행 상태에 보관합니다.",
        "when": "작업 계획 스위치를 켜면 set_plan·complete_plan_step을 공개합니다. 계획 체크는 모델이 보고한 상태이며 실제 작업 검증은 별도 게이트가 담당합니다.",
        "edit_path": "local_agent_lab/toolkit.py",
        "exercise": "계획의 done 표시와 read_file·검사 결과를 따로 확인하세요.",
        "decided": {
            "code": "계획 도구 공개와 상태 보관. 실제 작업 검증은 별도 게이트가 합니다.",
            "model": "계획 항목과 완료 표시",
            "change": "작업 계획 도구 스위치, local_agent_lab/toolkit.py",
        },
    },
    "loop": {
        "purpose": "모델 응답을 답변과 도구 요청으로 구분하고, 필요하면 도구 결과를 넣어 다시 호출합니다.",
        "when": "요청을 받은 뒤 완료·중지·오류가 날 때까지 반복합니다.",
        "edit_path": "local_agent_lab/agent_loop.py",
        "exercise": "도구 결과가 추가된 다음 모델 호출의 messages를 확인하세요.",
        "decided": {
            "code": "응답을 답변과 도구 요청으로 나누는 분기, 호출 예산, 반복 중단",
            "model": "이번 응답에서 답할지 도구를 요청할지",
            "change": "local_agent_lab/agent_loop.py, 에이전트 모드의 호출 예산",
        },
    },
    "rules": {
        "purpose": "하네스가 정해진 파일을 읽어 모델의 system 메시지에 넣는 프로젝트 지침입니다.",
        "when": "매 요청에 AGENTS.md, 없으면 agent.md를 찾습니다. 모델이 파일명을 저절로 아는 것은 아닙니다.",
        "edit_path": "demo_workspace/AGENTS.md",
        "exercise": "답변 언어 규칙을 바꾸고 입력 지침과 실제 응답을 비교하세요.",
        "decided": {
            "code": "AGENTS.md, 없으면 agent.md를 찾아 system 메시지에 넣는 규칙",
            "model": "지침을 실제로 따를지",
            "change": "AGENTS.md 내용과 스위치",
        },
    },
    "memory": {
        "purpose": "완료한 대화와 직전 답변 발췌를 저장하고 다음 요청에 다시 넣습니다.",
        "when": "완료 대화 원본은 JSONL에 추가합니다. 기본은 최근 두 대화와 답변 발췌를 선택하며 요약을 켜면 별도 선택 정책을 사용합니다. 모델 학습·KV 캐시와 다른 데이터입니다.",
        "edit_path": "local_agent_lab/harness.py",
        "exercise": "후속 질문을 한 뒤 이력을 끄거나 새 세션으로 비교하세요.",
        "decided": {
            "code": "완료 대화 저장, 최근 두 대화와 답변 발췌 선택",
            "model": "다시 넣은 내용을 답변에 어떻게 쓸지",
            "change": "대화 이력 스위치, local_agent_lab/harness.py",
        },
    },
    "skills": {
        "purpose": "작업 절차를 파일에 작성해 두고 필요한 스킬의 본문을 불러옵니다.",
        "when": "이름·설명 목록을 먼저 넣고 모델이 load_skill을 요청하면 본문을 도구 결과로 전달합니다.",
        "edit_path": "demo_workspace/skills/file-summary/SKILL.md",
        "exercise": "스킬 목록과 본문이 처음 모델 입력에 들어가는 시점을 각각 찾아보세요.",
        "decided": {
            "code": "skills/*/SKILL.md를 찾아 이름·설명 목록을 입력에 넣는 규칙",
            "model": "load_skill로 본문을 불러올지",
            "change": "SKILL.md 내용과 스킬 스위치",
        },
    },
    "commands": {
        "purpose": "/summary 같은 입력을 정해 둔 프롬프트 템플릿으로 확장합니다.",
        "when": "셸 실행이 아닙니다. $ARGUMENTS를 사용자 인자로 바꿉니다.",
        "edit_path": "demo_workspace/commands/summary.md",
        "exercise": "/summary notes/agent-guide.md의 원문과 확장된 요청을 비교하세요.",
        "decided": {
            "code": "/이름 해석과 $ARGUMENTS 치환",
            "model": "없음. 확장된 요청을 입력으로 받습니다.",
            "change": "demo_workspace/commands/*.md, /명령어 스위치",
        },
    },
    "context": {
        "purpose": "규칙·이력·스킬 목록·요청을 역할이 있는 messages 배열로 조립합니다.",
        "when": "파일을 읽은 사실과 그 내용을 모델에 넣은 사실은 다릅니다. 이 코드가 포함할 내용을 정합니다.",
        "edit_path": "local_agent_lab/context.py",
        "exercise": "규칙이나 이력을 끈 뒤 messages와 토큰 수를 비교하세요.",
        "decided": {
            "code": "규칙·이력·스킬 목록·요청의 순서와 포함 범위",
            "model": "없음. 조립된 입력을 받습니다.",
            "change": "local_agent_lab/context.py와 각 스위치",
        },
    },
    "files": {
        "purpose": "경로 확인, 존재 검사, 열기, 바이트 읽기, 닫기, UTF-8 해석을 기록합니다.",
        "when": "같은 파일 함수를 써도 자동 규칙 읽기와 모델이 요청한 도구 읽기는 호출 주체가 다릅니다.",
        "edit_path": "local_agent_lab/files.py",
        "exercise": "규칙 로더와 read_file 도구의 실제 호출 경로를 비교하세요.",
        "decided": {
            "code": "경로 제한, 바이트 읽기, UTF-8 해석, 자동 로딩 순서",
            "model": "read_file 도구로 어떤 파일을 읽을지(자동 로딩은 코드)",
            "change": "local_agent_lab/files.py",
        },
    },
    "tools": {
        "purpose": "모델이 요청한 도구 이름을 등록된 Python 함수에 연결합니다.",
        "when": "모델은 요청을 만듭니다. 하네스가 검사하고 함수를 호출해야 실제 행동이 일어납니다.",
        "edit_path": "local_agent_lab/toolkit.py",
        "exercise": "add_numbers 구현과 schema, 함수 반환값이 모델에 전달되는 과정을 확인하세요.",
        "decided": {
            "code": "공개할 도구 목록, 인자·경로 검사, 함수 실행",
            "model": "어떤 도구를 어떤 인자로 부를지",
            "change": "local_agent_lab/toolkit.py, 도구·파일 쓰기 스위치",
        },
    },
    "gate": {
        "purpose": "자연어 지침과 별도로 코드에서 실행·완료 조건을 강제합니다.",
        "when": "공개 도구·인자·경로와 수정 파일을 검사합니다. 조건이 남은 채 같은 응답을 세 번 반복하고 새 도구 호출이 없으면 실패로 중단합니다.",
        "edit_path": "local_agent_lab/policy.py",
        "exercise": "JSON 수정 후 check_file 결과와 최종 완료 검사를 확인하세요.",
        "decided": {
            "code": "도구 인자·완료 조건·수정 파일 검사와 완료 판정",
            "model": "없음. 판정에 참여하지 않습니다.",
            "change": "local_agent_lab/policy.py, 완료 조건 입력",
        },
    },
    "tokenizer": {
        "purpose": "메시지를 모델용 문자열로 바꾸고 토큰 조각·ID로 변환합니다.",
        "when": "토큰은 단어 번호가 아닙니다. 한글의 일부 바이트만 담은 토큰도 있습니다.",
        "edit_path": "local_agent_lab/inference.py",
        "exercise": "자신의 요청 토큰을 골라 조각·ID·UTF-8 바이트를 확인하세요.",
        "decided": {
            "code": "실행기가 모델의 템플릿과 tokenizer로 계산합니다.",
            "model": "없음",
            "change": "모델 파일(템플릿·tokenizer는 모델에 포함)",
        },
    },
    "model": {
        "purpose": "교육용은 Python에서 forward와 샘플링을 직접 제어하고, 빠른 실행기는 로컬 HTTP로 연결합니다.",
        "when": "실행기에 따라 관측할 수 있는 값이 다릅니다. 실측값과 개념 설명을 구분합니다.",
        "edit_path": "local_agent_lab/inference.py",
        "exercise": "forward 입력 길이, Q/K/V 표본, 선택 토큰을 순서대로 확인하세요.",
        "decided": {
            "code": "생성 파라미터 전달과 응답 수신",
            "model": "다음 토큰 선택과 그 결과인 답변·도구 요청",
            "change": "실행 설정(temperature·top_k·top_p·seed·토큰 한도), 모델 파일",
        },
    },
    "kv": {
        "purpose": "이전 위치의 K와 V 계산 상태를 보관합니다. 대화 파일·모델 가중치와 별개입니다.",
        "when": "요청 간 접두부 재사용과 한 답변을 생성하는 동안의 KV 사용은 다른 설정입니다.",
        "edit_path": "local_agent_lab/reference_engine.py",
        "exercise": "교육용에서 생성 중 KV를 켜고 끈 뒤 forward 입력 길이와 누적 계산량을 비교하세요.",
        "decided": {
            "code": "실행기의 계산 캐시. 재사용 여부는 실행 설정이 정합니다.",
            "model": "없음",
            "change": "실행 설정의 생성 중 KV·접두부 재사용",
        },
    },
    "mcp": {
        "purpose": "외부 도구 서버와 기능 목록·호출·결과를 주고받는 연결 규약입니다.",
        "when": "연결을 켜면 별도 프로세스의 server/discover·tools/list를 호출하고 도구 schema를 등록합니다. 모델 요청은 tools/call로 전달합니다. 최신 2026-07-28 stdio 도구 경로와 이전 initialize 연결 경로를 구분합니다.",
        "edit_path": "demo_workspace/integrations.json",
        "exercise": "MCP 계산 예제와 연결 전후 비교에서 원본 JSON-RPC, 프로세스 ID, schema, 추가 입력 토큰을 확인하세요. 현재 어댑터는 단순 스칼라 도구 인자를 지원합니다.",
        "decided": {
            "code": "allow_tools·read_only_tools 필터, 단순 스칼라 인자 도구만 등록, tools/call 전달",
            "model": "공개된 MCP 도구를 부를지와 인자",
            "change": "demo_workspace/integrations.json, MCP 스위치",
        },
    },
    "lsp": {
        "purpose": "언어 서버에 코드 진단·정의·참조 같은 정보를 질의하는 규약입니다.",
        "when": "실제 python-lsp-server를 시작하고 문서 URI·버전을 동기화합니다. 진단·문서 심볼·정의 요청을 별도 프로토콜로 보냅니다.",
        "edit_path": "demo_workspace/integrations.json",
        "exercise": "lsp_demo.py는 구문 검사를 통과합니다. LSP가 반환한 missing_amount 진단과 모델의 답변을 비교하세요. 원본 Content-Length framing과 JSON을 확인할 수 있습니다.",
        "decided": {
            "code": "언어 서버 시작, 문서 동기화, 응답 정리",
            "model": "진단·심볼 도구를 부를지",
            "change": "demo_workspace/integrations.json, LSP 스위치",
        },
    },
}
