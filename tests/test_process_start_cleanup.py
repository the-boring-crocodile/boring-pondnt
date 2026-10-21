"""Startup failure ownership, with injected Win32 boundaries and no real process."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from boring_service_kit import process


@pytest.fixture
def windows(monkeypatch):
    child = Mock(_handle=123)
    api = SimpleNamespace(CloseHandle=Mock())
    job = object()
    jobs = SimpleNamespace(
        CreateJobObject=Mock(return_value=job),
        QueryInformationJobObject=Mock(return_value={"BasicLimitInformation": {"LimitFlags": 0}, "ActiveProcesses": 0}),
        SetInformationJobObject=Mock(),
        AssignProcessToJobObject=Mock(),
        TerminateJobObject=Mock(),
        JobObjectExtendedLimitInformation=9,
        JobObjectBasicAccountingInformation=1,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=8192,
    )
    ntdll = SimpleNamespace(NtResumeProcess=Mock(return_value=0))
    popen = Mock(return_value=child)
    monkeypatch.setattr(process, "IS_WINDOWS", True)
    monkeypatch.setattr(process, "win32api", api, raising=False)
    monkeypatch.setattr(process, "win32job", jobs, raising=False)
    monkeypatch.setattr(process, "_ntdll", ntdll, raising=False)
    monkeypatch.setattr(process.subprocess, "Popen", popen)
    return SimpleNamespace(child=child, api=api, jobs=jobs, job=job, ntdll=ntdll, popen=popen)


def start():
    return process.Tree(["fixture"], Path("."), {}, None, None)


@pytest.mark.parametrize("phase", ["query", "limits", "spawn", "assign", "resume"])
def test_start_failure_releases_every_acquired_resource(windows, phase):
    w = windows
    failing = {
        "query": w.jobs.QueryInformationJobObject,
        "limits": w.jobs.SetInformationJobObject,
        "spawn": w.popen,
        "assign": w.jobs.AssignProcessToJobObject,
        "resume": w.ntdll.NtResumeProcess,
    }[phase]
    failure = OSError("synthetic startup failure")
    failing.side_effect = failure
    with pytest.raises(OSError) as caught:
        start()
    assert caught.value is failure
    w.api.CloseHandle.assert_called_once_with(w.job)
    if phase in {"assign", "resume"}:
        w.child.kill.assert_called_once_with()
        w.child.wait.assert_called_once_with(10)
    else:
        w.child.kill.assert_not_called()
    if phase in {"query", "limits", "spawn", "assign"}:
        w.ntdll.NtResumeProcess.assert_not_called()


def test_resume_error_status_cleans_up(windows):
    windows.ntdll.NtResumeProcess.return_value = -1
    with pytest.raises(OSError, match="NtResumeProcess failed"):
        start()
    windows.child.kill.assert_called_once_with()
    windows.api.CloseHandle.assert_called_once_with(windows.job)


def test_failed_child_cleanup_still_releases_job(windows):
    windows.jobs.AssignProcessToJobObject.side_effect = OSError("assignment")
    windows.child.kill.side_effect = OSError("termination")
    with pytest.raises(OSError, match="termination"):
        start()
    windows.api.CloseHandle.assert_called_once_with(windows.job)


def test_success_retains_job_until_owner_closes_it(windows):
    tree = start()
    windows.child.kill.assert_not_called()
    windows.api.CloseHandle.assert_not_called()
    tree.close()
    tree.close()
    windows.api.CloseHandle.assert_called_once_with(windows.job)
