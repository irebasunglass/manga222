"""VOICEVOX連携ユーティリティ"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import requests


class VoiceVoxError(RuntimeError):
    """VOICEVOX関連エラー"""


class VoiceVoxClient:
    """VOICEVOX Engine HTTPクライアント"""

    def __init__(self, base_url: str = "http://127.0.0.1:50021"):
        self.base_url = base_url.rstrip("/")

    def health_check(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/version", timeout=3)
            response.raise_for_status()
            return True
        except Exception:
            return False

    def synthesize(self, text: str, speaker: int = 1, speed_scale: float = 1.0) -> bytes:
        if not text.strip():
            return b""

        try:
            query_resp = requests.post(
                f"{self.base_url}/audio_query",
                params={"text": text, "speaker": speaker},
                timeout=20,
            )
            query_resp.raise_for_status()
            query = query_resp.json()
            query["speedScale"] = speed_scale

            synth_resp = requests.post(
                f"{self.base_url}/synthesis",
                params={"speaker": speaker},
                json=query,
                timeout=60,
            )
            synth_resp.raise_for_status()
            return synth_resp.content
        except requests.RequestException as exc:
            raise VoiceVoxError(f"VOICEVOX音声合成に失敗しました: {exc}") from exc


def _play_with_system_player(path: Path) -> bool:
    system = platform.system()

    if system == "Darwin":
        player = shutil.which("afplay")
        if player:
            subprocess.run([player, str(path)], check=False)
            return True

    if system == "Linux":
        for candidate in ("aplay", "paplay", "ffplay"):
            player = shutil.which(candidate)
            if not player:
                continue
            if candidate == "ffplay":
                subprocess.run(
                    [player, "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
                    check=False,
                )
            else:
                subprocess.run([player, str(path)], check=False)
            return True

    return False


def play_wav_bytes(wav_bytes: bytes) -> None:
    """WAV bytesを再生する。OSごとの手段がなければ例外。"""
    if not wav_bytes:
        return

    system = platform.system()
    if system == "Windows":
        import winsound

        winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
        return

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = Path(tmp.name)

    try:
        # 最低限WAVかをチェック
        with wave.open(str(tmp_path), "rb"):
            pass

        if not _play_with_system_player(tmp_path):
            raise VoiceVoxError(
                "音声再生コマンドが見つかりませんでした。"
                "macOS: afplay / Linux: aplay,paplay,ffplay のいずれかを用意してください。"
            )
    finally:
        tmp_path.unlink(missing_ok=True)
