"""CLI tests for ``hermes-a365 allowlist resolve`` (stdin only)."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import subprocess
from types import SimpleNamespace

import pytest

from hermes_a365 import cli

UPN = "zz-canary+325@example.test"
OID = "11111111-2222-3333-4444-555555555555"
OID2 = "66666666-7777-8888-9999-aaaaaaaaaaaa"


def _fake_az(responses):
    calls = []

    def fake_run(argv, **_kw):
        calls.append(argv)
        rc, out, err = responses.pop(0)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    return fake_run, calls


def _main(argv, monkeypatch, capsys, responses, stdin=""):
    fake, calls = _fake_az(responses)
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    try:
        rc = cli.main(argv)
    except SystemExit as exc:
        rc = exc.code
    out, err = capsys.readouterr()
    return rc, out, err, calls


def test_cli_allowlist_resolve_prints_object_ids_only(monkeypatch, capsys):
    rc, out, err, calls = _main(
        ["allowlist", "resolve"],
        monkeypatch,
        capsys,
        [(0, json.dumps({"id": OID.upper()}), "")],
        stdin=f"{UPN}\n{OID2}\n",
    )
    assert rc == 0
    assert out == f"A365_ALLOWED_USERS={OID},{OID2}\n"
    assert "@" not in out and UPN not in err
    assert len(calls) == 1 and calls[0][:4] == ["az", "rest", "--method", "GET"]


@pytest.mark.parametrize(
    ("responses", "code", "stderr_frag", "call_count"),
    [
        (
            [
                (1, "", "Not Found(Request_ResourceNotFound)"),
                (0, json.dumps({"value": [{"id": OID}, {"id": OID2}]}), ""),
            ],
            2,
            "entry 1 ambiguous",
            2,
        ),
        (
            [
                (1, "", "Not Found(Request_ResourceNotFound)"),
                (0, json.dumps({"value": []}), ""),
            ],
            2,
            "entry 1 unresolvable",
            2,
        ),
        ([(1, "", "Forbidden(Authorization_RequestDenied)")], 3, None, 1),
        ([(1, "", "Too Many Requests(TooManyRequests)")], 4, None, 1),
        ([(1, "", "Internal Server Error")], 4, None, 1),
    ],
)
def test_cli_allowlist_resolve_fails_closed(
    monkeypatch, capsys, responses, code, stderr_frag, call_count
):
    rc, out, err, calls = _main(
        ["allowlist", "resolve"],
        monkeypatch,
        capsys,
        responses,
        stdin=f"{UPN}\n",
    )
    assert rc == code
    assert "A365_ALLOWED_USERS" not in out
    assert len(calls) <= 2
    if stderr_frag:
        assert stderr_frag in err
        assert len(calls) == call_count


def test_cli_allowlist_resolve_timeout_fails_closed(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(["az"], 30, output=UPN)

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{UPN}\n"))
    try:
        rc = cli.main(["allowlist", "resolve"])
    except SystemExit as exc:
        rc = exc.code
    out, err = capsys.readouterr()
    assert rc == 4 and "A365_ALLOWED_USERS" not in out and UPN not in err


@pytest.mark.parametrize(
    "stub",
    [
        "az_stderr",
        "timeout",
        "ambiguous",
        "unresolvable",
        "403",
        "429",
        "5xx",
    ],
)
def test_cli_allowlist_resolve_never_leaks_entry(monkeypatch, capsys, caplog, stub):
    caplog.set_level(logging.DEBUG)
    err_msg = f'Not Found({{"error":{{"message":"{UPN}"}}}})'
    if stub == "az_stderr":
        fake, _calls = _fake_az([(1, "", err_msg), (0, json.dumps({"value": []}), "")])
        monkeypatch.setattr(subprocess, "run", fake)
    elif stub == "timeout":

        def boom(*_a, **_k):
            raise subprocess.TimeoutExpired(["az"], 30, output=UPN)

        monkeypatch.setattr(subprocess, "run", boom)
    elif stub == "ambiguous":
        fake, _calls = _fake_az(
            [
                (1, "", err_msg),
                (0, json.dumps({"value": [{"id": OID}, {"id": OID2}]}), ""),
            ]
        )
        monkeypatch.setattr(subprocess, "run", fake)
    elif stub == "unresolvable":
        fake, _calls = _fake_az([(1, "", err_msg), (0, json.dumps({"value": []}), "")])
        monkeypatch.setattr(subprocess, "run", fake)
    elif stub == "403":
        fake, _calls = _fake_az([(1, "", f"Forbidden(Authorization_RequestDenied {UPN})")])
        monkeypatch.setattr(subprocess, "run", fake)
    elif stub == "429":
        fake, _calls = _fake_az([(1, "", f"Too Many Requests(TooManyRequests {UPN})")])
        monkeypatch.setattr(subprocess, "run", fake)
    else:
        fake, _calls = _fake_az([(1, "", f"Internal Server Error {UPN}")])
        monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{UPN}\n"))
    with contextlib.suppress(SystemExit):
        cli.main(["allowlist", "resolve"])
    out, err = capsys.readouterr()
    for text in (out, err, caplog.text):
        assert UPN not in text and "zz-canary" not in text and "example.test" not in text


def test_user_lookup_guard_message_has_no_url_or_entry():
    from hermes_a365.allowlist_resolve import assert_user_lookup_url

    for bad in (
        f"https://graph.microsoft.com/v1.0/users/{UPN}/memberOf?$select=id",
        f"https://evil.test/v1.0/users/{UPN}?$select=id",
        f"https://graph.microsoft.com/v1.0/users/{UPN}?$select=id,mail",
    ):
        with pytest.raises(ValueError) as exc:
            assert_user_lookup_url(bad)
        assert UPN not in str(exc.value) and "graph.microsoft.com" not in str(exc.value)
