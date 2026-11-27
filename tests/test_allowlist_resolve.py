"""Tests for ``hermes-a365 allowlist resolve`` (#325)."""

from __future__ import annotations

import argparse
import logging
import sys
from io import StringIO
from typing import Any
from urllib.parse import unquote

import pytest

from hermes_a365 import allowlist_resolve
from hermes_a365.entra_state import EntraStateError, assert_user_lookup_url

CANARY = "zz-canary+325@example.test"
CANARY_LOCAL = "zz-canary+325"
CANARY_QUOTED = "zz-canary%2B325%40example.test"


def _run(
    stdin: str,
    *,
    getter,
    sleep=None,
    verify: bool = False,
    capsys=None,
    caplog=None,
):
    args = argparse.Namespace(verify=verify)
    if caplog is not None:
        caplog.set_level(logging.DEBUG)
    from pytest import MonkeyPatch

    mp = MonkeyPatch()
    mp.setattr(sys, "stdin", StringIO(stdin))
    try:
        code = allowlist_resolve.run(args, getter=getter, sleep=sleep or (lambda _s: None))
    finally:
        mp.undo()
    if capsys is not None:
        return code, capsys.readouterr()
    return code


class RoutingGetter:
    def __init__(self, table: list[tuple[Any, dict[str, Any]]]) -> None:
        self.table = table
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict[str, Any]:
        self.urls.append(url)
        decoded = unquote(url)
        for needle, body in self.table:
            if callable(needle):
                if needle(url, decoded):
                    return body
            elif needle in url or needle in decoded:
                return body
        return {"status": 404, "error_code": "Request_ResourceNotFound"}


def test_resolve_emits_object_ids_only_and_never_upn(capsys: pytest.CaptureFixture[str]) -> None:
    upn_a = "alice@contoso.example"
    upn_b = "bob@contoso.example"
    oid_a = "11111111-1111-1111-1111-111111111111"
    oid_b = "22222222-2222-2222-2222-222222222222"
    kept = "33333333-3333-3333-3333-333333333333"
    getter = RoutingGetter(
        [
            ("alice%40", {"status": 200, "error_code": "", "id": oid_a}),
            ("bob%40", {"status": 200, "error_code": "", "id": oid_b}),
        ]
    )
    code, (out, err) = _run(f"{upn_a}\n{upn_b}\n{kept}\n", getter=getter, capsys=capsys)
    assert code == 0
    assert out == f"A365_ALLOWED_USERS={oid_a},{oid_b},{kept}\n"
    assert "@" not in out
    assert "resolved=" in err


def test_resolve_refuses_ambiguous_mail_and_prints_no_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    upn = "ambiguous@contoso.example"
    getter = RoutingGetter(
        [
            (lambda url, _decoded: "/users/" in url and "ambiguous" in url, {"status": 404, "error_code": "Request_ResourceNotFound"}),
            (
                lambda url, _decoded: "$filter=" in url,
                {
                    "status": 200,
                    "error_code": "",
                    "ids": [
                        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    ],
                },
            ),
        ]
    )
    code, (out, err) = _run(f"{upn}\n", getter=getter, capsys=capsys)
    assert code == 2
    assert not out.startswith("A365_ALLOWED_USERS=")
    assert "index=1" in err
    assert "fingerprint=" in err
    assert upn not in err
    assert "contoso.example" not in err


@pytest.mark.parametrize(
    ("status", "code", "expect_exit", "retry_after"),
    [
        (403, "Authorization_RequestDenied", 3, None),
        (429, "activityLimitReached", 4, 2),
        (503, "ServiceUnavailable", 4, None),
    ],
)
def test_resolve_fails_closed_on_403_429_5xx(
    status: int,
    code: str,
    expect_exit: int,
    retry_after: int | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sleeps: list[float] = []

    def getter(_url: str) -> dict[str, Any]:
        body: dict[str, Any] = {"status": status, "error_code": code}
        if retry_after is not None:
            body["retry_after"] = retry_after
        return body

    upn = "solo@contoso.example"
    exit_code, (out, _err) = _run(
        f"{upn}\n",
        getter=getter,
        sleep=lambda s: sleeps.append(s),
        capsys=capsys,
    )
    assert exit_code == expect_exit
    assert not out.startswith("A365_ALLOWED_USERS=")
    if status == 429:
        assert sleeps == [2, 2, 2]


def test_resolve_never_logs_entry_or_graph_body(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = CANARY

    def embed(msg: str) -> str:
        return f"{msg} {sentinel} body-token"

    scenarios = [
        ({"status": 200, "error_code": "", "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}, 0),
        ({"status": 403, "error_code": "Authorization_RequestDenied"}, 3),
        ({"status": 404, "error_code": "Request_ResourceNotFound"}, None),
        ({"status": 429, "error_code": "activityLimitReached", "retry_after": 1}, 4),
        ({"status": 503, "error_code": embed("svc")}, 4),
        (
            {
                "status": 200,
                "error_code": "",
                "ids": [
                    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                ],
            },
            2,
        ),
    ]

    for body, expect in scenarios:
        if expect is None:
            getter = RoutingGetter(
                [
                    (lambda _u, _d: True, body),
                    (
                        lambda _u, _d: True,
                        {"status": 404, "error_code": "Request_ResourceNotFound"},
                    ),
                ]
            )
        else:
            getter = RoutingGetter([(lambda _u, _d: True, body)])
        caplog.clear()
        code, (out, err) = _run(f"{sentinel}\n", getter=getter, capsys=capsys, caplog=caplog)
        if expect is not None:
            assert code == expect
        haystacks = out + err + caplog.text
        for banned in (sentinel, CANARY_LOCAL, CANARY_QUOTED, "body-token", "example.test"):
            assert banned not in haystacks


def test_verify_reports_stale_object_id_by_index(capsys: pytest.CaptureFixture[str]) -> None:
    fresh = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    stale = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def getter(url: str) -> dict[str, Any]:
        if stale in url:
            return {"status": 404, "error_code": "Request_ResourceNotFound"}
        return {"status": 200, "error_code": "", "id": fresh}

    code, (out, err) = _run(f"{fresh}\n{stale}\n", getter=getter, verify=True, capsys=capsys)
    assert code == 2
    assert out == ""
    assert "index=2" in err
    assert stale not in err


def test_user_lookup_url_guard_message_has_no_path() -> None:
    with pytest.raises(EntraStateError) as exc:
        assert_user_lookup_url("https://graph.microsoft.com/v1.0/groups?$select=id")
    message = str(exc.value)
    assert "/v1.0/" not in message
    assert "graph.microsoft.com" not in message
    assert "groups" not in message