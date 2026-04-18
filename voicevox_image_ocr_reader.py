
import io
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

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


def is_http_url(text: str) -> bool:
    try:
        p = urlparse(text.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


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
            raise RuntimeError("easyocr が見つかりません。`pip install easyocr` を実行してください。")
        if self.reader is None:
            self.reader = easyocr.Reader(["ja", "en"], gpu=False)

    def _variants(self, image: Image.Image):
        base = image.convert("L")
        contrast = ImageEnhance.Contrast(base).enhance(2.0)
        variants = []

        for scale in (1.8, 2.5):
            w, h = contrast.size
            resized = contrast.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
            sharp = resized.filter(ImageFilter.SHARPEN)
            bw = sharp.point(lambda p: 255 if p > 180 else 0)
            inv_bw = ImageOps.invert(sharp).point(lambda p: 255 if p > 180 else 0)
            variants.extend([resized, sharp, bw, inv_bw])

        return variants

    def _score_text(self, text: str) -> int:
        t = text.strip()
        if not t:
            return 0
        core = len(re.sub(r"\s+", "", t))
        jp = sum(1 for ch in t if ("\u3040" <= ch <= "\u30ff") or ("\u4e00" <= ch <= "\u9fff"))
        bad = sum(1 for ch in t if ch in "|/\\[]{}<>_-=+*~")
        return core + jp * 2 - bad * 2

    def image_to_text(self, image: Image.Image) -> str:
        self.ensure_reader()
        candidates = []

        for variant in self._variants(image):
            for angle in (0, 90, 270):
                rotated = variant.rotate(angle, expand=True)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    temp_path = tmp.name
                try:
                    rotated.save(temp_path)
                    results = self.reader.readtext(temp_path, detail=0, paragraph=False)
                    text = "\n".join(str(x).strip() for x in results if str(x).strip())
                    candidates.append((self._score_text(text), text))
                finally:
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1].strip() if candidates else ""


class OCRVoiceApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("画像OCR → VOICEVOX 読み上げ")
        self.root.geometry("980x720")

        self.voicevox = VoiceVoxClient()
        self.ocr = OCRReader()
        self.speakers = []
        self.current_image = None
        self.current_source = ""

        self.base_url_var = tk.StringVar(value="http://127.0.0.1:50021")
        self.status_var = tk.StringVar(value="画像ファイルか画像URLを入れてください。")
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speaker_var = tk.StringVar()
        self.url_var = tk.StringVar()

        self.build_ui()

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="VOICEVOX URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.base_url_var, width=34).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(top, text="接続確認", command=self.connect_voicevox).grid(row=0, column=2, padx=4)
        ttk.Button(top, text="話者一覧更新", command=self.load_speakers).grid(row=0, column=3, padx=4)
        top.columnconfigure(1, weight=1)

        source = ttk.LabelFrame(self.root, text="画像入力", padding=10)
        source.pack(fill="x", padx=10, pady=4)

        ttk.Button(source, text="画像ファイルを開く", command=self.open_file).grid(row=0, column=0, padx=4, pady=4)
        ttk.Label(source, text="画像URL").grid(row=0, column=1, sticky="w")
        ttk.Entry(source, textvariable=self.url_var).grid(row=0, column=2, sticky="we", padx=6)
        ttk.Button(source, text="URLから読込", command=self.load_from_url).grid(row=0, column=3, padx=4)
        ttk.Button(source, text="クリップボードURL貼付", command=self.paste_url).grid(row=0, column=4, padx=4)
        source.columnconfigure(2, weight=1)

        voice = ttk.LabelFrame(self.root, text="音声設定", padding=10)
        voice.pack(fill="x", padx=10, pady=4)

        ttk.Label(voice, text="話者").grid(row=0, column=0, sticky="w")
        self.speaker_combo = ttk.Combobox(voice, textvariable=self.speaker_var, state="readonly", width=50)
        self.speaker_combo.grid(row=0, column=1, sticky="we", padx=6)

        ttk.Label(voice, text="話速").grid(row=0, column=2, sticky="w")
        ttk.Scale(voice, from_=0.5, to=1.8, variable=self.speed_var, orient="horizontal").grid(row=0, column=3, sticky="we", padx=6)
        self.speed_label = ttk.Label(voice, text="1.00")
        self.speed_label.grid(row=0, column=4, sticky="w")
        self.speed_var.trace_add("write", lambda *_: self.speed_label.config(text=f"{self.speed_var.get():.2f}"))
        voice.columnconfigure(1, weight=1)
        voice.columnconfigure(3, weight=1)

        actions = ttk.Frame(self.root, padding=(10, 6))
        actions.pack(fill="x")
        ttk.Button(actions, text="OCRだけ実行", command=self.run_ocr_only).pack(side="left", padx=4)
        ttk.Button(actions, text="OCRして読み上げ", command=self.run_ocr_and_speak).pack(side="left", padx=4)

        middle = ttk.PanedWindow(self.root, orient="horizontal")
        middle.pack(fill="both", expand=True, padx=10, pady=4)

        left = ttk.Frame(middle)
        right = ttk.Frame(middle)
        middle.add(left, weight=2)
        middle.add(right, weight=2)

        ttk.Label(left, text="OCR結果").pack(anchor="w", pady=(0, 6))
        self.text = tk.Text(left, wrap="word", font=("Yu Gothic UI", 13))
        self.text.pack(fill="both", expand=True)

        ttk.Label(right, text="入力元").pack(anchor="w", pady=(0, 6))
        self.source_text = tk.Text(right, height=6, wrap="word", font=("Yu Gothic UI", 11))
        self.source_text.pack(fill="x")
        self.source_text.insert("1.0", "ここにファイルパスや画像URLが表示されます。")
        self.source_text.config(state="disabled")

        preview_label = ttk.Label(right, text="画像サイズ")
        preview_label.pack(anchor="w", pady=(10, 2))
        self.info_var = tk.StringVar(value="-")
        ttk.Label(right, textvariable=self.info_var).pack(anchor="w")

        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status_var).pack(anchor="w")

    def set_status(self, msg: str):
        self.status_var.set(msg)
        self.root.update_idletasks()

    def _set_source_text(self, text: str):
        self.source_text.config(state="normal")
        self.source_text.delete("1.0", "end")
        self.source_text.insert("1.0", text)
        self.source_text.config(state="disabled")

    def connect_voicevox(self):
        self.voicevox = VoiceVoxClient(self.base_url_var.get())
        if self.voicevox.health_check():
            self.set_status("VOICEVOX に接続できました。")
            self.load_speakers()
        else:
            self.set_status("VOICEVOX に接続できません。")
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

    def open_file(self):
        path = filedialog.askopenfilename(
            title="画像を選択",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.webp *.bmp")],
        )
        if not path:
            return
        try:
            img = Image.open(path).convert("RGB")
            self.current_image = img
            self.current_source = path
            self._set_source_text(path)
            self.info_var.set(f"{img.width} x {img.height}")
            self.set_status("画像を読み込みました。")
        except Exception as e:
            messagebox.showerror("読込失敗", str(e))

    def paste_url(self):
        try:
            if pyperclip is not None:
                value = pyperclip.paste()
            else:
                value = self.root.clipboard_get()
            self.url_var.set(str(value).strip())
        except Exception as e:
            messagebox.showerror("貼り付け失敗", str(e))

    def load_from_url(self):
        url = self.url_var.get().strip()
        if not is_http_url(url):
            messagebox.showinfo("確認", "画像URLを入れてください。")
            return
        try:
            self.set_status("画像URLから取得中です…")
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            self.current_image = img
            self.current_source = url
            self._set_source_text(url)
            self.info_var.set(f"{img.width} x {img.height}")
            self.set_status("画像URLから読み込みました。")
        except Exception as e:
            self.set_status(f"URL読込失敗: {e}")
            messagebox.showerror("URL読込失敗", str(e))

    def run_ocr_only(self):
        self._run_ocr(speak=False)

    def run_ocr_and_speak(self):
        self._run_ocr(speak=True)

    def _run_ocr(self, speak: bool):
        if self.current_image is None:
            messagebox.showinfo("確認", "先に画像ファイルか画像URLを読み込んでください。")
            return

        def task():
            try:
                self.set_status("OCR 実行中です…")
                text = self.ocr.image_to_text(self.current_image)
                self.text.delete("1.0", "end")
                self.text.insert("1.0", text)

                if not text.strip():
                    self.set_status("文字を抽出できませんでした。")
                    return

                self.set_status("OCR 完了。")
                if speak:
                    self._speak_text(text)
            except Exception as e:
                self.set_status(f"OCR失敗: {e}")
                messagebox.showerror("OCR失敗", str(e))

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

            self.set_status("読み上げ中です。")
            if playsound is not None:
                playsound(tmp_path)
            elif os.name == "nt":
                os.startfile(tmp_path)
            else:
                self.set_status(f"音声生成完了: {tmp_path}")
                return
            self.set_status("読み上げ完了。")
        except Exception as e:
            self.set_status(f"読み上げ失敗: {e}")
            messagebox.showerror("読み上げ失敗", str(e))


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    OCRVoiceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
