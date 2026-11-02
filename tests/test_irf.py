import pytest

from irris import irf


def test_irf_record_is_kept():
    r = irf.normalize({"irf": "1.0", "type": "Reading", "source": "line-3/plc", "id": "x1",
                       "time": "2026-11-02T08:15:00Z", "subject": "asset:press-7",
                       "data": {"value": 182.4, "unit": "C"}, "units": {"value": "C"}}, now=0)
    assert r["type"] == "reading" and r["id"] == "x1" and r["subject"] == "asset:press-7"
    assert r["time"] == 1793607300.0
    assert "value: 182.4 C" in irf.body(r)


def test_any_json_is_wrapped():
    recs = irf.normalize_many([{"order": 7, "qty": 3}, {"order": 8}], default_type="work_order",
                              default_source="erp", now=5)
    assert [r["type"] for r in recs] == ["work_order", "work_order"]
    assert recs[0]["data"] == {"order": 7, "qty": 3} and recs[0]["source"] == "erp" and recs[0]["time"] == 5


def test_unknown_fields_move_into_data():
    r = irf.normalize({"irf": "1.0", "type": "event", "machine": "press-7", "data": {"code": 41}}, now=0)
    assert r["data"] == {"machine": "press-7", "code": 41}


def test_scalars_and_records_wrapper():
    assert irf.normalize_many({"records": [{"type": "note", "text": "hi"}]}, now=0)[0]["text"] == "hi"
    assert irf.normalize(42, now=0)["data"] == {"value": 42}


def test_errors_name_the_record():
    with pytest.raises(irf.IRFError, match="record 1"):
        irf.normalize_many([{"type": "note", "text": "ok"}, {"type": "note", "time": "yesterday", "text": "x"}], now=0)
    with pytest.raises(irf.IRFError):
        irf.normalize({"type": "note"}, now=0)


def test_millisecond_time():
    assert irf.parse_time(1793607300000, 0) == 1793607300.0


def test_chunk_splits_long_text():
    parts = irf.chunk("word " * 2000, size=1200)
    assert len(parts) > 5 and all(len(p) <= 1200 for p in parts)
