"""LTC 时间码处理包。"""

from .ltc import (
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

__all__ = [
    "TimecodeError",
    "bits_to_samples",
    "decode_bits",
    "drop_frame_adjust",
    "encode_timecode",
    "find_sync",
    "merge_chunks",
    "rate_ratio",
    "sync_batch",
]
