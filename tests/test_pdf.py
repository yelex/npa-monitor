"""Тесты parser/pdf.py. OCR-вызов (`pytesseract`) мокается — реального бинарника
`tesseract` в среде разработки/CI нет, см. докстринг parser/pdf.py.

Фикстуры используют латинский текст: builtin-шрифт PyMuPDF (`insert_text` без
указания `fontfile`) не кодирует кириллицу корректно при вставке "на лету" — это
особенность тестовой генерации PDF, не парсинга: реальные гос. PDF приходят с уже
встроенным шрифтом, поддерживающим кириллицу. Тест проверяет механику (текстовый
слой vs OCR-fallback), не конкретный алфавит.
"""
from __future__ import annotations

import fitz
import pytest

from parser.pdf import MIN_PAGE_TEXT_LENGTH, extract_text


def _make_pdf(*page_texts: str) -> bytes:
    document = fitz.open()
    for text in page_texts:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
    return document.tobytes()


def test_extract_text_uses_text_layer_when_present() -> None:
    pdf_bytes = _make_pdf("Government decree on support measures")

    result = extract_text(pdf_bytes)

    assert "Government decree" in result


def test_extract_text_joins_multiple_pages_with_blank_line() -> None:
    pdf_bytes = _make_pdf("First page of the document", "Second page of the document")

    result = extract_text(pdf_bytes)

    pages = result.split("\n\n")
    assert len(pages) == 2
    assert "First page" in pages[0]
    assert "Second page" in pages[1]


def test_extract_text_falls_back_to_ocr_for_blank_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_bytes = _make_pdf("")  # страница без текстового слоя

    calls = []

    def fake_image_to_string(image, lang=None):
        calls.append(lang)
        return "распознанный OCR текст"

    monkeypatch.setattr("parser.pdf.pytesseract.image_to_string", fake_image_to_string)

    result = extract_text(pdf_bytes)

    assert result == "распознанный OCR текст"
    assert calls == ["rus"]


def test_extract_text_skips_ocr_when_text_layer_is_long_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_text = "word " * 10  # заведомо длиннее MIN_PAGE_TEXT_LENGTH
    assert len(long_text) >= MIN_PAGE_TEXT_LENGTH
    pdf_bytes = _make_pdf(long_text)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("OCR не должен вызываться, когда есть текстовый слой")

    monkeypatch.setattr("parser.pdf.pytesseract.image_to_string", fail_if_called)

    result = extract_text(pdf_bytes)

    assert "word" in result
