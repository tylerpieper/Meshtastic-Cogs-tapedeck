import asyncio
import shutil
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Minimal discord / redbot stubs when running outside Redbot venv ──────────


def _install_stubs():
    if "discord" not in sys.modules:
        discord = types.ModuleType("discord")

        class _MessageType:
            default = 0
            thread_starter_message = 21

        class _ChannelType:
            public_thread = 11

        class _HTTPException(Exception):
            pass

        class _NotFound(_HTTPException):
            pass

        class _Forbidden(_HTTPException):
            pass

        class _File:
            def __init__(self, fp, filename=None):
                self.fp = Path(fp)
                self.filename = filename or self.fp.name

        class _TextChannel:
            pass

        class _Thread:
            pass

        class _Guild:
            pass

        class _Message:
            pass

        discord.MessageType = _MessageType
        discord.ChannelType = _ChannelType
        discord.HTTPException = _HTTPException
        discord.NotFound = _NotFound
        discord.Forbidden = _Forbidden
        discord.File = _File
        discord.TextChannel = _TextChannel
        discord.Thread = _Thread
        discord.Guild = _Guild
        discord.Message = _Message
        sys.modules["discord"] = discord

    if "redbot" not in sys.modules:
        redbot = types.ModuleType("redbot")
        core = types.ModuleType("redbot.core")
        bot_mod = types.ModuleType("redbot.core.bot")
        dm_mod = types.ModuleType("redbot.core.data_manager")
        utils_mod = types.ModuleType("redbot.core.utils")
        fmt_mod = types.ModuleType("redbot.core.utils.chat_formatting")

        class _Cog:
            pass

        class _Group:
            def __init__(self, fn):
                self.fn = fn

            def command(self, *args, **kwargs):
                def deco(f):
                    return f

                return deco

        class _Commands:
            Cog = _Cog

            @staticmethod
            def group(*args, **kwargs):
                def deco(f):
                    return _Group(f)

                return deco

            @staticmethod
            def guild_only():
                return lambda f: f

            @staticmethod
            def admin_or_permissions(**kwargs):
                return lambda f: f

        class _Config:
            @staticmethod
            def get_conf(*args, **kwargs):
                return MagicMock()

        class _Red:
            pass

        def _pagify(text, delims=("\n",), page_length=2000):
            while len(text) > page_length:
                idx = text.rfind("\n", 0, page_length)
                if idx <= 0:
                    idx = page_length
                yield text[:idx]
                text = text[idx:].lstrip("\n")
            if text:
                yield text

        core.Config = _Config
        core.commands = _Commands
        bot_mod.Red = _Red
        dm_mod.cog_data_path = lambda cog: Path(getattr(cog, "_test_data_path", "."))
        fmt_mod.pagify = _pagify

        sys.modules["redbot"] = redbot
        sys.modules["redbot.core"] = core
        sys.modules["redbot.core.bot"] = bot_mod
        sys.modules["redbot.core.data_manager"] = dm_mod
        sys.modules["redbot.core.utils"] = utils_mod
        sys.modules["redbot.core.utils.chat_formatting"] = fmt_mod


_install_stubs()

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import discord  # noqa: E402
from posts_as_code.posts_as_code import (  # noqa: E402
    DesiredSlot,
    PostsAsCode,
    PostsAsCodeError,
    build_git_auth_args,
    compute_slot_diff,
    scan_folder,
    scrub_secrets,
    split_markdown,
    validate_repo_path,
)


# ── Pure Function Tests ──────────────────────────────────────────────────────


def test_split_markdown_short_unchanged():
    text = "# Rules\nBe kind."
    assert split_markdown(text, limit=2000) == [text]


def test_split_markdown_paragraphs_and_code_fences():
    para = "A" * 800
    code_lines = "\n".join(f"print({i})" for i in range(120))
    text = f"{para}\n\n```python\n{code_lines}\n```\n\n{para}"
    chunks = split_markdown(text, limit=1000)

    assert len(chunks) >= 3
    for chunk in chunks:
        assert len(chunk) <= 1000
    # Code block split across chunks should close and reopen cleanly
    assert any(c.endswith("```") for c in chunks[:-1])
    assert any(c.startswith("```python\n") for c in chunks[1:])


def test_validate_repo_path_rejects_traversal_and_git():
    root = Path("/tmp/fake-repo")
    assert validate_repo_path(root, "discord/rules") == (root / "discord/rules").resolve()
    assert validate_repo_path(root, ".") == root.resolve()

    with pytest.raises(PostsAsCodeError):
        validate_repo_path(root, "../secrets")
    with pytest.raises(PostsAsCodeError):
        validate_repo_path(root, ".git/hooks")
    with pytest.raises(PostsAsCodeError):
        validate_repo_path(root, "-flag")


def test_scan_folder_orders_mixed_markdown_and_images(tmp_path: Path):
    folder = tmp_path / "discord" / "rules"
    folder.mkdir(parents=True)
    (folder / "01-intro.md").write_text("# Welcome\nHello!\r\n", encoding="utf-8")
    (folder / "02-banner.png").write_bytes(b"\x89PNG\r\n\x1a\nfakeimg")
    (folder / "03-details.md").write_text("X" * 2100, encoding="utf-8")
    (folder / ".hidden.md").write_text("ignored", encoding="utf-8")
    (folder / "empty.md").write_text("   \n", encoding="utf-8")
    (folder / "notes.txt").write_text("ignored txt", encoding="utf-8")

    scan = scan_folder(tmp_path, "discord/rules", filesize_limit=8 * 1024 * 1024)

    assert [s.source for s in scan.slots] == [
        "01-intro.md",
        "02-banner.png",
        "03-details.md",
        "03-details.md",
    ]
    assert [s.kind for s in scan.slots] == ["text", "image", "text", "text"]
    assert scan.oversized_markdown == [("03-details.md", 2100, 2)]
    assert scan.oversized_images == []
    assert scan.empty_files == ["empty.md"]


def test_scan_folder_flags_oversized_image(tmp_path: Path):
    folder = tmp_path / "imgs"
    folder.mkdir()
    (folder / "01-huge.jpg").write_bytes(b"x" * 2048)

    scan = scan_folder(tmp_path, "imgs", filesize_limit=1024)
    assert scan.oversized_images == [("01-huge.jpg", 2048)]
    assert len(scan.slots) == 1
    assert scan.slots[0].content_hash == "oversized"


def test_compute_slot_diff():
    existing = [
        {"kind": "text", "hash": "h1"},
        {"kind": "image", "hash": "h2"},
        {"kind": "text", "hash": "h3"},
    ]
    desired = [
        DesiredSlot(kind="text", source="01.md", content_hash="h1", content="a"),
        DesiredSlot(kind="text", source="02.md", content_hash="h_new", content="b"),
    ]
    assert compute_slot_diff(existing, desired) == (1, 1, 0, 1)


def test_git_auth_args_and_secret_scrubbing():
    args, secrets = build_git_auth_args(
        "https://github.com/org/repo.git", "ghp_superSecret+123"
    )
    assert len(args) == 2
    assert args[0] == "-c"
    assert "oauth2:ghp_superSecret%2B123@github.com/" in args[1]

    redacted = scrub_secrets(
        f"fatal: could not read {args[1]} with ghp_superSecret+123", secrets
    )
    assert "ghp_superSecret" not in redacted
    assert "%2B123" not in redacted


# ── Fake Discord Harness for Sync / Reset Tests ──────────────────────────────


class FakeMessage:
    def __init__(self, msg_id: int, content="", attachments=None, msg_type=0):
        self.id = msg_id
        self.content = content
        self.attachments = attachments or []
        self.type = msg_type
        self.deleted = False

    async def edit(self, content=None, attachments=None):
        if content is not None:
            self.content = content
        if attachments is not None:
            self.attachments = list(attachments)
        return self

    async def delete(self):
        self.deleted = True


class FakeChannel(discord.TextChannel):
    def __init__(self, channel_id: int, guild):
        self.id = channel_id
        self.guild = guild
        self.mention = f"<#{channel_id}>"
        self.messages: list[FakeMessage] = []
        self._next_id = 1000

    def permissions_for(self, member):
        perms = MagicMock()
        perms.view_channel = True
        perms.send_messages = True
        perms.send_messages_in_threads = True
        perms.attach_files = True
        perms.read_message_history = True
        perms.manage_messages = True
        perms.manage_threads = True
        return perms

    async def send(self, content="", file=None, delete_after=None):
        self._next_id += 1
        attachments = [file] if file is not None else []
        msg = FakeMessage(self._next_id, content=content, attachments=attachments)
        self.messages.append(msg)
        return msg

    async def fetch_message(self, msg_id: int):
        for m in self.messages:
            if m.id == msg_id and not m.deleted:
                return m
        raise discord.NotFound()

    def get_partial_message(self, msg_id: int):
        channel = self

        class _Partial:
            async def delete(self):
                for m in channel.messages:
                    if m.id == msg_id and not m.deleted:
                        m.deleted = True
                        channel.messages.remove(m)
                        return
                raise discord.NotFound()

        return _Partial()

    async def purge(self, limit=None, check=None, bulk=True):
        removed = []
        kept = []
        for m in self.messages:
            if check is None or check(m):
                m.deleted = True
                removed.append(m)
            else:
                kept.append(m)
        self.messages = kept
        return removed


class _ConfigValue:
    def __init__(self, data: dict, name: str):
        self._data = data
        self._name = name

    def __call__(self):
        return _ConfigCallResult(self._data, self._name)

    async def set(self, val):
        self._data[self._name] = val


class _ConfigCallResult:
    def __init__(self, data: dict, name: str):
        self._data = data
        self._name = name

    def __await__(self):
        async def _get():
            return self._data.get(self._name)

        return _get().__await__()

    async def __aenter__(self):
        return self._data.setdefault(self._name, {})

    async def __aexit__(self, *args):
        return False


class _DictConfigGroup:
    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, name):
        return _ConfigValue(self._data, name)

    async def all(self):
        return dict(self._data)


def _make_cog(tmp_path: Path, guild_data: dict) -> PostsAsCode:
    bot = MagicMock()
    cog = PostsAsCode(bot)
    cog._test_data_path = tmp_path
    group = _DictConfigGroup(guild_data)
    cog.config = MagicMock()
    cog.config.guild = lambda g: group
    return cog


def test_edit_mode_mid_insert_and_missing_message_compaction(tmp_path: Path):
    async def run():
        guild = MagicMock()
        guild.id = 42
        guild.filesize_limit = 8 * 1024 * 1024
        guild.me = MagicMock()
        chan = FakeChannel(999, guild)
        guild.get_channel_or_thread = lambda cid: chan if cid == 999 else None

        # Pre-existing messages: msg 101 ("01"), msg 102 ("03") — simulating "02" deleted in Discord
        m1 = FakeMessage(101, content="Post 1")
        m3 = FakeMessage(102, content="Post 3")
        chan.messages.extend([m1, m3])

        repo_dir = tmp_path / "repos" / "42" / "rules"
        repo_dir.mkdir(parents=True)
        (repo_dir / "01-one.md").write_text("Post 1", encoding="utf-8")
        (repo_dir / "02-pic.png").write_bytes(b"PNGDATA")
        (repo_dir / "03-three.md").write_text("Post 3", encoding="utf-8")

        from posts_as_code.posts_as_code import sha256_text

        mapping = {
            "path": "rules",
            "slots": [
                {"message_id": 101, "kind": "text", "hash": sha256_text("Post 1"), "source": "01-one.md"},
                {"message_id": 999999, "kind": "text", "hash": "old", "source": "02-old.md"},
                {"message_id": 102, "kind": "text", "hash": sha256_text("Post 3"), "source": "03-three.md"},
            ],
        }

        cog = _make_cog(tmp_path, {"mappings": {"999": mapping}})
        scan = scan_folder(tmp_path / "repos" / "42", "rules", guild.filesize_limit)

        stats, new_slots = await cog._apply_sync_to_target(guild, 999, mapping, scan, "edit")

        assert stats.unchanged == 1  # m1 stayed Post 1
        assert stats.edited == 1     # m3 edited in place to hold 02-pic.png
        assert stats.created == 1    # new message appended for 03-three.md
        assert len(chan.messages) == 3
        assert chan.messages[0].content == "Post 1"
        assert chan.messages[1].content == ""
        assert len(chan.messages[1].attachments) == 1
        assert chan.messages[1].attachments[0].filename == "02-pic.png"
        assert chan.messages[2].content == "Post 3"
        assert [s["source"] for s in new_slots] == ["01-one.md", "02-pic.png", "03-three.md"]

    asyncio.run(run())


def test_reset_guardrail_and_full_purge(tmp_path: Path):
    async def run():
        guild = MagicMock()
        guild.id = 42
        guild.filesize_limit = 8 * 1024 * 1024
        guild.me = MagicMock()

        mapped_chan = FakeChannel(111, guild)
        unmapped_chan = FakeChannel(222, guild)
        # Seed mapped_chan with a user message and an old bot message
        mapped_chan.messages.append(FakeMessage(1, content="Random user chat"))
        mapped_chan.messages.append(FakeMessage(2, content="Old bot post"))

        guild.get_channel_or_thread = lambda cid: (
            mapped_chan if cid == 111 else unmapped_chan if cid == 222 else None
        )

        # Prepare local repo directly and mock _sync_git_repo
        repo_dir = tmp_path / "repos" / "42" / "docs"
        repo_dir.mkdir(parents=True)
        (repo_dir / "01-rules.md").write_text("Official Rules", encoding="utf-8")

        guild_data = {
            "repo_url": "https://example.com/repo.git",
            "branch": "main",
            "mappings": {"111": {"path": "docs", "slots": [], "last_synced": None}},
        }
        cog = _make_cog(tmp_path, guild_data)
        cog._sync_git_repo = AsyncMock(return_value="abc1234")
        cog._confirm_prompt = AsyncMock(return_value=True)

        # 1) Unmapped channel must be rejected
        ctx_unmapped = MagicMock()
        ctx_unmapped.guild = guild
        ctx_unmapped.channel = unmapped_chan
        ctx_unmapped.clean_prefix = "!"
        ctx_unmapped.send = AsyncMock()
        await cog._execute_sync(ctx_unmapped, target=unmapped_chan, mode="reset")
        assert "does not have a repository mapping" in ctx_unmapped.send.call_args[0][0]
        assert len(mapped_chan.messages) == 2

        # 2) Mapped channel reset purges ALL messages (including user chat) and reposts
        ctx_mapped = MagicMock()
        ctx_mapped.guild = guild
        ctx_mapped.channel = mapped_chan
        ctx_mapped.clean_prefix = "!"
        ctx_mapped.send = AsyncMock()
        ctx_mapped.typing = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))

        await cog._execute_sync(ctx_mapped, target=mapped_chan, mode="reset")
        assert [m.content for m in mapped_chan.messages] == ["Official Rules"]
        assert guild_data["mappings"]["111"]["slots"][0]["source"] == "01-rules.md"

    asyncio.run(run())


def test_oversized_markdown_declined_aborts_cleanly(tmp_path: Path):
    async def run():
        guild = MagicMock()
        guild.id = 42
        guild.filesize_limit = 8 * 1024 * 1024
        guild.me = MagicMock()
        chan = FakeChannel(111, guild)
        chan.messages.append(FakeMessage(1, content="Original"))
        guild.get_channel_or_thread = lambda cid: chan if cid == 111 else None

        repo_dir = tmp_path / "repos" / "42" / "docs"
        repo_dir.mkdir(parents=True)
        (repo_dir / "01-long.md").write_text("Word " * 500, encoding="utf-8")

        cog = _make_cog(
            tmp_path,
            {"mappings": {"111": {"path": "docs", "slots": [], "last_synced": None}}},
        )
        cog._sync_git_repo = AsyncMock(return_value="abc1234")
        cog._confirm_prompt = AsyncMock(return_value=False)

        ctx = MagicMock()
        ctx.guild = guild
        ctx.channel = chan
        ctx.clean_prefix = "!"
        ctx.send = AsyncMock()
        ctx.typing = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))

        await cog._execute_sync(ctx, target=chan, mode="edit")
        assert [m.content for m in chan.messages] == ["Original"]
        assert "Sync cancelled" in ctx.send.call_args[0][0]

    asyncio.run(run())


def test_git_clone_and_pull_roundtrip(tmp_path: Path):
    if not shutil.which("git"):
        pytest.skip("git CLI not installed")

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", str(origin), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-b", "main"], check=True)
    (work / "rules").mkdir()
    (work / "rules" / "01.md").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-u", "origin", "main"], check=True)

    async def run():
        guild = MagicMock()
        guild.id = 77
        cog = _make_cog(
            tmp_path / "cogdata",
            {"repo_url": str(origin), "branch": "main", "token": None, "mappings": {}},
        )
        sha1 = await cog._sync_git_repo(guild)
        cloned_file = cog._repo_dir(77) / "rules" / "01.md"
        assert cloned_file.read_text(encoding="utf-8") == "v1"

        (work / "rules" / "01.md").write_text("v2", encoding="utf-8")
        subprocess.run(["git", "-C", str(work), "add", "."], check=True)
        subprocess.run(["git", "-C", str(work), "commit", "-m", "update"], check=True)
        subprocess.run(["git", "-C", str(work), "push"], check=True)

        sha2 = await cog._sync_git_repo(guild)
        assert sha2 != sha1
        assert cloned_file.read_text(encoding="utf-8") == "v2"

    asyncio.run(run())


def test_sanitize_mapping_path():
    assert PostsAsCode._sanitize_mapping_path("discord/rules") == "discord/rules"
    assert PostsAsCode._sanitize_mapping_path("/") == "."
    assert PostsAsCode._sanitize_mapping_path("../secret") is None
    assert PostsAsCode._sanitize_mapping_path(".git/hooks") is None
    assert PostsAsCode._sanitize_mapping_path("-flag") is None


def test_partial_sync_saves_progress_on_http_error(tmp_path: Path):
    async def run():
        guild = MagicMock()
        guild.id = 42
        guild.filesize_limit = 8 * 1024 * 1024
        guild.me = MagicMock()

        chan = FakeChannel(111, guild)
        orig_send = chan.send
        send_count = 0

        async def flaky_send(content="", file=None, delete_after=None):
            nonlocal send_count
            send_count += 1
            if send_count == 2:
                raise discord.HTTPException("Simulated transient 503")
            return await orig_send(content=content, file=file, delete_after=delete_after)

        chan.send = flaky_send
        guild.get_channel_or_thread = lambda cid: chan if cid == 111 else None

        repo_dir = tmp_path / "repos" / "42" / "docs"
        repo_dir.mkdir(parents=True)
        (repo_dir / "01.md").write_text("First", encoding="utf-8")
        (repo_dir / "02.md").write_text("Second", encoding="utf-8")

        guild_data = {
            "mappings": {"111": {"path": "docs", "slots": [], "last_synced": None}},
        }
        cog = _make_cog(tmp_path, guild_data)
        cog._sync_git_repo = AsyncMock(return_value="abc1234")

        ctx = MagicMock()
        ctx.guild = guild
        ctx.channel = chan
        ctx.clean_prefix = "!"
        ctx.send = AsyncMock()
        ctx.typing = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))

        await cog._execute_sync(ctx, target=chan, mode="repost")
        saved_slots = guild_data["mappings"]["111"]["slots"]
        assert len(saved_slots) == 1
        assert saved_slots[0]["source"] == "01.md"
        assert "Partial sync error (progress saved)" in ctx.send.call_args[0][0]

    asyncio.run(run())
