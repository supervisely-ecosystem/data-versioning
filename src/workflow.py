import supervisely as sly

# import all necessary functions and variables from globals.py to prevent circular imports in main.py
from globals import (
    PROJECT_ID,
    TASK_ID,
    action,
    api,
    create_meta,
    restore_meta,
    version_description,
    version_num,
    version_title,
)


def check_compatibility(func):
    def wrapper(self, *args, **kwargs):
        if self.is_compatible is None:
            self.is_compatible = self.check_instance_ver_compatibility()
        if not self.is_compatible:
            return
        return func(self, *args, **kwargs)

    return wrapper


class Workflow:
    def __init__(self, api: sly.Api, min_instance_version: str = None):
        self.is_compatible = None
        self.api = api
        self._min_instance_version = (
            "6.9.31" if min_instance_version is None else min_instance_version
        )

    def check_instance_ver_compatibility(self):
        if not self.api.is_version_supported(self._min_instance_version):
            sly.logger.info(
                f"Supervisely instance version {self.api.instance_version} does not support workflow and versioning features."
            )
            if not sly.is_community():
                sly.logger.info(
                    f"To use them, please update your instance to version {self._min_instance_version} or higher."
                )
            return False
        return True

    @check_compatibility
    def add_input(self, project_info: sly.ProjectInfo):
        if action == "create":
            api.app.workflow.add_input_project(project_info, task_id=TASK_ID, meta=create_meta)
        else:
            api.app.workflow.add_input_project(
                project_info, task_id=TASK_ID, version_num=version_num, meta=restore_meta
            )

    @check_compatibility
    def add_output(self, project_info: sly.ProjectInfo, project_version_id: int = None):
        api.app.workflow.add_output_project(project_info, project_version_id)


workflow = Workflow(api)
