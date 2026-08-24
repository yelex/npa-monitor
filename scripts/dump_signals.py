from db.session import make_engine, make_session_factory, session_scope
from db.models import Signal
from config import Settings

settings = Settings()
engine = make_engine(settings.database_path)
factory = make_session_factory(engine)

with session_scope(factory) as session:
    signals = session.query(Signal).order_by(Signal.created_at.desc()).all()
    print(f"Всего сигналов: {len(signals)}\n")
    for s in signals:
        cats = ", ".join(c.category.value for c in s.categories)
        print(f"[{s.id}] {s.status.value} | {s.priority.value} | {cats} | {s.region.value}")
        print(f"    {s.title or '(без названия)'}")
        print(f"    event={s.event_type.value} source={s.source_url}")
        if s.npa_link:
            print(f"    npa_link={s.npa_link}")
        print(f"    created_at={s.created_at} measure_id={s.measure_id}")
        print()
