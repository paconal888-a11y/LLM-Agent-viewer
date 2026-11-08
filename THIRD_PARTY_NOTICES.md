# 외부 구성 요소

이 저장소의 자체 코드는 MIT 라이선스입니다. 다운로드하는 모델·실행 파일·Python 패키지에는 각각의 라이선스가 적용됩니다. 모델과 실행 파일을 이 저장소에 재배포하지 않습니다.

| 구성 | 출처 | 라이선스 |
| --- | --- | --- |
| Qwen3-0.6B | [공식 모델 저장소](https://huggingface.co/Qwen/Qwen3-0.6B) | Apache-2.0; 내려받은 모델 폴더에 LICENSE 보관 |
| Qwen3-4B GGUF | [공식 GGUF 저장소](https://huggingface.co/Qwen/Qwen3-4B-GGUF) | Apache-2.0 |
| llama.cpp | [공식 소스](https://github.com/ggml-org/llama.cpp) | MIT; 배포 압축파일의 고지 보존 |
| Transformers | [공식 소스](https://github.com/huggingface/transformers) | Apache-2.0 |
| PyTorch | [공식 소스](https://github.com/pytorch/pytorch) | BSD 계열; 패키지 내 고지 참조 |
| python-lsp-server | [공식 소스](https://github.com/python-lsp/python-lsp-server) | MIT |
| Pyflakes | [공식 소스](https://github.com/PyCQA/pyflakes) | MIT |
| Jedi | [공식 소스](https://github.com/davidhalter/jedi) | MIT |

직접 작성한 Qwen 어댑터는 모델의 공개 대화 템플릿을 사용합니다. Q/K/V 계산과 attention 연산 자체는 Transformers/PyTorch의 구현을 호출합니다. 모델 훈련이나 행렬 연산 라이브러리를 새로 작성한 프로젝트는 아닙니다.
