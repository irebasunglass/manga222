import os
import subprocess
import sys
import threading
import tempfile
import time
import ctypes
import ctypes.wintypes
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests
from PIL import Image, ImageGrab, ImageOps, ImageFilter, ImageEnhance

try:
    import pyperclip
except Exception:
    pyperclip = None

try:
    import easyocr
except Exception:
    easyocr = None

try:
    from playsound import playsound
except Exception:
    playsound = None


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
                result.append({
                    "label": f"{speaker_name} / {style.get('name', 'default')} ({style.get('id')})",
                    "id": style.get("id"),
                })
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
            self.reader = easyocr.Reader(["ja", "en"], gpu=True)

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.replace(" ", "").replace("\u3000", "")
        while "\n\n" in text:
            text = text.replace("\n\n", "\n")
        return text.strip()

    @staticmethod
    def _looks_like_japanese(text: str) -> int:
        score = 0
        for ch in text:
            if ('ぁ' <= ch <= 'ゖ') or ('ァ' <= ch <= 'ヺ') or ('一' <= ch <= '龯'):
                score += 3
            elif ch.isdigit() or ch.isalpha():
                score += 1
        return score

    def _variant_images(self, image: Image.Image):
        base = image.convert("RGB")
        gray = ImageOps.grayscale(base)
        variants = []

        # 1. 原画像に近いもの
        v1 = ImageOps.autocontrast(gray)
        v1 = v1.resize((max(1, v1.width * 2), max(1, v1.height * 2)), Image.Resampling.LANCZOS)
        variants.append(("gray2x", v1))

        # 2. コントラスト強め
        v2 = ImageOps.autocontrast(gray)
        v2 = ImageEnhance.Contrast(v2).enhance(2.2)
        v2 = v2.filter(ImageFilter.SHARPEN)
        v2 = v2.resize((max(1, v2.width * 3), max(1, v2.height * 3)), Image.Resampling.LANCZOS)
        variants.append(("contrast3x", v2))

        # 3. 二値化寄り
        v3 = ImageOps.autocontrast(gray)
        v3 = ImageEnhance.Contrast(v3).enhance(2.8)
        v3 = v3.resize((max(1, v3.width * 3), max(1, v3.height * 3)), Image.Resampling.LANCZOS)
        v3 = v3.point(lambda p: 255 if p > 175 else 0)
        variants.append(("binary3x", v3))

        # 4. 色文字向け: RGBのまま拡大→グレースケール
        v4 = base.resize((max(1, base.width * 3), max(1, base.height * 3)), Image.Resampling.LANCZOS)
        v4 = ImageOps.grayscale(v4)
        v4 = ImageOps.autocontrast(v4)
        variants.append(("color3x", v4))

        return variants

    def _ocr_once(self, img: Image.Image):
        fd, tmp_path = tempfile.mkstemp(suffix="_ocr.png")
        os.close(fd)
        img.save(tmp_path)
        try:
            results = self.reader.readtext(tmp_path, detail=1, paragraph=False)
            texts = []
            conf_sum = 0.0
            for item in results:
                if len(item) >= 3:
                    txt = str(item[1]).strip()
                    conf = float(item[2])
                else:
                    txt = str(item).strip()
                    conf = 0.0
                if txt:
                    texts.append(txt)
                    conf_sum += max(conf, 0.0)
            joined = "\n".join(texts)
            score = len(joined) + self._looks_like_japanese(joined) + int(conf_sum * 10)
            return self._normalize_text(joined), score, tmp_path
        except Exception:
            return "", -1, tmp_path

    def image_to_text(self, image_path: str):
        self.ensure_reader()
        src = Image.open(image_path)
        src.load()
        candidates = []

        for name, variant in self._variant_images(src):
            # 横書き想定
            text0, score0, path0 = self._ocr_once(variant)
            candidates.append((score0, text0, path0, f"{name}-0"))
            # 縦書き想定: 90/270度回転
            rot90 = variant.rotate(90, expand=True)
            text90, score90, path90 = self._ocr_once(rot90)
            candidates.append((score90, text90, path90, f"{name}-90"))

            rot270 = variant.rotate(270, expand=True)
            text270, score270, path270 = self._ocr_once(rot270)
            candidates.append((score270, text270, path270, f"{name}-270"))

        best_score, best_text, best_path, best_name = max(candidates, key=lambda x: x[0])
        if best_score <= 0 or not best_text:
            return "", best_path, "OCR候補なし"
        return best_text, best_path, best_name


class ScreenCaptureOverlay(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.withdraw()
        self.start_x = 0
        self.start_y = 0
        self.cur_x = 0
        self.cur_y = 0
        self.result_bbox = None
        self.attributes("-fullscreen", True)
        self.attributes("-alpha", 0.25)
        self.attributes("-topmost", True)
        self.configure(bg="black")
        self.overrideredirect(True)

        self.canvas = tk.Canvas(self, cursor="cross", bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.rect = None

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<Escape>", self.on_escape)

    def on_press(self, event):
        self.start_x = self.canvas.canvasx(event.x)
        self.start_y = self.canvas.canvasy(event.y)
        self.cur_x = self.start_x
        self.cur_y = self.start_y
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y, outline="red", width=2
        )

    def on_drag(self, event):
        self.cur_x = self.canvas.canvasx(event.x)
        self.cur_y = self.canvas.canvasy(event.y)
        self.canvas.coords(self.rect, self.start_x, self.start_y, self.cur_x, self.cur_y)

    def on_release(self, event):
        self.cur_x = self.canvas.canvasx(event.x)
        self.cur_y = self.canvas.canvasy(event.y)
        left = int(min(self.start_x, self.cur_x))
        top = int(min(self.start_y, self.cur_y))
        right = int(max(self.start_x, self.cur_x))
        bottom = int(max(self.start_y, self.cur_y))
        if right - left > 5 and bottom - top > 5:
            self.result_bbox = (left, top, right, bottom)
        self.destroy()

    def on_escape(self, _event):
        self.result_bbox = None
        self.destroy()


class MangaReaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VOICEVOX ネット漫画セリフ読み上げ v3")
        self.root.geometry("1020x800")

        self.voicevox = VoiceVoxClient()
        self.ocr = OCRReader()
        self.speakers = []
        self.last_audio_file = None
        self.last_capture_file = None
        self.capture_delay_var = tk.IntVar(value=3)

        self.base_url_var = tk.StringVar(value="http://127.0.0.1:50021")
        self.status_var = tk.StringVar(value="起動後は『接続確認』→『話者一覧更新』を押してください。")
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speaker_var = tk.StringVar()
        self.auto_clipboard_var = tk.BooleanVar(value=False)
        self.last_clipboard_text = ""
        self.auto_speak_var = tk.BooleanVar(value=True)

        self.build_ui()
        self.root.after(1200, self.clipboard_loop)

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="VOICEVOX URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.base_url_var, width=35).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(top, text="接続確認", command=self.connect_voicevox).grid(row=0, column=2, padx=4)
        ttk.Button(top, text="話者一覧更新", command=self.load_speakers).grid(row=0, column=3, padx=4)
        top.columnconfigure(1, weight=1)

        speaker_frame = ttk.LabelFrame(self.root, text="音声設定", padding=10)
        speaker_frame.pack(fill="x", padx=10, pady=6)
        ttk.Label(speaker_frame, text="話者").grid(row=0, column=0, sticky="w")
        self.speaker_combo = ttk.Combobox(speaker_frame, textvariable=self.speaker_var, state="readonly", width=60)
        self.speaker_combo.grid(row=0, column=1, sticky="we", padx=6)
        ttk.Label(speaker_frame, text="話速").grid(row=0, column=2, sticky="w")
        ttk.Scale(speaker_frame, from_=0.5, to=1.8, variable=self.speed_var, orient="horizontal").grid(row=0, column=3, sticky="we", padx=6)
        self.speed_label = ttk.Label(speaker_frame, text="1.00")
        self.speed_label.grid(row=0, column=4, sticky="w")
        self.speed_var.trace_add("write", lambda *_: self.speed_label.config(text=f"{self.speed_var.get():.2f}"))
        speaker_frame.columnconfigure(1, weight=1)
        speaker_frame.columnconfigure(3, weight=1)

        capture_frame = ttk.LabelFrame(self.root, text="画面読み上げ", padding=10)
        capture_frame.pack(fill="x", padx=10, pady=6)
        ttk.Label(capture_frame, text="待機秒数").pack(side="left")
        ttk.Spinbox(capture_frame, from_=1, to=10, textvariable=self.capture_delay_var, width=5).pack(side="left", padx=(4, 12))
        ttk.Button(capture_frame, text="アクティブ画面OCR", command=self.ocr_active_window_delayed).pack(side="left", padx=4)
        ttk.Button(capture_frame, text="範囲選択OCR", command=self.ocr_from_selection).pack(side="left", padx=4)
        ttk.Button(capture_frame, text="画像からOCR", command=self.ocr_from_image).pack(side="left", padx=4)
        ttk.Button(capture_frame, text="最後の画像を開く", command=self.open_last_capture).pack(side="left", padx=4)
        ttk.Checkbutton(capture_frame, text="OCR後に自動で読む", variable=self.auto_speak_var).pack(side="left", padx=10)

        ocr_frame = ttk.LabelFrame(self.root, text="セリフ入力", padding=10)
        ocr_frame.pack(fill="both", expand=True, padx=10, pady=6)
        btns = ttk.Frame(ocr_frame)
        btns.pack(fill="x", pady=(0, 8))
        ttk.Button(btns, text="クリップボード貼り付け", command=self.paste_clipboard).pack(side="left", padx=4)
        ttk.Checkbutton(btns, text="クリップボード自動読み上げ", variable=self.auto_clipboard_var).pack(side="left", padx=8)
        ttk.Button(btns, text="読み上げ", command=self.speak_text).pack(side="left", padx=4)
        ttk.Button(btns, text="WAV保存", command=self.save_wav).pack(side="left", padx=4)
        ttk.Button(btns, text="クリア", command=self.clear_text).pack(side="left", padx=4)

        self.text = tk.Text(ocr_frame, wrap="word", font=("Yu Gothic UI", 14))
        self.text.pack(fill="both", expand=True)

        status_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        status_frame.pack(fill="x")
        ttk.Label(status_frame, textvariable=self.status_var, foreground="#333333").pack(anchor="w")

    def set_status(self, msg: str):
        self.status_var.set(msg)
        self.root.update_idletasks()

    def connect_voicevox(self):
        self.voicevox = VoiceVoxClient(self.base_url_var.get())
        ok = self.voicevox.health_check()
        if ok:
            self.set_status("VOICEVOX に接続できました。話者一覧も読み込みます。")
            self.load_speakers()
        else:
            self.set_status("VOICEVOX に接続できません。VOICEVOX本体または Engine が起動しているか確認してください。")
            messagebox.showwarning("接続失敗", "VOICEVOX へ接続できませんでした。\nVOICEVOX を起動してから再度お試しください。")

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
            self.set_status(f"話者一覧の取得に失敗しました: {e}")
            messagebox.showerror("取得失敗", str(e))

    def get_selected_speaker_id(self) -> int:
        label = self.speaker_var.get()
        for s in self.speakers:
            if s["label"] == label:
                return int(s["id"])
        raise RuntimeError("話者が選択されていません。")

    def _play_audio_file(self, path: str):
        if playsound is not None:
            playsound(path)
            return
        if sys.platform.startswith("win"):
            os.startfile(path)
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
            return
        subprocess.Popen(["xdg-open", path])

    def _ocr_and_fill(self, path: str):
        try:
            self.set_status("OCR 実行中です…")
            text, best_path, mode = self.ocr.image_to_text(path)
            self.last_capture_file = best_path or path
            if not text.strip():
                self.set_status("文字を抽出できませんでした。範囲を小さくして再試行してください。")
                messagebox.showinfo("OCR結果", "文字を抽出できませんでした。\n吹き出し1つだけを小さく囲うと成功しやすいです。")
                return
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text)
            self.set_status(f"OCR 完了（採用: {mode}）。必要なら文を整えてください。")
            if self.auto_speak_var.get():
                self.speak_text()
        except Exception as e:
            self.set_status(f"OCR に失敗しました: {e}")
            messagebox.showerror("OCR失敗", str(e))

    def ocr_from_image(self):
        path = filedialog.askopenfilename(
            title="画像を選択",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.webp *.bmp")],
        )
        if not path:
            return
        threading.Thread(target=lambda: self._ocr_and_fill(path), daemon=True).start()

    def ocr_from_selection(self):
        self.set_status("範囲選択を開始します。ドラッグでセリフ部分だけ囲ってください。Escでキャンセル。")
        self.root.withdraw()
        self.root.update()
        time.sleep(0.2)

        overlay = ScreenCaptureOverlay(self.root)
        overlay.deiconify()
        overlay.focus_force()
        overlay.grab_set()
        self.root.wait_window(overlay)

        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

        if not overlay.result_bbox:
            self.set_status("範囲選択をキャンセルしました。")
            return

        try:
            bbox = overlay.result_bbox
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
            fd, tmp_path = tempfile.mkstemp(suffix="_selection.png")
            os.close(fd)
            img.save(tmp_path)
            self.last_capture_file = tmp_path
            threading.Thread(target=lambda: self._ocr_and_fill(tmp_path), daemon=True).start()
        except Exception as e:
            self.set_status(f"範囲選択OCRに失敗しました: {e}")
            messagebox.showerror("範囲選択OCR失敗", str(e))

    def _get_active_window_bbox_windows(self):
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        rect = ctypes.wintypes.RECT()
        ok = user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if not ok:
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)

    def ocr_active_window_delayed(self):
        delay = max(1, int(self.capture_delay_var.get()))

        def task():
            try:
                self.set_status(f"{delay}秒後にアクティブ画面を取り込みます。読みたい画面を前面にしてください。")
                self.root.withdraw()
                for _ in range(delay * 10):
                    time.sleep(0.1)
                bbox = None
                if sys.platform.startswith("win"):
                    bbox = self._get_active_window_bbox_windows()
                img = ImageGrab.grab(bbox=bbox, all_screens=True)
                fd, tmp_path = tempfile.mkstemp(suffix="_active.png")
                os.close(fd)
                img.save(tmp_path)
                self.last_capture_file = tmp_path
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("画面取得失敗", str(e)))
                self.root.after(0, lambda: self.set_status(f"画面取得に失敗しました: {e}"))
                self.root.after(0, self.root.deiconify)
                return

            self.root.after(0, self.root.deiconify)
            self.root.after(0, self.root.lift)
            self.root.after(0, self.root.focus_force)
            threading.Thread(target=lambda: self._ocr_and_fill(tmp_path), daemon=True).start()

        threading.Thread(target=task, daemon=True).start()

    def open_last_capture(self):
        if not self.last_capture_file or not Path(self.last_capture_file).exists():
            messagebox.showinfo("確認", "開ける画像がまだありません。")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(self.last_capture_file)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.last_capture_file])
            else:
                subprocess.Popen(["xdg-open", self.last_capture_file])
        except Exception as e:
            messagebox.showerror("画像を開けません", str(e))

    def paste_clipboard(self):
        try:
            content = pyperclip.paste() if pyperclip is not None else self.root.clipboard_get()
            self.text.delete("1.0", "end")
            self.text.insert("1.0", content)
            self.set_status("クリップボードの内容を貼り付けました。")
        except Exception as e:
            messagebox.showerror("貼り付け失敗", str(e))

    def clear_text(self):
        self.text.delete("1.0", "end")
        self.set_status("テキストをクリアしました。")

    def speak_text(self):
        text = self.text.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("確認", "先にセリフを入力してください。")
            return

        def task():
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
                self._play_audio_file(tmp_path)
                self.set_status("読み上げを開始しました。")
            except Exception as e:
                self.set_status(f"読み上げに失敗しました: {e}")
                messagebox.showerror("読み上げ失敗", str(e))

        threading.Thread(target=task, daemon=True).start()

    def save_wav(self):
        text = self.text.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("確認", "保存する前にセリフを入力してください。")
            return
        path = filedialog.asksaveasfilename(title="WAV保存", defaultextension=".wav", filetypes=[("WAV", "*.wav")])
        if not path:
            return
        try:
            speaker_id = self.get_selected_speaker_id()
            speed = float(self.speed_var.get())
            self.set_status("WAV を保存中です…")
            wav_data = self.voicevox.synthesize(text, speaker_id, speed)
            Path(path).write_bytes(wav_data)
            self.last_audio_file = path
            self.set_status(f"保存しました: {path}")
        except Exception as e:
            self.set_status(f"保存に失敗しました: {e}")
            messagebox.showerror("保存失敗", str(e))

    def clipboard_loop(self):
        try:
            if self.auto_clipboard_var.get():
                content = pyperclip.paste() if pyperclip is not None else self.root.clipboard_get()
                content = str(content).strip()
                if content and content != self.last_clipboard_text:
                    self.last_clipboard_text = content
                    self.text.delete("1.0", "end")
                    self.text.insert("1.0", content)
                    self.speak_text()
        except Exception:
            pass
        finally:
            self.root.after(1200, self.clipboard_loop)


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    MangaReaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
