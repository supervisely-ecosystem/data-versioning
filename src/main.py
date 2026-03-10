import supervisely as sly
from supervisely import logger
from supervisely.api.app_api import WorkflowMeta, WorkflowSettings
from supervisely.tiny_timer import TinyTimer

import globals as g


def main():
    project_info = g.api.project.get_info_by_id(g.PROJECT_ID)
    if g.action == g.ActionType.CREATE:
        g.api.app.workflow.add_input_project(project_info, task_id=g.TASK_ID, meta=g.create_meta)
    else:
        g.api.app.workflow.add_input_project(
            project_info, task_id=g.TASK_ID, version_num=g.version_num, meta=g.restore_meta
        )
    timer = TinyTimer()
    if project_info.version:
        version_id = project_info.version.get("id")
        version_num = project_info.version.get("version")
    else:
        version_id, version_num = None, None
    if g.action == g.ActionType.CREATE:
        logger.info(f"Create new version for project: {project_info.name}")
        logger.info(f"Name: {g.version_name}, Description: {g.version_description}")
        logger.info(f"Enable preview: {g.enable_preview}")
        project_version_id = g.api.project.version.create(
            project_info=project_info,
            version_title=g.version_name,
            version_description=g.version_description,
            enable_preview=g.enable_preview,
        )
        if project_version_id is None:
            g.api.app.set_output_text(
                g.TASK_ID,
                f'New restore point was not created for "{project_info.name}"',
                description=f"See logs for details. Project ID: {project_info.id}",
                zmdi_icon="zmdi-close-circle",
                icon_color="#FFA500",
                background_color="#FFE8BE",
            )
        elif project_info.version and project_version_id == version_id:
            g.api.app.set_output_text(
                g.TASK_ID,
                f'New restore point was not created for "{project_info.name}"',
                description=f"No changes with the previous version {version_num}. Project ID: {project_info.id}",
                zmdi_icon="zmdi-close-circle",
                icon_color="#FFA500",
                background_color="#FFE8BE",
            )

        else:
            if version_num is None:
                version_num = 0

            meta = None
            if g.enable_preview:
                preview_project_info = g.api.project.version.get_info_by_id(
                    project_info.id, version_id=project_version_id
                )
                if preview_project_info.preview_project_id is not None:
                    meta = WorkflowMeta(
                        node_settings=WorkflowSettings(
                            url=g.api.server_address
                            + f"/projects/{preview_project_info.preview_project_id}/datasets",
                            url_title="Open",
                            description="With Enabled Preview",
                        )
                    )
            g.api.app.workflow.add_output_project(
                project_info,
                project_version_id,
                task_id=g.TASK_ID,
                meta=meta,
            )
            g.api.app.set_output_text(
                g.TASK_ID,
                f'New restore point created for "{project_info.name}"',
                description=f"Project ID: {project_info.id}, Version: {version_num + 1}",
                zmdi_icon="zmdi-time-restore",
            )
    elif g.action == g.ActionType.RESTORE:
        logger.info(f"Restore project: {project_info.name} from version: {g.version_num}")
        new_project_info = g.api.project.version.restore(project_info, version_num=g.version_num)
        if new_project_info is None:
            g.api.app.set_output_text(
                g.TASK_ID,
                f'Project "{project_info.name}" was not restored',
                description=f"Version number: {g.version_num}. See logs for details",
                zmdi_icon="zmdi-close-circle",
                icon_color="#FFA500",
                background_color="#FFE8BE",
            )
        else:
            g.api.app.workflow.add_output_project(new_project_info, task_id=g.TASK_ID)
            g.api.app.set_output_project(g.TASK_ID, new_project_info.id, new_project_info.name)
    elif g.action == g.ActionType.ENABLE_PREVIEW:
        logger.info(f"Enabling preview of version {g.version_num} for project {project_info.name}")
        version_id = g.api.project.version.get_id_by_number(
            project_id=project_info.id, version_num=g.version_num
        )
        new_project_info = g.api.project.version.enable_preview(
            project=project_info, version_id=version_id
        )
        logger.info(f"Preview enabled successfully. Preview project ID: {new_project_info.id}")
        g.api.app.workflow.add_output_project(
            project=project_info,
            version_id=version_id,
            task_id=g.TASK_ID,
            meta=WorkflowMeta(
                node_settings=WorkflowSettings(
                    title=f"Enable Version Preview",
                    url=g.api.server_address + f"/projects/{new_project_info.id}/datasets",
                    url_title="Open",
                )
            ),
        )
        g.api.app.set_output_text(
            g.TASK_ID,
            f"Preview enabled for version number {g.version_num}",
            description=f"Project ID: {project_info.id}, Version ID: {version_id}",
            zmdi_icon="zmdi-eye",
        )
    elif g.action == g.ActionType.RESTORE_PREVIEW:
        logger.info(f"Restoring version {g.version_num} preview")
        version_id = g.api.project.version.get_id_by_number(
            project_id=project_info.id, version_num=g.version_num
        )
        new_project_info = g.api.project.version.enable_preview(
            project=project_info, version_id=version_id, overwrite=True
        )
        logger.info(f"Preview restored successfully. Preview project ID: {new_project_info.id}")
        g.api.app.workflow.add_output_project(
            project=project_info,
            version_id=version_id,
            task_id=g.TASK_ID,
            meta=WorkflowMeta(
                node_settings=WorkflowSettings(
                    title=f"Restore Version Preview",
                    url=g.api.server_address + f"/projects/{new_project_info.id}/datasets",
                    url_title="Open",
                )
            ),
        )
        g.api.app.set_output_text(
            g.TASK_ID,
            f"Preview restored for version number {g.version_num}",
            description=f"Project ID: {project_info.id}, Version ID: {version_id}",
            zmdi_icon="zmdi-eye",
        )
    diff = timer.get_sec()
    logger.debug(f"Project version {g.action} took {diff:.2f} sec")


if __name__ == "__main__":
    sly.main_wrapper("main", main)
