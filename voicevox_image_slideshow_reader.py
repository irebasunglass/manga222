
import io
import os
import threading
import tempfile
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageTk

try:
    import easyocr
except Exception:
    easyocr = None

try:
    import pyperclip
except Exception:
    pyperclip = None

try:
    from playsound import playsound
except Exception:
    playsound = None


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


class VoiceVoxClient:
    def __init__(self, base_url: str = "http://127.0.0.1:50021"):
        self.base_url = base_url.rstrip("/")

    def health_check(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/version", timeout=3)
            r.raise_for_status()
            return True
        except Exception:
            return False

    def get_speakers(self):
        r = requests.get(f"{self.base_url}/speakers", timeout=10)
        r.raise_for_status()
        data = r.json()
        result = []
        for speaker in data:
            speaker_name = speaker.get("name", "Unknown")
            for style in speaker.get("styles", []):
                result.append(
                    {
                        "label": f"{speaker_name} / {style.get('name', 'default')} ({style.get('id')})",
                        "id": style.get("id"),
                    }
                )
        return result

    def synthesize(self, text: str, speaker_id: int, speed_scale: float = 1.0) -> bytes:
        params = {"text": text, "speaker": speaker_id}
        r = requests.post(f"{self.base_url}/audio_query", params=params, timeout=20)
        r.raise_for_status()
        query = r.json()
        query["speedScale"] = speed_scale

        r = requests.post(
            f"{self.base_url}/synthesis",
            params={"speaker": speaker_id},
            json=query,
            timeout=60,
        )
        r.raise_for_status()
        return r.content


class OCRReader:
    def __init__(self):
        self.reader = None

    def ensure_reader(self):
        if easyocr is None:
            raise RuntimeError(
                "easyocr が見つかりません。`pip install easyocr` を実行してください。"
            )
        if self.reader is None:
            self.reader = easyocr.Reader(["ja", "en"], gpu=True)

    def _variants(self, image: Image.Image):
        variants = []
        base = image.convert("L")

        # コントラスト強化
        contrast = ImageEnhance.Contrast(base).enhance(2.2)

        # 拡大
        for scale in (1.8, 2.5, 3.0):
            w, h = contrast.size
            resized = contrast.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

            # そのまま
            variants.append(("normal", resized))

            # シャープ化
            sharp = resized.filter(ImageFilter.SHARPEN)
            variants.append(("sharp", sharp))

            # 二値化
            bw = sharp.point(lambda p: 255 if p > 180 else 0)
            variants.append(("bw", bw))

            # 反転二値化
            inv = ImageOps.invert(sharp)
            inv_bw = inv.point(lambda p: 255 if p > 180 else 0)
            variants.append(("inv_bw", inv_bw))

        return variants

    def _score_text(self, text: str) -> int:
        t = text.strip()
        if not t:
            return 0
        score = len(t.replace(" ", "").replace("\n", ""))
        # 日本語っぽい文字を少し優遇
        jp_bonus = sum(1 for ch in t if (
            "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff"
        ))
        # 記号だらけは減点
        sym_penalty = sum(1 for ch in t if ch in "|/\\[]{}<>_-=+*~")
        return score + jp_bonus * 2 - sym_penalty * 2

    def image_to_text(self, image_path: str) -> str:
        self.ensure_reader()

        img = Image.open(image_path).convert("RGB")
        candidates = []

        for _, variant in self._variants(img):
            for angle in (0, 90):
                rotated = variant.rotate(angle, expand=True)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    rotated.save(tmp_path)
                    results = self.reader.readtext(tmp_path, detail=0, paragraph=False)
                    text = "\n".join(str(x).strip() for x in results if str(x).strip())
                    candidates.append((self._score_text(text), text))
                finally:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        if not candidates:
            return ""

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1].strip()


class MangaSlideShowReaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VOICEVOX 画像スライドショー読み上げ")
        self.root.geometry("1180x820")

        self.voicevox = VoiceVoxClient()
        self.ocr = OCRReader()
        self.speakers = []

        self.image_paths = []
        self.index = -1
        self.current_preview = None
        self.current_image = None
        self.last_audio_file = None
        self.slideshow_job = None
        self.is_busy = False

        self.base_url_var = tk.StringVar(value="http://127.0.0.1:50021")
        self.status_var = tk.StringVar(value="画像フォルダを開いてください。")
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speaker_var = tk.StringVar()
        self.interval_var = tk.IntVar(value=5)
        self.auto_read_var = tk.BooleanVar(value=True)
        self.fit_mode_var = tk.StringVar(value="全体表示")
        self.copy_ocr_var = tk.BooleanVar(value=False)

        self.build_ui()

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="VOICEVOX URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.base_url_var, width=35).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(top, text="接続確認", command=self.connect_voicevox).grid(row=0, column=2, padx=4)
        ttk.Button(top, text="話者一覧更新", command=self.load_speakers).grid(row=0, column=3, padx=4)
        ttk.Button(top, text="画像フォルダを開く", command=self.open_folder).grid(row=0, column=4, padx=10)
        top.columnconfigure(1, weight=1)

        settings = ttk.LabelFrame(self.root, text="設定", padding=10)
        settings.pack(fill="x", padx=10, pady=4)

        ttk.Label(settings, text="話者").grid(row=0, column=0, sticky="w")
        self.speaker_combo = ttk.Combobox(settings, textvariable=self.speaker_var, state="readonly", width=42)
        self.speaker_combo.grid(row=0, column=1, sticky="we", padx=6)

        ttk.Label(settings, text="話速").grid(row=0, column=2, sticky="w")
        ttk.Scale(settings, from_=0.5, to=1.8, variable=self.speed_var, orient="horizontal").grid(
            row=0, column=3, sticky="we", padx=6
        )
        self.speed_label = ttk.Label(settings, text="1.00")
        self.speed_label.grid(row=0, column=4, sticky="w")
        self.speed_var.trace_add("write", lambda *_: self.speed_label.config(text=f"{self.speed_var.get():.2f}"))

        ttk.Label(settings, text="切替秒").grid(row=0, column=5, sticky="w", padx=(12, 0))
        ttk.Spinbox(settings, from_=1, to=60, textvariable=self.interval_var, width=6).grid(row=0, column=6, padx=4)

        ttk.Checkbutton(settings, text="切替時に自動読み上げ", variable=self.auto_read_var).grid(
            row=0, column=7, padx=10
        )
        ttk.Checkbutton(settings, text="OCR結果をクリップボードへコピー", variable=self.copy_ocr_var).grid(
            row=0, column=8, padx=10
        )

        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        nav = ttk.LabelFrame(self.root, text="スライド操作", padding=10)
        nav.pack(fill="x", padx=10, pady=4)

        ttk.Button(nav, text="◀ 前へ", command=self.prev_image).pack(side="left", padx=4)
        ttk.Button(nav, text="次へ ▶", command=self.next_image).pack(side="left", padx=4)
        ttk.Button(nav, text="現在の画像をOCRして読む", command=self.ocr_and_speak_current).pack(side="left", padx=12)
        ttk.Button(nav, text="OCRだけ実行", command=self.ocr_only_current).pack(side="left", padx=4)
        ttk.Button(nav, text="スライドショー開始", command=self.start_slideshow).pack(side="left", padx=12)
        ttk.Button(nav, text="停止", command=self.stop_slideshow).pack(side="left", padx=4)

        ttk.Label(nav, text="表示").pack(side="left", padx=(20, 4))
        ttk.Combobox(
            nav,
            textvariable=self.fit_mode_var,
            values=["全体表示", "横幅に合わせる", "原寸に近い"],
            state="readonly",
            width=14,
        ).pack(side="left")

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=6)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=2)

        self.canvas = tk.Canvas(left, bg="#1a1a1a", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.refresh_preview())

        info = ttk.Frame(right)
        info.pack(fill="both", expand=True)

        ttk.Label(info, text="OCR結果", font=("Yu Gothic UI", 12, "bold")).pack(anchor="w", pady=(0, 6))
        self.text = tk.Text(info, wrap="word", font=("Yu Gothic UI", 13))
        self.text.pack(fill="both", expand=True)

        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")

        self.path_label = ttk.Label(bottom, text="ファイル: -")
        self.path_label.pack(anchor="w")

        ttk.Label(bottom, textvariable=self.status_var).pack(anchor="w", pady=(6, 0))

    def set_status(self, msg: str):
        self.status_var.set(msg)
        self.root.update_idletasks()

    def connect_voicevox(self):
        self.voicevox = VoiceVoxClient(self.base_url_var.get())
        if self.voicevox.health_check():
            self.set_status("VOICEVOX に接続できました。")
            self.load_speakers()
        else:
            self.set_status("VOICEVOX に接続できません。起動を確認してください。")
            messagebox.showwarning("接続失敗", "VOICEVOX 本体または Engine を起動してください。")

    def load_speakers(self):
        try:
            self.voicevox = VoiceVoxClient(self.base_url_var.get())
            self.speakers = self.voicevox.get_speakers()
            labels = [s["label"] for s in self.speakers]
            self.speaker_combo["values"] = labels
            if labels and not self.speaker_var.get():
                self.speaker_combo.current(0)
            self.set_status(f"話者を {len(labels)} 件読み込みました。")
        except Exception as e:
            self.set_status(f"話者一覧取得失敗: {e}")
            messagebox.showerror("取得失敗", str(e))

    def get_selected_speaker_id(self) -> int:
        label = self.speaker_var.get()
        for s in self.speakers:
            if s["label"] == label:
                return int(s["id"])
        raise RuntimeError("話者が選択されていません。")

    def open_folder(self):
        folder = filedialog.askdirectory(title="画像フォルダを選択")
        if not folder:
            return
        paths = sorted(
            str(p) for p in Path(folder).iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        )
        if not paths:
            messagebox.showinfo("確認", "画像が見つかりませんでした。")
            return
        self.image_paths = paths
        self.index = 0
        self.show_current_image()
        self.set_status(f"{len(paths)} 枚の画像を読み込みました。")

    def show_current_image(self):
        if not self.image_paths or self.index < 0 or self.index >= len(self.image_paths):
            return
        path = self.image_paths[self.index]
        self.path_label.config(text=f"ファイル: {self.index + 1}/{len(self.image_paths)}  {path}")
        try:
            self.current_image = Image.open(path).convert("RGB")
        except Exception as e:
            self.set_status(f"画像を開けません: {e}")
            return
        self.refresh_preview()

    def refresh_preview(self):
        if self.current_image is None:
            return
        canvas_w = max(self.canvas.winfo_width(), 200)
        canvas_h = max(self.canvas.winfo_height(), 200)

        img = self.current_image.copy()
        mode = self.fit_mode_var.get()

        if mode == "全体表示":
            img.thumbnail((canvas_w - 20, canvas_h - 20), Image.Resampling.LANCZOS)
        elif mode == "横幅に合わせる":
            scale = (canvas_w - 20) / max(img.width, 1)
            new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        else:  # 原寸に近い
            scale = min(1.3, (canvas_w - 20) / max(img.width, 1), (canvas_h - 20) / max(img.height, 1))
            new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        self.current_preview = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(canvas_w // 2, canvas_h // 2, image=self.current_preview, anchor="center")

    def prev_image(self):
        if not self.image_paths:
            return
        self.index = (self.index - 1) % len(self.image_paths)
        self.show_current_image()
        if self.auto_read_var.get():
            self.ocr_and_speak_current()

    def next_image(self):
        if not self.image_paths:
            return
        self.index = (self.index + 1) % len(self.image_paths)
        self.show_current_image()
        if self.auto_read_var.get():
            self.ocr_and_speak_current()

    def ocr_only_current(self):
        self._run_ocr(speak=False)

    def ocr_and_speak_current(self):
        self._run_ocr(speak=True)

    def _run_ocr(self, speak: bool):
        if not self.image_paths:
            messagebox.showinfo("確認", "先に画像フォルダを開いてください。")
            return
        if self.is_busy:
            self.set_status("処理中です。少し待ってください。")
            return

        path = self.image_paths[self.index]

        def task():
            self.is_busy = True
            try:
                self.set_status("OCR 実行中です…")
                text = self.ocr.image_to_text(path)
                self.text.delete("1.0", "end")
                self.text.insert("1.0", text)

                if self.copy_ocr_var.get() and pyperclip is not None and text.strip():
                    pyperclip.copy(text)

                if not text.strip():
                    self.set_status("OCRで文字を抽出できませんでした。")
                    return

                self.set_status("OCR 完了。")
                if speak:
                    self._speak_text(text)
            except Exception as e:
                self.set_status(f"OCR失敗: {e}")
                messagebox.showerror("OCR失敗", str(e))
            finally:
                self.is_busy = False

        threading.Thread(target=task, daemon=True).start()

    def _speak_text(self, text: str):
        try:
            speaker_id = self.get_selected_speaker_id()
            speed = float(self.speed_var.get())
            self.set_status("音声合成中です…")
            wav_data = self.voicevox.synthesize(text, speaker_id, speed)

            fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            Path(tmp_path).write_bytes(wav_data)
            self.last_audio_file = tmp_path

            self.set_status("読み上げ中です。")
            if playsound is not None:
                playsound(tmp_path)
            elif os.name == "nt":
                os.startfile(tmp_path)
            else:
                self.set_status(f"音声生成完了。保存先: {tmp_path}")
                return
            self.set_status("読み上げ完了。")
        except Exception as e:
            self.set_status(f"読み上げ失敗: {e}")
            messagebox.showerror("読み上げ失敗", str(e))

    def start_slideshow(self):
        if not self.image_paths:
            messagebox.showinfo("確認", "先に画像フォルダを開いてください。")
            return
        self.stop_slideshow()
        self.set_status("スライドショーを開始しました。")
        self._schedule_next_slide()

    def _schedule_next_slide(self):
        interval_ms = max(1, int(self.interval_var.get())) * 1000
        self.slideshow_job = self.root.after(interval_ms, self._advance_slide)

    def _advance_slide(self):
        self.next_image()
        self._schedule_next_slide()

    def stop_slideshow(self):
        if self.slideshow_job is not None:
            self.root.after_cancel(self.slideshow_job)
            self.slideshow_job = None
            self.set_status("スライドショーを停止しました。")


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    MangaSlideShowReaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
