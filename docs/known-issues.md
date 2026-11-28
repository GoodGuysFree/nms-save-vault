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

From the data-corruption audit of 2026-11-23 (docs/CORRUPTION_AUDIT_REPORT.md); the findings
that could lose or wedge data were fixed, these were judged rare or harmless and left as is:

- **Undo from the Organizer of a whole-folder restore leaves the account data.** Undo last
  restore asks whether to put the account data back; the Organizer's Undo does not.
- **Pin and unpin of one version at the same moment from two windows** can lose one of them.
- **A two-save move cut off by a crash, then synced by Steam Cloud** before the next start can
  only be left as it is, with the asset in both saves.
- **Two NMS Save Vault windows at once:** the last to close keeps its window settings, and
  the slot identities one found for old saves may be forgotten by the other.
- **Xbox:** a failed write of an xgs mirror file is not rolled back by itself.
- **An edit stopped while it is prepared** can leave a temporary copy in %TEMP% and an unused
  extract in the vault.
- **Undoing a vault upgrade resumed after a crash** can ask for more free space than it needs;
  keeping the upgrade after a crash at one moment can leave a backup inside .rollback.
- **After Check vault repairs damaged copies**, the vault window can still say damaged files
  were found.
- **The search index set aside as damaged** while another window uses it is rebuilt there too.

Fixed in 0.6.0 (the kill tests): a whole-folder restore with the account data cut off between
its two files is now settled with the restore at the next start.
