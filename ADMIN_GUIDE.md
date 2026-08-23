# GTC Archive Bot — Admin Command Guide

This is the complete command reference for the GTC Archive Bot (`Archive-Bot`). It covers the
Discord-management commands and the new **wiki archiving** commands. All commands are Discord
**slash commands** and their replies are private (ephemeral — only you see them).

> **Tip:** you can get this reference in Discord at any time with **`/help`**, or details for one
> command with **`/help command:<name>`** (e.g. `/help command:publish`).

Most commands require the **Manage Channels** (or **Manage Roles**) permission.

---

## The archiving workflow (read this first)

The bot keeps a permanent, searchable copy of course channels on the wiki
(`https://gtc-wiki.completeelectronics.net`) **before** anything is deleted. The safe end-of-term
flow is:

1. **`/publish`** each channel you intend to remove → the bot copies it to the wiki and verifies it saved.
2. Confirm it looks right on the wiki (the bot posts the page link in **#archives**).
3. **`/delete`** the channel(s). Deletion is **blocked** for any text channel that hasn't been published yet.

`/archive` (moving channels into a "… Archive" category in Discord) is optional and separate — it does
**not** create the wiki copy. Only `/publish` does that.

---

## Wiki archiving commands

### `/publish` — archive a channel to the wiki
Captures a text channel's full history, uploads its attachments, publishes a wiki page, and verifies it.

- **Usage:** `/publish [channel:#channel]`
- **Parameters:**
  - `channel` *(optional)* — the text channel to archive. Defaults to the channel you run the command in.
- **Examples:**
  - `/publish`
  - `/publish channel:#cpt-257-summer-2023`
- **What it does:** reads every message (author, timestamp, text, attachments, reply references),
  uploads attachments to the wiki, and creates a page at **`Archive:YYYY/Term/DEPT-NUM`**
  (e.g. `Archive:2023/Summer/CPT-257`). Channels that don't match the `dept-num-term-year` naming go to
  **`Archive:Misc/<name>`**. The bot then **reads the page back to verify** it saved, posts the link in
  **#archives**, and marks the channel as safe to delete.
- **If verification fails:** the bot tells you and does **not** mark the channel deletable — do not delete it; re-run `/publish`.
- **Permission:** Manage Channels.

### `/wiki_status` — check the wiki connection
Confirms the bot can reach the wiki and has the rights it needs.

- **Usage:** `/wiki_status`
- **Example:** `/wiki_status`
- **Shows:** the MediaWiki version, the bot's wiki account and groups, whether it can **edit** and **upload**,
  and the archive namespace. Use this first if `/publish` ever misbehaves.
- **Permission:** Manage Channels.

---

## Channel & role management commands

### `/delete` — permanently delete channels/categories (guarded)
Bulk-deletes channels and/or categories after a confirmation, and **only** once they're archived.

- **Usage:** `/delete target_type:<Category|Channel|Both> targets:<comma,separated,names>`
- **Parameters:**
  - `target_type` — `Category` (a category **and all channels inside it**), `Channel` (only the named channels), or `Both`.
  - `targets` — comma-separated, **exact** channel/category names (case-insensitive).
- **Examples:**
  - Delete specific channels:
    `/delete target_type:Channel targets:cpt-257-summer-2023, ist-201-summer-2023`
  - Delete a whole term category and its channels:
    `/delete target_type:Category targets:Summer 2023 Archive`
- **Safety:**
  - **Every selected text channel must already be published to the wiki** (via `/publish`) or the whole
    deletion is **blocked**, and the bot lists which channels still need publishing.
  - A confirmation summary (counts of categories/channels) appears and **expires after 60 seconds**;
    only the admin who ran the command can press **Confirm Delete**.
  - **Deletion is permanent.**
- **Permission:** Manage Channels.

### `/archive` — move a term's channels into an archive category (Discord only)
Tidies the server by moving a term's channels into a read-only archive category.

- **Usage:** `/archive term:<Spring|Summer|Fall> year:<YYYY>`
- **Parameters:** `term` (Spring/Summer/Fall), `year` (4-digit).
- **Example:** `/archive term:Summer year:2023`
- **What it does:** moves channels whose name contains `-<term>-<year>` into a **"<Term> <Year> Archive"**
  category and grants the **Verified** role read-only access.
- **Note:** this is Discord housekeeping only — it does **not** create the wiki copy. Use `/publish` for that.

### `/populate` — create course channels for a term
Creates the per-course channels for a department, each locked to its course role.

- **Usage:** `/populate category:<CPT|CYB|IST|SPC|SOC|HSS|HIS> term:<Spring|Summer|Fall> year:<YYYY> courses:<numbers>`
- **Parameters:**
  - `category` — department (CPT, CYB, IST, SPC, SOC, HSS, HIS).
  - `term` — Spring/Summer/Fall. `year` — 4-digit.
  - `courses` — comma-separated course numbers.
- **Example:**
  `/populate category:CPT term:Spring year:2027 courses:113, 127, 170, 209, 257`
- **What it does:** creates `<DEPT>-<num>-<Term>-<Year>` channels, each private to the matching
  `<DEPT>-<num>` role plus **Lab Tech**.
- **Prerequisite:** the course roles must already exist — run **`/add_role`** first. Missing roles are
  skipped with a warning (the channel is still created, without that role's access).

### `/add_role` — create/update course roles
Creates missing course roles (and syncs permissions on existing ones) with the standard student permission set.

- **Usage:** `/add_role category:<CPT|CYB|IST|SPC|SOC|HSS|HIS> courses:<numbers>`
- **Example:** `/add_role category:CYB courses:110, 201, 269, 282, 293, 294`
- **What it does:** creates `<DEPT>-<num>` roles that don't exist and updates permissions on ones that do.
- **Permission:** Manage Roles (the bot's top role must be above the roles it manages).

### `/help` — command help
- **Usage:** `/help [command:<name>]`
- **Examples:** `/help`, `/help command:publish`
- Lists all commands, or shows full usage/parameters/examples for one.

---

## Worked example: archiving and removing last term's CPT channels

1. Verify the bot is healthy: `/wiki_status` → should show *edit: yes, upload: yes*.
2. Publish each channel:
   `/publish channel:#cpt-257-summer-2023` (repeat per channel; check the link in **#archives**).
3. Once all are published and verified, delete them:
   `/delete target_type:Channel targets:cpt-257-summer-2023, cpt-209-summer-2023`
   → review the confirmation (counts), then **Confirm Delete** within 60 seconds.

If `/delete` reports a channel is "blocked — no verified wiki archive," run `/publish` on it first.

---

## Setting up a new term (roles + channels)

1. Create/refresh roles: `/add_role category:CPT courses:113, 127, 170, 209, 257`
2. Create the channels: `/populate category:CPT term:Spring year:2027 courses:113, 127, 170, 209, 257`

Repeat per department (CYB, IST, SPC, SOC, HSS, HIS).

---

## Troubleshooting

- **A command doesn't appear in Discord:** slash commands sync globally and can take up to ~1 hour to show
  after a bot update. Try again shortly.
- **`/publish` fails or `/wiki_status` shows edit/upload = NO:** the bot's wiki login or permissions are off —
  notify the maintainer (check the bot's wiki account and BotPassword grants).
- **`/delete` is blocked:** that's by design — publish the listed channels first.
- **Nothing happens / errors:** the bot logs to its `logs/` directory on the host; share the timestamp with the maintainer.
