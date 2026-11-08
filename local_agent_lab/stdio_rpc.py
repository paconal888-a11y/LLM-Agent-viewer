"""실제 자식 프로세스의 JSON-RPC 바이트를 기록한다. MCP와 LSP의 framing은 다르다."""

from __future__ import annotations
import json
import os
import queue
import subprocess
import threading
import time
from .runtime import RuntimeFailure
from .trace import RunStopped

LIMIT = 2 * 1024 * 1024


class RpcError(RuntimeFailure):
    def __init__(self, error):
        self.error = error
        super().__init__(f"JSON-RPC {error.get('code')}: {error.get('message')}")


class StdioRpc:
    def __init__(self, argv, cwd, protocol, timeout=12):
        self.argv = argv
        self.cwd = cwd
        self.protocol = protocol
        self.timeout = timeout
        self.process = None
        self.inbox = queue.Queue()
        self.notifications = []
        self.counter = 0
        self.lock = threading.RLock()
        self.stderr = []

    def start(self, run):
        if self.process and self.process.poll() is None:
            run.emit(
                self.protocol + ".process.reuse",
                "기존 도구 서버 프로세스 사용",
                "같은 서버 프로세스를 재사용합니다. 모델의 KV 캐시 재사용과 다릅니다.",
                category="integrations",
                actor=self.protocol,
                output={"pid": self.process.pid, "argv": self.argv},
            )
            return
        self.inbox = queue.Queue()
        self.notifications = []
        self.process = subprocess.Popen(
            self.argv,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(
            target=self._read, daemon=True, name=self.protocol + "-reader"
        ).start()
        threading.Thread(
            target=self._read_stderr, daemon=True, name=self.protocol + "-stderr"
        ).start()
        run.emit(
            self.protocol + ".process.start",
            "로컬 도구 서버 프로세스 시작",
            "shell을 통하지 않고 설정된 argv로 실제 프로세스를 시작했습니다.",
            category="integrations",
            actor=self.protocol,
            output={
                "pid": self.process.pid,
                "argv": self.argv,
                "framing": "Content-Length"
                if self.protocol == "lsp"
                else "newline-delimited JSON",
            },
        )

    def _read_stderr(self):
        for line in self.process.stderr:
            self.stderr.append(line.decode("utf-8", errors="replace")[:2000])
            self.stderr = self.stderr[-30:]

    def _read(self):
        try:
            stream = self.process.stdout
            while True:
                header = b""
                if self.protocol == "lsp":
                    length = None
                    while True:
                        line = stream.readline(8193)
                        if not line:
                            raise EOFError("서버 stdout 종료")
                        if len(line) > 8192:
                            raise ValueError("헤더 길이 초과")
                        header += line
                        if line in (b"\r\n", b"\n"):
                            break
                        key, _, value = line.partition(b":")
                        if key.lower() == b"content-length":
                            length = int(value.strip())
                    if length is None or not 0 < length <= LIMIT:
                        raise ValueError("잘못된 Content-Length")
                    raw = stream.read(length)
                    if len(raw) != length:
                        raise EOFError("잘린 LSP 본문")
                else:
                    raw = stream.readline(LIMIT + 1)
                    if not raw:
                        raise EOFError("서버 stdout 종료")
                    if len(raw) > LIMIT or not raw.endswith(b"\n"):
                        raise ValueError("잘린 MCP 메시지 또는 길이 초과")
                decoded = raw.decode("utf-8")
                message = json.loads(decoded)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError("JSON-RPC 객체가 아닙니다.")
                self.inbox.put(
                    {
                        "message": message,
                        "raw_text": decoded,
                        "header": header.decode("ascii"),
                        "wire_bytes": len(header) + len(raw),
                        "received_at": time.perf_counter(),
                    }
                )
        except Exception as error:
            self.inbox.put({"reader_error": str(error)})

    def send(self, run, message, parent=None):
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(raw) > LIMIT:
            raise RuntimeFailure("RPC 요청 크기 초과")
        header = (
            f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
            if self.protocol == "lsp"
            else b""
        )
        packet = header + raw + (b"\n" if self.protocol == "mcp" else b"")
        self.process.stdin.write(packet)
        self.process.stdin.flush()
        run.emit(
            self.protocol + ".send",
            self.protocol.upper() + " 원본 메시지 전송",
            "실제 stdin에 쓴 UTF-8 메시지와 framing입니다. 서버가 실행한 결과와 구분합니다.",
            category="integrations",
            actor=self.protocol,
            parent=parent,
            input={"message": message},
            output={
                "header": header.decode("ascii"),
                "raw_text": raw.decode("utf-8"),
                "wire_bytes": len(packet),
                "pid": self.process.pid,
            },
        )

    def receive(self, run, deadline, parent=None):
        while time.monotonic() < deadline:
            if run.stopped:
                raise RunStopped()
            try:
                item = self.inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if "reader_error" in item:
                raise RuntimeFailure(
                    item["reader_error"] + " " + "".join(self.stderr)[-1500:]
                )
            run.emit(
                self.protocol + ".receive",
                self.protocol.upper() + " 원본 메시지 수신",
                "stdout에서 실제 읽은 바이트를 JSON으로 해석했습니다. 서버의 통지와 요청 ID가 있는 응답을 구분합니다.",
                category="integrations",
                actor=self.protocol,
                parent=parent,
                output={
                    **{
                        key: value
                        for key, value in item.items()
                        if key != "received_at"
                    },
                    "reader_received_elapsed_ms": round(
                        (item["received_at"] - run.started) * 1000, 3
                    ),
                    "queue_wait_ms": round(
                        (time.perf_counter() - item["received_at"]) * 1000, 3
                    ),
                },
            )
            return item
        raise RuntimeFailure(self.protocol.upper() + " 서버 응답 시간 초과")

    def request(self, run, method, params=None, parent=None):
        with self.lock:
            self.counter += 1
            identity = self.counter
            started = time.perf_counter()
            self.send(
                run,
                {
                    "jsonrpc": "2.0",
                    "id": identity,
                    "method": method,
                    "params": params or {},
                },
                parent,
            )
            deadline = time.monotonic() + self.timeout
            while True:
                item = self.receive(run, deadline, parent)
                message = item["message"]
                if "method" in message:
                    if "id" in message:
                        # LSP 서버의 configuration 질의. MCP 2026에서는 서버 요청을 허용하지 않는다.
                        if (
                            self.protocol == "lsp"
                            and message["method"] == "workspace/configuration"
                        ):
                            self.send(
                                run,
                                {
                                    "jsonrpc": "2.0",
                                    "id": message["id"],
                                    "result": [
                                        {}
                                        for _ in message.get("params", {}).get(
                                            "items", []
                                        )
                                    ],
                                },
                                parent,
                            )
                        else:
                            self.send(
                                run,
                                {
                                    "jsonrpc": "2.0",
                                    "id": message["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "Unsupported server request",
                                    },
                                },
                                parent,
                            )
                    else:
                        self.notifications.append(item)
                        self.notifications = self.notifications[-100:]
                    continue
                if message.get("id") != identity:
                    raise RuntimeFailure("RPC 응답 ID가 현재 요청과 다릅니다.")
                if "error" in message:
                    raise RpcError(message["error"])
                run.emit(
                    self.protocol + ".roundtrip",
                    "요청과 응답 연결",
                    "요청 ID로 실제 응답을 연결했습니다. 시간은 관찰 정지를 포함할 수 있습니다.",
                    category="integrations",
                    actor=self.protocol,
                    parent=parent,
                    output={
                        "method": method,
                        "id": identity,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    },
                )
                return message.get("result")

    def notify(self, run, method, params=None, parent=None):
        self.send(
            run, {"jsonrpc": "2.0", "method": method, "params": params or {}}, parent
        )

    def wait_notification(self, run, predicate, parent=None):
        deadline = time.monotonic() + self.timeout
        for index, item in enumerate(self.notifications):
            if predicate(item["message"]):
                return self.notifications.pop(index)["message"]
        while True:
            item = self.receive(run, deadline, parent)
            if predicate(item["message"]):
                return item["message"]
            self.notifications.append(item)

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(self.process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        timeout=4,
                    )
                else:
                    self.process.kill()
                self.process.wait(timeout=2)
        if self.process:
            for stream in (
                self.process.stdin,
                self.process.stdout,
                self.process.stderr,
            ):
                if stream:
                    stream.close()
