"""공식 Qwen 모델을 고정 revision에서 받고 safetensors의 SHA256을 검증한다."""

import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_agent_lab.paths import reference_model_path

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
    "config.json": (726, None),
    "generation_config.json": (239, None),
    "tokenizer_config.json": (9732, None),
    "vocab.json": (2776833, None),
    "merges.txt": (1671853, None),
    "LICENSE": (11343, None),
}


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def download(directory):
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "repository": REPOSITORY,
        "revision": REVISION,
        "license": "Apache-2.0",
        "files": {},
    }
    for name, (size, expected) in FILES.items():
        path = directory / name
        if (
            path.exists()
            and path.stat().st_size == size
            and (not expected or digest(path) == expected)
        ):
            print(f"{name}: ready", flush=True)
        else:
            temporary = path.with_name(path.name + ".part")
            offset = temporary.stat().st_size if temporary.exists() else 0
            if offset > size:
                raise ValueError(f"Unexpected partial download size: {temporary}")
            request = urllib.request.Request(
                f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}",
                headers={"Range": f"bytes={offset}-"} if offset else {},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                if response.status != 206:
                    offset = 0
                with temporary.open("ab" if offset else "wb") as output:
                    written = offset
                    reported = offset
                    while block := response.read(1024 * 1024):
                        output.write(block)
                        written += len(block)
                        if written - reported >= 64 * 1024 * 1024:
                            print(
                                f"{name}: {written / size:.0%} ({written // 1048576} MiB)",
                                flush=True,
                            )
                            reported = written
            if temporary.stat().st_size != size:
                raise ValueError(f"Download size mismatch: {name}")
            if expected and digest(temporary) != expected:
                raise ValueError(f"SHA256 mismatch: {name}")
            os.replace(temporary, path)
        manifest["files"][name] = {"bytes": size, "sha256": digest(path)}
    (directory / "agent-lens-model.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(f"Reference model ready: {directory}", flush=True)


if __name__ == "__main__":
    download(reference_model_path())
