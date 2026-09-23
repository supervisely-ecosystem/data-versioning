# coding: utf-8

"""The rendered report over `diff.json` — what a person actually opens.

`diff.json` is the artifact and this is a renderer over it: everything here is derived
from the published directory and nothing is recomputed, which is what lets a report be
re-rendered months later without touching a snapshot.

Built on `sly.template.BaseGenerator`, the same machinery Model Benchmark uses: a Jinja
template rendered into `template.vue` plus a `state.json`, uploaded to Team Files, and
read back by the panel through `instance-widgets.get-template`. The report id is the
team-file id of `template.vue`.

The shape is the nesting the data actually has — **dataset → item → object → figure →
tag** — because that is how a person looks for a change: which dataset, which item, then
what inside it. The levels a modality does not have are simply absent: an image figure
*is* the label, so there is no object above it.

Three decisions worth knowing.

**The tree is static HTML inside `v-pre`.** It is a `<details>` tree, so it collapses
with no script of ours, and `v-pre` stops Vue compiling the contents — item and class
names are data, and a name is not a template even when it happens to contain braces.

**No markdown, and no widgets either.** The house reports render markdown through the
SDK's Jinja extension, which needs the `markdown` package the base image does not ship;
and the paged table widget resolves its data server-side on every page turn. A report that
is one static page needs neither: it renders identically inside the panel and outside it,
which is also what makes it reviewable without an instance.

**Filtering is CSS, not script.** The page is compiled as a Vue template, so a `<script>`
of ours is not an option. The change-type filter is a radio group plus sibling selectors -
checked radio hides every item that does not carry the matching class - and the tree
collapses through `<details>`. No JavaScript runs here at all.

**The tree is capped, twice.** Only the first `ITEM_TREE_LIMIT` changed items are drawn,
and each one's own branch was already capped when the diff was computed. A 14k-item diff
must open in a browser; the full detail is in `data/items_*.json`, which is what an
automated consumer reads anyway.
"""

import html
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from supervisely import logger
from supervisely.template.base_generator import BaseGenerator
from supervisely.template.template_renderer import TemplateRenderer

from versions_diff import (
    DETAILS_DIR_NAME,
    DIFF_FILE_NAME,
    ITEM_COUNT_KEY,
    ITEM_STATUSES,
    META_FROM_FILE,
    META_TO_FILE,
    STATUS_UNCHANGED,
)

# Items drawn in the tree. Beyond this the page stops being openable, and the counters
# above it still describe the whole diff.
ITEM_TREE_LIMIT = 500

# Rows in the overview's class and tag lists. Five are drawn and the rest open in a dialog,
# so every one of these cards is the same height whatever the project has in it - a list is
# meant to be taken in at a glance, not to push the tree off the screen. The hard limit is
# what the page is willing to hold at all; past it the rest is in diff.json.
OVERVIEW_VISIBLE_ROWS = 5
OVERVIEW_ROW_LIMIT = 100

# What a definition row says happened to it, in the order the lists are built.
DEFINITION_ACTIONS = ("added", "removed", "modified")

# How a class or tag stands relative to the two metas. Anything else is in both.
META_STATE_NEW = "new"
META_STATE_REMOVED = "removed from meta"

# The panel's own icons, so a class reads the same here as it does everywhere else in the
# product - `SHAPE_ICONS` in panel/src/components/Projects/ProjectClasses/ProjectClasses.vue,
# keyed by the geometry vocabulary in shared/lib/packages/Classes/ClassesTypes.js. Recoloured
# to the class colour, which is what makes a shape recognisable at a glance.
SHAPE_ICONS = {
    "point": "zmdi zmdi-dot-circle-alt",
    "rectangle": "zmdi zmdi-crop-din",
    "oriented_bbox": "zmdi zmdi-crop-din",
    "polygon": "icons8-polygon",
    "multipolygon": "icons8-polygon",
    "line": "zmdi zmdi-minus",
    "polyline": "zmdi zmdi-minus",
    "polyline_3d": "zmdi zmdi-minus",
    "bitmap": "zmdi zmdi-brush",
    "alpha_mask": "zmdi zmdi-brush",
    "mask_3d": "zmdi zmdi-brush",
    "graph": "zmdi zmdi-grain",
    "mesh": "zmdi zmdi-grain",
    "closed_surface_mesh": "zmdi zmdi-grain",
    "cuboid_2d": "zmdi zmdi-ungroup",
    "cuboid_3d": "zmdi zmdi-ungroup",
    "point_cloud": "zmdi zmdi-cloud-outline",
    "point_3d": "zmdi zmdi-dot-circle-alt",
    # "Any Shape" is a shape the panel draws too, and a class declared that way is common
    # enough that leaving it to the fallback dot lost the icon on half a project's classes.
    "any": "zmdi zmdi-grain",
}

# A class of unknown shape keeps the plain dot it used to have.
DEFAULT_SHAPE_ICON = "zmdi zmdi-circle"

# What the product calls each shape - `SHAPE_TYPES_LABELS` in
# shared/lib/packages/Classes/ClassesTypes.js. Only read where a row has nothing else to
# say, which is a figure that sits on no slice: a Mask 3D is the volume, not a place in it.
SHAPE_LABELS = {
    "point": "Point",
    "point_3d": "Point",
    "rectangle": "Bounding Box",
    "oriented_bbox": "Oriented Bounding Box",
    "polygon": "Polygon",
    "multipolygon": "Multipolygon",
    "line": "Line",
    "polyline": "Line",
    "polyline_3d": "Polyline 3D",
    "bitmap": "Mask",
    "alpha_mask": "Alpha Mask",
    "mask_3d": "Mask 3D",
    "closed_surface_mesh": "3D Interpolation",
    "graph": "Keypoints",
    "mesh": "Mesh",
    "cuboid_2d": "Cuboid 2D",
    "cuboid_3d": "Cuboid",
    "point_cloud": "Point Cloud",
    "any": "Any Shape",
}

TAG_ICON = "zmdi zmdi-label"
# "~" needed explaining every time; a pencil does not.
MODIFIED_ICON = "zmdi zmdi-edit"
DATASET_ICON = "zmdi zmdi-folder"

ITEM_ICONS = {
    "images": "zmdi zmdi-image",
    "videos": "zmdi zmdi-videocam",
    "volumes": "zmdi zmdi-layers",
}

# How a status reads in the report.
STATUS_LABELS = {
    "added": "added",
    "removed": "removed",
    "renamed": "renamed",
    "moved": "moved",
    "contentChanged": "content changed",
    "annotationChanged": "annotation changed",
}

ACTION_LABELS = {"added": "added", "removed": "removed", "changed": "modified"}

# A node with no action of its own is in the tree because something under it moved: it is
# filed under that something's heading (see _effective_action) and says nothing else. An
# object cannot be "modified" - only its figures can - so a word there is always noise.
IMPLICIT_ACTION = ""


def _table(columns, rows: List[dict]) -> dict:
    return {
        "columns": list(columns),
        "columnsOptions": [{} for _ in columns],
        "content": rows,
    }


def _row(key: Any, items: List[Any]) -> dict:
    return {"id": str(key), "items": items, "row": {}}


def _text(value: Any) -> str:
    """Any value as text safe to put in the page.

    Everything drawn in the tree goes through here. The tree lives inside `v-pre`, so
    Vue never compiles it, and this is what keeps a class named `<b>` from being markup.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return html.escape(json.dumps(value, ensure_ascii=False))
    return html.escape(str(value))


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _when(timestamp: Any) -> str:
    """`14 Sep 2026 01:14:02`, the way every other date in the product is written.

    In UTC, because a static page has no viewer to ask: the panel's own lists render dates
    through a client-side filter, and this report deliberately runs no script.
    """
    if not isinstance(timestamp, str):
        return _text(timestamp)

    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return _text(timestamp)

    return (
        f"{moment.day:02d} {MONTHS[moment.month - 1]} {moment.year} "
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
    )


def _frames(bounds: Any, noun: str = "frame") -> str:
    """`frames 30–34`, or `frame 30` when a range is one frame wide.

    A volume is cut into slices, not frames, and the figure index the diff carries means
    the same thing in both - so the axis is named by the modality rather than by the
    column it came out of.
    """
    if not isinstance(bounds, list) or not bounds:
        return _text(bounds)

    start, end = bounds[0], bounds[-1]

    return (
        f"{noun} {_text(start)}"
        if start == end
        else f"{noun}s {_text(start)}–{_text(end)}"
    )


def _settings_rows(settings: dict) -> List[dict]:
    """Project settings as one row per leaf that changed.

    A setting is rarely a scalar - `multiView` is an object with four fields in it - and
    printing the object on both sides of an arrow makes the reader diff two lines of JSON
    by eye. So the pair is walked down to the leaves, and only the leaves that differ
    become rows: `multiView.enabled  off → on`.
    """
    rows: List[dict] = []

    def walk(path: str, before: Any, after: Any) -> None:
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(set(before) | set(after)):
                if before.get(key) != after.get(key):
                    walk(f"{path}.{key}" if path else key, before.get(key), after.get(key))
            return
        rows.append({"name": path, "before": _setting_value(before), "after": _setting_value(after)})

    for field, change in settings.items():
        if isinstance(change, list) and len(change) == 2:
            walk(field, change[0], change[1])
        else:
            rows.append({"name": field, "before": None, "after": _setting_value(change)})

    return rows


def _setting_value(value: Any) -> str:
    """What a settings value reads as. Booleans are switches; nothing at all is a dash."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    return str(value)


def _transition(change: Any) -> str:
    """A `[before, after]` pair as `before → after`, and anything else as itself."""
    if isinstance(change, list) and len(change) == 2:
        return f"{_text(change[0])} → {_text(change[1])}"
    return _text(change)


class VersionsDiffReport(BaseGenerator):
    """Renders a published diff directory into `template.vue` + `state.json`.

    :param api: Supervisely API client - used by the base class for uploading only.
    :param report_dir: A local directory holding `diff.json` and its `data/` files. The
        rendered report is written into the same directory, next to what it renders.
    """

    def __init__(self, api, report_dir: str):
        super().__init__(api, report_dir)
        self.report_dir = report_dir
        # No markdown extension - see the module docstring.
        self.template_renderer = TemplateRenderer(jinja_extensions=[])
        self.summary = self._read_json(DIFF_FILE_NAME)
        self.records = self._read_records()
        # Class colours, newer meta first: a class that still exists keeps the colour it has
        # now, and one that was deleted keeps the colour it had while it existed.
        self._colours = {
            name: self._colour(entry)
            for name, entry in {
                **self._meta_index(META_FROM_FILE, "classes", "title"),
                **self._meta_index(META_TO_FILE, "classes", "title"),
            }.items()
        }
        self._shapes = {
            name: (entry or {}).get("shape")
            for name, entry in {
                **self._meta_index(META_FROM_FILE, "classes", "title"),
                **self._meta_index(META_TO_FILE, "classes", "title"),
            }.items()
        }
        self._has_objects = self.summary.get("project", {}).get("type") in ("videos", "volumes")
        # What the figure index counts along: frames in a video, slices in a volume.
        self._axis = (
            "slice" if self.summary.get("project", {}).get("type") == "volumes" else "frame"
        )
        self._tag_colours = {
            name: self._colour(entry)
            for name, entry in {
                **self._meta_index(META_FROM_FILE, "tags", "name"),
                **self._meta_index(META_TO_FILE, "tags", "name"),
            }.items()
        }

    # ------------------------------------------------------------------ reading

    def _path(self, relative: str) -> str:
        return os.path.join(self.report_dir, *relative.split("/"))

    def _read_json(self, relative: str, default: Any = None) -> Any:
        path = self._path(relative)
        if not os.path.isfile(path):
            return default
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, relative: str, payload: Any) -> None:
        path = self._path(relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def _read_records(self) -> List[dict]:
        """The detail chunks, up to what the tree can draw.

        Read in chunk order and stopped at the cap rather than read whole and sliced: the
        point of the cap is not to hold a 14k-item report in memory to draw 500 of it.
        """
        records: List[dict] = []
        for chunk in self.summary.get("details", {}).get("chunks", []):
            for record in self._read_json(chunk, default=[]):
                records.append(record)
                if len(records) >= ITEM_TREE_LIMIT:
                    return records
        return records

    # ------------------------------------------------------------------ writing

    def dump_view_model(self, path: str) -> dict:
        """Write what the report is made of, as JSON, for a renderer that is not this one.

        The panel draws the report itself now, and this is what it draws: the same model the
        template here is rendered from - the tree, the counters, the definitions, the labels
        and the icons - already shaped by the pass that knows the diff. Keeping the shaping
        here and the drawing there is what stops a fix to either from needing the other, and
        what stops every published report from carrying a copy of a stylesheet.
        """
        model = self.context()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(model, f, ensure_ascii=False)
        return model

    def generate(self):
        super().generate()
        logger.debug(f"Rendered version diff report in {self.report_dir}")

    def state(self) -> dict:
        return {}

    # ------------------------------------------------------------------ widget data

    # ------------------------------------------------------------------ overview

    def _meta_index(self, relative: str, key: str, name_key: str) -> Dict[str, dict]:
        entries = (self._read_json(relative, default={}) or {}).get(key, []) or []

        return {entry.get(name_key): entry for entry in entries if entry.get(name_key)}

    def _meta_state(self, name: str, before: Dict[str, dict], after: Dict[str, dict]):
        """Whether this class or tag exists in both metas, or only in one of them.

        A class deleted between the versions still owns every figure that disappeared with
        it, so it has to appear in the list - and it has to say why it is greyed out rather
        than look like any other row.
        """
        if name in before and name not in after:
            return META_STATE_REMOVED
        if name in after and name not in before:
            return META_STATE_NEW

        return None

    @staticmethod
    def _colour(entry: Optional[dict]) -> Optional[str]:
        colour = (entry or {}).get("color")

        return colour if isinstance(colour, str) and colour.startswith("#") else None

    def _overview_rows(self, counts: Dict[str, dict], before, after, keys, icon) -> List[dict]:
        """One list, sorted by how much moved."""
        added_key, removed_key, changed_key = keys
        rows = []

        for name, counters in counts.items():
            added = counters.get(added_key, 0)
            removed = counters.get(removed_key, 0)
            changed = counters.get(changed_key, 0)
            entry = after.get(name) or before.get(name)
            state = self._meta_state(name, before, after)
            rows.append({
                "name": _text(name),
                "colour": self._colour(entry),
                "icon": icon(name, entry),
                "state": state,
                # Only what is gone is greyed out. A class that is new is very much here -
                # fading it made a diff of a freshly filled project look uniformly dead.
                "muted": state == META_STATE_REMOVED,
                "added": added,
                "removed": removed,
                "changed": changed,
                "total": added + removed + changed,
            })

        rows.sort(key=lambda row: (-row["total"], row["name"]))

        drawn = rows[:OVERVIEW_ROW_LIMIT]
        for position, row in enumerate(drawn):
            # Drawn, but folded away until the reader asks for it.
            row["extra"] = position >= OVERVIEW_VISIBLE_ROWS

        return (
            drawn,
            max(0, len(drawn) - OVERVIEW_VISIBLE_ROWS),
            max(0, len(rows) - OVERVIEW_ROW_LIMIT),
        )

    def _classes_overview(self):
        before = self._meta_index(META_FROM_FILE, "classes", "title")
        after = self._meta_index(META_TO_FILE, "classes", "title")

        # Objects where the modality has them. A video figure is one box on one frame, so a
        # tracked object crossing 200 frames would otherwise read as 200 changes.
        keys = (
            ("objectsAdded", "objectsRemoved", "objectsModified")
            if self._has_objects
            else ("figuresAdded", "figuresRemoved", "figuresModified")
        )

        return self._overview_rows(
            self.summary["annotations"].get("byClass") or {},
            before,
            after,
            keys,
            lambda name, entry: SHAPE_ICONS.get((entry or {}).get("shape"), DEFAULT_SHAPE_ICON),
        )

    def _tags_overview(self):
        before = self._meta_index(META_FROM_FILE, "tags", "name")
        after = self._meta_index(META_TO_FILE, "tags", "name")

        return self._overview_rows(
            (self.summary["annotations"].get("tags") or {}).get("byName") or {},
            before,
            after,
            ("added", "removed", "changed"),
            lambda name, entry: TAG_ICON,
        )

    def _tree(self) -> List[dict]:
        """Datasets, each with the changed items the report draws.

        Every dataset that has changed items appears, even when the item cap means none of
        its items are drawn: the counts come from the summary, which covers the whole
        diff, so "nothing under ds7 is listed" never reads as "nothing changed in ds7".
        """
        by_path = (self.summary.get("datasets") or {}).get("byPath") or {}
        drawn: Dict[str, List[dict]] = {}
        for record in self.records:
            drawn.setdefault(record.get("datasetPath") or "", []).append(record)

        datasets = []
        for path in sorted(set(by_path) | set(drawn)):
            counts = by_path.get(path, {})
            changed_items = counts.get(ITEM_COUNT_KEY, 0)
            entries = [self._item_node(record) for record in drawn.get(path, [])]
            datasets.append(
                {
                    "path": _text(path) if path else "—",
                    "summary": ", ".join(
                        f"{count} {STATUS_LABELS.get(status, status)}"
                        for status, count in sorted(counts.items())
                        if count and status != ITEM_COUNT_KEY
                    ),
                    "total": changed_items,
                    # Not "items": Jinja resolves a dict's .items to the method.
                    "entries": entries,
                    "omitted": max(0, changed_items - len(entries)),
                }
            )
        return datasets

    def _item_node(self, record: dict) -> dict:
        tree = record.get("tree") or {}
        statuses = record.get("statuses", [])

        children = [self._object_node(node) for node in tree.get("objects", [])]
        children += [self._figure_node(node) for node in tree.get("figures", [])]
        children += [self._tag_entry(node) for node in tree.get("tags", [])]

        groups = self._group_by_action(children)

        return {
            "name": _text(record.get("name")),
            "id": record.get("itemId"),
            # Both the words and the raw statuses: one is read, the other is what the CSS
            # filter matches on.
            "statuses": [
                {"status": status, "label": STATUS_LABELS.get(status, status)}
                for status in statuses
            ],
            "status_classes": " ".join(f"is-{status}" for status in statuses),
            "previous": self._previous(record),
            "counts": self._counts_line(record),
            # What a whole item brought or took with it, when there is no tree for it.
            "classes": self._class_line(record),
            "groups": groups,
            # Without this the row draws a disclosure triangle over nothing, which is how a
            # deliberate omission reads as a broken page.
            "expandable": bool(children),
            "omitted": record.get("omitted", 0),
        }

    def _collapse_runs(self, entries: List[dict]) -> List[dict]:
        """Fold a tracked box on consecutive frames into one line.

        Five figures of one object on frames 30 to 34 are one box being tracked, and five
        identical lines say that worse than `frames 30–34` does. Only a genuine run is
        folded - same class, same geometry, consecutive frames - so a gap stays visible as
        two lines, which is the thing worth noticing.
        """
        folded: List[dict] = []

        for node in entries:
            frame = node.get("frame")
            previous = folded[-1] if folded else None

            if (
                previous is not None
                and frame is not None
                and previous.get("frame_end") is not None
                and frame == previous["frame_end"] + 1
                and previous["parts"]["class"] == node["parts"]["class"]
                and previous["parts"].get("geometry") == node["parts"].get("geometry")
                and not previous.get("groups")
                and not node.get("groups")
            ):
                previous["frame_end"] = frame
                previous["run"] = previous.get("run", 1) + 1
                previous["parts"] = {
                    **previous["parts"],
                    "detail": previous["parts"]["detail_head"],
                    # A run has no single id to quote, and its length is the range: five
                    # consecutive frames are five figures without being told so.
                    "id": "",
                }
                previous["parts"]["detail"] = " · ".join(
                    part
                    for part in (
                        previous["parts"]["detail_head"],
                        _frames([previous["frame_start"], previous["frame_end"]], self._axis),
                    )
                    if part
                )
                continue

            folded.append(node)

        return folded

    @staticmethod
    def _effective_action(entry: dict) -> str:
        """Which of added / removed / modified a row belongs under.

        A figure that was not touched but whose tag was added belongs under "added" - the
        addition is the change, the figure is only where it happened. A fourth bucket for
        "things that changed inside" said the same thing in a way nobody could read.
        """
        action = entry.get("action_key", "implicit")
        if action != "implicit":
            return action

        inside = {group["key"] for group in entry.get("groups", [])}

        return inside.pop() if len(inside) == 1 else "changed"

    def _group_by_action(self, entries: List[dict]) -> List[dict]:
        """Split one level of the tree into added / removed / changed, in that order.

        A mixed column of everything that happened to an item is read line by line to find
        out what kind of change each line is; three short lists are read by heading.
        """
        order = [
            ("added", "added"),
            ("removed", "removed"),
            ("changed", "modified"),
        ]
        buckets: Dict[str, List[dict]] = {key: [] for key, _ in order}

        for entry in entries:
            buckets[self._effective_action(entry)].append(entry)

        return [
            {
                "key": key,
                "label": label,
                "entries": self._collapse_runs(buckets[key]),
                "count": len(buckets[key]),
            }
            for key, label in order
            if buckets[key]
        ]

    def _class_line(self, record: dict) -> str:
        """`car 2, person 1` - what an added or removed item is made of.

        Not a tree: an item that arrived whole has every one of its figures "added", and
        listing four thousand of them one by one is a download rather than a report.
        """
        classes = record.get("classes") or {}

        return ", ".join(f"{_text(name)} {count}" for name, count in classes.items())

    @staticmethod
    def _previous(record: dict) -> Optional[str]:
        previous = record.get("previous")
        if not previous:
            return None
        was = f"{previous.get('datasetPath')}/{previous.get('name')}"
        return _text(was)

    def _counts_line(self, record: dict) -> List[dict]:
        """What an item's change amounts to, in the same `+ − pencil` vocabulary the
        overview counts in - so the number next to an item and the number next to its class
        are read the same way."""
        # "figures" even where the overview counts objects: these are the figure numbers
        # the diff recorded, and a row that calls them objects would be off by every frame.
        parts = []
        for label, counts in (("figures", record.get("figures")), ("tags", record.get("tags"))):
            counts = counts or {}
            if any(counts.get(key) for key in ("added", "removed", "changed")):
                parts.append(
                    {
                        "label": label,
                        "added": counts.get("added") or 0,
                        "removed": counts.get("removed") or 0,
                        "changed": counts.get("changed") or 0,
                    }
                )
        return parts

    @staticmethod
    def _dominant_geometry(figures: List[dict]) -> Optional[str]:
        """The geometry the object's figures are drawn with, when they agree on one."""
        shapes = {figure.get("geometry") for figure in figures if figure.get("geometry")}

        return shapes.pop() if len(shapes) == 1 else None

    def _object_node(self, node: dict) -> dict:
        """One annotation object, or - when there is nothing to group - the figure itself.

        An object that changed on a single frame and carries no tags of its own is two lines
        saying one thing: the class, and then the class again with a geometry. The object
        line earns its place only when it groups something - several figures, or tags that
        belong to the object rather than to any one frame.
        """
        raw_figures = node.get("figures", [])
        geometry = self._dominant_geometry(raw_figures)

        tags = [self._tag_entry(child) for child in node.get("tags", [])]
        if len(raw_figures) == 1 and not tags and node.get("action") is None:
            return self._figure_node(raw_figures[0])

        # Under an object every figure carries its class and, usually, its geometry - the
        # object's own. Repeating both on every frame is noise around the one thing that
        # differs, which is the frame.
        figures = [
            self._figure_node(child, nested=True, inherited=geometry) for child in raw_figures
        ]

        groups = self._group_by_action(figures + tags)
        parts = self._entity_parts(node, axis=self._axis)
        # The row is a heading for what hangs off it, and says so: `people · 5 figures`
        # reads as a group, where a bare class name reads as a duplicate of the line below.
        if figures:
            parts["detail"] = _text(f"{len(figures)} figure" + ("s" if len(figures) > 1 else ""))
        # An object with no figures in either version has nothing to take a class from -
        # only a figure carries one - so the noun is all there is to put on the row, and
        # without it the row is an icon and an id.
        if node.get("class") is None:
            parts["detail"] = " · ".join(part for part in ("object", parts["detail"]) if part)

        return {
            "kind": "entity",
            "action_key": parts["action_key"],
            "colour": self._class_colour(node.get("class")),
            # Drawn the way its figures are drawn: a class declared as `any` has no shape of
            # its own, and the object is the sum of what was actually drawn under it.
            "icon": self._shape_icon(node.get("class"), geometry),
            "parts": parts,
            "frame": None,
            "frame_start": None,
            "frame_end": None,
            "groups": groups,
        }

    def _figure_node(
        self, node: dict, nested: bool = False, inherited: Optional[str] = None
    ) -> dict:
        """One figure.

        `nested` means the row sits under its object, which already names the class - so it
        does not name it again. `inherited` is the geometry that object is drawn with: a
        figure matching it says only which frame it is on, and one that differs keeps its
        geometry, because differing is the thing worth seeing.
        """
        children = [self._tag_entry(child) for child in node.get("tags", [])]
        groups = self._group_by_action(children)

        parts = self._entity_parts(node, nested=nested, inherited=inherited, axis=self._axis)

        return {
            "kind": "entity",
            "action_key": parts["action_key"],
            "colour": self._class_colour(node.get("class")),
            "icon": self._shape_icon(node.get("class"), node.get("geometry")),
            "parts": parts,
            "frame": node.get("frame"),
            "frame_start": node.get("frame"),
            "frame_end": node.get("frame"),
            "groups": groups,
        }

    def _tag_entry(self, node: dict) -> dict:
        line = self._tag_line(node)

        return {
            "kind": "tag",
            "action_key": node.get("action") or "implicit",
            "colour": line["colour"],
            "icon": line["icon"],
            "name": line["name"],
            "value": line["value"],
            "frames": line["frames"],
            "text": line["text"],
            "action": line["action"],
            "groups": [],
        }

    def _class_colour(self, name) -> Optional[str]:
        if not isinstance(name, str):
            return None

        return self._colours.get(name)

    def _shape_icon(self, name, geometry: Optional[str] = None) -> str:
        """The icon for a class, by the figure's geometry where there is one.

        A figure knows its own geometry and the meta knows the class's; they agree except
        where a class was deleted between the versions, and then the figure is the one that
        was actually there.
        """
        shape = geometry or (self._shapes.get(name) if isinstance(name, str) else None)

        return SHAPE_ICONS.get(shape, DEFAULT_SHAPE_ICON)

    @staticmethod
    def _entity_parts(
        node: dict,
        nested: bool = False,
        inherited: Optional[str] = None,
        axis: str = "frame",
    ) -> dict:
        """One object or figure, split into what the eye needs in that order.

        The word "figure" is not among them: a class hanging off an item is a figure or an
        object and nothing else, so the noun says nothing the nesting has not already said.
        The id is kept - it is what you quote when reporting something - but it goes last
        and stays grey, because it is never what you are reading for.
        """
        # The geometry is dropped only when the row sits under an object drawn the same way
        # *and* the row has somewhere else to put the eye; a figure that differs from its
        # object keeps it, and so does one that sits on no slice - a Mask 3D is the whole
        # volume rather than a place in it, and without its shape that row is a bare icon.
        geometry = node.get("geometry")
        repeats_object = bool(geometry) and nested and geometry == inherited
        head = (
            ""
            if repeats_object and node.get("frame") is not None
            else (_text(SHAPE_LABELS.get(geometry, geometry)) if geometry else "")
        )
        detail = [head] if head else []
        if node.get("frame") is not None:
            detail.append(_frames([node["frame"], node["frame"]], axis))

        action = node.get("action")

        return {
            # An inherited row says nothing about its class: it is its object's. Neither
            # does an object whose class nothing in the diff names - better a row that is
            # only an icon and an id than one whose first word is a dash.
            "class": (
                ""
                if nested or node.get("class") is None
                else _transition(node.get("class"))
            ),
            "geometry": node.get("geometry"),
            # The part in front of the frames, kept so a run of frames can be folded into
            # one line without re-deriving it.
            "detail_head": head,
            "detail": " · ".join(detail),
            "action": ACTION_LABELS.get(action, IMPLICIT_ACTION),
            "action_key": action or "implicit",
            "id": _text(node.get("id")),
        }

    def _tag_line(self, node: dict) -> dict:
        """`reviewed: "no" → "yes"`, or `vt-num-frames frames 61–133` when it is new.

        Split into the name, what it says, and where it says it, because the three are not
        read with the same attention: the frames of a tag are context for its value, and
        are drawn a shade lighter so the value is what the eye lands on.

        A changed tag carries `[before, after]` for whatever moved; an added or removed one
        carries the value itself. Those look identical for a frame range - `[0, 5]` is both
        a pair and a range - so the two cases are formatted apart rather than guessed at.
        """
        name = _text(node.get("name"))
        changed = node.get("action") == "changed"

        value = []
        if "value" in node:
            value.append(_transition(node["value"]) if changed else _text(node["value"]))
        if node.get("field"):
            value.append(_text(node["field"]))

        frames = ""
        if "frameRange" in node:
            frames = (
                _transition([_frames(bounds, self._axis) for bounds in node["frameRange"]])
                if changed
                else _frames(node["frameRange"], self._axis)
            )

        value = ", ".join(value)

        return {
            "name": name,
            "value": value,
            "frames": frames,
            "text": " ".join(part for part in (f"{name}:" if value else name, value, frames) if part),
            "action": ACTION_LABELS.get(node.get("action"), IMPLICIT_ACTION),
            "colour": self._tag_colours.get(node.get("name")),
            "icon": TAG_ICON,
        }

    # ------------------------------------------------------------------ template context

    def context(self) -> dict:
        counts = self.summary.get("items", {})
        changed = sum(counts.get(status, 0) for status in ITEM_STATUSES)
        record_count = self.summary.get("details", {}).get("recordCount", 0)

        classes, classes_hidden, classes_omitted = self._classes_overview()
        tags, tags_hidden, tags_omitted = self._tags_overview()

        return {
            "summary": self.summary,
            "when": {
                "from": _when(self.summary.get("from", {}).get("createdAt")),
                "to": _when(self.summary.get("to", {}).get("createdAt")),
            },
            # Video and volume projects are compared in objects; images have none.
            "counted": "objects" if self._has_objects else "figures",
            "items": {
                "counts": [
                    {
                        "status": status,
                        "label": STATUS_LABELS.get(status, status),
                        "count": counts.get(status, 0),
                    }
                    for status in ITEM_STATUSES
                    if counts.get(status)
                ],
                "changed": changed,
                "unchanged": counts.get(STATUS_UNCHANGED, 0),
                "total": changed + counts.get(STATUS_UNCHANGED, 0),
                "drawn": len(self.records),
                "record_count": record_count,
                "truncated": len(self.records) < record_count,
            },
            # The filter offers only what this diff actually contains, so there are no dead
            # buttons and no filter that empties the tree.
            "filters": [
                {"status": status, "label": STATUS_LABELS.get(status, status)}
                for status in ITEM_STATUSES
                if counts.get(status)
            ],
            "modified_icon": MODIFIED_ICON,
            "meta": self._meta_context(),
            # Which icon stands for an item here: a project of videos is not a project of
            # pictures, and the tree should not pretend otherwise.
            "icons": {
                "dataset": DATASET_ICON,
                "item": ITEM_ICONS.get(
                    self.summary.get("project", {}).get("type"), ITEM_ICONS["images"]
                ),
            },
            "classes": classes,
            "classes_hidden": classes_hidden,
            "classes_omitted": classes_omitted,
            "tags": tags,
            "tags_hidden": tags_hidden,
            "tags_omitted": tags_omitted,
            "tree": self._tree(),
            "details_dir": DETAILS_DIR_NAME,
        }

    def _meta_context(self) -> dict:
        """The project's definitions - its classes and its tags - and what moved in them.

        Two lists shaped like the overview's, because they are read the same way and the
        product calls them the same thing: Definitions, classes and tags.
        """
        meta = self.summary.get("meta", {})
        classes = meta.get("classes") or {}
        tag_metas = meta.get("tagMetas") or {}
        settings = (meta.get("settings") or {}).get("modified") or {}

        def capped(values: List) -> dict:
            drawn = values[:OVERVIEW_ROW_LIMIT]
            for position, row in enumerate(drawn):
                if isinstance(row, dict):
                    row["extra"] = position >= OVERVIEW_VISIBLE_ROWS
            return {
                "shown": drawn,
                "hidden": max(0, len(drawn) - OVERVIEW_VISIBLE_ROWS),
                "omitted": max(0, len(values) - OVERVIEW_ROW_LIMIT),
            }

        def rows(entity: dict, icon, colour) -> dict:
            drawn = []
            for action in DEFINITION_ACTIONS:
                for entry in entity.get(action, []):
                    name = entry.get("name") if isinstance(entry, dict) else entry
                    drawn.append({
                        "name": _text(name),
                        "icon": icon(name),
                        "colour": colour(name),
                        "action": action,
                        # What a rewrite actually did to it: `color #ff0000 → #00ff00`.
                        # Only a modified row has any, and it is the whole reason the row
                        # is in the list rather than a name on its own.
                        "changes": [
                            f"{_text(field)} {_transition(change)}"
                            for field, change in (
                                (entry.get("changes") or {}) if isinstance(entry, dict) else {}
                            ).items()
                        ],
                    })
            return capped(drawn)

        return {
            "classes": rows(classes, self._shape_icon, self._class_colour),
            "tag_metas": rows(tag_metas, lambda name: TAG_ICON, self._tag_colours.get),
            "settings": capped(_settings_rows(settings)),
            "changed": bool(
                classes.get("added")
                or classes.get("removed")
                or classes.get("modified")
                or tag_metas.get("added")
                or tag_metas.get("removed")
                or tag_metas.get("modified")
                or settings
            ),
        }

    def _report_url(self, server_address: str, template_id: int) -> str:
        # The generic instance-widgets viewer, which renders any template by file id.
        # supervisely/issues#6139 gives versions their own route; until then this is where
        # a report can be opened.
        return f"{server_address}/model-benchmark?id={template_id}"
