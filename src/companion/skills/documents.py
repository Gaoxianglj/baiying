"""按内容寻址的 PDF 文本/页图/OCR 缓存；调用前仍须按原文件核对聊天权限。"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from companion.skills.base import SkillContext, SkillError

_PDFIUM_LOCK = threading.RLock()  # PDFium 连不同文档也不能并发调用。


class Documents:
    def __init__(self, folder: Path, vision: Callable[[Path, int], str] | None = None) -> None:
        self.folder, self.vision = folder, vision

    @staticmethod
    def _check(ctx: SkillContext) -> None:
        if ctx.stop_event and ctx.stop_event.is_set():
            raise SkillError("任务已停止；已识别的页面保留在缓存中")
        if ctx.deadline and time.monotonic() > ctx.deadline:
            raise SkillError("任务达到运行时限；识别结果已逐页保存，可继续任务")

    def _folder(self, data: bytes) -> Path:
        folder = self.folder / hashlib.sha256(b"pdf-v1\0" + data).hexdigest()
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def _json(path: Path, data: object) -> None:
        temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        temp.replace(path)

    def pages(self, data: bytes) -> list[str]:
        from pypdf import PdfReader
        from pypdf.errors import DependencyError, PyPdfError, WrongPasswordError

        cache = self._folder(data) / "text.json"
        if cache.exists():
            try:
                return json.loads(cache.read_text("utf-8"))["pages"]
            except (ValueError, KeyError):
                pass
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                raise SkillError("这个 PDF 有密码，读不了")
            pages = [(p.extract_text() or "").strip() for p in reader.pages]
        except DependencyError as e:
            raise SkillError("PDF 加密依赖缺失，请在项目中运行 uv sync 后重启") from e
        except WrongPasswordError as e:
            raise SkillError("这个 PDF 有密码，读不了") from e
        except (PyPdfError, ValueError, KeyError, TypeError, IndexError) as e:
            raise SkillError("PDF 文本提取失败，请尝试按页查看或更换文件") from e
        if not pages:
            raise SkillError("这个 PDF 没有页面")
        self._json(cache, {"pages": pages})
        return pages

    def render(self, data: bytes, number: int) -> Path:
        import pypdfium2 as pdfium

        output = self._folder(data) / f"page-{number:06}.jpg"
        if output.exists():
            return output
        with _PDFIUM_LOCK:
            try:
                doc = pdfium.PdfDocument(data, password="")
                try:
                    if number < 1 or number > len(doc):
                        raise SkillError("没有这个 PDF 页码")
                    page = doc[number - 1]
                    try:
                        width, height = page.get_size()
                        side = max(width, height)
                        if not math.isfinite(side) or side <= 0:
                            raise SkillError("页面尺寸异常，不能渲染")
                        bitmap = page.render(scale=min(2000 / side, 3.0))
                        try:
                            image = bitmap.to_pil().convert("RGB")
                            temp = output.with_name(output.name + "." + uuid4().hex + ".tmp")
                            image.save(temp, format="JPEG", quality=90)
                            image.close()
                            temp.replace(output)
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
                finally:
                    doc.close()
            except SkillError:
                raise
            except Exception as e:
                raise SkillError("PDF 页面渲染失败，文件可能有密码或已损坏") from e
        return output

    def ocr(self, data: bytes, number: int, ctx: SkillContext) -> str:
        self._check(ctx)
        cache = self._folder(data) / f"ocr-{number:06}.json"
        if cache.exists():
            try:
                return json.loads(cache.read_text("utf-8"))["text"]
            except (ValueError, KeyError):
                pass
        if ctx.stop_event and ctx.stop_event.is_set():
            raise SkillError("任务已停止；识别结果已逐页保存，继续任务可复用")
        if self.vision is None:
            raise SkillError("这是扫描页，需要启用 PDF 视觉识别，或使用【读页】查看页面")
        if ctx.task_book and ctx.task_id:
            ctx.task_book.progress(ctx.task_id, f"正在识别 PDF 第 {number} 页，已完成页面可复用")
        text = self.vision(self.render(data, number), number).strip()
        if not text:
            raise SkillError(f"PDF 第 {number} 页未能识别出内容")
        self._json(cache, {"text": text, "method": "model-vision", "page": number})
        return text

    def extract(self, data: bytes, ctx: SkillContext) -> str:
        parts = []
        for number, raw in enumerate(self.pages(data), 1):
            self._check(ctx)
            if ctx.stop_event and ctx.stop_event.is_set():
                raise SkillError("任务已停止；已识别的页面保留在缓存中")
            text = raw
            method = "文字层"
            if len(raw.strip()) < 20:
                text, method = self.ocr(data, number, ctx), "视觉识别，疑难文字需核对页图"
            parts.append(f"## PDF 第 {number} 页（{method}）\n\n{text}")
        return "\n\n".join(parts)

    def searchable_pages(self, data: bytes) -> list[str]:
        pages = self.pages(data)
        folder = self._folder(data)
        for i, text in enumerate(pages):
            cache = folder / f"ocr-{i + 1:06}.json"
            if len(text) < 20 and cache.exists():
                try:
                    pages[i] = json.loads(cache.read_text("utf-8"))["text"]
                except (ValueError, KeyError):
                    pass
        return pages

    def read_pages(self, data: bytes, start: int, end: int, ctx: SkillContext) -> tuple[str, list[Path]]:
        pages = self.pages(data)
        if start < 1 or end < start or end > len(pages):
            raise SkillError(f"页码范围不对，这个 PDF 共 {len(pages)} 页")
        parts, images = [], []
        for number in range(start, end + 1):
            self._check(ctx)
            if ctx.stop_event and ctx.stop_event.is_set():
                raise SkillError("任务已停止")
            images.append(self.render(data, number))
            text = pages[number - 1]
            parts.append(f"【PDF 第 {number}/{len(pages)} 页】\n"
                         + (text[:18000] if text else "此页没有文字层，请根据附上的页图读取。"))
        return "\n\n".join(parts), images
