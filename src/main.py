import supervisely as sly
from supervisely.tiny_timer import TinyTimer

import globals as g


def main():
    project_info = g.api.project.get_info_by_id(g.PROJECT_ID)
    g.api.app.add_input_project(project_info)
    timer = TinyTimer()
    if project_info.version:
        version_id = project_info.version.get("id")
        version_num = project_info.version.get("version")
    else:
        version_id, version_num = None, None
    if g.action == "create":
        project_version_id = g.api.project.version.create(
            project_info, g.version_title, g.version_description
        )
        if project_info.version and project_version_id == version_id:
            g.api.app.set_output_text(
                g.TASK_ID,
                "New restore point was not created",
                description=f'There are no changes in the project "{project_info.name}"',
                zmdi_icon="zmdi-close-circle",
                icon_color="#FFA500",
                background_color="#FFE8BE",
            )
        else:
            if version_num is None:
                version_num = 0
            g.api.app.add_output_project(project_info, project_version_id)
            g.api.app.set_output_text(
                g.TASK_ID,
                "New restore point created",
                description=f"Version: {version_num + 1}",
                zmdi_icon="zmdi-time-restore",
            )
    else:
        new_project_info = g.api.project.version.restore(project_info, g.target_version)
        g.api.app.add_output_project(new_project_info)
        g.api.app.set_output_project(g.TASK_ID, new_project_info.id, new_project_info.name)
    diff = timer.get_sec()
    sly.logger.debug(f"Project version {g.action} took {diff:.2f} sec")


if __name__ == "__main__":
    sly.main_wrapper("main", main)
