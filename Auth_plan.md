# Discord Wiki Authentication Plan

## Goal

Provide one private, Discord-authenticated MediaWiki for the GTC archive. The existing Discord Archiver bot captures and verifies Discord content, then publishes it directly to MediaWiki.

## Architecture

1. A visitor opens MediaWiki on either Apache/PHP web server.
2. MediaWiki redirects the visitor to Authentik using OpenID Connect (OIDC).
3. Authentik redirects the visitor to Discord OAuth2.
4. Discord authenticates the user and returns identity, guild membership, and role information.
5. Authentik applies guild and role policies and returns OIDC claims to MediaWiki.
6. MediaWiki creates or updates the user and assigns local groups from those claims.

### Hosts and databases

- The two existing Apache/PHP servers run MediaWiki and share the existing remote MySQL database.
- Jesse runs Authentik and its required local PostgreSQL database. Authentik does not use MySQL.
- Jesse is also the backup/failover host for HTTP and MySQL.
- Back up Authentik's PostgreSQL database off-host because Jesse has had infrastructure-related outages.

## Components

- MediaWiki
- MediaWiki PluggableAuth extension
- MediaWiki OpenID Connect extension
- Authentik
- Authentik PostgreSQL database
- Discord OAuth2 application
- Existing Discord Archiver bot

## Discord OAuth2

Request only the scopes needed:

- `identify`
- `guilds`
- `guilds.members.read`

Do not request `email` unless a later requirement needs it.

Configure Authentik's official Discord source to check guild membership and synchronize Discord roles. Allowlist every OAuth redirect URI exactly; do not use wildcard redirects.

## Authorization mapping

| Discord state or role | Authentik group | MediaWiki group | Result |
|---|---|---|---|
| Not a member of the configured guild | None | None | Deny login |
| Guild member | `gtc-member` | `member` | Read the private wiki |
| Wiki editor role | `gtc-editor` | `editor` | Edit approved content |
| Archive maintainer role | `gtc-archivist` | `archivist` | Manage archive pages and bot output |
| Administrator role | `gtc-admin` | `sysop` | MediaWiki administration |

Use Discord role IDs, not mutable role names, as the source of truth. Keep the actual guild and role IDs in deployment configuration rather than this repository.

## MediaWiki login policy

Normal operation uses Discord SSO only:

```php
$wgPluggableAuth_EnableAutoLogin = true;
$wgPluggableAuth_EnableLocalLogin = false;
```

Disable anonymous reading, editing, and manual account creation. Permit account autocreation only through the OIDC flow.

Keep two or three local MediaWiki administrator accounts as break-glass accounts. Give them long random passwords, MFA where supported, and no routine use. During an Authentik or Discord outage, an administrator may temporarily enable the local form:

```php
$wgPluggableAuth_EnableAutoLogin = false;
$wgPluggableAuth_EnableLocalLogin = true;
```

After recovery, test OIDC, disable the local form again, and record the emergency access in the audit log.

## Bot publishing flow

The existing bot remains responsible for Discord capture and archive verification:

1. Capture messages, threads, replies, reactions, edits, and attachments.
2. Verify the archive is complete.
3. Convert content to safe MediaWiki wikitext.
4. Upload attachments through the MediaWiki API.
5. Publish protected pages in the `Archive:` namespace.
6. Read back page revision IDs and verify stored content.
7. Post the final wiki URL to `#archives`.
8. Only then permit the guarded Discord deletion workflow.

Example page structure:

- `Archive:2023/Summer/CPT_257`
- `Archive:2023/Summer/CPT_257/general`
- `Archive:2023/Summer/CPT_257/assignments`

Split large channels by month or part and create stable per-message anchors so links can target a specific item.

Give the bot a dedicated MediaWiki service account with only the API and namespace permissions it needs. Do not reuse a human administrator account.

## Shared MediaWiki requirements

Because either web server may answer a request, both must use:

- The same MediaWiki version and extensions
- The same `LocalSettings.php` values
- The same MySQL database
- Shared or replicated uploads
- A common session store, or load-balancer session affinity
- The same HTTPS public URL and OIDC redirect URI

## Security requirements

- HTTPS everywhere
- OIDC state and nonce validation
- Exact allowlisted redirect URLs
- Secure, HTTP-only, SameSite cookies
- No anonymous reading or editing
- Secrets outside Git and outside web-accessible directories
- Minimal Discord scopes and least-privilege MediaWiki groups
- Authentik, MediaWiki, bot, and emergency-access audit logs
- Regular off-host database and configuration backups

## Configuration placeholders

Deployment configuration will supply these values; never commit real secrets:

- `MEDIAWIKI_PUBLIC_URL`
- `AUTHENTIK_PUBLIC_URL`
- `AUTHENTIK_OIDC_CLIENT_ID`
- `AUTHENTIK_OIDC_CLIENT_SECRET`
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `DISCORD_GUILD_ID`
- Discord role IDs for member, editor, archivist, and administrator

## Rollout

1. Install Authentik and PostgreSQL on Jesse.
2. Configure HTTPS, backups, and monitoring.
3. Create the Discord OAuth2 application and Authentik Discord source.
4. Create Authentik groups, expressions, and guild/role policies.
5. Install PluggableAuth and OpenID Connect on a staging MediaWiki.
6. Test allowed, denied, role-change, logout, and break-glass flows.
7. Connect the bot to staging and verify attachment and revision checks.
8. Deploy the same MediaWiki configuration to both web servers.
9. Enable Discord-only login after the break-glass procedure is tested.

## References

- [Authentik Discord source](https://docs.goauthentik.io/users-sources/sources/social-logins/discord/)
- [Authentik OAuth2/OIDC provider](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/)
- [MediaWiki PluggableAuth](https://www.mediawiki.org/wiki/Extension:PluggableAuth)
- [MediaWiki OpenID Connect](https://www.mediawiki.org/wiki/Extension:OpenID_Connect)
