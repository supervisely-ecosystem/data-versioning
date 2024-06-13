import supervisely as sly
from supervisely.tiny_timer import TinyTimer

import globals as g


def main():
    project_info = g.api.project.get_info_by_id(g.PROJECT_ID)
    timer = TinyTimer()
    if g.action == "create":
        project_info = g.api.project.version.create(
            project_info, g.version_title, g.version_description
        )
        g.api.app.set_output_text(
            g.api.task_id, "New restore point created.", zmdi_icon="zmdi-time-restore"
        )
    else:
        new_project_info = g.api.project.version.restore(project_info, g.target_version)
        g.api.app.set_output_project(g.api.task_id, new_project_info.id, new_project_info.name)
    diff = timer.get_sec()
    sly.logger.debug(f"Project version {g.action} took {diff:.2f} sec")


if __name__ == "__main__":
    sly.main_wrapper("main", main)
