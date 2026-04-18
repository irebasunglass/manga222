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
from PIL import Image, ImageGrab, ImageOps, ImageFilter

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
            self.reader = easyocr.Reader(["ja", "en"], gpu=False)

    def preprocess_image(self, image_path: str) -> str:
        img = Image.open(image_path).convert("L")
        img = ImageOps.autocontrast(img)
        img = img.filter(ImageFilter.SHARPEN)
        scale = 2
        img = img.resize((img.width * scale, img.height * scale), Image.Resampling.LANCZOS)

        # 二値化寄りにして漫画文字を拾いやすくする
        threshold = 185
        img = img.point(lambda p: 255 if p > threshold else 0)

        fd, tmp_path = tempfile.mkstemp(suffix="_pre.png")
        os.close(fd)
        img.save(tmp_path)
        return tmp_path

    def image_to_text(self, image_path: str):
        self.ensure_reader()
        Image.open(image_path).close()

        # 元画像と前処理画像の両方を試して、文字数が多い方を採用
        candidates = []
        raw = self.reader.readtext(image_path, detail=0, paragraph=True)
        raw_text = "\n".join([str(x).strip() for x in raw if str(x).strip()])
        candidates.append((raw_text, image_path))

        pre_path = self.preprocess_image(image_path)
        pre = self.reader.readtext(pre_path, detail=0, paragraph=True)
        pre_text = "\n".join([str(x).strip() for x in pre if str(x).strip()])
        candidates.append((pre_text, pre_path))

        best_text, best_path = max(candidates, key=lambda x: len(x[0]))
        return best_text, best_path


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
        self.root.title("VOICEVOX ネット漫画セリフ読み上げ")
        self.root.geometry("980x780")

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
        ttk.Button(capture_frame, text="最後の画像を開く", command=self.open_last_capture).pack(side="left", padx=4)

        ocr_frame = ttk.LabelFrame(self.root, text="セリフ入力", padding=10)
        ocr_frame.pack(fill="both", expand=True, padx=10, pady=6)
        btns = ttk.Frame(ocr_frame)
        btns.pack(fill="x", pady=(0, 8))
        ttk.Button(btns, text="画像からOCR", command=self.ocr_from_image).pack(side="left", padx=4)
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
        if self.voicevox.health_check():
            self.set_status("VOICEVOX に接続できました。話者一覧を読み込みます。")
            self.load_speakers()
        else:
            self.set_status("VOICEVOX に接続できません。")
            messagebox.showwarning("接続失敗", "VOICEVOX を起動してから再度お試しください。")

    def load_speakers(self):
        try:
            self.voicevox = VoiceVoxClient(self.base_url_var.get())
            self.speakers = self.voicevox.get_speakers()
            labels = [s["label"] for s in self.speakers]
            self.speaker_combo["values"] = labels
            if labels and self.speaker_var.get() not in labels:
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

    def insert_text(self, text: str):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)

    def extract_text_from_image(self, image_path: str, auto_speak: bool = False):
        try:
            self.set_status("OCR 実行中です…")
            text, used_image = self.ocr.image_to_text(image_path)
            self.last_capture_file = used_image
            if not text.strip():
                self.set_status(f"文字を抽出できませんでした。確認画像: {used_image}")
                self.root.after(0, lambda: messagebox.showinfo("OCR結果", "文字を抽出できませんでした。『最後の画像を開く』で確認してください。"))
                return
            self.root.after(0, lambda t=text: self.insert_text(t))
            self.set_status(f"OCR 完了。確認画像: {used_image}")
            if auto_speak:
                self.root.after(200, self.speak_text)
        except Exception as e:
            self.set_status(f"OCR に失敗しました: {e}")
            self.root.after(0, lambda err=str(e): messagebox.showerror("OCR失敗", err))

    def ocr_from_image(self):
        path = filedialog.askopenfilename(title="漫画画像を選択", filetypes=[("Image files", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if path:
            threading.Thread(target=lambda: self.extract_text_from_image(path), daemon=True).start()

    def get_active_window_bbox_windows(self):
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise RuntimeError("アクティブウィンドウを取得できませんでした。")
        rect = ctypes.wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)) == 0:
            raise RuntimeError("ウィンドウ座標を取得できませんでした。")
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        if right - left < 20 or bottom - top < 20:
            raise RuntimeError("取得したウィンドウサイズが小さすぎます。")
        return left, top, right, bottom

    def capture_bbox_to_temp(self, bbox):
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        image.save(tmp_path)
        self.last_capture_file = tmp_path
        return tmp_path

    def ocr_active_window_delayed(self):
        delay = max(1, int(self.capture_delay_var.get()))
        def task():
            try:
                self.set_status(f"{delay}秒以内に読みたい画面を前面にしてください…")
                self.root.after(0, self.root.iconify)
                time.sleep(delay)
                if os.name != "nt":
                    raise RuntimeError("アクティブ画面OCRは現在Windows向けです。")
                bbox = self.get_active_window_bbox_windows()
                image_path = self.capture_bbox_to_temp(bbox)
                self.root.after(0, self.root.deiconify)
                self.extract_text_from_image(image_path, auto_speak=True)
            except Exception as e:
                self.root.after(0, self.root.deiconify)
                self.set_status(f"アクティブ画面OCRに失敗しました: {e}")
                self.root.after(0, lambda err=str(e): messagebox.showerror("画面OCR失敗", err))
        threading.Thread(target=task, daemon=True).start()

    def ocr_from_selection(self):
        self.set_status("ドラッグして範囲を選んでください。Escで中止できます。")
        self.root.iconify()
        def start_overlay():
            overlay = ScreenCaptureOverlay(self.root)
            overlay.deiconify()
            overlay.focus_force()
            self.root.wait_window(overlay)
            self.root.deiconify()
            bbox = overlay.result_bbox
            if not bbox:
                self.set_status("範囲選択を中止しました。")
                return
            threading.Thread(target=self._ocr_from_bbox_and_speak, args=(bbox,), daemon=True).start()
        self.root.after(250, start_overlay)

    def _ocr_from_bbox_and_speak(self, bbox):
        try:
            image_path = self.capture_bbox_to_temp(bbox)
            self.extract_text_from_image(image_path, auto_speak=True)
        except Exception as e:
            self.set_status(f"範囲OCRに失敗しました: {e}")
            self.root.after(0, lambda err=str(e): messagebox.showerror("範囲OCR失敗", err))

    def paste_clipboard(self):
        try:
            content = pyperclip.paste() if pyperclip is not None else self.root.clipboard_get()
            self.insert_text(content)
            self.set_status("クリップボードの内容を貼り付けました。")
        except Exception as e:
            messagebox.showerror("貼り付け失敗", str(e))

    def clear_text(self):
        self.text.delete("1.0", "end")
        self.set_status("テキストをクリアしました。")

    def open_audio_file(self, path: str):
        if playsound is not None:
            try:
                playsound(path)
                return
            except Exception:
                pass
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def open_last_capture(self):
        if not self.last_capture_file or not Path(self.last_capture_file).exists():
            messagebox.showinfo("確認", "まだ確認用画像がありません。")
            return
        if os.name == "nt":
            os.startfile(self.last_capture_file)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", self.last_capture_file])
        else:
            subprocess.Popen(["xdg-open", self.last_capture_file])

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
                self.open_audio_file(tmp_path)
                self.set_status(f"音声を再生しました。保存先: {tmp_path}")
            except Exception as e:
                self.set_status(f"読み上げに失敗しました: {e}")
                self.root.after(0, lambda err=str(e): messagebox.showerror("読み上げ失敗", err))
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
                    self.insert_text(content)
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
