# coding: utf-8

"""Diff engine tests — what the pass reports for two versions of one project.

The snapshots are real archives; see tests/snapshots.py for how they are built.
"""

import os

from supervisely.annotation.obj_class import ObjClass
from supervisely.annotation.obj_class_collection import ObjClassCollection
from supervisely.annotation.tag_meta import TagMeta, TagValueType
from supervisely.annotation.tag_meta_collection import TagMetaCollection
from supervisely.geometry.rectangle import Rectangle
from supervisely.project.project_meta import ProjectMeta
from supervisely.project.versioning.snapshot_reader import VersionSnapshot
from supervisely.project.versioning.tag_schema import OWNER_FIGURE, OWNER_ITEM, OWNER_OBJECT

from versions_diff import (
    ObjectClasses,
    VersionRef,
    compute_diff,
    STATUS_ADDED,
    STATUS_ANNOTATION_CHANGED,
    STATUS_CONTENT_CHANGED,
    STATUS_MOVED,
    STATUS_REMOVED,
    STATUS_RENAMED,
)
from snapshots import (
    SCHEMA_VERSION,
    VolumeSnapshotBuilder,
    SnapshotBuilder,
    VideoSnapshotBuilder,
    diff_of,
    one_dataset,
    records_by_item,
)


# --------------------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------------------


def test_untouched_items_are_counted_and_never_written_out(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1").image(101, 1, "img2")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1").image(101, 1, "img2")

    summary, records = diff_of(tmp_path, before, after)

    assert summary["items"]["unchanged"] == 2
    assert records == []
    assert summary["details"]["recordCount"] == 0


def test_added_and_removed_items_come_out_by_id(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1").image(101, 1, "img2")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1").image(102, 1, "img3")

    summary, records = diff_of(tmp_path, before, after)

    assert summary["items"]["added"] == 1
    assert summary["items"]["removed"] == 1
    assert summary["items"]["unchanged"] == 1
    statuses = {record["itemId"]: record["statuses"] for record in records}
    assert statuses == {102: [STATUS_ADDED], 101: [STATUS_REMOVED]}


def test_a_reuploaded_item_is_matched_by_path_and_name_not_reported_twice(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", hash_="old")
    after = one_dataset(tmp_path, "b").image(999, 1, "img1", hash_="new")

    summary, records = diff_of(tmp_path, before, after)

    assert summary["items"]["added"] == 0
    assert summary["items"]["removed"] == 0
    assert summary["items"][STATUS_CONTENT_CHANGED] == 1
    assert records[0]["previous"]["itemId"] == 100


def test_a_moved_item_keeping_its_bytes_is_matched_by_hash(tmp_path):
    before = (
        SnapshotBuilder(tmp_path, "a")
        .dataset(1, "ds1", full_path="ds1")
        .dataset(2, "ds2", full_path="ds2")
        .image(100, 1, "img1", hash_="same")
    )
    after = (
        SnapshotBuilder(tmp_path, "b")
        .dataset(1, "ds1", full_path="ds1")
        .dataset(2, "ds2", full_path="ds2")
        .image(200, 2, "renamed.jpg", hash_="same")
    )

    summary, records = diff_of(tmp_path, before, after)

    assert summary["items"]["added"] == 0
    assert summary["items"]["removed"] == 0
    assert set(records[0]["statuses"]) == {STATUS_RENAMED, STATUS_MOVED}


def test_a_rename_in_place_keeps_the_item_id_and_is_not_an_annotation_change(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1_renamed", updated_at="t2")

    summary, records = diff_of(tmp_path, before, after)

    assert summary["items"][STATUS_RENAMED] == 1
    # updated_at moved, but nothing about the annotation did - and this is the whole
    # reason the status is derived from the deltas rather than from the timestamp.
    assert summary["items"][STATUS_ANNOTATION_CHANGED] == 0
    assert records[0]["statuses"] == [STATUS_RENAMED]


# --------------------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------------------


def test_figures_are_read_only_for_items_whose_timestamp_moved(tmp_path):
    before = (
        one_dataset(tmp_path, "a")
        .image(100, 1, "img1", updated_at="t1")
        .image(101, 1, "img2", updated_at="t1")
        .figure(500, 100)
        .figure(501, 101)
    )
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1", updated_at="t2")
        .image(101, 1, "img2", updated_at="t1")
        .figure(500, 100, updated_at="t2")
        .figure(501, 101)
    )

    summary, records = diff_of(tmp_path, before, after)

    assert summary["items"][STATUS_ANNOTATION_CHANGED] == 1
    assert records_by_item(records)[100]["figures"] == {"added": 0, "removed": 0, "changed": 1}
    assert summary["annotations"]["byClass"]["car"]["figuresModified"] == 1


def test_added_and_removed_figures_are_counted_per_class(tmp_path):
    before = (
        one_dataset(tmp_path, "a")
        .image(100, 1, "img1", updated_at="t1")
        .figure(500, 100, class_name="car")
        .figure(501, 100, class_name="person")
    )
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1", updated_at="t2")
        .figure(500, 100, class_name="car")
        .figure(502, 100, class_name="person")
    )

    summary, records = diff_of(tmp_path, before, after)

    assert records_by_item(records)[100]["figures"] == {"added": 1, "removed": 1, "changed": 0}
    person = summary["annotations"]["byClass"]["person"]
    assert (person["figuresAdded"], person["figuresRemoved"], person["figuresModified"]) == (1, 1, 0)
    # An image figure has no object above it, so objects and figures are the same count.
    assert (person["objectsAdded"], person["objectsRemoved"]) == (1, 1)
    assert "car" not in summary["annotations"]["byClass"]


def test_a_figure_that_changed_class_is_counted_once_under_the_class_it_reached(tmp_path):
    before = (
        one_dataset(tmp_path, "a")
        .image(100, 1, "img1", updated_at="t1")
        .figure(500, 100, class_name="car", updated_at="t1")
    )
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1", updated_at="t2")
        .figure(500, 100, class_name="person", updated_at="t2")
    )

    summary, _ = diff_of(tmp_path, before, after)

    assert summary["annotations"]["byClass"]["person"]["figuresModified"] == 1
    assert "car" not in summary["annotations"]["byClass"]


def test_every_figure_of_an_added_item_counts_as_added(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1")
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1")
        .image(101, 1, "img2")
        .figure(600, 101)
        .figure(601, 101)
    )

    summary, records = diff_of(tmp_path, before, after)

    assert records_by_item(records)[101]["figures"]["added"] == 2
    assert summary["annotations"]["byClass"]["car"]["figuresAdded"] == 2


# --------------------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------------------


def test_a_new_tag_on_an_item_is_tracked(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1", updated_at="t2")
        .tag(900, OWNER_ITEM, 100, 100)
    )

    summary, records = diff_of(tmp_path, before, after)

    assert summary["annotations"]["tags"]["added"] == 1
    assert summary["annotations"]["tags"]["byName"] == {
        "reviewed": {"added": 1, "removed": 0, "changed": 0}
    }
    assert records_by_item(records)[100]["tags"]["added"] == 1
    assert records_by_item(records)[100]["statuses"] == [STATUS_ANNOTATION_CHANGED]


def test_a_widened_frame_range_is_a_change_not_an_addition(tmp_path):
    before = (
        one_dataset(tmp_path, "a")
        .image(100, 1, "img1", updated_at="t1")
        .tag(900, OWNER_ITEM, 100, 100, frame_range=(61, 133), updated_at="t1")
    )
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1", updated_at="t2")
        .tag(900, OWNER_ITEM, 100, 100, frame_range=(61, 158), updated_at="t2")
    )

    summary, records = diff_of(tmp_path, before, after)

    assert summary["annotations"]["tags"]["changed"] == 1
    assert summary["annotations"]["tags"]["byName"] == {
        "reviewed": {"added": 0, "removed": 0, "changed": 1}
    }
    assert records_by_item(records)[100]["tags"]["changed"] == 1


def test_a_figure_tag_changing_value_is_seen_without_its_own_timestamp(tmp_path):
    """The one assignment with no updated_at of its own, and the reason for the two-level
    pass: the figure's timestamp does not move, only the item's."""
    before = (
        one_dataset(tmp_path, "a")
        .image(100, 1, "img1", updated_at="t1")
        .figure(500, 100, updated_at="t1")
        .tag(900, OWNER_FIGURE, 500, 100, name="reviewed", value="no", updated_at=None)
    )
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1", updated_at="t2")
        .figure(500, 100, updated_at="t1")
        .tag(900, OWNER_FIGURE, 500, 100, name="reviewed", value="yes", updated_at=None)
    )

    summary, records = diff_of(tmp_path, before, after)

    assert summary["annotations"]["tags"]["changed"] == 1
    assert records_by_item(records)[100]["figures"]["changed"] == 0
    assert records_by_item(records)[100]["statuses"] == [STATUS_ANNOTATION_CHANGED]


def test_object_tags_are_attributed_to_their_item(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1", updated_at="t2")
        .tag(901, OWNER_OBJECT, 42, 100)
    )

    _, records = diff_of(tmp_path, before, after)

    assert records_by_item(records)[100]["tags"]["added"] == 1


# --------------------------------------------------------------------------------------
# Meta and datasets
# --------------------------------------------------------------------------------------


def test_class_and_tag_meta_changes_are_reported_by_name(tmp_path):
    other_meta = ProjectMeta(
        obj_classes=ObjClassCollection(
            [ObjClass("car", Rectangle, color=[9, 9, 9]), ObjClass("truck", Rectangle)]
        ),
        tag_metas=TagMetaCollection([TagMeta("checked", TagValueType.NONE)]),
    )
    before = one_dataset(tmp_path, "a").image(100, 1, "img1")
    after = SnapshotBuilder(tmp_path, "b", meta=other_meta).dataset(1, "ds1", full_path="ds1")
    after.image(100, 1, "img1")

    summary, _ = diff_of(tmp_path, before, after)

    assert summary["meta"]["classes"]["added"] == ["truck"]
    assert summary["meta"]["classes"]["removed"] == ["person"]
    assert summary["meta"]["classes"]["modified"][0]["name"] == "car"
    assert summary["meta"]["classes"]["modified"][0]["changes"]["color"] == ["#010203", "#090909"]
    assert summary["meta"]["tagMetas"]["added"] == ["checked"]
    assert summary["meta"]["tagMetas"]["removed"] == ["reviewed"]


def test_a_dataset_renamed_keeps_its_id_and_is_not_an_addition(tmp_path):
    before = SnapshotBuilder(tmp_path, "a").dataset(1, "ds1", full_path="ds1")
    before.image(100, 1, "img1")
    after = SnapshotBuilder(tmp_path, "b").dataset(1, "ds_renamed", full_path="ds_renamed")
    after.image(100, 1, "img1")

    summary, _ = diff_of(tmp_path, before, after)

    assert summary["datasets"]["added"] == []
    assert summary["datasets"]["removed"] == []
    assert summary["datasets"]["renamed"] == [{"id": 1, "from": "ds1", "to": "ds_renamed"}]


def test_a_nested_dataset_that_changed_parent_is_a_move(tmp_path):
    before = SnapshotBuilder(tmp_path, "a").dataset(1, "top", full_path="top")
    before.dataset(2, "child", full_path="top/child", parent_id=1)
    before.image(100, 2, "img1")
    after = SnapshotBuilder(tmp_path, "b").dataset(1, "top", full_path="top")
    after.dataset(3, "other", full_path="other")
    after.dataset(2, "child", full_path="other/child", parent_id=3)
    after.image(100, 2, "img1")

    summary, _ = diff_of(tmp_path, before, after)

    assert summary["datasets"]["added"] == ["other"]
    assert summary["datasets"]["moved"] == [{"id": 2, "from": "top/child", "to": "other/child"}]


# --------------------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------------------


def test_details_are_written_in_chunks(tmp_path):
    before = one_dataset(tmp_path, "a")
    after = one_dataset(tmp_path, "b")
    for image_id in range(100, 105):
        after.image(image_id, 1, f"img{image_id}")

    summary, records = diff_of(tmp_path, before, after, details_chunk_size=2)

    assert summary["details"]["chunks"] == [
        "data/items_0001.json",
        "data/items_0002.json",
        "data/items_0003.json",
    ]
    assert summary["details"]["recordCount"] == 5
    assert len(records) == 5


def test_the_summary_carries_both_version_headers_and_the_project(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1")

    summary, _ = diff_of(tmp_path, before, after)

    assert summary["schemaVersion"] == "v1.0.0"
    assert summary["project"] == {"id": 1, "type": "images"}
    assert summary["from"]["versionId"] == 10 and summary["from"]["version"] == 4
    assert summary["to"]["versionId"] == 17 and summary["to"]["version"] == 7
    assert summary["to"]["schemaVersion"] == SCHEMA_VERSION
    assert summary["annotations"]["comparable"] is True


# --------------------------------------------------------------------------------------
# The change tree
# --------------------------------------------------------------------------------------


def test_an_image_figures_tag_hangs_off_that_figure(tmp_path):
    """Images have no annotation objects — a figure is the label — so the tree is
    item → figure → tag."""
    before = (
        one_dataset(tmp_path, "a")
        .image(100, 1, "img1", updated_at="t1")
        .figure(500, 100, class_name="car", updated_at="t1")
        .tag(900, OWNER_FIGURE, 500, 100, name="reviewed", value="no")
    )
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1", updated_at="t2")
        .figure(500, 100, class_name="car", updated_at="t1")
        .tag(900, OWNER_FIGURE, 500, 100, name="reviewed", value="yes")
    )

    _, records = diff_of(tmp_path, before, after)
    tree = records_by_item(records)[100]["tree"]

    assert "objects" not in tree
    figure = tree["figures"][0]
    assert figure["id"] == 500 and figure["class"] == "car"
    # The figure itself did not change, only its tag - so it carries no action of its own.
    assert "action" not in figure
    assert figure["tags"] == [{"name": "reviewed", "action": "changed", "value": ["no", "yes"]}]


def test_an_items_own_tag_sits_at_the_top_of_its_tree(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    after = (
        one_dataset(tmp_path, "b")
        .image(100, 1, "img1", updated_at="t2")
        .tag(900, OWNER_ITEM, 100, 100, name="reviewed")
    )

    _, records = diff_of(tmp_path, before, after)

    assert records_by_item(records)[100]["tree"] == {
        "tags": [{"name": "reviewed", "action": "added"}]
    }


def test_video_figures_nest_under_their_object_with_frames(tmp_path):
    """The full shape the report draws: dataset → video → object → figure, with the
    object's own tags on the object and the frame on each figure."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=7, updated_at="t1")
    before.tag(900, OWNER_OBJECT, 5, 10, name="reviewed", frame_range=(61, 133))

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=7, updated_at="t2")
    after.figure(101, 5, 10, frame_index=8, updated_at="t2")
    after.tag(900, OWNER_OBJECT, 5, 10, name="reviewed", frame_range=(61, 158), updated_at="t2")

    summary, records = diff_of(tmp_path, before, after)
    tree = records_by_item(records)[10]["tree"]

    assert "figures" not in tree
    obj = tree["objects"][0]
    assert obj["id"] == 5 and obj["class"] == "car"
    assert [(f["id"], f["frame"], f["action"]) for f in obj["figures"]] == [
        (100, 7, "changed"),
        (101, 8, "added"),
    ]
    # A widened frame range is describable, not merely detectable.
    assert obj["tags"] == [
        {"name": "reviewed", "action": "changed", "frameRange": [[61, 133], [61, 158]]}
    ]
    car = summary["annotations"]["byClass"]["car"]
    assert (car["figuresAdded"], car["figuresRemoved"], car["figuresModified"]) == (1, 0, 1)
    # Both figures belong to one tracked object, which is the count a person reads.
    assert (car["objectsAdded"], car["objectsModified"]) == (1, 0)


def test_an_added_item_gets_counts_but_no_tree(tmp_path):
    """Listing a new item's every figure as an addition is what turns a report into a
    download; "this item is new" already says it."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1")
    after.image(101, 1, "img2").figure(600, 101).figure(601, 101)

    _, records = diff_of(tmp_path, before, after)
    record = records_by_item(records)[101]

    assert record["figures"]["added"] == 2
    assert "tree" not in record


def test_the_tree_is_capped_and_says_how_much_it_left_out(tmp_path):
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1", updated_at="t2")
    for figure_id in range(500, 510):
        after.figure(figure_id, 100)

    _, records = diff_of(tmp_path, before, after, max_nodes_per_item=4)
    record = records_by_item(records)[100]

    assert record["figures"]["added"] == 10
    assert len(record["tree"]["figures"]) == 4
    assert record["omitted"] == 6


def test_changed_items_are_counted_per_dataset(tmp_path):
    """The top level of the tree stays complete even when the item level under it is
    capped."""
    before = SnapshotBuilder(tmp_path, "a").dataset(1, "ds1", full_path="ds1")
    before.dataset(2, "ds2", full_path="ds2")
    before.image(100, 1, "img1", updated_at="t1").image(200, 2, "img2", updated_at="t1")

    after = SnapshotBuilder(tmp_path, "b").dataset(1, "ds1", full_path="ds1")
    after.dataset(2, "ds2", full_path="ds2")
    after.image(100, 1, "img1", updated_at="t1")
    after.image(200, 2, "img2_renamed", updated_at="t2")
    after.image(201, 2, "img3", updated_at="t1")

    summary, _ = diff_of(tmp_path, before, after)

    # `items` counts items and the rest are statuses: the renamed item holds one status
    # here, but an item that is renamed *and* re-annotated must not be counted twice.
    assert summary["datasets"]["byPath"] == {"ds2": {"items": 2, "renamed": 1, "added": 1}}


def test_a_tag_with_no_name_of_its_own_is_named_from_the_project_meta(tmp_path):
    """An image's own tags come off the image listing, which carries `tagId` and no name.

    The meta is in the snapshot and knows the pair, so the report says which tag was
    added rather than that one was.
    """
    meta = ProjectMeta(
        obj_classes=ObjClassCollection([ObjClass("car", Rectangle)]),
        tag_metas=TagMetaCollection([TagMeta("reviewed", TagValueType.NONE, sly_id=7)]),
    )
    before = SnapshotBuilder(tmp_path, "a", meta=meta).dataset(1, "ds1", full_path="ds1")
    before.image(100, 1, "img1", updated_at="t1")
    after = SnapshotBuilder(tmp_path, "b", meta=meta).dataset(1, "ds1", full_path="ds1")
    after.image(100, 1, "img1", updated_at="t2")
    after.tag(900, OWNER_ITEM, 100, 100, name=None)

    summary, records = diff_of(tmp_path, before, after)

    assert records_by_item(records)[100]["tree"]["tags"][0]["name"] == "reviewed"
    assert summary["annotations"]["tags"]["byName"] == {
        "reviewed": {"added": 1, "removed": 0, "changed": 0}
    }


def test_an_item_whose_dataset_was_renamed_is_reported_as_moved(tmp_path):
    """The item did not move — its dataset did — but its path changed, and the path is
    what the report shows. Compared by dataset id, so this case needs the correction."""
    before = SnapshotBuilder(tmp_path, "a").dataset(1, "ds1", full_path="ds1")
    before.image(100, 1, "img1", updated_at="t1")
    after = SnapshotBuilder(tmp_path, "b").dataset(1, "renamed_ds", full_path="renamed_ds")
    after.image(100, 1, "img1", updated_at="t1")

    summary, records = diff_of(tmp_path, before, after)

    assert summary["items"]["moved"] == 1
    assert summary["items"]["unchanged"] == 0
    assert records[0]["previous"]["datasetPath"] == "ds1"
    assert records[0]["datasetPath"] == "renamed_ds"


def test_a_hash_that_appears_where_there_was_none_is_a_content_change(tmp_path):
    """Null is a value here: comparing with not_equal alone would drop this silently."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", hash_=None, updated_at="t1")
    before.images[-1]["hash"] = None
    after = one_dataset(tmp_path, "b").image(100, 1, "img1", hash_="now-hashed", updated_at="t1")

    summary, _ = diff_of(tmp_path, before, after)

    assert summary["items"]["contentChanged"] == 1


def test_an_added_item_says_what_it_brought_by_class(tmp_path):
    """No per-figure tree for a whole item arriving - that is a download - but "+3 figures"
    alone does not say of what."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1")
    after = one_dataset(tmp_path, "b").image(100, 1, "img1")
    after.image(101, 1, "img2")
    after.figure(600, 101, class_name="car").figure(601, 101, class_name="car")
    after.figure(602, 101, class_name="person")

    _, records = diff_of(tmp_path, before, after)
    record = records_by_item(records)[101]

    assert record["figures"]["added"] == 3
    assert record["classes"] == {"car": 2, "person": 1}
    assert "tree" not in record


def test_every_clone_of_one_file_is_matched_not_just_the_first(tmp_path):
    """The weaker passes key on the hash, and clones share it. Keeping one candidate per
    key meant the second clone found it taken and was reported as an addition plus a
    removal - the exact miscount these passes exist to prevent."""
    before = SnapshotBuilder(tmp_path, "a")
    after = SnapshotBuilder(tmp_path, "b")
    for builder in (before, after):
        for index in range(1, 5):
            builder.dataset(index, f"ds{index}", full_path=f"ds{index}")

    # Three items with the same bytes and the same name, in three datasets...
    for position, dataset_id in enumerate((1, 2, 3)):
        before.image(100 + position, dataset_id, "clip.mp4", hash_="same-bytes")
    # ...all moved into a fourth, still under that name.
    for position in range(3):
        after.image(200 + position, 4, "clip.mp4", hash_="same-bytes")

    summary, records = diff_of(tmp_path, before, after)

    assert summary["items"]["moved"] == 3
    assert summary["items"]["added"] == 0
    assert summary["items"]["removed"] == 0
    assert len(records) == 3


def test_items_sharing_one_hash_are_still_matched_by_name(tmp_path):
    """A project built by cloning one file has dozens of items with identical bytes, so a
    hash resolves at most one of them. Moving several of those at once used to report the
    rest as unrelated additions and removals."""
    before = SnapshotBuilder(tmp_path, "a").dataset(1, "night", full_path="night")
    before.dataset(2, "cam_a", full_path="cam_a")
    for index in range(3):
        before.image(100 + index, 1, f"clip{index}.mp4", hash_="same-bytes")

    after = SnapshotBuilder(tmp_path, "b").dataset(1, "night", full_path="night")
    after.dataset(2, "cam_a", full_path="cam_a")
    # Moved as the platform moves them: copied to the new dataset under the same name and
    # deleted from the old one, so every one of them arrives with a new id.
    for index in range(3):
        after.image(200 + index, 2, f"clip{index}.mp4", hash_="same-bytes")

    summary, records = diff_of(tmp_path, before, after)

    assert summary["items"]["moved"] == 3
    assert summary["items"]["added"] == 0
    assert summary["items"]["removed"] == 0
    assert {record["name"] for record in records} == {"clip0.mp4", "clip1.mp4", "clip2.mp4"}



# --------------------------------------------------------------------------------------
# Naming the object a tag hangs on
# --------------------------------------------------------------------------------------


class _CountingSnapshot:
    """A snapshot that says how many times its objects table was read."""

    def __init__(self, rows):
        self.rows = rows
        self.reads = 0

    def iter_objects(self, columns=None):
        self.reads += 1
        yield self.rows


def test_the_objects_table_is_read_once_and_only_when_something_needs_it(tmp_path):
    """It is a scan, and almost every object in a diff is already named by its figures.
    Reading it up front would put that scan on every diff of every video project."""
    from supervisely.project.versioning.snapshot_reader import SnapshotColumn

    snapshot = _CountingSnapshot(
        [{SnapshotColumn.OBJECT_ID: 5, SnapshotColumn.CLASS_NAME: "car"}]
    )
    classes = ObjectClasses(snapshot)

    assert snapshot.reads == 0
    assert classes.get(5) == "car"
    assert classes.get(6) is None
    assert classes.get(5) == "car"
    assert snapshot.reads == 1


def test_an_object_whose_class_changed_is_named_by_the_newer_version(tmp_path):
    from supervisely.project.versioning.snapshot_reader import SnapshotColumn

    older = _CountingSnapshot([{SnapshotColumn.OBJECT_ID: 5, SnapshotColumn.CLASS_NAME: "car"}])
    newer = _CountingSnapshot([{SnapshotColumn.OBJECT_ID: 5, SnapshotColumn.CLASS_NAME: "truck"}])

    assert ObjectClasses(older, newer).get(5) == "truck"


def test_a_tag_on_an_undrawn_object_still_names_its_class(tmp_path):
    """End to end: the object has no figures in either version, so nothing but the objects
    table knows what it is."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car")
    after.tag(900, OWNER_OBJECT, 5, 10, name="reviewed", updated_at="t2")

    _, records = diff_of(tmp_path, before, after)

    objects = records[0]["tree"]["objects"]
    assert objects[0]["id"] == 5
    assert objects[0]["class"] == "car"
    # Nothing drawn under it in either version - the whole reason the objects table had
    # to be read to name it.
    assert "figures" not in objects[0]
    assert [tag["name"] for tag in objects[0]["tags"]] == ["reviewed"]



def test_a_run_reports_what_it_cost_and_how_much_it_read(tmp_path):
    """The one line the service logs when a diff finishes is only as good as this: a run
    that is slow or surprising is diagnosed from the sizes and the stage costs, or not at
    all. `diff.json` deliberately carries none of it - it describes the two versions, not
    the machine that compared them."""
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    before.figure(500, 100, class_name="car")
    before.image(101, 1, "img2", updated_at="t1")
    before.tag(900, OWNER_ITEM, 100, 100, name="reviewed")

    after = one_dataset(tmp_path, "b").image(100, 1, "img1", updated_at="t2")
    after.figure(500, 100, class_name="car", updated_at="t2")
    after.image(101, 1, "img2", updated_at="t1")
    after.tag(900, OWNER_ITEM, 100, 100, name="reviewed")

    summary, _ = diff_of(tmp_path, before, after)
    assert "stats" not in summary

    output_dir = os.path.join(str(tmp_path), "stats")
    os.makedirs(output_dir, exist_ok=True)
    with VersionSnapshot.open_archive(
        before.build(), payload_dir=os.path.join(str(tmp_path), "sa")
    ) as snapshot_from:
        with VersionSnapshot.open_archive(
            after.build(), payload_dir=os.path.join(str(tmp_path), "sb")
        ) as snapshot_to:
            result = compute_diff(
                snapshot_from,
                snapshot_to,
                version_from=VersionRef(id=1, number=1, created_at="2026-01-01T00:00:00.000Z"),
                version_to=VersionRef(id=2, number=2, created_at="2026-02-02T00:00:00.000Z"),
                output_dir=output_dir,
            )

    stats = result.stats
    # Both sides' size, and how much of them the two-level pass actually had to read:
    # one item moved, so only that item's figures and tags are touched.
    assert stats["items_from"] == 2 and stats["items_to"] == 2
    assert stats["touched"] == 1
    assert stats["figures_read"] == 2
    assert stats["tags_read"] == 2
    for stage in ("items_msec", "figures_msec", "tags_msec"):
        assert isinstance(stats[stage], float)


def test_the_first_frame_is_not_mistaken_for_an_empty_field(tmp_path):
    """Records drop empty fields to stay small, and `frame: 0` is not one. It is the first
    frame of a video and the first slice of a volume - testing the value for truth threw it
    away with the empty lists."""
    before = VideoSnapshotBuilder(tmp_path, "va").dataset(1, "ds1", full_path="ds1")
    before.video(10, 1, "clip.mp4", updated_at="t1")
    before.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t1")

    after = VideoSnapshotBuilder(tmp_path, "vb").dataset(1, "ds1", full_path="ds1")
    after.video(10, 1, "clip.mp4", updated_at="t2")
    after.obj(5, 10, class_name="car").figure(100, 5, 10, frame_index=0, updated_at="t2")

    _, records = diff_of(tmp_path, before, after)

    figure = records[0]["tree"]["objects"][0]["figures"][0]
    assert figure["frame"] == 0


def test_one_edited_slice_figure_is_one_modification(tmp_path):
    """What a volume diff is for. The uuid keys differ between any two snapshots by
    construction - the SDK mints them on every parse - so pairing on them reported every
    figure of a touched volume as removed and added again. The server ids are what match."""
    def build(name, edited_at):
        builder = VolumeSnapshotBuilder(tmp_path, name).dataset(1, "ct")
        builder.volume(10, 1, "chest.nrrd", updated_at="t1" if name == "a" else "t2")
        builder.obj("obj-1", class_name="car", object_id=770)
        builder.figure(100, slice_index=3)
        builder.figure(101, slice_index=4, updated_at=edited_at)
        builder.figure(102, slice_index=5)
        return builder

    summary, records = diff_of(tmp_path, build("a", "t1"), build("b", "t2"))

    assert summary["items"]["annotationChanged"] == 1
    assert summary["annotations"]["byClass"]["car"] == {
        "figuresAdded": 0,
        "figuresRemoved": 0,
        "figuresModified": 1,
        "objectsAdded": 0,
        "objectsRemoved": 0,
        "objectsModified": 1,
    }

    figures = records[0]["tree"]["objects"][0]["figures"]
    assert [f["id"] for f in figures] == [101]
    assert figures[0]["action"] == "changed"
    assert figures[0]["frame"] == 4
