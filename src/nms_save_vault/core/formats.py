"""On-disk format constants for No Man's Sky PC saves (Steam/GOG).

All values here were verified against the reference implementation libNOM.io and
empirically confirmed by decrypting/decompressing the user's actual save files.

Two file kinds per logical save:
  * ``saveN.hg``    - the save *data*: a stream of LZ4-compressed chunks of obfuscated
                      JSON. NOT encrypted; fully portable between slots.
  * ``mf_saveN.hg`` - the *meta* (manifest): a small fixed-size record, XXTEA-encrypted
                      with a key derived from the file's storage-slot ordinal. Holds the
                      save name, sizes, mode, play time, timestamp, etc.
"""
from __future__ import annotations

import struct

# --- Save data (streaming LZ4 container) -------------------------------------

# Little-endian magic 0xFEEDA1E5 that prefixes every compressed chunk.
SAVE_MAGIC = 0xFEEDA1E5
SAVE_STREAMING_HEADER = b"\xe5\xa1\xed\xfe"
# Each chunk: [magic u32][compressed_size u32][decompressed_size u32][0 u32] + payload
SAVE_CHUNK_HEADER_LENGTH = 0x10  # 16
SAVE_CHUNK_DECOMPRESSED_MAX = 0x80000  # 524288

# --- Meta (mf_*.hg) ----------------------------------------------------------

# First u32 of the *decrypted* meta; used to confirm the right key/ordinal was used.
META_HEADER = 0xEEEEEEBE

# Total meta sizes by game era (Steam). The user's saves are WORLDS_PART_II (432).
META_LEN_VANILLA = 0x68  # 104
META_LEN_WAYPOINT = 0x168  # 360
META_LEN_WORLDS_I = 0x180  # 384
META_LEN_WORLDS_II = 0x1B0  # 432
META_LENGTHS_KNOWN = (META_LEN_VANILLA, META_LEN_WAYPOINT, META_LEN_WORLDS_I, META_LEN_WORLDS_II)

# Meta format version stored at offset 4 (and again near the tail for Worlds).
META_FORMAT_1 = 0x7D1  # 2001 (1.10)
META_FORMAT_2 = 0x7D2  # 2002 (3.60 Frontiers)
META_FORMAT_3 = 0x7D3  # 2003 (5.00 Worlds I)
META_FORMAT_4 = 0x7D4  # 2004 (5.50 Worlds II)

# XXTEA key. key[1..3] are constant (ASCII "NAESEVADNAYRTNRG"); key[0] is replaced by a
# value derived from the per-file storage ordinal (see meta.slot_key()).
META_ENCRYPTION_KEY = struct.unpack("<4I", b"NAESEVADNAYRTNRG")
KEY_CONST_XOR = 0x1422CB8C
KEY_CONST_ADD = 0xE6546B64
XXTEA_DELTA = 0x9E3779B9
XXTEA_DELTA_NEG = 0x61C88647  # == (2**32 - XXTEA_DELTA); decrypt subtracts DELTA per round

# Field offsets within the *decrypted* meta (Steam, Waypoint/Worlds layout). Confirmed
# against the user's files: SizeDecompressed/SizeDisk matched decompression byte counts
# and on-disk file sizes exactly; names round-tripped ("Main Save (S2)", etc.).
OFF_HEADER = 0x00              # u32  == META_HEADER
OFF_META_FORMAT = 0x04         # u32
OFF_SIZE_DECOMPRESSED = 0x38   # u32  total decompressed JSON length (incl. terminator)
OFF_SIZE_DISK = 0x3C           # u32  on-disk compressed size of the saveN.hg
OFF_BASE_VERSION = 0x44        # u32
OFF_GAME_MODE = 0x48           # u16
OFF_SEASON = 0x4A              # u16
OFF_TOTAL_PLAY_TIME = 0x4C     # u64  seconds
OFF_SAVE_NAME = 0x58           # 128 bytes, NUL-terminated UTF-8
OFF_SAVE_SUMMARY = 0xD8        # 128 bytes, NUL-terminated UTF-8
OFF_DIFFICULTY = 0x158         # u32 (Worlds; 1 byte in Waypoint)
OFF_SLOT_IDENTIFIER = 0x15C    # 8 bytes (libNOM leaves this untouched)
OFF_TIMESTAMP = 0x164          # u32  unix seconds; what the load screen sorts "newest" by
OFF_META_FORMAT_TAIL = 0x168   # u32  meta format, repeated

NAME_FIELD_LENGTH = 0x80  # 128
SUMMARY_FIELD_LENGTH = 0x80  # 128

# --- Save-slot model ---------------------------------------------------------

MAX_SAVE_SLOTS = 15
MAX_SAVE_PER_SLOT = 2
MAX_SAVE_FILES = MAX_SAVE_SLOTS * MAX_SAVE_PER_SLOT  # 30

# Account-level files (not a slot).
ACCOUNT_DATA_NAME = "accountdata.hg"
ACCOUNT_META_NAME = "mf_accountdata.hg"
ACCOUNT_STORAGE_ORDINAL = 1  # cTkStoragePersistent::Slot.AccountData

# Other files that belong to a save folder.
STEAM_AUTOCLOUD = "steam_autocloud.vdf"
CACHE_DIR = "cache"
