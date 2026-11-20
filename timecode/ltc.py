"""LTC（Linear Timecode）位级编解码、丢帧换算与批量同步。

公共约定
========
帧率
    仅支持 24 / 25 / 29.97 / 30 fps。29.97 按精确有理数 30000/1001
    处理，其名义帧率为 30；drop-frame 时间码仍以 30 帧/秒计数。
单位
    时间码与帧号使用“帧”；音频位置使用“采样点”（sample）。
    LTC 用 0/1 的 ``list[int]`` 表示，每帧固定 80 bit，bit 0 先出现。
容差
    涉及帧的换算与比较允许 1 帧误差；帧率比误差要求不超过 1e-9。
异常
    非法帧率、非法时间码、坏位流、非正 sample_rate/baud 等一律抛
    :class:`TimecodeError`。

80 bit 帧布局（LSB first）
==========================
======  ====================================================
bit     含义
======  ====================================================
0..3    帧个位（BCD）
4..5    帧十位（2 bit）
6       drop-frame 标志
7       color-frame 标志
8..11   秒个位（BCD）
12..14  秒十位（3 bit）
15      极性修正位：使 bit 0..63 中 1 的个数为偶数
16..19  分个位（BCD）
20..22  分十位（3 bit）
23      binary-group 标志
24..27  时个位（BCD）
28..29  时十位（2 bit）
30..31  保留
32..63  用户位
64..79  同步字 0x3FFD（LSB first）
======  ====================================================
"""

SYNC_WORD = (0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1)
"""16 bit LTC 同步字，对应 0x3FFD，按 bit 64..79 的发送顺序排列。"""

FRAME_BITS = 80
SYNC_START = 64
DROP_FRAME_BIT = 6
COLOR_FRAME_BIT = 7
POLARITY_BIT = 15


class TimecodeError(ValueError):
    """非法时间码、帧率、LTC 位流或音频参数。"""


# 帧率的精确有理数表示：fps -> (分子, 分母)。
_RATE_RATIONALS = {
    24: (24000, 1000),
    25: (25000, 1000),
    29.97: (30000, 1001),
    30: (30000, 1000),
}

SECONDS_PER_DAY = 24 * 60 * 60
# SMPTE drop-frame 每分钟名义帧数，以及每非整十分钟丢弃的帧数。
NOMINAL_FRAMES_PER_MINUTE = 30 * 60
DROP_FRAMES_PER_MINUTE = 2


def _normalize_fps(fps):
    """校验帧率并返回规范值（29.97 用 30000/1001 的名义值 29.97）。"""
    if isinstance(fps, bool) or not isinstance(fps, (int, float)):
        raise TimecodeError(f"非法帧率: {fps!r}")
    if isinstance(fps, float):
        # 同时接受字面量 29.97 与精确值 30000/1001。
        if abs(fps - 29.97) < 1e-3 or abs(fps - 30000 / 1001) < 1e-6:
            return 29.97
        if fps.is_integer():
            fps = int(fps)
    if isinstance(fps, int) and fps in (24, 25, 30):
        return fps
    raise TimecodeError(f"非法帧率: {fps!r}")


def _nominal_fps(fps):
    """名义帧率：29.97 按 30 帧/秒计数。"""
    return 30 if fps == 29.97 else int(fps)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _max_day_frames(fps):
    return _nominal_fps(fps) * SECONDS_PER_DAY


def _validate_bits(bits):
    if not isinstance(bits, (list, tuple)):
        raise TimecodeError("bits 必须是 0/1 列表")
    for bit in bits:
        if bit not in (0, 1):
            raise TimecodeError("bits 只能包含 0 或 1")


def _put(bits, value, start, width):
    """把 value 的低 width 位写入 bits[start:start+width]，LSB first。"""
    for offset in range(width):
        bits[start + offset] = (value >> offset) & 1


def _get(bits, start, width):
    """从 bits[start:start+width] 读取 width 位，LSB first。"""
    value = 0
    for offset in range(width):
        value |= bits[start + offset] << offset
    return value


def _is_illegal_drop_frame_value(total_minutes, seconds, frames, fps):
    """29.97 drop-frame 下，非整十分钟的 :00/:01 是非法跳号。"""
    return (
        fps == 29.97
        and total_minutes % 10 != 0
        and seconds == 0
        and frames in (0, 1)
    )


def _df_number_is_legal(number, fps):
    """判断名义帧号在 29.97 drop-frame 编号中是否合法。"""
    if fps != 29.97:
        return True
    if number < 0:
        return False
    minutes = number // NOMINAL_FRAMES_PER_MINUTE
    if minutes % 10 == 0:
        return True
    return number % NOMINAL_FRAMES_PER_MINUTE >= DROP_FRAMES_PER_MINUTE


def decode_bits(bits, fps):
    """把 LTC 位流按每 80 bit 一帧解码。

    参数
    ----
    bits : list[int]
        0/1 位列表，bit 0 是 LTC 帧的第一位。长度应为 80 的整数倍；
        末尾不足 80 bit 的残位忽略。每一帧允许整体极性反转（0/1
        互换），此时应根据同步字的互补形式识别并翻回。
    fps : int | float
        24、25、29.97 或 30。29.97 的名义帧率为 30。

    返回
    ----
    dict
        ``{"frames": [...], "frame_count": int, "drop_frame": bool}``。
        ``frames`` 中每一项为::

            {"hours": int, "minutes": int, "seconds": int,
             "frames": int, "drop_frame": bool,
             "total_frames": int, "elapsed_seconds": float}

        ``total_frames`` 是从 00:00:00:00 起的绝对帧序号：非
        drop-frame 按名义帧率计数；29.97 drop-frame 先按 30 fps 名义
        计数，再扣除每分钟 2 帧，但整十分钟（00、10、...、50 分）
        不扣。``elapsed_seconds`` 为 ``total_frames`` 除以实际帧率，
        29.97 的实际帧率为 30000/1001；允许 1 帧误差。

    空位流返回 ``{"frames": [], "frame_count": 0, "drop_frame": False}``。
    帧率不在支持范围、帧字段超出名义帧数、29.97 drop-frame 出现
    每分钟 :00/:01（整十分钟除外）等非法时间码，抛 TimecodeError。
    """
    fps = _normalize_fps(fps)
    _validate_bits(bits)

    nominal = _nominal_fps(fps)
    actual_fps = 30000 / 1001 if fps == 29.97 else float(nominal)

    frames = []
    stream_drop_frame = False

    for offset in range(0, len(bits) - FRAME_BITS + 1, FRAME_BITS):
        window = list(bits[offset:offset + FRAME_BITS])
        sync = window[SYNC_START:FRAME_BITS]

        # 整体极性反转的帧：同步字为互补形式时翻回。
        if sync == list(SYNC_WORD):
            pass
        elif [1 - bit for bit in sync] == list(SYNC_WORD):
            window = [1 - bit for bit in window]
        else:
            raise TimecodeError(f"第 {offset // FRAME_BITS} 帧同步字不匹配")

        frame_units = _get(window, 0, 4)
        frame_tens = _get(window, 4, 2)
        drop_frame = bool(window[DROP_FRAME_BIT])
        seconds_units = _get(window, 8, 4)
        seconds_tens = _get(window, 12, 3)
        minutes_units = _get(window, 16, 4)
        minutes_tens = _get(window, 20, 3)
        hours_units = _get(window, 24, 4)
        hours_tens = _get(window, 28, 2)

        if (
            frame_units > 9
            or seconds_units > 9
            or minutes_units > 9
            or hours_units > 9
        ):
            raise TimecodeError("时间码包含非法 BCD 位")

        frames_value = frame_tens * 10 + frame_units
        seconds = seconds_tens * 10 + seconds_units
        minutes = minutes_tens * 10 + minutes_units
        hours = hours_tens * 10 + hours_units

        if hours > 23 or minutes > 59 or seconds > 59:
            raise TimecodeError("时间码字段越界")
        if frames_value >= nominal:
            raise TimecodeError("帧字段超出名义帧数")
        if drop_frame and fps != 29.97:
            raise TimecodeError("非 29.97 帧率不允许 drop-frame")

        total_minutes = hours * 60 + minutes
        if _is_illegal_drop_frame_value(
            total_minutes, seconds, frames_value, fps
        ) and drop_frame:
            raise TimecodeError("29.97 drop-frame 出现非法跳号 :00/:01")

        nominal_total = (
            (hours * 3600 + minutes * 60 + seconds) * nominal + frames_value
        )
        if fps == 29.97 and drop_frame:
            drops = DROP_FRAMES_PER_MINUTE * (
                total_minutes - total_minutes // 10
            )
            total_frames = nominal_total - drops
        else:
            total_frames = nominal_total

        frames.append(
            {
                "hours": hours,
                "minutes": minutes,
                "seconds": seconds,
                "frames": frames_value,
                "drop_frame": drop_frame,
                "total_frames": total_frames,
                "elapsed_seconds": total_frames / actual_fps,
            }
        )
        if drop_frame:
            stream_drop_frame = True

    return {
        "frames": frames,
        "frame_count": len(frames),
        "drop_frame": stream_drop_frame,
    }


def encode_timecode(tc, fps):
    """把时间码编码成 80 bit 的 LTC 帧。

    参数
    ----
    tc : dict
        至少包含 ``hours``、``minutes``、``seconds``、``frames``，
        可选 ``drop_frame``（默认 False）。字段必须是整数：小时
        0..23，分/秒 0..59，帧小于名义帧率（29.97 为 30）。
    fps : int | float
        24、25、29.97 或 30。

    返回
    ----
    list[int]
        长度 80 的 0/1 位列表。bit 64..79 写入 :data:`SYNC_WORD`；
        bit 6 写入 drop-frame 标志；bit 15 为极性修正位，使
        bit 0..63 中 1 的个数为偶数。

    24/25/30 不允许 drop-frame；29.97 drop-frame 下，分钟不是 10 的
    倍数时 frames 不能为 00 或 01。非法帧率、越界时间码或非法
    drop-frame 组合抛 TimecodeError。
    """
    fps = _normalize_fps(fps)
    if not isinstance(tc, dict):
        raise TimecodeError("tc 必须是 dict")

    for key in ("hours", "minutes", "seconds", "frames"):
        if key not in tc:
            raise TimecodeError(f"时间码缺少字段: {key}")
        if not _is_int(tc[key]):
            raise TimecodeError(f"时间码字段必须是整数: {key}")

    hours = tc["hours"]
    minutes = tc["minutes"]
    seconds = tc["seconds"]
    frame = tc["frames"]
    drop_frame = bool(tc.get("drop_frame", False))

    if drop_frame and fps != 29.97:
        raise TimecodeError("非 29.97 帧率不允许 drop-frame")

    nominal = _nominal_fps(fps)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59):
        raise TimecodeError("时间码字段越界")
    if not (0 <= frame < nominal):
        raise TimecodeError("帧字段超出名义帧数")
    if drop_frame and seconds == 0 and frame in (0, 1) and minutes % 10 != 0:
        raise TimecodeError("29.97 drop-frame 不允许 :00/:01")

    bits = [0] * FRAME_BITS
    _put(bits, frame % 10, 0, 4)
    _put(bits, frame // 10, 4, 2)
    bits[DROP_FRAME_BIT] = int(drop_frame)
    _put(bits, seconds % 10, 8, 4)
    _put(bits, seconds // 10, 12, 3)
    _put(bits, minutes % 10, 16, 4)
    _put(bits, minutes // 10, 20, 3)
    _put(bits, hours % 10, 24, 4)
    _put(bits, hours // 10, 28, 2)

    # 极性修正位：使 bit 0..63 中 1 的个数为偶数。
    bits[POLARITY_BIT] = sum(bits[:64]) % 2
    bits[SYNC_START:FRAME_BITS] = list(SYNC_WORD)
    return bits


def drop_frame_adjust(frames, fps):
    """把 29.97 的名义帧序号换算成 drop-frame 帧序号。

    参数
    ----
    frames : int
        按名义 30 fps 连续计数的非负帧号。
    fps : int | float
        24、25、29.97 或 30。只有 29.97 需要换算；其余帧率原样返回。

    返回
    ----
    int
        SMPTE drop-frame 帧号：每个非整十分钟的分钟扣 2 帧，整十分钟
        不扣，即 ``drops = 2 * (minutes - minutes // 10)``，其中
        ``minutes = frames // 1800``。2 小时 216000 名义帧换算为
        215784 帧，24 小时 2592000 名义帧换算为 2589408 帧，累计
        不漂移。帧号单位是帧，允许 1 帧误差。

    非法帧率或负帧号抛 TimecodeError。
    """
    fps = _normalize_fps(fps)
    if not _is_int(frames) or frames < 0:
        raise TimecodeError("帧号必须是非负整数")
    if fps != 29.97:
        return frames
    minutes = frames // NOMINAL_FRAMES_PER_MINUTE
    drops = DROP_FRAMES_PER_MINUTE * (minutes - minutes // 10)
    return frames - drops


def bits_to_samples(bits, sample_rate, baud):
    """把 LTC 位序列映射为音频采样点位置。

    参数
    ----
    bits : list[int]
        0/1 位列表。
    sample_rate : int
        音频采样率，单位 Hz（采样点/秒），必须为正。
    baud : int
        LTC 波特率，单位 bit/s，必须为正。

    返回
    ----
    list[int]
        与 ``bits`` 等长；第 i 位对应采样点::

            round_half_up(i * sample_rate / baud)

        第 0 位固定为 0，即从上升沿开始对齐。例：48000 Hz、
        2400 baud 时每 bit 20 点，``[0, 1, 0, 1]`` 返回
        ``[0, 20, 40, 60]``。

    sample_rate <= 0 或 baud <= 0 抛 TimecodeError。
    """
    if not _is_int(sample_rate) or sample_rate <= 0:
        raise TimecodeError("sample_rate 必须为正整数")
    if not _is_int(baud) or baud <= 0:
        raise TimecodeError("baud 必须为正整数")
    _validate_bits(bits)

    # round_half_up(i * sample_rate / baud) 用整数运算避免浮点误差：
    # round_half_up(a / b) == (2 * a + b) // (2 * b)。
    denominator = 2 * baud
    return [
        (2 * index * sample_rate + baud) // denominator
        for index in range(len(bits))
    ]


def find_sync(bits, max_shift):
    """在 LTC 帧尾附近搜索 16 bit 同步字。

    LTC 的同步字标准位置是一帧的 bit 64..79。以 bit 64 为中心，
    在 ``[-max_shift, +max_shift]`` 的位偏移范围内逐位尝试；候选
    窗口必须与 :data:`SYNC_WORD` 的 16 位完全相等（允许位置偏移，
    但不允许位翻转）。

    参数
    ----
    bits : list[int]
        0/1 位列表。可以比一帧长，例如位抖动导致同步字后移一位。
    max_shift : int
        允许的同步字位置偏移，单位 bit。``max_shift=0`` 时只检查
        标准位置 bit 64；负值抛 TimecodeError。

    返回
    ----
    int
        命中窗口的起始下标。多个候选时返回绝对偏移最小者，其次
        下标最小者；没有命中返回 -1。本函数只做 bit 级定位，与
        帧率无关。
    """
    if not _is_int(max_shift) or max_shift < 0:
        raise TimecodeError("max_shift 必须是非负整数")
    _validate_bits(bits)

    sync_word = list(SYNC_WORD)
    bits = list(bits)

    # 按 |shift| 从小到大枚举，保证绝对偏移最小者先被命中；同一
    # 绝对偏移下先试负向再试正向，即下标较小者优先。
    for magnitude in range(max_shift + 1):
        if magnitude == 0:
            shifts = (0,)
        else:
            shifts = (-magnitude, magnitude)
        for shift in shifts:
            start = SYNC_START + shift
            if start < 0 or start + len(sync_word) > len(bits):
                continue
            if bits[start:start + len(sync_word)] == sync_word:
                return start
    return -1


def merge_chunks(chunks, fps):
    """拼接多段 LTC 帧序号，去重并补齐缺口。

    参数
    ----
    chunks : list[dict]
        ``[{"start": int, "frames": [int, ...]}, ...]``。每段
        ``frames`` 是该段包含的绝对帧序号（从 0 开始），通常递增；
        ``start`` 是该段第一帧的帧号。
    fps : int | float
        24、25、29.97 或 30，用于校验 drop-frame 时间码。

    返回
    ----
    dict
        ``{"frames": list[int], "total_frames": int,
        "duplicates_removed": int, "gaps_filled": int}``。
        合并规则：

        - 所有段按 ``start`` 排序后合并；
        - 接缝处重复出现的绝对帧号只保留一次；
        - 从最小帧号到最大帧号之间缺失的帧号补齐；
        - 29.97 drop-frame 下，每分钟的 :00/:01（整十分钟除外）
          属于非法跳号，不补。

    空 ``chunks`` 或所有段都没有帧时返回
    ``{"frames": [], "total_frames": 0, "duplicates_removed": 0,
    "gaps_filled": 0}``。帧率非法、chunk 结构非法或帧号越界抛
    TimecodeError。
    """
    fps = _normalize_fps(fps)
    if not isinstance(chunks, (list, tuple)):
        raise TimecodeError("chunks 必须是列表")

    validated = _validate_chunks(chunks, fps)
    return _merge_validated(validated, fps)


def _validate_chunks(chunks, fps):
    """校验 chunk 结构与帧号，返回 (start, frames) 元组列表。"""
    max_frames = _max_day_frames(fps)
    validated = []
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            raise TimecodeError(f"第 {index} 段结构非法")
        if "start" not in chunk or "frames" not in chunk:
            raise TimecodeError(f"第 {index} 段缺少 start/frames")
        if not _is_int(chunk["start"]) or chunk["start"] < 0:
            raise TimecodeError(f"第 {index} 段 start 非法")
        start = chunk["start"]
        frames = chunk["frames"]
        if not isinstance(frames, (list, tuple)):
            raise TimecodeError(f"第 {index} 段 frames 必须是列表")
        for frame in frames:
            if not _is_int(frame) or frame < 0:
                raise TimecodeError(f"第 {index} 段帧号非法")
            if frame >= max_frames:
                raise TimecodeError(f"第 {index} 段帧号越界")
        validated.append((start, list(frames)))
    return validated


def _merge_validated(validated, fps):
    """合并已校验的 (start, frames) 段。

    先按 start 稳定排序；每一段内部只接受相对该段起点连续递增的
    帧号，接缝处重复的帧号去重。缺口按 drop-frame 合法性补齐。
    """
    if not validated:
        return {
            "frames": [],
            "total_frames": 0,
            "duplicates_removed": 0,
            "gaps_filled": 0,
        }

    ordered = sorted(validated, key=lambda item: item[0])

    all_frames = set()
    for _, frames in ordered:
        all_frames.update(frames)

    if not all_frames:
        return {
            "frames": [],
            "total_frames": 0,
            "duplicates_removed": 0,
            "gaps_filled": 0,
        }

    lowest = min(all_frames)
    highest = max(all_frames)

    duplicates_removed = 0
    gaps_filled = 0
    # 从低到高整合：已有帧只保留一次，缺失的合法帧号补齐。
    present = set()
    for _, frames in ordered:
        for frame in frames:
            if frame in present:
                duplicates_removed += 1
            else:
                present.add(frame)

    merged = []
    for frame in range(lowest, highest + 1):
        if frame in present:
            merged.append(frame)
        else:
            # drop-frame 下被合法跳过的帧号不补，属于跳号而非缺口。
            if fps == 29.97 and not _df_number_is_legal(frame, fps):
                continue
            merged.append(frame)
            gaps_filled += 1

    return {
        "frames": merged,
        "total_frames": len(merged),
        "duplicates_removed": duplicates_removed,
        "gaps_filled": gaps_filled,
    }


def rate_ratio(from_fps, to_fps):
    """返回 from_fps 到 to_fps 的精确有理数帧率比。

    帧率按整数有理数处理：24 -> 24000/1000，25 -> 25000/1000，
    29.97 -> 30000/1001，30 -> 30000/1000。返回值为::

        (num_from * den_to) / (den_from * num_to)

    的 float，不得先把 29.97 舍入成浮点再相除；24 -> 29.97 的结果
    应为 24024/30000 = 0.8008，与精确值的误差不超过 1e-9。帧率非法
    抛 TimecodeError。
    """
    from_fps = _normalize_fps(from_fps)
    to_fps = _normalize_fps(to_fps)
    num_from, den_from = _RATE_RATIONALS[from_fps]
    num_to, den_to = _RATE_RATIONALS[to_fps]
    return (num_from * den_to) / (den_from * num_to)


def sync_batch(tracks, fps):
    """批量对齐多轨 LTC，成功时保持输入顺序，失败时整体回滚。

    参数
    ----
    tracks : list[dict]
        有序列表，每项为::

            {"name": str,
             "chunks": [{"start": int, "frames": [int, ...]}, ...]}

    fps : int | float
        24、25、29.97 或 30。

    返回
    ----
    dict
        ``{"tracks": [{"name": str, "frames": list[int],
        "total_frames": int}, ...], "track_names": list[str],
        "total_tracks": int}``。``tracks`` 与 ``track_names`` 的顺序
        必须与输入完全一致。

    任一条 track 非法（缺少 name、chunks 为空、chunk 无 frames、
    帧号越界等）都必须在产生任何输出或修改输入之前抛
    TimecodeError；不得跳过坏段，也不得返回部分结果。帧率非法同样
    抛 TimecodeError。
    """
    fps = _normalize_fps(fps)
    if not isinstance(tracks, (list, tuple)):
        raise TimecodeError("tracks 必须是列表")

    # 第一阶段：只读取、只校验，不产生输出也不修改输入。
    validated = []
    for index, track in enumerate(tracks):
        if not isinstance(track, dict):
            raise TimecodeError(f"第 {index} 条 track 结构非法")
        if "name" not in track or not isinstance(track["name"], str):
            raise TimecodeError(f"第 {index} 条 track 缺少合法 name")
        chunks = track.get("chunks")
        if not isinstance(chunks, (list, tuple)) or not chunks:
            raise TimecodeError(f"第 {index} 条 track chunks 为空")
        for chunk_index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                raise TimecodeError(
                    f"第 {index} 条 track 第 {chunk_index} 段结构非法"
                )
            if "frames" not in chunk:
                raise TimecodeError(
                    f"第 {index} 条 track 第 {chunk_index} 段缺少 frames"
                )
        validated.append((track["name"], _validate_chunks(chunks, fps)))

    # 第二阶段：全部校验通过后才合并、才产生输出。
    results = []
    for name, chunks in validated:
        merged = _merge_validated(chunks, fps)
        results.append(
            {
                "name": name,
                "frames": merged["frames"],
                "total_frames": merged["total_frames"],
            }
        )

    return {
        "tracks": results,
        "track_names": [name for name, _ in validated],
        "total_tracks": len(results),
    }
