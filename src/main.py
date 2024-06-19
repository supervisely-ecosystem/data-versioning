import supervisely as sly
from supervisely.tiny_timer import TinyTimer

import globals as g


def main():
    project_info = g.api.project.get_info_by_id(g.PROJECT_ID)
    timer = TinyTimer()
    if g.action == "create":
        project_version_id = g.api.project.version.create(
            project_info, g.version_title, g.version_description
        )
        if project_info.version and project_version_id == project_info.version.get("id"):
            g.api.app.set_output_text(
                g.TASK_ID,
                "There is no changes in the project. New restore point was not created.",
                description="Version",
                zmdi_icon="zmdi-close-circle",
                icon_color="#FFA500",
                background_color="#FFE8BE",
            )
        else:
            g.api.app.add_output_project(project_info, project_version_id)
            g.api.app.set_output_text(
                g.TASK_ID,
                "New restore point created",
                description="Version",
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
