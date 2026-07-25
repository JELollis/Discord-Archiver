# Discord-Archiver — Code Audit

**Audit date:** 2026-07-19
**Primary subject:** `Archive_Bot2.py` (the file currently run in production)
**Supporting files reviewed:** `Archive_Bot.py`, `archive.py`, `quick_update.py`, `roles_generator.py`, `README.md`, `.gitignore`, `.gitattributes`

Each finding lists severity, the concrete failure mode, and a recommended fix.

> **Remediation status (2026-07-19):** Findings **#1–#10 and #14 are now fixed
> in `Archive_Bot2.py`** (and a new `requirements.txt`). Fixes were verified by
> importing the module against `discord.py` 2.7.1 and asserting that every
> command registers with `guild_only` + the correct `default_permissions` +
> a permission check, that the error handler is wired, and that the new
> anchored `/archive` matcher rejects near-miss channel names. The remaining
> items are repo-hygiene/behavior decisions left to the maintainer:
> **#11** (untrack vs. un-ignore `quick_update.py`), **#12** (remove/relocate
> the legacy prototype scripts), and **#13** (archived-channel access policy —
> flagged as a deliberate choice to confirm, not a bug). The per-finding
> "Fix" sections below describe what was applied.

---

## Summary

`Archive_Bot2.py` is a slash-command bot exposing three privileged operations —
`/archive`, `/populate`, and `/add_role` — that create/move channels, rewrite
channel permission overwrites, and create/edit guild roles. The code is
readable and its error-reporting-to-user pattern is consistent. The most
important gap is **authorization**: none of the three commands restricts who can
invoke them, even though the earlier `Archive_Bot.py` prototype did gate its
command behind an admin/role check. There is also one guaranteed crash path in
`/populate` and several robustness/operational issues that matter at the scale
of a real course server.

| # | Severity | Area | Finding | Status |
|---|----------|------|---------|--------|
| 1 | **Critical** | Security | Slash commands have no permission gate — any member can archive/populate/create roles | ✅ Fixed |
| 2 | Medium | Security | `message_content` privileged intent requested but unused | ✅ Fixed |
| 3 | **High** | Correctness | `/populate` crashes when the `Lab Tech` role is missing (unchecked `None` overwrite key) | ✅ Fixed |
| 4 | Medium | Correctness | `/archive` and `/populate` have no `guild_only` / `guild is None` guard | ✅ Fixed |
| 5 | Medium | Correctness | `/archive` uses an unanchored substring match for channel names | ✅ Fixed |
| 6 | Medium | Reliability | `/archive` permission rewrite makes many serial API calls — rate-limit / 15-min token expiry risk | ✅ Fixed |
| 7 | Medium | Reliability | `tree.sync()` runs on every `on_ready` (re-syncs on every reconnect) | ✅ Fixed |
| 8 | Low | Security | Raw exception text (`str(e)`) is surfaced to users | ✅ Fixed |
| 9 | Low | Reliability | `Bot Key.txt` read is unguarded, relative-path, no env fallback | ✅ Fixed |
| 10 | Low | Robustness | `/add_role` aborts the whole batch on the first `Forbidden` | ✅ Fixed |
| 11 | Low | Hygiene | `quick_update.py` is committed *and* listed in `.gitignore` | ⏳ Maintainer decision |
| 12 | Low | Hygiene | Legacy/dead files with placeholder tokens; no doc of which file is production | ⏳ Maintainer decision |
| 13 | Info | Behavior | Archiving grants read only to `Verified`; the course role loses access | ⏳ Confirm intent |
| 14 | Low | Ops | No `requirements.txt` / pinned `discord.py`; `hasattr` version guards imply uncertainty | ✅ Fixed |

---

## Findings

### 1. Critical — Slash commands are unauthenticated
**Where:** `Archive_Bot2.py:42` (`/archive`), `:114` (`/populate`), `:238` (`/add_role`)

None of the three commands has a permission check. Any member of the guild can:
- `/archive` — move channels into an archive category and strip/rewrite their permission overwrites,
- `/populate` — mass-create categories and channels,
- `/add_role` — create new roles and rewrite the permissions of existing roles.

By contrast, the older `Archive_Bot.py:41` gated its command with
`@is_admin_or_has_role("Admin")`. That protection was dropped in the slash-command rewrite.

**Impact:** Privilege escalation / griefing. `/add_role` in particular lets an
unprivileged user create roles whose permission set is defined by
`build_course_permissions()`, and generally alter server structure.

**Fix:** Restrict at registration time (shown to non-privileged users as
disabled) and enforce at runtime:

```python
@tree.command(name="archive", ...)
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)   # or administrator=True
@app_commands.checks.has_permissions(manage_channels=True)
async def archive(...):
    ...
```

Use `manage_roles=True` for `/add_role`. Add an `on_app_command_error`/`error`
handler (or per-command `.error`) so `app_commands.MissingPermissions` is
reported cleanly instead of falling through the generic `except`.

---

### 2. Medium — Unused privileged intent
**Where:** `Archive_Bot2.py:25` — `intents.message_content = True`

The bot only uses application (slash) commands; it never reads message content.
`message_content` is a **privileged** intent that must be enabled in the Developer
Portal and gates verification for larger bots. Requesting it here grants the bot
access to message text it never needs.

**Fix:** Drop `intents.message_content = True` and run on
`discord.Intents.default()` (or `Intents.none()` plus `guilds`). Slash commands
do not require it.

---

### 3. High — `/populate` crashes when the `Lab Tech` role does not exist
**Where:** `Archive_Bot2.py:175-184`

```python
lab_tech_role = discord.utils.get(guild.roles, name="Lab Tech")
if not role:                 # only the *course* role is checked
    ...
    continue
overwrites = {
    guild.default_role: ...,
    role: ...,
    lab_tech_role: discord.PermissionOverwrite(...)   # lab_tech_role may be None
}
new_channel = await guild.create_text_channel(..., overwrites=overwrites)
```

The course `role` is checked for `None`, but `lab_tech_role` is not. If the
`Lab Tech` role is absent, `lab_tech_role` is `None`, and a `None` key in the
`overwrites` mapping raises when `create_text_channel` processes it (it expects
`Role`/`Member` objects). The generic `except` then reports "An error occurred"
and **no channels are created** — the command is completely broken whenever that
role happens to be missing, which is easy to trigger.

**Fix:** Build overwrites conditionally:

```python
overwrites = {
    guild.default_role: discord.PermissionOverwrite(read_messages=False),
    role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
}
if lab_tech_role:
    overwrites[lab_tech_role] = discord.PermissionOverwrite(
        read_messages=True, send_messages=True, read_message_history=True)
else:
    logging.warning("Lab Tech role not found; creating '%s' without it.", channel_name)
```

---

### 4. Medium — No guild-only guard on `/archive` and `/populate`
**Where:** `Archive_Bot2.py:62` and `:160` use `interaction.guild` directly

`/add_role` checks `if guild is None` (`:263`), but `/archive` and `/populate`
do not. Invoked in a DM context, `interaction.guild` is `None` and the first
`guild.<attr>` access raises `AttributeError` (caught, but reported as a generic
error).

**Fix:** Add `@app_commands.guild_only()` to all three commands (belt-and-suspenders
with an explicit `None` check if you prefer).

---

### 5. Medium — Unanchored channel-name matching in `/archive`
**Where:** `Archive_Bot2.py:84` — `if f"-{term}-{year}" in channel.name.lower():`

This is a substring test, so `-fall-2025` matches `cpt-113-fall-2025` *and*
`cpt-113-fall-2025-backup`, `announcements-fall-2025-notes`, etc. `quick_update.py:24`
already does this correctly with an anchored regex:
`^[a-zA-Z]+-\d{3}-summer-2025$`.

**Impact:** Channels you did not intend to archive can be swept in and have their
permissions rewritten.

**Fix:** Match with an anchored pattern consistent with the `/populate` naming
scheme (`{CAT}-{course}-{Term}-{year}`), e.g.:

```python
pat = re.compile(rf'^[a-z]+-\d+-{re.escape(term)}-{year}$')
if pat.match(channel.name.lower()):
```

---

### 6. Medium — `/archive` permission rewrite is API-heavy
**Where:** `Archive_Bot2.py:85-93`

Per matching channel the code issues: one `channel.edit(category=...)`, then one
`set_permissions(target, overwrite=None)` **per existing overwrite**, then two
more `set_permissions` calls. For a term with many channels this is a large,
serial burst of REST calls. Two consequences:

- Discord rate-limits will slow it (and may surface as errors).
- The interaction token expires ~15 minutes after `defer()`; a long run can make
  the final `followup.send` fail even though the work partly completed.

**Fix:** Collapse each channel to a single write with a fully-specified overwrite
map:

```python
overwrites = {
    guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
    verified_role: discord.PermissionOverwrite(read_messages=True, send_messages=False, read_message_history=True),
}
await channel.edit(category=archive_category, overwrites=overwrites)
```

`channel.edit(overwrites=...)` replaces the whole overwrite set, so the separate
clear loop is unnecessary. For very large servers, consider sending an interim
progress `followup` and/or chunking.

---

### 7. Medium — Global command sync on every `on_ready`
**Where:** `Archive_Bot2.py:33-38`

`on_ready` can fire multiple times over a process lifetime (every gateway
reconnect/resume). Each fire calls `await tree.sync()`, a **global** sync, which
is heavily rate-limited and slow to propagate. Repeated global syncs waste that
budget.

**Fix:** Sync once — e.g. guard with a flag, or sync in `setup_hook` instead of
`on_ready`. During development, prefer `tree.copy_global_to(guild=...)` +
`tree.sync(guild=...)` for instant, per-guild updates.

---

### 8. Low — Raw exception text surfaced to users
**Where:** `:107`, `:109`, `:297`, `:314`, `:331`, `:332` (`f"...: {str(e)}"` / `{e}`)

Sending `str(e)` to users can leak internal details (IDs, paths, library
internals). It is ephemeral, which limits exposure, but a generic message plus a
full server-side log entry is cleaner.

**Fix:** Show a fixed message ("Something went wrong — an admin has been
notified.") and keep the detail in `logging.exception(...)`.

---

### 9. Low — Token loading is fragile
**Where:** `Archive_Bot2.py:21-22`

`open("Bot Key.txt", ...)` uses a **relative** path (breaks if the process CWD
isn't the repo root, e.g. under a service manager), has no error handling (a
missing file raises an unhandled `FileNotFoundError` at import), and has no
environment-variable fallback.

**Fix:** Resolve relative to the file (`Path(__file__).parent / "Bot Key.txt"`),
prefer an env var (`os.environ.get("DISCORD_TOKEN")`) with the file as fallback,
and fail with a clear message if neither is present.

---

### 10. Low — `/add_role` aborts the whole batch on first failure
**Where:** `Archive_Bot2.py:290-315`

On the first `discord.Forbidden` (create or edit), the command `return`s
immediately. If it already created/updated earlier roles in the list, the user
gets an error with no record of the partial success.

**Fix:** Collect per-role failures into a list and continue, then include a
`⚠️ Failed:` section in the summary alongside created/updated/unchanged.

---

### 11. Low — `quick_update.py` is tracked but also `.gitignore`d
**Where:** `.gitignore:5` lists `quick_update.py`, yet `git ls-files` shows it committed.

`.gitignore` does not untrack an already-tracked file, so the ignore entry is
misleading — the file is still versioned and its changes still commit. This is
contradictory intent.

**Fix:** Decide one way. To stop tracking it:
`git rm --cached quick_update.py` (keep the ignore entry). Otherwise remove it
from `.gitignore`. Note `quick_update.py` is a one-off, hard-coded
`summer-2025` migration script (`quick_update.py:24`), so untracking (or moving
it to a `scripts/` folder and dropping the ignore) is reasonable.

---

### 12. Low — Legacy/dead files and no "which file is production" doc
**Where:** `archive.py:101` (`bot.run('')`), `Archive_Bot.py:145` (`bot.run('')`),
`roles_generator.py:78` (`bot.run('{API_Key}')`)

Three scripts ship with empty or literal-placeholder tokens and cannot run as-is.
`archive.py` and `Archive_Bot.py` are earlier prototypes of the same archive flow
now superseded by `Archive_Bot2.py`. Nothing in the repo states that
`Archive_Bot2.py` is the production entrypoint, which is a maintenance hazard
(someone could edit/run the wrong file).

**Fix:** Remove or move the superseded prototypes into an `archive/` or `legacy/`
folder, and document the production entrypoint + commands in `README.md`. If
`roles_generator.py` is still used, align it with `Archive_Bot2.py`'s token
loading instead of a hard-coded placeholder.

---

### 13. Info — Archived channels drop the course role's access
**Where:** `Archive_Bot2.py:87-93`

Archiving clears all overwrites and grants read to `Verified` (not to the
original per-course role). This may be the intended policy (any verified member
can read archives, nobody can post), but it means students in that specific
course lose their distinct access. Flagging so the behavior is a deliberate
choice, not an accident. If per-course read retention is desired, preserve or
re-add the course role overwrite.

---

### 14. Low — No dependency pinning; version-guarded permission flags
**Where:** `Archive_Bot2.py:213, 224, 231` (`hasattr(discord.Permissions, ...)`)

The `hasattr` guards for `create_forum_threads` / `use_external_stickers` /
`use_embedded_activities` suggest the target `discord.py` version is uncertain.
There is no `requirements.txt`, so deployments can drift to an incompatible
library version.

**Fix:** Add a `requirements.txt` pinning `discord.py` (a version new enough that
those flags exist lets you drop the `hasattr` guards), and document the Python
version.

---

## Suggested remediation order

1. **#1** authorization (security-critical; small change).
2. **#3** `/populate` `None` crash (guaranteed failure path).
3. **#4, #5, #6** `/archive` correctness & scale hardening.
4. **#2, #7, #9** intent, sync-once, robust token loading.
5. **#8, #10, #11, #12, #14** hygiene & docs.

None of these require rearchitecting; the bot's structure is sound. Items #1 and
#3 are the two that materially affect a production deployment today.
