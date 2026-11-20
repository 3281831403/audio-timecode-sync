# audio-timecode-sync

## 题面

现场多机位录制接上时间码后，声画对齐全乱：解码端按固定 30 fps 处理，25 fps 素材每 6 帧就偏 1 帧；drop-frame 标志位读错一位，时间码永远差 2 帧；编码不写同步字，解码端找不到帧头；drop-frame 换算每分钟都丢，2 小时累计少 4 秒；baud 换算不按上升沿对齐，采样点偏移 1 ms；同步搜索要求严格 16 位匹配，一位抖动就丢整帧；多段拼接不去重，接缝处出现重复帧；帧率比用浮点 29.97，长片换算漂移 1 帧/小时；批量遇坏段只跳过，输出缺段；极性反转不处理，半段 LTC 解不出来。timecode/ltc.py 是核心，tests/test_ltc.py 15个红。要修的是：帧率与丢帧换算、同步字与极性、采样点对齐、多段拼接和批量回滚。

## 运行方式

在仓库根目录执行：

```powershell
python -m pytest tests -q
```

初始状态必须恰好输出 `15 failed`，且没有 collection error、import error 或 syntax error。修复完成后 15 条测试应全部通过。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 音视频处理 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 约束

- 仅使用 Python 标准库（math、collections）与 pytest；离线运行，不使用随机数，不调用 sleep。
- 帧率仅支持 24 / 25 / 29.97 / 30；时间码单位为帧，音频单位为采样点，帧级容差为 1 帧。
- LTC 用 80 bit 的 0/1 列表表示，音频位置用采样点列表表示；29.97 按 30000/1001 的有理数处理。
- `tests/test_ltc.py` 是验收测试，不可修改；不要通过改测试来让结果变绿。

## 文件

```text
audio-timecode-sync/
├── README.md
├── conftest.py
├── .gitignore
├── timecode/
│   ├── __init__.py
│   └── ltc.py
└── tests/
    └── test_ltc.py
```
