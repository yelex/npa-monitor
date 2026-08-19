"""PDF → текст, PLAN.md Фаза 3 («многие НПА приложены как PDF», AGENTS.md раздел 4).

Текстовый слой — основной путь (`PyMuPDF`/`fitz`, без внешних бинарников). Если у
страницы текстового слоя нет или он короче `MIN_PAGE_TEXT_LENGTH` — OCR-fallback через
`pytesseract`. Требует установленный системный бинарник `tesseract` + языковой пакет
`rus` на машине, где выполняется парсер (не ставится через pip — системная зависимость,
добавить в Dockerfile на Фазе 7 «Деплой»). Юнит-тесты мокают вызов OCR, не полагаются
на реально установленный `tesseract` (его нет в CI/локальной среде разработки).
"""
from __future__ import annotations

import io

import fitz
import pytesseract
from PIL import Image

MIN_PAGE_TEXT_LENGTH = 20
OCR_DPI = 200
OCR_LANG = "rus"


def _ocr_page(page: fitz.Page) -> str:
    pixmap = page.get_pixmap(dpi=OCR_DPI)
    image = Image.open(io.BytesIO(pixmap.tobytes("png")))
    return pytesseract.image_to_string(image, lang=OCR_LANG)


def extract_text(pdf_bytes: bytes) -> str:
    """Извлекает текст из PDF постранично: текстовый слой, OCR — если слоя нет/он короткий.

    Страницы соединяются пустой строкой — приемлемо для дальнейшей keyword/regex-
    классификации (Фаза 4), точное сохранение вёрстки документа не требуется.
    """
    pages_text: list[str] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        for page in document:
            text = page.get_text().strip()
            if len(text) < MIN_PAGE_TEXT_LENGTH:
                text = _ocr_page(page).strip()
            pages_text.append(text)
    return "\n\n".join(pages_text)
