"""audio-timecode-sync 的验收测试：初始 15 条全红，不得修改。"""

import pytest

from timecode.ltc import (
    COLOR_FRAME_BIT,
    DROP_FRAME_BIT,
    FRAME_BITS,
    POLARITY_BIT,
    SYNC_START,
    SYNC_WORD,
    TimecodeError,
    bits_to_samples,
    decode_bits,
    drop_frame_adjust,
    encode_timecode,
    find_sync,
    merge_chunks,
    rate_ratio,
    sync_batch,
)


def _put(bits, value, start, width):
    """把 value 的低 width 位写入 bits[start:start+width]，LSB first。"""
    for offset in range(width):
        bits[start + offset] = (value >> offset) & 1


def _make_frame(hours, minutes, seconds, frame, fps=25,
                drop_frame=False, color_frame=False):
    """按 README 的 80 bit 布局独立构造一帧参考 LTC。"""
    nominal = 30 if fps == 29.97 else int(fps)
    assert 0 <= frame < nominal
    bits = [0] * FRAME_BITS
    _put(bits, frame % 10, 0, 4)
    _put(bits, frame // 10, 4, 2)
    bits[DROP_FRAME_BIT] = int(bool(drop_frame))
    bits[COLOR_FRAME_BIT] = int(bool(color_frame))
    _put(bits, seconds % 10, 8, 4)
    _put(bits, seconds // 10, 12, 3)
    _put(bits, minutes % 10, 16, 4)
    _put(bits, minutes // 10, 20, 3)
    _put(bits, hours % 10, 24, 4)
    _put(bits, hours // 10, 28, 2)
    bits[POLARITY_BIT] = sum(bits[:64]) % 2
    bits[SYNC_START:FRAME_BITS] = list(SYNC_WORD)
    return bits


def _tc(hours, minutes, seconds, frames, drop_frame=False):
    return {
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
        "frames": frames,
        "drop_frame": drop_frame,
    }


def test_25fps_decode_does_not_assume_30fps():
    """缺陷 1：固定 30 fps 解码会让 25 fps 素材每 6 帧偏 1 帧。"""
    decoded = decode_bits(_make_frame(0, 0, 0, 12, fps=25), 25)
    frame = decoded["frames"][0]
    assert frame["total_frames"] == 12
    assert abs(frame["elapsed_seconds"] - 12 / 25) < 1 / 25


def test_drop_frame_flag_is_bit_6_not_neighbor():
    """缺陷 2：drop-frame 标志读错位会让时间码永远差 2 帧。"""
    df = decode_bits(
        _make_frame(0, 1, 0, 2, fps=29.97, drop_frame=True), 29.97
    )["frames"][0]
    assert df["drop_frame"] is True
    assert df["total_frames"] == 1800

    ndf = decode_bits(
        _make_frame(0, 0, 0, 0, fps=29.97, drop_frame=False,
                    color_frame=True),
        29.97,
    )["frames"][0]
    assert ndf["drop_frame"] is False


def test_encode_writes_sync_word():
    """缺陷 3：编码不写同步字，解码端找不到帧头。"""
    bits = encode_timecode(_tc(1, 2, 3, 4), 25)
    assert len(bits) == FRAME_BITS
    assert bits[SYNC_START:FRAME_BITS] == list(SYNC_WORD)
    assert sum(bits[:64]) % 2 == 0


def test_drop_frame_skips_tenth_minute():
    """缺陷 4：每分钟都丢会让 2 小时累计失准，整十分钟必须不丢。"""
    assert drop_frame_adjust(216_000, 29.97) == 215_784
    assert drop_frame_adjust(2_592_000, 29.97) == 2_589_408


def test_bits_to_samples_aligns_rising_edge():
    """缺陷 5：baud 换算不按上升沿对齐，采样点会偏移。"""
    assert bits_to_samples([0, 1, 0, 1], 48000, 2400) == [0, 20, 40, 60]
    assert bits_to_samples([0, 1, 0, 1, 0], 44100, 2400) == [0, 18, 37, 55, 74]


def test_find_sync_tolerates_one_bit_jitter():
    """缺陷 6：严格 16 位匹配时，一位抖动就会丢整帧。"""
    bits = _make_frame(1, 2, 3, 4, fps=25)
    assert find_sync(bits, 1) == SYNC_START
    shifted = [0] + bits
    assert find_sync(shifted, 1) == SYNC_START + 1


def test_merge_chunks_dedupes_seam_and_fills_gap():
    """缺陷 7：多段拼接不去重，接缝处会出现重复帧。"""
    seam = merge_chunks(
        [
            {"start": 0, "frames": [0, 1, 2]},
            {"start": 2, "frames": [2, 3, 4]},
        ],
        25,
    )
    assert seam["frames"] == [0, 1, 2, 3, 4]
    assert seam["duplicates_removed"] == 1
    assert seam["gaps_filled"] == 0

    gap = merge_chunks(
        [
            {"start": 0, "frames": [0, 1, 2]},
            {"start": 4, "frames": [4, 5]},
        ],
        25,
    )
    assert gap["frames"] == [0, 1, 2, 3, 4, 5]
    assert gap["gaps_filled"] == 1


def test_rate_ratio_uses_exact_rational():
    """缺陷 8：浮点 29.97 会让长片换算漂移 1 帧/小时。"""
    assert abs(rate_ratio(24, 29.97) - 24024 / 30000) <= 1e-9
    assert abs(rate_ratio(30, 29.97) - 1001 / 1000) <= 1e-9


def test_sync_batch_rolls_back_on_bad_track():
    """缺陷 9：批量遇坏段只跳过，会让输出缺段。"""
    tracks = [
        {"name": "camA", "chunks": [{"start": 0, "frames": [0, 1]}]},
        {"name": "camB", "chunks": [{"start": 0, "frames": [0, 1]}]},
        {"name": "camBad", "chunks": []},
    ]
    snapshot = repr(tracks)
    with pytest.raises(TimecodeError):
        sync_batch(tracks, 25)
    assert repr(tracks) == snapshot


def test_decode_handles_inverted_polarity():
    """缺陷 10：极性反转不处理，半段 LTC 解不出来。"""
    bits = _make_frame(1, 2, 3, 4, fps=25)
    inverted = [1 - bit for bit in bits]
    frame = decode_bits(inverted, 25)["frames"][0]
    assert (
        frame["hours"], frame["minutes"], frame["seconds"], frame["frames"]
    ) == (1, 2, 3, 4)


def test_encode_decode_roundtrip_all_rates():
    """组合 1：encode 后再 decode，24/25/29.97 往返一致。"""
    cases = [
        (24, _tc(23, 59, 59, 23)),
        (25, _tc(1, 2, 3, 24)),
        (29.97, _tc(0, 10, 0, 0, drop_frame=True)),
    ]
    for fps, tc in cases:
        frame = decode_bits(encode_timecode(tc, fps), fps)["frames"][0]
        for key in ("hours", "minutes", "seconds", "frames", "drop_frame"):
            assert frame[key] == tc[key], (fps, key)


def test_find_sync_is_strict_when_max_shift_zero():
    """组合 2：max_shift=0 时只认标准位置，不容忍位偏移。"""
    bits = _make_frame(0, 0, 0, 0, fps=25)
    assert find_sync(bits, 0) == SYNC_START
    assert find_sync([0] + bits, 0) == -1


def test_bits_to_samples_rejects_nonpositive_baud():
    """组合 3：baud<=0 抛 TimecodeError。"""
    for bad_baud in (0, -2400):
        with pytest.raises(TimecodeError):
            bits_to_samples([0, 1], 48000, bad_baud)


def test_decode_empty_bits_returns_empty_structure():
    """组合 4：空 bits 返回空结构。"""
    assert decode_bits([], 25) == {
        "frames": [],
        "frame_count": 0,
        "drop_frame": False,
    }


def test_sync_batch_preserves_order_on_success():
    """组合 5：批量全成功时输出顺序与输入一致。"""
    tracks = [
        {"name": "camB", "chunks": [{"start": 0, "frames": [0, 1]}]},
        {"name": "camA", "chunks": [{"start": 10, "frames": [10, 11, 12]}]},
    ]
    result = sync_batch(tracks, 25)
    assert result["track_names"] == ["camB", "camA"]
    assert [track["name"] for track in result["tracks"]] == ["camB", "camA"]
    assert [track["total_frames"] for track in result["tracks"]] == [2, 3]
