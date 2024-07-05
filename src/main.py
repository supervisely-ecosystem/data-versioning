import supervisely as sly
from supervisely.tiny_timer import TinyTimer

import workflow as f


def main():
    project_info = f.api.project.get_info_by_id(f.PROJECT_ID)
    f.workflow.add_input(project_info)
    timer = TinyTimer()
    if project_info.version:
        version_id = project_info.version.get("id")
        version_num = project_info.version.get("version")
    else:
        version_id, version_num = None, None
    if f.action == "create":
        project_version_id = f.api.project.version.create(
            project_info, f.version_title, f.version_description
        )
        if project_version_id is None:
            f.api.app.set_output_text(
                f.TASK_ID,
                "New restore point was not created",
                description=f"See logs for details. Project ID: {project_info.id}",
                zmdi_icon="zmdi-close-circle",
                icon_color="#FFA500",
                background_color="#FFE8BE",
            )
        elif project_info.version and project_version_id == version_id:
            f.api.app.set_output_text(
                f.TASK_ID,
                "New restore point was not created",
                description=f"No changes with the previous version {version_num}. Project ID: {project_info.id}",
                zmdi_icon="zmdi-close-circle",
                icon_color="#FFA500",
                background_color="#FFE8BE",
            )

        else:
            if version_num is None:
                version_num = 0
            f.workflow.add_output(project_info, project_version_id)
            f.api.app.set_output_text(
                f.TASK_ID,
                f'New restore point created for "{project_info.name}"',
                description=f"Project ID: {project_info.id}, Version: {version_num + 1}",
                zmdi_icon="zmdi-time-restore",
            )
    else:
        new_project_info = f.api.project.version.restore(project_info, version_num=f.version_num)
        if new_project_info is None:
            f.api.app.set_output_text(
                f.TASK_ID,
                "Project was not restored",
                description="See logs for details",
                zmdi_icon="zmdi-close-circle",
                icon_color="#FFA500",
                background_color="#FFE8BE",
            )
        else:
            f.workflow.add_output(new_project_info)
            f.api.app.set_output_project(f.TASK_ID, new_project_info.id, new_project_info.name)
    diff = timer.get_sec()
    sly.logger.debug(f"Project version {f.action} took {diff:.2f} sec")


if __name__ == "__main__":
    sly.main_wrapper("main", main)
