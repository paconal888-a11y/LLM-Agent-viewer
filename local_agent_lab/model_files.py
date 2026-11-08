"""공식 Qwen snapshot의 고정 파일 크기·SHA256. 로드 전에 설정도 검사한다."""

import hashlib

REPOSITORY = "Qwen/Qwen3-0.6B"
REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
FILES = {
    "model.safetensors": (
        1503300328,
        "f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b",
    ),
    "tokenizer.json": (
        11422654,
        "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4",
    ),
    "config.json": (
        726,
        "660db3b73d788119c04535e48cf9be5f55bc3100841a718637ae695b442f27dd",
    ),
    "generation_config.json": (
        239,
        "2325da0f15bb848e018c5ae071b7943332e9f871d6b60e2ed22ca97d4cb993d2",
    ),
    "tokenizer_config.json": (
        9732,
        "d5d09f07b48c3086c508b30d1c9114bd1189145b74e982a265350c923acd8101",
    ),
    "vocab.json": (
        2776833,
        "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
    ),
    "merges.txt": (
        1671853,
        "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
    ),
    "LICENSE": (
        11343,
        "832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e",
    ),
}


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


# 검증 목록 밖에 있어도 허용하는 파일. 그 외의 파일이 모델 폴더에 있으면 로드를 거부한다.
ALLOWED_EXTRA = {"agent-lens-model.json"}
# Transformers가 발견하면 적용하는 설정 파일들. 목록에 없는 이름이 있으면 입력 템플릿·특수 토큰이
# 고정 snapshot과 달라질 수 있으므로 "검증 통과"라고 말할 수 없다.
LOADER_SENSITIVE = {
    "chat_template.jinja",
    "chat_template.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "tokenizer.model",
    "model.safetensors.index.json",
    "additional_chat_templates",
}


def verify_snapshot(directory, files=None):
    expected_files = FILES if files is None else files
    for name, (size, expected) in expected_files.items():
        path = directory / name
        if (
            not path.is_file()
            or path.stat().st_size != size
            or digest(path) != expected
        ):
            raise ValueError(
                "고정 모델 파일 검증 실패: "
                + name
                + ". scripts/setup_reference.py로 다시 준비하세요."
            )
    extra = sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.name not in expected_files
        and entry.name not in ALLOWED_EXTRA
        and not entry.name.endswith(".part")
    )
    if extra:
        raise ValueError(
            "고정 모델 폴더에 검증 목록 밖의 파일이 있습니다: "
            + ", ".join(extra)
            + ". 로더가 이런 파일(예: "
            + ", ".join(sorted(LOADER_SENSITIVE)[:3])
            + ")을 적용하면 입력이 고정 snapshot과 달라질 수 있어 불러오지 않습니다. 파일을 치우거나 scripts/setup_reference.py로 다시 준비하세요."
        )
    return {
        "verified_files": sorted(expected_files),
        "allowed_extra": sorted(ALLOWED_EXTRA),
        "extra_files": extra,
    }
