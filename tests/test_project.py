import json

import pytest

from framewise.core.errors import ProjectError
from framewise.project.document import ProjectDocument
from framewise.project.io import load_project, save_project
from framewise.project.model import ClipSettings, MediaItem, Project
from framewise.project.undo import UndoStack


class _Set:
    def __init__(self, store, key, value):
        self.store, self.key, self.value, self.text = store, key, value, "set"
        self.old = None

    def do(self):
        self.old = self.store.get(self.key)
        self.store[self.key] = self.value

    def undo(self):
        self.store[self.key] = self.old


def test_undo_redo_and_clean_state():
    st, store = UndoStack(), {}
    assert st.is_clean()
    st.push(_Set(store, "a", 1))
    st.push(_Set(store, "a", 2))
    assert store["a"] == 2 and not st.is_clean()
    st.undo()
    assert store["a"] == 1
    st.set_clean()
    st.redo()
    assert store["a"] == 2 and not st.is_clean()
    st.undo()
    assert st.is_clean()
    st.undo()
    st.push(_Set(store, "b", 1))  # branching away from the saved state
    assert not st.is_clean() and not st.can_redo()


def test_undo_limit_drops_oldest():
    st, store = UndoStack(limit=3), {}
    for v in range(5):
        st.push(_Set(store, "a", v))
    while st.undo():
        pass
    assert store["a"] == 1


def test_settings_validation_repairs_bad_values():
    s = ClipSettings.from_dict({"preset": "bogus", "target": "9x", "encoder": "nope", "quality": 99, "extra": 1})
    assert (s.preset, s.target, s.encoder, s.quality) == ("general", "2x", "libx264", 35)


def test_project_save_load_roundtrip_with_relative_paths(tmp_path):
    media = tmp_path / "videos" / "a.mp4"
    media.parent.mkdir()
    media.write_bytes(b"x")
    proj = Project([MediaItem(str(media), ClipSettings(preset="anime", quality=20))])
    path = tmp_path / "p.fwproj"
    save_project(proj, path)
    raw = json.loads(path.read_text())
    assert raw["items"][0]["path"] == "videos/a.mp4"
    loaded = load_project(path)
    assert loaded.items[0].settings == proj.items[0].settings
    assert loaded.items[0].id == proj.items[0].id
    assert loaded.items[0].exists()


@pytest.mark.parametrize("content", ["nope", "{}", json.dumps({"format": "framewise-project", "version": 99})])
def test_load_rejects_bad_files(tmp_path, content):
    p = tmp_path / "bad.fwproj"
    p.write_text(content)
    with pytest.raises(ProjectError):
        load_project(p)


def test_document_edits_are_undoable_and_emit_events(tmp_path):
    doc, events = ProjectDocument(), []
    doc.subscribe(lambda e, p: events.append(e))
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    items = doc.add_media([str(a), str(b), str(a)])
    assert len(items) == 2 and doc.dirty
    assert doc.add_media([str(a)]) == []  # duplicates ignored
    doc.update_settings(items[0].id, quality=25)
    doc.update_settings(items[0].id, quality=25)  # no-op change is not recorded
    doc.remove_media([items[1].id])
    assert [i.name for i in doc.project.items] == ["a.mp4"]
    doc.undo.undo()
    assert [i.name for i in doc.project.items] == ["a.mp4", "b.mp4"]
    doc.undo.undo()
    assert doc.project.items[0].settings.quality == 18
    doc.undo.undo()
    assert doc.project.items == [] and not doc.dirty
    assert {"added", "removed", "settings", "state"} <= set(events)


def test_document_save_open_new(tmp_path):
    doc = ProjectDocument()
    doc.add_media([str(tmp_path / "a.mp4")])
    with pytest.raises(ValueError):
        doc.save()
    doc.save(tmp_path / "x.fwproj")
    assert not doc.dirty and doc.title == "x"
    other = ProjectDocument()
    other.open(tmp_path / "x.fwproj")
    assert len(other.project.items) == 1 and not other.dirty
    with pytest.raises(ProjectError):
        other.open(tmp_path / "missing.fwproj")
    assert len(other.project.items) == 1  # failed open leaves state untouched
    other.new()
    assert other.project.items == [] and other.path is None
