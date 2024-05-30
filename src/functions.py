from typing import List, Union

import supervisely as sly
from supervisely import Api, Dataset, OpenMode, Project, ProjectInfo, ProjectMeta
from supervisely._utils import batched
from supervisely.project.project import Project, _maybe_append_image_extension


def create_project_map(api: sly.Api, project_id: int, project_version: int):
    """
    Create a mapping of project items to datasets

    :param api: sly.Api object
    :param project_id: int
    :return: dict
    """

    project_map = {"datasets": {}, "items": {}}

    for parents, dataset_info in api.dataset.tree(project_id):
        parent_id = parents[-1] if parents else 0
        project_map["datasets"][dataset_info.id] = {
            "name": dataset_info.name,
            "version": project_version,
            "parent": parent_id,
        }

        for image_list in api.image.get_list_generator(dataset_info.id):
            for image in image_list:
                version_info = {
                    "name": image.name,
                    "hash": image.hash,
                    "updated_at": image.updated_at,
                    "version": project_version,
                    "parent": dataset_info.id,
                    "status": "new",
                }
                project_map["items"][image.id] = version_info
    return project_map


def diff_and_update_maps(old_map, new_map, new_version):
    """
    Compare two maps and update items info. Return updated map, new item ids and deleted item ids.

    :param old_map: dict
    :param new_map: dict
    :param new_version: int
    :return: tuple
    """
    if not old_map or not new_map:
        raise ValueError("Both old_map and new_map must be provided")

    old_items = {str(k): v for k, v in old_map.get("items", {}).items()}
    new_items = {str(k): v for k, v in new_map.get("items", {}).items()}
    deleted_items = set(old_items) - set(new_items)

    new_item_ids = set()
    deleted_item_ids = set()

    for image_id, image_info in new_items.items():
        image_id_str = str(image_id)
        if image_id_str in old_items:
            if old_items[image_id_str].get("updated_at") != image_info.get("updated_at"):
                old_items[image_id_str]["status"] = "new"
                old_items[image_id_str]["version"] = new_version
                old_items[image_id_str]["updated_at"] = image_info.get("updated_at")
                new_item_ids.add(int(image_id_str))
            else:
                old_items[image_id_str]["status"] = "unchanged"
        else:
            image_info["status"] = "new"
            old_items[image_id_str] = image_info
            new_item_ids.add(int(image_id_str))

    for item_id in deleted_items:
        if old_items[item_id]["status"] != "deleted":
            old_items[item_id]["status"] = "deleted"
            old_items[item_id]["version"] = new_version
            deleted_item_ids.add(item_id)

    old_map["items"] = old_items

    return old_map, new_item_ids, deleted_item_ids


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
        item_ids = {img.id for img in images_to_download}
    else:
        filters = [{"field": "id", "operator": "=", "value": id} for id in item_ids]
        images_to_download = api.image.get_list(dataset_id, filters)

    ann_info_list = api.annotation.download_batch(dataset_id, list(item_ids))
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
