import os
from typing import List, Union

import supervisely as sly
from supervisely import Api, Dataset, OpenMode, Project, ProjectInfo, ProjectMeta
from supervisely._utils import batched
from supervisely.project.project import Project, _maybe_append_image_extension


def create_project_mapping(api: sly.Api, project_id: int, project_version: int, download_dir: str):
    """
    Create a mapping of project items to datasets

    :param api: sly.Api object
    :param project_id: int
    :param download_dir: str
    :return: dict
    """
    project = api.project.get_info_by_id(project_id)
    project_mapping = {}
    for parents, dataset_info in api.dataset.tree(project_id):
        dataset_mapping = {}
        for image in api.image.get_list(dataset_info.id):
            version_info = {
                "name": image.name,
                "hash": image.hash,
                "updated_at": image.updated_at,
                "version": project_version,
            }
            dataset_mapping[image.id] = {"type": "image", "info": version_info}

        current_level = project_mapping
        for parent in parents:
            current_level = current_level.setdefault(parent, {})
        current_level[dataset_info.id] = {
            "type": "dataset",
            "items": dataset_mapping,
            "name": dataset_info.name,
        }

    json_path = os.path.join(download_dir, f"{project.id}_mapping.json")
    sly.json.dump_json_file(project_mapping, json_path)
    return project_mapping


def diff_and_update_maps(old_map, new_map):
    """
    Compare two maps and update the version of image.id in version_info if its updated_at is the same

    :param old_map: dict
    :param new_map: dict
    :return: tuple (updated new_map, list of image ids with updated version)
    """
    updated_image_ids = []
    for dataset_id, dataset_info in new_map.items():
        if dataset_id in old_map:
            for image_id, image_info in dataset_info["items"].items():
                if image_id in old_map[dataset_id]["items"]:
                    old_image_info = old_map[dataset_id]["items"][image_id]["info"]
                    new_image_info = image_info["info"]
                    if new_image_info["updated_at"] == old_image_info["updated_at"]:
                        new_image_info["version"] = old_image_info["version"]
                    else:
                        updated_image_ids.append(image_id)
    return new_map, updated_image_ids


def download_dataset(
    api: Api,
    dataset: Dataset,
    dataset_id: int,
    item_ids: Union[List[int], None] = None,
):
    """
    Download a dataset from Supervisely to a local directory consisting only of annotations for the specified items.

    :param api: Supervisely API object
    :param dataset: Dataset object
    :param dataset_id: Dataset id
    :param item_ids: List of item ids to download
    """

    if item_ids is None:
        images_to_download = api.image.get_list(dataset_id)
        item_ids = [img.id for img in images_to_download]
    else:
        filters = [{"field": "id", "operator": "=", "value": id} for id in item_ids]
        images_to_download = api.image.get_list(dataset_id, filters)

    ann_info_list = api.annotation.download_batch(dataset_id, item_ids)
    img_name_to_ann = {ann.image_id: ann.annotation for ann in ann_info_list}
    for img_info_batch in batched(images_to_download):
        images_nps = [None] * len(img_info_batch)
        for index, _ in enumerate(images_nps):
            img_info = img_info_batch[index]
            image_name = _maybe_append_image_extension(img_info.name, img_info.ext)

            dataset.add_item_np(
                item_name=image_name,
                img=None,
                ann=img_name_to_ann[img_info.id],
                img_info=None,
            )


def download_project(
    api: Api,
    project_info: ProjectInfo,
    project_dir: str,
    item_ids=None,
):
    """
    Download a project from Supervisely to a local directory consisting only of annotations for the specified items.

    :param api: Supervisely API object
    :param project_info: ProjectInfo object
    :param project_dir: Directory to download the project to
    :param item_ids: List of item ids to download
    """

    project_id = project_info.id
    project_fs = Project(project_dir, OpenMode.CREATE)
    meta = ProjectMeta.from_json(api.project.get_meta(project_id, with_settings=True))
    project_fs.set_meta(meta)
    for parents, dataset_info in api.dataset.tree(project_id):
        dataset_path = Dataset._get_dataset_path(dataset_info.name, parents)
        dataset_name = dataset_info.name
        dataset_id = dataset_info.id

        dataset = project_fs.create_dataset(dataset_name, dataset_path)
        download_dataset(
            api,
            dataset,
            dataset_id,
            item_ids=item_ids,
        )
