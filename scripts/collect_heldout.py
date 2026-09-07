"""Сбор кандидатов held-out выборки для калибровки Stage B (SPEC_hybrid_classifier).

Выкачивает day-листинги pravo.gov.ru за N дней назад (через ru_proxy, как парсер),
прогоняет каждый заголовок через Stage A (keyword-путь) и раскладывает:
  - candidates_missed: Stage A НЕ нашёл ЖС, но topic_block/document_marker совпали
    (профиль кейса Камчатки — кандидаты в выборку 2 «потерянные»);
  - irrelevant_no_topic: ни ЖС, ни тематики (кандидаты в выборку 3 «контроль»);
  - stage_a_hits: Stage A нашёл ЖС (для сверки, не в выборку).

Результат — JSON в /tmp/heldout_candidates.json для ручной курации.

Запуск (host, .venv):
    RU_PROXY_URL=http://95.142.42.28:8888 .venv/bin/python scripts/collect_heldout.py --days 60
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

from parser.classifier import Classifier
from parser.models import Publication
from parser.sources.pravo_gov import fetch_documents

MOSCOW_TZ = dt.timezone(dt.timedelta(hours=3))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="сколько дней назад обходить")
    ap.add_argument("--proxy", default="http://95.142.42.28:8888")
    ap.add_argument("--out", default="/tmp/heldout_candidates.json")
    ap.add_argument("--resume", action="store_true",
                    help="подхватить существующий --out (дни с датами в нём пропускаются)")
    args = ap.parse_args()

    clf = Classifier.load()
    today = dt.datetime.now(MOSCOW_TZ).date()
    buckets: dict[str, list[dict]] = {
        "candidates_missed": [],
        "irrelevant_no_topic": [],
        "stage_a_hits": [],
    }
    done_days: set[str] = set()
    if args.resume:
        try:
            with open(args.out, encoding="utf-8") as f:
                buckets = json.load(f)
            done_days = {r["date"] for b in buckets.values() for r in b}
            print(f"resume: {len(done_days)} дней уже собрано, пропускаю")
        except FileNotFoundError:
            pass

    for back in range(1, args.days + 1):
        day = today - dt.timedelta(days=back)
        if day.isoformat() in done_days:
            continue
        pubs: list[Publication] = []
        page = 1
        while page <= 30:  # предохранитель как в оркестраторе
            try:
                chunk = fetch_documents(
                    period="day", date=day.strftime("%d.%m.%Y"), page=page, ru_proxy_url=args.proxy
                )
            except Exception as e:  # noqa: BLE001
                print(f"{day} p{page}: FETCH ERR {e}")
                break
            if not chunk:
                break
            pubs.extend(chunk)
            page += 1
        for p in pubs:
            if p.published_at is None:
                continue
            tr = clf.explain(p)
            rec = {
                "title": p.title,
                "url": p.url,
                "date": day.isoformat(),
                "topic": list(tr.topic_block_matches),
                "markers": list(tr.document_marker_matches),
                "categories": [c.value for c in tr.category_matches],
            }
            if tr.category_matches:
                buckets["stage_a_hits"].append(rec)
            elif tr.topic_block_matches or tr.document_marker_matches:
                buckets["candidates_missed"].append(rec)
            else:
                buckets["irrelevant_no_topic"].append(rec)
        print(f"{day}: docs={len(pubs)} missed={len(buckets['candidates_missed'])} "
              f"irr={len(buckets['irrelevant_no_topic'])} hits={len(buckets['stage_a_hits'])}", flush=True)
        # чекпоинт после каждого дня: убийство процесса ничего не теряет
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(buckets, f, ensure_ascii=False)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(buckets, f, ensure_ascii=False, indent=1)
    print(f"\nsaved -> {args.out}: "
          f"missed={len(buckets['candidates_missed'])} "
          f"irrelevant={len(buckets['irrelevant_no_topic'])} "
          f"hits={len(buckets['stage_a_hits'])}")


if __name__ == "__main__":
    main()
