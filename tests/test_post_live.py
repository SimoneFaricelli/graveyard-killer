"""Opt-in: the real tool chain on a public YouTube video. Run with `uv run pytest -m live`.

Needs network and the real tools (see `post.py check`), and an API key per provider for
the speech-to-text tests. Never touches Instagram.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SCRIPT

VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def run_post(*args: str) -> dict[str, object]:
    done = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)
    result: dict[str, object] = json.loads(done.stdout)
    return result


@pytest.mark.live
def test_the_real_tools_read_a_public_youtube_video() -> None:
    tools = run_post("check")
    if not tools["ok"]:
        pytest.skip(f"missing tools: {tools['missing']}")

    result = run_post("read", VIDEO)
    try:
        assert result["status"] == "ok", result.get("reason")
        assert result["link"] == VIDEO
        assert result["author"] == "Rick Astley"
        assert result["transcript"]
        assert result["keyframes"]
        assert Path(str(result["thumbnail"])).is_file()
    finally:
        run_post("clean", str(result["work"]))


SPEECH = "Buongiorno a tutti. Oggi cuciniamo i ceci croccanti al forno con rosmarino e paprika."


@pytest.fixture(scope="module")
def spoken_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A short Italian sentence, spoken by espeak-ng, as an mp4 with a plain picture."""
    if not (shutil.which("espeak-ng") and shutil.which("ffmpeg")):
        pytest.skip("espeak-ng and ffmpeg are needed to build a spoken clip")
    folder = tmp_path_factory.mktemp("speech")
    wav = folder / "speech.wav"
    subprocess.run(["espeak-ng", "-v", "it", "-s", "140", "-w", str(wav), SPEECH], check=True)
    clip = folder / "speech.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=gray:s=320x240:r=25", "-i", str(wav),
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)], check=True)
    return clip


@pytest.mark.live
@pytest.mark.parametrize("provider", ["deepgram", "openai"])
def test_the_real_provider_transcribes_speech(spoken_clip: Path, tmp_path: Path, provider: str) -> None:
    if not run_post("check", "--provider", provider)["keys"][provider]:  # type: ignore[index]
        pytest.skip(f"no API key for {provider}")
    code = (
        "import json, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import post; "
        "post.PROVIDERS = (sys.argv[4],); "  # this provider alone: no fallback hides a failure
        "print(json.dumps(post.transcribe(Path(sys.argv[2]), Path(sys.argv[3]), 'it', sys.argv[4])))"
    )
    done = subprocess.run([sys.executable, "-c", code, str(SCRIPT.parent), str(spoken_clip),
                           str(tmp_path / "out"), provider], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-500:]
    text, language, source = json.loads(done.stdout)
    assert source == provider
    assert language == "it"
    assert "ceci" in text.lower()
