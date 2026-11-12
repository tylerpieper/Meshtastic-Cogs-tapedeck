"""
PostsAsCode — Sync Discord channels and plain threads from a Git repository.

Treats a Git repository as the authoritative source of truth for static
documentation channels and threads (rules, getting-started guides, FAQs, etc.).
Supports ordered Markdown (.md) and standalone image files, edit-in-place slot
syncing, delete-and-repost syncing, and full channel resets.
"""

import asyncio
import hashlib
import logging
import os
import re
import shutil
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.meshtastic_cogs.posts_as_code")

# ── Constants ────────────────────────────────────────────────────────────────

MAX_MESSAGE_LENGTH = 2000
TEXT_EXTENSIONS = {".md"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | IMAGE_EXTENSIONS
VALID_SYNC_MODES = ("edit", "repost", "reset")
API_DELAY_SECONDS = 0.25
CONFIRM_TIMEOUT_SECONDS = 60

TargetChannel = Union[discord.TextChannel, discord.Thread]


# ── Exceptions & Data Models ─────────────────────────────────────────────────


class PostsAsCodeError(Exception):
    """Base user-facing error for PostsAsCode."""


class PartialSyncError(PostsAsCodeError):
    """Raised when a sync fails mid-way after already modifying Discord state."""

    def __init__(self, message: str, partial_slots: list[dict]) -> None:
        super().__init__(message)
        self.partial_slots = partial_slots


@dataclass
class DesiredSlot:
    """Represents a single target Discord message generated from a repo file."""

    kind: str  # "text" | "image"
    source: str  # Relative filename, e.g. "01-rules.md"
    content_hash: str
    content: str = ""
    file_path: Optional[Path] = None
    chunk_index: int = 1
    chunk_total: int = 1


@dataclass
class FolderScanResult:
    """Result of scanning a mapped repository folder."""

    slots: list[DesiredSlot]
    oversized_markdown: list[tuple[str, int, int]]  # (filename, char_len, chunk_count)
    oversized_images: list[tuple[str, int]]  # (filename, byte_size)
    empty_files: list[str]


@dataclass
class SyncStats:
    """Per-target sync execution summary."""

    target_id: int
    repo_path: str
    mode: str
    unchanged: int = 0
    edited: int = 0
    created: int = 0
    deleted: int = 0
    purged: int = 0


# ── Pure Helpers (Testable without Discord) ──────────────────────────────────


def normalize_markdown(raw_text: str) -> str:
    """Normalize line endings and strip leading/trailing whitespace."""
    return raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()


def sha256_text(text: str) -> str:
    """Return hex SHA-256 digest for normalized UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return hex SHA-256 digest for raw bytes."""
    return hashlib.sha256(data).hexdigest()


def split_markdown(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """
    Split markdown text into chunks no longer than ``limit`` characters.

    Prefers splitting on paragraph boundaries (``\\n\\n``), then single lines
    (``\\n``), then whitespace, falling back to hard slices only for unbroken
    lines longer than ``limit``. Tracks open fenced code blocks (`` ``` `` or
    ``~~~``) so code fences are closed and reopened cleanly across chunks.
    """
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text
    active_fence: Optional[str] = None  # e.g. "```python" or "~~~"

    while remaining:
        prefix = f"{active_fence}\n" if active_fence else ""

        if len(prefix) + len(remaining) <= limit:
            chunks.append(prefix + remaining)
            break

        max_body = limit - len(prefix) - len("\n~~~")  # Safe upper bound if fence opens mid-chunk
        if max_body <= 0:
            max_body = limit - len(prefix)

        window = remaining[:max_body]
        split_at = _find_split_index(window)

        part = remaining[:split_at].rstrip(" \t\r")
        trimmed = False
        if not part:
            # Handle leading whitespace/blank-line edge case by advancing cleanly.
            split_at = max(1, min(max_body, len(remaining)))
            part = remaining[:split_at]

        chunk_text = prefix + part
        next_fence = _track_code_fence(chunk_text)
        if next_fence:
            close_seq = f"\n{next_fence[:3]}"
            if len(chunk_text) + len(close_seq) > limit:
                trim_by = len(chunk_text) + len(close_seq) - limit
                part = part[:-trim_by].rstrip()
                trimmed = True
                chunk_text = prefix + part
                next_fence = _track_code_fence(chunk_text)
            if next_fence:
                chunk_text = f"{chunk_text}\n{next_fence[:3]}"

        chunks.append(chunk_text)
        active_fence = next_fence
        consumed = len(part) if trimmed else split_at
        remaining = remaining[consumed:].lstrip("\n")
        if not active_fence:
            remaining = remaining.lstrip(" ")

    return chunks


def _find_split_index(window: str) -> int:
    """Choose the cleanest split offset within ``window``."""
    min_acceptable = max(1, len(window) // 3)

    para_idx = window.rfind("\n\n")
    if para_idx >= min_acceptable:
        return para_idx

    line_idx = window.rfind("\n")
    if line_idx >= min_acceptable:
        return line_idx

    space_idx = window.rfind(" ")
    if space_idx >= min_acceptable:
        return space_idx

    return len(window)


def _track_code_fence(text: str) -> Optional[str]:
    """Return the active opening code fence header if ``text`` leaves a fence open."""
    fence_open: Optional[str] = None
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if fence_open is None:
                fence_open = stripped.rstrip()
            elif fence_open.startswith(marker):
                fence_open = None
    return fence_open


def validate_repo_path(repo_dir: Path, rel_path: str) -> Path:
    """
    Resolve ``rel_path`` inside ``repo_dir`` while preventing path traversal.

    Raises ``PostsAsCodeError`` if the path is invalid, points to ``.git``,
    or escapes ``repo_dir``.
    """
    cleaned = rel_path.strip().replace("\\", "/")
    if "\x00" in cleaned:
        raise PostsAsCodeError("Repository path contains invalid null bytes.")
    if cleaned.startswith("-"):
        raise PostsAsCodeError("Repository path cannot start with '-'.")

    cleaned = cleaned.strip("/")
    repo_root = repo_dir.resolve()
    target = (repo_root / cleaned).resolve() if cleaned and cleaned != "." else repo_root

    if not target.is_relative_to(repo_root):
        raise PostsAsCodeError(f"Path `{rel_path}` escapes the repository root.")

    rel_parts = target.relative_to(repo_root).parts
    if ".git" in rel_parts:
        raise PostsAsCodeError("Path cannot point inside `.git`.")

    return target


def scan_folder(
    repo_dir: Path,
    rel_path: str,
    filesize_limit: int,
) -> FolderScanResult:
    """
    Scan ``rel_path`` in ``repo_dir`` for ordered Markdown and image files.

    Files are sorted lexicographically (case-insensitive). Returns desired
    message slots alongside any oversized markdown/image diagnostics.
    """
    folder = validate_repo_path(repo_dir, rel_path)
    if not folder.exists() or not folder.is_dir():
        raise PostsAsCodeError(
            f"Mapped folder `{rel_path}` does not exist in the repository."
        )

    files = [
        entry
        for entry in folder.iterdir()
        if entry.is_file()
        and not entry.name.startswith(".")
        and entry.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    files.sort(key=lambda p: (p.name.lower(), p.name))

    if not files:
        raise PostsAsCodeError(
            f"Mapped folder `{rel_path}` contains no supported `.md` or image files."
        )

    slots: list[DesiredSlot] = []
    oversized_markdown: list[tuple[str, int, int]] = []
    oversized_images: list[tuple[str, int]] = []
    empty_files: list[str] = []

    for file_path in files:
        suffix = file_path.suffix.lower()
        if suffix in TEXT_EXTENSIONS:
            try:
                raw_text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise PostsAsCodeError(
                    f"File `{file_path.name}` is not valid UTF-8: {exc}"
                ) from exc

            normalized = normalize_markdown(raw_text)
            if not normalized:
                empty_files.append(file_path.name)
                continue

            if len(normalized) > MAX_MESSAGE_LENGTH:
                chunks = split_markdown(normalized, MAX_MESSAGE_LENGTH)
                oversized_markdown.append((file_path.name, len(normalized), len(chunks)))
            else:
                chunks = [normalized]

            total = len(chunks)
            for idx, chunk in enumerate(chunks, start=1):
                slots.append(
                    DesiredSlot(
                        kind="text",
                        source=file_path.name,
                        content_hash=sha256_text(chunk),
                        content=chunk,
                        chunk_index=idx,
                        chunk_total=total,
                    )
                )
        elif suffix in IMAGE_EXTENSIONS:
            size = file_path.stat().st_size
            if size == 0:
                empty_files.append(file_path.name)
                continue
            if size > filesize_limit:
                oversized_images.append((file_path.name, size))
                slots.append(
                    DesiredSlot(
                        kind="image",
                        source=file_path.name,
                        content_hash="oversized",
                        file_path=file_path,
                    )
                )
                continue

            raw_bytes = file_path.read_bytes()
            slots.append(
                DesiredSlot(
                    kind="image",
                    source=file_path.name,
                    content_hash=sha256_bytes(raw_bytes),
                    file_path=file_path,
                )
            )

    if not slots and not oversized_images:
        raise PostsAsCodeError(
            f"Mapped folder `{rel_path}` has no non-empty posts to publish."
        )

    return FolderScanResult(
        slots=slots,
        oversized_markdown=oversized_markdown,
        oversized_images=oversized_images,
        empty_files=empty_files,
    )


def compute_slot_diff(
    existing_slots: list[dict],
    desired_slots: list[DesiredSlot],
) -> tuple[int, int, int, int]:
    """
    Compare stored ``existing_slots`` against ``desired_slots`` for edit-mode diffing.

    Returns ``(unchanged, edited, created, deleted)``.
    """
    unchanged = 0
    edited = 0
    paired = min(len(existing_slots), len(desired_slots))

    for i in range(paired):
        prev = existing_slots[i]
        curr = desired_slots[i]
        if prev.get("kind") == curr.kind and prev.get("hash") == curr.content_hash:
            unchanged += 1
        else:
            edited += 1

    created = max(0, len(desired_slots) - len(existing_slots))
    deleted = max(0, len(existing_slots) - len(desired_slots))
    return unchanged, edited, created, deleted


def build_git_auth_args(repo_url: str, token: Optional[str]) -> tuple[list[str], list[str]]:
    """
    Build in-memory ``git -c`` authentication arguments for HTTPS repositories.

    Returns ``(git_config_args, secret_values_to_scrub)``.
    """
    if not token:
        return [], []

    parsed = urllib.parse.urlsplit(repo_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return [], [token]

    host = parsed.netloc.rsplit("@", 1)[-1]
    quoted_token = urllib.parse.quote(token, safe="")
    instead_of = (
        f"url.{parsed.scheme}://oauth2:{quoted_token}@{host}/.insteadOf="
        f"{parsed.scheme}://{host}/"
    )
    return ["-c", instead_of], [token, quoted_token]


def scrub_secrets(text: str, secrets: list[str]) -> str:
    """Redact any secret tokens from command output before displaying to users."""
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


# ── Cog ──────────────────────────────────────────────────────────────────────


class PostsAsCode(commands.Cog):
    """
    Manage Discord channel and thread documentation from a Git repository.

    Map Discord text channels or plain threads to folders in a Git repository
    containing ordered Markdown (``.md``) and image files, then sync via
    edit-in-place, delete-and-repost, or full channel reset.
    """

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=7392048156, force_registration=True
        )
        self.config.register_guild(
            repo_url=None,
            branch="main",
            token=None,
            last_commit=None,
            mappings={},
        )
        self._guild_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    def _repo_dir(self, guild_id: int) -> Path:
        return cog_data_path(self) / "repos" / str(guild_id)

    # ── Git Operations ───────────────────────────────────────────────────────

    async def _run_git(
        self,
        args: list[str],
        cwd: Optional[Path] = None,
        git_config_args: Optional[list[str]] = None,
        secrets: Optional[list[str]] = None,
    ) -> str:
        git_bin = shutil.which("git")
        if not git_bin:
            raise PostsAsCodeError(
                "`git` is not installed or not on PATH on the bot host."
            )

        cmd = [git_bin, *(git_config_args or []), *args]
        env = {"GIT_TERMINAL_PROMPT": "0"}

        # Preserve minimal OS environment variables needed by git/SSL/HOME.
        for key in (
            "PATH",
            "HOME",
            "USERPROFILE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "SSH_AUTH_SOCK",
            "GIT_SSH_COMMAND",
        ):
            if key in os.environ:
                env[key] = os.environ[key]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout_b, stderr_b = await proc.communicate()
        stdout = scrub_secrets(stdout_b.decode("utf-8", errors="replace"), secrets or [])
        stderr = scrub_secrets(stderr_b.decode("utf-8", errors="replace"), secrets or [])

        if proc.returncode != 0:
            detail = (stderr or stdout).strip() or f"exit code {proc.returncode}"
            raise PostsAsCodeError(f"Git command failed: {detail}")

        return stdout.strip()

    async def _sync_git_repo(self, guild: discord.Guild) -> str:
        """Clone or fast-forward/reset the guild's repository clone; return short SHA."""
        repo_url = await self.config.guild(guild).repo_url()
        branch = await self.config.guild(guild).branch()
        token = await self.config.guild(guild).token()

        if not repo_url:
            raise PostsAsCodeError(
                "No repository URL configured. Use `[p]pac repo <url> [branch]` first."
            )
        if repo_url.startswith("-") or branch.startswith("-"):
            raise PostsAsCodeError("Invalid repository URL or branch name.")

        repo_dir = self._repo_dir(guild.id)
        git_cfg, secrets = build_git_auth_args(repo_url, token)

        if (repo_dir / ".git").is_dir():
            try:
                current_url = await self._run_git(
                    ["remote", "get-url", "origin"], cwd=repo_dir, secrets=secrets
                )
            except PostsAsCodeError:
                current_url = ""

            if current_url != repo_url:
                await self._run_git(
                    ["remote", "set-url", "origin", repo_url],
                    cwd=repo_dir,
                    secrets=secrets,
                )

            await self._run_git(
                ["fetch", "--depth", "1", "origin", branch],
                cwd=repo_dir,
                git_config_args=git_cfg,
                secrets=secrets,
            )
            await self._run_git(
                ["reset", "--hard", "FETCH_HEAD"], cwd=repo_dir, secrets=secrets
            )
            await self._run_git(["clean", "-fd"], cwd=repo_dir, secrets=secrets)
        else:
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            repo_dir.mkdir(parents=True, exist_ok=True)
            await self._run_git(
                [
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    branch,
                    "--single-branch",
                    repo_url,
                    str(repo_dir),
                ],
                git_config_args=git_cfg,
                secrets=secrets,
            )

        commit_sha = await self._run_git(
            ["rev-parse", "--short", "HEAD"], cwd=repo_dir, secrets=secrets
        )
        await self.config.guild(guild).last_commit.set(commit_sha)
        return commit_sha

    # ── Discord Helpers ──────────────────────────────────────────────────────

    async def _resolve_target(
        self, guild: discord.Guild, target_id: int
    ) -> TargetChannel:
        channel = guild.get_channel_or_thread(target_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(target_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                raise PostsAsCodeError(
                    f"Target channel/thread `{target_id}` could not be accessed: {exc}"
                ) from exc

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise PostsAsCodeError(
                f"Target `{target_id}` is not a text channel or plain thread."
            )
        return channel

    async def _ensure_thread_open(self, target: TargetChannel) -> None:
        if isinstance(target, discord.Thread) and target.archived:
            try:
                await target.edit(archived=False, reason="PostsAsCode sync")
            except discord.HTTPException as exc:
                raise PostsAsCodeError(
                    f"Failed to unarchive thread {target.mention}: {exc}"
                ) from exc

    def _check_permissions(
        self,
        target: TargetChannel,
        needs_attachments: bool,
        needs_manage_messages: bool,
        needs_history: bool,
    ) -> None:
        perms = target.permissions_for(target.guild.me)
        missing: list[str] = []

        if not perms.view_channel:
            missing.append("View Channel")
        if isinstance(target, discord.Thread):
            if not perms.send_messages_in_threads:
                missing.append("Send Messages in Threads")
            if target.archived and not perms.manage_threads:
                missing.append("Manage Threads (to unarchive)")
        else:
            if not perms.send_messages:
                missing.append("Send Messages")

        if needs_attachments and not perms.attach_files:
            missing.append("Attach Files")
        if needs_history and not perms.read_message_history:
            missing.append("Read Message History")
        if needs_manage_messages and not perms.manage_messages:
            missing.append("Manage Messages")

        if missing:
            raise PostsAsCodeError(
                f"Missing permissions in {target.mention}: {', '.join(missing)}."
            )

    async def _send_chunked(
        self,
        ctx: commands.Context,
        text: str,
        delete_after: Optional[float] = None,
    ) -> None:
        """Send potentially long status text split safely at <= 2,000 chars per message."""
        for page in pagify(text, delims=["\n"], page_length=1950):
            await ctx.send(page, delete_after=delete_after)

    @staticmethod
    def _target_label(guild: discord.Guild, target_id: Union[int, str]) -> str:
        chan = guild.get_channel_or_thread(int(target_id))
        return chan.mention if chan else f"`{target_id}`"

    async def _confirm_prompt(self, ctx: commands.Context, prompt: str) -> bool:
        """Ask ``ctx.author`` for yes/no confirmation in ``ctx.channel``."""
        await self._send_chunked(ctx, prompt)

        def check(msg: discord.Message) -> bool:
            return (
                msg.author.id == ctx.author.id
                and msg.channel.id == ctx.channel.id
                and msg.content.strip().lower() in ("y", "yes", "n", "no")
            )

        try:
            reply = await self.bot.wait_for(
                "message", check=check, timeout=CONFIRM_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await ctx.send("Confirmation timed out. Cancelled with no changes made.")
            return False

        return reply.content.strip().lower() in ("y", "yes")

    async def _send_slot(
        self, target: TargetChannel, slot: DesiredSlot
    ) -> discord.Message:
        if slot.kind == "text":
            return await target.send(content=slot.content)
        if slot.kind == "image" and slot.file_path is not None:
            return await target.send(file=discord.File(slot.file_path))
        raise PostsAsCodeError(f"Unsupported slot kind `{slot.kind}`.")

    async def _edit_slot(
        self, msg: discord.Message, slot: DesiredSlot
    ) -> discord.Message:
        if slot.kind == "text":
            return await msg.edit(content=slot.content, attachments=[])
        if slot.kind == "image" and slot.file_path is not None:
            return await msg.edit(content="", attachments=[discord.File(slot.file_path)])
        raise PostsAsCodeError(f"Unsupported slot kind `{slot.kind}`.")

    async def _purge_all_messages(self, target: TargetChannel) -> int:
        """Delete all deletable messages in ``target`` (bot and non-bot)."""

        def can_delete(msg: discord.Message) -> bool:
            if msg.id == target.id:
                return False
            if msg.type == discord.MessageType.thread_starter_message:
                return False
            return True

        deleted = await target.purge(limit=None, check=can_delete, bulk=True)
        return len(deleted)

    # ── Sync Engine ──────────────────────────────────────────────────────────

    async def _apply_sync_to_target(
        self,
        guild: discord.Guild,
        target_id: int,
        mapping: dict,
        scan: FolderScanResult,
        mode: str,
    ) -> tuple[SyncStats, list[dict]]:
        target = await self._resolve_target(guild, target_id)

        has_images = any(s.kind == "image" for s in scan.slots)
        existing_slots: list[dict] = list(mapping.get("slots", []))

        self._check_permissions(
            target=target,
            needs_attachments=has_images,
            needs_manage_messages=(mode == "reset"),
            needs_history=(mode in ("edit", "reset")),
        )
        await self._ensure_thread_open(target)

        stats = SyncStats(
            target_id=target_id,
            repo_path=mapping["path"],
            mode=mode,
        )
        new_slots: list[dict] = []

        if mode == "reset":
            try:
                stats.purged = await self._purge_all_messages(target)
                for slot in scan.slots:
                    sent = await self._send_slot(target, slot)
                    new_slots.append(self._slot_record(sent.id, slot))
                    stats.created += 1
                    await asyncio.sleep(API_DELAY_SECONDS)
            except discord.HTTPException as exc:
                raise PartialSyncError(str(exc), new_slots) from exc

        elif mode == "repost":
            remaining_old = list(existing_slots)
            try:
                for idx, prev in enumerate(existing_slots):
                    msg_id = prev.get("message_id")
                    if msg_id:
                        try:
                            msg = target.get_partial_message(msg_id)
                            await msg.delete()
                            stats.deleted += 1
                            await asyncio.sleep(API_DELAY_SECONDS)
                        except discord.NotFound:
                            pass
                    remaining_old = existing_slots[idx + 1 :]

                for slot in scan.slots:
                    sent = await self._send_slot(target, slot)
                    new_slots.append(self._slot_record(sent.id, slot))
                    stats.created += 1
                    await asyncio.sleep(API_DELAY_SECONDS)
            except discord.HTTPException as exc:
                raise PartialSyncError(str(exc), new_slots + remaining_old) from exc

        else:  # mode == "edit"
            surviving: list[tuple[dict, discord.Message]] = []
            for prev in existing_slots:
                msg_id = prev.get("message_id")
                if not msg_id:
                    continue
                try:
                    msg = await target.fetch_message(msg_id)
                    surviving.append((prev, msg))
                except discord.NotFound:
                    continue

            processed_surviving = 0
            try:
                for idx, desired in enumerate(scan.slots):
                    if idx < len(surviving):
                        prev_meta, msg = surviving[idx]
                        processed_surviving = idx + 1
                        if (
                            prev_meta.get("kind") == desired.kind
                            and prev_meta.get("hash") == desired.content_hash
                        ):
                            new_slots.append(self._slot_record(msg.id, desired))
                            stats.unchanged += 1
                            continue

                        edited_msg = await self._edit_slot(msg, desired)
                        new_slots.append(self._slot_record(edited_msg.id, desired))
                        stats.edited += 1
                        await asyncio.sleep(API_DELAY_SECONDS)
                    else:
                        sent = await self._send_slot(target, desired)
                        new_slots.append(self._slot_record(sent.id, desired))
                        stats.created += 1
                        await asyncio.sleep(API_DELAY_SECONDS)

                for extra_meta, extra_msg in surviving[len(scan.slots) :]:
                    try:
                        await extra_msg.delete()
                        stats.deleted += 1
                        await asyncio.sleep(API_DELAY_SECONDS)
                    except discord.NotFound:
                        pass
                    processed_surviving += 1
            except discord.HTTPException as exc:
                unvisited = [meta for meta, _ in surviving[processed_surviving:]]
                raise PartialSyncError(str(exc), new_slots + unvisited) from exc

        return stats, new_slots

    @staticmethod
    def _slot_record(message_id: int, slot: DesiredSlot) -> dict:
        return {
            "message_id": message_id,
            "kind": slot.kind,
            "hash": slot.content_hash,
            "source": slot.source,
        }

    # ── Commands ─────────────────────────────────────────────────────────────

    @commands.group(name="postsascode", aliases=["pac"], invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def postsascode(self, ctx: commands.Context) -> None:
        """Manage Discord channel and thread documentation from a Git repository."""
        await ctx.send_help(ctx.command)

    @postsascode.command(name="repo")
    async def pac_repo(
        self, ctx: commands.Context, url: str, branch: str = "main"
    ) -> None:
        """Configure the Git repository URL and branch for this server."""
        cleaned_url = url.strip()
        cleaned_branch = branch.strip()

        if cleaned_url.startswith("-") or not re.match(
            r"^(https?://|git@|ssh://)", cleaned_url
        ):
            await ctx.send(
                "Please provide a valid `https://`, `http://`, `ssh://`, or `git@` repository URL."
            )
            return
        if (
            not cleaned_branch
            or cleaned_branch.startswith("-")
            or ".." in cleaned_branch
            or not re.match(r"^[A-Za-z0-9._/-]+$", cleaned_branch)
        ):
            await ctx.send("Please provide a valid branch name.")
            return

        async with self._get_lock(ctx.guild.id):
            await self.config.guild(ctx.guild).repo_url.set(cleaned_url)
            await self.config.guild(ctx.guild).branch.set(cleaned_branch)

        await ctx.send(
            f"Repository configured:\n• **URL:** `{cleaned_url}`\n• **Branch:** `{cleaned_branch}`"
        )

    @postsascode.command(name="token")
    async def pac_token(
        self, ctx: commands.Context, token: Optional[str] = None
    ) -> None:
        """
        Set or clear an access token for private HTTPS repositories.

        Omit the token argument to clear any saved token.
        """
        if ctx.guild is not None:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

        async with self._get_lock(ctx.guild.id):
            await self.config.guild(ctx.guild).token.set(token.strip() if token else None)

        if token:
            await ctx.send("Repository access token saved securely.")
        else:
            await ctx.send("Repository access token cleared.")

    @staticmethod
    def _sanitize_mapping_path(repo_path: str) -> Optional[str]:
        cleaned = repo_path.strip().replace("\\", "/").strip("/") or "."
        parts = Path(cleaned).parts
        if (
            "\x00" in cleaned
            or cleaned.startswith("-")
            or ".." in parts
            or ".git" in parts
        ):
            return None
        return cleaned

    @postsascode.command(name="map")
    async def pac_map(
        self,
        ctx: commands.Context,
        target: TargetChannel,
        repo_path: str,
    ) -> None:
        """Associate a text channel or plain thread with a folder in the repository."""
        cleaned = self._sanitize_mapping_path(repo_path)
        if cleaned is None:
            await ctx.send("Invalid repository path.")
            return

        async with self._get_lock(ctx.guild.id):
            async with self.config.guild(ctx.guild).mappings() as mappings:
                key = str(target.id)
                existing = mappings.get(key, {})
                mappings[key] = {
                    "path": cleaned,
                    "slots": existing.get("slots", []),
                    "last_synced": existing.get("last_synced"),
                }

        await ctx.send(
            f"Mapped {target.mention} (`{target.id}`) to `{cleaned}`.\n"
            f"Run `{ctx.clean_prefix}pac sync {target.mention}` to publish."
        )

    @postsascode.command(name="mkthread")
    async def pac_mkthread(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        repo_path: str,
        *,
        thread_name: str,
    ) -> None:
        """Create a new plain public thread in a channel and map it to a repo folder."""
        cleaned = self._sanitize_mapping_path(repo_path)
        if cleaned is None:
            await ctx.send("Invalid repository path.")
            return

        perms = channel.permissions_for(ctx.guild.me)
        if not perms.create_public_threads or not perms.send_messages_in_threads:
            await ctx.send(
                f"I need **Create Public Threads** and **Send Messages in Threads** in {channel.mention}."
            )
            return

        try:
            thread = await channel.create_thread(
                name=thread_name.strip()[:100],
                type=discord.ChannelType.public_thread,
                reason=f"PostsAsCode mapped thread by {ctx.author}",
            )
        except discord.HTTPException as exc:
            await ctx.send(f"Failed to create thread: {exc}")
            return

        async with self._get_lock(ctx.guild.id):
            async with self.config.guild(ctx.guild).mappings() as mappings:
                mappings[str(thread.id)] = {
                    "path": cleaned,
                    "slots": [],
                    "last_synced": None,
                }

        await ctx.send(
            f"Created thread {thread.mention} and mapped it to `{cleaned}`.\n"
            f"Run `{ctx.clean_prefix}pac sync {thread.mention}` to publish."
        )

    @postsascode.command(name="unmap")
    async def pac_unmap(self, ctx: commands.Context, target: TargetChannel) -> None:
        """Remove a channel or thread mapping (existing Discord posts are kept)."""
        async with self._get_lock(ctx.guild.id):
            async with self.config.guild(ctx.guild).mappings() as mappings:
                key = str(target.id)
                if key not in mappings:
                    await ctx.send(f"{target.mention} is not currently mapped.")
                    return
                removed = mappings.pop(key)

        await ctx.send(
            f"Unmapped {target.mention} (was `{removed.get('path')}`). "
            "Existing messages in Discord were not deleted."
        )

    @postsascode.command(name="list")
    async def pac_list(self, ctx: commands.Context) -> None:
        """Show the configured repository and all channel/thread mappings."""
        cfg = await self.config.guild(ctx.guild).all()
        repo_url = cfg.get("repo_url") or "*Not configured*"
        branch = cfg.get("branch", "main")
        has_token = "Yes" if cfg.get("token") else "No"
        last_commit = cfg.get("last_commit") or "*Never synced*"
        mappings: dict = cfg.get("mappings", {})

        lines = [
            f"**Repository:** {repo_url}",
            f"**Branch:** `{branch}` | **Token set:** {has_token} | **Last commit:** `{last_commit}`",
            "",
        ]

        if not mappings:
            lines.append("No channels or threads mapped yet.")
        else:
            lines.append("**Mappings:**")
            for target_id_str, info in mappings.items():
                target = ctx.guild.get_channel_or_thread(int(target_id_str))
                mention = target.mention if target else f"`{target_id_str}` (missing/uncached)"
                slot_count = len(info.get("slots", []))
                last_synced = info.get("last_synced") or "never"
                lines.append(
                    f"• {mention} → `{info.get('path')}` "
                    f"({slot_count} tracked post(s), last synced: {last_synced})"
                )

        await self._send_chunked(ctx, "\n".join(lines))

    @postsascode.command(name="diff")
    async def pac_diff(
        self, ctx: commands.Context, target: Optional[TargetChannel] = None
    ) -> None:
        """Pull the repository and preview what `[p]pac sync` would change."""
        async with self._get_lock(ctx.guild.id):
            mappings = await self.config.guild(ctx.guild).mappings()
            if not mappings:
                await ctx.send("No channels or threads are mapped.")
                return

            selected = self._select_mappings(mappings, target)
            if not selected:
                await ctx.send(f"{target.mention} is not mapped.")
                return

            async with ctx.typing():
                try:
                    commit_sha = await self._sync_git_repo(ctx.guild)
                except PostsAsCodeError as exc:
                    await ctx.send(f"❌ {exc}")
                    return

                repo_dir = self._repo_dir(ctx.guild.id)
                report_lines = [f"**Diff preview @ `{commit_sha}`**"]

                for tid, info in selected.items():
                    label = self._target_label(ctx.guild, tid)
                    try:
                        scan = scan_folder(
                            repo_dir, info["path"], ctx.guild.filesize_limit
                        )
                    except PostsAsCodeError as exc:
                        report_lines.append(f"• {label} (`{info['path']}`): ❌ {exc}")
                        continue

                    unchanged, edited, created, deleted = compute_slot_diff(
                        info.get("slots", []), scan.slots
                    )
                    summary = (
                        f"• {label} (`{info['path']}`): "
                        f"{unchanged} unchanged, {edited} edit(s), "
                        f"{created} new, {deleted} delete(s)"
                    )
                    if scan.oversized_markdown:
                        details = ", ".join(
                            f"`{name}` ({chars} chars → {parts} posts)"
                            for name, chars, parts in scan.oversized_markdown
                        )
                        summary += f"\n  ⚠️ Over 2,000 chars: {details}"
                    if scan.oversized_images:
                        details = ", ".join(
                            f"`{name}` ({size // 1024} KB)"
                            for name, size in scan.oversized_images
                        )
                        summary += f"\n  ❌ Image exceeds server upload limit: {details}"
                    report_lines.append(summary)

        await self._send_chunked(ctx, "\n".join(report_lines))

    @postsascode.command(name="sync")
    async def pac_sync(
        self,
        ctx: commands.Context,
        target: Optional[TargetChannel] = None,
        mode: str = "edit",
    ) -> None:
        """
        Pull the repository and sync mapped channel(s)/thread(s).

        Modes:
        - ``edit`` (default): Edit posts in place by order, append/delete at the end.
        - ``repost``: Delete tracked bot posts and repost fresh in order.
        - ``reset``: Requires a specific target; purges ALL messages (including non-bot) and reposts.
        """
        mode_lower = mode.strip().lower()
        if mode_lower not in VALID_SYNC_MODES:
            await ctx.send(
                f"Invalid sync mode `{mode}`. Valid modes: `edit`, `repost`, `reset`.\n"
                "(If you meant a channel or thread, double-check the channel mention or ID.)"
            )
            return

        if mode_lower == "reset" and target is None:
            await ctx.send(
                f"For safety, `reset` mode requires specifying a mapped channel or thread "
                f"(e.g. `{ctx.clean_prefix}pac reset #channel`)."
            )
            return

        await self._execute_sync(ctx, target=target, mode=mode_lower)

    @postsascode.command(name="reset")
    async def pac_reset(self, ctx: commands.Context, target: TargetChannel) -> None:
        """
        Completely purge ALL messages in a mapped channel/thread and repost from Git.

        Only allowed on channels or threads that already have a mapping.
        """
        await self._execute_sync(ctx, target=target, mode="reset")

    def _select_mappings(
        self, mappings: dict, target: Optional[TargetChannel]
    ) -> dict[str, dict]:
        if target is None:
            return dict(mappings)
        key = str(target.id)
        if key not in mappings:
            return {}
        return {key: mappings[key]}

    async def _execute_sync(
        self,
        ctx: commands.Context,
        target: Optional[TargetChannel],
        mode: str,
    ) -> None:
        async with self._get_lock(ctx.guild.id):
            mappings = await self.config.guild(ctx.guild).mappings()
            if not mappings:
                await ctx.send("No channels or threads are mapped.")
                return

            selected = self._select_mappings(mappings, target)
            if not selected:
                assert target is not None
                await ctx.send(
                    f"{target.mention} does not have a repository mapping. "
                    f"Use `{ctx.clean_prefix}pac map` first."
                )
                return

            async with ctx.typing():
                try:
                    commit_sha = await self._sync_git_repo(ctx.guild)
                except PostsAsCodeError as exc:
                    await ctx.send(f"❌ {exc}")
                    return

                repo_dir = self._repo_dir(ctx.guild.id)
                scans: dict[str, FolderScanResult] = {}
                scan_errors: dict[str, str] = {}
                all_oversized_md: list[tuple[str, str, int, int]] = []
                all_oversized_img: list[tuple[str, str, int]] = []

                for tid, info in selected.items():
                    try:
                        scan = scan_folder(
                            repo_dir, info["path"], ctx.guild.filesize_limit
                        )
                        scans[tid] = scan
                        for fname, chars, parts in scan.oversized_markdown:
                            all_oversized_md.append((info["path"], fname, chars, parts))
                        for fname, size in scan.oversized_images:
                            all_oversized_img.append((info["path"], fname, size))
                    except PostsAsCodeError as exc:
                        scan_errors[tid] = str(exc)

            if all_oversized_img:
                limit_mb = ctx.guild.filesize_limit / (1024 * 1024)
                img_lines = "\n".join(
                    f"• `{path}/{fname}` ({size / (1024 * 1024):.2f} MB)"
                    for path, fname, size in all_oversized_img
                )
                await ctx.send(
                    f"❌ Aborting sync before making changes. The following image(s) exceed "
                    f"this server's {limit_mb:.1f} MB upload limit:\n{img_lines}"
                )
                return

            if not scans:
                err_lines = "\n".join(
                    f"• `{tid}`: {err}" for tid, err in scan_errors.items()
                )
                await ctx.send(f"❌ Nothing to sync:\n{err_lines}")
                return

            # Warn before modifying Discord if any .md file exceeds 2,000 characters.
            if all_oversized_md:
                warn_lines = "\n".join(
                    f"• `{path}/{fname}` — **{chars}** chars (will split into **{parts}** posts)"
                    for path, fname, chars, parts in all_oversized_md
                )
                confirmed = await self._confirm_prompt(
                    ctx,
                    "⚠️ **Markdown character limit warning (> 2,000 chars):**\n"
                    f"{warn_lines}\n\n"
                    "Reply `yes` to proceed with splitting these files into multiple posts, "
                    "or `no` to cancel so you can edit the source files.",
                )
                if not confirmed:
                    await ctx.send("Sync cancelled. No Discord posts were modified.")
                    return

            # Confirm destructive full-channel wipe for reset mode.
            if mode == "reset":
                assert target is not None
                repo_path = selected[str(target.id)]["path"]
                confirmed = await self._confirm_prompt(
                    ctx,
                    f"⚠️ **Confirm full channel reset for {target.mention}:**\n"
                    f"This will permanently delete **ALL** messages in {target.mention} "
                    f"(including messages not sent by the bot) and re-post from `{repo_path}`.\n"
                    "Reply `yes` to confirm or `no` to cancel.",
                )
                if not confirmed:
                    await ctx.send("Reset cancelled. No messages were deleted.")
                    return

            results: list[str] = [f"**Sync complete @ `{commit_sha}` ({mode} mode):**"]
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            async with ctx.typing():
                for tid, err in scan_errors.items():
                    label = self._target_label(ctx.guild, tid)
                    results.append(f"• {label}: ❌ {err}")

                for tid, scan in scans.items():
                    info = selected[tid]
                    label = self._target_label(ctx.guild, tid)

                    try:
                        stats, new_slots = await self._apply_sync_to_target(
                            ctx.guild, int(tid), info, scan, mode
                        )
                    except PartialSyncError as exc:
                        async with self.config.guild(ctx.guild).mappings() as mutable:
                            if tid in mutable:
                                mutable[tid]["slots"] = exc.partial_slots
                        results.append(
                            f"• {label} (`{info['path']}`): ❌ Partial sync error (progress saved): {exc}"
                        )
                        continue
                    except (PostsAsCodeError, discord.HTTPException) as exc:
                        results.append(f"• {label} (`{info['path']}`): ❌ {exc}")
                        continue

                    async with self.config.guild(ctx.guild).mappings() as mutable:
                        if tid in mutable:
                            mutable[tid]["slots"] = new_slots
                            mutable[tid]["last_synced"] = now_iso

                    if mode == "reset":
                        results.append(
                            f"• {label} (`{info['path']}`): purged {stats.purged} message(s), "
                            f"posted {stats.created} post(s)."
                        )
                    elif mode == "repost":
                        results.append(
                            f"• {label} (`{info['path']}`): deleted {stats.deleted}, "
                            f"posted {stats.created} post(s)."
                        )
                    else:
                        results.append(
                            f"• {label} (`{info['path']}`): {stats.unchanged} unchanged, "
                            f"{stats.edited} edited, {stats.created} created, {stats.deleted} deleted."
                        )

            summary_msg = "\n".join(results)
            if mode == "reset" and target is not None and ctx.channel.id == target.id:
                await self._send_chunked(ctx, summary_msg, delete_after=10)
            else:
                await self._send_chunked(ctx, summary_msg)
