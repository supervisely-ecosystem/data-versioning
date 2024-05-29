import os

import supervisely as sly
import zstd
from dotenv import load_dotenv
from supervisely.io.fs import archive_directory
from tqdm import tqdm

from func import create_project_mapping, diff_and_update_maps, download_project

if sly.is_development():
    load_dotenv("local.env")
    load_dotenv(os.path.expanduser("~/supervisely.env"))

PROJECT_ID = int(os.getenv("PROJECT_ID"))


def main():
    api = sly.Api.from_env()

    download_dir = os.path.join(os.getcwd(), "download")

    project_info = api.project.get_info_by_id(PROJECT_ID)
    cur_version = project_info.custom_data.get("project-version", None)
    cur_project_mapping = project_info.custom_data.get("project-mapping", None)

    if cur_version is None:
        version = 1
    else:
        version = int(cur_version) + 1
    project_info.custom_data["project-version"] = version

    project_mapping = create_project_mapping(api, PROJECT_ID, version, download_dir)

    if cur_project_mapping is not None:
        project_mapping, item_ids = diff_and_update_maps(cur_project_mapping, project_mapping)
    else:
        item_ids = None

    if item_ids is None or len(item_ids) > 0:
        if os.path.exists(download_dir):
            sly.fs.remove_dir(download_dir)
        download_project(api, project_info, download_dir, item_ids=item_ids)
    else:
        sly.logger.info(
            "You already have the latest snapshot of the project. No need to backup it again."
        )
        return

    archive_path = os.path.join(os.path.dirname(download_dir), "download.tar")
    archive_directory(download_dir, archive_path)
    zst_archive_path = archive_path + ".zst"
    with open(archive_path, "rb") as tar:
        with open(zst_archive_path, "wb") as zst:
            zst.write(zstd.ZSTD_compress(tar.read()))

    remote_path = f"/PVS/{PROJECT_ID}/{PROJECT_ID}_v{version}.tar.zst"

    progress_cb = tqdm(
        desc=f"Creating backup version v{version}",
        total=os.path.getsize(zst_archive_path),
        unit="B",
        unit_scale=True,
    )
    api.file.upload(project_info.team_id, zst_archive_path, remote_path, progress_cb)
    progress_cb.close()

    project_info.custom_data["project-mapping"] = project_mapping
    api.project.update_custom_data(PROJECT_ID, project_info.custom_data, silent=True)

    sly.fs.silent_remove(archive_path)
    sly.fs.silent_remove(zst_archive_path)
    sly.fs.silent_remove(download_dir)


if __name__ == "__main__":
    main()
