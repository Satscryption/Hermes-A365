"""Entra registration reads and the reconcile decision (#127).

These tests never talk to Graph. They cover the create path, an owned
re-run, and the states that must fail closed before any mutation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

import pytest

from hermes_a365.entra_state import (
    GRAPH_READ_PERMISSION_HINT,
    EntraStateError,
    GraphApp,
    RegistrationSnapshot,
    _app_by_id,
    _confirm_blueprint,
    applications_by_display_name_url,
    assert_registration_read_url,
    blueprint_display_name,
    confirmed_created_app_id,
    decide_registration,
    graph_get_from_mutator,
    identity_display_name,
    load_anchor_app_id,
    read_registration_snapshot,
    record_ownership_anchor,
    redact,
    registration_slug,
)
from hermes_a365.register import RegisterError, RegisterInputs, apply_register_plan

APP = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OBJ = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SP = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OTHER = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OTHER_OBJ = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
TENANT = "aaaa1111-0000-0000-0000-000000000001"
SECRET = "super-secret-client-value"
OBS_SP = "11111111-1111-1111-1111-111111111111"
MCP_SP = "22222222-2222-2222-2222-222222222222"
GRAPH_SP = "33333333-3333-3333-3333-333333333333"
OBS_APP = "9b975845-388f-4429-889e-eab1ef63949c"
MCP_APP = "99999999-9999-9999-9999-999999999999"
GRAPH_APP = "00000003-0000-0000-c000-000000000000"
ROLE = "8f71190c-00c8-461d-a63b-f74abde9ba52"


def _app(
    name: str,
    *,
    app_id: str = APP,
    object_id: str = OBJ,
    audience: str = "AzureADMultipleOrgs",
) -> dict[str, str]:
    return {
        "id": object_id,
        "appId": app_id,
        "displayName": name,
        "signInAudience": audience,
    }


class Router:
    def __init__(self, responses: list[tuple[str, dict[str, Any] | Exception]]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict[str, Any] | None:
        assert_registration_read_url(url)
        self.urls.append(url)
        for needle, body in self.responses:
            if needle in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(f"unexpected Graph URL {url}")


def _owned_responses(
    agent: str = "inbox-helper",
    *,
    audience: str = "AzureADMultipleOrgs",
    cast: dict[str, Any] | None = None,
    principals: dict[str, Any] | None = None,
    roles: dict[str, Any] | None = None,
    grants: dict[str, Any] | None = None,
    resources: dict[str, dict[str, Any]] | None = None,
    extra_apps: list[dict[str, Any]] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    blueprint = _app(blueprint_display_name(agent), audience=audience)
    rows = [blueprint, *(extra_apps or [])]
    role_body = (
        roles
        if roles is not None
        else {"value": [{"id": ROLE, "resourceId": OBS_SP, "appRoleId": ROLE}]}
    )
    grant_body = grants if grants is not None else {"value": [{"id": MCP_SP, "resourceId": MCP_SP}]}
    resource_map = (
        resources
        if resources is not None
        else {
            OBS_SP: {"appId": OBS_APP},
            MCP_SP: {"appId": MCP_APP},
        }
    )
    pairs: list[tuple[str, dict[str, Any]]] = [
        ("/beta/applications?", {"value": rows}),
        ("applications(appId=", blueprint),
        (
            "agentIdentityBlueprint",
            cast if cast is not None else {**blueprint, "uniqueName": registration_slug(agent)},
        ),
        (
            "servicePrincipals?",
            principals if principals is not None else {"value": [{"id": SP, "appId": APP}]},
        ),
        ("appRoleAssignments", role_body),
        ("oauth2PermissionGrants", grant_body),
    ]
    for object_id, body in resource_map.items():
        pairs.append((f"/servicePrincipals/{object_id}?", body))
    return pairs


def _write_anchor(home: Path, agent: str, app_id: str) -> Path:
    from hermes_a365._common import slugify

    path = home / "agents" / slugify(agent) / ".env"
    path.parent.mkdir(parents=True)
    path.write_text(f"A365_APP_ID={app_id}\nA365_BF_CLIENT_SECRET={SECRET}\n")
    return path


def _snapshot(home: Path, agent: str, router: Router) -> RegistrationSnapshot:
    anchor = load_anchor_app_id(home, agent)
    return read_registration_snapshot(router, agent_name=agent, anchor_app_id=anchor)


class TestLeastPrivilegeUrls:
    def test_display_name_query_selects_only_identity_fields(self) -> None:
        url = applications_by_display_name_url("Inbox Helper Blueprint", "Inbox Helper Identity")
        assert_registration_read_url(url)
        assert "passwordCredentials" not in url
        assert "keyCredentials" not in url
        assert "$select=id,appId,displayName,signInAudience" in url
        query = parse_qs(url.split("?", 1)[1])
        filt = unquote(query["$filter"][0])
        assert "displayName eq 'Inbox Helper Blueprint'" in filt
        assert "displayName eq 'Inbox Helper Identity'" in filt

    def test_quote_in_display_name_is_odata_escaped(self) -> None:
        url = applications_by_display_name_url("O'Brien Blueprint")
        filt = unquote(parse_qs(url.split("?", 1)[1])["$filter"][0])
        assert "O''Brien Blueprint" in filt
        assert_registration_read_url(url)

    def test_control_character_is_rejected_before_a_query(self) -> None:
        with pytest.raises(EntraStateError, match="control character"):
            applications_by_display_name_url("bad\nname")

    @pytest.mark.parametrize(
        "url",
        [
            "http://graph.microsoft.com/v1.0/applications?$select=id",
            "https://evil.example/beta/applications?$select=id",
            "https://graph.microsoft.com/beta/applications?$select=passwordCredentials",
            "https://graph.microsoft.com/v1.0/applications/anything?$select=id,appId",
            "https://graph.microsoft.com/beta/applications",
        ],
    )
    def test_rejects_wide_or_off_host_reads(self, url: str) -> None:
        with pytest.raises(EntraStateError):
            assert_registration_read_url(url)

    def test_redact_strips_tokens(self) -> None:
        raw = (
            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature "
            '{"access_token":"secret-token","password":"hunter2"}'
        )
        cleaned = redact(raw)
        assert "eyJ" not in cleaned
        assert "secret-token" not in cleaned
        assert "hunter2" not in cleaned
        assert "[redacted]" in cleaned


class TestReadAndDecide:
    def test_empty_directory_is_a_create(self, tmp_path: Path) -> None:
        router = Router([("/beta/applications?", {"value": []})])
        decision = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert decision.action == "create"
        assert decision.step_names == ("blueprint", "permissions-mcp", "permissions-bot")
        assert len(router.urls) == 1
        assert "servicePrincipals" not in router.urls[0]

    def test_owned_match_with_grants_is_a_noop(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        router = Router(_owned_responses())
        decision = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert decision.action == "noop"
        assert decision.step_names == ()
        assert decision.app_plan is not None and decision.app_plan.action == "noop"
        assert decision.blueprint_plan is not None and decision.blueprint_plan.action == "noop"
        assert all("$select=" in url for url in router.urls)
        assert not any("password" in url.lower() for url in router.urls)

    def test_owned_match_without_grants_runs_permissions_only(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        router = Router(_owned_responses(roles={"value": []}, grants={"value": []}))
        decision = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert decision.action == "repair"
        assert decision.step_names == ("permissions-mcp", "permissions-bot")
        assert "does not count" in decision.messages[0]

    def test_graph_admin_consent_does_not_skip_permission_steps(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        decision = decide_registration(
            _snapshot(
                tmp_path,
                "inbox-helper",
                Router(
                    _owned_responses(
                        roles={"value": []},
                        grants={"value": [{"id": "graph-grant", "resourceId": GRAPH_SP}]},
                        resources={GRAPH_SP: {"appId": GRAPH_APP}},
                    )
                ),
            )
        )
        assert decision.action == "repair"
        assert decision.step_names == ("permissions-mcp", "permissions-bot")
        assert "setup blueprint will not run" in decision.messages[0]

    def test_mcp_marker_without_bot_runs_only_bot(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        decision = decide_registration(
            _snapshot(
                tmp_path,
                "inbox-helper",
                Router(
                    _owned_responses(
                        roles={"value": []},
                        grants={"value": [{"id": "mcp-grant", "resourceId": MCP_SP}]},
                        resources={MCP_SP: {"appId": MCP_APP}},
                    )
                ),
            )
        )
        assert decision.step_names == ("permissions-bot",)

    def test_bot_marker_without_mcp_runs_only_mcp(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        decision = decide_registration(
            _snapshot(
                tmp_path,
                "inbox-helper",
                Router(
                    _owned_responses(
                        roles={"value": [{"id": ROLE, "resourceId": OBS_SP, "appRoleId": ROLE}]},
                        grants={"value": [{"id": "graph-grant", "resourceId": GRAPH_SP}]},
                        resources={
                            OBS_SP: {"appId": OBS_APP},
                            GRAPH_SP: {"appId": GRAPH_APP},
                        },
                    )
                ),
            )
        )
        assert decision.step_names == ("permissions-mcp",)

    def test_unique_name_drift_aborts_even_when_display_names_match(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        cast = _app(blueprint_display_name("inbox-helper"))
        cast["uniqueName"] = "someone-else"
        decision = decide_registration(
            _snapshot(tmp_path, "inbox-helper", Router(_owned_responses(cast=cast)))
        )
        assert decision.action == "abort"
        assert decision.step_names == ()
        assert "slug" in (decision.reason or "")

    def test_missing_unique_name_is_not_derived_from_the_display_name(self, tmp_path: Path) -> None:
        from hermes_a365._common import slugify

        _write_anchor(tmp_path, "inbox-helper", APP)
        decision = decide_registration(
            _snapshot(
                tmp_path,
                "inbox-helper",
                Router(_owned_responses(cast=_app(blueprint_display_name("inbox-helper")))),
            )
        )
        assert decision.action == "noop"
        assert decision.blueprint_plan is not None
        actual = decision.blueprint_plan.actual
        assert actual is not None
        compared = actual["agentIdentity"]["slug"]
        assert compared == registration_slug("inbox-helper")
        assert compared != slugify(blueprint_display_name("inbox-helper"))

    def test_unclassified_grant_fails_closed(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        denied = {
            "error": {
                "code": "Authorization_RequestDenied",
                "message": "need Directory.Read.All",
            }
        }
        decision = decide_registration(
            _snapshot(
                tmp_path,
                "inbox-helper",
                Router(
                    _owned_responses(
                        roles={"value": []},
                        grants={"value": [{"id": "graph-grant", "resourceId": GRAPH_SP}]},
                        resources={GRAPH_SP: denied},
                    )
                ),
            )
        )
        assert decision.action == "abort"
        assert decision.step_names == ()
        assert "Authorization_RequestDenied" in (decision.reason or "")
        assert "will run" not in (decision.reason or "")

    def test_update_permissions_regrants_without_rerunning_blueprint(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        decision = decide_registration(
            _snapshot(tmp_path, "inbox-helper", Router(_owned_responses())),
            update_permissions=True,
        )
        assert decision.step_names == ("permissions-mcp", "permissions-bot")
        assert "setup blueprint will not run" in decision.messages[0]

    def test_missing_service_principal_fails_closed_unless_flag(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        snapshot = _snapshot(
            tmp_path,
            "inbox-helper",
            Router(_owned_responses(principals={"value": []})),
        )
        refused = decide_registration(snapshot)
        assert refused.action == "abort"
        assert refused.step_names == ()
        assert "--update-permissions" in (refused.reason or "")
        repaired = decide_registration(snapshot, update_permissions=True)
        assert repaired.action == "repair"
        assert repaired.step_names[0] == "blueprint"
        assert "mint a client secret" in repaired.messages[0]

    def test_unowned_display_name_is_not_adopted(self, tmp_path: Path) -> None:
        name = blueprint_display_name("inbox-helper")
        router = Router([("/beta/applications?", {"value": [_app(name)]})])
        decision = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert decision.action == "abort"
        assert "no per-agent A365_APP_ID" in (decision.reason or "")
        assert APP in (decision.reason or "")
        assert len(router.urls) == 1

    def test_update_permissions_does_not_bypass_unowned(self, tmp_path: Path) -> None:
        name = blueprint_display_name("inbox-helper")
        router = Router([("/beta/applications?", {"value": [_app(name)]})])
        decision = decide_registration(
            _snapshot(tmp_path, "inbox-helper", router),
            update_permissions=True,
        )
        assert decision.action == "abort"
        assert decision.step_names == ()

    def test_anchor_points_at_a_different_app(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", OTHER)
        blueprint = _app(blueprint_display_name("inbox-helper"))
        other = _app("Other Blueprint", app_id=OTHER, object_id=OTHER_OBJ)
        router = Router(
            [
                ("/beta/applications?", {"value": [blueprint]}),
                ("applications(appId=", other),
            ]
        )
        decision = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert decision.action == "abort"
        assert "different" in (decision.reason or "").lower() or "disagree" in (
            decision.reason or ""
        )
        assert not any("appRoleAssignments" in url for url in router.urls)

    def test_anchor_name_mismatch_aborts_via_blueprint_reconcile(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        other = _app("Other Blueprint")
        other["uniqueName"] = "other"
        router = Router(
            [
                ("/beta/applications?", {"value": []}),
                ("applications(appId=", other),
                ("agentIdentityBlueprint", other),
            ]
        )
        decision = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert decision.action == "abort"
        assert decision.blueprint_plan is not None
        assert decision.blueprint_plan.action == "abort"
        assert "slug" in (decision.reason or "")
        assert decision.step_names == ()

    def test_multiple_display_names_abort(self, tmp_path: Path) -> None:
        name = blueprint_display_name("inbox-helper")
        router = Router(
            [
                (
                    "displayName",
                    {
                        "value": [
                            _app(name),
                            _app(name, app_id=OTHER, object_id=OTHER_OBJ),
                        ]
                    },
                )
            ]
        )
        decision = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert decision.action == "abort"
        assert "multiple" in (decision.reason or "")
        assert APP in (decision.reason or "") and OTHER in (decision.reason or "")

    def test_ordinary_app_is_a_tier_abort(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        router = Router(
            _owned_responses(
                cast={"error": {"code": "Request_ResourceNotFound", "message": "not a blueprint"}}
            )
        )
        decision = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert decision.action == "abort"
        assert decision.app_plan is not None and decision.app_plan.action == "abort"
        assert "tier" in (decision.reason or "")
        assert not any("appRoleAssignments" in url for url in router.urls)

    def test_audience_drift_is_not_patched(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        single = "AzureADMyOrg"
        router = Router(_owned_responses(audience=single))
        refused = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert refused.action == "abort"
        assert "sign-in audience" in (refused.reason or "")
        again = decide_registration(
            _snapshot(tmp_path, "inbox-helper", Router(_owned_responses(audience=single))),
            update_permissions=True,
        )
        assert again.action == "abort"

    def test_identity_name_blocks_create(self, tmp_path: Path) -> None:
        identity = _app(identity_display_name("inbox-helper"), app_id=OTHER, object_id=OTHER_OBJ)
        router = Router([("/beta/applications?", {"value": [identity]})])
        decision = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert decision.action == "abort"
        assert "Identity" in (decision.reason or "")

    def test_sibling_identity_is_reported_and_not_modified(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        identity = _app(identity_display_name("inbox-helper"), app_id=OTHER, object_id=OTHER_OBJ)
        router = Router(_owned_responses(extra_apps=[identity]))
        decision = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert decision.action == "noop"
        assert OTHER in decision.messages[0]
        assert "not modified" in decision.messages[0]

    def test_missing_anchor_app_does_not_create_a_replacement(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        router = Router(
            [
                ("/beta/applications?", {"value": []}),
                (
                    "applications(appId=",
                    {"error": {"code": "Request_ResourceNotFound", "message": "gone"}},
                ),
            ]
        )
        decision = decide_registration(_snapshot(tmp_path, "inbox-helper", router))
        assert decision.action == "abort"
        assert "not found" in (decision.reason or "")
        assert "another" in (decision.reason or "")

    def test_non_guid_anchor_fails_closed(self, tmp_path: Path) -> None:
        path = _write_anchor(tmp_path, "inbox-helper", APP)
        path.write_text("A365_APP_ID=not-a-guid\n")
        with pytest.raises(EntraStateError, match="not a GUID"):
            load_anchor_app_id(tmp_path, "inbox-helper")

    def test_symlink_anchor_is_not_read(self, tmp_path: Path) -> None:
        from hermes_a365._common import slugify

        real = tmp_path / "real.env"
        real.write_text(f"A365_APP_ID={APP}\n")
        link_dir = tmp_path / "agents" / slugify("inbox-helper")
        link_dir.mkdir(parents=True)
        (link_dir / ".env").symlink_to(real)
        with pytest.raises(EntraStateError, match="symlink"):
            load_anchor_app_id(tmp_path, "inbox-helper")

    def test_dangling_symlink_anchor_fails_closed(self, tmp_path: Path) -> None:
        from hermes_a365._common import slugify

        link_dir = tmp_path / "agents" / slugify("inbox-helper")
        link_dir.mkdir(parents=True)
        (link_dir / ".env").symlink_to(tmp_path / "missing.env")
        with pytest.raises(EntraStateError, match="symlink"):
            load_anchor_app_id(tmp_path, "inbox-helper")

    def test_symlinked_agent_directory_fails_closed(self, tmp_path: Path) -> None:
        from hermes_a365._common import slugify

        real_dir = tmp_path / "elsewhere"
        real_dir.mkdir()
        (real_dir / ".env").write_text(f"A365_APP_ID={APP}\n")
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / slugify("inbox-helper")).symlink_to(real_dir, target_is_directory=True)
        with pytest.raises(EntraStateError, match="symlink"):
            load_anchor_app_id(tmp_path, "inbox-helper")

    def test_graph_403_names_the_read_permission(self, tmp_path: Path) -> None:
        body = {
            "error": {
                "code": "Authorization_RequestDenied",
                "message": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaa.bbb denied",
            }
        }
        with pytest.raises(EntraStateError, match=r"Application\.Read\.All") as excinfo:
            _snapshot(tmp_path, "inbox-helper", Router([("/beta/applications?", body)]))
        assert "eyJ" not in str(excinfo.value)
        assert GRAPH_READ_PERMISSION_HINT.split(".")[0] in str(excinfo.value)

    def test_unreadable_body_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(EntraStateError, match="not trustworthy"):
            _snapshot(tmp_path, "inbox-helper", Router([("/beta/applications?", None)]))  # type: ignore[list-item]

    def test_paged_application_result_fails_closed(self, tmp_path: Path) -> None:
        body = {
            "value": [],
            "@odata.nextLink": "https://graph.microsoft.com/beta/applications?$skiptoken=1",
        }
        with pytest.raises(EntraStateError, match="paged"):
            _snapshot(tmp_path, "inbox-helper", Router([("/beta/applications?", body)]))

    def test_count_mismatch_fails_closed(self, tmp_path: Path) -> None:
        body = {"value": [_app(blueprint_display_name("inbox-helper"))], "@odata.count": 2}
        with pytest.raises(EntraStateError, match=r"odata\.count"):
            _snapshot(tmp_path, "inbox-helper", Router([("/beta/applications?", body)]))

    def test_grant_read_403_does_not_look_like_absent(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        denied = {
            "error": {
                "code": "Authorization_RequestDenied",
                "message": "need DelegatedPermissionGrant.Read.All",
            }
        }
        snapshot = _snapshot(
            tmp_path,
            "inbox-helper",
            Router(_owned_responses(roles=denied)),
        )
        decision = decide_registration(snapshot)
        assert decision.action == "abort"
        assert decision.step_names == ()
        assert "Application.Read.All" in (decision.reason or "") or "DelegatedPermissionGrant" in (
            decision.reason or ""
        )

    def test_ambiguous_service_principals_abort(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        principals = {
            "value": [
                {"id": SP, "appId": APP},
                {"id": OTHER, "appId": APP},
            ]
        }
        decision = decide_registration(
            _snapshot(
                tmp_path,
                "inbox-helper",
                Router(_owned_responses(principals=principals)),
            )
        )
        assert decision.action == "abort"
        assert "more than one service principal" in (decision.reason or "")

    def test_surprise_display_name_row_fails_closed(self, tmp_path: Path) -> None:
        router = Router([("/beta/applications?", {"value": [_app("Somebody Else")]})])
        with pytest.raises(EntraStateError, match="not requested"):
            _snapshot(tmp_path, "inbox-helper", router)

    def test_cast_disagreement_fails_closed(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        cast = _app(blueprint_display_name("inbox-helper"), audience="AzureADMyOrg")
        with pytest.raises(EntraStateError, match="disagreed"):
            _snapshot(tmp_path, "inbox-helper", Router(_owned_responses(cast=cast)))


class TestAnchorRecord:
    def test_records_only_the_app_id_and_preserves_existing_secret(self, tmp_path: Path) -> None:
        from hermes_a365._common import slugify

        path = tmp_path / "agents" / slugify("inbox-helper") / ".env"
        path.parent.mkdir(parents=True)
        path.write_text(f"A365_BF_CLIENT_SECRET={SECRET}\n")
        written = record_ownership_anchor(tmp_path, "inbox-helper", APP.upper())
        assert written == path
        text = path.read_text()
        assert f"A365_APP_ID={APP}" in text
        assert SECRET in text  # preserved, not dropped or logged by the writer
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_refuses_to_overwrite_a_different_anchor(self, tmp_path: Path) -> None:
        path = _write_anchor(tmp_path, "inbox-helper", APP)
        before = path.read_text()
        with pytest.raises(EntraStateError, match="overwrite"):
            record_ownership_anchor(tmp_path, "inbox-helper", OTHER)
        assert path.read_text() == before

    def test_confirm_rejects_unverified_id_without_echoing_a_secret(self, tmp_path: Path) -> None:
        generated = tmp_path / "a365.generated.config.json"
        generated.write_text(
            json.dumps(
                {
                    "agentBlueprintId": APP,
                    "agentBlueprintClientSecret": SECRET,
                }
            )
        )
        router = Router(
            [
                (
                    "applications(appId=",
                    {"error": {"code": "Request_ResourceNotFound", "message": SECRET}},
                )
            ]
        )
        with pytest.raises(EntraStateError, match="did not confirm") as excinfo:
            confirmed_created_app_id(
                router,
                generated,
                expected_display_name=blueprint_display_name("inbox-helper"),
            )
        assert SECRET not in str(excinfo.value)

    def test_confirm_rejects_a_non_blueprint(self, tmp_path: Path) -> None:
        generated = tmp_path / "a365.generated.config.json"
        generated.write_text(
            json.dumps({"agentBlueprintId": APP, "agentBlueprintClientSecret": SECRET})
        )
        blueprint = _app(blueprint_display_name("inbox-helper"))
        router = Router(
            [
                ("applications(appId=", blueprint),
                (
                    "agentIdentityBlueprint",
                    {"error": {"code": "Request_ResourceNotFound", "message": "no"}},
                ),
            ]
        )
        with pytest.raises(EntraStateError, match="not an agent identity blueprint"):
            confirmed_created_app_id(
                router,
                generated,
                expected_display_name=blueprint_display_name("inbox-helper"),
            )

    def test_missing_generated_id_does_not_invent_an_anchor(self, tmp_path: Path) -> None:
        generated = tmp_path / "a365.generated.config.json"
        generated.write_text(json.dumps({"agentBlueprintClientSecret": SECRET}))
        router = Router([])
        assert (
            confirmed_created_app_id(
                router,
                generated,
                expected_display_name=blueprint_display_name("inbox-helper"),
            )
            is None
        )
        assert router.urls == []


def _tenant_mutator():
    from hermes_a365.mutator import RunResult

    class Recording:
        available = True

        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(self, argv: list[str], **_: Any) -> RunResult:
            self.calls.append(list(argv))
            if argv[:3] == ["az", "account", "show"]:
                return RunResult(
                    argv=argv,
                    returncode=0,
                    stdout=json.dumps({"tenantId": TENANT}),
                    stderr="",
                )
            return RunResult(argv=argv, returncode=0, stdout="ok", stderr="")

    return Recording()


def _apply(tmp_path: Path, router: Router, *, agent: str = "inbox-helper", **kwargs: Any):
    from hermes_a365.register import build_register_plan

    plan = build_register_plan(RegisterInputs(agent_name=agent, tenant_id=TENANT))
    return apply_register_plan(
        plan,
        mutator=kwargs.pop("mutator"),
        hermes_home=tmp_path,
        graph_get=router,
        config_path=kwargs.pop("config_path", None),
        generated_config_path=kwargs.pop("generated_config_path", tmp_path / "missing.json"),
        **kwargs,
    )


class TestApplyUsesTheDecision:
    def test_noop_does_not_call_a365_or_print_the_secret(self, tmp_path: Path) -> None:
        from hermes_a365.mutator import RunResult

        class Recording:
            available = True
            calls: list[list[str]]

            def __init__(self) -> None:
                self.calls = []

            def run(self, argv: list[str], **_: Any) -> RunResult:
                self.calls.append(list(argv))
                if argv[:3] == ["az", "account", "show"]:
                    return RunResult(
                        argv=argv,
                        returncode=0,
                        stdout=json.dumps({"tenantId": TENANT}),
                        stderr="",
                    )
                raise AssertionError(argv)

        _write_anchor(tmp_path, "inbox-helper", APP)
        mutator = Recording()
        config = tmp_path / "a365.config.json"
        config.write_text("{}\n")
        result = _apply(tmp_path, Router(_owned_responses()), mutator=mutator, config_path=config)
        assert result.completed == []
        assert not any(call[0] == "a365" for call in mutator.calls)
        assert SECRET not in "\n".join(result.messages)
        on_disk = json.loads(config.read_text())
        assert on_disk["agentBlueprintDisplayName"] == "inbox-helper Blueprint"

    def test_mcp_grants_without_bot_resume_only_the_bot_step(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        mutator = _tenant_mutator()
        result = _apply(
            tmp_path,
            Router(
                _owned_responses(
                    roles={"value": []},
                    grants={"value": [{"id": "mcp-grant", "resourceId": MCP_SP}]},
                    resources={MCP_SP: {"appId": MCP_APP}},
                )
            ),
            mutator=mutator,
        )
        assert result.completed == ["permissions-bot"]
        assert not any(call[:3] == ["a365", "setup", "blueprint"] for call in mutator.calls)

    def test_graph_admin_consent_only_resumes_both_permission_steps(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        mutator = _tenant_mutator()
        result = _apply(
            tmp_path,
            Router(
                _owned_responses(
                    roles={"value": []},
                    grants={"value": [{"id": "graph-grant", "resourceId": GRAPH_SP}]},
                    resources={GRAPH_SP: {"appId": GRAPH_APP}},
                )
            ),
            mutator=mutator,
        )
        assert result.completed == ["permissions-mcp", "permissions-bot"]
        assert not any(call[:3] == ["a365", "setup", "blueprint"] for call in mutator.calls)

    def test_complete_registration_stays_a_noop(self, tmp_path: Path) -> None:
        _write_anchor(tmp_path, "inbox-helper", APP)
        mutator = _tenant_mutator()
        result = _apply(tmp_path, Router(_owned_responses()), mutator=mutator)
        assert result.completed == []
        assert not any(call[0] == "a365" for call in mutator.calls)

    def test_abort_leaves_config_and_skips_a365(self, tmp_path: Path) -> None:
        from hermes_a365.mutator import RunResult

        class Recording:
            available = True

            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def run(self, argv: list[str], **_: Any) -> RunResult:
                self.calls.append(list(argv))
                if argv[:3] == ["az", "account", "show"]:
                    return RunResult(
                        argv=argv, returncode=0, stdout=json.dumps({"tenantId": TENANT}), stderr=""
                    )
                raise AssertionError(argv)

        mutator = Recording()
        config = tmp_path / "a365.config.json"
        config.write_text('{"tenantId":"original"}\n')
        name = blueprint_display_name("inbox-helper")
        with pytest.raises(RegisterError, match="no per-agent A365_APP_ID"):
            _apply(
                tmp_path,
                Router([("/beta/applications?", {"value": [_app(name)]})]),
                mutator=mutator,
                config_path=config,
            )
        assert json.loads(config.read_text())["tenantId"] == "original"
        assert not any(call[0] == "a365" for call in mutator.calls)

    def test_unconfirmed_create_stops_before_permissions(self, tmp_path: Path) -> None:
        from hermes_a365.mutator import RunResult

        class Recording:
            available = True

            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def run(self, argv: list[str], **_: Any) -> RunResult:
                self.calls.append(list(argv))
                if argv[:3] == ["az", "account", "show"]:
                    return RunResult(
                        argv=argv, returncode=0, stdout=json.dumps({"tenantId": TENANT}), stderr=""
                    )
                return RunResult(argv=argv, returncode=0, stdout="ok", stderr="")

        generated = tmp_path / "a365.generated.config.json"
        generated.write_text(
            json.dumps({"agentBlueprintId": APP, "agentBlueprintClientSecret": SECRET})
        )
        router = Router(
            [
                ("/beta/applications?", {"value": []}),
                (
                    "applications(appId=",
                    {"error": {"code": "Request_ResourceNotFound", "message": "lag"}},
                ),
            ]
        )
        mutator = Recording()
        with pytest.raises(RegisterError, match="did not confirm") as excinfo:
            _apply(tmp_path, router, mutator=mutator, generated_config_path=generated)
        assert SECRET not in str(excinfo.value)
        a365 = [call for call in mutator.calls if call[0] == "a365"]
        assert [call[2] for call in a365] == ["blueprint"]
        assert not (tmp_path / "agents").exists()

    def test_confirmed_create_records_the_anchor_without_the_secret(self, tmp_path: Path) -> None:
        from hermes_a365._common import slugify
        from hermes_a365.mutator import RunResult

        class Recording:
            available = True

            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def run(self, argv: list[str], **_: Any) -> RunResult:
                self.calls.append(list(argv))
                if argv[:3] == ["az", "account", "show"]:
                    return RunResult(
                        argv=argv, returncode=0, stdout=json.dumps({"tenantId": TENANT}), stderr=""
                    )
                return RunResult(argv=argv, returncode=0, stdout="ok", stderr="")

        generated = tmp_path / "a365.generated.config.json"
        generated.write_text(
            json.dumps({"agentBlueprintId": APP, "agentBlueprintClientSecret": SECRET})
        )
        blueprint = _app(blueprint_display_name("inbox-helper"))
        router = Router(
            [
                ("/beta/applications?", {"value": []}),
                ("applications(appId=", blueprint),
                ("agentIdentityBlueprint", blueprint),
            ]
        )
        result = _apply(
            tmp_path,
            router,
            mutator=Recording(),
            generated_config_path=generated,
        )
        assert result.completed == ["blueprint", "permissions-mcp", "permissions-bot"]
        env = tmp_path / "agents" / slugify("inbox-helper") / ".env"
        text = env.read_text()
        assert text == f"A365_APP_ID={APP}\n"
        assert SECRET not in text
        assert SECRET not in "\n".join(result.messages)
        assert (env.stat().st_mode & 0o777) == 0o600


def test_graph_get_is_sensitive_and_redacts_the_error_body(
    capfd: pytest.CaptureFixture[str],
) -> None:
    from hermes_a365.mutator import CliInvocationError

    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaa.bbb"

    class Spy:
        available = True

        def run(self, argv: list[str], **kwargs: Any) -> Any:
            assert kwargs.get("sensitive") is True
            raise CliInvocationError(
                argv,
                1,
                "<output suppressed>",
                sensitive=True,
                graph_error=(
                    "Authorization_RequestDenied",
                    redact(f"Bearer {token}"),
                ),
            )

    getter = graph_get_from_mutator(Spy())  # type: ignore[arg-type]
    with pytest.raises(EntraStateError) as excinfo:
        read_registration_snapshot(getter, agent_name="inbox-helper", anchor_app_id="")
    assert token not in str(excinfo.value)
    assert "eyJ" not in str(excinfo.value)
    out, err = capfd.readouterr()
    assert token not in out + err


def _install_az_rest_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, response_body: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    az = bin_dir / "az"
    az.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2 $3" = "rest --method GET" ]; then\n'
        f"  printf '%s\\n' '{response_body}'\n"
        "  exit 1\n"
        "fi\n"
        "echo unexpected az >&2\n"
        "exit 1\n"
    )
    az.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def _assert_no_secret_in_exception_chain(exc: BaseException, marker: str) -> None:
    assert marker not in str(exc)
    assert all(marker not in str(arg) for arg in exc.args)
    for key, value in vars(exc).items():
        if key == "argv" or value is None:
            continue
        assert marker not in str(value), key
    if exc.__cause__ is not None:
        _assert_no_secret_in_exception_chain(exc.__cause__, marker)
    if exc.__context__ is not None and exc.__context__ is not exc.__cause__:
        _assert_no_secret_in_exception_chain(exc.__context__, marker)


class TestSensitiveGraphViaMutator:
    def test_not_found_returns_none_without_raw_body(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from hermes_a365.mutator import A365CliMutator

        marker = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaa.bbb"
        body = json.dumps(
            {
                "error": {
                    "code": "Request_ResourceNotFound",
                    "message": f"Bearer {marker}",
                }
            }
        )
        _install_az_rest_shim(tmp_path, monkeypatch, response_body=body)
        monkeypatch.setenv("DOTNET_ROOT", "/nonexistent")
        mutator = A365CliMutator()
        mutator.available = True
        getter = graph_get_from_mutator(mutator)
        assert _app_by_id(getter, APP) is None

    def test_sensitive_mutator_graph_error_has_no_raw_body(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from hermes_a365.mutator import A365CliMutator, CliInvocationError

        marker = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaa.bbb"
        body = json.dumps(
            {
                "error": {
                    "code": "Request_ResourceNotFound",
                    "message": f"Bearer {marker}",
                }
            }
        )
        _install_az_rest_shim(tmp_path, monkeypatch, response_body=body)
        monkeypatch.setenv("DOTNET_ROOT", "/nonexistent")
        mutator = A365CliMutator()
        mutator.available = True
        with pytest.raises(CliInvocationError) as excinfo:
            mutator.run(
                ["az", "rest", "--method", "GET", "--uri", "https://graph.microsoft.test/v1.0/x"],
                sensitive=True,
            )
        _assert_no_secret_in_exception_chain(excinfo.value, marker)
        assert excinfo.value.graph_error is not None
        assert excinfo.value.graph_error[0] == "Request_ResourceNotFound"
        assert marker not in excinfo.value.graph_error[1]
        assert marker not in repr(excinfo.value)
        assert excinfo.value.captured_output is None

    def test_cast_not_found_records_non_blueprint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from hermes_a365.mutator import A365CliMutator

        app = GraphApp(
            object_id=OBJ,
            app_id=APP,
            display_name="n",
            sign_in_audience="AzureADMultipleOrgs",
            is_agent_blueprint=None,
        )
        body = json.dumps(
            {"error": {"code": "Request_ResourceNotFound", "message": "not a blueprint"}}
        )
        _install_az_rest_shim(tmp_path, monkeypatch, response_body=body)
        monkeypatch.setenv("DOTNET_ROOT", "/nonexistent")
        mutator = A365CliMutator()
        mutator.available = True
        getter = graph_get_from_mutator(mutator)
        result = _confirm_blueprint(getter, app)
        assert result.is_agent_blueprint is False


def test_graph_app_shape_is_the_fields_we_read() -> None:
    app = GraphApp(
        object_id=OBJ,
        app_id=APP,
        display_name="n",
        sign_in_audience="AzureADMultipleOrgs",
        is_agent_blueprint=True,
    )
    assert not hasattr(app, "passwordCredentials")
