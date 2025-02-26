import os

import supervisely as sly
from dotenv import load_dotenv
from supervisely import logger

if sly.is_development():
    load_dotenv("local.env")
    load_dotenv(os.path.expanduser("~/supervisely.env"))


PROJECT_ID = int(os.getenv("PROJECT_ID"))
api: sly.Api = sly.Api.from_env()

if sly.is_development():
    TASK_ID = int(os.getenv("TASK_ID"))
    api.app.workflow.enable()
else:
    TASK_ID = int(api.task_id)

action = os.environ.get("modal.state.actionType")
if action == "create":
    version_name = str(os.environ.get("modal.state.versionName"))
    if version_name == "":
        version_name = None
    version_description = str(os.environ.get("modal.state.description"))
    if version_description == "":
        version_description = None
    version_num = None
    logger.info(f"Version Name  - '{version_name}', Version Description - '{version_description}'")
elif action == "restore":
    version_name = None
    version_description = None
    version_num = int(os.environ.get("modal.state.version"))


create_meta = {"customNodeSettings": {"title": "<h4>Create New Version</h4>"}}

restore_meta = {"customNodeSettings": {"title": "<h4>Restore From Version</h4>"}}
