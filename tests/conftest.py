"""Fixtures for the post script: fake network tools, a fake speech-to-text API, real ffmpeg, built media."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

SCRIPT = Path(__file__).parent.parent / "plugin/skills/graveyard-killer/scripts/post.py"
FAKE = Path(__file__).parent / "fakes/fake_tool.py"
FAKE_NAMES = ("yt-dlp", "gallery-dl", "uv")
DEEPGRAM_KEY = "dg-test-key-0123456789"
OPENAI_KEY = "sk-test-key-0123456789"


def link_or_copy(source: str | Path, target: Path) -> None:
    """A hard link needs no privilege on Windows, unlike a symlink, and is not a new file:
    a fresh copy of an executable in every test is slow, and Defender scans each one."""
    try:
        os.link(source, target)
    except OSError:  # another volume
        shutil.copy(source, target)


@pytest.fixture(scope="session")
def fake_launcher(tmp_path_factory: pytest.TempPathFactory) -> Path | None:
    """On Windows, the fake tool wrapped once in a real .exe.

    post.py starts only .exe tools on Windows, never through cmd.exe. distlib's launcher,
    the one pip uses for console scripts, runs the fake with Python and passes the
    arguments untouched.
    """
    if os.name != "nt":
        return None
    from distlib.scripts import ScriptMaker

    maker = ScriptMaker(str(FAKE.parent), str(tmp_path_factory.mktemp("launcher")))
    maker.executable = sys.executable
    maker.variants = {""}
    return Path(maker.make(FAKE.name)[0])


def install_fakes(bin_dir: Path, launcher: Path | None) -> None:
    """Put one fake tool per name in bin_dir, runnable the way post.py runs tools."""
    for name in FAKE_NAMES:
        if launcher is None:
            target = bin_dir / name
            shutil.copy(FAKE, target)
            target.chmod(0o755)
        else:
            link_or_copy(launcher, bin_dir / f"{name}.exe")


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Small real media files, built once per test session."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is needed to build test media")
    d = tmp_path_factory.mktemp("media")
    # Three scenes of 2 seconds each, with a tone, so there are scene changes and audio.
    ffmpeg(
        "-f", "lavfi", "-i", "color=red:s=1280x720:d=2:r=25",
        "-f", "lavfi", "-i", "color=green:s=1280x720:d=2:r=25",
        "-f", "lavfi", "-i", "color=blue:s=1280x720:d=2:r=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(d / "scenes.mp4"),
    )
    # Twelve half-second scenes in twelve different colours.
    ffmpeg("-f", "lavfi", "-i", "color=black:s=320x240:d=6:r=25,format=rgb24,"
           "geq=r='mod(floor(T*2)*40,256)':g='mod(floor(T*2)*90,256)':b='mod(floor(T*2)*150,256)'",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "twelve_scenes.mp4"))
    ffmpeg("-f", "lavfi", "-i", "color=gray:s=640x360:d=4:r=25",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "silent.mp4"))
    ffmpeg("-f", "lavfi", "-i", "color=orange:s=640x640", "-frames:v", "1", str(d / "thumb.jpg"))
    # Noise compresses badly, so this JPEG is well over Notion's 5 MiB limit.
    ffmpeg("-f", "lavfi", "-i", "nullsrc=s=4000x4000,geq=random(1)*255:random(2)*255:random(3)*255",
           "-frames:v", "1", "-q:v", "1", str(d / "big_thumb.jpg"))
    for name, color in (("slide1.jpg", "white"), ("slide2.jpg", "black"), ("slide3.jpg", "yellow")):
        ffmpeg("-f", "lavfi", "-i", f"color={color}:s=1080x1350", "-frames:v", "1", str(d / name))
    return {p.stem: p for p in d.iterdir()}


@dataclass
class ApiRequest:
    """One call to the fake speech-to-text API."""

    provider: str  # "deepgram" or "openai"
    query: dict[str, str]
    headers: dict[str, str]
    fields: dict[str, str]  # OpenAI's form fields, the file left out
    audio_bytes: int


@dataclass
class FakeApi:
    """Deepgram and OpenAI on one local port. Each call plays the provider's next response:

        {"deepgram": [{"status": 401}, {"text": "...", "language": "it"}], "openai": [{"text": "..."}]}

    The last response repeats. A response may hold status (200 when left out), text and language.
    """

    url: str = ""
    responses: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    requests: list[ApiRequest] = field(default_factory=list)

    def calls(self, provider: str) -> list[ApiRequest]:
        return [r for r in self.requests if r.provider == provider]


def multipart_fields(body: bytes, content_type: str) -> tuple[dict[str, str], int]:
    boundary = content_type.split("boundary=")[1].encode()
    fields: dict[str, str] = {}
    audio = 0
    for part in body.split(b"--" + boundary):
        head, _, value = part.partition(b"\r\n\r\n")
        if b"name=" not in head:
            continue
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        value = value[:-2] if value.endswith(b"\r\n") else value
        if name == "file":
            audio = len(value)
        else:
            fields[name] = value.decode()
    return fields, audio


@pytest.fixture
def api() -> Iterator[FakeApi]:
    fake = FakeApi()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            provider = "deepgram" if parsed.path == "/v1/listen" else "openai"
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            fields, audio = ({}, len(body)) if provider == "deepgram" else multipart_fields(
                body, self.headers.get("Content-Type", ""))
            fake.requests.append(ApiRequest(provider, {k: v[0] for k, v in parse_qs(parsed.query).items()},
                                            dict(self.headers.items()), fields, audio))
            played = fake.responses.get(provider) or [{"status": 500}]
            response = played[min(len(fake.calls(provider)) - 1, len(played) - 1)]
            status = response.get("status", 200)
            if status != 200:
                answer: dict[str, Any] = {"error": {"message": f"Incorrect API key provided: {OPENAI_KEY[:6]}***"}}
            elif provider == "deepgram":
                channel: dict[str, Any] = {"alternatives": [{"transcript": response.get("text", "")}]}
                if "language" in response:
                    channel["detected_language"] = response["language"]
                answer = {"results": {"channels": [channel]}}
            else:
                answer = {"text": response.get("text", "")}
            data = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield fake
    server.shutdown()
    server.server_close()


@dataclass
class Run:
    exit_code: int
    output: dict[str, Any]
    calls: list[dict[str, Any]]
    requests: list[ApiRequest] = field(default_factory=list)

    def called(self, tool: str) -> list[list[str]]:
        return [c["argv"] for c in self.calls if c["tool"] == tool]

    def asked(self, provider: str) -> list[ApiRequest]:
        return [r for r in self.requests if r.provider == provider]


@pytest.fixture
def post(tmp_path: Path, fake_launcher: Path | None, api: FakeApi) -> Iterator[Callable[..., Run]]:
    """Run the post script as the agent does, with a scenario for the fake tools and the fake API.

    The scenario's "deepgram" and "openai" entries are the fake API's responses. Both keys
    are set unless env sets one to "" or unset removes it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_fakes(bin_dir, fake_launcher)
    for real in ("ffmpeg", "ffprobe"):
        found = shutil.which(real)
        if found:
            link = bin_dir / Path(found).name  # keeps .exe on Windows
            try:
                link.symlink_to(found)
            except OSError:  # Windows makes symlinks only in Developer Mode or as an admin
                link_or_copy(found, link)
    log = tmp_path / "calls.jsonl"

    def run(
        *args: str,
        scenario: dict[str, Any] | None = None,
        without: tuple[str, ...] = (),
        stdin: str = "",
        env: dict[str, str] | None = None,
        unset: tuple[str, ...] = (),
    ) -> Run:
        for name in without:
            for path in (name, f"{name}.exe"):
                (bin_dir / path).unlink(missing_ok=True)
        scenario = dict(scenario or {})
        api.responses = {name: scenario.pop(name, [{"text": ""}]) for name in ("deepgram", "openai")}
        api.requests.clear()
        scenario_file = tmp_path / "scenario.json"
        scenario_file.write_text(json.dumps(scenario))
        log.write_text("")
        temp = tmp_path / "temp"  # work folders land here, inside pytest's own cleaned-up folder
        temp.mkdir(exist_ok=True)
        full_env = {
            **os.environ,
            "TMPDIR": str(temp),
            "DEEPGRAM_API_KEY": DEEPGRAM_KEY,
            "OPENAI_API_KEY": OPENAI_KEY,
            "GK_KEYS_FILE": str(tmp_path / "keys.env"),
            "GK_DEEPGRAM_URL": api.url,
            "GK_OPENAI_URL": api.url,
            **(env or {}),
            "PATH": os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent)]),
            "GK_FAKE_SCENARIO": str(scenario_file),
            "GK_FAKE_LOG": str(log),
        }
        for name in unset:
            full_env.pop(name, None)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True, env=full_env
        )
        assert result.stdout, result.stderr
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return Run(result.returncode, json.loads(result.stdout), calls, list(api.requests))

    yield run
