# Known issues

Kept for the release notes (GGF, 2026-11-16: "Keep those issues on record, we'll put them in
the release notes when we publish"). Found by review R31 (of the whole-folder restore fixes
797e650 and cca7e1c); each was judged low and left as is.

- **A whole-folder restore stopped late can leave its snapshot behind.** The restore takes a
  snapshot of the folder ("before restoring the whole folder") before it writes anything. If
  the game saves into the folder just after that snapshot, the restore stops and writes
  nothing, but the snapshot stays in the vault. Nothing is lost: the snapshot is a true copy
  of the folder at that moment.
- **The same when the kept copy cannot be read back.** After the snapshot is taken, the
  restore reads the copies it keeps back from the vault. If that read fails, the restore
  stops and writes nothing, and again the snapshot stays in the vault.
