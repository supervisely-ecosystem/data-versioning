# coding: utf-8

"""Snapshot archives for the version-diff tests.

Built with the SDK's own schemas and packed with its own container rather than faked
behind the reader interface: the whole point of the pass under test is which columns it
reads and how the backends surface them, and a stubbed reader would test neither.
"""

import json
import os

import pyarrow
import pyarrow.parquet as parquet
from supervisely.annotation.obj_class import ObjClass
from supervisely.annotation.obj_class_collection import ObjClassCollection
from supervisely.annotation.tag_meta import TagMeta, TagValueType
from supervisely.annotation.tag_meta_collection import TagMetaCollection
from supervisely.annotation.label import LabelJsonFields
from supervisely.api.module_api import ApiField
from supervisely.geometry.rectangle import Rectangle
from supervisely.volume_annotation import constants as volume_constants
from supervisely.io.json import dump_json_file
from supervisely.project.project_meta import ProjectMeta
from supervisely.project.versioning.common import (
    get_image_snapshot_schema,
    get_video_snapshot_schema,
    get_volume_snapshot_schema,
)
from supervisely.project.versioning.container import pack_payload_dir, table_meta
from supervisely.project.versioning.schema_fields import VersionSchemaField
from supervisely.project.versioning.snapshot_reader import VersionSnapshot
from supervisely.project.versioning.tag_schema import get_tag_schema

from versions_diff import VersionRef, compute_diff

SCHEMA_VERSION = "v2.1.0"

META = ProjectMeta(
    obj_classes=ObjClassCollection(
        [ObjClass("car", Rectangle, color=[1, 2, 3]), ObjClass("person", Rectangle)]
    ),
    tag_metas=TagMetaCollection([TagMeta("reviewed", TagValueType.NONE)]),
)

GEOMETRY = {"points": {"exterior": [[0, 0], [4, 4]], "interior": []}}


# --------------------------------------------------------------------------------------
# Building snapshots
# --------------------------------------------------------------------------------------


class SnapshotBuilder:
    """One image snapshot, assembled row by row and packed like the writer packs it."""

    def __init__(
        self, root, name="v", meta=META, project_type="images", schema_version=SCHEMA_VERSION
    ):
        self.root = os.path.join(str(root), name)
        self.meta = meta
        self.project_type = project_type
        self.schema_version = schema_version
        self.schema = get_image_snapshot_schema(schema_version)
        self.tag_schema = get_tag_schema()
        self.datasets = []
        self.images = []
        self.figures = []
        self.tags = []

    def dataset(self, dataset_id, name, full_path=None, parent_id=None):
        self.datasets.append(
            {
                VersionSchemaField.SRC_DATASET_ID: dataset_id,
                VersionSchemaField.PARENT_SRC_DATASET_ID: parent_id,
                VersionSchemaField.NAME: name,
                VersionSchemaField.FULL_PATH: full_path or name,
                VersionSchemaField.DESCRIPTION: None,
                VersionSchemaField.CUSTOM_DATA: None,
            }
        )
        return self

    def image(self, image_id, dataset_id, name, hash_=None, updated_at="2026-01-01T00:00:00.000Z"):
        # Distinct by default: two items sharing a hash are the same bytes, which is a
        # match the pass is supposed to make, and a fixture should have to ask for it.
        hash_ = hash_ if hash_ is not None else f"hash-{image_id}"
        self.images.append(
            {
                VersionSchemaField.SRC_IMAGE_ID: image_id,
                VersionSchemaField.SRC_DATASET_ID: dataset_id,
                VersionSchemaField.NAME: name,
                VersionSchemaField.HASH: hash_,
                VersionSchemaField.UPDATED_AT: updated_at,
                VersionSchemaField.CREATED_AT: "2026-01-01T00:00:00.000Z",
            }
        )
        return self

    def figure(
        self,
        figure_id,
        image_id,
        class_name="car",
        updated_at="2026-01-01T00:00:00.000Z",
        dataset_id=1,
    ):
        self.figures.append(
            {
                VersionSchemaField.SRC_FIGURE_ID: figure_id,
                VersionSchemaField.SRC_IMAGE_ID: image_id,
                VersionSchemaField.SRC_DATASET_ID: dataset_id,
                VersionSchemaField.CLASS_NAME: class_name,
                VersionSchemaField.GEOMETRY_TYPE: "rectangle",
                VersionSchemaField.GEOMETRY_JSON: json.dumps(GEOMETRY),
                VersionSchemaField.UPDATED_AT: updated_at,
                VersionSchemaField.CREATED_AT: "2026-01-01T00:00:00.000Z",
            }
        )
        return self

    def tag(
        self,
        tag_assignment_id,
        owner_type,
        owner_id,
        item_id,
        name="reviewed",
        value=None,
        frame_range=None,
        updated_at="2026-01-01T00:00:00.000Z",
    ):
        tag_json = {
            "id": tag_assignment_id,
            "tagId": 7,
            "name": name,
            "value": value,
            "updatedAt": updated_at,
            "createdAt": "2026-01-01T00:00:00.000Z",
        }
        if frame_range is not None:
            tag_json["frameRange"] = list(frame_range)
        self.tags.append(
            self.tag_schema.tag_row(
                tag_json, owner_type=owner_type, owner_id=owner_id, src_item_id=item_id
            )
        )
        return self

    def build(self):
        payload_dir = os.path.join(self.root, "payload")
        os.makedirs(payload_dir, exist_ok=True)

        dump_json_file(
            {"id": 1, "name": "p", "type": self.project_type},
            os.path.join(payload_dir, "project_info.json"),
        )
        dump_json_file(self.meta.to_json(), os.path.join(payload_dir, "project_meta.json"))

        tables = {
            "datasets": (self.schema.datasets_schema(pyarrow), self.datasets),
            "images": (self.schema.images_schema(pyarrow), self.images),
            "figures": (self.schema.figures_schema(pyarrow), self.figures),
            "tags": (self.tag_schema.tags_schema(pyarrow), self.tags),
        }

        tables_meta = []
        for name, (schema, rows) in tables.items():
            if not rows:
                continue
            table = pyarrow.Table.from_pylist(rows, schema=schema)
            parquet.write_table(table, os.path.join(payload_dir, f"{name}.parquet"))
            tables_meta.append(table_meta(name, f"{name}.parquet", table.num_rows))

        dump_json_file(
            {
                VersionSchemaField.SCHEMA_VERSION: self.schema_version,
                VersionSchemaField.TABLES: tables_meta,
            },
            os.path.join(payload_dir, "manifest.json"),
        )

        archive_path = os.path.join(self.root, "version.bin")
        with open(archive_path, "wb") as f:
            f.write(pack_payload_dir(payload_dir, self.root).getvalue())
        return archive_path


class VolumeSnapshotBuilder:
    """One volume snapshot.

    A different container from the other two - one binary blob of length-prefixed
    sections rather than a tar of Parquet files - and a different shape inside it: whole
    JSON annotation records, one per volume, with the objects, their figures and every
    tag nested in them. Built here the way the writer builds it, because that nesting is
    exactly what the reader has to flatten and what the diff then walks.
    """

    def __init__(self, root, name="v", meta=META):
        self.root = os.path.join(str(root), name)
        self.meta = meta
        self.schema = get_volume_snapshot_schema("v2.0.0")
        self.datasets = []
        self.volumes = []
        self.annotations = {}
        self._object_id = None
        self._object_ids = {}
        # The uuid keys are invented per parse, so two snapshots of one project never
        # share them. Naming the builder into them keeps the fixtures honest about that.
        self._parse = name

    def dataset(self, dataset_id, name, parent_id=None):
        self.datasets.append({ApiField.ID: dataset_id, ApiField.NAME: name, ApiField.PARENT_ID: parent_id})
        return self

    def volume(self, volume_id, dataset_id, name, hash_=None, updated_at="t1"):
        self.volumes.append(
            {
                ApiField.ID: volume_id,
                "dataset_id": dataset_id,
                ApiField.NAME: name,
                ApiField.HASH: hash_ or f"hash-{volume_id}",
                "created_at": "2026-01-01T00:00:00.000Z",
                "updated_at": updated_at,
            }
        )
        self.annotations.setdefault(
            volume_id,
            {volume_constants.TAGS: [], volume_constants.OBJECTS: [], volume_constants.PLANES: []},
        )
        self._volume_id = volume_id
        return self

    def obj(self, object_key, volume_id=None, class_name="car", object_id=None):
        """One annotation object. Both identities, the way a snapshot written by the SDK
        carries them: the server's id is what two versions are compared on, the uuid key is
        what binds figures to it when a version is restored."""
        annotation = self.annotations[volume_id or self._volume_id]
        annotation[volume_constants.OBJECTS].append(
            {
                volume_constants.ID: object_id,
                volume_constants.KEY: object_key,
                LabelJsonFields.OBJ_CLASS_NAME: class_name,
                volume_constants.TAGS: [],
            }
        )
        self._object_key = object_key
        self._object_id = object_id
        self._object_ids[object_key] = object_id
        return self

    def figure(
        self,
        figure_id,
        object_key=None,
        volume_id=None,
        slice_index=0,
        plane="axial",
        geometry=None,
        geometry_type=None,
        updated_at="t1",
    ):
        annotation = self.annotations[volume_id or self._volume_id]
        planes = annotation[volume_constants.PLANES]
        found = next((p for p in planes if p[volume_constants.NAME] == plane), None)
        if found is None:
            found = {volume_constants.NAME: plane, volume_constants.SLICES: []}
            planes.append(found)
        slices = found[volume_constants.SLICES]
        current = next(
            (s for s in slices if s[volume_constants.INDEX] == slice_index), None
        )
        if current is None:
            current = {volume_constants.INDEX: slice_index, volume_constants.FIGURES: []}
            slices.append(current)
        current[volume_constants.FIGURES].append(
            {
                volume_constants.ID: figure_id,
                volume_constants.KEY: f"fig-{figure_id}-{self._parse}",
                # A figure handed an earlier object's key must not take the last one's id.
                volume_constants.OBJECT_ID: self._object_ids.get(object_key or self._object_key),
                volume_constants.OBJECT_KEY: object_key or self._object_key,
                ApiField.GEOMETRY_TYPE: geometry_type or Rectangle.geometry_name(),
                ApiField.GEOMETRY: geometry or GEOMETRY,
                ApiField.UPDATED_AT: updated_at,
            }
        )
        return self

    def spatial_figure(
        self,
        figure_id,
        object_key=None,
        volume_id=None,
        geometry_type="mask_3d",
        updated_at="t1",
    ):
        """A figure that is the volume rather than a place in it - a 3D mask, a mesh - which
        is why it sits outside the planes and has no slice to name."""
        annotation = self.annotations[volume_id or self._volume_id]
        annotation.setdefault(volume_constants.SPATIAL_FIGURES, []).append(
            {
                volume_constants.ID: figure_id,
                volume_constants.KEY: f"fig-{figure_id}-{self._parse}",
                volume_constants.OBJECT_ID: self._object_ids.get(object_key or self._object_key),
                volume_constants.OBJECT_KEY: object_key or self._object_key,
                ApiField.GEOMETRY_TYPE: geometry_type,
                ApiField.GEOMETRY: GEOMETRY,
                ApiField.UPDATED_AT: updated_at,
            }
        )
        return self

    def tag(self, tag_assignment_id, volume_id=None, object_key=None, name="reviewed", value=None):
        """On the volume, or on one of its objects. Volumes have no figure tags."""
        annotation = self.annotations[volume_id or self._volume_id]
        tag_json = {"id": tag_assignment_id, "name": name, "value": value}
        if object_key is None:
            annotation[volume_constants.TAGS].append(tag_json)
        else:
            owner = next(
                obj
                for obj in annotation[volume_constants.OBJECTS]
                if obj[volume_constants.KEY] == object_key
            )
            owner[volume_constants.TAGS].append(tag_json)
        return self

    def build(self):
        from supervisely.project.volume_project import VolumeProject

        def table(rows, schema):
            sink = pyarrow.BufferOutputStream()
            parquet.write_table(pyarrow.Table.from_pylist(rows, schema=schema), sink)
            return sink.getvalue().to_pybytes()

        blob = VolumeProject._assemble_sections([
            (
                VolumeProject._SECTION_PROJECT_INFO,
                json.dumps({"id": 1, "name": "p", "type": "volumes"}).encode(),
            ),
            (VolumeProject._SECTION_PROJECT_META, json.dumps(self.meta.to_json()).encode()),
            (
                VolumeProject._SECTION_DATASETS,
                table(
                    [self.schema.dataset_row_from_record(row) for row in self.datasets],
                    self.schema.datasets_table_schema(pyarrow),
                ),
            ),
            (
                VolumeProject._SECTION_VOLUMES,
                table(
                    [self.schema.volume_row_from_record(row) for row in self.volumes],
                    self.schema.volumes_table_schema(pyarrow),
                ),
            ),
            (
                VolumeProject._SECTION_ANNOTATIONS,
                table(
                    [
                        self.schema.annotation_row_from_dict(
                            src_volume_id=volume_id, annotation=annotation
                        )
                        for volume_id, annotation in self.annotations.items()
                    ],
                    self.schema.annotations_table_schema(pyarrow),
                ),
            ),
        ])

        os.makedirs(self.root, exist_ok=True)
        archive_path = os.path.join(self.root, "version.bin")
        with open(archive_path, "wb") as f:
            f.write(blob)
        return archive_path


def diff_of(tmp_path, builder_from, builder_to, **kwargs):
    """Run the pass over two builders and return (summary, detail records)."""
    output_dir = os.path.join(str(tmp_path), "out")
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

    records = []
    for chunk in result.detail_chunks:
        with open(os.path.join(output_dir, chunk), encoding="utf-8") as f:
            records.extend(json.load(f))
    return result.summary, records


def one_dataset(root, name):
    return SnapshotBuilder(root, name).dataset(1, "ds1", full_path="ds1")


def records_by_item(records):
    return {record["itemId"]: record for record in records}




class VideoSnapshotBuilder:
    """A video snapshot, the one shape with an annotation-object level above figures.

    Kept separate from SnapshotBuilder rather than generalised: the two modalities have
    genuinely different tables, and a builder that pretended otherwise would hide the one
    thing these tests are about.
    """

    SCHEMA_VERSION = "v2.1.0"

    def __init__(self, root, name="v", meta=META):
        self.root = os.path.join(str(root), name)
        self.meta = meta
        self.schema = get_video_snapshot_schema(self.SCHEMA_VERSION)
        self.tag_schema = get_tag_schema()
        self.datasets = []
        self.videos = []
        self.objects = []
        self.figures = []
        self.tags = []

    def dataset(self, dataset_id, name, full_path=None, parent_id=None):
        self.datasets.append(
            {
                VersionSchemaField.SRC_DATASET_ID: dataset_id,
                VersionSchemaField.PARENT_SRC_DATASET_ID: parent_id,
                VersionSchemaField.NAME: name,
                VersionSchemaField.FULL_PATH: full_path or name,
            }
        )
        return self

    def video(self, video_id, dataset_id, name, updated_at="t1", frames_count=10):
        self.videos.append(
            {
                VersionSchemaField.SRC_VIDEO_ID: video_id,
                VersionSchemaField.SRC_DATASET_ID: dataset_id,
                VersionSchemaField.NAME: name,
                VersionSchemaField.HASH: f"hash-{video_id}",
                VersionSchemaField.FRAMES_COUNT: frames_count,
                VersionSchemaField.UPDATED_AT: updated_at,
            }
        )
        return self

    def obj(self, object_id, video_id, class_name="car", updated_at="t1"):
        self.objects.append(
            {
                VersionSchemaField.SRC_OBJECT_ID: object_id,
                VersionSchemaField.SRC_VIDEO_ID: video_id,
                VersionSchemaField.CLASS_NAME: class_name,
                VersionSchemaField.UPDATED_AT: updated_at,
            }
        )
        return self

    def figure(
        self,
        figure_id,
        object_id,
        video_id,
        frame_index=0,
        geometry_type="rectangle",
        updated_at="t1",
    ):
        self.figures.append(
            {
                VersionSchemaField.SRC_FIGURE_ID: figure_id,
                VersionSchemaField.SRC_OBJECT_ID: object_id,
                VersionSchemaField.SRC_VIDEO_ID: video_id,
                VersionSchemaField.FRAME_INDEX: frame_index,
                VersionSchemaField.GEOMETRY_TYPE: geometry_type,
                VersionSchemaField.GEOMETRY_JSON: json.dumps(GEOMETRY),
                VersionSchemaField.UPDATED_AT: updated_at,
            }
        )
        return self

    def tag(
        self,
        tag_assignment_id,
        owner_type,
        owner_id,
        item_id,
        name="reviewed",
        value=None,
        frame_range=None,
        updated_at="t1",
    ):
        tag_json = {
            "id": tag_assignment_id,
            "tagId": 7,
            "name": name,
            "value": value,
            "updatedAt": updated_at,
        }
        if frame_range is not None:
            tag_json["frameRange"] = list(frame_range)
        self.tags.append(
            self.tag_schema.tag_row(
                tag_json, owner_type=owner_type, owner_id=owner_id, src_item_id=item_id
            )
        )
        return self

    def build(self):
        payload_dir = os.path.join(self.root, "payload")
        os.makedirs(payload_dir, exist_ok=True)

        dump_json_file(
            {"id": 1, "name": "p", "type": "videos"},
            os.path.join(payload_dir, "project_info.json"),
        )
        dump_json_file(self.meta.to_json(), os.path.join(payload_dir, "project_meta.json"))

        tables = {
            "datasets": (self.schema.datasets_schema(pyarrow), self.datasets),
            "videos": (self.schema.videos_schema(pyarrow), self.videos),
            "objects": (self.schema.objects_schema(pyarrow), self.objects),
            "figures": (self.schema.figures_schema(pyarrow), self.figures),
            "tags": (self.tag_schema.tags_schema(pyarrow), self.tags),
        }

        tables_meta = []
        for name, (schema, rows) in tables.items():
            if not rows:
                continue
            table = pyarrow.Table.from_pylist(rows, schema=schema)
            parquet.write_table(table, os.path.join(payload_dir, f"{name}.parquet"))
            tables_meta.append(table_meta(name, f"{name}.parquet", table.num_rows))

        dump_json_file(
            {
                VersionSchemaField.SCHEMA_VERSION: self.SCHEMA_VERSION,
                VersionSchemaField.TABLES: tables_meta,
            },
            os.path.join(payload_dir, "manifest.json"),
        )

        archive_path = os.path.join(self.root, "version.bin")
        with open(archive_path, "wb") as f:
            f.write(pack_payload_dir(payload_dir, self.root).getvalue())
        return archive_path
