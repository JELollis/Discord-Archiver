# Discord Archiver + GTC Archive Wiki

A Discord bot and supporting stack for the **GTC Tech Student** community that manages course
channels/roles and **permanently archives Discord channels to a private, Discord-authenticated
MediaWiki** before they are deleted.

The bot captures a channel's full history, uploads its attachments, publishes a wiki page, and
**revalidates the saved page, split parts, files, and thread names** before the channel can be removed.

---

## What it does

- **Course management** — create/update course roles and per-course channels each term, and tidy old
  terms into read-only archive categories.
- **Wiki archiving** — copy a Discord text channel to the wiki as a formatted page (authors,
  timestamps, message text with mentions resolved, attachments, reply links), with read-back
  verification and a link posted to `#archives`.
- **Guarded deletion** — text channels require a current integrity-sealed wiki archive whose pages,
  attachments, thread names, and message boundaries still match; unsupported message channels are
  blocked, and voice/stage channels must have empty persistent text chat.
- **Private wiki with Discord SSO** — the wiki is readable only by verified members, logging in with
  Discord via Authentik (OpenID Connect).

## Architecture

```
Admin (Discord)                         Members (browser / mobile)
      │ slash commands                        │ Discord SSO
      ▼                                        ▼
 Archive Bot ──MediaWiki API──►  MediaWiki  ◄──OIDC──  Authentik  ◄──OAuth2──  Discord
 (discord.py)   (BotPassword)   (private wiki)         (identity)
                                     │
                          MySQL (content) · Redis (shared sessions)
```

- **Bot → MediaWiki:** the bot publishes via the MediaWiki API using a scoped **BotPassword**.
- **Members → MediaWiki:** browser login is Discord → Authentik (OIDC) → MediaWiki; guild membership is
  required and Discord roles map to wiki groups (member / editor / sysop).
- The deployment details are kept in private operator documentation and are intentionally not included here.

## Repository layout

| Path | Purpose |
|---|---|
| `Archive_Bot.py` | **Current bot** — course management + wiki archiving (`/publish`, `/delete` gate, `/wiki_status`, `/help`). |
| `wiki_client.py` | Async MediaWiki API client (BotPassword login, edit, upload, read-back verify). |
| `discord_export.py` | Renders a captured channel into safe MediaWiki wikitext (mention resolution, escaping, page-title mapping). |
| `Archive_Bot2.py` | Previous bot version, kept for reference/rollback. |
| `archive.py`, `roles_generator.py`, `quick_update.py` | Older/utility scripts. |

## Commands (summary)

| Command | What it does |
|---|---|
| `/publish [channel] [category] [channels]` | Archive a read-only channel, category, or channel list to the wiki (stage → post link → finalize → verify). |
| `/wiki_status` | Check the wiki connection and the bot's wiki permissions. |
| `/delete target_type targets` | Permanently delete channels/categories (confirmation + requires a verified archive). |
| `/archive term year` | Move a term's channels into a read-only Discord archive category. |
| `/populate category term year courses` | Create per-course channels for a term, locked to their course roles. |
| `/add_role category courses` | Create/update course roles with the standard permission set. |
| `/help [command]` | List commands, or show full help for one. |

Command parameters and safety behavior are also available through the bot's `/help` command.

The required retirement workflow is `/archive` → `/publish` → `/delete`. `/publish` refuses writable
channels, and `/delete` revalidates the parent channel and every thread immediately before removal.

## Wiki page structure

Course channels map to `Archive:DEPT-NUM/Term Year` (e.g. `cpt-257-summer-2023` →
`Archive:CPT-257/Summer 2023`). Channels that don't match the `dept-num-term-year` pattern are archived
under `Archive:Misc/<name>`. Each message gets a stable anchor so links can target a specific message.

## Configuration

The bot reads two pieces of configuration; **neither is committed to the repository**.

1. **Discord bot token** — a file named `Bot Key.txt` in the bot's working directory, containing the token
   on the first line.
2. **Wiki settings** — environment variables (systemd can load them via
   `EnvironmentFile=/etc/discord-archiver/wiki.env`; a local `wiki.env` also works for development):

   ```dotenv
   MEDIAWIKI_API_URL=https://your-wiki.example.com/api.php
   MEDIAWIKI_BOT_USERNAME=DiscordArchiveBot@ArchivePublisher
   MEDIAWIKI_BOT_PASSWORD=<BotPassword secret>
   MEDIAWIKI_ARCHIVE_NAMESPACE=Archive
   ```

   The BotPassword is created in the wiki at `Special:BotPasswords` with grants for editing, uploading, and
   **High-volume (bot) access**. The underlying service account must also have the `noratelimit` right. Run
   `/wiki_status` to verify the effective login retains it; the bot uses bounded backoff if it does not.
   If the wiki settings are absent, the bot still runs but the wiki commands are disabled.

## Running

Requirements: **Python 3.10+**, [`discord.py`](https://discordpy.readthedocs.io/) 2.x (which provides
`aiohttp`). Install with:

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt
```

Run the bot:

```bash
venv/bin/python Archive_Bot.py
```

In production it runs as a `systemd` service (`discord-archiver.service`) as an unprivileged user. The wiki,
identity provider, and databases are deployed separately using private operator documentation.

The wiki requires MediaWiki **1.43 LTS** with the **PluggableAuth** and **OpenID Connect** extensions, an
`Archive` namespace, and uploads enabled.

SQL source files are valid plain-text archive material. Keep MIME verification enabled and add this mapping to
the wiki's `LocalSettings.php` so `.sql` attachments remain directly downloadable instead of using the bot's
automatic ZIP fallback:

```php
$wgFileExtensions[] = 'sql';
$wgHooks['MimeMagicInit'][] = static function ( $mime ) {
    $mime->addExtraTypes( 'text/plain sql' );
};
```

After changing `LocalSettings.php`, run `php -l` before reloading the web server. The bot also preserves banned
or MIME-mismatched attachments inside a ZIP, so an unfamiliar file type cannot make an archive appear complete
while silently dropping content.

## Security notes

- Secrets (`Bot Key.txt`, `wiki.env`, wiki `LocalSettings.php`/`private.php`, keys/certs) are **gitignored**
  and must never be committed.
- The bot authenticates to the wiki with a **least-privilege BotPassword**, not a human/admin account.
- User-authored message text is escaped before it is written to the wiki, so archived content cannot inject
  wiki markup.
- The wiki is private: no anonymous read/edit; access is gated by Discord guild membership via OIDC.
