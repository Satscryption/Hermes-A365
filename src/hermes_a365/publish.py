"""hermes a365 publish — wrap ``a365 publish``.

The GA CLI's ``publish`` does **two different things** depending on the
agent flavour:

- **AI Teammate** (``--aiteammate``): updates manifest IDs and emits a
  zip the operator uploads to the M365 Admin Centre. Channel
  deployment is operator-side after that.
- **Blueprint-only** (default): calls the Agent Instance Graph API
  (``POST /beta/agentRegistry/agentInstances``) to register the agent
  instance. No zip; nothing to upload. The
  resulting ``agentInstanceId`` lands in ``a365.generated.config.json``.

The 2026-05-05 walkthrough caught the wrapper rendering the Admin
Centre upload language for both flows, which misled operators in
blueprint-only mode (slice 18t / bug #14 fixes that — the plan and
post-apply messages now branch on ``aiteammate``).

Default mode is dry-run; ``--apply`` runs ``a365 publish`` for real.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
import uuid
from dataclasses import dataclass, field

from .mutator import AADSTSError, CliInvocationError, Mutator, RunResult, get_mutator

ADMIN_CENTRE_URL = "https://admin.microsoft.com/"

# Defensive parser: when the CLI emits a "Created package:" / "Wrote zip:" /
# similar line, grab the path. The exact wording isn't pinned in v1.1.171 yet,
# so we accept several phrasings.
_PACKAGE_PATH_RE = re.compile(
    r"(?:created package|wrote zip|package(?: created)?)[\s:]+"
    r"(?P<path>\"[^\r\n\"]+?\.zip\"|'[^\r\n']+?\.zip'|[^\r\n]+?\.zip)"
    r"(?:\s|$)",
    re.IGNORECASE,
)

# Microsoft a365 CLI >= 1.1.181 changed the publish flow: instead of
# emitting a packaged ``*.zip``, it extracts a manifest *template* to a
# directory and stops for customisation ("Extracted manifest templates to
# <dir>" / "Manifest updated: <dir>/manifest.json"). When no zip is found
# we package that directory ourselves so the CEA transform pipeline can
# run (older CLIs emitted the zip directly — both paths are supported).
_MANIFEST_DIR_RE = re.compile(
    r"Extracted manifest templates to\s+(?P<path>[^\r\n]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_MANIFEST_JSON_RE = re.compile(
    r"Manifest updated:\s+(?P<path>[^\r\n]+?manifest\.json)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Slice 18t (bug #14): blueprint-only flow registers an instance via
# Graph and prints "Agent instance registered: <guid>" — extract the
# id so the post-apply message is concrete.
_INSTANCE_ID_RE = re.compile(
    r"Agent instance registered:\s*([0-9a-fA-F-]{8,})",
)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class PublishInputs:
    agent_name: str
    tenant_id: str | None = None
    aiteammate: bool = False  # blueprint-only by default per CLI default
    copilot_chat: bool = False  # slice 19u-a (#24): Custom Engine Agent emit
    bot_id: str | None = None  # optional override for the Copilot Chat botId
    manifest_id: str | None = None  # "auto" or explicit Teams App Catalog id
    use_blueprint: bool = False  # blueprint-based non-DW flow (only with aiteammate=False)
    # #74 — Copilot Chat zero-state prompt starters; None → the default set.
    prompt_starters: tuple[dict[str, str], ...] | None = None
    verbose: bool = False

    def __post_init__(self) -> None:
        if not self.agent_name:
            raise ValueError("agent_name must be non-empty")
        if self.prompt_starters and not self.copilot_chat:
            raise ValueError("--prompt-starter is only meaningful with --copilot-chat")
        if self.use_blueprint and self.aiteammate:
            raise ValueError("--use-blueprint is only meaningful with --aiteammate false")
        if self.use_blueprint and self.copilot_chat:
            raise ValueError("--use-blueprint is incompatible with --copilot-chat")
        if self.manifest_id and not self.copilot_chat:
            raise ValueError("--manifest-id is only meaningful with --copilot-chat")
        if self.manifest_id and self.manifest_id != "auto":
            try:
                uuid.UUID(self.manifest_id)
            except ValueError as e:
                raise ValueError("--manifest-id must be 'auto' or a GUID") from e


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class PublishStep:
    argv: list[str]
    description: str


@dataclass
class PublishPlan:
    inputs: PublishInputs
    step: PublishStep

    def render_human(self) -> str:
        lines = [f"[plan] hermes a365 publish {self.inputs.agent_name}"]
        if self.inputs.tenant_id:
            lines.append(f"  tenant: {self.inputs.tenant_id}")
        else:
            lines.append("  tenant: (auto-detect from `az account show`)")
        if self.inputs.aiteammate and self.inputs.copilot_chat:
            flavour = "AI Teammate + Custom Engine Agent (both surfaces)"
        elif self.inputs.copilot_chat:
            flavour = "Custom Engine Agent (Copilot Chat)"
        elif self.inputs.aiteammate:
            flavour = "AI Teammate"
        else:
            flavour = "blueprint-only"
        lines.append(f"  flavour: {flavour}")
        # Slice 18t (bug #14) + 19u-a (#24): be explicit about what this
        # run will produce so operators know which admin centre to hit.
        if self.inputs.aiteammate and self.inputs.copilot_chat:
            lines.append(
                "  output:  AI Teammate zip (M365 Admin Centre) + "
                "Copilot Chat zip (Microsoft Admin Portal)"
            )
        elif self.inputs.copilot_chat:
            lines.append("  output:  Custom Engine Agent zip for Microsoft Admin Portal upload")
        elif self.inputs.aiteammate:
            lines.append("  output:  manifest zip for M365 Admin Centre upload")
        else:
            lines.append("  output:  Graph API instance registration (no zip)")
        if self.inputs.use_blueprint:
            lines.append("  flow:    blueprint-based non-DW (explicit)")
        if self.inputs.bot_id:
            lines.append(f"  bot-id:  {self.inputs.bot_id} (override)")
        if self.inputs.manifest_id:
            lines.append(f"  manifest-id: {self.inputs.manifest_id}")
        elif self.inputs.aiteammate and self.inputs.copilot_chat:
            lines.append("  manifest-id: auto (Copilot Chat catalog id)")
        lines.append(f"  step:    {self.step.description}")
        # shlex.join (slice 18p, bug #7) keeps multi-word values quoted
        # so the printed line is shell-pasteable verbatim.
        lines.append(f"           $ {shlex.join(self.step.argv)}")
        return "\n".join(lines)


def _build_argv(inputs: PublishInputs) -> list[str]:
    argv = ["a365", "publish", "--agent-name", inputs.agent_name]
    if inputs.tenant_id:
        argv.extend(["--tenant-id", inputs.tenant_id])
    # Slice 19u-a: the GA CLI only emits a starter zip when invoked with
    # ``--aiteammate``. The Copilot Chat flow post-processes that zip
    # into a Custom Engine Agent shape, so it must also invoke the CLI
    # in AI Teammate mode underneath.
    if inputs.aiteammate or inputs.copilot_chat:
        argv.append("--aiteammate")
    if inputs.use_blueprint:
        argv.append("--use-blueprint")
    if inputs.verbose:
        argv.append("--verbose")
    return argv


def _step_description(inputs: PublishInputs) -> str:
    if inputs.aiteammate and inputs.copilot_chat:
        return "package both AI Teammate + Custom Engine Agent manifests"
    if inputs.copilot_chat:
        return "package the Custom Engine Agent manifest for Microsoft Admin Portal upload"
    if inputs.aiteammate:
        return "package the agent manifest for M365 Admin Centre upload"
    return "register agent instance via Microsoft Graph (no zip emitted)"


def build_publish_plan(inputs: PublishInputs) -> PublishPlan:
    return PublishPlan(
        inputs=inputs,
        step=PublishStep(
            argv=_build_argv(inputs),
            description=_step_description(inputs),
        ),
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class PublishResult:
    plan: PublishPlan
    raw: RunResult
    package_path: str | None  # set on AI Teammate flow if a zip was produced
    instance_id: str | None  # set on blueprint-only flow if registration succeeded
    # Slice 19u-a (#24): Custom Engine Agent zip produced for Copilot Chat.
    copilot_chat_package_path: str | None = None
    copilot_chat_bot_id: str | None = None
    copilot_chat_manifest_id: str | None = None
    messages: list[str] = field(default_factory=list)


def _extract_package_path(output: str) -> str | None:
    """Best-effort grep for a `*.zip` path in the CLI's stdout/stderr."""
    match = _PACKAGE_PATH_RE.search(output)
    if not match:
        return None
    return match.group("path").strip("\"'")


def _extract_manifest_dir(output: str) -> str | None:
    """Best-effort grep for the extracted manifest *directory* (Microsoft
    a365 CLI >= 1.1.181 extracts a template + stops instead of emitting a
    zip). Prefers the explicit "Extracted manifest templates to <dir>" line;
    falls back to the parent of the "Manifest updated: <dir>/manifest.json"
    line."""
    from pathlib import Path

    match = _MANIFEST_DIR_RE.search(output)
    if match:
        return match.group("path").strip().strip("\"'")
    match = _MANIFEST_JSON_RE.search(output)
    if match:
        return str(Path(match.group("path").strip().strip("\"'")).parent)
    return None


def _zip_manifest_dir(manifest_dir: str) -> str | None:
    """Package an extracted manifest directory into a sibling ``.zip`` so the
    CEA transform pipeline (which operates on a zip) can run against the
    newer CLI's extract-and-stop output. Returns the zip path, or ``None``
    when the directory is missing or carries no ``manifest.json``. Files are
    written flat (the CLI extracts manifest.json + icons at the dir root)."""
    import zipfile
    from pathlib import Path

    d = Path(manifest_dir)
    if not d.is_dir() or not (d / "manifest.json").is_file():
        return None
    zip_path = d.with_name(d.name + ".zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(d.iterdir()):
                if f.is_file():
                    zf.write(f, arcname=f.name)
    except OSError:
        return None
    return str(zip_path)


# Slice 19r-c (round-8 walkthrough finding, 2026-05-11): the GA CLI
# emits manifests with ``name.short = "<agent-name> Blueprint"`` even
# when ``<agent-name>`` itself would push the total past 30 characters.
# M365 Admin Centre rejects upload at the manifest validation step
# without surfacing the schema-specific reason in its UI; operators
# see only a generic "Upload failed" toast. The CLI's "Customize
# before packaging" output flags this as a warning but still produces
# the zip.
#
# We post-process the emitted zip to bring ``name.short`` under 30
# chars. Strategy, in order:
#   1. If it ends with " Blueprint" — strip the suffix.
#   2. Else truncate at the last word boundary that fits in 30 chars.
# ``name.full`` is left untouched (it has a 100-char cap which the CLI
# emit reliably respects).
_NAME_SHORT_MAX = 30


def _truncate_name_short_to(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    words = value.split(" ")
    out_words: list[str] = []
    out_len = 0
    for w in words:
        candidate_len = out_len + len(w) + (1 if out_words else 0)
        if candidate_len > max_chars:
            break
        out_words.append(w)
        out_len = candidate_len
    if out_words:
        return " ".join(out_words)
    return value[:max_chars].rstrip()


def _truncate_name_short(value: str) -> str:
    """Return ``value`` shortened to ``<=30`` chars at a sensible boundary.

    Pure function so tests can exercise both branches without zip I/O.
    """
    if len(value) <= _NAME_SHORT_MAX:
        return value
    if value.endswith(" Blueprint"):
        stripped = value[: -len(" Blueprint")].rstrip()
        if 1 <= len(stripped) <= _NAME_SHORT_MAX:
            return stripped
    return _truncate_name_short_to(value, _NAME_SHORT_MAX)


def _with_name_short_suffix(value: str, suffix: str = " CC") -> str:
    """Append a suffix while preserving the 30-char manifest cap."""
    if not value:
        return value
    if value.endswith(suffix):
        return _truncate_name_short(value)
    base_max = _NAME_SHORT_MAX - len(suffix)
    base = _truncate_name_short_to(value, base_max).rstrip()
    if not base:
        return suffix.strip()[:_NAME_SHORT_MAX]
    return f"{base}{suffix}"


def _patch_manifest_name_short(zip_path: str) -> tuple[str, str] | None:
    """If ``manifest.json`` in *zip_path* has ``name.short`` > 30 chars,
    rewrite it in-place via a re-zip and return ``(old, new)``. Returns
    ``None`` when no patch was needed or when something prevented the
    patch (e.g. missing zip file). Best-effort: any I/O failure leaves
    the original zip untouched and reports ``None``.
    """
    import json
    import tempfile
    import zipfile
    from pathlib import Path

    zp = Path(zip_path)
    if not zp.is_file():
        return None

    try:
        with zipfile.ZipFile(zp, "r") as zf:
            names = zf.namelist()
            if "manifest.json" not in names:
                return None
            with zf.open("manifest.json") as fh:
                manifest = json.load(fh)
            other_files = {n: zf.read(n) for n in names if n != "manifest.json"}
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
        return None

    name_block = manifest.get("name") if isinstance(manifest.get("name"), dict) else None
    if not name_block:
        return None
    short = str(name_block.get("short") or "")
    if len(short) <= _NAME_SHORT_MAX:
        return None
    new_short = _truncate_name_short(short)
    if new_short == short or not new_short:
        return None

    name_block["short"] = new_short
    new_manifest = json.dumps(manifest, indent=2).encode("utf-8")

    # Re-zip via a temp file in the same dir, then atomic-rename. Keeps
    # the original on disk if anything fails mid-flight.
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(zp.parent), prefix=zp.name + ".", suffix=".tmp", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", new_manifest)
            for name, blob in other_files.items():
                zf.writestr(name, blob)
        tmp_path.replace(zp)
    except OSError:
        import contextlib

        with contextlib.suppress(Exception):
            tmp_path.unlink(missing_ok=True)  # type: ignore[name-defined]
        return None

    return (short, new_short)


# ---------------------------------------------------------------------------
# Slice 19u-a (#24): Custom Engine Agent manifest emit for Copilot Chat
# ---------------------------------------------------------------------------
#
# Microsoft surfaces two parallel manifest shapes from the same blueprint
# Entra app:
#
# - **AI Teammate** (``manifestVersion: "devPreview"``,
#   ``agenticUserTemplates`` block) — surfaces in Teams 1:1 "Built for
#   your org". Emitted by the GA CLI's ``a365 publish --aiteammate``.
# - **Custom Engine Agent** (``manifestVersion: "1.21"``+, ``bots`` +
#   ``copilotAgents.customEngineAgents`` blocks) — surfaces in M365
#   Copilot Chat's agents picker.
#
# The GA CLI doesn't ship a Copilot Chat emitter. We invoke it in AI
# Teammate mode (the side effect — Entra app + service principal —
# is the same bot identity for both surfaces) and post-process the
# emitted zip into the 1.21 shape.

# #74 — bumped 1.21 → 1.27: the minimum manifest version that adds the bot
# command-list command `type` (`basic`|`prompt`) + `prompt` fields (verified
# against Microsoft's live schema, v1.27 / May 2026). The rest of the CEA shape
# (bots + copilotAgents) is unchanged and remains valid at 1.27.
_COPILOT_CHAT_MANIFEST_VERSION = "1.27"
_COPILOT_CHAT_DEFAULT_SCOPES: tuple[str, ...] = ("copilot", "personal", "team")

# #74 — Copilot Chat zero-state prompt starters. Command-list commands of
# type:"prompt" (manifest 1.27+): `title` is the button label (≤128), `prompt` is
# the text sent when the user taps it (≤4000). `description` is optional in 1.27
# but drives the card's subtitle — Copilot Chat repeats the title when it is
# absent (walk-observed 2026-07-06), so we set it to the prompt (title + preview).
# Starters render as the CC zero-state and in the "View prompts" flyout, on the
# copilot + personal
# surfaces (team scope is the @mention command menu, not a zero-state surface, so
# it is deliberately excluded). Operator-supplied starters are capped.
_PROMPT_STARTER_MAX = 10
_PROMPT_STARTER_SCOPES: tuple[str, ...] = ("copilot", "personal")
_DEFAULT_PROMPT_STARTERS: tuple[dict[str, str], ...] = (
    {"title": "What can you do?", "prompt": "What can you help me with?"},
    {"title": "Get started", "prompt": "How do I get started with you?"},
    {"title": "Show an example", "prompt": "Show me an example of something you can do."},
)
_COPILOT_CHAT_ZIP_INFIX = ".copilot-chat"

# Wall-clock budget for ``a365 publish`` invoked under ``apply_publish_plan``.
# This is the only interactive call in the wrapper chain — when MSAL cannot
# silent-token (fresh shell, stale cache), ``a365`` falls back to device-code
# auth (browser open → sign-in → optional MFA → return), and Microsoft's
# device-code lifetime is 15 minutes = 900 s. The original 180 s override
# truncated valid auth flows mid-handshake on every fresh-tenant walk (#52).
# 900.0 matches both the mutator default and the upstream auth constraint.
_PUBLISH_APPLY_TIMEOUT_SECONDS = 900.0


def _transform_manifest_to_copilot_chat(
    manifest: dict,
    *,
    bot_id: str,
    manifest_id: str | None = None,
    distinguish_name_short: bool = False,
    scopes: tuple[str, ...] = _COPILOT_CHAT_DEFAULT_SCOPES,
    prompt_starters: tuple[dict[str, str], ...] | None = None,
) -> dict:
    """Pure transform: AI Teammate manifest dict → Custom Engine Agent shape."""
    out = dict(manifest)
    # #74 — resolve + cap the zero-state prompt starters (the default set when the
    # operator supplied none).
    starters = list(prompt_starters or _DEFAULT_PROMPT_STARTERS)[:_PROMPT_STARTER_MAX]
    if manifest_id:
        out["id"] = manifest_id
    out["manifestVersion"] = _COPILOT_CHAT_MANIFEST_VERSION
    # ``agenticUserTemplates`` is the AI Teammate shape and is not part
    # of the 1.21+ schema; strip it.
    out.pop("agenticUserTemplates", None)
    if distinguish_name_short and isinstance(out.get("name"), dict):
        short = str(out["name"].get("short") or "")
        if short:
            out["name"] = dict(out["name"])
            out["name"]["short"] = _with_name_short_suffix(short)
    out["bots"] = [
        {
            "botId": bot_id,
            "scopes": list(scopes),
            "supportsCalling": False,
            "supportsVideo": False,
            # #76c — enables Teams file transfer both ways: inbound attachments
            # (download.info → media cache) and outbound FileConsentCard →
            # OneDrive upload. Personal (Teams 1:1) scope; group/channel needs
            # Graph and is deferred (#76 slice d).
            "supportsFiles": True,
            "isNotificationOnly": False,
            "commandLists": [
                {
                    "scopes": list(_PROMPT_STARTER_SCOPES),
                    "commands": [
                        # description drives the card's subtitle line; without it
                        # Copilot Chat repeats the title (walk-observed 2026-07-06).
                        # Set it to the prompt so the card reads title + preview.
                        {
                            "title": s["title"],
                            "description": s["prompt"],
                            "type": "prompt",
                            "prompt": s["prompt"],
                        }
                        for s in starters
                    ],
                }
            ],
        }
    ]
    out["copilotAgents"] = {
        "customEngineAgents": [
            {"type": "bot", "id": bot_id},
        ]
    }
    return out


def _extract_bot_id_from_manifest(manifest: dict) -> str | None:
    """Best-effort grab of the bot/app id from an AI Teammate manifest.

    Order:
    1. ``webApplicationInfo.id`` — Custom Engine Agent / classic bot
       manifests carry this. Not present in current GA-CLI AI Teammate
       emit (1.1.174+).
    2. ``bots[0].botId`` — present in Custom Engine Agent manifests we
       may be re-processing. Not present in AI Teammate emits.
    3. ``id`` (top-level) — the Teams manifest's app identifier. In
       Hermes-A365's deployment pattern, the blueprint Entra app is
       both the Teams app id and the bot identity, so this is the
       canonical fallback for AI-Teammate-emitted manifests.
    """
    wai = manifest.get("webApplicationInfo")
    if isinstance(wai, dict):
        bot_id = wai.get("id")
        if isinstance(bot_id, str) and bot_id:
            return bot_id
    bots = manifest.get("bots")
    if isinstance(bots, list) and bots:
        first = bots[0]
        if isinstance(first, dict):
            bid = first.get("botId")
            if isinstance(bid, str) and bid:
                return bid
    top_id = manifest.get("id")
    if isinstance(top_id, str) and top_id:
        return top_id
    return None


def _agentic_template_sidecar_files(manifest: dict) -> set[str]:
    """Return AI-Teammate template sidecars to omit from CEA zips."""
    sidecars = {"agenticUserTemplateManifest.json"}
    templates = manifest.get("agenticUserTemplates")
    if isinstance(templates, list):
        for item in templates:
            if isinstance(item, dict) and isinstance(item.get("file"), str):
                sidecars.add(item["file"])
    return sidecars


def _patch_manifest_to_copilot_chat(
    zip_path: str,
    *,
    bot_id_override: str | None = None,
    manifest_id: str | None = None,
    distinguish_name_short: bool = False,
    scopes: tuple[str, ...] = _COPILOT_CHAT_DEFAULT_SCOPES,
    prompt_starters: tuple[dict[str, str], ...] | None = None,
) -> tuple[str, dict] | None:
    """Rewrite ``manifest.json`` inside *zip_path* into a Custom Engine
    Agent shape. Returns ``(bot_id_used, summary)`` on success, ``None``
    on any failure (file missing, no manifest.json, bad json, can't
    determine bot id). Best-effort: any I/O failure leaves the original
    zip untouched.
    """
    import json
    import tempfile
    import zipfile
    from pathlib import Path

    zp = Path(zip_path)
    if not zp.is_file():
        return None
    try:
        with zipfile.ZipFile(zp, "r") as zf:
            names = zf.namelist()
            if "manifest.json" not in names:
                return None
            with zf.open("manifest.json") as fh:
                manifest = json.load(fh)
            sidecars = _agentic_template_sidecar_files(manifest)
            dropped_sidecars = sidecars.intersection(names)
            other_files = {
                n: zf.read(n)
                for n in names
                if n != "manifest.json" and n not in dropped_sidecars
            }
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
        return None

    bot_id = bot_id_override or _extract_bot_id_from_manifest(manifest)
    if not bot_id:
        return None

    had_aut = "agenticUserTemplates" in manifest
    new_manifest = _transform_manifest_to_copilot_chat(
        manifest,
        bot_id=bot_id,
        manifest_id=manifest_id,
        distinguish_name_short=distinguish_name_short,
        scopes=scopes,
        prompt_starters=prompt_starters,
    )
    blob = json.dumps(new_manifest, indent=2).encode("utf-8")

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(zp.parent), prefix=zp.name + ".", suffix=".tmp", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", blob)
            for name, b in other_files.items():
                zf.writestr(name, b)
        tmp_path.replace(zp)
    except OSError:
        import contextlib

        if tmp_path is not None:
            with contextlib.suppress(Exception):
                tmp_path.unlink(missing_ok=True)
        return None

    summary = {
        "manifest_version": _COPILOT_CHAT_MANIFEST_VERSION,
        "bot_id": bot_id,
        "manifest_id": new_manifest.get("id"),
        "scopes": list(scopes),
        "dropped_agentic_user_templates": had_aut,
        "dropped_agentic_template_files": sorted(dropped_sidecars),
    }
    return (bot_id, summary)


def _extract_instance_id(output: str) -> str | None:
    """Grep for the registered instance id in blueprint-only flow output."""
    match = _INSTANCE_ID_RE.search(output)
    return match.group(1) if match else None


def apply_publish_plan(
    plan: PublishPlan,
    *,
    mutator: Mutator,
) -> PublishResult:
    """Run ``a365 publish``; surface the produced artefact (zip or
    registered instance) appropriate to the flow."""
    from pathlib import Path
    from shutil import copyfile

    run = mutator.run(plan.step.argv, timeout=_PUBLISH_APPLY_TIMEOUT_SECONDS)
    package_path: str | None = None
    copilot_chat_package_path: str | None = None
    copilot_chat_bot_id: str | None = None
    copilot_chat_manifest_id: str | None = None
    instance_id: str | None = None
    messages: list[str] = [f"[apply] {plan.step.description} — done"]

    if plan.inputs.aiteammate or plan.inputs.copilot_chat:
        emitted_zip = _extract_package_path(run.combined)
        if not emitted_zip:
            # Microsoft a365 CLI >= 1.1.181 extracts a manifest template and
            # stops ("Customize before packaging") instead of emitting a zip.
            # Package the extracted directory ourselves so the CEA transform
            # pipeline below runs unchanged (older CLIs emitted the zip).
            manifest_dir = _extract_manifest_dir(run.combined)
            if manifest_dir:
                emitted_zip = _zip_manifest_dir(manifest_dir)
                if emitted_zip:
                    messages.append(
                        "[apply] packaged extracted manifest template → "
                        f"{emitted_zip} (CLI no longer emits a zip directly)"
                    )
        if emitted_zip:
            if plan.inputs.manifest_id == "auto" or (
                plan.inputs.manifest_id is None
                and plan.inputs.aiteammate
                and plan.inputs.copilot_chat
            ):
                copilot_chat_manifest_id = str(uuid.uuid4())
            else:
                copilot_chat_manifest_id = plan.inputs.manifest_id

            # Decide which file we end up calling the Copilot Chat zip.
            # - Both surfaces requested → copy the CLI-emitted zip aside
            #   to a sibling ``.copilot-chat.zip`` and transform the copy
            #   (the original stays as the AI Teammate zip).
            # - Copilot Chat only → transform the CLI-emitted zip in
            #   place; no AI Teammate zip is kept.
            # - AI Teammate only → no transform; keep emitted as-is.
            if plan.inputs.copilot_chat and plan.inputs.aiteammate:
                orig = Path(emitted_zip)
                sibling = orig.with_name(
                    orig.stem + _COPILOT_CHAT_ZIP_INFIX + orig.suffix
                )
                copyfile(orig, sibling)
                copilot_chat_target: str | None = str(sibling)
                package_path = emitted_zip
            elif plan.inputs.copilot_chat:
                copilot_chat_target = emitted_zip
                package_path = None
            else:
                copilot_chat_target = None
                package_path = emitted_zip

            if copilot_chat_target is not None:
                cc_result = _patch_manifest_to_copilot_chat(
                    copilot_chat_target,
                    bot_id_override=plan.inputs.bot_id,
                    manifest_id=copilot_chat_manifest_id,
                    distinguish_name_short=(
                        plan.inputs.aiteammate and plan.inputs.copilot_chat
                    ),
                    prompt_starters=plan.inputs.prompt_starters,
                )
                if cc_result is not None:
                    copilot_chat_bot_id, summary = cc_result
                    copilot_chat_manifest_id = summary.get("manifest_id")
                    copilot_chat_package_path = copilot_chat_target
                    dropped = " (dropped agenticUserTemplates)" if summary[
                        "dropped_agentic_user_templates"
                    ] else ""
                    messages.append(
                        f"[apply] transformed to Custom Engine Agent: "
                        f"manifestVersion={summary['manifest_version']}, "
                        f"botId={copilot_chat_bot_id}, "
                        f"manifestId={summary['manifest_id']}, "
                        f"scopes={summary['scopes']}" + dropped
                    )
                    if summary["dropped_agentic_template_files"]:
                        messages.append(
                            "[apply] omitted AI Teammate template files from "
                            "Copilot Chat zip: "
                            + ", ".join(summary["dropped_agentic_template_files"])
                        )
                else:
                    messages.append(
                        "[apply] WARNING: Copilot Chat transform failed "
                        "(no bot id found or zip unreadable); "
                        "pass --bot-id to override"
                    )
                    # Roll back the sibling copy so we don't leave a
                    # half-baked zip behind.
                    if (
                        plan.inputs.copilot_chat
                        and plan.inputs.aiteammate
                        and copilot_chat_target != emitted_zip
                    ):
                        import contextlib

                        with contextlib.suppress(OSError):
                            Path(copilot_chat_target).unlink(missing_ok=True)

            # Slice 19r-c: post-process emitted zip(s) to keep name.short
            # ≤ 30 chars (Admin Centre rejects > 30). Applies to either
            # the AI Teammate zip or the Copilot Chat zip — both feed
            # admin-centre uploads that enforce the same cap.
            for zp in [p for p in (package_path, copilot_chat_package_path) if p]:
                patched = _patch_manifest_name_short(zp)
                if patched is not None:
                    old, new = patched
                    fname = Path(zp).name
                    messages.append(
                        f"[apply] truncated name.short in {fname}: "
                        f"{old!r} ({len(old)} chars) → {new!r} ({len(new)} chars) "
                        "to satisfy the 30-char Admin Centre cap"
                    )

        # AI Teammate upload reminder is unconditional (matches pre-19u-a
        # behaviour — the CLI sometimes prints the zip path in a phrasing
        # we don't recognise, but the operator still needs to upload it).
        if plan.inputs.aiteammate:
            if package_path:
                messages.append(f"[apply] AI Teammate package: {package_path}")
            messages.append(
                f"[apply] AI Teammate next: upload the package to the "
                f"M365 Admin Centre at {ADMIN_CENTRE_URL}"
            )
        if copilot_chat_package_path:
            messages.append(
                f"[apply] Copilot Chat package: {copilot_chat_package_path}"
            )
            messages.append(
                "[apply] Copilot Chat next: upload to Microsoft Admin Portal → "
                "Agents → Upload custom agent"
            )
    else:
        instance_id = _extract_instance_id(run.combined)
        if instance_id:
            messages.append(f"[apply] agent instance registered: {instance_id}")
        messages.append(
            "[apply] no upload needed — the instance is registered server-side via Graph"
        )

    return PublishResult(
        plan=plan,
        raw=run,
        package_path=package_path,
        instance_id=instance_id,
        copilot_chat_package_path=copilot_chat_package_path,
        copilot_chat_bot_id=copilot_chat_bot_id,
        copilot_chat_manifest_id=copilot_chat_manifest_id,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
        parser = argparse.ArgumentParser(
            description="hermes a365 publish — package the agent manifest for admin-centre upload.",
        )
    parser.add_argument("--agent-name", required=True, help="agent base name")
    parser.add_argument(
        "--tenant-id",
        help="tenant id; default auto-detects via `az account show`",
    )
    parser.add_argument(
        "--aiteammate",
        action="store_true",
        help="treat as AI Teammate (creates Entra user); default is blueprint-only",
    )
    parser.add_argument(
        "--copilot-chat",
        action="store_true",
        help=(
            "emit a Custom Engine Agent manifest (manifestVersion 1.27, "
            "bots + copilotAgents blocks) for M365 Copilot Chat upload; "
            "combine with --aiteammate to emit both surfaces"
        ),
    )
    parser.add_argument(
        "--bot-id",
        help=(
            "override the botId used in the Copilot Chat manifest; "
            "default extracts it from the emitted manifest's "
            "webApplicationInfo.id"
        ),
    )
    parser.add_argument(
        "--manifest-id",
        help=(
            "override the Teams App Catalog manifest.id used in the Copilot "
            "Chat zip; pass 'auto' to generate a fresh GUID. Defaults to "
            "auto when --aiteammate and --copilot-chat are combined"
        ),
    )
    parser.add_argument(
        "--use-blueprint",
        action="store_true",
        help="use blueprint-based non-DW flow (only with --aiteammate false)",
    )
    parser.add_argument(
        "--prompt-starter",
        action="append",
        metavar="TITLE|PROMPT",
        help=(
            "Copilot Chat zero-state prompt starter as 'Title|Prompt text' "
            f"(repeatable, max {_PROMPT_STARTER_MAX}; only with --copilot-chat). "
            "Defaults to a generic set when omitted."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--apply", action="store_true", help="execute the plan; default is dry-run")
    return parser


def _parse_prompt_starter(spec: str) -> dict[str, str]:
    """Parse a ``--prompt-starter`` value ('Title|Prompt text') into a
    ``{title, prompt}`` command. The first ``|`` splits title from prompt; with
    no ``|`` the whole string is used for both. Fields are trimmed to the 1.27
    schema caps (title ≤128, prompt ≤4000)."""
    title, sep, prompt = spec.partition("|")
    title = title.strip()
    prompt = prompt.strip() if sep else title
    if not title:
        raise ValueError(
            "--prompt-starter needs a non-empty title (format: 'Title|Prompt text')"
        )
    return {"title": title[:128], "prompt": prompt[:4000]}


def run(args: argparse.Namespace) -> int:
    try:
        inputs = PublishInputs(
            agent_name=args.agent_name,
            tenant_id=args.tenant_id,
            aiteammate=args.aiteammate,
            copilot_chat=args.copilot_chat,
            bot_id=args.bot_id,
            manifest_id=args.manifest_id,
            use_blueprint=args.use_blueprint,
            prompt_starters=(
                tuple(_parse_prompt_starter(s) for s in args.prompt_starter)
                if getattr(args, "prompt_starter", None)
                else None
            ),
            verbose=args.verbose,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    plan = build_publish_plan(inputs)
    sys.stdout.write(plan.render_human() + "\n")

    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to package.\n")
        return 0

    try:
        result = apply_publish_plan(plan, mutator=get_mutator())
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2
    except CliInvocationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
