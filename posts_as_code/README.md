# PostsAsCode

Manage static Discord documentation channels and plain threads (rules, getting-started guides, FAQs, etc.) as code in a Git repository for [Red-DiscordBot](https://github.com/cog-creators/red-discordbot).

---

## Repository Layout

Map each Discord text channel or plain thread to a folder in your Git repository. Files inside each folder are sorted alphabetically (case-insensitive) and posted in order:

```text
discord/
├── rules/
│   ├── 01-banner.png
│   ├── 02-server-rules.md
│   └── 03-channel-guide.md
└── getting-started/
    ├── 01-welcome.md
    ├── 02-diagram.png
    └── 03-next-steps.md
```

- **Markdown files (`.md`):** Each file becomes one Discord post. If any `.md` file exceeds Discord's 2,000-character limit, `[p]pac sync` warns you before changing anything and lets you confirm splitting or cancel to edit the source file.
- **Image files (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`):** Posted as standalone image messages in sequence between surrounding `.md` posts. Checked against the server's upload size limit before syncing.

---

## Quick Start

```text
[p]pac repo https://github.com/your-org/discord-docs main
[p]pac map #rules discord/rules
[p]pac mkthread #guides discord/getting-started Getting Started
[p]pac diff
[p]pac sync
```

If using a private HTTPS repository, set a Personal Access Token (the invoking message is automatically deleted):

```text
[p]pac token <your-token>
```

---

## Sync Modes

| Mode | Command | Behavior |
|------|---------|----------|
| **Edit** *(default)* | `[p]pac sync [target]` | Edits posts in place by slot order; unchanged posts are skipped, new posts are appended at the end, and extra posts at the end are deleted. |
| **Repost** | `[p]pac sync [target] repost` | Deletes only the bot's tracked posts for the mapping and re-posts fresh in order. |
| **Reset** | `[p]pac reset <target>` *(or `[p]pac sync <target> reset`)* | Only allowed on mapped channels/threads. Prompts for confirmation, deletes **all** messages in the channel/thread (including non-bot messages), and re-posts fresh from Git. |

---

## Commands

All commands require **Admin** role or **Manage Guild** permission (`[p]postsascode` / `[p]pac`).

| Command | Description |
|---------|-------------|
| `[p]pac repo <url> [branch=main]` | Set the Git repository URL and branch |
| `[p]pac token [token]` | Set or clear a token for private HTTPS repos |
| `[p]pac map <channel_or_thread> <repo_path>` | Map an existing text channel or plain thread to a repo folder |
| `[p]pac mkthread <channel> <repo_path> <thread_name>` | Create a new plain public thread and map it |
| `[p]pac unmap <channel_or_thread>` | Remove a mapping (keeps existing Discord messages) |
| `[p]pac list` | Show repo settings, last synced commit, and mappings |
| `[p]pac diff [channel_or_thread]` | Pull the repo and preview changes without modifying Discord |
| `[p]pac sync [channel_or_thread] [mode=edit]` | Pull the repo and sync mapped target(s) (`edit`, `repost`, or `reset`) |
| `[p]pac reset <channel_or_thread>` | Confirm, purge all messages in a mapped target, and re-post |

---

## Requirements

- `git` must be installed and available on `PATH` on the Red-DiscordBot host.
- Bot permissions per mapped target:
  - **View Channel** & **Send Messages** (or **Send Messages in Threads** + **Manage Threads** for archived threads)
  - **Read Message History** (for `edit` and `reset`)
  - **Attach Files** (if publishing images)
  - **Manage Messages** (for `reset`)

---

## Installation

```text
[p]repo add Meshtastic-Cogs https://github.com/ArizonaMeshtasticCommunity/Meshtastic-Cogs
[p]cog install Meshtastic-Cogs posts_as_code
[p]load posts_as_code
```
