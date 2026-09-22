# coding: utf-8

"""The diff task, against a fake instance.

Team Files is a local directory here, and the version snapshots the fake instance serves
are real archives (tests/snapshots.py). What is worth asserting at this level is the
published layout, the order `status.json` is written in, and the answers the task gives
when it cannot do the work: the diff itself is covered in test_versions_diff.py.
"""

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import diff_task
from snapshots import SnapshotBuilder, VolumeSnapshotBuilder, one_dataset

TEAM_ID = 3
PROJECT_ID = 92
VERSION_ID_FROM = 10
VERSION_ID_TO = 17
VERSION_FROM = 4
VERSION_TO = 7

REPORT_DIR = f"/system/versions/{PROJECT_ID}/diffs/{VERSION_ID_FROM}_{VERSION_ID_TO}/"


class _FakeFileApi:
    """Team Files as a directory tree, with ids handed out in upload order."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.next_id = 1000
        self.removed: List[str] = []
        # Every write in order, so a test can assert what was on the instance when.
        self.writes: List[str] = []

    def _local(self, remote_path: str) -> Path:
        return self.root / remote_path.lstrip("/")

    def dir_exists(self, team_id: int, remote_path: str) -> bool:
        return self._local(remote_path).is_dir()

    def remove_dir(self, team_id: int, remote_path: str, silent: bool = False) -> None:
        self.removed.append(remote_path)
        shutil.rmtree(self._local(remote_path), ignore_errors=True)

    def _store(self, src: str, dst: str) -> SimpleNamespace:
        target = self._local(dst)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        self.next_id += 1
        self.writes.append(dst)
        return SimpleNamespace(id=self.next_id, name=os.path.basename(dst), path=dst)

    def upload(self, team_id: int, src: str, dst: str, progress_cb: Any = None):
        return self._store(src, dst)

    def upload_bulk(
        self, team_id: int, src_paths: List[str], dst_paths: List[str], progress_cb: Any = None
    ) -> List[SimpleNamespace]:
        return [self._store(src, dst) for src, dst in zip(src_paths, dst_paths)]

    def get_info_by_path(self, team_id: int, remote_path: str):
        local = self._local(remote_path)
        if not local.is_file():
            return None
        return SimpleNamespace(id=1, name=local.name, path=remote_path)

    def get_json_file_content(self, team_id: int, remote_path: str, download: bool = False):
        return json.loads(self._local(remote_path).read_text(encoding="utf-8"))

    def download_directory(
        self, team_id: int, remote_path: str, local_save_path: str, progress_cb: Any = None
    ) -> None:
        shutil.copytree(self._local(remote_path), local_save_path, dirs_exist_ok=True)

    def listing(self, remote_dir: str = REPORT_DIR) -> List[str]:
        base = self._local(remote_dir)
        if not base.is_dir():
            return []
        return sorted(
            str(path.relative_to(base)) for path in base.rglob("*") if path.is_file()
        )

    def status(self, remote_dir: str = REPORT_DIR) -> dict:
        return json.loads(
            (self._local(remote_dir) / diff_task.STATUS_FILE_NAME).read_text(encoding="utf-8")
        )


class _FakeVersionApi:
    def __init__(self, snapshots: Dict[int, str]):
        self.snapshots = snapshots
        self.numbers = {VERSION_ID_FROM: VERSION_FROM, VERSION_ID_TO: VERSION_TO}

    def get_info_by_id(self, project_id: int, version_id: int) -> Optional[SimpleNamespace]:
        if version_id not in self.numbers:
            return None
        return SimpleNamespace(
            id=version_id,
            project_id=project_id,
            version=self.numbers[version_id],
            team_id=TEAM_ID,
            created_at="2026-01-01T00:00:00.000Z",
            preview_project_id=None,
        )

    def download_snapshot(self, project, version_id: int, dest_path: str = None):
        shutil.copyfile(self.snapshots[version_id], dest_path)
        return dest_path


class _FakeTaskApi:
    def __init__(self, statuses: Optional[Dict[int, str]] = None) -> None:
        self.statuses = statuses or {}

    def get_status(self, task_id: int):
        return self.statuses[task_id]


def _api(files: _FakeFileApi, versions: _FakeVersionApi, tasks: _FakeTaskApi = None):
    return SimpleNamespace(
        file=files,
        project=SimpleNamespace(version=versions),
        task=tasks or _FakeTaskApi(),
    )


@pytest.fixture
def files(tmp_path: Path) -> _FakeFileApi:
    return _FakeFileApi(tmp_path / "tf")


def _two_versions(tmp_path: Path) -> Dict[int, str]:
    before = one_dataset(tmp_path, "a").image(100, 1, "img1", updated_at="t1")
    before.image(101, 1, "img2", updated_at="t1").figure(500, 100, updated_at="t1")

    after = one_dataset(tmp_path, "b").image(100, 1, "img1", updated_at="t2")
    after.image(102, 1, "img3", updated_at="t1").figure(500, 100, updated_at="t2")

    return {VERSION_ID_FROM: before.build(), VERSION_ID_TO: after.build()}


def _run(api, version_id_from=VERSION_ID_FROM, version_id_to=VERSION_ID_TO, task_id=555):
    return diff_task.run(
        api, PROJECT_ID, version_id_from, version_id_to, task_id=task_id
    )


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


def test_the_report_is_published_under_the_version_ids(
    files: _FakeFileApi, tmp_path: Path
) -> None:
    api = _api(files, _FakeVersionApi(_two_versions(tmp_path)))

    status = _run(api)

    assert status["status"] == diff_task.STATUS_DONE
    assert files.listing() == [
        "data/items_0001.json",
        # Both metas, so the report can be re-rendered from the published directory alone.
        "data/meta_from.json",
        "data/meta_to.json",
        "diff.json",
        "state.json",
        # The state of the run; the panel reads only this to decide.
        "status.json",
        # The rendered report. Its team-file id is the report id.
        "template.vue",
    ]
    assert status["reportId"] is not None
    assert status["diffFileId"] is not None

    published = json.loads((files.root / REPORT_DIR.lstrip("/") / "diff.json").read_text())
    assert published["items"] == {
        "added": 1,
        "removed": 1,
        "renamed": 0,
        "moved": 0,
        "contentChanged": 0,
        "annotationChanged": 1,
        "unchanged": 0,
    }


def test_the_status_is_in_progress_before_the_report_and_done_after_it(
    files: _FakeFileApi, tmp_path: Path
) -> None:
    """A half-uploaded directory must never read as a report.

    The panel decides from `status.json` alone, so it is written first — carrying the task
    id, which is how a run that died is told from one still working — and rewritten to
    `done` only once every file it promises is on the instance.
    """
    api = _api(files, _FakeVersionApi(_two_versions(tmp_path)))

    _run(api, task_id=777)

    status_path = f"{REPORT_DIR}status.json"
    assert files.writes[0] == status_path
    assert files.writes[-1] == status_path
    # Nothing else is written twice: the report files go up once, between the two statuses.
    assert files.writes.count(status_path) == 2
    assert files.status()["taskId"] == 777


def test_the_pair_gets_one_directory_whichever_order_it_was_picked_in(
    files: _FakeFileApi, tmp_path: Path
) -> None:
    api = _api(files, _FakeVersionApi(_two_versions(tmp_path)))

    status = _run(api, version_id_from=VERSION_ID_TO, version_id_to=VERSION_ID_FROM)

    assert status["versionIdFrom"] == VERSION_ID_FROM
    assert status["versionIdTo"] == VERSION_ID_TO
    assert files.listing() != []


def test_a_recomputed_report_never_deletes_and_indexes_its_own_chunks(
    files: _FakeFileApi, tmp_path: Path
) -> None:
    """Nothing under /system/ can be deleted through the API by any caller, so a stale
    chunk from a previous run survives. It is inert because diff.json lists the chunks
    that belong to the report, and only those are ever read."""
    stale = files.root / REPORT_DIR.lstrip("/") / "data"
    stale.mkdir(parents=True)
    (stale / "items_0009.json").write_text("[]", encoding="utf-8")

    api = _api(files, _FakeVersionApi(_two_versions(tmp_path)))
    _run(api)

    assert files.removed == []
    assert "data/items_0009.json" in files.listing()
    published = json.loads((files.root / REPORT_DIR.lstrip("/") / "diff.json").read_text())
    assert published["details"]["chunks"] == ["data/items_0001.json"]


def test_a_run_overwrites_the_wreckage_of_the_one_before_it(
    files: _FakeFileApi, tmp_path: Path
) -> None:
    """A task killed mid-run leaves `in_progress` behind. Since the directory cannot be
    deleted, the recompute has to be able to write straight over it."""
    broken = files.root / REPORT_DIR.lstrip("/")
    broken.mkdir(parents=True)
    (broken / "status.json").write_text(
        json.dumps({"status": diff_task.STATUS_IN_PROGRESS, "taskId": 41}), encoding="utf-8"
    )
    api = _api(files, _FakeVersionApi(_two_versions(tmp_path)), _FakeTaskApi({41: "error"}))

    status = _run(api, task_id=42)

    assert status["status"] == diff_task.STATUS_DONE
    assert files.status()["taskId"] == 42


def test_a_pair_a_live_task_already_owns_is_refused(
    files: _FakeFileApi, tmp_path: Path
) -> None:
    """The panel is the real gate; this is what catches a request that went around it."""
    running = files.root / REPORT_DIR.lstrip("/")
    running.mkdir(parents=True)
    (running / "status.json").write_text(
        json.dumps({"status": diff_task.STATUS_IN_PROGRESS, "taskId": 41}), encoding="utf-8"
    )
    api = _api(files, _FakeVersionApi(_two_versions(tmp_path)), _FakeTaskApi({41: "started"}))

    with pytest.raises(diff_task.DiffAlreadyRunning):
        _run(api, task_id=42)

    assert files.status()["taskId"] == 41


# --------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------


def test_an_unknown_version_is_named_in_the_error(
    files: _FakeFileApi, tmp_path: Path
) -> None:
    api = _api(files, _FakeVersionApi(_two_versions(tmp_path)))

    with pytest.raises(ValueError, match="999"):
        _run(api, version_id_to=999)

    # Refused before anything was claimed on the instance.
    assert files.writes == []


def test_an_old_snapshot_is_recorded_as_unsupported_not_failed(
    files: _FakeFileApi, tmp_path: Path
) -> None:
    old = SnapshotBuilder(tmp_path, "old", schema_version="v2.0.0").dataset(1, "ds1")
    old.image(100, 1, "img1")
    current = one_dataset(tmp_path, "current").image(100, 1, "img1")
    api = _api(
        files, _FakeVersionApi({VERSION_ID_FROM: old.build(), VERSION_ID_TO: current.build()})
    )

    status = _run(api)

    assert status["status"] == diff_task.STATUS_UNSUPPORTED
    # A code the panel branches on, not a sentence it has to parse.
    assert status["code"] == "schema_too_old"
    assert status["versionId"] == VERSION_ID_FROM
    assert status["schemaVersion"] == "v2.0.0"
    assert str(VERSION_ID_FROM) in status["message"]
    # The refusal is the only thing published; there is no report to show.
    assert files.listing() == ["status.json"]
    assert files.status() == status


def test_a_v21_volume_snapshot_without_server_figure_ids_is_unsupported(
    files: _FakeFileApi, tmp_path: Path
) -> None:
    broken = VolumeSnapshotBuilder(tmp_path, "broken").dataset(1, "ds1")
    broken.volume(100, 1, "scan").obj("obj", object_id=500).figure(None)
    current = VolumeSnapshotBuilder(tmp_path, "current").dataset(1, "ds1")
    current.volume(100, 1, "scan").obj("obj", object_id=500).figure(700)
    api = _api(
        files,
        _FakeVersionApi({VERSION_ID_FROM: broken.build(), VERSION_ID_TO: current.build()}),
    )

    status = _run(api)

    assert status["status"] == diff_task.STATUS_UNSUPPORTED
    # The schema is current here — only the records give it away.
    assert status["code"] == "no_server_figure_ids"
    assert status["schemaVersion"] == "v2.1.0"
    assert "server-side figure ids" in status["message"]
    assert files.listing() == ["status.json"]


def test_a_crash_leaves_a_failed_status_rather_than_a_run_that_never_ends(
    files: _FakeFileApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(files, _FakeVersionApi(_two_versions(tmp_path)))

    def explode(*args, **kwargs):
        raise RuntimeError("out of disk")

    monkeypatch.setattr(diff_task, "_publish", explode)

    with pytest.raises(RuntimeError):
        _run(api)

    status = files.status()
    assert status["status"] == diff_task.STATUS_FAILED
    assert "out of disk" in status["message"]
