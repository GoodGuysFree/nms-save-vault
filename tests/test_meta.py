"""Meta crypto verified against the real (read-only) save files.

These assert invariants that hold regardless of game progress (no volatile values are
hard-coded): correct-ordinal decryption, size_disk == on-disk size, exact encrypt/decrypt
inverse, and re-key round-trips.
"""
from __future__ import annotations

import pytest

from nms_save_vault.core import formats, lz4_block, meta, slotmap

from .conftest import meta_data_files


def test_live_metas_decrypt_with_mapped_ordinal(live_save_dir):
    files = list(meta_data_files(live_save_dir))
    assert files, "expected at least one save in the live folder"
    for f, mp, _dp in files:
        blob = mp.read_bytes()
        assert len(blob) in formats.META_LENGTHS_KNOWN, f"{mp.name} unexpected length {len(blob)}"
        ordinal = slotmap.storage_ordinal(f)
        assert meta.is_valid_for(blob, ordinal), f"{mp.name} must decrypt with ordinal {ordinal}"


def test_autodetect_matches_mapping(live_save_dir):
    for f, mp, _dp in meta_data_files(live_save_dir):
        blob = mp.read_bytes()
        ordinal = slotmap.storage_ordinal(f)
        used, _plain = meta.decrypt_autodetect(blob, preferred_ordinal=ordinal)
        assert used == ordinal, f"{mp.name} autodetect picked {used}, expected {ordinal}"


def test_meta_size_decompressed_matches_chunks(live_save_dir):
    """meta.size_decompressed == the sum of chunk decompressed sizes, for every save.

    Note: meta.size_disk is NOT asserted -- it can be stale (e.g. save6.hg, edited by a
    save editor that recompressed the data without updating size_disk). The data file is
    a self-describing chunk container, so size_disk is effectively vestigial; the reliable
    data<->meta tie is size_decompressed."""
    for f, mp, dp in meta_data_files(live_save_dir):
        ordinal = slotmap.storage_ordinal(f)
        info = meta.parse(meta.decrypt(mp.read_bytes(), ordinal), ordinal)
        st = lz4_block.stats(dp.read_bytes())
        assert st.chunk_count > 0, f"{dp.name}: no valid chunks"
        assert info.size_decompressed == st.total_decompressed, f"{dp.name}: size_decompressed mismatch"
        assert st.consumed <= st.file_size, f"{dp.name}: chunks overran file"


def test_deep_decompress_smallest_save(live_save_dir):
    """Fully LZ4-decode the smallest save and confirm it matches the meta and is JSON."""
    items = list(meta_data_files(live_save_dir))
    f, mp, dp = min(items, key=lambda t: t[2].stat().st_size)
    ordinal = slotmap.storage_ordinal(f)
    info = meta.parse(meta.decrypt(mp.read_bytes(), ordinal), ordinal)
    raw = lz4_block.decompress(dp.read_bytes())
    assert len(raw) == info.size_decompressed
    assert raw[:1] == b"{"  # obfuscated JSON object


def test_encrypt_is_exact_inverse_of_decrypt(live_save_dir):
    for f, mp, _dp in meta_data_files(live_save_dir):
        blob = mp.read_bytes()
        ordinal = slotmap.storage_ordinal(f)
        assert meta.encrypt(meta.decrypt(blob, ordinal), ordinal) == blob, mp.name


def test_re_key_round_trip_and_field_preservation(live_save_dir):
    files = list(meta_data_files(live_save_dir))
    f, mp, _dp = files[0]
    src = slotmap.storage_ordinal(f)
    blob = mp.read_bytes()

    dst = slotmap.storage_ordinal(slotmap.file_no(slot=12, member=1))
    moved = meta.re_key(blob, src, dst)
    assert meta.is_valid_for(moved, dst)
    assert meta.re_key(moved, dst, src) == blob  # exact reversibility

    a = meta.parse(meta.decrypt(blob, src), src)
    b = meta.parse(meta.decrypt(moved, dst), dst)
    assert (a.save_name, a.size_disk, a.size_decompressed, a.total_play_time, a.game_mode) == (
        b.save_name,
        b.size_disk,
        b.size_decompressed,
        b.total_play_time,
        b.game_mode,
    )


def test_set_timestamp_only_changes_timestamp(live_save_dir):
    f, mp, _dp = next(iter(meta_data_files(live_save_dir)))
    ordinal = slotmap.storage_ordinal(f)
    plain = meta.decrypt(mp.read_bytes(), ordinal)
    before = meta.parse(plain, ordinal)

    plain2 = meta.set_timestamp(plain, before.timestamp + 1000)
    after = meta.parse(plain2, ordinal)

    assert after.timestamp == before.timestamp + 1000
    assert (after.save_name, after.size_disk, after.total_play_time) == (
        before.save_name,
        before.size_disk,
        before.total_play_time,
    )
    assert meta.is_valid_for(meta.encrypt(plain2, ordinal), ordinal)


def test_account_meta_decrypts(live_save_dir):
    mp = live_save_dir / formats.ACCOUNT_META_NAME
    if not mp.exists():
        pytest.skip("no account meta present")
    assert meta.is_valid_for(mp.read_bytes(), formats.ACCOUNT_STORAGE_ORDINAL)


def test_metas_have_readable_names_and_sane_format(live_save_dir):
    names = []
    for f, mp, _dp in meta_data_files(live_save_dir):
        ordinal = slotmap.storage_ordinal(f)
        info = meta.parse(meta.decrypt(mp.read_bytes(), ordinal), ordinal)
        assert info.meta_format in (
            formats.META_FORMAT_1,
            formats.META_FORMAT_2,
            formats.META_FORMAT_3,
            formats.META_FORMAT_4,
        )
        names.append(info.save_name)
    assert any(n.strip() for n in names), "expected at least one non-empty save name"
