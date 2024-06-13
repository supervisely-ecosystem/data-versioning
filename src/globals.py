import os
from distutils.util import strtobool

import supervisely as sly
from dotenv import load_dotenv

if sly.is_development():
    load_dotenv("local.env")
    load_dotenv(os.path.expanduser("~/supervisely.env"))

PROJECT_ID = int(os.getenv("PROJECT_ID"))
api: sly.Api = sly.Api.from_env()

action = "create" if bool(strtobool(os.environ.get("modal.state.create"))) else "restore"
if action is "create":
    version_title = str(os.environ.get("modal.state.versionTitle"))
    version_description = str(os.environ.get("modal.state.versionDescription"))
    target_version = None
elif action is "restore":
    version_title = None
    version_description = None
    target_version = int(os.environ.get("modal.state.targetVersion"))
