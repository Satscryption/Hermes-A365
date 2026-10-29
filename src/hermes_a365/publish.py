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
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import uuid
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._common import (
    TENANT_ID_HELP,
    UNPINNED_TENANT_PLAN_LINE,
    SidecarLockError,
    TenantResolutionError,
    active_az_tenant,
    ensure_contained,
    exclusive_sidecar_lock,
    resolve_expected_tenant,
    sidecar_backup_path,
    slugify,
    tenant_ids_match,
    validate_slug,
    write_owner_only_bytes_exclusive,
    write_owner_only_text_atomic,
)
from .mutator import AADSTSError, CliInvocationError, Mutator, RunResult, get_mutator
from .sidecar_schema import (
    SidecarSchemaError,
    publish_unsupported_schema_message,
    validate_publish_state_schema_version,
)

ADMIN_CENTRE_URL = "https://admin.microsoft.com/"


class PublishError(RuntimeError):
    pass


_ZIP_MAX_MEMBERS = 128
_ZIP_MAX_DEPTH = 16
_ZIP_MAX_MANIFEST_BYTES = 1024 * 1024
_ZIP_MAX_TOTAL_UNCOMPRESSED = 64 * 1024 * 1024
_ZIP_MAX_COMPRESSION_RATIO = 100


def _regular_nonsymlink(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def _open_regular_nofollow(path: Path) -> tuple[int, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise OSError("path is not a regular non-symlink file") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise OSError("path is not a regular non-symlink file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OSError("path is not a regular non-symlink file") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
            raise OSError("path is not a stable regular non-symlink file")
        return fd, st
    except BaseException:
        os.close(fd)
        raise


def _validated_zip_infos(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > _ZIP_MAX_MEMBERS:
        raise ValueError("manifest archive has too many members")
    seen: set[str] = set()
    total = 0
    manifest_count = 0
    for info in infos:
        name = info.filename
        parts = Path(name).parts
        if not name or name.startswith(("/", "\\")) or ".." in parts or "\\" in name:
            raise ValueError("manifest archive contains an unsafe member path")
        folded = name.casefold()
        if folded in seen:
            raise ValueError("manifest archive contains duplicate member names")
        seen.add(folded)
        if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
            raise ValueError("manifest archive contains a symbolic link")
        total += info.file_size
        if total > _ZIP_MAX_TOTAL_UNCOMPRESSED:
            raise ValueError("manifest archive exceeds the uncompressed-size limit")
        if info.compress_size == 0 and info.file_size:
            raise ValueError("manifest archive contains an invalid compressed member")
        if info.compress_size and info.file_size / info.compress_size > _ZIP_MAX_COMPRESSION_RATIO:
            raise ValueError("manifest archive exceeds the compression-ratio limit")
        if name == "manifest.json":
            manifest_count += 1
            if info.file_size > _ZIP_MAX_MANIFEST_BYTES:
                raise ValueError("manifest.json exceeds the size limit")
    if manifest_count != 1:
        raise ValueError("manifest archive must contain exactly one manifest.json")
    return infos


def _rewrite_manifest_zip(zp: Path, manifest_blob: bytes, *, drop: set[str] | None = None) -> None:
    if len(manifest_blob) > _ZIP_MAX_MANIFEST_BYTES:
        raise OSError("rewritten manifest exceeds the size limit")
    dropped = drop or set()
    tmp_path: Path | None = None
    source_fd = -1
    try:
        source_fd, _ = _open_regular_nofollow(zp)
        with tempfile.NamedTemporaryFile(
            dir=str(zp.parent), prefix=zp.name + ".", suffix=".tmp", delete=False
        ) as tmp:
            os.fchmod(tmp.fileno(), 0o600)
            tmp_path = Path(tmp.name)
        with os.fdopen(source_fd, "rb") as source_file:
            source_fd = -1
            with (
                zipfile.ZipFile(source_file, "r") as source,
                zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as target,
            ):
                infos = _validated_zip_infos(source)
                target.writestr("manifest.json", manifest_blob)
                for info in infos:
                    if info.filename == "manifest.json" or info.filename in dropped:
                        continue
                    with source.open(info) as src, target.open(info.filename, "w") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
        with tmp_path.open("rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp_path, zp)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    finally:
        if source_fd >= 0:
            os.close(source_fd)


def _copy_regular_file_atomic(source: Path, destination: Path) -> None:
    if destination.exists() and not _regular_nonsymlink(destination):
        raise OSError("destination is not a regular non-symlink file")
    tmp_path: Path | None = None
    source_fd = -1
    try:
        source_fd, _ = _open_regular_nofollow(source)
        with os.fdopen(source_fd, "rb") as src:
            source_fd = -1
            with tempfile.NamedTemporaryFile(
                dir=str(destination.parent), prefix=destination.name + ".", delete=False
            ) as dst:
                os.fchmod(dst.fileno(), 0o600)
                tmp_path = Path(dst.name)
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
        os.replace(tmp_path, destination)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    finally:
        if source_fd >= 0:
            os.close(source_fd)


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
    # #109 — optional override for manifest ``version`` (must beat last publish).
    app_version: str | None = None
    # local ``~/.hermes/agents/<slug>/`` dir; defaults to slugify(agent_name).
    slug: str | None = None
    # Hermes gateway profile for publish-state partitioning (``--profile``).
    profile: str | None = None
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
                self.manifest_id = _canonical_manifest_guid(self.manifest_id)
            except ValueError as e:
                raise ValueError("--manifest-id must be 'auto' or a GUID") from e
        if self.app_version and not self.copilot_chat:
            raise ValueError("--app-version is only meaningful with --copilot-chat")
        if self.app_version:
            _parse_app_version_triple(self.app_version)

    @property
    def resolved_slug(self) -> str:
        if self.slug:
            return validate_slug(self.slug)
        return slugify(self.agent_name)


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
            lines.append(UNPINNED_TENANT_PLAN_LINE)
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
        if self.inputs.app_version:
            lines.append(f"  app-version: {self.inputs.app_version} (override)")
        if self.inputs.profile:
            lines.append(f"  profile: {self.inputs.profile} (publish state scope)")
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
    when the directory is missing or carries no root ``manifest.json``. Bounded
    nested regular files are preserved; links and special files fail closed."""
    d = Path(manifest_dir)
    zip_path = d.with_name(d.name + ".zip")
    if zip_path.exists() and not _regular_nonsymlink(zip_path):
        return None
    tmp_path: Path | None = None
    directory_fd = -1
    try:
        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            dir_flags |= os.O_NOFOLLOW
        directory_fd = os.open(d, dir_flags)
        root_names = sorted(os.listdir(directory_fd))
        if "manifest.json" not in root_names:
            return None
        with ExitStack() as stack:
            sources: list[tuple[str, Any, os.stat_result]] = []
            total = 0

            def collect(dir_fd: int, prefix: str, depth: int) -> bool:
                nonlocal total
                if depth > _ZIP_MAX_DEPTH:
                    return False
                for name in sorted(os.listdir(dir_fd)):
                    archive_name = f"{prefix}/{name}" if prefix else name
                    if "\\" in archive_name:
                        return False
                    before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                    if stat.S_ISLNK(before.st_mode):
                        return False
                    if stat.S_ISDIR(before.st_mode):
                        flags = os.O_RDONLY
                        if hasattr(os, "O_DIRECTORY"):
                            flags |= os.O_DIRECTORY
                        if hasattr(os, "O_NOFOLLOW"):
                            flags |= os.O_NOFOLLOW
                        child_fd = os.open(name, flags, dir_fd=dir_fd)
                        child_st = os.fstat(child_fd)
                        if not stat.S_ISDIR(child_st.st_mode) or (
                            child_st.st_dev,
                            child_st.st_ino,
                        ) != (before.st_dev, before.st_ino):
                            os.close(child_fd)
                            return False
                        stack.callback(os.close, child_fd)
                        if not collect(child_fd, archive_name, depth + 1):
                            return False
                        continue
                    if not stat.S_ISREG(before.st_mode):
                        return False
                    fd = os.open(
                        name,
                        os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
                        dir_fd=dir_fd,
                    )
                    source = stack.enter_context(os.fdopen(fd, "rb"))
                    st = os.fstat(source.fileno())
                    if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (
                        before.st_dev,
                        before.st_ino,
                    ):
                        return False
                    total += st.st_size
                    if total > _ZIP_MAX_TOTAL_UNCOMPRESSED or len(sources) >= _ZIP_MAX_MEMBERS:
                        return False
                    sources.append((archive_name, source, st))
                return True

            if not collect(directory_fd, "", 1):
                return None
            with tempfile.NamedTemporaryFile(dir=str(d.parent), delete=False) as tmp:
                os.fchmod(tmp.fileno(), 0o600)
                tmp_path = Path(tmp.name)
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, source, st in sources:
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | stat.S_IMODE(st.st_mode)) << 16
                    with zf.open(info, "w") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
        with zipfile.ZipFile(tmp_path, "r") as zf:
            _validated_zip_infos(zf)
        os.replace(tmp_path, zip_path)
    except (OSError, ValueError, zipfile.BadZipFile):
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        return None
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
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
    zp = Path(zip_path)
    if not _regular_nonsymlink(zp):
        return None

    try:
        with zipfile.ZipFile(zp, "r") as zf:
            _validated_zip_infos(zf)
            with zf.open("manifest.json") as fh:
                manifest = json.load(fh)
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
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

    try:
        _rewrite_manifest_zip(zp, new_manifest)
    except (OSError, ValueError, zipfile.BadZipFile):
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

# #109 — Microsoft Admin Center rejects a CEA *update* upload when the app
# ``version`` is not strictly greater than the installed package. The GA CLI
# template re-stamps the same value every run; we monotonically bump it on each
# successful ``publish --copilot-chat --apply`` and record the last value in
# ``$HERMES_HOME/agents/<slug>/copilot-chat-publish.json`` (keyed by Teams
# catalog ``manifest.id``).
_COPILOT_CHAT_PUBLISH_STATE_FILENAME = "copilot-chat-publish.json"
_COPILOT_CHAT_PUBLISH_STATE_SCHEMA = 2
_COPILOT_CHAT_PUBLISH_STATE_SCHEMA_V1 = 1
_APP_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# Wall-clock budget for ``a365 publish`` invoked under ``apply_publish_plan``.
# This is the only interactive call in the wrapper chain — when MSAL cannot
# silent-token (fresh shell, stale cache), ``a365`` falls back to device-code
# auth (browser open → sign-in → optional MFA → return), and Microsoft's
# device-code lifetime is 15 minutes = 900 s. The original 180 s override
# truncated valid auth flows mid-handshake on every fresh-tenant walk (#52).
# 900.0 matches both the mutator default and the upstream auth constraint.
_PUBLISH_APPLY_TIMEOUT_SECONDS = 900.0


def _parse_app_version_triple(value: str) -> tuple[int, int, int]:
    match = _APP_VERSION_RE.match(value.strip())
    if not match:
        raise ValueError(
            "app version must be three non-negative integers separated by dots (e.g. 1.1.4)"
        )
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _format_app_version(parts: tuple[int, int, int]) -> str:
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def _compare_app_versions(left: str, right: str) -> int:
    a = _parse_app_version_triple(left)
    b = _parse_app_version_triple(right)
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def _bump_app_version_patch(value: str) -> str:
    major, minor, patch = _parse_app_version_triple(value)
    return _format_app_version((major, minor, patch + 1))


def _canonical_manifest_guid(value: str) -> str:
    """Return the lowercase hyphenated Teams catalog ``manifest.id`` form."""
    return str(uuid.UUID(value.strip()))


def _parse_template_app_version(manifest: dict) -> str | None:
    """Parse manifest ``version`` as ``X.Y.Z``, or ``None`` when missing/invalid."""
    raw = manifest.get("version")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return _format_app_version(_parse_app_version_triple(raw))
    except ValueError:
        return None


def _resolve_next_copilot_chat_app_version(
    *,
    template_version: str | None,
    last_published: str | None,
    override: str | None,
) -> str:
    """Return the manifest ``version`` to stamp on the next CEA zip."""
    if override is not None:
        chosen = _format_app_version(_parse_app_version_triple(override))
        if last_published is not None and _compare_app_versions(chosen, last_published) <= 0:
            raise ValueError(
                f"--app-version {chosen} must be strictly greater than the last "
                f"published app version {last_published}"
            )
        return chosen
    if last_published is None:
        if template_version is None:
            raise ValueError(
                "manifest version is missing or unparseable (expected X.Y.Z); "
                "pass --app-version to set the Teams app version explicitly"
            )
        return template_version
    if template_version is not None and _compare_app_versions(template_version, last_published) > 0:
        return template_version
    return _bump_app_version_patch(last_published)


def _agents_root(hermes_home: Path) -> Path:
    return hermes_home / "agents"


def _copilot_chat_publish_state_path(hermes_home: Path, slug: str) -> Path:
    return _agents_root(hermes_home) / slug / _COPILOT_CHAT_PUBLISH_STATE_FILENAME


def _guard_copilot_chat_publish_state_path(path: Path, hermes_home: Path) -> None:
    """Refuse read/write when ``agents/<slug>/`` escapes the agents root."""
    root = _agents_root(hermes_home)
    try:
        ensure_contained(path.parent, root)
        ensure_contained(path, root)
    except ValueError as exc:
        raise PublishError(str(exc)) from exc


def _resolve_hermes_profile_name(cli_profile: str | None = None) -> str:
    """Hermes gateway profile name for publish-state partitioning.

    ``publish --profile`` wins, then ``HERMES_PROFILE`` /
    ``HERMES_GATEWAY_PROFILE``, then ``default``. The gateway's
    ``hermes gateway run --profile`` does not set those env vars — pass the
    same name here (or export ``HERMES_PROFILE``) so publish state matches the
    profile's ``HERMES_HOME``.
    """
    if cli_profile and cli_profile.strip():
        return cli_profile.strip()
    for key in ("HERMES_PROFILE", "HERMES_GATEWAY_PROFILE"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return "default"


_COPILOT_CHAT_PUBLISH_STATE_UNSCOPED_V1 = "_unscopedV1"


def _normalize_publish_state_tenant_id(tenant_id: str) -> str:
    """Canonical tenant GUID for publish-state scope (matches ``tenant_ids_match``)."""
    tenant = tenant_id.strip()
    if not tenant:
        raise PublishError(
            "no canonical tenant pin was resolved; pass --tenant-id or set "
            "A365_TENANT_ID before publishing"
        )
    try:
        return str(uuid.UUID(tenant))
    except ValueError:
        return tenant


@dataclass(frozen=True)
class _CopilotChatManifestIdResolution:
    manifest_id: str | None
    reused_from_state: bool
    freshly_minted: bool


def _require_copilot_chat_slug(inputs: PublishInputs) -> str:
    try:
        slug = validate_slug(inputs.slug) if inputs.slug else slugify(inputs.agent_name)
    except ValueError as exc:
        raise PublishError(str(exc)) from exc
    if not slug:
        raise PublishError(
            "agent_name does not yield a local slug; pass --slug "
            "to record Copilot Chat publish versions"
        )
    return slug


def _normalize_publish_state_v1_fields(
    raw: dict[str, object],
) -> dict[str, object]:
    """Parse legacy schema v1 without claiming tenant/profile scope."""
    return {
        "schemaVersion": _COPILOT_CHAT_PUBLISH_STATE_SCHEMA_V1,
        "manifestId": raw["manifestId"],
        "lastAppVersion": raw["lastAppVersion"],
        "ceaCatalogIdMinted": False,
        _COPILOT_CHAT_PUBLISH_STATE_UNSCOPED_V1: True,
    }


def _refuse_unscoped_v1_publish_state(
    record: dict[str, object],
    *,
    path: Path,
    app_version_override: str | None,
) -> None:
    if not record.get(_COPILOT_CHAT_PUBLISH_STATE_UNSCOPED_V1):
        return
    if app_version_override is not None:
        return
    raise PublishError(
        f"Copilot Chat publish state at {path} is legacy schema v1 (no tenant "
        "or profile scope); pass --app-version strictly above the version shown "
        "in Microsoft Admin Center to seed scoped schema v2 state, or remove "
        "the file before re-running publish --apply"
    )


def _publish_state_allows_manifest_id_change(
    inputs: PublishInputs,
    resolution: _CopilotChatManifestIdResolution | None,
) -> bool:
    """Whether apply may replace on-disk state keyed by a different manifestId."""
    if inputs.manifest_id and inputs.manifest_id != "auto" and inputs.app_version is not None:
        return True
    return bool(resolution and resolution.freshly_minted)


def _refuse_copilot_chat_manifest_id_colliding_with_aiteammate(
    inputs: PublishInputs,
    *,
    aiteammate_catalog_id: str | None,
) -> None:
    """Combined emits must not stamp the AI Teammate catalog id on the CEA zip."""
    if not (inputs.aiteammate and inputs.copilot_chat):
        return
    if not aiteammate_catalog_id or inputs.manifest_id in (None, "auto"):
        return
    try:
        chosen = _canonical_manifest_guid(inputs.manifest_id)
        template = _canonical_manifest_guid(aiteammate_catalog_id)
    except ValueError:
        return
    if chosen != template:
        return
    raise PublishError(
        f"--manifest-id {chosen} matches the AI Teammate catalog id; combined "
        "--aiteammate --copilot-chat publishes need a distinct CEA catalog id. "
        "Omit --manifest-id for auto mint or pass a new GUID with --app-version."
    )


def _publish_state_manifest_id_mismatch_message(
    path: Path,
    *,
    stored_id: str,
    catalog_manifest_id: str,
    aiteammate_catalog_id: str | None,
    inputs: PublishInputs,
    state_cea_catalog_id_minted: bool,
) -> str:
    base = (
        f"Copilot Chat publish state at {path} records manifestId {stored_id}, "
        f"but this publish targets {catalog_manifest_id}. "
    )
    combined_or_auto_state = (
        inputs.aiteammate and inputs.copilot_chat
    ) or state_cea_catalog_id_minted
    if (
        combined_or_auto_state
        and aiteammate_catalog_id
        and stored_id == _canonical_manifest_guid(aiteammate_catalog_id)
    ):
        return (
            base + "The stored id is the AI Teammate catalog id — do not pass "
            "--manifest-id with that value on combined publishes. Omit "
            "--manifest-id to mint a distinct CEA catalog id, remove or rename "
            "the state file, or pass a new --manifest-id with --app-version."
        )
    return (
        base + f"Pass --manifest-id {stored_id} to match the stored id, remove or "
        "rename the state file, or pass a new --manifest-id with --app-version "
        "for a separate catalog entry."
    )


def _validate_publish_state_scope(
    record: dict[str, object],
    *,
    path: Path,
    tenant_id: str,
    profile: str,
) -> None:
    stored_tenant = record.get("tenantId")
    if not isinstance(stored_tenant, str) or not stored_tenant.strip():
        raise PublishError(
            f"Copilot Chat publish state at {path} is missing tenantId; "
            "fix or remove the file before re-running publish --apply"
        )
    stored_profile = record.get("profile")
    if not isinstance(stored_profile, str) or not stored_profile.strip():
        raise PublishError(
            f"Copilot Chat publish state at {path} is missing profile; "
            "fix or remove the file before re-running publish --apply"
        )
    if _normalize_publish_state_tenant_id(stored_tenant) != _normalize_publish_state_tenant_id(
        tenant_id
    ):
        raise PublishError(
            f"Copilot Chat publish state at {path} belongs to tenant "
            f"{stored_tenant!r}, not the current publish pin {tenant_id!r}. "
            "Use a matching --tenant-id, a different --slug, or remove the "
            "state file before re-running publish --apply"
        )
    if stored_profile != profile:
        raise PublishError(
            f"Copilot Chat publish state at {path} belongs to Hermes profile "
            f"{stored_profile!r}, not the current profile {profile!r}. "
            "Set HERMES_PROFILE to match, use a different --slug, or remove "
            "the state file before re-running publish --apply"
        )


def _load_copilot_chat_publish_state_record(
    path: Path,
    *,
    hermes_home: Path,
    tenant_id: str,
    profile: str,
) -> dict[str, object] | None:
    """Return a validated publish-state object, or ``None`` when absent."""
    if not path.is_file():
        return None
    _guard_copilot_chat_publish_state_path(path, hermes_home)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublishError(
            f"could not read Copilot Chat publish state at {path}: {exc}; "
            "fix or remove the file before re-running publish --apply"
        ) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PublishError(
            f"Copilot Chat publish state at {path} is not valid JSON ({exc}); "
            "fix or remove the file before re-running publish --apply"
        ) from exc
    if not isinstance(raw, dict):
        raise PublishError(
            f"Copilot Chat publish state at {path} must be a JSON object; "
            "fix or remove the file before re-running publish --apply"
        )
    try:
        schema = validate_publish_state_schema_version(raw)
    except SidecarSchemaError as exc:
        raise PublishError(
            publish_unsupported_schema_message(
                path_label=str(path),
                expected_v1=_COPILOT_CHAT_PUBLISH_STATE_SCHEMA_V1,
                expected_v2=_COPILOT_CHAT_PUBLISH_STATE_SCHEMA,
            )
        ) from exc
    if schema == _COPILOT_CHAT_PUBLISH_STATE_SCHEMA_V1:
        stored_id = raw.get("manifestId")
        last = raw.get("lastAppVersion")
        if not isinstance(stored_id, str) or not isinstance(last, str):
            raise PublishError(
                f"Copilot Chat publish state at {path} is incomplete schema v1; "
                "fix or remove the file before re-running publish --apply"
            )
        try:
            raw = _normalize_publish_state_v1_fields(
                {
                    "manifestId": _canonical_manifest_guid(stored_id),
                    "lastAppVersion": _format_app_version(_parse_app_version_triple(last)),
                }
            )
        except ValueError as exc:
            raise PublishError(
                f"Copilot Chat publish state at {path} has invalid v1 fields "
                f"({exc}); fix or remove the file before re-running publish --apply"
            ) from exc
    elif schema == _COPILOT_CHAT_PUBLISH_STATE_SCHEMA:
        stored_id = raw.get("manifestId")
        if not isinstance(stored_id, str) or not stored_id.strip():
            raise PublishError(
                f"Copilot Chat publish state at {path} is missing a string "
                "manifestId; fix or remove the file before re-running publish --apply"
            )
        try:
            raw["manifestId"] = _canonical_manifest_guid(stored_id)
        except ValueError:
            raise PublishError(
                f"Copilot Chat publish state at {path} has invalid manifestId "
                f"{stored_id!r}; fix or remove the file before re-running publish --apply"
            ) from None
        last = raw.get("lastAppVersion")
        if not isinstance(last, str):
            raise PublishError(
                f"Copilot Chat publish state at {path} is missing a string "
                "lastAppVersion; fix or remove the file before re-running publish --apply"
            )
        try:
            raw["lastAppVersion"] = _format_app_version(_parse_app_version_triple(last))
        except ValueError:
            raise PublishError(
                f"Copilot Chat publish state at {path} has invalid lastAppVersion "
                f"{last!r} (expected X.Y.Z); fix or remove the file before "
                "re-running publish --apply"
            ) from None
        minted = raw.get("ceaCatalogIdMinted")
        if minted is not None and not isinstance(minted, bool):
            raise PublishError(
                f"Copilot Chat publish state at {path} has invalid "
                f"ceaCatalogIdMinted={minted!r}; fix or remove the file before "
                "re-running publish --apply"
            )
        if minted is None:
            raw["ceaCatalogIdMinted"] = False
        _validate_publish_state_scope(raw, path=path, tenant_id=tenant_id, profile=profile)

    return raw


def _assert_publish_state_matches_catalog_id(
    path: Path,
    *,
    catalog_manifest_id: str,
    hermes_home: Path,
    tenant_id: str,
    profile: str,
    inputs: PublishInputs,
    manifest_id_resolution: _CopilotChatManifestIdResolution | None,
    aiteammate_catalog_id: str | None,
) -> None:
    """Refuse apply when on-disk state targets a different catalog ``manifest.id``."""
    if _publish_state_allows_manifest_id_change(inputs, manifest_id_resolution):
        return
    record = _load_copilot_chat_publish_state_record(
        path, hermes_home=hermes_home, tenant_id=tenant_id, profile=profile
    )
    if record is None:
        return
    stored_id = record.get("manifestId")
    if not isinstance(stored_id, str):
        return
    if stored_id == _canonical_manifest_guid(catalog_manifest_id):
        return
    minted = record.get("ceaCatalogIdMinted")
    state_cea_catalog_id_minted = bool(minted) if isinstance(minted, bool) else False
    raise PublishError(
        _publish_state_manifest_id_mismatch_message(
            path,
            stored_id=stored_id,
            catalog_manifest_id=catalog_manifest_id,
            aiteammate_catalog_id=aiteammate_catalog_id,
            inputs=inputs,
            state_cea_catalog_id_minted=state_cea_catalog_id_minted,
        )
    )


def _cea_manifest_id_for_auto_reuse(
    record: dict[str, object] | None,
    *,
    aiteammate_catalog_id: str | None,
) -> str | None:
    if record is None:
        return None
    if not record.get("ceaCatalogIdMinted"):
        return None
    manifest_id = record.get("manifestId")
    if not isinstance(manifest_id, str):
        return None
    if aiteammate_catalog_id and manifest_id == aiteammate_catalog_id:
        return None
    return manifest_id


def _copilot_chat_mints_catalog_id(inputs: PublishInputs) -> bool:
    """Whether this run assigns (or reuses) a generated catalog ``manifest.id``."""
    return inputs.manifest_id == "auto" or (
        inputs.manifest_id is None and inputs.aiteammate and inputs.copilot_chat
    )


def _resolve_copilot_chat_manifest_id_for_apply(
    inputs: PublishInputs,
    *,
    hermes_home: Path,
    tenant_id: str,
    profile: str,
    source_manifest: dict,
) -> _CopilotChatManifestIdResolution:
    """Catalog ``manifest.id`` to stamp on the Copilot Chat zip for this apply."""
    aiteammate_catalog_id = _catalog_manifest_id_for_copilot_chat(source_manifest, None)
    _refuse_copilot_chat_manifest_id_colliding_with_aiteammate(
        inputs, aiteammate_catalog_id=aiteammate_catalog_id
    )
    if not _copilot_chat_mints_catalog_id(inputs):
        return _CopilotChatManifestIdResolution(
            manifest_id=inputs.manifest_id,
            reused_from_state=False,
            freshly_minted=False,
        )
    slug = _require_copilot_chat_slug(inputs)
    state_path = _copilot_chat_publish_state_path(hermes_home, slug)
    record = _load_copilot_chat_publish_state_record(
        state_path,
        hermes_home=hermes_home,
        tenant_id=tenant_id,
        profile=profile,
    )
    stored = _cea_manifest_id_for_auto_reuse(record, aiteammate_catalog_id=aiteammate_catalog_id)
    if stored:
        return _CopilotChatManifestIdResolution(
            manifest_id=stored,
            reused_from_state=True,
            freshly_minted=False,
        )
    while True:
        minted = str(uuid.uuid4())
        if aiteammate_catalog_id and minted == aiteammate_catalog_id:
            continue
        return _CopilotChatManifestIdResolution(
            manifest_id=minted,
            reused_from_state=False,
            freshly_minted=True,
        )


def _read_copilot_chat_publish_state(
    path: Path,
    *,
    manifest_id: str,
    hermes_home: Path,
    tenant_id: str,
    profile: str,
) -> str | None:
    """Return the last published app version for *manifest_id*, if recorded.

    Returns ``None`` when the state file is absent, or when it records a
    different (valid) catalog ``manifestId``. Raises :class:`PublishError`
    when the file exists but is corrupt or untrusted.
    """
    record = _load_copilot_chat_publish_state_record(
        path, hermes_home=hermes_home, tenant_id=tenant_id, profile=profile
    )
    if record is None:
        return None
    try:
        current_canon = _canonical_manifest_guid(manifest_id)
    except ValueError as exc:
        raise PublishError(f"invalid catalog manifest id: {exc}") from exc
    stored_canon = record.get("manifestId")
    if not isinstance(stored_canon, str) or stored_canon != current_canon:
        return None
    last = record.get("lastAppVersion")
    return last if isinstance(last, str) else None


def _write_copilot_chat_publish_state(
    path: Path,
    *,
    manifest_id: str,
    app_version: str,
    hermes_home: Path,
    tenant_id: str,
    profile: str,
    cea_catalog_id_minted: bool,
) -> None:
    _guard_copilot_chat_publish_state_path(path, hermes_home)
    payload = {
        "schemaVersion": _COPILOT_CHAT_PUBLISH_STATE_SCHEMA,
        "tenantId": _normalize_publish_state_tenant_id(tenant_id),
        "profile": profile,
        "manifestId": _canonical_manifest_guid(manifest_id),
        "lastAppVersion": _format_app_version(_parse_app_version_triple(app_version)),
        "ceaCatalogIdMinted": cea_catalog_id_minted,
    }
    text = json.dumps(payload, indent=2) + "\n"
    unsupported = publish_unsupported_schema_message(
        path_label=str(path),
        expected_v1=_COPILOT_CHAT_PUBLISH_STATE_SCHEMA_V1,
        expected_v2=_COPILOT_CHAT_PUBLISH_STATE_SCHEMA,
    )
    try:
        with exclusive_sidecar_lock(path):
            if path.is_file():
                snapshot = path.read_bytes()
                try:
                    raw = json.loads(snapshot)
                except json.JSONDecodeError as exc:
                    raise PublishError(
                        f"Copilot Chat publish state at {path} is not valid JSON ({exc}); "
                        "fix or remove the file before re-running publish --apply"
                    ) from exc
                if not isinstance(raw, dict):
                    raise PublishError(
                        f"Copilot Chat publish state at {path} must be a JSON object; "
                        "fix or remove the file before re-running publish --apply"
                    )
                try:
                    schema = validate_publish_state_schema_version(raw)
                except SidecarSchemaError as exc:
                    raise PublishError(unsupported) from exc
                recheck = json.loads(path.read_bytes())
                try:
                    validate_publish_state_schema_version(recheck)
                except SidecarSchemaError as exc:
                    raise PublishError(unsupported) from exc
                if schema == _COPILOT_CHAT_PUBLISH_STATE_SCHEMA_V1:
                    backup = sidecar_backup_path(path)
                    write_owner_only_bytes_exclusive(backup, snapshot)
            write_owner_only_text_atomic(path, text, mode=0o600)
    except SidecarLockError as exc:
        raise PublishError(
            f"another publish operation holds {path.with_name(path.name + '.lock')}; "
            "refusing before writing publish state"
        ) from exc


def _transform_manifest_to_copilot_chat(
    manifest: dict,
    *,
    bot_id: str,
    manifest_id: str | None = None,
    app_version: str | None = None,
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
    if app_version is not None:
        out["version"] = _format_app_version(_parse_app_version_triple(app_version))
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


def _read_manifest_json_from_zip(zip_path: str) -> dict | None:
    zp = Path(zip_path)
    if not _regular_nonsymlink(zp):
        return None
    try:
        with zipfile.ZipFile(zp, "r") as zf:
            _validated_zip_infos(zf)
            with zf.open("manifest.json") as fh:
                manifest = json.load(fh)
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _catalog_manifest_id_for_copilot_chat(
    source_manifest: dict, requested_id: str | None
) -> str | None:
    if requested_id:
        try:
            return _canonical_manifest_guid(requested_id)
        except ValueError:
            return None
    top_id = source_manifest.get("id")
    if isinstance(top_id, str) and top_id.strip():
        try:
            return _canonical_manifest_guid(top_id)
        except ValueError:
            return None
    return None


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
    app_version: str | None = None,
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
    zp = Path(zip_path)
    if not _regular_nonsymlink(zp):
        return None
    try:
        with zipfile.ZipFile(zp, "r") as zf:
            infos = _validated_zip_infos(zf)
            names = [info.filename for info in infos]
            with zf.open("manifest.json") as fh:
                manifest = json.load(fh)
            sidecars = _agentic_template_sidecar_files(manifest)
            dropped_sidecars = sidecars.intersection(names)
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
        return None

    bot_id = bot_id_override or _extract_bot_id_from_manifest(manifest)
    if not bot_id:
        return None

    had_aut = "agenticUserTemplates" in manifest
    new_manifest = _transform_manifest_to_copilot_chat(
        manifest,
        bot_id=bot_id,
        manifest_id=manifest_id,
        app_version=app_version,
        distinguish_name_short=distinguish_name_short,
        scopes=scopes,
        prompt_starters=prompt_starters,
    )
    blob = json.dumps(new_manifest, indent=2).encode("utf-8")

    try:
        _rewrite_manifest_zip(zp, blob, drop=dropped_sidecars)
    except (OSError, ValueError, zipfile.BadZipFile):
        return None

    summary = {
        "manifest_version": _COPILOT_CHAT_MANIFEST_VERSION,
        "app_version": new_manifest.get("version"),
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
    hermes_home: Path | None = None,
) -> PublishResult:
    """Run ``a365 publish``; surface the produced artefact (zip or
    registered instance) appropriate to the flow."""
    from pathlib import Path

    if hermes_home is None:
        hermes_home = Path(os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes"))

    expected_tenant = (plan.inputs.tenant_id or "").strip()
    if not expected_tenant:
        raise PublishError(
            "no canonical tenant pin was resolved; pass --tenant-id or set "
            "A365_TENANT_ID before publishing"
        )
    # The a365 mutator refuses every argv, including `az account show`, when
    # the a365 binary is missing. That used to surface as a tenant-verification
    # failure (#123 F5).
    if getattr(mutator, "available", True) is False:
        raise PublishError(
            "a365 CLI not found on PATH. Install the Agent 365 CLI "
            "(dotnet tool install -g Microsoft.Agents.A365.DevTools.Cli --prerelease) "
            "and put its directory on PATH. publish --apply did not verify the "
            "Azure tenant because the a365 command runner rejects every "
            "invocation, including `az account show`, when that binary is missing."
        )
    active = active_az_tenant(lambda argv: mutator.run(argv))
    if active is None:
        raise PublishError("could not verify the active Azure tenant")
    try:
        matches = tenant_ids_match(active, expected_tenant, source="publish tenant pin")
    except TenantResolutionError as exc:
        raise PublishError(str(exc)) from exc
    if not matches:
        raise PublishError("active Azure tenant does not match the publish tenant pin")
    if plan.inputs.copilot_chat:
        _require_copilot_chat_slug(plan.inputs)
    publish_tenant_id = _normalize_publish_state_tenant_id(expected_tenant)
    publish_profile = _resolve_hermes_profile_name(plan.inputs.profile)
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
                if not emitted_zip:
                    raise PublishError(
                        "the CLI extracted a manifest directory, but it could not be "
                        "packaged safely; inspect it for a missing root manifest.json, "
                        "links, special files, excessive nesting, or size limits"
                    )
                messages.append(
                    "[apply] packaged extracted manifest template → "
                    f"{emitted_zip} (CLI no longer emits a zip directly)"
                )
        if emitted_zip:
            # Decide which file we end up calling the Copilot Chat zip.
            # - Both surfaces requested → copy the CLI-emitted zip aside
            #   to a sibling ``.copilot-chat.zip`` and transform the copy
            #   (the original stays as the AI Teammate zip).
            # - Copilot Chat only → transform the CLI-emitted zip in
            #   place; no AI Teammate zip is kept.
            # - AI Teammate only → no transform; keep emitted as-is.
            if plan.inputs.copilot_chat and plan.inputs.aiteammate:
                orig = Path(emitted_zip)
                sibling = orig.with_name(orig.stem + _COPILOT_CHAT_ZIP_INFIX + orig.suffix)
                _copy_regular_file_atomic(orig, sibling)
                copilot_chat_target: str | None = str(sibling)
                package_path = emitted_zip
            elif plan.inputs.copilot_chat:
                copilot_chat_target = emitted_zip
                package_path = None
            else:
                copilot_chat_target = None
                package_path = emitted_zip

            manifest_id_resolution: _CopilotChatManifestIdResolution | None = None
            if copilot_chat_target is not None:
                source_manifest = _read_manifest_json_from_zip(copilot_chat_target) or {}
                aiteammate_catalog_id = _catalog_manifest_id_for_copilot_chat(source_manifest, None)
                manifest_id_resolution = _resolve_copilot_chat_manifest_id_for_apply(
                    plan.inputs,
                    hermes_home=hermes_home,
                    tenant_id=publish_tenant_id,
                    profile=publish_profile,
                    source_manifest=source_manifest,
                )
                copilot_chat_manifest_id = manifest_id_resolution.manifest_id
                catalog_manifest_id = _catalog_manifest_id_for_copilot_chat(
                    source_manifest, copilot_chat_manifest_id
                )
                copilot_app_version: str | None = None
                publish_state_path: Path | None = None
                if plan.inputs.copilot_chat:
                    slug = _require_copilot_chat_slug(plan.inputs)
                    publish_state_path = _copilot_chat_publish_state_path(hermes_home, slug)
                    publish_state_record = _load_copilot_chat_publish_state_record(
                        publish_state_path,
                        hermes_home=hermes_home,
                        tenant_id=publish_tenant_id,
                        profile=publish_profile,
                    )
                    if publish_state_record is not None:
                        _refuse_unscoped_v1_publish_state(
                            publish_state_record,
                            path=publish_state_path,
                            app_version_override=plan.inputs.app_version,
                        )
                    if catalog_manifest_id:
                        _assert_publish_state_matches_catalog_id(
                            publish_state_path,
                            catalog_manifest_id=catalog_manifest_id,
                            hermes_home=hermes_home,
                            tenant_id=publish_tenant_id,
                            profile=publish_profile,
                            inputs=plan.inputs,
                            manifest_id_resolution=manifest_id_resolution,
                            aiteammate_catalog_id=aiteammate_catalog_id,
                        )
                    last_published = (
                        _read_copilot_chat_publish_state(
                            publish_state_path,
                            manifest_id=catalog_manifest_id,
                            hermes_home=hermes_home,
                            tenant_id=publish_tenant_id,
                            profile=publish_profile,
                        )
                        if catalog_manifest_id
                        else None
                    )
                    template_version = _parse_template_app_version(source_manifest)
                    if (
                        last_published is None
                        and plan.inputs.app_version is None
                        and not manifest_id_resolution.freshly_minted
                        and catalog_manifest_id is not None
                    ):
                        raise PublishError(
                            "no Copilot Chat publish record for catalog "
                            f"manifest.id {catalog_manifest_id}; pass --app-version "
                            "strictly greater than the app version shown in "
                            "Microsoft Admin Center (required for MAC updates when "
                            "the package was installed outside Hermes or local "
                            "state is missing)"
                        )
                    try:
                        copilot_app_version = _resolve_next_copilot_chat_app_version(
                            template_version=template_version,
                            last_published=last_published,
                            override=plan.inputs.app_version,
                        )
                    except ValueError as exc:
                        raise PublishError(str(exc)) from exc
                cc_result = _patch_manifest_to_copilot_chat(
                    copilot_chat_target,
                    bot_id_override=plan.inputs.bot_id,
                    manifest_id=copilot_chat_manifest_id,
                    app_version=copilot_app_version,
                    distinguish_name_short=(plan.inputs.aiteammate and plan.inputs.copilot_chat),
                    prompt_starters=plan.inputs.prompt_starters,
                )
                if cc_result is not None:
                    copilot_chat_bot_id, summary = cc_result
                    copilot_chat_manifest_id = summary.get("manifest_id")
                    copilot_chat_package_path = copilot_chat_target
                    dropped = (
                        " (dropped agenticUserTemplates)"
                        if summary["dropped_agentic_user_templates"]
                        else ""
                    )
                    messages.append(
                        f"[apply] transformed to Custom Engine Agent: "
                        f"manifestVersion={summary['manifest_version']}, "
                        f"appVersion={summary.get('app_version')}, "
                        f"botId={copilot_chat_bot_id}, "
                        f"manifestId={summary['manifest_id']}, "
                        f"scopes={summary['scopes']}" + dropped
                    )
                    if manifest_id_resolution and manifest_id_resolution.reused_from_state:
                        if plan.inputs.manifest_id == "auto":
                            reuse_label = "--manifest-id auto"
                        else:
                            reuse_label = "combined --aiteammate --copilot-chat default"
                        messages.append(
                            "[apply] reusing catalog manifest.id from "
                            f"{publish_state_path} ({reuse_label})"
                        )
                    if (
                        publish_state_path is not None
                        and summary.get("manifest_id")
                        and summary.get("app_version")
                    ):
                        if manifest_id_resolution and manifest_id_resolution.freshly_minted:
                            cea_minted = True
                        elif (
                            manifest_id_resolution
                            and manifest_id_resolution.reused_from_state
                            and publish_state_record is not None
                        ):
                            cea_minted = bool(publish_state_record.get("ceaCatalogIdMinted"))
                        else:
                            cea_minted = False
                        _write_copilot_chat_publish_state(
                            publish_state_path,
                            manifest_id=str(summary["manifest_id"]),
                            app_version=str(summary["app_version"]),
                            hermes_home=hermes_home,
                            tenant_id=publish_tenant_id,
                            profile=publish_profile,
                            cea_catalog_id_minted=cea_minted,
                        )
                        messages.append(
                            f"[apply] recorded last published app version in {publish_state_path}"
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
            messages.append(f"[apply] Copilot Chat package: {copilot_chat_package_path}")
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
        help=TENANT_ID_HELP,
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
            "Chat zip; pass 'auto' for a distinct CEA catalog id (reuses a "
            "prior auto-minted id from copilot-chat-publish.json for the same "
            "tenant and Hermes profile when present). Defaults to auto when "
            "--aiteammate and --copilot-chat are combined. Do not use auto on "
            "Path-B-only publishes you update in place — omit the flag and keep "
            "the stable manifest.id from the zip"
        ),
    )
    parser.add_argument(
        "--app-version",
        help=(
            "override the Teams app manifest version in the Copilot Chat zip; "
            "required when there is no matching publish record for this catalog "
            "manifest.id (MAC updates without local state). Must be strictly "
            "greater than the last published version recorded in "
            f"$HERMES_HOME/agents/<slug>/{_COPILOT_CHAT_PUBLISH_STATE_FILENAME} "
            "for the same tenant, profile, and manifest.id"
        ),
    )
    parser.add_argument(
        "--slug",
        help=(
            "local agent slug for ~/.hermes/agents/<slug>/ (publish version "
            "state); defaults to slugify(--agent-name)"
        ),
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help=(
            "Hermes gateway profile name for copilot-chat-publish.json "
            "partitioning (must match the profile used with "
            "'hermes gateway run --profile NAME', or set HERMES_PROFILE / "
            "HERMES_GATEWAY_PROFILE); defaults to 'default'"
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
        raise ValueError("--prompt-starter needs a non-empty title (format: 'Title|Prompt text')")
    return {"title": title[:128], "prompt": prompt[:4000]}


def run(args: argparse.Namespace) -> int:
    try:
        hermes_home = Path(os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes"))
        tenant_id, _source = resolve_expected_tenant(hermes_home, args.tenant_id)
        if tenant_id is None:
            raise ValueError(
                "no canonical tenant pin was resolved; pass --tenant-id or set A365_TENANT_ID"
            )
        inputs = PublishInputs(
            agent_name=args.agent_name,
            tenant_id=tenant_id,
            aiteammate=args.aiteammate,
            copilot_chat=args.copilot_chat,
            bot_id=args.bot_id,
            manifest_id=args.manifest_id,
            app_version=args.app_version,
            slug=args.slug,
            profile=getattr(args, "profile", None),
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
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2
    except CliInvocationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except PublishError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
