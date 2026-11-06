from pathlib import Path

from opencrawl.research.subjects import load_subjects


def test_priority_field_kept(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "subjects.yaml").write_text(
        "subjects:\n- {id: a, name: Ann Able, priority: true, websites: ['https://example.org']}\n- {id: b, name: Bob Baker}\n")
    c = {x["id"]: x for x in load_subjects(tmp_path)}
    assert c["a"]["priority"] is True and not c["b"].get("priority")
    assert c["a"]["websites"] == ["https://example.org"]


def test_starter_subjects_file_loads_empty():
    root = Path(__file__).resolve().parents[1]
    assert load_subjects(root) == []   # the starter file ships with no subjects: nobody is researched by default
