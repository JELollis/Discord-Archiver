# Code Review Findings — Post-Merge (feature/gtc-archive-wiki)

**Date:** 2026-08-26  
**Scope:** Current `main` after merge of PR #2 (wiki archiving stack)  
**Reviewer notes:** Focused on areas of opportunity; the integrity model (ownership markers, canonical seals, attachment/thread manifests, empty-channel registry, gated delete) is a strength and should be preserved.

---

## Summary

The bot is production-oriented and carefully hardened around “fail closed, re-verify before delete.” Supporting modules (`wiki_client`, `discord_export`, `empty_archive`, `attachment_archive`, `discord_rate_limit`) are well-factored. The main remaining work is hygiene, config robustness, file size of the primary bot module, and operational polish.

---

## 1. Repo hygiene & dead code

**Priority: High (low risk, high clarity)**

- **Legacy scripts still in root:**
  - `Archive_Bot2.py`
  - `archive.py`
  - `roles_generator.py`
  - `quick_update.py`

  These create ambiguity about the production entrypoint. Prefer moving them to `legacy/` or `scripts/`, or removing them if unused.

- **`.gitignore` inconsistency:** `quick_update.py` is listed in `.gitignore` but remains tracked. Either:
  - `git rm --cached quick_update.py` (keep the ignore entry), or
  - remove the ignore line if the file should stay versioned.

- Confirm the committed `README.md` is the full GTC Archive Wiki version (not the old one-liner).

---

## 2. Configuration & secrets loading

**Priority: High**

- Discord token loading is still relative-path:
  ```python
  with open("Bot Key.txt", "r", encoding="utf-8") as key_file:
      TOKEN = key_file.readline().strip()
  ```
  Prefer:
  1. Environment variable first (`DISCORD_TOKEN` / `BOT_TOKEN`)
  2. Fallback to `Path(__file__).resolve().parent / "Bot Key.txt"`
  3. Clear error if neither is present

- Wiki config already uses env + `wiki.env` / `/etc/discord-archiver/wiki.env`. Aligning Discord token loading with that pattern improves systemd/service-manager reliability.

---

## 3. Intents

**Priority: Medium**

- `intents.message_content = True` is still requested.
- The bot is primarily slash-command driven; channel history for archival is fetched via history APIs.
- If message content intent is not required for capture, dropping the privileged intent reduces permission surface and verification friction.

---

## 4. Monolithic bot file

**Priority: Medium**

- `Archive_Bot.py` is large (~112 KB / 2300+ lines).
- Supporting modules are already clean. Further extraction options:
  - Discord cogs / command groups (`CourseCommands`, `ArchiveCommands`, `AdminCommands`)
  - Capture + publish pipeline → dedicated module
  - Delete-gate / verification helpers → dedicated module

This would improve reviewability and unit-test targeting without changing behavior.

---

## 5. Testing

**Priority: Medium**

Existing coverage is solid for pure modules:
- `tests/test_wiki_client.py`
- `tests/test_discord_export.py`
- `tests/test_attachment_archive.py`
- `tests/test_empty_archive.py`
- `tests/test_discord_rate_limit.py`
- `tests/test_archive_bot_source.py`

Opportunities:
- Integration-style tests for **publish → verify → delete gate** (mocked Discord + MediaWiki).
- Explicit coverage of 429 / rate-limit handling and the global delete pacer.
- Failure-path tests for NAS oversize-sink and ZIP-fallback behavior.

---

## 6. Operational / resilience polish

**Priority: Medium–Low**

| Area | Suggestion |
|------|------------|
| Logging | One timestamped file per process start under `logs/`. Consider `RotatingFileHandler` / `TimedRotatingFileHandler` or structured logging for production. |
| Shutdown | Ensure MediaWiki client session is closed on bot shutdown (`close` override / cleanup path). |
| Progress | Long `/publish` runs can approach interaction token lifetime; keep progress messaging consistent. |
| Concurrency | Category-wide publish is sequential. Bounded concurrency (respecting MediaWiki + Discord rate limits) could speed end-of-term runs. |

---

## 7. Feature / product opportunities

**Priority: Low (product decisions)**

- Reactions, embeds, stickers, and edit history are explicitly out of scope for v1; optional later fidelity pass.
- Dry-run / preview mode for `/publish` and `/delete` would help admins validate impact safely.
- Simple metrics (channels published, failures, attachment bytes) via status command or log summary.
- Document the exact empty / voice / stage decision table in `/help` or operator docs.

---

## 8. Dependency & packaging

**Priority: Low**

- `requirements.txt` only pins `discord.py>=2.5,<3`. Consider a tighter pin or lock file for reproducible deploys.
- No `pyproject.toml` yet. Optional unless packaging / editable install becomes useful.

---

## Suggested priority order

1. **Hygiene** — move/remove legacy scripts; fix `quick_update.py` gitignore inconsistency.  
2. **Config** — robust Discord token loading (env + absolute path fallback).  
3. **Intents** — drop privileged `message_content` if unused.  
4. **Structure** — start carving `Archive_Bot.py` into modules/cogs.  
5. **Tests** — expand integration coverage around the delete gate.

---

## Non-goals / preserve

Do **not** weaken:

- Ownership markers and anti-overwrite checks  
- Canonical content seals and attachment/thread manifests  
- Empty-channel shared registry + live re-check  
- Gated `/delete` that revalidates before removal  
- Fail-closed behavior on partial or unverifiable archives  

The safety model is the strongest part of the project.
