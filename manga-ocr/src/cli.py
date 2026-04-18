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


def parse_args() -> argparse.Namespace:
    """コマンドライン引数を解析"""
    parser = argparse.ArgumentParser(
        description=(
            "Zipファイルの漫画画像、またはアクティブウィンドウのスクリーンショットをOCR処理するCLIツール"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  %(prog)s comic.zip
  %(prog)s --active-window
  %(prog)s --active-window --capture-delay 3 --output-format txt
  %(prog)s comic.zip -o ./results --verbose
        """.strip(),
    )

    parser.add_argument("zip_file", nargs="?", type=str, help="処理対象のZipファイルのパス")

    parser.add_argument(
        "--active-window",
        action="store_true",
        default=False,
        help="Zipファイルの代わりにアクティブウィンドウをキャプチャして処理",
    )

    parser.add_argument(
        "--capture-delay",
        type=float,
        default=2.0,
        help="アクティブウィンドウをキャプチャするまでの待機秒数（デフォルト: 2.0秒）",
    )

    # 出力関連オプション
    parser.add_argument(
        "--output-dir",
        "-o",
        type=str,
        default=None,
        help="出力ディレクトリを指定（デフォルト: 入力ファイルと同じディレクトリ、active-window時はカレント）",
    )

    parser.add_argument(
        "--output-format",
        "-f",
        type=str,
        choices=["json", "txt", "both"],
        default="both",
        help="出力形式を指定（デフォルト: both）",
    )

    parser.add_argument(
        "--device",
        type=str,
        choices=["auto", "mps", "cpu"],
        default="auto",
        help="使用デバイスを指定（デフォルト: auto）",
    )

    parser.add_argument(
        "--voicevox-url",
        type=str,
        default=None,
        help="一時ディレクトリを指定（デフォルト: システムの一時ディレクトリ）",
    )

    parser.add_argument(
        "--skip-errors",
        action="store_true",
        default=True,
        help="エラーが発生した画像をスキップして処理を継続（デフォルト: 有効）",
    )

    parser.add_argument(
        "--no-skip-errors",
        action="store_false",
        dest="skip_errors",
        help="エラー時に処理を中断",
    )

    parser.add_argument("--verbose", "-v", action="store_true", default=False, help="詳細ログを出力")

    parser.add_argument("--quiet", "-q", action="store_true", default=False, help="エラー以外のログを抑制")

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    """引数の妥当性を検証"""
    if args.active_window:
        if args.zip_file:
            print("エラー: --active-window 使用時は zip_file を指定しないでください", file=sys.stderr)
            sys.exit(1)
        if args.capture_delay < 0:
            print("エラー: --capture-delay は0以上を指定してください", file=sys.stderr)
            sys.exit(1)
        return

    if not args.zip_file:
        print("エラー: zip_file を指定するか --active-window を指定してください", file=sys.stderr)
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


def _process_from_zip(args: argparse.Namespace, device: str) -> tuple[list, Path, str]:
    if args.verbose:
        print(f"Zipファイルを展開中: {args.zip_file}")

    with extract_zip(args.zip_file, args.temp_dir) as extracted_dir:
        image_files = get_image_files(extracted_dir)

        if not args.quiet:
            print(f"画像ファイルを {len(image_files)} 個見つけました")

        if args.verbose:
            print("見つかった画像ファイル:")
            for img in image_files[:10]:
                print(f"  [{img.index}] {img.filename}")
            if len(image_files) > 10:
                print(f"  ... 他 {len(image_files) - 10} 個")

        results = process_images(
            image_files,
            device=device,
            skip_errors=args.skip_errors,
            detector_model_path=None,
        )

    zip_path = Path(args.zip_file)
    output_dir = Path(args.output_dir) if args.output_dir else zip_path.parent
    base_name = zip_path.stem
    return results, output_dir, base_name


def _process_from_active_window(args: argparse.Namespace, device: str) -> tuple[list, Path, str]:
    if not args.quiet:
        print(f"{args.capture_delay:.1f}秒後にアクティブウィンドウをキャプチャします...")
    time.sleep(args.capture_delay)

    with tempfile.TemporaryDirectory(prefix="manga-ocr-active-window-") as tmp:
        capture_path = Path(tmp) / "active_window.png"
        capture_active_window(capture_path)

        if args.verbose:
            print(f"キャプチャ画像: {capture_path}")

        image_files = [ImageFile(path=capture_path, filename=capture_path.name, index=1)]
        results = process_images(
            image_files,
            device=device,
            skip_errors=args.skip_errors,
            detector_model_path=None,
        )

    output_dir = Path(args.output_dir) if args.output_dir else Path.cwd()
    base_name = f"active_window_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return results, output_dir, base_name


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
            if args.active_window:
                results, output_dir, base_name = _process_from_active_window(args, device)
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

            if args.verbose and results:
                total_time = sum(r.processing_time for r in results)
                print(f"\n総処理時間: {total_time:.2f}秒")
                print(f"平均処理時間: {total_time/len(results):.2f}秒/ページ")

            return 0

        except ActiveWindowCaptureError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        except ZipExtractionError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        except NoImagesFoundError as e:
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
