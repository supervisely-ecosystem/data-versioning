import supervisely as sly
from supervisely import logger
from supervisely.tiny_timer import TinyTimer

import globals as g


def main():
    project_info = g.api.project.get_info_by_id(g.PROJECT_ID)
    if g.action == "create":
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
    if g.action == "create":
        logger.info(f"Create new version for project: {project_info.name}")
        logger.info(f"Title: {g.version_title}, Description: {g.version_description}")
        project_version_id = g.api.project.version.create(
            project_info, g.version_title, g.version_description
        )
        if project_version_id is None:
            g.api.app.set_output_text(
                g.TASK_ID,
                "New restore point was not created",
                description=f"See logs for details. Project ID: {project_info.id}",
                zmdi_icon="zmdi-close-circle",
                icon_color="#FFA500",
                background_color="#FFE8BE",
            )
        elif project_info.version and project_version_id == version_id:
            g.api.app.set_output_text(
                g.TASK_ID,
                "New restore point was not created",
                description=f"No changes with the previous version {version_num}. Project ID: {project_info.id}",
                zmdi_icon="zmdi-close-circle",
                icon_color="#FFA500",
                background_color="#FFE8BE",
            )

        else:
            if version_num is None:
                version_num = 0
            g.api.app.workflow.add_output_project(
                project_info, project_version_id, task_id=g.TASK_ID
            )
            g.api.app.set_output_text(
                g.TASK_ID,
                f'New restore point created for "{project_info.name}"',
                description=f"Project ID: {project_info.id}, Version: {version_num + 1}",
                zmdi_icon="zmdi-time-restore",
            )
    else:
        logger.info(f"Restore project: {project_info.name} from version: {g.version_num}")
        new_project_info = g.api.project.version.restore(project_info, version_num=g.version_num)
        if new_project_info is None:
            g.api.app.set_output_text(
                g.TASK_ID,
                "Project was not restored",
                description="See logs for details",
                zmdi_icon="zmdi-close-circle",
                icon_color="#FFA500",
                background_color="#FFE8BE",
            )
        else:
            g.api.app.workflow.add_output_project(new_project_info, task_id=g.TASK_ID)
            g.api.app.set_output_project(g.TASK_ID, new_project_info.id, new_project_info.name)
    diff = timer.get_sec()
    sly.logger.debug(f"Project version {g.action} took {diff:.2f} sec")


if __name__ == "__main__":
    sly.main_wrapper("main", main)
