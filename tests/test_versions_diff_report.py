# coding: utf-8

"""The report's model — the tree a person reads, before anyone draws it.

The page is drawn by the panel, so what this module owes it is the shape: the nesting, the
words, the order, the caps, and the icon and colour of every row. These assert on that
model. The diff behind it is real (tests/snapshots.py), so a change that stops the engine
from emitting a level of the tree fails here too.

`drawn()` joins a row's fields the way a row reads - name, value, detail, action - so an
assertion can still be written as the line a person sees rather than as a field path.
"""

import json
import os
import re

from supervisely.annotation.obj_class import ObjClass
from supervisely.annotation.obj_class_collection import ObjClassCollection
from supervisely.geometry.any_geometry import AnyGeometry
from supervisely.geometry.rectangle import Rectangle
from supervisely.project.project_meta import ProjectMeta
from supervisely.project.versioning.snapshot_reader import VersionSnapshot
from supervisely.project.versioning.tag_schema import OWNER_FIGURE, OWNER_ITEM, OWNER_OBJECT

from snapshots import (
    SnapshotBuilder,
    VideoSnapshotBuilder,
    VolumeSnapshotBuilder,
    one_dataset,
)
from versions_diff import DIFF_FILE_NAME, VersionRef, compute_diff
from versions_diff_report import VersionsDiffReport
from versions_diff_report.generator import ITEM_TREE_LIMIT


def report_of(tmp_path, builder_from, builder_to, **kwargs):
    """Compute a diff, publish it into a directory, shape the model. Returns (dir, model)."""
    output_dir = os.path.join(str(tmp_path), "report")
    os.makedirs(output_dir, exist_ok=True)

    with VersionSnapshot.open_archive(
        builder_from.build(), payload_dir=os.path.join(str(tmp_path), "pa")
    ) as snapshot_from:
        with VersionSnapshot.open_archive(
            builder_to.build(), payload_dir=os.path.join(str(tmp_path), "pb")
        ) as snapshot_to:
            result = compute_diff(
                snapshot_from,
                snapshot_to,
                version_from=VersionRef(id=10, number=4, created_at="2026-01-01T00:00:00.000Z"),
                version_to=VersionRef(id=17, number=7, created_at="2026-02-02T00:00:00.000Z"),
                output_dir=output_dir,
                **kwargs,
            )

    with open(os.path.join(output_dir, DIFF_FILE_NAME), "w", encoding="utf-8") as f:
        json.dump(result.summary, f)

    return output_dir, VersionsDiffReport(output_dir).context()


def dump(model) -> str:
    """The model as JSON, for questions about the shape rather than about the page."""
    return json.dumps(model, ensure_ascii=False)


def nodes(model) -> list:
    """Every row of every item's tree, in the order they are drawn."""
    found = []

    def walk(groups):
        for group in groups:
            for node in group["entries"]:
                found.append(node)
                walk(node.get("groups") or [])

    for dataset in model.get("tree", []):
        for item in dataset.get("entries", []):
            walk(item.get("groups") or [])

    return found


def groups(model) -> list:
    """Every group heading in every item: added, removed, changed."""
    found = []

    def walk(groups_):
        for group in groups_:
            found.append(group)
            for node in group["entries"]:
                walk(node.get("groups") or [])

    for dataset in model.get("tree", []):
        for item in dataset.get("entries", []):
            walk(item.get("groups") or [])

    return found


def line_of(node, under=None) -> str:
    """One row as the panel puts it on one line: icon, name, detail, action, id.

    `under` is the group heading this row sits below. A row whose action is the same word as
    that heading does not repeat it - VersionsDiffNode.vue decides that with `action_key`,
    and so does this, or an assertion would read a word the page never draws.

    An id is written `#500` so that an assertion about one cannot match a count or a frame
    number somewhere else on the page.
    """
    if node.get("kind") == "tag":
        name = f"{node['name']}:" if node.get("value") else node.get("name")
        action = node.get("action") if node.get("action_key") != under else None
        pieces = [node.get("icon"), name, node.get("value"), node.get("frames"), action]
    else:
        parts = node.get("parts") or {}
        action = parts.get("action") if parts.get("action_key") != under else None
        pieces = [
            node.get("icon"),
            parts.get("class"),
            parts.get("detail"),
            action,
            f"#{parts['id']}" if parts.get("id") else None,
        ]

    return " ".join(str(piece) for piece in pieces if piece)


def _counts_line(entry) -> str:
    return ", ".join(
        " ".join(
            piece
            for piece in [
                part["label"],
                f"+{part['added']}" if part.get("added") else None,
                f"\u2212{part['removed']}" if part.get("removed") else None,
                f"pencil {part['changed']}" if part.get("changed") else None,
            ]
            if piece
        )
        for part in entry.get("counts", [])
    )


def drawn(model) -> str:
    """Everything the report says, in the order a person reads it.

    The overview first, then every dataset with its items and every row under them, each row
    rendered by `line_of`. Ordering assertions are written against this, so it has to follow
    the page rather than the dict.
    """
    lines = [json.dumps(model.get("summary"), ensure_ascii=False)]
    when = model.get("when") or {}
    lines.append(f"{when.get('from', '')} \u2192 {when.get('to', '')}")
    lines.append(f"counted {model.get('counted') or ''}")

    items = model.get("items") or {}
    for status in items.get("counts", []):
        lines.append(f"{status['label']} {status['count']}")
    lines.append(f"{items.get('drawn')} of {items.get('total')} items")
    lines.append(model.get("modified_icon") or "")

    meta = model.get("meta") or {}
    for key in ("classes", "tag_metas", "settings"):
        section = meta.get(key) or {}
        lines.append(f"Definitions {key} {section.get('hidden')} {section.get('omitted')}")
        for row in section.get("shown") or []:
            lines.append(json.dumps(row, ensure_ascii=False))

    counted = (model.get("counted") or "").capitalize()
    for key, heading in (("classes", f"{counted} by class"), ("tags", "Tags")):
        lines.append(heading)
        for row in model.get(key) or []:
            lines.append(_row_line(row))
        lines.append(f"{key} hidden {model.get(f'{key}_hidden')} omitted {model.get(f'{key}_omitted')}")

    for entry in model.get("filters") or []:
        lines.append(f"{entry['status']} {entry['label']}")

    icons = model.get("icons") or {}
    lines.append(f"{icons.get('dataset', '')} {icons.get('item', '')}")

    # Everything above is the overview; the tree starts here, under its own heading.
    lines.append("What changed")
    if items.get("truncated"):
        lines.append(
            f"This tree shows the first {items['drawn']} changed items of {items['changed']}"
        )

    def walk(groups_, inherited=None):
        for group in groups_:
            # A level is headed when it holds more than one kind of change, or its one kind is
            # not the one the level above announced. VersionsDiffLevel.vue, same rule.
            headed = len(groups_) > 1 or group["key"] != inherited
            under = group["key"] if headed else inherited
            if headed:
                lines.append(f"{group['label']} {group['count']}")
            for node in group["entries"]:
                lines.append(line_of(node, under))
                own = (node.get("parts") or {}).get("action_key")
                walk(node.get("groups") or [], own or under)

    for dataset in model.get("tree", []):
        lines.append(f"{dataset['path']} {dataset.get('summary') or ''}")
        for item in dataset.get("entries", []):
            statuses = " ".join(status["label"] for status in item.get("statuses", []))
            lines.append(
                " ".join(
                    str(piece)
                    for piece in [
                        item["name"],
                        statuses,
                        _counts_line(item),
                        item.get("classes"),
                        item.get("previous"),
                    ]
                    if piece
                )
            )
            walk(item.get("groups") or [])
        if dataset.get("omitted"):
            lines.append(f"and {dataset['omitted']} more changed items in this dataset")

    if model.get("omitted"):
        lines.append(f"and {model['omitted']} more")

    return " | ".join(line for line in lines if line)


def _row_line(row) -> str:
    """One row of the overview tables: a class or a tag with its icon, colour and counts."""
    pieces = [
        row.get("icon"),
        row.get("colour"),
        row.get("name"),
        row.get("state"),
        f"+{row['added']}" if row.get("added") else None,
        f"\u2212{row['removed']}" if row.get("removed") else None,
        f"pencil {row['changed']}" if row.get("changed") else None,
        row.get("total"),
    ]

    return " ".join(str(piece) for piece in pieces if piece)


def row_classes(model) -> list:
    """The class each row of the tree is named by - the question "what names this row?"."""
    return [
        (node.get("parts") or {}).get("class")
        for node in nodes(model)
        if node.get("kind") != "tag"
    ]


def position(haystack: str, needle: str) -> int:
    index = haystack.find(needle)
    assert index != -1, f"not in the report: {needle}"
    return index


# --------------------------------------------------------------------------------------
# The tree
# --------------------------------------------------------------------------------------


def test_the_tree_nests_dataset_item_object_figure_tag(tmp_path):
    """The whole point of the layout: each level is inside the one above it."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=7, updated_at="t1")
    before.tag(900, OWNER_FIGURE, 100, 10, name="reviewed", value="no")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=7, updated_at="t1")
    after.tag(900, OWNER_FIGURE, 100, 10, name="reviewed", value="yes", updated_at="t2")

    _, model = report_of(tmp_path, before, after)

    dataset = position(drawn(model), "ds1")
    item = position(drawn(model), "clip.mp4")
    # The object row is absent here on purpose: it holds one figure and no tags of its own,
    # so it would only repeat the class name above the figure that carries it. See
    # test_an_object_that_groups_several_figures_says_so for the case where it earns a row.
    figure = position(drawn(model), '#100')
    tag = position(drawn(model), "reviewed:")
    assert dataset < item < figure < tag

    assert "Bounding Box · frame 7" in drawn(model)
    # The figure itself did not move, so its row claims nothing: it is in the tree because
    # of the tag under it, and the heading it sits below has already said what that was.
    assert "tags only" not in drawn(model)
    assert "not itself changed" not in drawn(model)
    assert "reviewed: no → yes" in drawn(model)


def test_a_widened_frame_range_reads_as_the_two_ranges(tmp_path):
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10).figure(100, 5, 10, updated_at="t1")
    before.tag(900, OWNER_OBJECT, 5, 10, name="vt-num-frames", frame_range=(61, 133))

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10).figure(100, 5, 10, updated_at="t1")
    after.tag(
        900, OWNER_OBJECT, 5, 10, name="vt-num-frames", frame_range=(61, 158), updated_at="t2"
    )

    _, model = report_of(tmp_path, before, after)

    # The name, then where the tag applies - drawn in its own span so it can be lighter.
    assert "vt-num-frames frames 61–133 → frames 61–158" in drawn(model)
    assert "frames 61–133 → frames 61–158" in drawn(model)


def test_image_figures_hang_off_the_item_with_no_object_level(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1.jpg", updated_at="t1")
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1.jpg", updated_at="t2")
        .figure(500, 100, class_name="person")
        .tag(900, OWNER_ITEM, 100, 100, name="reviewed")
    )

    _, model = report_of(tmp_path, before, after)

    assert "person" in drawn(model)
    assert "Bounding Box" in drawn(model)
    assert '#500' in drawn(model)
    assert "reviewed" in drawn(model)


def test_names_are_carried_verbatim(tmp_path):
    """A class or an item can be called anything, including something that looks like markup
    or like a Vue interpolation. The report is data now, so a name travels as the characters
    it is made of; the panel binds it as text, and nothing on the way compiles it."""
    meta = ProjectMeta(
        obj_classes=ObjClassCollection([ObjClass("<b>{{ hack }}</b>", Rectangle)])
    )
    before = SnapshotBuilder(tmp_path, "a", meta=meta).dataset(1, "ds1", full_path="ds1")
    before.image(100, 1, "img1", updated_at="t1")
    after = SnapshotBuilder(tmp_path, "b", meta=meta).dataset(1, "ds1", full_path="ds1")
    after.image(100, 1, "<script>x</script>", updated_at="t2")
    after.figure(500, 100, class_name="<b>{{ hack }}</b>")

    _, model = report_of(tmp_path, before, after)

    assert "<script>x</script>" in drawn(model)
    assert "<b>{{ hack }}</b>" in drawn(model)
    # Nothing was html-escaped on the way: that was the renderer's job, and there is no
    # renderer between here and the panel any more.
    assert "&lt;" not in drawn(model)


# --------------------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------------------


def test_a_dataset_says_how_many_of_its_items_are_not_drawn(tmp_path):
    before = one_dataset(tmp_path, "a")
    after = one_dataset(tmp_path, "b")
    for image_id in range(100, 104):
        after.image(image_id, 1, f"img{image_id}")

    _, model = report_of(tmp_path, before, after)
    assert "4 added" in drawn(model)

    # The cap is a page-load budget; the counts above the tree still cover the whole diff.
    import versions_diff_report.generator as generator

    original = generator.ITEM_TREE_LIMIT
    generator.ITEM_TREE_LIMIT = 2
    try:
        _, capped_model = report_of(tmp_path, one_dataset(tmp_path, "c"), after)
    finally:
        generator.ITEM_TREE_LIMIT = original

    assert "and 2 more changed items in this dataset" in drawn(capped_model)
    assert "This tree shows the first 2 changed items of 4" in drawn(capped_model)


def test_the_overview_carries_class_colours_from_the_meta(tmp_path):
    """The counts by class and by tag are the first thing on the page, and a class is
    recognised by its colour before its name is read - both metas are in the artifact, so
    neither costs a request."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1", updated_at="t2")
        .figure(500, 100, class_name="car")
        .tag(900, OWNER_ITEM, 100, 100, name="reviewed")
    )

    report_dir, model = report_of(tmp_path, before, after)

    # `car` is defined with colour [1, 2, 3] and shape rectangle in the fixture meta, and
    # wears the panel's own rectangle icon in that colour.
    assert '#010203' in drawn(model)
    assert "zmdi-crop-din" in drawn(model)
    assert position(drawn(model), "Figures by class") < position(drawn(model), "What changed")
    assert "reviewed" in drawn(model)
    # No widget data files: the page is self-contained, which is also why it renders the
    # same outside the panel.
    assert not os.path.isfile(os.path.join(report_dir, "data", "classes_table.json"))


def test_a_class_of_any_shape_still_wears_an_icon(tmp_path):
    """"Any Shape" is a shape the panel draws too. Leaving it to the fallback dot lost the
    icon on every class a project declares that way, which is most of them in practice."""
    meta = ProjectMeta(
        obj_classes=ObjClassCollection([ObjClass("thing", AnyGeometry, color=[1, 2, 3])])
    )
    before = SnapshotBuilder(tmp_path, "a", meta=meta).dataset(1, "ds1", full_path="ds1")
    before.image(100, 1, "img1", updated_at="t1")
    after = SnapshotBuilder(tmp_path, "b", meta=meta).dataset(1, "ds1", full_path="ds1")
    after.image(100, 1, "img1", updated_at="t2").figure(500, 100, class_name="thing")

    _, model = report_of(tmp_path, before, after)

    assert "zmdi-grain" in drawn(model)
    assert "zmdi-circle" not in drawn(model)


def test_the_overview_draws_five_rows_and_puts_the_rest_in_a_dialog(tmp_path):
    """A project's whole class list in front of the tree is a wall. Five rows, and the rest
    in a dialog - a checkbox and labels, because no script of ours runs on this page."""
    names = [f"class{index:02d}" for index in range(13)]
    meta = ProjectMeta(
        obj_classes=ObjClassCollection([ObjClass(name, Rectangle) for name in names])
    )
    before = SnapshotBuilder(tmp_path, "a", meta=meta).dataset(1, "ds1", full_path="ds1")
    before.image(100, 1, "img1", updated_at="t1")
    after = SnapshotBuilder(tmp_path, "b", meta=meta).dataset(1, "ds1", full_path="ds1")
    after.image(100, 1, "img1", updated_at="t2")
    for index, name in enumerate(names):
        after.figure(500 + index, 100, class_name=name)

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    # Every class is in the page - the card holds five and the dialog holds them all, and
    # they are reachable without a request, which is the point of drawing them rather than
    # linking to diff.json.
    for name in names:
        assert name in drawn_text
    assert len([row for row in model["classes"] if not row["extra"]]) == 5
    assert len(model["classes"]) == len(names)
    assert model["classes_hidden"] == len(names) - 5


def test_a_class_deleted_between_the_versions_is_marked_in_the_overview(tmp_path):
    """Its figures are all "removed", so it has to be in the list - and it has to say why
    it is greyed out rather than look like every other row."""
    with_dog = ProjectMeta(
        obj_classes=ObjClassCollection(
            [ObjClass("car", Rectangle, color=[1, 2, 3]), ObjClass("dog", Rectangle, color=[9, 9, 9])]
        )
    )
    before = SnapshotBuilder(tmp_path, "a", meta=with_dog).dataset(1, "ds1", full_path="ds1")
    before.image(100, 1, "img1", updated_at="t1").figure(500, 100, class_name="dog")

    after = SnapshotBuilder(tmp_path, "b").dataset(1, "ds1", full_path="ds1")
    after.image(100, 1, "img1", updated_at="t2")

    _, model = report_of(tmp_path, before, after)

    assert "removed from meta" in drawn(model)
    # Keeps the colour it had while it existed, rather than falling back to grey.
    assert '#090909' in drawn(model)


def test_the_filter_offers_only_what_the_diff_contains(tmp_path):
    """A filter that empties the tree is a dead button; the page offers the statuses this
    diff actually has, and filters with CSS because no script of ours can run here."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1", updated_at="t1")
    after.image(101, 1, "img2")

    _, model = report_of(tmp_path, before, after)

    assert [entry["status"] for entry in model["filters"]] == ["added"]
    assert "removed" not in [entry["status"] for entry in model["filters"]]
    assert model["filters"]


def test_definitions_say_what_happened_to_each_class_and_tag(tmp_path):
    """The product calls these Definitions, and splits them into classes and tags; the
    report says the same thing the same way, one row per definition with its own icon,
    its own colour and a badge for what happened to it."""
    other = ProjectMeta(obj_classes=ObjClassCollection([ObjClass("truck", Rectangle)]))
    before = one_dataset(tmp_path, "a").image(100, 1, "img1")
    after = SnapshotBuilder(tmp_path, "b", meta=other).dataset(1, "ds1", full_path="ds1")
    after.image(100, 1, "img1")

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    # Both lists are there, and each row says what happened to its definition.
    assert model["meta"]["classes"]["shown"]
    assert model["meta"]["tag_metas"]["shown"]
    assert "Project meta" not in drawn(model)
    by_name = {row["name"]: row for row in model["meta"]["classes"]["shown"]}
    for name, action in (("truck", "added"), ("car", "removed")):
        assert by_name[name]["action"] == action
    # The class keeps the icon and the colour it has everywhere else in the product.
    assert "zmdi-crop-din" in drawn_text
    assert ITEM_TREE_LIMIT > 0


def test_a_dataset_whose_items_are_all_drawn_does_not_claim_more(tmp_path):
    """An item can hold several statuses at once — renamed and re-annotated — and summing
    the status counters would tell the reader that items are missing from the tree."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    before.figure(500, 100, updated_at="t1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1-renamed", updated_at="t2")
    after.figure(500, 100, updated_at="t2")

    _, model = report_of(tmp_path, before, after)

    assert "renamed" in drawn(model)
    assert "annotation changed" in drawn(model)
    assert "more changed items" not in drawn(model)


def test_each_version_is_dated_by_its_own_timestamp(tmp_path):
    """The snapshot only knows the project's created_at, so the caller supplies the
    version's — otherwise both sides of the header carry the same date."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1")

    _, model = report_of(tmp_path, before, after)

    assert model["summary"]["from"]["version"] == 4
    assert model["summary"]["to"]["version"] == 7
    # Written the way every other date in the product is, rather than as the ISO string the
    # artifact carries.
    assert "01 Jan 2026 00:00:00 → 02 Feb 2026 00:00:00" in drawn(model)


def test_an_items_changes_are_grouped_by_what_happened(tmp_path):
    """One column holding additions, removals and edits has to be read line by line to be
    sorted; three headed lists are read by heading."""
    before = (
        one_dataset(tmp_path, "a")
        .image(100, 1, "img1", updated_at="t1")
        .figure(500, 100, class_name="car", updated_at="t1")
        .figure(501, 100, class_name="person", updated_at="t1")
    )
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1", updated_at="t2")
        .figure(500, 100, class_name="car", updated_at="t2")
        .figure(502, 100, class_name="person", updated_at="t1")
    )

    _, model = report_of(tmp_path, before, after)

    drawn_text = drawn(model)
    assert "added" in [group["key"] for group in groups(model)]
    assert "removed" in [group["key"] for group in groups(model)]
    assert "changed" in [group["key"] for group in groups(model)]
    # The heading carries the action, so the rows under it do not repeat it.
    assert position(drawn(model), "added 1") < position(drawn(model), '#502')


def test_one_kind_of_change_is_headed_once_instead_of_labelled_on_every_row(tmp_path):
    """A list of thirty additions used to say "added" thirty times. The heading says it
    once and carries the count, and the rows are left to say what was added."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1", updated_at="t2")
    for figure_id in (500, 501, 502):
        after.figure(figure_id, 100, class_name="car")

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    assert "added" in [group["key"] for group in groups(model)]
    assert '#500' in drawn_text
    # One heading, and the word does not come back on any of the three rows under it.
    tree = drawn_text[position(drawn_text, "What changed"):]
    assert tree.count("added") == 1


def test_a_box_tracked_across_frames_is_one_line(tmp_path):
    """Five figures of one object on consecutive frames are one box being tracked, and five
    identical lines say that worse than a range does."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")
    for index, frame in enumerate(range(30, 35)):
        after.figure(200 + index, 5, 10, frame_index=frame, updated_at="t2")
    # A gap, so the run stops rather than swallowing everything on the object.
    after.figure(300, 5, 10, frame_index=40, updated_at="t2")

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    assert "frames 30–34" in drawn_text
    # No id on a folded line: there are five, and the range is already how many.
    assert "×5" not in drawn_text
    # The frame on the far side of the gap keeps its own line and its own id.
    assert "frame 40" in drawn_text
    assert '#300' in drawn_text
    # Folding lines does not change what is counted: six figures were added, drawn on two.
    assert "figures +6" in drawn_text


def test_a_long_class_name_is_cut_rather_than_pushing_the_counts_away(tmp_path):
    """The counts belong next to what they count, so the names column is as wide as the
    longest name and no wider - and a name too long for it keeps the whole of itself in the
    title, which the browser shows on hover."""
    long_name = "a-very-long-class-name-" * 4
    wide = ProjectMeta(
        obj_classes=ObjClassCollection([ObjClass(long_name, Rectangle, color=[1, 2, 3])])
    )
    before = SnapshotBuilder(tmp_path, "a", meta=wide).dataset(1, "ds1", full_path="ds1")
    before.image(100, 1, "img1", updated_at="t1")
    after = SnapshotBuilder(tmp_path, "b", meta=wide).dataset(1, "ds1", full_path="ds1")
    after.image(100, 1, "img1", updated_at="t2").figure(500, 100, class_name=long_name)

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    assert long_name in drawn_text
    assert model["classes"]


def test_an_object_with_one_figure_is_drawn_as_that_figure(tmp_path):
    """Two lines for one change - the class, then the class again with a geometry - is what
    an object row costs when it has nothing to group."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t2")

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    # One row: the figure, with its geometry and its own id.
    assert len(nodes(model)) == 1
    assert "Bounding Box" in drawn_text
    assert '#100' in drawn_text


def test_an_object_that_groups_several_figures_says_so(tmp_path):
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t2")
    after.figure(101, 5, 10, frame_index=1, updated_at="t2")
    after.figure(102, 5, 10, frame_index=2, updated_at="t2")

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    assert "3 figures" in drawn_text
    assert '#5' in drawn_text


def test_a_volume_diff_is_drawn_the_same_way_but_counts_slices(tmp_path):
    """Volumes are the third modality and the only one stored as whole JSON records, so
    the levels have to survive being flattened out of the annotation document - and the
    axis a figure sits on is a slice there, not a frame."""
    before = VolumeSnapshotBuilder(tmp_path, "a").dataset(1, "ct")
    before.volume(10, 1, "scan.nrrd", updated_at="t1").obj("obj-1", class_name="car")
    before.figure(100, slice_index=3)
    before.tag(900, object_key="obj-1", name="reviewed")

    after = VolumeSnapshotBuilder(tmp_path, "b").dataset(1, "ct")
    after.volume(10, 1, "scan.nrrd", updated_at="t2").obj("obj-1", class_name="car")
    after.figure(100, slice_index=3)
    after.figure(101, slice_index=4)
    after.tag(900, object_key="obj-1", name="reviewed")

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    # Dataset, then item, then the object it hangs under - inside the tree, not in the
    # overview above it, where the class names also appear.
    tree = drawn_text[position(drawn_text, "What changed"):]
    assert position(tree, "ct") < position(tree, "scan.nrrd") < position(tree, "car")
    # A volume is cut into slices; calling them frames was the video's word, not this one's.
    assert "slice 4" in drawn_text
    assert "frame" not in drawn(model)
    # Volumes are counted in objects, like videos and unlike images.
    assert "Objects by class" in drawn_text
    # And the item wears the icon of its own modality.
    assert "zmdi-layers" in drawn_text


def test_an_object_whose_only_change_is_a_tag_is_named_by_its_class(tmp_path):
    """A tag names its object and nothing else, and the tags table holds no class - so the
    row it hangs on would otherwise be an icon and an id."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")
    before.tag(900, OWNER_OBJECT, 5, 10, name="reviewed", value="no")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")
    after.tag(900, OWNER_OBJECT, 5, 10, name="reviewed", value="yes", updated_at="t2")

    _, model = report_of(tmp_path, before, after)

    assert row_classes(model) == ["car"]
    assert position(drawn(model), "car") < position(drawn(model), "reviewed:")


def test_an_object_with_no_figures_at_all_is_named_from_the_objects_table(tmp_path):
    """Tagged, never drawn. No figure names it, so the class comes from the snapshot's own
    objects table - which is the only place a video object's class actually lives."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car")
    after.tag(900, OWNER_OBJECT, 5, 10, name="reviewed", value="yes", updated_at="t2")

    _, model = report_of(tmp_path, before, after)

    assert position(drawn(model), "car") < position(drawn(model), "reviewed:")
    assert row_classes(model) == ["car"]


def test_an_object_nothing_can_name_keeps_the_noun(tmp_path):
    """The fallback is still there for a snapshot whose objects carry no class - an older
    format, or a record written without one. A row that is an icon and an id says less
    than the word does."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name=None)

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name=None)
    after.tag(900, OWNER_OBJECT, 5, 10, name="reviewed", value="yes", updated_at="t2")

    _, model = report_of(tmp_path, before, after)

    # The noun goes where the class would have been read - on the row's detail, since only a
    # figure carries a class and this object never had one.
    assert row_classes(model) == [""]
    assert "object" in drawn(model)


def test_the_item_line_counts_the_way_the_overview_does(tmp_path):
    """`+ − pencil`, not `+ − ~`: the tilde needed explaining every time it was read."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1.jpg", updated_at="t1")
    before.figure(500, 100, class_name="person")
    before.figure(501, 100, class_name="person")

    after = one_dataset(tmp_path, "b").image(100, 1, "img1.jpg", updated_at="t2")
    after.figure(500, 100, class_name="person", updated_at="t2")

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    assert "~" not in drawn(model)
    assert model["modified_icon"] == "zmdi zmdi-edit"


def test_an_untouched_entity_is_filed_under_what_moved_inside_it(tmp_path):
    """There is no bucket for "things that changed inside": what moved is what names the
    heading, and the entity is only where it happened."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")
    before.figure(101, 5, 10, frame_index=1, updated_at="t1")
    before.tag(900, OWNER_ITEM, 10, 10, name="reviewed")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    # Both figures move, so the object keeps a row of its own to group them under.
    after.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t2")
    after.figure(101, 5, 10, frame_index=1, updated_at="t2")

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    # Both figures were modified, so the object is under "modified" rather than in a bucket
    # named after the fact that it is not itself the thing that changed - and its own row
    # says nothing at all, because an object is never the thing that was edited.
    assert "changed" in [group["key"] for group in groups(model)]
    assert "with changes inside" not in drawn_text
    assert "with tag changes" not in drawn_text
    assert "not itself changed" not in drawn_text
    assert "tags only" not in drawn_text


def test_figures_under_an_object_say_only_what_differs(tmp_path):
    """They are all that object's class, drawn its way; the frame is the only thing that
    changes from row to row, so it is the only thing the row carries."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")
    after.figure(101, 5, 10, frame_index=7, geometry_type="rectangle", updated_at="t2")
    after.figure(102, 5, 10, frame_index=9, geometry_type="bitmap", updated_at="t2")

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    # The object names the class once, and is drawn the way its figures are drawn.
    assert "zmdi-crop-din" in drawn_text
    tree = drawn_text.split("What changed", 1)[1]
    assert len([node for node in nodes(model) if (node.get("parts") or {}).get("class") == "car"]) == 1
    # The figure that matches the object says only its frame; the one drawn differently
    # keeps its geometry, because that is the thing worth noticing.
    assert "frame 7" in drawn_text
    assert "Mask · frame 9" in drawn_text


def test_a_volume_figure_that_sits_on_no_slice_says_its_shape(tmp_path):
    """A Mask 3D is the whole volume rather than a place in it, so there is no slice to
    name - and a row under an object drawn the same way drops its geometry too. Without the
    shape that row is an icon and an id, which reads as an empty line."""
    before = VolumeSnapshotBuilder(tmp_path, "a").dataset(1, "ct")
    before.volume(10, 1, "scan.nrrd", updated_at="t1")

    after = VolumeSnapshotBuilder(tmp_path, "b").dataset(1, "ct")
    after.volume(10, 1, "scan.nrrd", updated_at="t2")
    after.obj("obj-1", class_name="car", object_id=770)
    after.spatial_figure(200)
    after.tag(900, object_key="obj-1", name="any-oneof", value="high")

    _, model = report_of(tmp_path, before, after)
    drawn_text = drawn(model)

    # The product's own word for the shape, not the vocabulary the annotation is stored in.
    assert "Mask 3D" in drawn_text
    assert "mask_3d" not in drawn_text
    # Nothing to say about slices: the figure is not on one.
    assert "slice" not in drawn(model)
