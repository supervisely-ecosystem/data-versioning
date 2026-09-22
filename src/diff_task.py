# coding: utf-8

"""Diff between two project versions, computed by this app's task — supervisely/issues#5891.

The computation used to live in `python-api` behind a BullMQ queue, with a row in
`projects_versions_diffs` holding its state. Both are gone: a task of this app reads the two
snapshots on its agent's resources and writes everything — result *and* state — into Team
Files, so there is exactly one source of truth and no shared worker pool to queue behind.

**Nothing is restored and nothing is rendered from pixels.** The report names what changed —
a tree of dataset, item, object, figure, tag — rather than showing it, so one implementation
serves images, videos and volumes by reading the snapshots directly (`VersionSnapshot`).

The report directory is the product, and its layout is a contract with the panel:

    /system/versions/<project_id>/diffs/<from_version_id>_<to_version_id>/
        status.json             # the state of the run; the panel reads only this to decide
        template.vue            # the rendered report; its team-file id IS the report id
        state.json
        diff.json               # the artifact; the report is a renderer over it
        data/items_0001.json    # detail records, chunked
        data/meta_from.json     # both metas, so the report can be re-rendered later
        data/meta_to.json

`status.json` is written first and rewritten last, never in between: until the final write
says `done`, a half-uploaded directory reads as unfinished rather than as a report. Nothing
under `/system/` can be deleted through the API by design, so a recompute overwrites in
place; detail chunks left over from a longer previous run are dead bytes, because
`diff.json` carries the list of its own chunks and the report reads only what that names.
"""

import json
import os
import time
from tempfile import TemporaryDirectory
from typing import Optional, Tuple

import supervisely as sly
from supervisely import logger
from supervisely.api.task_api import TaskApi
from supervisely.project.data_version import VersionInfo
from supervisely.project.versioning.snapshot_reader import VersionSnapshot

from versions_diff import DIFF_FILE_NAME, VersionRef, compute_diff, msec
from versions_diff_report import VersionsDiffReport

# Where versions already live. The diff sits beside them because it is derived from them
# and shares their lifetime.
TF_VERSIONS_DIR = "/system/versions"
TF_DIFFS_DIR_NAME = "diffs"

STATUS_FILE_NAME = "status.json"

# The rendered report's entry point. Its team-file id is the report id, which is what
# `instance-widgets.get-template` takes.
TEMPLATE_FILE_NAME = "template.vue"

STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_UNSUPPORTED = "unsupported"

# Statuses a task can still be in while it works. Anything else means the task that wrote
# an `in_progress` status is gone and the result it promised will never arrive.
_LIVE_TASK_STATUSES = frozenset(
    status.value
    for status in (
        TaskApi.Status.QUEUED,
        TaskApi.Status.CONSUMED,
        TaskApi.Status.STARTED,
        TaskApi.Status.DEPLOYED,
    )
)


class DiffUnsupported(Exception):
    """The pair cannot be compared and never will be.

    Told apart from a failure on purpose: a failure is worth retrying, this is not.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["message"])
        self.detail = detail


class DiffAlreadyRunning(Exception):
    """Another live task is already computing this pair."""


def report_dir(project_id: int, version_id_from: int, version_id_to: int) -> str:
    """Keyed by version *ids*: that is the key the panel joins a diff to a version by."""
    return (
        f"{TF_VERSIONS_DIR}/{project_id}/{TF_DIFFS_DIR_NAME}/"
        f"{version_id_from}_{version_id_to}/"
    )


def run(
    api: sly.Api,
    project_id: int,
    version_id_from: int,
    version_id_to: int,
    task_id: Optional[int] = None,
    work_root: Optional[str] = None,
) -> dict:
    """Compute the diff for one version pair and publish it. Returns the written status.

    Raises `DiffUnsupported` and `DiffAlreadyRunning` for the two refusals the caller
    reports differently; every other failure is caught, recorded as `failed` in
    `status.json` and re-raised.
    """
    version_from, version_to = _version_pair(
        api, project_id, version_id_from, version_id_to
    )
    # Normalise so the lower version number is `from`: one pair, one directory, whichever
    # order the pair was picked in.
    if version_from.version > version_to.version:
        version_from, version_to = version_to, version_from
        version_id_from, version_id_to = version_id_to, version_id_from

    team_id = version_to.team_id
    remote_dir = report_dir(project_id, version_id_from, version_id_to)
    log_meta = {
        "projectId": project_id,
        "versionIdFrom": version_id_from,
        "versionIdTo": version_id_to,
        "taskId": task_id,
    }
    logger.info("Version diff started", extra=log_meta)
    tm = sly.TinyTimer()

    _refuse_if_running(api, team_id, remote_dir, task_id, log_meta)

    status = {
        "status": STATUS_IN_PROGRESS,
        "taskId": task_id,
        "versionIdFrom": version_id_from,
        "versionIdTo": version_id_to,
        "startedAt": _now(),
    }
    _write_status(api, team_id, remote_dir, status)

    progress = sly.Progress("Comparing versions", total_cnt=4)
    try:
        with TemporaryDirectory(prefix=f"diff-{project_id}-", dir=work_root) as work_dir:
            summary, stats = _compute_and_write(
                api,
                project_id,
                version_id_from,
                version_id_to,
                version_from,
                version_to,
                work_dir,
                log_meta,
                progress,
            )

            stage = time.perf_counter()
            VersionsDiffReport(api, work_dir).generate()
            stats["render_msec"] = msec(stage)
            progress.iter_done_report()

            stage = time.perf_counter()
            report_id, diff_file_id = _publish(api, team_id, work_dir, remote_dir)
            stats["publish_msec"] = msec(stage)
            progress.iter_done_report()
    except DiffUnsupported as refusal:
        # `message` is a reserved LogRecord field, so the refusal goes in as one value.
        logger.info("Version diff refused", extra={**log_meta, "refusal": refusal.detail})
        return _write_status(
            api,
            team_id,
            remote_dir,
            {**status, "status": STATUS_UNSUPPORTED, "finishedAt": _now(), **refusal.detail},
        )
    except Exception as error:
        logger.error("Version diff failed", extra=log_meta, exc_info=True)
        _write_status(
            api,
            team_id,
            remote_dir,
            {
                **status,
                "status": STATUS_FAILED,
                "finishedAt": _now(),
                "message": f"{error.__class__.__name__}: {error}",
            },
        )
        raise

    # One structured line with the whole run in it: how big the two versions were, how much
    # of them had to be read, what each stage cost, and which team file to open. A diff that
    # is slow or surprising is diagnosed from here or not at all.
    logger.info(
        "Version diff finished",
        extra={
            **log_meta,
            "durat_msec": round(tm.get_sec() * 1000.0, 1),
            "records": summary["details"]["recordCount"],
            "reportId": report_id,
            "reportDir": remote_dir,
            **stats,
        },
    )
    # Last write, and only now: everything the report needs is already uploaded.
    return _write_status(
        api,
        team_id,
        remote_dir,
        {
            **status,
            "status": STATUS_DONE,
            "finishedAt": _now(),
            "reportId": report_id,
            "diffFileId": diff_file_id,
        },
    )


def read_status(api: sly.Api, team_id: int, remote_dir: str) -> Optional[dict]:
    """The recorded state of a run, or None when this pair was never computed."""
    path = f"{remote_dir}{STATUS_FILE_NAME}"
    if api.file.get_info_by_path(team_id, path) is None:
        return None
    try:
        return api.file.get_json_file_content(team_id, path)
    except Exception:
        # A status we cannot read is a status we cannot trust; treat it as no status and
        # let the run overwrite it.
        logger.warning(f"Unreadable diff status at {path}", exc_info=True)
        return None


def _refuse_if_running(
    api: sly.Api,
    team_id: int,
    remote_dir: str,
    task_id: Optional[int],
    log_meta: dict,
) -> None:
    """Refuse only when another task is demonstrably still working on this pair.

    The panel is the real gate; this catches the case where it was bypassed. A stale
    `in_progress` left by a task that was killed does not block anything — that is the
    whole reason the task id is recorded next to the status.
    """
    status = read_status(api, team_id, remote_dir)
    if status is None or status.get("status") != STATUS_IN_PROGRESS:
        return
    other_task_id = status.get("taskId")
    if other_task_id is None or other_task_id == task_id:
        return
    try:
        other_status = api.task.get_status(other_task_id)
    except Exception:
        logger.warning(
            f"Cannot tell whether task {other_task_id} is still running", extra=log_meta
        )
        return
    if str(getattr(other_status, "value", other_status)) in _LIVE_TASK_STATUSES:
        raise DiffAlreadyRunning(
            f"Task {other_task_id} is already comparing these versions."
        )


def _version_pair(
    api: sly.Api, project_id: int, version_id_from: int, version_id_to: int
) -> Tuple[VersionInfo, VersionInfo]:
    versions = {}
    for version_id in (version_id_from, version_id_to):
        info = api.project.version.get_info_by_id(project_id, version_id)
        if info is None:
            raise ValueError(
                f"Version {version_id} does not exist in project {project_id}."
            )
        versions[version_id] = info
    return versions[version_id_from], versions[version_id_to]


def _publish(
    api: sly.Api, team_id: int, local_dir: str, remote_dir: str
) -> Tuple[Optional[int], Optional[int]]:
    """Upload the report directory; return the team-file ids of `template.vue` and `diff.json`."""
    src_paths, dst_paths = [], []
    for root, _, files in os.walk(local_dir):
        for name in sorted(files):
            local_path = os.path.join(root, name)
            relative = os.path.relpath(local_path, local_dir)
            src_paths.append(local_path)
            dst_paths.append(f"{remote_dir}{relative}")

    uploaded = api.file.upload_bulk(team_id, src_paths, dst_paths)
    logger.debug(f"Published {len(uploaded)} report files to {remote_dir}")

    ids = {file_info.name: file_info.id for file_info in uploaded}
    return ids.get(TEMPLATE_FILE_NAME), ids.get(DIFF_FILE_NAME)


def _write_status(api: sly.Api, team_id: int, remote_dir: str, payload: dict) -> dict:
    with TemporaryDirectory() as tmp_dir:
        local_path = os.path.join(tmp_dir, STATUS_FILE_NAME)
        _write_json(local_path, payload)
        api.file.upload(team_id, local_path, f"{remote_dir}{STATUS_FILE_NAME}")
    return payload


def _unsupported_diff(version_id: int, snapshot: VersionSnapshot) -> Optional[dict]:
    """Why this snapshot cannot take part in a comparison, in a form a caller can act on.

    The panel gates on the format recorded in `versions.json`, which is the cheap answer;
    this is the one the snapshot itself gives, and it catches what the record cannot know —
    a snapshot numbered as the current format whose figures went in without server ids.
    A code rather than a sentence, because what the caller does with it differs: neither is
    worth retrying, and only one of them can be fixed by cutting a new version.
    """
    reason = snapshot.diff_unsupported_reason
    if reason is None:
        return None
    return {
        "code": (
            "schema_too_old"
            if not VersionSnapshot.format_is_diffable(snapshot.schema_version)
            else "no_server_figure_ids"
        ),
        "versionId": version_id,
        "schemaVersion": snapshot.schema_version,
        "message": f"Version {version_id} cannot be compared: {reason}.",
    }


def _snapshot_identity(prefix: str, snapshot: VersionSnapshot) -> dict:
    """What decides which path the pass takes, and whether it can compare annotations.

    Logged because a report that says "annotations were not compared" has its reason here,
    and a run that is slower than its size explains is usually one that fell back off the
    columnar path.
    """
    return {
        f"{prefix}Schema": snapshot.schema_version,
        f"{prefix}Columnar": snapshot.is_columnar,
        # Not the same question: a volume snapshot is columnar and still has no columns to
        # project, which is exactly the case that reads slowly.
        f"{prefix}Arrow": snapshot.serves_arrow,
        f"{prefix}ServerFigureIds": snapshot.figure_ids_are_server_ids,
    }


def _compute_and_write(
    api: sly.Api,
    project_id: int,
    version_id_from: int,
    version_id_to: int,
    version_from: VersionInfo,
    version_to: VersionInfo,
    work_dir: str,
    log_meta: dict,
    progress: Optional[sly.Progress] = None,
) -> Tuple[dict, dict]:
    """Open both snapshots, diff them, and write the artifact into `work_dir`."""
    stage = time.perf_counter()
    with VersionSnapshot.open(api, project_id, version_id_from) as snapshot_from:
        with VersionSnapshot.open(api, project_id, version_id_to) as snapshot_to:
            for version_id, snapshot in (
                (version_id_from, snapshot_from),
                (version_id_to, snapshot_to),
            ):
                refusal = _unsupported_diff(version_id, snapshot)
                if refusal is not None:
                    raise DiffUnsupported(refusal)
            opened_msec = msec(stage)
            logger.info(
                "Version snapshots opened",
                extra={
                    **log_meta,
                    "snapshots_msec": opened_msec,
                    **_snapshot_identity("from", snapshot_from),
                    **_snapshot_identity("to", snapshot_to),
                },
            )
            if progress is not None:
                progress.iter_done_report()
            result = compute_diff(
                snapshot_from,
                snapshot_to,
                version_from=VersionRef(
                    id=version_id_from,
                    number=version_from.version,
                    created_at=version_from.created_at,
                ),
                version_to=VersionRef(
                    id=version_id_to,
                    number=version_to.version,
                    created_at=version_to.created_at,
                ),
                output_dir=work_dir,
            )
            if progress is not None:
                progress.iter_done_report()

    summary = result.summary
    _write_json(os.path.join(work_dir, DIFF_FILE_NAME), summary)
    return summary, {"snapshots_msec": opened_msec, **result.stats}


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
