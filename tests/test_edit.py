"""Edit model v2: validation, serialisation, fingerprints, v1→v2 migration and undo."""
import json
import math
import shutil
from dataclasses import fields
from pathlib import Path

import pytest

from framewise.core.errors import ProjectError
from framewise.project.document import ProjectDocument
from framewise.project.edit import (COLOR_SPECS, SECTIONS, Audio, Color, CropRect, Edit, Enhance, Export,
                                    Filter, Speed, Transform, Trim)
from framewise.project.io import FORMAT_VERSION, load_project, save_project
from framewise.project.migrations import migrate

PHASE1_FIXTURE = Path(__file__).parent / "data" / "phase1" / "phase1_project.fwproj"
GARBAGE = [None, "abc", float("nan"), float("inf"), -1e12, 1e12, [], {}]


# --- defaults --------------------------------------------------------------------------------
def test_default_edit_is_neutral_and_matches_phase1_behaviour():
    e = Edit()
    assert e == e.validated()
    assert e.trim.is_default and e.color.is_default
    assert e.transform == Transform() and e.transform.crop is None and e.transform.rotate == 0
    assert (e.speed.rate, e.speed.reverse) == (1.0, False)
    assert e.filter.preset == "none"
    assert (e.enhance.upscale, e.enhance.preset, e.enhance.target) == (True, "general", "2x")
    assert (e.enhance.denoise, e.enhance.deinterlace, e.enhance.sharpen, e.enhance.stabilize) == ("off", "off", 0, False)
    assert (e.audio.mute, e.audio.volume_db, e.audio.normalize, e.audio.replace_path) == (False, 0.0, False, "")
    x = e.export
    assert (x.format, x.video_codec, x.audio_codec, x.audio_bitrate, x.quality, x.rate_control) == \
        ("mp4", "libx264", "aac", 192, 18, "quality")
    assert (x.width, x.height, x.fps, x.output) == (0, 0, 0, "")


# --- validation ------------------------------------------------------------------------------
@pytest.mark.parametrize("section", list(SECTIONS))
@pytest.mark.parametrize("junk", GARBAGE)
def test_every_field_survives_garbage(section, junk):
    cls = SECTIONS[section]
    for f in fields(cls):
        obj = cls(**{f.name: junk}).validated()  # must never raise
        assert obj == obj.validated(), f"{section}.{f.name} not idempotent"
        for v in vars(obj).values():
            assert not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def test_color_specs_cover_every_color_field_and_clamp():
    assert {s.name for s in COLOR_SPECS} == {f.name for f in fields(Color)}
    c = Color(brightness=500, gamma=0.01, hue=-999, exposure=1.23456, vignette=-5).validated()
    assert (c.brightness, c.gamma, c.hue, c.vignette) == (100, 0.3, -180, 0)
    assert c.exposure == 1.25  # snapped to the 0.05 EV step
    assert all(s.lo <= s.default <= s.hi for s in COLOR_SPECS)
    assert all(s.tip for s in COLOR_SPECS), "every slider needs a tooltip"


def test_trim_validation_and_clamping():
    assert Trim(start=5, end=3).validated() == Trim(start=5, end=None)
    assert Trim(start=-2, end=4.12345).validated() == Trim(start=0, end=4.123)
    t = Trim(start=8, end=20).validated()
    assert t.clamp_to(10.0) == (8, 10.0)
    assert Trim(start=50).validated().clamp_to(10.0)[0] < 10.0  # start beyond the end is pulled back
    assert Trim().clamp_to(12.5) == (0.0, 12.5)


def test_crop_rect_is_always_even_and_fits_frame():
    r = CropRect(3, 5, 101, 57).validated()
    assert (r.x, r.y, r.w, r.h) == (2, 4, 100, 56)
    assert all(v % 2 == 0 for v in (r.x, r.y, r.w, r.h))
    f = CropRect(1800, 1000, 400, 200).fitted(1920, 1080)
    assert f.x + f.w <= 1920 and f.y + f.h <= 1080 and (f.w, f.h) == (400, 200)
    assert CropRect(0, 0, 5000, 5000).fitted(641, 361) == CropRect(0, 0, 640, 360)


def test_transform_validation():
    assert Transform(rotate=-90).validated().rotate == 270
    assert Transform(rotate=450).validated().rotate == 90
    assert Transform(rotate=45).validated().rotate == 0
    t = Transform(crop={"x": 11, "y": 0, "w": 99, "h": 50}, alignment="nope", crop_ratio="7:3").validated()
    assert t.crop == CropRect(10, 0, 98, 50) and t.alignment == "fit" and t.crop_ratio == "free"
    assert Transform(crop={"x": 1}).validated().crop is None


def test_other_sections_clamp():
    assert Speed(rate=10).validated().rate == 4.0 and Speed(rate=0.01).validated().rate == 0.25
    assert Filter(preset="sepia?", strength=250).validated() == Filter("none", 100)
    assert Enhance(denoise="max", stab_smoothing=500).validated().denoise == "off"
    assert Enhance(stab_smoothing=500).validated().stab_smoothing == 60
    assert Audio(volume_db=99, fade_in=-1, replace_fit="stretch").validated() == Audio(volume_db=12, replace_fit="trim")
    assert Export(width=1920, height=0).validated().width == 0  # half a resolution is dropped
    odd = Export(width=1281, height=721).validated()
    assert (odd.width, odd.height) == (1280, 720)  # yuv420 needs even dimensions


# --- immutability / editing -------------------------------------------------------------------
def test_with_section_returns_new_validated_edit():
    e = Edit()
    e2 = e.with_section("color", brightness=500, contrast=10)
    assert e.color.brightness == 0, "original must not change"
    assert (e2.color.brightness, e2.color.contrast) == (100, 10)
    assert e2.transform is e.transform
    with pytest.raises(ValueError):
        e.with_section("colour", brightness=1)
    with pytest.raises(ValueError):
        e.with_section("color", brightnes=1)
    assert e2.reset_section("color").color == Color()
    with pytest.raises(ValueError):
        e2.reset_section("nope")


# --- serialisation / fingerprint ---------------------------------------------------------------
def _busy_edit() -> Edit:
    return (Edit()
            .with_section("trim", start=1.5, end=9)
            .with_section("transform", rotate=90, flip_h=True, crop=CropRect(10, 20, 640, 360), alignment="blur")
            .with_section("color", exposure=0.5, temperature=-30, fade=12)
            .with_section("filter", preset="cinematic", strength=60)
            .with_section("enhance", upscale=False, denoise="quality", stabilize=True)
            .with_section("speed", rate=0.5, reverse=True)
            .with_section("audio", replace_path="C:\\Music\\track (1).mp3", fade_out=2)
            .with_section("export", format="webm", rate_control="size", target_mb=8))


def test_json_roundtrip_is_exact():
    e = _busy_edit()
    again = Edit.from_dict(json.loads(json.dumps(e.to_dict())))
    assert again == e
    assert again.fingerprint() == e.fingerprint()


@pytest.mark.parametrize("raw", [None, [], "edit", {"color": "red"}, {"transform": {"crop": [1, 2]}},
                                 {"unknown_section": {}}, {"speed": {"rate": "fast", "bogus": 1}}])
def test_from_dict_never_raises_on_damaged_input(raw):
    e = Edit.from_dict(raw)
    assert e == e.validated()


def test_fingerprint_is_stable_and_section_scoped():
    e = Edit()
    assert e.fingerprint() == Edit().fingerprint()
    c = e.with_section("color", contrast=5)
    assert c.fingerprint() != e.fingerprint()
    assert c.fingerprint("enhance", "export") == e.fingerprint("enhance", "export")
    assert c.fingerprint("color") != e.fingerprint("color")


# --- migration ---------------------------------------------------------------------------------
def test_phase1_project_file_loads_with_all_settings(tmp_path):
    copy = tmp_path / "old.fwproj"
    shutil.copy(PHASE1_FIXTURE, copy)
    before = copy.read_bytes()
    proj = load_project(copy)
    assert copy.read_bytes() == before, "loading must never rewrite the user's file"
    a, b = proj.items
    assert Path(a.path) == tmp_path / "media" / "family trip.final.v2.mp4"
    assert (a.edit.enhance.upscale, a.edit.enhance.preset, a.edit.enhance.target) == (True, "anime", "1080p")
    assert (a.edit.export.video_codec, a.edit.export.quality, a.edit.export.output) == ("libx265", 22, "")
    assert (b.edit.enhance.upscale, b.edit.export.output) == (False, "D:\\Videos\\out\\custom name.mp4")
    assert a.id == "9b68649cfd3d4a46824ba73856203373"
    # everything Phase 1 did not know about is neutral
    for it in proj.items:
        assert it.edit.color.is_default and it.edit.trim.is_default and it.edit.speed == Speed()

    save_project(proj, copy)
    raw = json.loads(copy.read_text(encoding="utf-8"))
    assert raw["version"] == FORMAT_VERSION == 2
    assert "settings" not in raw["items"][0] and "edit" in raw["items"][0]
    assert [i.edit for i in load_project(copy).items] == [a.edit, b.edit]


def test_migrate_handles_odd_v1_items():
    data = {"format": "framewise-project", "version": 1,
            "items": [{"path": "a.mp4"}, {"path": "b.mp4", "settings": "junk"}, "not-a-dict"]}
    out = migrate(data, 2)
    assert out["version"] == 2 and out["items"][0]["edit"] == {"enhance": {}, "export": {}}
    assert data["version"] == 1, "input dict must not be mutated"
    with pytest.raises(ValueError):
        migrate({"version": 0}, 2)


@pytest.mark.parametrize("version", [0, "2", True, None, 3])
def test_bad_versions_rejected(tmp_path, version):
    p = tmp_path / "x.fwproj"
    p.write_text(json.dumps({"format": "framewise-project", "version": version, "items": []}))
    with pytest.raises(ProjectError):
        load_project(p)


def test_unicode_and_odd_paths_roundtrip(tmp_path):
    folder = tmp_path / "My Videos" / "日本 trip"
    folder.mkdir(parents=True)
    media = folder / "clip.final.v2 — été.mp4"
    media.write_bytes(b"x")
    doc = ProjectDocument()
    doc.add_media([str(media)])
    path = tmp_path / "proj with spaces.fwproj"
    doc.save(path)
    assert "clip.final.v2 — été.mp4" in path.read_text(encoding="utf-8")
    other = ProjectDocument()
    other.open(path)
    assert other.project.items[0].exists() and other.project.items[0].name == media.name


# --- undo ---------------------------------------------------------------------------------------
def test_each_edit_change_is_one_undo_step_and_restores_exactly(tmp_path):
    doc = ProjectDocument()
    (item,) = doc.add_media([str(tmp_path / "a.mp4")])
    start = item.edit
    doc.update_edit(item.id, "color", text="Change brightness", brightness=30)
    doc.update_edit(item.id, "transform", rotate=90, flip_v=True)
    doc.update_edit(item.id, "transform", rotate=90, flip_v=True)  # identical → not recorded
    after = item.edit
    assert doc.undo.undo_text() == "Change transform"
    doc.reset_section(item.id, "color")
    assert item.edit.color == Color() and doc.undo.undo_text() == "Reset color"
    doc.set_edit(item.id, _busy_edit(), "Apply look")
    doc.undo.undo()
    doc.undo.undo()
    assert item.edit == after
    doc.undo.undo()
    doc.undo.undo()
    assert item.edit == start
    assert doc.undo.undo_text() == "Import a.mp4"
    while doc.undo.redo():
        pass
    assert item.edit == _busy_edit().validated()


def test_invalid_edits_record_nothing(tmp_path):
    doc = ProjectDocument()
    (item,) = doc.add_media([str(tmp_path / "a.mp4")])
    steps = doc.undo.undo_text()
    with pytest.raises(ValueError):
        doc.update_edit(item.id, "nope", x=1)
    with pytest.raises(ValueError):
        doc.update_edit(item.id, "color", not_a_field=1)
    assert doc.undo.undo_text() == steps
    doc.update_edit("missing-id", "color", brightness=1)  # unknown item: ignored
    assert doc.undo.undo_text() == steps
