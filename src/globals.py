import os
from distutils.util import strtobool

import supervisely as sly
from dotenv import load_dotenv

if sly.is_development():
    load_dotenv("local.env")
    load_dotenv(os.path.expanduser("~/supervisely.env"))


PROJECT_ID = int(os.getenv("PROJECT_ID"))
api: sly.Api = sly.Api.from_env()

if sly.is_development():
    TASK_ID = int(os.getenv("TASK_ID"))
else:
    TASK_ID = int(api.task_id)

action = os.environ.get("modal.state.actionType")
skip_missed = strtobool(os.environ.get("modal.state.skipMissed"))
if action == "create":
    version_title = str(os.environ.get("modal.state.title"))
    if version_title == "":
        version_title = None
    version_description = str(os.environ.get("modal.state.description"))
    if version_description == "":
        version_description = None
    version_num = None
elif action == "restore":
    version_title = None
    version_description = None
    version_num = int(os.environ.get("modal.state.version"))


create_meta = {"customNodeSettings": {"title": "<h4>Create New Version</h4>"}}

restore_meta = {"customNodeSettings": {"title": "<h4>Restore From Version</h4>"}}
