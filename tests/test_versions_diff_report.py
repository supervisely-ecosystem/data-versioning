# coding: utf-8

"""The rendered report — the tree a person reads.

These assert on the rendered `template.vue`, not on an intermediate context: what makes
this report readable is the nesting and the escaping, and both only exist after Jinja has
run. The diff behind it is real (tests/snapshots.py), so a change that stops the engine
from emitting a level of the tree fails here too.
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
    """Compute a diff, publish it into a directory, render it. Returns (dir, template)."""
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

    VersionsDiffReport(api=None, report_dir=output_dir).generate()

    with open(os.path.join(output_dir, "template.vue"), encoding="utf-8") as f:
        return output_dir, f.read()


def markup(template: str) -> str:
    """The page without its stylesheet, for assertions about what is drawn.

    The CSS is inlined in the same file and names every class the markup can use, so a
    plain substring check against the whole template answers yes to anything.
    """
    return template.split("</sly-style>", 1)[-1]


def text(template: str) -> str:
    """The drawn page as a reader sees it, tags gone and spacing collapsed.

    A line of the tree is several spans - the name, the value, the frames a shade lighter -
    so an assertion about what a row says is an assertion about its text, not its markup.
    """
    return " ".join(re.sub(r"<[^>]+>", " ", markup(template)).split())


def position(template: str, needle: str) -> int:
    index = template.find(needle)
    assert index != -1, f"not in the rendered report: {needle}"
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

    _, template = report_of(tmp_path, before, after)

    dataset = position(template, "ds1")
    item = position(template, "clip.mp4")
    # The object row is absent here on purpose: it holds one figure and no tags of its own,
    # so it would only repeat the class name above the figure that carries it. See
    # test_an_object_that_groups_several_figures_says_so for the case where it earns a row.
    figure = position(template, ">100</span>")
    tag = position(template, "reviewed:")
    assert dataset < item < figure < tag

    assert "Bounding Box · frame 7" in template
    # The figure itself did not move, so its row claims nothing: it is in the tree because
    # of the tag under it, and the heading it sits below has already said what that was.
    assert "tags only" not in template
    assert "not itself changed" not in template
    assert "reviewed: no → yes" in text(template)


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

    _, template = report_of(tmp_path, before, after)

    # The name, then where the tag applies - drawn in its own span so it can be lighter.
    assert "vt-num-frames frames 61–133 → frames 61–158" in text(template)
    assert '<span class="sly-vdiff__dim">frames 61–133 → frames 61–158</span>' in markup(template)


def test_image_figures_hang_off_the_item_with_no_object_level(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1.jpg", updated_at="t1")
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1.jpg", updated_at="t2")
        .figure(500, 100, class_name="person")
        .tag(900, OWNER_ITEM, 100, 100, name="reviewed")
    )

    _, template = report_of(tmp_path, before, after)

    assert ">person</span>" in template
    assert ">Bounding Box</span>" in template
    assert ">500</span>" in template
    assert "reviewed" in template


def test_names_are_data_and_the_tree_is_not_compiled(tmp_path):
    """A class or an item can be called anything, including something that looks like
    markup or like a Vue interpolation. `v-pre` is what makes that safe, and escaping is
    what keeps it readable."""
    meta = ProjectMeta(
        obj_classes=ObjClassCollection([ObjClass("<b>{{ hack }}</b>", Rectangle)])
    )
    before = SnapshotBuilder(tmp_path, "a", meta=meta).dataset(1, "ds1", full_path="ds1")
    before.image(100, 1, "img1", updated_at="t1")
    after = SnapshotBuilder(tmp_path, "b", meta=meta).dataset(1, "ds1", full_path="ds1")
    after.image(100, 1, "<script>x</script>", updated_at="t2")
    after.figure(500, 100, class_name="<b>{{ hack }}</b>")

    _, template = report_of(tmp_path, before, after)

    assert "<script>x</script>" not in template
    assert "&lt;script&gt;x&lt;/script&gt;" in template
    assert "&lt;b&gt;{{ hack }}&lt;/b&gt;" in template
    # The content is inside a v-pre block, so the braces above are text rather than an
    # interpolation Vue would try to resolve...
    assert "<div v-pre>" in template
    # ...while the stylesheet is outside it, because sly-style is a component the panel has
    # to compile. With v-pre on the root it renders as text on the page.
    assert position(template, "<sly-style>") < position(template, "<div v-pre>")


# --------------------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------------------


def test_a_dataset_says_how_many_of_its_items_are_not_drawn(tmp_path):
    before = one_dataset(tmp_path, "a")
    after = one_dataset(tmp_path, "b")
    for image_id in range(100, 104):
        after.image(image_id, 1, f"img{image_id}")

    _, template = report_of(tmp_path, before, after)
    assert "4 added" in template

    # The cap is a page-load budget; the counts above the tree still cover the whole diff.
    import versions_diff_report.generator as generator

    original = generator.ITEM_TREE_LIMIT
    generator.ITEM_TREE_LIMIT = 2
    try:
        _, capped = report_of(tmp_path, one_dataset(tmp_path, "c"), after)
    finally:
        generator.ITEM_TREE_LIMIT = original

    assert "and 2 more changed items in this dataset" in capped
    assert "Drawing the first 2 of 4 changed items" in capped


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

    report_dir, template = report_of(tmp_path, before, after)

    # `car` is defined with colour [1, 2, 3] and shape rectangle in the fixture meta, and
    # wears the panel's own rectangle icon in that colour.
    assert "color: #010203" in template
    assert "zmdi-crop-din" in template
    assert position(template, "Figures by class") < position(template, "What changed")
    assert "reviewed" in template
    assert os.path.isfile(os.path.join(report_dir, "state.json"))
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

    _, template = report_of(tmp_path, before, after)

    assert "zmdi-grain" in template
    assert "zmdi-circle" not in template


def test_the_overview_draws_ten_rows_and_folds_the_rest_behind_a_click(tmp_path):
    """A project's whole class list in front of the tree is a wall. Ten rows, and the rest
    on a click - a checkbox and a label, because no script of ours runs on this page."""
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

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    # Every class is in the page - three of them folded away, and reachable without a
    # request, which is the point of drawing them rather than linking to diff.json.
    for name in names:
        assert f">{name}</span>" in drawn
    assert drawn.count('sly-vdiff__cell--name is-extra') == 3
    assert "and 3 more classes" in drawn
    assert 'id="vdiff-more-classes"' in drawn
    assert "show fewer classes" in drawn


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

    _, template = report_of(tmp_path, before, after)

    assert "removed from meta" in template
    # Keeps the colour it had while it existed, rather than falling back to grey.
    assert "color: #090909" in template


def test_the_filter_offers_only_what_the_diff_contains(tmp_path):
    """A filter that empties the tree is a dead button; the page offers the statuses this
    diff actually has, and filters with CSS because no script of ours can run here."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1", updated_at="t1")
    after.image(101, 1, "img2")

    _, template = report_of(tmp_path, before, after)

    assert 'id="vdiff-added"' in template
    assert 'id="vdiff-removed"' not in template
    assert "<script" not in template


def test_definitions_say_what_happened_to_each_class_and_tag(tmp_path):
    """The product calls these Definitions, and splits them into classes and tags; the
    report says the same thing the same way, one row per definition with its own icon,
    its own colour and a badge for what happened to it."""
    other = ProjectMeta(obj_classes=ObjClassCollection([ObjClass("truck", Rectangle)]))
    before = one_dataset(tmp_path, "a").image(100, 1, "img1")
    after = SnapshotBuilder(tmp_path, "b", meta=other).dataset(1, "ds1", full_path="ds1")
    after.image(100, 1, "img1")

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    # The renderer stamps an anchor id onto every heading, so match the text, not the tag.
    assert ">Class definitions</h2>" in drawn
    assert ">Tag definitions</h2>" in drawn
    assert "Project meta" not in drawn
    for name, action in (("truck", "added"), ("car", "removed")):
        row = drawn[position(drawn, f'title="{name}"'):]
        assert f'sly-vdiff__chip--{action}">{action}</span>' in row[:400]
    # The class keeps the icon and the colour it has everywhere else in the product.
    assert "zmdi-crop-din" in drawn
    assert ITEM_TREE_LIMIT > 0


def test_a_dataset_whose_items_are_all_drawn_does_not_claim_more(tmp_path):
    """An item can hold several statuses at once — renamed and re-annotated — and summing
    the status counters would tell the reader that items are missing from the tree."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    before.figure(500, 100, updated_at="t1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1-renamed", updated_at="t2")
    after.figure(500, 100, updated_at="t2")

    _, template = report_of(tmp_path, before, after)

    assert ">renamed</span>" in template
    assert ">annotation changed</span>" in template
    assert "more changed items" not in template


def test_each_version_is_dated_by_its_own_timestamp(tmp_path):
    """The snapshot only knows the project's created_at, so the caller supplies the
    version's — otherwise both sides of the header carry the same date."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1")

    _, template = report_of(tmp_path, before, after)

    assert ">v4</span>" in template
    assert ">v7</span>" in template
    # Written the way every other date in the product is, rather than as the ISO string the
    # artifact carries.
    assert "01 Jan 2026 00:00:00 → 02 Feb 2026 00:00:00" in template


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

    _, template = report_of(tmp_path, before, after)

    drawn = markup(template)
    assert "sly-vdiff__group--added" in drawn
    assert "sly-vdiff__group--removed" in drawn
    assert "sly-vdiff__group--changed" in drawn
    # The heading carries the action, so the rows under it do not repeat it.
    assert position(template, "sly-vdiff__group--added") < position(template, ">502</span>")


def test_one_kind_of_change_is_headed_once_instead_of_labelled_on_every_row(tmp_path):
    """A list of thirty additions used to say "added" thirty times. The heading says it
    once and carries the count, and the rows are left to say what was added."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1", updated_at="t2")
    for figure_id in (500, 501, 502):
        after.figure(figure_id, 100, class_name="car")

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    assert '<div class="sly-vdiff__group-title">added' in drawn
    assert ">500</span>" in drawn
    assert drawn.count('sly-vdiff__act is-added') == 0


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

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    assert "frames 30–34" in drawn
    # No id on a folded line: there are five, and the range is already how many.
    assert "×5" not in drawn
    # The frame on the far side of the gap keeps its own line and its own id.
    assert "frame 40" in drawn
    assert ">300</span>" in drawn
    # Folding lines does not change what is counted: six figures were added, drawn on two.
    assert "figures +6" in drawn


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

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    assert f'title="{long_name}"' in drawn
    assert "sly-vdiff__list" in drawn


def test_an_object_with_one_figure_is_drawn_as_that_figure(tmp_path):
    """Two lines for one change - the class, then the class again with a geometry - is what
    an object row costs when it has nothing to group."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t2")

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    # One row: the figure, with its geometry and its own id.
    assert drawn.count('class="sly-vdiff__leaf"') == 1
    assert "Bounding Box" in drawn
    assert ">100</span>" in drawn


def test_an_object_that_groups_several_figures_says_so(tmp_path):
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t2")
    after.figure(101, 5, 10, frame_index=1, updated_at="t2")
    after.figure(102, 5, 10, frame_index=2, updated_at="t2")

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    assert "3 figures" in drawn
    assert ">5</span>" in drawn


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

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    # Dataset, then item, then the object it hangs under - inside the tree, not in the
    # overview above it, where the class names also appear.
    tree = drawn[position(drawn, "What changed"):]
    assert position(tree, ">ct</span>") < position(tree, "scan.nrrd") < position(tree, ">car</span>")
    # A volume is cut into slices; calling them frames was the video's word, not this one's.
    assert "slice 4" in drawn
    assert "frame" not in text(template)
    # Volumes are counted in objects, like videos and unlike images.
    assert "Objects by class" in drawn
    # And the item wears the icon of its own modality.
    assert "zmdi-layers" in drawn


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

    _, template = report_of(tmp_path, before, after)

    assert '<span class="sly-vdiff__dim">object</span>' not in markup(template)
    assert position(template, ">car</span>") < position(template, "reviewed:")


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

    _, template = report_of(tmp_path, before, after)

    assert position(template, ">car</span>") < position(template, "reviewed:")
    assert '<span class="sly-vdiff__dim">object</span>' not in markup(template)


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

    _, template = report_of(tmp_path, before, after)

    assert '<span class="sly-vdiff__dim">object</span>' in markup(template)


def test_the_item_line_counts_the_way_the_overview_does(tmp_path):
    """`+ − pencil`, not `+ − ~`: the tilde needed explaining every time it was read."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1.jpg", updated_at="t1")
    before.figure(500, 100, class_name="person")
    before.figure(501, 100, class_name="person")

    after = one_dataset(tmp_path, "b").image(100, 1, "img1.jpg", updated_at="t2")
    after.figure(500, 100, class_name="person", updated_at="t2")

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    assert "~" not in text(template)
    assert 'class="zmdi zmdi-edit"' in drawn


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

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    # Both figures were modified, so the object is under "modified" rather than in a bucket
    # named after the fact that it is not itself the thing that changed - and its own row
    # says nothing at all, because an object is never the thing that was edited.
    assert "sly-vdiff__group--changed" in drawn
    assert "with changes inside" not in drawn
    assert "with tag changes" not in drawn
    assert "not itself changed" not in drawn
    assert "tags only" not in drawn


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

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    # The object names the class once, and is drawn the way its figures are drawn.
    assert "zmdi-crop-din" in drawn
    tree = drawn.split("What changed", 1)[1]
    assert tree.count(">car</span>") == 1
    # The figure that matches the object says only its frame; the one drawn differently
    # keeps its geometry, because that is the thing worth noticing.
    assert "frame 7" in drawn
    assert "Mask · frame 9" in drawn


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

    _, template = report_of(tmp_path, before, after)
    drawn = markup(template)

    # The product's own word for the shape, not the vocabulary the annotation is stored in.
    assert "Mask 3D" in drawn
    assert "mask_3d" not in drawn
    # Nothing to say about slices: the figure is not on one.
    assert "slice" not in text(template)
