# Один образ для parser и bot — общий код и зависимости (PLAN.md раздел 1: «разные
# процессы, не разные образы» — общий код, docker-compose разводит их по сервисам с
# разными command). См. README раздел «Деплой».
FROM python:3.12-slim

# tesseract-ocr — OCR-fallback для PDF без текстового слоя (parser/pdf.py, PLAN.md
# Фаза 3); rus — языковой пакет, без него pytesseract.image_to_string(lang="rus")
# падает с ошибкой отсутствия tessdata.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# -e (editable), не `pip install .`: db/catalog.py резолвит data/*.yaml через
# `__file__` относительно пакета — при обычной установке пакет копируется в
# site-packages без соседней data/ (data/ не объявлена как package data) и падает
# с CatalogError; editable-установка оставляет __file__ указывающим на реальную
# /app/data, которая тут же рядом (COPY . . включает и код, и данные). Проверено
# вживую при подготовке Фазы 7 — см. AGENTS.md раздел 16.
RUN pip install --no-cache-dir -e .

VOLUME ["/app/data"]

CMD ["python", "-m", "bot"]
