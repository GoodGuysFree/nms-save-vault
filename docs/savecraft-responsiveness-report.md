# Savecraft responsiveness: measured, changed, measured again (2026-11-23)

The plan `docs/1795423452413-savecraft-responsiveness-report-plan.md` was checked against the
code before anything was changed. Its main findings held. The changes below were made
test-first, and each one keeps every safety invariant the plan lists in its section 3.

## How it was measured

`bench_sc.py` (kept with the session's notes, not in the repo) runs Savecraft's real window
on a **copy** of the live Steam folder. The live folder is only read, to copy it. Each run
starts with fresh settings, cache and vault, and uses the bundled game data; the vault's
history is off. The save is "Master" (slot 9). Every step is timed from the call to the
window settling, and Savecraft's own `timings.log` lines are taken too:

- 5 exosuit moves and 5 ship cargo moves, made as a drop makes them;
- each page shown;
- 4 letters typed in the Assets Filter;
- 8 assets selected;
- Apply All Changes (10 changes).

The machine is this development PC. Your log on your own PC showed the same shape: "change
held" 1.9–2.9 s, "drop to shown" 3.1 s.

## Before and after (medians, seconds)

| What the user waits for | Before | After | Change |
|---|---|---|---|
| Exosuit move: drop to shown | 2.04 | 1.13 | −45 % |
| Ship cargo move: drop to shown | 2.73 | 1.18 | −57 % |
| "change held" (exosuit / ship) | 1.09 / 1.77 | 0.71 / 0.83 | −35 % / −53 % |
| Saves read again after each change ("light") | 0.20 | 0.05 | −75 % |
| Assets page refilled while hidden, per change | 0.206 | 0.000 | gone |
| Inventories page fill after a change | 0.381 | 0.161 | −58 % |
| Typing 4 letters in the Assets Filter | 0.303 | 0.179 | −41 % |
| Ship slots page shown | 0.35 | 0.11 | −69 % |
| Outfits page shown | 0.96 | 0.48 | −50 % |
| Unlocks page shown | 2.90 | 2.43 | −16 % |
| Asset selected to card shown | 0.126 | 0.108 | −14 % |
| Apply All Changes (10 changes), to settled | 12.10 | 9.11 | −25 % |
| Start-up, first read (cold cache) | 7.28 | 7.31 | not targeted |

The Assets page now shows in 0.40 s instead of 0.35 s, because the refill it skipped while
hidden happens when it is shown. The raw runs are in `bench/before.json`, `before2.json` and
`after2.json` with the session's notes.

## What was changed

1. **A page out of view is not refilled on every read** (Assets, Find items, Storage, Saves,
   Changes). It is marked and filled when next shown, as History already did. This is display
   only. (`tests/test_hidden_pages.py`)
2. **An unchanged save is not read again.** Every read after a change used to read each save
   file whole, hash it and parse its cached facts. The facts are now kept by the file's size,
   time and file id. A file changed in the last 2 seconds is always read again, as git does,
   because two writes within one clock step could look alike. Each folder is scanned once per
   read, not twice. The write path still reads and checks the files itself.
   (`tests/test_facts_memo.py`)
3. **A held save is parsed once per change, not up to six times.** The save as last held is
   kept parsed (one save only: a big save parsed takes about 150 MB). It is shared with the
   readers that only read it: a change's build, the base-part limit check, the unlock check,
   the Inventories page, Outfits, Unlocks, Ship slots and Raw view. `Held.root` still hands
   out a fresh copy to anything that may change it. In the tests, `held.CHECK_SHARED` fails any
   reader that changes a shared save. The whole suite passes with it on, so no build changes
   what it reads. The bytes written still come only from the proven byte-range patches.
   (`tests/test_shared_root.py`)
4. **Each change's inventories are found once,** not five times (in `Held.make` and
   `root_facts`).
5. **The unlock and part checks read the change's own before and after.** These are the
   checks that every ship-inventory edit triggers. They no longer parse both saves again.
6. **Apply All Changes:**
   - The ship-slot check is not decoded again for a save already recorded. A save just
     written is checked from its held changes as parsed.
   - What the written save holds is stored under its new version, so the read after the
     write does not decode it (`tests/test_slotflags_fast.py`, `tests/test_apply_keeps_facts.py`).
7. **Core `saveedit.rebuild` (NMS Save Vault repo)** decoded the new container twice in a
   row. `lz4_block.compress` already decodes it and checks it gives the edited JSON back. The
   second decode only split at the NUL, which is now checked directly: the same acceptance,
   one decode less (`tests/test_saveedit.py`).
8. **Typing in the Assets Filter** lists at once for the first key. Keys typed while it lists
   are taken together once typing pauses (0.15 s). A box set by the page itself lists once.
   (`tests/test_filter_typing.py`)
9. **Pictures are PNG-encoded at zlib level 1, not 9.** They are shown at once and kept in
   memory; level 9 took several times longer for a few percent smaller.

## The plan's claims, checked

| Plan item | Found in the code | Done |
|---|---|---|
| Tier 1.1 hidden pages rebuilt on every read | yes (Assets 0.21 s per change) | yes |
| Tier 1.2 the same save parsed up to 6 times per change | yes | yes |
| Tier 1.3 every ship-inventory edit runs the unlock check | yes (`gates.CARRIERS` has `ShipOwnership`) | made cheap, not narrowed: the check stays exactly as safe |
| Tier 1.4 facts rebuilt in full for small edits | yes (0.22 s) | no: kept as is (correctness over 0.2 s) |
| Tier 2.1 every save read and hashed on each light read | yes | yes |
| Tier 2.2 the post-Apply read decodes the written save | yes | yes |
| Tier 2.3 Xbox listing reads whole blobs | not measured here (Steam only) | no |
| Tier 3.1 slot flags decoded before and after a write | yes | yes |
| Tier 3.2 rebuild decodes the new container twice | yes | yes |
| Tier 3.3 faster pure-Python LZ4 | measured: a tighter decoder is only ×1.25 | no |
| Tier 4.1 the filter lists on every key | yes | yes |
| Tier 4.2 the Inventories grid redraws whole on a click | yes | no |
| Tier 4.3 PNG level 9, pictures on the window's thread | yes | level only |
| Tier 4.4 Lay out technology's optimiser | parked feature | no |

## What is left, largest first

- **Compressing a save on Apply** (pure Python, about 2.7 s for an 18 MB save) and
  **decompressing a save the first time it is edited** (about 0.5 s). Only a C LZ4 library
  (`lz4` from PyPI, kept as optional, with the pure-Python code as the fallback and the same
  round-trip check) would cut these by tens of times. It is a binary dependency in both apps'
  kits: GGF's call.
- **`root_facts` on every change** (about 0.2 s): reusing the earlier facts for an edit that
  touches no asset (an exosuit move, a currency) is possible but changes what the window shows
  from. It was not done.
- **The patch proof** (about 0.2 s per change, `savepatch.apply`): an invariant, kept.
- **The Unlocks page** loads about 1,100 row icons on the window's thread, a few per tick.
- **Start-up with a cold cache** decodes every save. With a warm cache it reads and hashes
  them; it was not targeted.

Savecraft suite: 2,088 passed, 5 skipped. NMS Save Vault suite: 3,456 passed, 3 skipped.
