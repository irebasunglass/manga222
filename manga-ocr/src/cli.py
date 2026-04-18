"""CLIエントリーポイント"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .exceptions import MangaOCRError, NoImagesFoundError, ZipExtractionError
from .output import generate_outputs
from .pipeline import process_images
from .processor import ImageFile, extract_zip, get_image_files
from .screen_capture import ActiveWindowCaptureError, capture_active_window
from .utils import get_device
from .voicevox import VoiceVoxClient, VoiceVoxError, play_wav_bytes


def parse_args() -> argparse.Namespace:
    """コマンドライン引数を解析"""
    parser = argparse.ArgumentParser(
        description=(
            "Zipファイルの漫画画像、またはアクティブウィンドウをOCR処理するCLIツール。"
            "監視モードでは画面変化ごとにOCR+VOICEVOX読み上げを行います。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  %(prog)s comic.zip
  %(prog)s --active-window
  %(prog)s --watch-active-window --poll-interval 0.8
  %(prog)s --watch-active-window --voicevox-speaker 3 --voicevox-speed 1.1
        """.strip(),
    )

    parser.add_argument("zip_file", nargs="?", type=str, help="処理対象のZipファイルのパス")

    parser.add_argument(
        "--active-window",
        action="store_true",
        default=False,
        help="Zipファイルの代わりにアクティブウィンドウを1回だけキャプチャして処理",
    )
    parser.add_argument(
        "--watch-active-window",
        action="store_true",
        default=False,
        help="アクティブウィンドウを監視し、画面変化があるたびにOCR+読み上げを実行",
    )
    parser.add_argument(
        "--capture-delay",
        type=float,
        default=2.0,
        help="キャプチャ開始前の待機秒数（デフォルト: 2.0秒）",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="監視モードでのポーリング間隔（秒、デフォルト: 1.0）",
    )

    parser.add_argument(
        "--voicevox-url",
        type=str,
        default="http://127.0.0.1:50021",
        help="VOICEVOX Engine URL（デフォルト: http://127.0.0.1:50021）",
    )
    parser.add_argument(
        "--voicevox-speaker",
        type=int,
        default=1,
        help="VOICEVOX話者ID（デフォルト: 1）",
    )
    parser.add_argument(
        "--voicevox-speed",
        type=float,
        default=1.0,
        help="VOICEVOX話速（デフォルト: 1.0）",
    )
    parser.add_argument(
        "--no-voice",
        action="store_true",
        default=False,
        help="監視モードでのVOICEVOX読み上げを無効化（OCR結果の表示のみ）",
    )

    parser.add_argument("--output-dir", "-o", type=str, default=None, help="出力ディレクトリ")
    parser.add_argument(
        "--output-format",
        "-f",
        type=str,
        choices=["json", "txt", "both"],
        default="both",
        help="出力形式を指定（デフォルト: both）",
    )
    parser.add_argument("--device", type=str, choices=["auto", "mps", "cpu"], default="auto")
    parser.add_argument("--temp-dir", type=str, default=None)

    parser.add_argument("--skip-errors", action="store_true", default=True)
    parser.add_argument("--no-skip-errors", action="store_false", dest="skip_errors")
    parser.add_argument("--verbose", "-v", action="store_true", default=False)
    parser.add_argument("--quiet", "-q", action="store_true", default=False)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    """引数の妥当性を検証"""
    active_mode = args.active_window or args.watch_active_window
    if args.active_window and args.watch_active_window:
        print("エラー: --active-window と --watch-active-window は同時指定できません", file=sys.stderr)
        sys.exit(1)

    if active_mode:
        if args.zip_file:
            print("エラー: active-window系オプション使用時は zip_file を指定しないでください", file=sys.stderr)
            sys.exit(1)
        if args.capture_delay < 0:
            print("エラー: --capture-delay は0以上を指定してください", file=sys.stderr)
            sys.exit(1)
        if args.poll_interval <= 0:
            print("エラー: --poll-interval は0より大きい値を指定してください", file=sys.stderr)
            sys.exit(1)
        if args.voicevox_speed <= 0:
            print("エラー: --voicevox-speed は0より大きい値を指定してください", file=sys.stderr)
            sys.exit(1)
        return

    if not args.zip_file:
        print("エラー: zip_file を指定するか --active-window / --watch-active-window を指定してください", file=sys.stderr)
        sys.exit(1)

    zip_path = Path(args.zip_file)
    if not zip_path.exists():
        print(f"エラー: Zipファイルが見つかりません: {args.zip_file}", file=sys.stderr)
        sys.exit(1)
    if not zip_path.is_file():
        print(f"エラー: 指定されたパスはファイルではありません: {args.zip_file}", file=sys.stderr)
        sys.exit(1)
    if zip_path.suffix.lower() != ".zip":
        print(f"警告: ファイル拡張子が .zip ではありません: {args.zip_file}", file=sys.stderr)


def _extract_texts(results: list) -> str:
    lines: list[str] = []
    for result in results:
        sorted_results = sorted(result.ocr_results, key=lambda r: r.region.reading_order if r.region else 0)
        for ocr_result in sorted_results:
            text = (ocr_result.text or "").strip()
            if text:
                lines.append(text)
    return "\n".join(lines).strip()


def _process_single_image(path: Path, device: str, skip_errors: bool) -> list:
    image_files = [ImageFile(path=path, filename=path.name, index=1)]
    return process_images(image_files, device=device, skip_errors=skip_errors, detector_model_path=None)


def _process_from_zip(args: argparse.Namespace, device: str) -> tuple[list, Path, str]:
    if args.verbose:
        print(f"Zipファイルを展開中: {args.zip_file}")

    with extract_zip(args.zip_file, args.temp_dir) as extracted_dir:
        image_files = get_image_files(extracted_dir)
        if not args.quiet:
            print(f"画像ファイルを {len(image_files)} 個見つけました")

        results = process_images(
            image_files,
            device=device,
            skip_errors=args.skip_errors,
            detector_model_path=None,
        )

    zip_path = Path(args.zip_file)
    output_dir = Path(args.output_dir) if args.output_dir else zip_path.parent
    return results, output_dir, zip_path.stem


def _process_from_active_window_once(args: argparse.Namespace, device: str) -> tuple[list, Path, str]:
    if not args.quiet:
        print(f"{args.capture_delay:.1f}秒後にアクティブウィンドウをキャプチャします...")
    time.sleep(args.capture_delay)

    with tempfile.TemporaryDirectory(prefix="manga-ocr-active-window-") as tmp:
        capture_path = Path(tmp) / "active_window.png"
        capture_active_window(capture_path)
        results = _process_single_image(capture_path, device=device, skip_errors=args.skip_errors)

    output_dir = Path(args.output_dir) if args.output_dir else Path.cwd()
    base_name = f"active_window_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return results, output_dir, base_name


def _watch_active_window(args: argparse.Namespace, device: str) -> int:
    if not args.quiet:
        print(
            f"{args.capture_delay:.1f}秒後に監視開始します。"
            f"画面変化を {args.poll_interval:.2f}秒間隔で監視します（Ctrl+Cで終了）。"
        )
    time.sleep(args.capture_delay)

    voice_client = VoiceVoxClient(args.voicevox_url)
    if not args.no_voice and not voice_client.health_check():
        raise VoiceVoxError(f"VOICEVOXに接続できません: {args.voicevox_url}")

    previous_hash: str | None = None
    previous_text: str = ""

    with tempfile.TemporaryDirectory(prefix="manga-ocr-watch-") as tmp:
        capture_path = Path(tmp) / "active_window.png"

        while True:
            capture_active_window(capture_path)
            image_bytes = capture_path.read_bytes()
            current_hash = hashlib.sha256(image_bytes).hexdigest()

            if current_hash == previous_hash:
                time.sleep(args.poll_interval)
                continue

            previous_hash = current_hash
            results = _process_single_image(capture_path, device=device, skip_errors=args.skip_errors)
            text = _extract_texts(results)

            if not text or text == previous_text:
                time.sleep(args.poll_interval)
                continue

            previous_text = text

            if not args.quiet:
                stamp = datetime.now().strftime("%H:%M:%S")
                print(f"\n[{stamp}] 検出テキスト:\n{text}\n")

            if not args.no_voice:
                wav = voice_client.synthesize(text, speaker=args.voicevox_speaker, speed_scale=args.voicevox_speed)
                play_wav_bytes(wav)

            time.sleep(args.poll_interval)


def main() -> int:
    """CLIメイン関数"""
    try:
        args = parse_args()
        validate_args(args)

        try:
            device = get_device(args.device)
            if args.verbose:
                print(f"使用デバイス: {device}")
        except RuntimeError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1

        try:
            if args.watch_active_window:
                return _watch_active_window(args, device)
            if args.active_window:
                results, output_dir, base_name = _process_from_active_window_once(args, device)
            else:
                results, output_dir, base_name = _process_from_zip(args, device)

            if not args.quiet:
                print(f"\n処理完了: {len(results)} ページを処理しました")

            output_dir.mkdir(parents=True, exist_ok=True)
            output_files = generate_outputs(results, output_dir, base_name, args.output_format)

            if not args.quiet:
                print("\n出力ファイル:")
                for output_file in output_files:
                    print(f"  - {output_file}")

            return 0

        except (ActiveWindowCaptureError, VoiceVoxError, ZipExtractionError, NoImagesFoundError) as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1

    except KeyboardInterrupt:
        print("\n処理が中断されました", file=sys.stderr)
        return 130
    except MangaOCRError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"予期しないエラー: {e}", file=sys.stderr)
        if args.verbose if "args" in locals() else False:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
