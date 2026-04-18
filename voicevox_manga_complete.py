import io
import os
import re
import tempfile
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import mss
import numpy as np
import pygetwindow as gw
import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

try:
    import easyocr
except Exception:
    easyocr = None

try:
    from playsound import playsound
except Exception:
    playsound = None


APP_TITLE = "VOICEVOX 漫画OCR 完全版"
DEFAULT_VOICEVOX_URL = "http://127.0.0.1:50021"


def pil_to_cv(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def cv_to_pil(img: np.ndarray) -> Image.Image:
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


class VoiceVoxClient:
    def __init__(self, base_url: str = DEFAULT_VOICEVOX_URL):
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
            # GPUは使わない
            self.reader = easyocr.Reader(["ja", "en"], gpu=True)

    def _build_variants(self, image: Image.Image):
        base = image.convert("L")
        variants = []

        # 基本強化
        contrast = ImageEnhance.Contrast(base).enhance(2.4)
        sharp = contrast.filter(ImageFilter.SHARPEN)

        for scale in (2.0, 2.6, 3.0):
            w, h = sharp.size
            resized = sharp.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)

            # 通常
            variants.append(("gray", resized))

            # 明るめ二値化
            bw1 = resized.point(lambda p: 255 if p > 175 else 0)
            variants.append(("bw175", bw1))

            # やや濃い二値化
            bw2 = resized.point(lambda p: 255 if p > 155 else 0)
            variants.append(("bw155", bw2))

            # 反転
            inv = ImageOps.invert(resized)
            inv_bw = inv.point(lambda p: 255 if p > 175 else 0)
            variants.append(("inv", inv_bw))

        return variants

    def _score_text(self, text: str) -> int:
        t = text.strip()
        if not t:
            return 0

        compact = re.sub(r"\s+", "", t)
        score = len(compact)

        jp_bonus = sum(
            1
            for ch in t
            if ("\u3040" <= ch <= "\u30ff") or ("\u4e00" <= ch <= "\u9fff")
        )
        bad_penalty = sum(1 for ch in t if ch in "|/\\[]{}<>_=+~`")
        numeric_penalty = sum(1 for ch in t if ch.isdigit())

        # 日本語らしさを少し優遇、数字過多や記号過多は減点
        score += jp_bonus * 2
        score -= bad_penalty * 3
        score -= numeric_penalty

        # 1文字だけなど短すぎる結果は弱くする
        if len(compact) <= 2:
            score -= 10

        return score

    def image_to_text(self, image: Image.Image) -> str:
        self.ensure_reader()

        candidates = []

        for _, variant in self._build_variants(image):
            # 縦書き優先
            for angle in (90, 270, 0):
                rotated = variant.rotate(angle, expand=True)
                arr = np.array(rotated)
                results = self.reader.readtext(arr, detail=0, paragraph=False)

                text = "\n".join(str(x).strip() for x in results if str(x).strip())
                score = self._score_text(text)
                candidates.append((score, text))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1].strip() if candidates else ""


class ScreenCapture:
    @staticmethod
    def get_active_window():
        try:
            return gw.getActiveWindow()
        except Exception:
            return None

    @staticmethod
    def capture_active_window(delay_ms: int = 800) -> Image.Image:
        # Tkのボタンを押した直後にこのアプリ自身がアクティブになるので少し待つ
        import time
        time.sleep(max(0, delay_ms) / 1000.0)

        win = ScreenCapture.get_active_window()
        if win is None:
            raise RuntimeError("アクティブウィンドウを取得できませんでした。")

        left, top, width, height = win.left, win.top, win.width, win.height
        if width <= 0 or height <= 0:
            raise RuntimeError("アクティブウィンドウのサイズが取得できませんでした。")

        with mss.mss() as sct:
            shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
            img = Image.frombytes("RGB", shot.size, shot.rgb)
        return img

    @staticmethod
    def crop_largest_image_like_area(image: Image.Image) -> Image.Image:
        """
        アクティブウィンドウ全体から、画像っぽい一番大きい領域を推定して切り出す。
        完璧ではないが、ブラウザ内の漫画画像を取り出す用途向け。
        """
        src = pil_to_cv(image)
        gray = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)

        # UIの線や文字列のノイズを減らしつつ大きな領域を拾う
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 40, 140)

        kernel = np.ones((7, 7), np.uint8)
        dilated = cv2.dilate(edges, kernel, iterations=2)
        closed = cv2.morphologyEx(dilated, cv2.MORPH_CLOSE, kernel, iterations=2)

        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        h, w = gray.shape[:2]
        win_area = w * h
        best = None
        best_score = -1

        for cnt in contours:
            x, y, cw, ch = cv2.boundingRect(cnt)
            area = cw * ch
            if area < win_area * 0.08:
                continue

            aspect = cw / max(ch, 1)
            # 漫画ページ/画像領域っぽいスコア
            score = area

            # 極端に細長い帯は避ける
            if aspect < 0.18 or aspect > 6.0:
                score *= 0.2

            # 画面中央寄りを少し優遇
            cx = x + cw / 2
            cy = y + ch / 2
            center_dist = abs(cx - w / 2) + abs(cy - h / 2)
            score -= center_dist * 2

            if score > best_score:
                best_score = score
                best = (x, y, cw, ch)

        if best is None:
            return image

        x, y, cw, ch = best

        # 少し内側へ寄せてブラウザUIの境界線を減らす
        pad_x = int(cw * 0.01)
        pad_y = int(ch * 0.01)
        x = max(0, x + pad_x)
        y = max(0, y + pad_y)
        cw = max(1, cw - pad_x * 2)
        ch = max(1, ch - pad_y * 2)

        cropped = src[y:y + ch, x:x + cw]
        return cv_to_pil(cropped)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1100x760")

        self.voicevox = VoiceVoxClient()
        self.ocr = OCRReader()

        self.speakers = []
        self.last_audio_file = None
        self.last_captured_image = None
        self.last_cropped_image = None

        self.base_url_var = tk.StringVar(value=DEFAULT_VOICEVOX_URL)
        self.status_var = tk.StringVar(value="VOICEVOXを起動して、接続確認を押してください。")
        self.speaker_var = tk.StringVar()
        self.speed_var = tk.DoubleVar(value=1.0)
        self.delay_var = tk.IntVar(value=1200)
        self.auto_read_var = tk.BooleanVar(value=True)
        self.crop_image_only_var = tk.BooleanVar(value=True)

        self.build_ui()

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="VOICEVOX URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.base_url_var, width=36).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(top, text="接続確認", command=self.connect_voicevox).grid(row=0, column=2, padx=4)
        ttk.Button(top, text="話者一覧更新", command=self.load_speakers).grid(row=0, column=3, padx=4)
        top.columnconfigure(1, weight=1)

        settings = ttk.LabelFrame(self.root, text="設定", padding=10)
        settings.pack(fill="x", padx=10, pady=4)

        ttk.Label(settings, text="話者").grid(row=0, column=0, sticky="w")
        self.speaker_combo = ttk.Combobox(settings, textvariable=self.speaker_var, state="readonly", width=52)
        self.speaker_combo.grid(row=0, column=1, sticky="we", padx=6)

        ttk.Label(settings, text="話速").grid(row=0, column=2, sticky="w")
        ttk.Scale(settings, from_=0.5, to=1.8, variable=self.speed_var, orient="horizontal").grid(
            row=0, column=3, sticky="we", padx=6
        )
        self.speed_label = ttk.Label(settings, text="1.00")
        self.speed_label.grid(row=0, column=4, sticky="w")
        self.speed_var.trace_add("write", lambda *_: self.speed_label.config(text=f"{self.speed_var.get():.2f}"))

        ttk.Label(settings, text="取得待ち(ms)").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(settings, from_=200, to=5000, increment=100, textvariable=self.delay_var, width=8).grid(
            row=1, column=1, sticky="w", pady=(8, 0)
        )
        ttk.Checkbutton(settings, text="画像っぽい領域だけ切り出す", variable=self.crop_image_only_var).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(8, 0)
        )
        ttk.Checkbutton(settings, text="OCR後に自動読み上げ", variable=self.auto_read_var).grid(
            row=1, column=4, sticky="w", pady=(8, 0)
        )

        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        actions = ttk.LabelFrame(self.root, text="操作", padding=10)
        actions.pack(fill="x", padx=10, pady=4)

        ttk.Button(actions, text="アクティブウィンドウ取得 → OCR", command=self.capture_and_ocr).pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="手動画像を開く", command=self.open_image_file).pack(side="left", padx=4)
        ttk.Button(actions, text="OCR結果を読み上げ", command=self.speak_text_from_box).pack(side="left", padx=18)
        ttk.Button(actions, text="最後の取得画像を保存", command=self.save_last_image).pack(side="left", padx=4)
        ttk.Button(actions, text="クリア", command=self.clear_text).pack(side="left", padx=18)

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=6)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=2)
        body.add(right, weight=3)

        ttk.Label(left, text="OCR結果").pack(anchor="w", pady=(0, 6))
        self.text = tk.Text(left, wrap="word", font=("Yu Gothic UI", 14))
        self.text.pack(fill="both", expand=True)

        info = ttk.LabelFrame(right, text="ログ / 補足", padding=10)
        info.pack(fill="both", expand=True)

        self.info_text = tk.Text(info, wrap="word", font=("Consolas", 11))
        self.info_text.pack(fill="both", expand=True)
        self.info_text.insert(
            "1.0",
            "使い方:\n"
            "1. 読みたい漫画のウィンドウを前面に出す\n"
            "2. このアプリで「アクティブウィンドウ取得 → OCR」を押す\n"
            "3. すぐに漫画側ウィンドウをクリックして前面にする\n"
            "4. 待ち時間後に取得してOCRします\n\n"
            "注意:\n"
            "- Windows向けです\n"
            "- ブラウザやビューアのUIが混ざる場合があります\n"
            "- 完璧な画像抽出ではないので、うまく切り出せない時は「画像っぽい領域だけ切り出す」をOFFにして試してください\n"
        )
        self.info_text.config(state="disabled")

        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status_var).pack(anchor="w")

    def set_status(self, msg: str):
        self.status_var.set(msg)
        self.root.update_idletasks()

    def connect_voicevox(self):
        self.voicevox = VoiceVoxClient(self.base_url_var.get())
        if self.voicevox.health_check():
            self.set_status("VOICEVOX に接続できました。")
            self.load_speakers()
        else:
            self.set_status("VOICEVOX に接続できません。VOICEVOX本体またはEngineを起動してください。")
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
            self.set_status(f"話者一覧の取得に失敗しました: {e}")
            messagebox.showerror("取得失敗", str(e))

    def get_selected_speaker_id(self) -> int:
        label = self.speaker_var.get()
        for s in self.speakers:
            if s["label"] == label:
                return int(s["id"])
        raise RuntimeError("話者が選択されていません。")

    def open_image_file(self):
        path = filedialog.askopenfilename(
            title="画像を選択",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.webp *.bmp")],
        )
        if not path:
            return
        try:
            img = Image.open(path).convert("RGB")
            self.last_captured_image = img
            self.last_cropped_image = img
            self.run_ocr_on_image(img, source=f"手動画像: {path}")
        except Exception as e:
            messagebox.showerror("読込失敗", str(e))

    def capture_and_ocr(self):
        def task():
            try:
                delay = int(self.delay_var.get())
                self.set_status("取得ボタンを押しました。読みたいウィンドウを前面にしてください…")
                img = ScreenCapture.capture_active_window(delay_ms=delay)
                self.last_captured_image = img

                source_note = "アクティブウィンドウ全体"
                target = img
                if self.crop_image_only_var.get():
                    target = ScreenCapture.crop_largest_image_like_area(img)
                    self.last_cropped_image = target
                    source_note = "アクティブウィンドウ → 画像っぽい領域を切り出し"
                else:
                    self.last_cropped_image = img

                self.run_ocr_on_image(target, source=source_note)
            except Exception as e:
                self.set_status(f"取得失敗: {e}")
                messagebox.showerror("取得失敗", str(e))

        threading.Thread(target=task, daemon=True).start()

    def run_ocr_on_image(self, image: Image.Image, source: str = ""):
        def task():
            try:
                self.set_status("OCR 実行中です…")
                text = self.ocr.image_to_text(image)

                self.text.delete("1.0", "end")
                self.text.insert("1.0", text)

                if not text.strip():
                    self.set_status("OCRで文字を抽出できませんでした。条件を変えて再試行してください。")
                    return

                self.set_status(f"OCR 完了。{source}")
                if self.auto_read_var.get():
                    self.speak_text(text)
            except Exception as e:
                self.set_status(f"OCR失敗: {e}")
                messagebox.showerror("OCR失敗", str(e))

        threading.Thread(target=task, daemon=True).start()

    def speak_text_from_box(self):
        text = self.text.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("確認", "先にOCRするかテキストを入れてください。")
            return
        self.speak_text(text)

    def speak_text(self, text: str):
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
                if playsound is not None:
                    playsound(tmp_path)
                elif os.name == "nt":
                    os.startfile(tmp_path)
                else:
                    self.set_status(f"音声は生成されましたが自動再生できません。保存先: {tmp_path}")
                    return

                self.set_status("読み上げが完了しました。")
            except Exception as e:
                self.set_status(f"読み上げ失敗: {e}")
                messagebox.showerror("読み上げ失敗", str(e))

        threading.Thread(target=task, daemon=True).start()

    def save_last_image(self):
        target = self.last_cropped_image or self.last_captured_image
        if target is None:
            messagebox.showinfo("確認", "まだ画像を取得していません。")
            return

        path = filedialog.asksaveasfilename(
            title="画像保存",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg *.jpeg")],
        )
        if not path:
            return
        try:
            target.save(path)
            self.set_status(f"画像を保存しました: {path}")
        except Exception as e:
            messagebox.showerror("保存失敗", str(e))

    def clear_text(self):
        self.text.delete("1.0", "end")
        self.set_status("OCR結果をクリアしました。")


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
