# coding: utf-8

"""The version diff itself — what changed between two snapshots of one project.

This is the computation; `routers/versions_diff.py` is the HTTP and Team Files half.
Nothing here touches the platform API, which is what makes it testable against
snapshots built on disk.

**Nothing is hashed and no geometry is ever read.** A changelog answers three
questions — what was added, what was removed, what changed — and all three come from
ids plus `updated_at`. If a figure changed, its geometry changed; describing *how* is a
drill-down that reads that one figure on demand, not part of this pass.

The pass is two-level, and that is not an optimisation but a correctness requirement.
Adding a tag to a figure does not move that figure's `updated_at` — the tag assignment
is a row of its own — but it does move the **item's**. So items are compared first, and
only the items whose `updated_at` moved have their figures and tags read at all. On the
14k-video reference project that is 14k rows against 227k figure rows and 133k tag rows.

Everything wide is read as Arrow and compared with `pyarrow.compute`. Building a dict
per row is what costs on a snapshot this shape: three columns of 4.5M figure rows take
0.13 s as Arrow batches against 4.6 s as dicts. Only the leftovers of the item match —
the items whose ids are in one snapshot and not the other — are handled row by row, and
those are small by construction.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import (
    Any,
    Deque,
    Dict,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import pyarrow as pa
import pyarrow.compute as pc
from supervisely import logger
from supervisely.project.versioning.snapshot_reader import SnapshotColumn, VersionSnapshot
from supervisely.project.versioning.tag_schema import OWNER_FIGURE, OWNER_OBJECT

DIFF_SCHEMA_VERSION = "v1.0.0"

DIFF_FILE_NAME = "diff.json"
DETAILS_DIR_NAME = "data"
DETAILS_CHUNK_TEMPLATE = "items_%04d.json"

# Both project metas, written beside the detail chunks. They are what lets the report be
# re-rendered from a published directory alone - which is what the preview attachment
# does, long after the snapshots have been closed and deleted.
META_FROM_FILE = f"{DETAILS_DIR_NAME}/meta_from.json"
META_TO_FILE = f"{DETAILS_DIR_NAME}/meta_to.json"

# Records per detail chunk. A 14k-item report has to open in a browser, so the detail
# records are never one blob; the report pages through these.
DETAILS_CHUNK_SIZE = 5000

# Rows per Arrow batch while scanning. Only ever a handful of narrow columns here — no
# geometry, no annotation documents — so this is far above the reader's own default.
SCAN_BATCH_SIZE = 50_000

ITEM_SCAN_COLUMNS = (
    SnapshotColumn.ITEM_ID,
    SnapshotColumn.DATASET_ID,
    SnapshotColumn.NAME,
    SnapshotColumn.HASH,
    SnapshotColumn.UPDATED_AT,
)

FIGURE_SCAN_COLUMNS = (
    SnapshotColumn.FIGURE_ID,
    SnapshotColumn.ITEM_ID,
    SnapshotColumn.CLASS_NAME,
    SnapshotColumn.GEOMETRY_TYPE,
    SnapshotColumn.UPDATED_AT,
)

# Only video and volume figures belong to an annotation object and sit on a frame. Asking
# an image snapshot for those columns is not a null read but an error - the reader refuses
# to surface a column its format does not have rather than lying about it - and it would
# cost the Arrow path, which is the whole reason image figures are cheap to compare.
OBJECT_SCAN_COLUMNS = (SnapshotColumn.OBJECT_ID, SnapshotColumn.FRAME_INDEX)

MODALITIES_WITH_OBJECTS = ("videos", "volumes")

TAG_SCAN_COLUMNS = (
    SnapshotColumn.TAG_ASSIGNMENT_ID,
    SnapshotColumn.OWNER_TYPE,
    SnapshotColumn.OWNER_ID,
    SnapshotColumn.ITEM_ID,
    # The tag meta, which is what names an assignment that arrived without a name.
    SnapshotColumn.TAG_ID,
    SnapshotColumn.NAME,
    # The value as stored, not decoded: two equal values are equal as text, and this is
    # the form the columnar path can serve.
    SnapshotColumn.VALUE_JSON,
    SnapshotColumn.FRAME_FROM,
    SnapshotColumn.FRAME_TO,
    SnapshotColumn.UPDATED_AT,
)

_ITEM_SCAN_SCHEMA = pa.schema(
    [
        (SnapshotColumn.ITEM_ID, pa.int64()),
        (SnapshotColumn.DATASET_ID, pa.int64()),
        (SnapshotColumn.NAME, pa.string()),
        (SnapshotColumn.HASH, pa.string()),
        (SnapshotColumn.UPDATED_AT, pa.string()),
    ]
)

# How an item in one snapshot was paired with an item in the other. Ids first because
# they are stable across snapshots of one project; the rest is what survives a
# re-upload, which gives an item a new id.
MATCH_BY_ID = "id"
MATCH_BY_PATH_NAME = "pathName"
MATCH_BY_NAME_HASH = "nameHash"
MATCH_BY_HASH = "hash"

STATUS_ADDED = "added"
STATUS_REMOVED = "removed"
STATUS_RENAMED = "renamed"
STATUS_MOVED = "moved"
STATUS_CONTENT_CHANGED = "contentChanged"
STATUS_ANNOTATION_CHANGED = "annotationChanged"
STATUS_UNCHANGED = "unchanged"

# Not a status: how many items of a dataset changed at all, whatever they changed.
ITEM_COUNT_KEY = "items"

# Every status an item can be counted under except "unchanged", which is what is left.
# An item can hold several at once: a renamed item whose figures also moved is counted
# in both, and the report filters on them independently.
ITEM_STATUSES = (
    STATUS_ADDED,
    STATUS_REMOVED,
    STATUS_RENAMED,
    STATUS_MOVED,
    STATUS_CONTENT_CHANGED,
    STATUS_ANNOTATION_CHANGED,
)


# --------------------------------------------------------------------------------------
# Reading two snapshots as columns
# --------------------------------------------------------------------------------------


def msec(started: float) -> float:
    """A stage's cost since `started`, rounded to what a log line is read at."""
    return round((time.perf_counter() - started) * 1000.0, 1)


def _column_array(values: list) -> pa.Array:
    """One column, typed by what is in it.

    Ids are not one type across the formats: a video figure has an integer id, a volume
    figure has a uuid, and a volume that predates ids has a key for some figures and an
    id for others. Inferring per column and falling back to text is what keeps a
    comparison working on all of them without one schema per backend - and the values
    that reach the report are still whatever the snapshot stored.
    """
    try:
        return pa.array(values)
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError):
        return pa.array(
            [None if value is None else str(value) for value in values], type=pa.string()
        )


def _rows_to_batch(
    rows: List[dict], columns: Sequence[str], schema: Optional[pa.Schema] = None
) -> pa.RecordBatch:
    """A batch of reader dicts as Arrow, for the backends that have no Arrow path.

    The legacy pickle format has no columns at all, and a volume's figures are flattened
    out of an annotation document when the row is built. Converting here rather than
    branching downstream keeps one comparison implementation instead of two.
    """
    if schema is not None:
        arrays = [
            pa.array([row.get(field_.name) for row in rows], type=field_.type)
            for field_ in schema
        ]
        return pa.RecordBatch.from_arrays(arrays, schema=schema)

    arrays = [_column_array([row.get(column) for row in rows]) for column in columns]
    return pa.RecordBatch.from_arrays(arrays, names=list(columns))


def _scan_items(snapshot: VersionSnapshot) -> Iterator[pa.RecordBatch]:
    try:
        yield from snapshot.iter_items_arrow(
            batch_size=SCAN_BATCH_SIZE, columns=list(ITEM_SCAN_COLUMNS)
        )
        return
    except (NotImplementedError, ValueError) as reason:
        logger.debug(f"Reading items row-wise: {reason}")

    for rows in snapshot.iter_items(batch_size=SCAN_BATCH_SIZE, columns=list(ITEM_SCAN_COLUMNS)):
        yield _rows_to_batch(rows, ITEM_SCAN_COLUMNS, schema=_ITEM_SCAN_SCHEMA)


def _figure_scan_columns(snapshot: VersionSnapshot) -> List[str]:
    columns = list(FIGURE_SCAN_COLUMNS)
    if snapshot.project_type in MODALITIES_WITH_OBJECTS:
        columns.extend(OBJECT_SCAN_COLUMNS)
    return columns


def _scan_figures(
    snapshot: VersionSnapshot, item_ids: Optional[Set[int]] = None
) -> Iterator[pa.RecordBatch]:
    columns = _figure_scan_columns(snapshot)
    try:
        yield from snapshot.iter_figures_arrow(batch_size=SCAN_BATCH_SIZE, columns=columns)
        return
    except (NotImplementedError, ValueError) as reason:
        logger.debug(f"Reading figures row-wise: {reason}")

    # with_geometry=False is the point of the fallback: the geometry column is most of a
    # snapshot's bytes and this pass never looks at it. `item_ids` is the other half -
    # without it a format that stores whole records parses every item to keep a handful,
    # which on a volumes project is the whole cost of the diff.
    for rows in snapshot.iter_figures(
        batch_size=SCAN_BATCH_SIZE,
        columns=columns,
        with_geometry=False,
        item_ids=item_ids,
    ):
        yield _rows_to_batch(rows, columns)


def _item_table(snapshot: VersionSnapshot) -> pa.Table:
    """Every item of a snapshot, five narrow columns of it.

    This is the one thing held whole for both snapshots at once, and it is why the pass
    fits: five columns of 1M items is tens of megabytes as Arrow, against gigabytes as
    ImageInfo objects.
    """
    batches = [batch for batch in _scan_items(snapshot) if batch.num_rows]
    if not batches:
        return _ITEM_SCAN_SCHEMA.empty_table()
    # Left chunked on purpose: combining is a full copy of every column, and nothing here
    # needs one contiguous buffer. On half a million items that copy was ~100 MB of peak
    # bought for nothing.
    return pa.Table.from_batches(batches).select(list(ITEM_SCAN_COLUMNS))


# --------------------------------------------------------------------------------------
# Project meta
# --------------------------------------------------------------------------------------


# Object classes are keyed by "title" in project meta JSON and tag metas by "name".
CLASS_NAME_KEY = "title"
TAG_META_NAME_KEY = "name"


def _by_name(entries: Iterable[dict], name_key: str) -> Dict[str, dict]:
    return {entry.get(name_key): entry for entry in entries if entry.get(name_key) is not None}


def _modified(before: Dict[str, dict], after: Dict[str, dict], ignore: Sequence[str]) -> List[dict]:
    """Entries present in both whose fields differ, as `{field: [before, after]}`.

    Ids are ignored: a class keeps its name across versions but not necessarily its id,
    and reporting an id change as a modification would fire on every restored project.
    """
    out = []
    for name in sorted(set(before) & set(after)):
        changes = {}
        for key in sorted(set(before[name]) | set(after[name])):
            if key in ignore:
                continue
            was, now = before[name].get(key), after[name].get(key)
            if was != now:
                changes[key] = [was, now]
        if changes:
            out.append({"name": name, "changes": changes})
    return out


def _meta_entity_diff(
    before: List[dict], after: List[dict], name_key: str, ignore: Sequence[str] = ("id",)
) -> dict:
    was, now = _by_name(before, name_key), _by_name(after, name_key)
    return {
        "added": sorted(set(now) - set(was)),
        "removed": sorted(set(was) - set(now)),
        "modified": _modified(was, now, ignore),
    }


def _settings_diff(before: Optional[dict], after: Optional[dict]) -> dict:
    before, after = before or {}, after or {}
    modified = {
        key: [before.get(key), after.get(key)]
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
    return {"modified": modified}


def meta_diff(snapshot_from: VersionSnapshot, snapshot_to: VersionSnapshot) -> dict:
    """Classes, tag metas and project settings, all matched by name."""
    meta_a, meta_b = snapshot_from.meta.to_json(), snapshot_to.meta.to_json()
    return {
        "classes": _meta_entity_diff(
            meta_a.get("classes", []), meta_b.get("classes", []), CLASS_NAME_KEY
        ),
        "tagMetas": _meta_entity_diff(
            meta_a.get("tags", []), meta_b.get("tags", []), TAG_META_NAME_KEY
        ),
        "settings": _settings_diff(meta_a.get("projectSettings"), meta_b.get("projectSettings")),
    }


# --------------------------------------------------------------------------------------
# Datasets
# --------------------------------------------------------------------------------------


def _dataset_paths(snapshot: VersionSnapshot) -> Dict[int, str]:
    """dataset id -> full path. The tree is small enough to always read whole."""
    return {ds.id: (ds.full_path or ds.name) for ds in snapshot.datasets()}


def dataset_diff(snapshot_from: VersionSnapshot, snapshot_to: VersionSnapshot) -> dict:
    """Datasets by id, falling back to full path — the same order items use.

    A dataset matched by id whose path changed was either renamed or moved, and the two
    are told apart by the leaf: a new leaf is a rename, a new parent is a move.
    """
    before = {ds.id: ds for ds in snapshot_from.datasets()}
    after = {ds.id: ds for ds in snapshot_to.datasets()}

    paths_before = {(ds.full_path or ds.name): ds for ds in before.values()}
    paths_after = {(ds.full_path or ds.name): ds for ds in after.values()}

    renamed, moved = [], []
    for dataset_id in sorted(set(before) & set(after)):
        was, now = before[dataset_id], after[dataset_id]
        path_was = was.full_path or was.name
        path_now = now.full_path or now.name
        if path_was == path_now:
            continue
        record = {"id": dataset_id, "from": path_was, "to": path_now}
        (renamed if was.name != now.name else moved).append(record)

    # Only paths whose id is absent from the other side can be an addition or a removal:
    # an id-matched dataset that moved is already accounted for above.
    added = sorted(path for path, ds in paths_after.items() if ds.id not in before)
    removed = sorted(path for path, ds in paths_before.items() if ds.id not in after)

    return {
        "added": [path for path in added if path not in paths_before],
        "removed": [path for path in removed if path not in paths_after],
        "renamed": renamed,
        "moved": moved,
    }


# --------------------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemRef:
    """One item of one snapshot, as much of it as the comparison needs."""

    item_id: int
    dataset_path: str
    name: str
    hash: Optional[str]
    updated_at: Optional[str]


@dataclass
class ItemPair:
    before: ItemRef
    after: ItemRef
    matched_by: str

    @property
    def renamed(self) -> bool:
        return self.before.name != self.after.name

    @property
    def moved(self) -> bool:
        return self.before.dataset_path != self.after.dataset_path

    @property
    def content_changed(self) -> bool:
        # Two nulls are not a match: link-backed items carry no hash at all, and calling
        # every one of them unchanged would be a guess rather than a reading.
        return bool(self.before.hash or self.after.hash) and self.before.hash != self.after.hash

    @property
    def touched(self) -> bool:
        """Whether anything about this item moved, and so whether it is worth reading.

        An item matched by anything but its id was re-uploaded — new id, new figure ids —
        so its annotations are read whichever way its timestamps compare.
        """
        if self.matched_by != MATCH_BY_ID:
            return True
        return self.before.updated_at != self.after.updated_at


@dataclass
class ItemMatch:
    pairs: List[ItemPair] = field(default_factory=list)
    added: List[ItemRef] = field(default_factory=list)
    removed: List[ItemRef] = field(default_factory=list)
    unchanged_count: int = 0

    def touched_pairs(self) -> List[ItemPair]:
        return [pair for pair in self.pairs if pair.touched]


def _refs_from_table(table: pa.Table, paths: Dict[int, str]) -> Iterator[ItemRef]:
    ids = table.column(SnapshotColumn.ITEM_ID).to_pylist()
    dataset_ids = table.column(SnapshotColumn.DATASET_ID).to_pylist()
    names = table.column(SnapshotColumn.NAME).to_pylist()
    hashes = table.column(SnapshotColumn.HASH).to_pylist()
    updated = table.column(SnapshotColumn.UPDATED_AT).to_pylist()
    for index in range(table.num_rows):
        yield ItemRef(
            item_id=ids[index],
            dataset_path=paths.get(dataset_ids[index], ""),
            name=names[index],
            hash=hashes[index],
            updated_at=updated[index],
        )


def _sorted_by_id(table: pa.Table) -> pa.Table:
    return table.sort_by([(SnapshotColumn.ITEM_ID, "ascending")])


def _column_differs(left: pa.ChunkedArray, right: pa.ChunkedArray) -> pa.ChunkedArray:
    """Elementwise "these two are not the same", with nulls given a meaning.

    `not_equal` yields null when either side is null, which would quietly drop the case
    that matters most - a value that appeared or disappeared. Two nulls are equal here
    (an item with no hash in either version has not changed its hash).
    """
    one_null = pc.xor(pc.is_null(left), pc.is_null(right))
    values_differ = pc.fill_null(pc.not_equal(left, right), False)
    return pc.or_(one_null, values_differ)


def _touched_mask(
    common_from: pa.Table,
    common_to: pa.Table,
    paths_from: Dict[int, str],
    paths_to: Dict[int, str],
) -> pa.ChunkedArray:
    """Rows of the two aligned tables where anything about the item moved.

    The dataset is compared by id and then corrected: an item can sit in the same dataset
    while that dataset is renamed or moved, and its path is what the report shows. The
    dataset tree is tiny, so the set of ids whose path changed is computed once and the
    rows are marked with a single `is_in`.
    """
    touched = _column_differs(
        common_from.column(SnapshotColumn.UPDATED_AT), common_to.column(SnapshotColumn.UPDATED_AT)
    )
    for column in (SnapshotColumn.NAME, SnapshotColumn.HASH, SnapshotColumn.DATASET_ID):
        touched = pc.or_(
            touched, _column_differs(common_from.column(column), common_to.column(column))
        )

    repathed = [
        dataset_id
        for dataset_id, path in paths_from.items()
        if dataset_id in paths_to and paths_to[dataset_id] != path
    ]
    if repathed:
        touched = pc.or_(
            touched,
            pc.is_in(
                common_to.column(SnapshotColumn.DATASET_ID),
                value_set=pa.array(repathed, type=pa.int64()),
            ),
        )

    return pc.fill_null(touched, True)


def match_items(
    table_from: pa.Table,
    table_to: pa.Table,
    paths_from: Dict[int, str],
    paths_to: Dict[int, str],
) -> ItemMatch:
    """Pair up the two item sets: by id, then by (dataset path, name), then by hash.

    Ids first, because they are stable across snapshots of one project and pairing on
    them is a single vectorised `is_in` over two columns. Only what is left over —
    items re-uploaded between the versions, and genuine additions and removals — is
    walked row by row, and there are few of those or the versions are unrelated.

    Matching on name alone is deliberately not a step: it reports a rename plus a move
    as one item changing twice over, which is what the old diff-merge app did and what
    made its output unreadable. Name *and* hash together is a step - see _match_leftovers.
    """
    # Sorted first, so that the two filtered tables below line up row for row - which is
    # what lets the whole id-matched majority be compared without building a single dict.
    table_from = _sorted_by_id(table_from)
    table_to = _sorted_by_id(table_to)

    ids_from = table_from.column(SnapshotColumn.ITEM_ID)
    ids_to = table_to.column(SnapshotColumn.ITEM_ID)

    in_both_from = pc.is_in(ids_from, value_set=ids_to)
    in_both_to = pc.is_in(ids_to, value_set=ids_from)

    common_from = table_from.filter(in_both_from)
    common_to = table_to.filter(in_both_to)

    # The leftovers are taken before the originals go out of scope, so that the two full
    # item tables can be released while the comparison below still runs.
    leftover_from = list(_refs_from_table(table_from.filter(pc.invert(in_both_from)), paths_from))
    leftover_to = list(_refs_from_table(table_to.filter(pc.invert(in_both_to)), paths_to))
    del table_from, table_to, ids_from, ids_to, in_both_from, in_both_to

    match = ItemMatch()

    # Which of the id-matched rows moved at all, decided as four column comparisons
    # rather than by building two Python objects per row. On half a million items that is
    # the difference between a 900 MB peak and a flat one: the rows that changed are a
    # handful, and only those become objects.
    touched = _touched_mask(common_from, common_to, paths_from, paths_to)
    changed_count = pc.sum(pc.cast(touched, pa.int64())).as_py() or 0
    match.unchanged_count += common_from.num_rows - changed_count

    for before, after in zip(
        _refs_from_table(common_from.filter(touched), paths_from),
        _refs_from_table(common_to.filter(touched), paths_to),
    ):
        match.pairs.append(ItemPair(before=before, after=after, matched_by=MATCH_BY_ID))

    _match_leftovers(match, leftover_from, leftover_to)
    return match


def _match_leftovers(
    match: ItemMatch, leftover_from: List[ItemRef], leftover_to: List[ItemRef]
) -> None:
    """Pair what the ids could not, then call the rest added and removed.

    Three passes, each narrower than the last:

    1. **(dataset path, name)** - the item is where it was and called what it was, so it was
       re-uploaded rather than moved.
    2. **(name, hash)** - same file under the same name somewhere else: a move. It needs the
       name because a hash is not a key: a project built by cloning one file holds dozens of
       items with identical bytes, and hash alone can only ever resolve one of them.
    3. **hash** - same bytes under a different name, which is a move and a rename together.

    A candidate is claimed by the first pass that takes it, so a later, weaker pass cannot
    steal an item a stronger one has already paired.
    """
    claimed: Set[int] = set()
    still_unmatched: List[ItemRef] = []

    def index(key):
        """key -> every unclaimed candidate under it, oldest id first.

        A queue rather than one entry: the weaker passes key on the hash, and a project
        built by cloning one file has dozens of items under that key. Keeping only the
        first meant the second clone found it taken and was reported as an addition and
        a removal - the very thing these passes exist to prevent.
        """
        table: Dict[Any, Deque[ItemRef]] = defaultdict(deque)
        for ref in leftover_from:
            if ref.item_id in claimed:
                continue
            value = key(ref)
            if value is not None:
                table[value].append(ref)
        return table

    def pass_over(candidates: List[ItemRef], key, matched_by: str) -> List[ItemRef]:
        table = index(key)
        unmatched = []
        for ref in candidates:
            value = key(ref)
            queue = table.get(value) if value is not None else None
            # Already-claimed refs can still sit in this pass's queue when a stronger
            # pass took them; drop them rather than pair twice.
            while queue and queue[0].item_id in claimed:
                queue.popleft()
            if not queue:
                unmatched.append(ref)
                continue
            counterpart = queue.popleft()
            claimed.add(counterpart.item_id)
            match.pairs.append(ItemPair(before=counterpart, after=ref, matched_by=matched_by))
        return unmatched

    still_unmatched = pass_over(
        leftover_to, lambda ref: (ref.dataset_path, ref.name), MATCH_BY_PATH_NAME
    )
    still_unmatched = pass_over(
        still_unmatched,
        lambda ref: (ref.name, ref.hash) if ref.hash else None,
        MATCH_BY_NAME_HASH,
    )
    still_unmatched = pass_over(still_unmatched, lambda ref: ref.hash, MATCH_BY_HASH)

    match.added.extend(still_unmatched)
    match.removed.extend(ref for ref in leftover_from if ref.item_id not in claimed)


# --------------------------------------------------------------------------------------
# Annotations: figures and tags
# --------------------------------------------------------------------------------------


@dataclass
class EntityDelta:
    """Added / removed / changed counts for one item's figures or tags."""

    added: int = 0
    removed: int = 0
    changed: int = 0

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def to_json(self) -> dict:
        return {"added": self.added, "removed": self.removed, "changed": self.changed}


@dataclass
class ClassDelta:
    """What happened to one class, counted in figures and in annotation objects.

    A video figure is one box on one frame, so a tracked object crossing 200 frames counts
    as 200 figures - a true number that says nothing a person wants to know. The object is
    the unit they think in, so both are counted and the report picks the one that fits the
    modality.
    """

    figures: EntityDelta = field(default_factory=EntityDelta)
    added_objects: Set[Any] = field(default_factory=set)
    removed_objects: Set[Any] = field(default_factory=set)
    changed_objects: Set[Any] = field(default_factory=set)

    def to_json(self) -> dict:
        return {
            "figuresAdded": self.figures.added,
            "figuresRemoved": self.figures.removed,
            "figuresModified": self.figures.changed,
            "objectsAdded": len(self.added_objects),
            "objectsRemoved": len(self.removed_objects),
            "objectsModified": len(self.changed_objects - self.added_objects - self.removed_objects),
        }


class FigureRow(NamedTuple):
    """As much of a figure as a comparison needs. No geometry, ever."""

    class_name: Optional[str]
    geometry_type: Optional[str]
    updated_at: Optional[str]
    # The annotation object this figure belongs to, and the frame or slice it sits on.
    # Both are null for images, which have neither: a figure there is the label itself.
    object_id: Any = None
    frame_index: Optional[int] = None


class TagRow(NamedTuple):
    """One tag assignment. `value_json` is the value encoded, so a number stays a
    number and a string stays a string when it is read back for the report."""

    name: Optional[str]
    owner_type: Optional[str]
    owner_id: Optional[int]
    value_json: Optional[str]
    frame_from: Optional[int]
    frame_to: Optional[int]
    updated_at: Optional[str]

    @property
    def frame_range(self) -> Optional[List[int]]:
        if self.frame_from is None:
            return None
        return [self.frame_from, self.frame_to]

    @property
    def value(self):
        return json.loads(self.value_json) if self.value_json is not None else None

    def comparable(self) -> tuple:
        """What makes two assignments of the same id different.

        `updated_at` is in here and so is the content. For everything except an image
        figure tag the timestamp alone would do; those have no timestamp of their own,
        so the content is what catches a changed value there.
        """
        return (
            self.name,
            self.owner_type,
            self.owner_id,
            self.value_json,
            self.frame_from,
            self.frame_to,
            self.updated_at,
        )


def _figure_index(
    snapshot: VersionSnapshot, item_ids: Set[int]
) -> Dict[int, Dict[int, FigureRow]]:
    """figure id -> FigureRow, grouped by item, for the named items only.

    The scan itself is over the whole figures table — a Parquet file has no index to
    seek by item — but only the rows belonging to a touched item are ever materialised,
    which is what keeps this bounded by what changed rather than by project size.
    """
    if not item_ids:
        return {}

    wanted = pa.array(sorted(item_ids), type=pa.int64())
    index: Dict[int, Dict[int, FigureRow]] = defaultdict(dict)

    for batch in _scan_figures(snapshot, item_ids):
        if not batch.num_rows:
            continue
        table = pa.Table.from_batches([batch]).filter(
            pc.is_in(batch.column(SnapshotColumn.ITEM_ID), value_set=wanted)
        )
        if not table.num_rows:
            continue
        def column(name: str) -> list:
            if name not in table.column_names:
                return [None] * table.num_rows
            return table.column(name).to_pylist()

        figure_ids = column(SnapshotColumn.FIGURE_ID)
        owners = column(SnapshotColumn.ITEM_ID)
        classes = column(SnapshotColumn.CLASS_NAME)
        geometries = column(SnapshotColumn.GEOMETRY_TYPE)
        updated = column(SnapshotColumn.UPDATED_AT)
        objects = column(SnapshotColumn.OBJECT_ID)
        frames = column(SnapshotColumn.FRAME_INDEX)

        for position, figure_id in enumerate(figure_ids):
            index[owners[position]][figure_id] = FigureRow(
                class_name=classes[position],
                geometry_type=geometries[position],
                updated_at=updated[position],
                object_id=objects[position],
                frame_index=frames[position],
            )

    return index


class ObjectClasses:
    """Object id -> class name, read from the snapshots' object tables on the first miss.

    A figure carries the class of the object it belongs to, so this is needed only for an
    object that has no figures in either version - tagged, never drawn, and otherwise
    impossible to name. The table is one row per object rather than per figure, but it is
    still a scan, so it is read once and only when such an object actually turns up; an
    image project has no objects at all and never reaches it.

    The newer snapshot is read last, so an object whose class was changed between the
    versions is named by what it is now.
    """

    def __init__(self, *snapshots: VersionSnapshot):
        self._snapshots = snapshots
        self._classes: Optional[Dict[Any, str]] = None

    def get(self, object_id: Any) -> Optional[str]:
        if self._classes is None:
            self._classes = self._read()

        return self._classes.get(object_id)

    def _read(self) -> Dict[Any, str]:
        classes: Dict[Any, str] = {}
        columns = [SnapshotColumn.OBJECT_ID, SnapshotColumn.CLASS_NAME]
        for snapshot in self._snapshots:
            for batch in snapshot.iter_objects(columns=columns):
                for row in batch:
                    name = row.get(SnapshotColumn.CLASS_NAME)
                    if name is not None:
                        classes[row.get(SnapshotColumn.OBJECT_ID)] = name

        logger.debug("Object classes read from the snapshots", extra={"objects": len(classes)})

        return classes


def _tag_names(snapshot: VersionSnapshot) -> Dict[int, str]:
    """tag meta id -> name, for the assignments that arrive without one.

    An image's own tags come off the image listing, which gives `tagId` and a value but
    no name, while a figure's come from the annotation endpoint, which gives all three.
    The project meta is in the snapshot and knows both, so a report never has to say
    "some tag was added".
    """
    names = {}
    for tag_meta in snapshot.meta.tag_metas:
        if tag_meta.sly_id is not None:
            names[tag_meta.sly_id] = tag_meta.name
    return names


def _tag_index(snapshot: VersionSnapshot, item_ids: Set[int]) -> Dict[int, Dict[Any, TagRow]]:
    """tag assignment id -> TagRow, grouped by item.

    An assignment with no id at all — which is how the legacy pickle format stored them —
    is keyed by its own content instead, so it can still be seen as present or absent.
    """
    if not item_ids:
        return {}

    names = _tag_names(snapshot)
    index: Dict[int, Dict[Any, TagRow]] = defaultdict(dict)

    for rows in _scan_tags(snapshot, item_ids):
        for row in rows:
            tag = TagRow(
                name=row.get(SnapshotColumn.NAME) or names.get(row.get(SnapshotColumn.TAG_ID)),
                owner_type=row.get(SnapshotColumn.OWNER_TYPE),
                owner_id=row.get(SnapshotColumn.OWNER_ID),
                value_json=row.get(SnapshotColumn.VALUE_JSON),
                frame_from=row.get(SnapshotColumn.FRAME_FROM),
                frame_to=row.get(SnapshotColumn.FRAME_TO),
                updated_at=row.get(SnapshotColumn.UPDATED_AT),
            )
            key = row.get(SnapshotColumn.TAG_ASSIGNMENT_ID)
            index[row[SnapshotColumn.ITEM_ID]][
                key if key is not None else tag.comparable()[:-1]
            ] = tag

    return index


def _scan_tags(snapshot: VersionSnapshot, item_ids: Set[int]) -> Iterator[List[dict]]:
    """Tag rows belonging to the named items, filtered before they become Python objects.

    Measured on a 507k-image project: reading the whole tags table row-wise and discarding
    what did not match cost 129 MB and 16 of the diff's 32 seconds, for a handful of rows.
    The filter is the same `is_in` the figures pass uses, and for the same reason.
    """
    wanted = pa.array(sorted(item_ids), type=pa.int64())
    try:
        for batch in snapshot.iter_tags_arrow(
            batch_size=SCAN_BATCH_SIZE, columns=list(TAG_SCAN_COLUMNS)
        ):
            if not batch.num_rows:
                continue
            table = pa.Table.from_batches([batch]).filter(
                pc.is_in(batch.column(SnapshotColumn.ITEM_ID), value_set=wanted)
            )
            if table.num_rows:
                yield table.to_pylist()
        return
    except (NotImplementedError, ValueError) as reason:
        logger.debug(f"Reading tags row-wise: {reason}")

    # Volumes keep their tags inside the annotation document, so there is no table to
    # project and the rows arrive already built. The value comes decoded on this path.
    columns = [
        SnapshotColumn.VALUE if column == SnapshotColumn.VALUE_JSON else column
        for column in TAG_SCAN_COLUMNS
    ]
    for rows in snapshot.iter_tags(
        batch_size=SCAN_BATCH_SIZE, columns=columns, item_ids=item_ids
    ):
        matching = []
        for row in rows:
            # The reader has already dropped the other items; a tag on a figure of a
            # wanted item can still be filtered out by owner, so the check stays.
            if row.get(SnapshotColumn.ITEM_ID) not in item_ids:
                continue
            value = row.pop(SnapshotColumn.VALUE, None)
            row[SnapshotColumn.VALUE_JSON] = (
                None if value is None else json.dumps(value, sort_keys=True)
            )
            matching.append(row)
        if matching:
            yield matching


# --------------------------------------------------------------------------------------
# Naming what changed
# --------------------------------------------------------------------------------------

ACTION_ADDED = "added"
ACTION_REMOVED = "removed"
ACTION_CHANGED = "changed"

# Nodes per item. Past this the tree stops being something a person reads, and the counts
# above it already say how much there is; `omitted` says how much was left out.
MAX_NODES_PER_ITEM = 200


def _tag_node(tag: TagRow, action: str, before: Optional[TagRow] = None) -> dict:
    """One tag assignment, carrying only what moved.

    A widened frame range comes out as `frameRange: [[61, 133], [61, 158]]` rather than
    as "something about this tag changed", because the bounds are columns of their own in
    the snapshot and not text inside a blob.
    """
    node = {"name": tag.name, "action": action}

    if action == ACTION_CHANGED and before is not None:
        if before.value_json != tag.value_json:
            node["value"] = [before.value, tag.value]
        if before.frame_range != tag.frame_range:
            node["frameRange"] = [before.frame_range, tag.frame_range]
        # Neither the value nor the range moved, so what moved is the assignment itself -
        # who set it, or when. Saying that is more useful than an empty node.
        if "value" not in node and "frameRange" not in node:
            node["field"] = "updatedAt"
        return node

    if tag.value is not None:
        node["value"] = tag.value
    if tag.frame_range is not None:
        node["frameRange"] = tag.frame_range
    return node


def _figure_node(figure_id: Any, figure: FigureRow, action: Optional[str]) -> dict:
    node: Dict[str, Any] = {"id": figure_id, "class": figure.class_name}
    if action is not None:
        node["action"] = action
    if figure.geometry_type is not None:
        node["geometry"] = figure.geometry_type
    if figure.frame_index is not None:
        node["frame"] = figure.frame_index
    return node


def _worth_keeping(key: str, value: Any) -> bool:
    """Whether a node's field earns its place in the record.

    Empty is dropped; false is not. `frame: 0` is the first frame of a video and the first
    slice of a volume, and testing the value for truth threw it away with the empty lists.
    """
    return key == "id" or value not in (None, "", [], {})


def _object_key(value: Any) -> Optional[str]:
    """A grouping key for an annotation object.

    Video objects are server ids and volume objects are uuid keys, so the key is text
    while the id in the output stays whatever the snapshot stores.
    """
    return None if value is None else str(value)


@dataclass
class ItemTree:
    """The changed annotations of one item, in the nesting they actually have.

    dataset → item → object → figure → tag, with the levels a modality does not have
    simply absent: an image figure *is* the label, so there is no object above it, and an
    item's own tags hang off the item.

    Only what changed is here. An item that is itself added or removed contributes no
    tree at all - "this item is new" already says everything about its annotations, and
    listing a fresh video's four thousand figures as four thousand additions is what
    turns a report into a download.
    """

    limit: int = MAX_NODES_PER_ITEM
    objects: Dict[str, dict] = field(default_factory=dict)
    figures: Dict[str, dict] = field(default_factory=dict)
    tags: List[dict] = field(default_factory=list)
    omitted: int = 0

    @property
    def node_count(self) -> int:
        return len(self.objects) + len(self.figures) + len(self.tags)

    def _room(self) -> bool:
        if self.node_count < self.limit:
            return True
        self.omitted += 1
        return False

    def object_node(self, object_id: Any, class_name: Optional[str]) -> Optional[dict]:
        key = _object_key(object_id)
        node = self.objects.get(key)
        if node is None:
            if not self._room():
                return None
            node = {"id": object_id, "class": class_name, "figures": [], "tags": []}
            self.objects[key] = node
        if node["class"] is None:
            node["class"] = class_name
        return node

    def figure_node(
        self, figure_id: Any, figure: FigureRow, action: Optional[str] = None
    ) -> Optional[dict]:
        """The node for one figure, under its object where the modality has one."""
        key = str(figure_id)
        existing = self.figures.get(key)
        if existing is None and figure.object_id is not None:
            parent = self.object_node(figure.object_id, figure.class_name)
            if parent is None:
                return None
            existing = next((n for n in parent["figures"] if str(n["id"]) == key), None)
            if existing is None:
                if not self._room():
                    return None
                existing = _figure_node(figure_id, figure, action)
                existing["tags"] = []
                parent["figures"].append(existing)
                self.figures[key] = existing
        elif existing is None:
            if not self._room():
                return None
            existing = _figure_node(figure_id, figure, action)
            existing["tags"] = []
            self.figures[key] = existing

        if action is not None:
            existing["action"] = action
        return existing

    def item_tag(self, node: dict) -> None:
        if self._room():
            self.tags.append(node)

    @staticmethod
    def _figure_order(node: dict) -> tuple:
        """Frame first, then id — the order the figures are in, not the order they were
        compared in. A reader of a video's tree is walking a timeline."""
        frame = node.get("frame")
        return (frame is None, frame or 0, str(node.get("id")))

    def to_json(self) -> dict:
        """The tree, with empty branches dropped so a record stays small."""
        objects = []
        for node in sorted(self.objects.values(), key=lambda n: str(n.get("id"))):
            node["figures"] = sorted(node["figures"], key=self._figure_order)
            trimmed = {key: value for key, value in node.items() if _worth_keeping(key, value)}
            objects.append(trimmed)

        nested = {
            id(child) for obj in self.objects.values() for child in obj["figures"]
        }
        figures = [
            {key: value for key, value in node.items() if _worth_keeping(key, value)}
            # A figure under an object is already in that object's branch.
            for node in sorted(self.figures.values(), key=self._figure_order)
            if id(node) not in nested
        ]

        tree: Dict[str, Any] = {}
        if objects:
            tree["objects"] = objects
        if figures:
            tree["figures"] = figures
        if self.tags:
            tree["tags"] = self.tags

        out: Dict[str, Any] = {"tree": tree}
        if self.omitted:
            out["omitted"] = self.omitted
        return out


def _compare_figures(
    before: Dict[Any, FigureRow],
    after: Dict[Any, FigureRow],
    by_class: Dict[str, ClassDelta],
    tree: Optional[ItemTree] = None,
) -> EntityDelta:
    delta = EntityDelta()

    def owner(figure: FigureRow, item_key: Any) -> Any:
        # An image figure has no object above it, so it is its own: counting objects there
        # counts figures, which is the right answer for that modality.
        return figure.object_id if figure.object_id is not None else item_key

    for figure_id in sorted(after.keys() - before.keys(), key=repr):
        figure = after[figure_id]
        delta.added += 1
        by_class[figure.class_name].figures.added += 1
        by_class[figure.class_name].added_objects.add(owner(figure, figure_id))
        if tree is not None:
            tree.figure_node(figure_id, figure, ACTION_ADDED)

    for figure_id in sorted(before.keys() - after.keys(), key=repr):
        figure = before[figure_id]
        delta.removed += 1
        by_class[figure.class_name].figures.removed += 1
        by_class[figure.class_name].removed_objects.add(owner(figure, figure_id))
        if tree is not None:
            tree.figure_node(figure_id, figure, ACTION_REMOVED)

    for figure_id in sorted(before.keys() & after.keys(), key=repr):
        was, now = before[figure_id], after[figure_id]
        if was.updated_at == now.updated_at:
            continue
        delta.changed += 1
        # Counted under the class it ended up in: a figure whose class was reassigned is
        # one modification, not a removal and an addition.
        by_class[now.class_name].figures.changed += 1
        by_class[now.class_name].changed_objects.add(owner(now, figure_id))
        if tree is not None:
            node = tree.figure_node(figure_id, now, ACTION_CHANGED)
            if node is not None and was.class_name != now.class_name:
                node["class"] = [was.class_name, now.class_name]

    return delta


def _compare_tags(
    before: Dict[Any, TagRow],
    after: Dict[Any, TagRow],
    totals: EntityDelta,
    by_name: Dict[str, EntityDelta],
    tree: Optional[ItemTree] = None,
    figures: Optional[Dict[Any, FigureRow]] = None,
    objects: Optional[ObjectClasses] = None,
) -> EntityDelta:
    """Compare tag assignments and hang each change where it belongs in the tree.

    `figures` is the figure index a figure tag's owner is looked up in: a tag can change
    on a figure that did not change itself, and that figure still has to appear in the
    tree for its tag to hang off something.

    `objects` is the fallback for naming the object a tag hangs on - see ObjectClasses.
    """
    delta = EntityDelta()
    figures = figures or {}
    # An object tag names its object and nothing else, and the tags table holds no class.
    # The figures do: an object is the class its figures are drawn with, so the index the
    # figure tags are resolved against also names the objects the object tags hang on.
    object_classes = {
        figure.object_id: figure.class_name
        for figure in figures.values()
        if figure.object_id is not None
    }

    def class_of(object_id: Any) -> Optional[str]:
        name = object_classes.get(object_id)
        # Nothing drawn under this object in either version, so the figures cannot name
        # it. The objects table can, and is read the first time that happens.
        if name is None and objects is not None:
            name = objects.get(object_id)

        return name

    def place(tag: TagRow, node: dict) -> None:
        if tree is None:
            return
        if tag.owner_type == OWNER_OBJECT:
            parent = tree.object_node(tag.owner_id, class_of(tag.owner_id))
            if parent is not None:
                parent["tags"].append(node)
            return
        if tag.owner_type == OWNER_FIGURE:
            figure = figures.get(tag.owner_id)
            parent = (
                tree.figure_node(tag.owner_id, figure)
                if figure is not None
                # The figure is gone from this version - a removed figure's tag - so the
                # tag is reported on the item rather than invented a parent.
                else None
            )
            if parent is not None:
                parent["tags"].append(node)
                return
        tree.item_tag(node)

    def sorted_keys(keys):
        # Ids and content tuples can both be keys here, so they are not comparable to
        # each other; sorting by text keeps a report reproducible either way.
        return sorted(keys, key=repr)

    for key in sorted_keys(after.keys() - before.keys()):
        tag = after[key]
        delta.added += 1
        by_name[tag.name].added += 1
        place(tag, _tag_node(tag, ACTION_ADDED))

    for key in sorted_keys(before.keys() - after.keys()):
        tag = before[key]
        delta.removed += 1
        by_name[tag.name].removed += 1
        place(tag, _tag_node(tag, ACTION_REMOVED))

    for key in sorted_keys(before.keys() & after.keys()):
        was, now = before[key], after[key]
        if was.comparable() == now.comparable():
            continue
        delta.changed += 1
        by_name[now.name].changed += 1
        place(now, _tag_node(now, ACTION_CHANGED, before=was))

    totals.added += delta.added
    totals.removed += delta.removed
    totals.changed += delta.changed
    return delta


# --------------------------------------------------------------------------------------
# Detail records
# --------------------------------------------------------------------------------------


class DetailWriter:
    """Per-item records, written out in chunks as they are produced.

    Never one blob: a 14k-item report has to open in a browser, and the report pages
    through these files. Nothing accumulates here beyond one chunk.
    """

    def __init__(self, root_dir: str, chunk_size: int = DETAILS_CHUNK_SIZE):
        self._dir = os.path.join(root_dir, DETAILS_DIR_NAME)
        self._chunk_size = chunk_size
        self._buffer: List[dict] = []
        self._chunks: List[str] = []
        self._count = 0
        os.makedirs(self._dir, exist_ok=True)

    def add(self, record: dict) -> None:
        self._buffer.append(record)
        self._count += 1
        if len(self._buffer) >= self._chunk_size:
            self._flush()

    def close(self) -> List[str]:
        self._flush()
        return self._chunks

    @property
    def record_count(self) -> int:
        return self._count

    def _flush(self) -> None:
        if not self._buffer:
            return
        name = DETAILS_CHUNK_TEMPLATE % (len(self._chunks) + 1)
        with open(os.path.join(self._dir, name), "w", encoding="utf-8") as f:
            json.dump(self._buffer, f, ensure_ascii=False)
        self._chunks.append(f"{DETAILS_DIR_NAME}/{name}")
        self._buffer = []


def _class_counts(figures: Dict[Any, FigureRow]) -> Dict[str, int]:
    """How many figures of each class an item holds.

    What a whole item arriving or leaving is worth saying about its annotations: naming its
    four thousand figures one by one is a download, and "+4000" alone does not say of what.
    """
    counts: Dict[str, int] = defaultdict(int)
    for figure in figures.values():
        counts[figure.class_name] += 1

    return {name: counts[name] for name in sorted(counts, key=lambda n: (n is None, n))}


def _item_record(
    ref: ItemRef,
    statuses: List[str],
    figures: Optional[EntityDelta] = None,
    tags: Optional[EntityDelta] = None,
    previous: Optional[ItemRef] = None,
    tree: Optional[ItemTree] = None,
    classes: Optional[Dict[str, int]] = None,
) -> dict:
    record = {
        "itemId": ref.item_id,
        "name": ref.name,
        "datasetPath": ref.dataset_path,
        "statuses": statuses,
        "figures": (figures or EntityDelta()).to_json(),
        "tags": (tags or EntityDelta()).to_json(),
    }
    if classes:
        record["classes"] = classes
    if tree is not None:
        record.update(tree.to_json())
    if previous is not None and (
        previous.item_id != ref.item_id
        or previous.name != ref.name
        or previous.dataset_path != ref.dataset_path
    ):
        record["previous"] = {
            "itemId": previous.item_id,
            "name": previous.name,
            "datasetPath": previous.dataset_path,
        }
    return record


# --------------------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------------------


class VersionRef(NamedTuple):
    """Which version, as the caller knows it.

    The snapshot itself cannot say: it carries the *project's* `created_at`, and a header
    that reported that for both sides would date two versions identically.
    """

    id: int
    number: Optional[int] = None
    created_at: Optional[str] = None


@dataclass
class DiffResult:
    summary: dict
    detail_chunks: List[str]
    record_count: int
    # What the run cost and how much it chewed through, for the caller to log in one
    # line. Not part of the artifact: `diff.json` describes the two versions, not the
    # machine that compared them.
    stats: dict = field(default_factory=dict)


def _write_metas(
    output_dir: str, snapshot_from: VersionSnapshot, snapshot_to: VersionSnapshot
) -> None:
    for relative, snapshot in ((META_FROM_FILE, snapshot_from), (META_TO_FILE, snapshot_to)):
        path = os.path.join(output_dir, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot.meta.to_json(), f, ensure_ascii=False)


def _version_header(snapshot: VersionSnapshot, version: VersionRef) -> dict:
    return {
        "versionId": version.id,
        "version": version.number,
        "createdAt": version.created_at,
        "schemaVersion": snapshot.schema_version,
    }


def _figures_comparable(
    snapshot_from: VersionSnapshot, snapshot_to: VersionSnapshot
) -> Tuple[bool, Optional[str]]:
    """Whether figure ids from these two snapshots can be matched against each other.

    A video snapshot written as schema v2.0.0 numbers its figures by position in the table,
    and a volume snapshot written before the ids reached the planes names its slice figures
    by a uuid the SDK mints on every parse. Either way pairing on them would report figures
    that never moved. Saying so is the only honest answer; the item-level diff is still
    computed and reported.
    """
    for snapshot in (snapshot_from, snapshot_to):
        if not snapshot.figure_ids_are_server_ids:
            return False, (
                "One of these versions stores no server-side figure ids, so figures cannot "
                "be matched across versions."
            )
    return True, None


def compute_diff(
    snapshot_from: VersionSnapshot,
    snapshot_to: VersionSnapshot,
    *,
    version_from: VersionRef,
    version_to: VersionRef,
    output_dir: str,
    details_chunk_size: int = DETAILS_CHUNK_SIZE,
    max_nodes_per_item: int = MAX_NODES_PER_ITEM,
) -> DiffResult:
    """Compare two open snapshots and write the detail chunks into `output_dir`.

    Returns the summary — `diff.json` without being written out — and the chunk paths
    relative to `output_dir`. Publishing is the caller's.
    """
    project_info = snapshot_to.project_info

    paths_from = _dataset_paths(snapshot_from)
    paths_to = _dataset_paths(snapshot_to)

    # Both tables go into the call and neither is kept here: match_items releases them as
    # soon as it has the pairs, and they are the largest thing the pass holds.
    stage = time.perf_counter()
    table_from, table_to = _item_table(snapshot_from), _item_table(snapshot_to)
    items_from, items_to = table_from.num_rows, table_to.num_rows
    match = match_items(table_from, table_to, paths_from, paths_to)
    del table_from, table_to
    stats = {"items_from": items_from, "items_to": items_to, "items_msec": msec(stage)}
    logger.debug(
        "Items matched",
        extra={
            "pairs": len(match.pairs),
            "added": len(match.added),
            "removed": len(match.removed),
            "unchanged": match.unchanged_count,
        },
    )

    comparable, reason = _figures_comparable(snapshot_from, snapshot_to)
    # Lazy: nothing is read from it unless a tag turns up on an object no figure names.
    object_classes = ObjectClasses(snapshot_from, snapshot_to)

    touched = match.touched_pairs()
    ids_from = {pair.before.item_id for pair in touched} | {ref.item_id for ref in match.removed}
    ids_to = {pair.after.item_id for pair in touched} | {ref.item_id for ref in match.added}

    stage = time.perf_counter()
    figures_from = _figure_index(snapshot_from, ids_from) if comparable else {}
    figures_to = _figure_index(snapshot_to, ids_to) if comparable else {}
    stats["figures_read"] = sum(len(figures) for figures in figures_from.values()) + sum(
        len(figures) for figures in figures_to.values()
    )
    stats["figures_msec"] = msec(stage)

    stage = time.perf_counter()
    tags_from = _tag_index(snapshot_from, ids_from)
    tags_to = _tag_index(snapshot_to, ids_to)
    stats["tags_read"] = sum(len(tags) for tags in tags_from.values()) + sum(
        len(tags) for tags in tags_to.values()
    )
    stats["tags_msec"] = msec(stage)
    stats["touched"] = len(touched)

    by_class: Dict[str, ClassDelta] = defaultdict(ClassDelta)
    by_tag_name: Dict[str, EntityDelta] = defaultdict(EntityDelta)
    tag_totals = EntityDelta()
    counts = {status: 0 for status in ITEM_STATUSES}
    # Items per dataset, so the top level of the report's tree is complete even when the
    # item level below it is capped. `items` is a count of items and the rest are
    # statuses: an item can hold several statuses at once, so summing them would count a
    # renamed-and-re-annotated item twice.
    by_dataset: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    details = DetailWriter(output_dir, chunk_size=details_chunk_size)

    # An added or a removed item gets counts but no tree - see ItemTree.
    for ref in match.added:
        arriving = figures_to.get(ref.item_id, {})
        figures = _compare_figures({}, arriving, by_class)
        tags = _compare_tags({}, tags_to.get(ref.item_id, {}), tag_totals, by_tag_name)
        counts[STATUS_ADDED] += 1
        by_dataset[ref.dataset_path][STATUS_ADDED] += 1
        by_dataset[ref.dataset_path][ITEM_COUNT_KEY] += 1
        details.add(
            _item_record(ref, [STATUS_ADDED], figures, tags, classes=_class_counts(arriving))
        )

    for ref in match.removed:
        leaving = figures_from.get(ref.item_id, {})
        figures = _compare_figures(leaving, {}, by_class)
        tags = _compare_tags(tags_from.get(ref.item_id, {}), {}, tag_totals, by_tag_name)
        counts[STATUS_REMOVED] += 1
        by_dataset[ref.dataset_path][STATUS_REMOVED] += 1
        by_dataset[ref.dataset_path][ITEM_COUNT_KEY] += 1
        details.add(
            _item_record(ref, [STATUS_REMOVED], figures, tags, classes=_class_counts(leaving))
        )

    for pair in match.pairs:
        figures = EntityDelta()
        tags = EntityDelta()
        tree = ItemTree(limit=max_nodes_per_item)
        if pair.touched:
            figures_was = figures_from.get(pair.before.item_id, {})
            figures_now = figures_to.get(pair.after.item_id, {})
            figures = _compare_figures(figures_was, figures_now, by_class, tree)
            tags = _compare_tags(
                tags_from.get(pair.before.item_id, {}),
                tags_to.get(pair.after.item_id, {}),
                tag_totals,
                by_tag_name,
                tree,
                # A figure tag is looked up in the newer version first: a tag that moved
                # on a figure that still exists belongs under that figure.
                figures={**figures_was, **figures_now},
                objects=object_classes,
            )

        statuses = []
        if pair.renamed:
            statuses.append(STATUS_RENAMED)
        if pair.moved:
            statuses.append(STATUS_MOVED)
        if pair.content_changed:
            statuses.append(STATUS_CONTENT_CHANGED)
        # Derived from the deltas, not from the timestamp: an item's updated_at also
        # moves when it is renamed or its meta is edited, and calling that an annotation
        # change would put thousands of untouched annotations in the report.
        if figures or tags:
            statuses.append(STATUS_ANNOTATION_CHANGED)

        if not statuses:
            match.unchanged_count += 1
            continue

        by_dataset[pair.after.dataset_path][ITEM_COUNT_KEY] += 1
        for status in statuses:
            counts[status] += 1
            by_dataset[pair.after.dataset_path][status] += 1
        details.add(
            _item_record(pair.after, statuses, figures, tags, previous=pair.before, tree=tree)
        )

    chunks = details.close()

    _write_metas(output_dir, snapshot_from, snapshot_to)

    summary = {
        "schemaVersion": DIFF_SCHEMA_VERSION,
        "project": {"id": project_info.id, "type": getattr(project_info, "type", None)},
        "from": _version_header(snapshot_from, version_from),
        "to": _version_header(snapshot_to, version_to),
        "meta": meta_diff(snapshot_from, snapshot_to),
        "datasets": {
            **dataset_diff(snapshot_from, snapshot_to),
            # Where the changed items are, per dataset. The structural changes above are
            # about the datasets themselves; this is about what happened inside them.
            "byPath": {
                path: dict(statuses) for path, statuses in sorted(by_dataset.items())
            },
        },
        "items": {**counts, STATUS_UNCHANGED: match.unchanged_count},
        "annotations": {
            "comparable": comparable,
            "byClass": {
                name: delta.to_json()
                for name, delta in sorted(by_class.items(), key=lambda kv: kv[0] or "")
                if name is not None
            },
            "tags": {
                "added": tag_totals.added,
                "removed": tag_totals.removed,
                "changed": tag_totals.changed,
                # Per tag name, the same way figures are counted per class: "which tags
                # moved" is the first question a changelog gets asked about tags.
                "byName": {
                    name: delta.to_json()
                    for name, delta in sorted(by_tag_name.items(), key=lambda kv: kv[0] or "")
                    if name is not None
                },
            },
        },
        "details": {"chunks": chunks, "recordCount": details.record_count},
    }
    if reason is not None:
        summary["annotations"]["reason"] = reason

    return DiffResult(
        summary=summary,
        detail_chunks=chunks,
        record_count=details.record_count,
        stats=stats,
    )
