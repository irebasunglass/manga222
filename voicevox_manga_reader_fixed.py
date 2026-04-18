import os
import subprocess
import sys
import threading
import tempfile
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests
from PIL import Image

# OCR is optional. The app works without it for pasted text.
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
                "easyocr が見つかりません。OCRを使う場合は `pip install easyocr` を実行してください。"
            )
        if self.reader is None:
            self.reader = easyocr.Reader(["ja", "en"], gpu=True)

    def image_to_text(self, image_path: str) -> str:
        self.ensure_reader()
        # 画像を開けるかだけ軽く確認
        Image.open(image_path).close()
        results = self.reader.readtext(image_path, detail=0, paragraph=True)
        cleaned = [line.strip() for line in results if str(line).strip()]
        return "\n".join(cleaned)


class MangaReaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VOICEVOX ネット漫画セリフ読み上げ")
        self.root.geometry("900x700")

        self.voicevox = VoiceVoxClient()
        self.ocr = OCRReader()
        self.speakers = []
        self.last_audio_file = None

        self.base_url_var = tk.StringVar(value="http://127.0.0.1:50021")
        self.status_var = tk.StringVar(value="起動直後です。まず VOICEVOX 接続確認を押してください。")
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
        ok = self.voicevox.health_check()
        if ok:
            self.set_status("VOICEVOX に接続できました。話者一覧も読み込みます。")
            self.load_speakers()
        else:
            self.set_status("VOICEVOX に接続できません。VOICEVOX本体または Engine が起動しているか確認してください。")
            messagebox.showwarning(
                "接続失敗",
                "VOICEVOX へ接続できませんでした。\nVOICEVOX を起動してから再度お試しください。",
            )

    def load_speakers(self):
        try:
            self.voicevox = VoiceVoxClient(self.base_url_var.get())
            self.speakers = self.voicevox.get_speakers()
            labels = [s["label"] for s in self.speakers]
            self.speaker_combo["values"] = labels
            if labels:
                current = self.speaker_var.get()
                if current not in labels:
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

    def ocr_from_image(self):
        path = filedialog.askopenfilename(
            title="漫画画像を選択",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.webp *.bmp")],
        )
        if not path:
            return

        def task():
            try:
                self.set_status("OCR 実行中です…")
                text = self.ocr.image_to_text(path)
                if not text.strip():
                    self.set_status("文字を抽出できませんでした。")
                    self.root.after(0, lambda: messagebox.showinfo("OCR結果", "文字を抽出できませんでした。画像を変えて試してください。"))
                    return
                self.root.after(0, lambda: self.text.delete("1.0", "end"))
                self.root.after(0, lambda t=text: self.text.insert("1.0", t))
                self.set_status("OCR 完了。必要なら文章を整えてから読み上げてください。")
            except Exception as e:
                self.set_status(f"OCR に失敗しました: {e}")
                self.root.after(0, lambda err=str(e): messagebox.showerror("OCR失敗", err))

        threading.Thread(target=task, daemon=True).start()

    def paste_clipboard(self):
        try:
            content = ""
            if pyperclip is not None:
                content = pyperclip.paste()
            if not content:
                content = self.root.clipboard_get()
            self.text.delete("1.0", "end")
            self.text.insert("1.0", content)
            self.set_status("クリップボードの内容を貼り付けました。")
        except Exception as e:
            messagebox.showerror("貼り付け失敗", str(e))

    def clear_text(self):
        self.text.delete("1.0", "end")
        self.set_status("テキストをクリアしました。")

    def open_audio_file(self, path: str):
        # 1) playsound が使えればそれを優先
        if playsound is not None:
            try:
                playsound(path)
                return
            except Exception:
                pass

        # 2) Windows なら既定アプリで再生
        if os.name == "nt":
            os.startfile(path)
            return

        # 3) macOS / Linux のフォールバック
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

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
        path = filedialog.asksaveasfilename(
            title="WAV保存",
            defaultextension=".wav",
            filetypes=[("WAV", "*.wav")],
        )
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
                content = ""
                if pyperclip is not None:
                    content = pyperclip.paste()
                else:
                    content = self.root.clipboard_get()
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
