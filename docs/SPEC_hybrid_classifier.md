# SPEC: Stage B — гибридный fallback-классификатор ЖС (BM25 + embedding)

## Статус
- [x] Done: реализовано, ревью claude APPROVE (2 мелких замечания закрыты), 448 passed; пороги стартовые — калибровка по held-out pending

## Контекст
Кейс 412-П Камчатки (24.08): пагинация достала документ, но keyword-классификатор
не узнал ЖС — заголовок «...в связи с проведением специальной военной операции»
не совпал ни с одним словом из `life_situations.yaml::svo`. Это второй случай
дрейфа словаря (первый — 20.08, «набор социальных услуг»). Точечные патчи yaml
не закрывают класс проблемы. План гибрида (обсуждён с claude, полный текст
/tmp/claude_hybrid_classifier_plan.md): BM25 + embedding косинусная близость,
слияние RRF, второй проход поверх keyword-пути.

Эмбеддер — из проекта auto, проверен в проде: rubert-tiny2 ONNX,
`/root/dev/auto/agent/src/src/product_agent/extraction_service/models/rubert-tiny2/`
(model_quantized.onnx 29 МБ + tokenizer.json + vocab). Подключение по образцу
`auto/agent/src/src/product_agent/agent/agents/semantic_retriever.py`:
`ort.InferenceSession(providers=['CPUExecutionProvider'])` + `HFTokenizer.from_file`,
mean-pooling, нормализация. Артефакты скопировать в `parser/models/rubert-tiny2/`
(в git — только если суммарно <50 МБ, иначе download-скрипт; quantized-вариант
достаточен). В контейнер — через Dockerfile COPY.

## Предполагаемый фикс
**Stage A** (`parser/classifier.py::match_categories`) — без изменений.

**Stage B** (новый модуль `parser/hybrid_classifier.py`):
1. Условие запуска: Stage A не нашёл ЖС-категорию, НО topic_block ИЛИ
   document_marker совпали.
2. Якорные фразы — из плана claude (7–8 на ЖС, реальные обороты НПА, п.4
   /tmp/claude_hybrid_classifier_plan.md), хранить в `data/hybrid_anchors.yaml`
   (расширяемо без кода).
3. На каждую ЖС: `cos_top1` (rubert-tiny2, косинус к якорям) и `bm25_top1`
   (rank_bm25, токенизация через существующий `parser/ru_stem`).
4. Слияние — RRF (k=60). Категория принимается при: явный лидер (зазор RRF от
   второй ≥ порога) И (`cos_top1 ≥ T_cos` ИЛИ `bm25_top1 ≥ T_bm25`).
5. Пометка источника решения `hybrid` в `ClassificationTrace` + якорь-победитель
   и оба сырых скора (объяснимость для эксперта).
6. Приоритет hybrid-сигналов — кап на MEDIUM.
7. Near-miss (категория не прошла порог) — в trace-лог для аудита якорей.
8. Fallback: эмбдер недоступен (нет весов/OOM/нет ort) → Stage B пропускается
   целиком (BM25 один не гоняем — по плану claude; пересмотрим после калибровки),
   один warning при старте, поведение = сегодняшнее. Паттерн — как
   `parser/llm.py::get_default_client()`.

## Калибровка и приёмка
- Выборка 1 (не-регрессия): ~146 исторических сигналов из БД — precision ≥ 0.90.
- Выборка 2 (held-out, собрать ДО приёмки): 20–30 реальных заголовков pravo.gov,
  которые keyword не находит (искать по канцеляризмам «в связи с прохождением»,
  «отдельным категориям граждан» через поиск pravo.gov/Яндекс) — recall ≥ 0.60.
- Выборка 3 (контроль): 15–20 заведомо нерелевантных (рыболовство, «пятилетка
  Китая» и т.п. из AGENTS.md разд.16) — precision ≥ 0.85.
- Калибровка порогов (T_cos, T_bm25, RRF-зазор) — leave-one-out на выборке 2+3.
- Скрипт калибровки: `scripts/calibrate_hybrid.py` — печатает метрики на трёх
  выборках при заданных порогах (без авто-тюнинга в проде).

## Как воспроизвести
- Юнит: заголовок 412-П (fixture) — Stage A пуст → Stage B → SVO, trace=hybrid.
- Юнит: «Пятилетка Китая» → не релевантно.

## Вне scope
- LLM-классификация; автопополнение якорей; BM25-solo режим; другие источники.

## Ревью
- [ ] claude -p (после реализации)
