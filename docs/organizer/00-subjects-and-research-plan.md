# Save-file organizer - subjects and research plan

Status: research phase. Designs come after consolidation. No production code on this branch.

## The ask (owner, 2026-10-16)

One view of every save-file the owner has - live and in the vault - folded so that all past
versions of one save-file are one line (expandable), sortable, with key facts and a marker for
which ones are live and where (15 Steam + 15 Xbox slots). Pick any line (or any older version
of it) and "move to live"; the app asks which live slot to evict to the vault. Notes per
save-file that stay with every backup. Two hard follow-ons: (1) restoring an older version
makes history a tree, which the user must be able to see; (2) knowing what is in a save needs
the save opened and indexed (milestones, glyphs, base parts, legacy parts, ships, companions,
galaxies with bases, more later). Possibly a restructure of the whole UI.

## Subjects, in dependency order

| # | Subject | Core question | Depends on |
|---|---|---|---|
| S1 | Identity and versions | What is one "save-file" (one line)? What is one "version"? How are copies folded and de-duplicated across backups, slots, platforms? | - |
| S2 | Content index | How to open a save fully, which facts to extract, where to store them, how fast, how to keep it current | S1 |
| S3 | Lineage / history tree | How to know which version came from which; how to record it from now on and infer it for the existing vault; how to show it | S1, S2 |
| S4 | Notes | Per save-file and per version notes; carried with every backup; auto "what changed" from S2 | S1, S3 |
| S5 | Live-slot operations | "Move to live" with eviction, swap, use an older version; cross-platform / cross-account; safety and undo | S1 |
| S6 | Vault storage | Keep today's folder-per-backup storage and index over it, or move to a per-save-file version store | S1, S5 |
| S7 | UI | Organizer view, live-slot board, history view, notes, move-to-live flow; evolve the Tk app vs restructure vs another UI technology | all |
| S8 | Phasing | What ships first; order of phases | all |

## Research topics (wave 1, parallel subagents)

| ID | Topic | Feeds | Report |
|---|---|---|---|
| R1 | Core model and operations audit (catalog, savedir, identity, operations, oplog, tests) | S1 S5 S6 | `research/R1-core-audit.md` |
| R2 | Current GUI audit, flows, screenshots (sandboxed) | S7 | `research/R2-gui-audit.md` |
| R3 | UI technology options under the stdlib-only / bundled-runtime constraints | S7 | `research/R3-ui-tech.md` |
| R4 | Prior art and UX patterns (NMS tools, save managers, history graphs, slot-swap flows, notes) | S3 S4 S7 | `research/R4-prior-art.md` |
| R5 | Full-save decoding, key mapping, performance, index storage | S2 | `research/R5-decode-index.md` |
| R6 | Save facts catalog (paths and logic for each fact the owner named, plus candidates) | S2 S4 | `research/R6-facts-catalog.md` |
| R7 | The real vault: identities, versions, duplication, branching evidence, lineage signals | S1 S3 S6 | `research/R7-vault-lineage.md` |
| R8 | Slot mechanics, move-to-live semantics, cross-platform / cross-account transfer, cloud sync | S5 | `research/R8-slot-ops-transfer.md` |

## After research

1. Consolidate: decide per subject, fix the order, and list the owner decisions as forks.
2. Design wave: one subagent per subject, and one per option where a decision is the owner's.
   UI options come with mockups.
3. Present the options for the owner to choose.
