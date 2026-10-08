import os

from dotenv import load_dotenv

import supervisely as sly
from supervisely import logger

if sly.is_development():
    load_dotenv("local.env")
    load_dotenv(os.path.expanduser("~/supervisely.env"))


class ActionType:
    CREATE = "create"
    RESTORE = "restore"
    ENABLE_PREVIEW = "enable_preview"
    RESTORE_PREVIEW = "restore_preview"
    DIFF = "diff"


api: sly.Api = sly.Api.from_env()

if sly.is_development():
    TASK_ID = int(os.getenv("TASK_ID"))
    api.app.workflow.enable()
else:
    TASK_ID = int(api.task_id)

action = os.environ.get("modal.state.actionType")
enable_preview = os.environ.get("modal.state.enablePreview", "false").strip().lower() in (
    "true",
    "1",
    "yes",
)
version_name = None
version_description = None
version_id = None
version_num = None
version_id_from = None
version_id_to = None
if action == ActionType.CREATE:
    PROJECT_ID = int(os.getenv("PROJECT_ID"))
    version_name = str(os.environ.get("modal.state.versionName"))
    if version_name == "":
        version_name = None
    version_description = str(os.environ.get("modal.state.description"))
    if version_description == "":
        version_description = None
elif action in (ActionType.RESTORE, ActionType.ENABLE_PREVIEW):
    PROJECT_ID = int(os.getenv("PROJECT_ID"))
    version_num = int(os.environ.get("modal.state.version"))
    version_id = int(os.environ.get("modal.state.versionId"))
elif action == ActionType.RESTORE_PREVIEW:
    PROJECT_ID = int(os.environ.get("modal.state.sourceProjectId"))
    version_id = int(os.environ.get("modal.state.versionId"))
elif action == ActionType.DIFF:
    PROJECT_ID = int(os.getenv("PROJECT_ID"))
    version_id_from = int(os.environ.get("modal.state.versionIdFrom"))
    version_id_to = int(os.environ.get("modal.state.versionIdTo"))
else:
    # Every branch above defines PROJECT_ID; without this the app used to die in main()
    # with a NameError instead of saying what it was asked to do.
    raise ValueError(f"Unknown action type: {action}")

create_meta = {"customNodeSettings": {"title": "<h4>Create New Version</h4>"}}
restore_meta = {"customNodeSettings": {"title": "<h4>Restore From Version</h4>"}}
