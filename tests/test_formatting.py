import json

from garmin_mcp.formatting import TRUNCATION_NOTE, drop_keys, pick, to_json_text


def test_small_payload_is_pretty_printed():
    text = to_json_text({"a": 1}, 1000)
    assert text == '{\n  "a": 1\n}'
    assert json.loads(text) == {"a": 1}


def test_medium_payload_falls_back_to_compact():
    data = {f"key{i}": i for i in range(50)}
    pretty = json.dumps(data, indent=2)
    compact = json.dumps(data, separators=(",", ":"))
    text = to_json_text(data, len(pretty) - 10)
    assert len(text) <= len(pretty) - 10
    assert len(text) == len(compact)
    assert json.loads(text) == data


def test_long_list_is_trimmed_but_stays_valid_json():
    data = [{"activityId": i, "name": "x" * 100} for i in range(500)]
    text = to_json_text(data, 4000)
    parsed = json.loads(text)
    assert len(text) <= 4000
    assert 0 < len(parsed["items"]) < 500
    assert "500 items" in parsed[TRUNCATION_NOTE]


def test_large_dict_drops_biggest_fields():
    data = {"summary": {"steps": 1000}, "series": ["y" * 50 for _ in range(500)]}
    text = to_json_text(data, 2000)
    parsed = json.loads(text)
    assert parsed["summary"] == {"steps": 1000}
    assert "series" in parsed[TRUNCATION_NOTE]


def test_none_and_empty():
    assert to_json_text(None, 100) == "null"
    assert to_json_text([], 100) == "[]"


def test_drop_keys_is_recursive():
    data = {"a": {"heavy": [1, 2, 3], "keep": 1}, "b": [{"heavy": 1, "keep": 2}]}
    assert drop_keys(data, {"heavy"}) == {"a": {"keep": 1}, "b": [{"keep": 2}]}


def test_pick_skips_missing_and_null():
    assert pick({"a": 1, "b": None, "c": 3}, ["a", "b", "d"]) == {"a": 1}
    assert pick("not a dict", ["a"]) == {}
