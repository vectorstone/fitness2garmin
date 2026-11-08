# fitness2garmin

把 Apple Watch 的**全部历史健身数据**完整迁移到 Garmin Connect 的命令行流水线：训练活动（GPS 轨迹 + 逐秒心率 + 分段）→ 标准 FIT 文件 → 静默批量上传。

> 完整的迁移实录（含踩坑记录）：[docs/migration-story.md](docs/migration-story.md)

## 解决什么问题

换表（Apple Watch → Garmin）时，历史数据面临三道墙：

1. **Apple 的导出不是给机器读的**：`export.xml` 里训练心率是 ~15 分钟聚合块，GPS 轨迹散落在 `workout-routes/*.gpx`，需要重新拼装
2. **Garmin 没有官方写入通道**：Personal API 只读，网页导入只收 Fitbit CSV（每日汇总、一次一年）——训练活动进不去
3. **重复上传会被拒绝而不是覆盖**（409）：之前导入过残缺数据的账户，盲目重传只会被挡住，需要先比对、再清理、再增量上传

本工具一次解决：流式解析 → FIT 编码 → 与 Garmin 现有活动比对生成迁移计划 → 幂等静默上传。重复训练（如 Keep 的双写）自动折叠，残缺旧导入可安全清理。

## 已验证规模

真实迁移：981 条训练 → 789 条上传 + 161 条重复折叠 + 111 条无数据跳过；68 条体重记录；清理 127 条历史碎片；全程 0 失败 0 重复。

## 安装

```bash
git clone https://github.com/vectorstone/fitness2garmin
cd fitness2garmin
python3 -m venv .venv && .venv/bin/pip install -e .
```

要求 Python ≥ 3.12。核心依赖：官方 [garmin-fit-sdk](https://github.com/garmin/fit-python-sdk) + [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) ≥ 0.3.20。

## 使用

```bash
# 0. iPhone 上：健康 App → 头像 → 导出所有健康数据，把 export.zip 放到 data/

# 1. 一次性登录（终端输密码 + MFA 码；国区账户加 --cn）
f2g login [--cn]

# 2~5. 一键跑到迁移计划（解析 → FIT → 拉取 Garmin 清单 → 比对）
f2g sync

#    或者分步执行：
f2g parse [--zip data/export.zip]   # export.zip → data/workouts.json（流式，GB 级不爆内存）
f2g convert                         # 每个训练一个 FIT → data/fit/
f2g inventory                       # Garmin 现有活动 → data/garmin_activities.json
f2g plan                            # 迁移计划 → data/plan.json

# 6. 清理旧导入的残缺碎片（garmin-only + 2026 前 + other 类型；先 dry-run）
f2g clean-fragments [--apply]

# 7. 静默批量上传（409 自动跳过、429 退避重试、SQLite 账本幂等）
f2g upload

# 8. 可选：体重记录
f2g weight

# 状态总览
f2g status
```

凭据只经终端 `getpass` 输入，token 缓存在 `data/garmin_tokens.json`（已 gitignore），首次登录后全程静默。

## Keep 历史数据补迁

如果早期用 Keep 记录运动（后来才换 Apple Watch / Garmin），Keep 导出的数据可以补迁到 Garmin，并与现有活动自动去重：

```bash
# 0. Keep App → 我 → 设置 → 数据导出，把导出目录放到 data/565994/
#    （目录里应有 <id>.xlsx 和 bundle-<id>.zip）

# 1~3. 一键：解析 → 与 Garmin 现有活动比对 → 生成 FIT
f2g keep-sync

#    或分步：
f2g keep-parse                     # xlsx + zip → data/keep_workouts.json + 摘要 FIT
f2g keep-plan                      # 去重比对 → data/keep_plan.json
f2g keep-convert                   # 有心率序列的重建为带 record 的 FIT

# 4. 修复垃圾时长活动（Keep 双写壳经 Apple Health 带进 Garmin 的 18~35 小时"跑步"）
f2g keep-clean [--apply]

# 5. 上传 Keep 独有活动
f2g keep-upload [--include-replace]
```

Keep 计划的判定逻辑：

| 动作 | 含义 |
|---|---|
| `upload` | Keep 有、Garmin 没有（如 2021 年手机记录的步行、没进 Apple Health 的健身课） |
| `skip` | 已在 Garmin：Apple 时期经健康 App 同步的训练、Garmin 原生手表活动（含跑步机）、Apple 记为 "Other" 的健身课 |
| `replace` | Garmin 里的双写垃圾壳（时长虚高达数十小时）→ 用 Keep 真实数据删旧传新 |
| `junk` / `twin_ghost` | 60 秒以下无数据碎片 / Keep 自身的双写空壳 |

匹配口径与苹果流水线一致：`startTimeGMT` UTC 绝对时刻 ±120s 一对一比对；带 GPS 轨迹的现有活动永不替换。心率序列在导出里没有时间戳，按训练时长均匀分布（与无 GPS 训练的距离插值同口径）。

## 迁移计划的判定逻辑

`f2g plan` 对每条数据给出五种动作：

| 动作 | 含义 |
|---|---|
| `upload` | 苹果有、Garmin 没有 → 待上传 |
| `skip` | 已在 Garmin 且完整（开始时间 ±120s 匹配，时长/距离/轨迹一致） |
| `replace` | 已在 Garmin 但残缺（时长差 >20% / 距离差 >30% / 苹果有轨迹而 Garmin 无）→ 删旧传新 |
| `apple_dup` | 苹果侧重复（同类型、重叠 ≥80%，如 Keep 双写空壳）→ 折叠 |
| `no_data` / `garmin_only` | 无法生成 FIT / Garmin 独有（手表原生活动，只报告不触碰） |

## 已知限制

- **步数、睡眠、HRV、每日心率无法迁移**——Garmin 没有任何（官方或非官方）写入 API，这是平台硬限制
- 非官方 API 存在变更/限流风险；已做 2s/文件节流和 429 退避，中断可重跑
- 无心率且无轨迹的训练无法生成 FIT（FIT 活动文件至少需要一个数据系列）

## 设计概览

| 模块 | 职责 |
|---|---|
| `apple_export.py` | 两遍流式 iterparse：训练+事件+轨迹引用+心率统计；窗口内心率挂到所有包含窗口；GPX 按引用匹配 + 时间窗兜底 |
| `fit_builder.py` | 官方 SDK 编码 file_id/event/record/lap/session/activity；GPS 轨迹贴最近心率（±5s）；累计距离按苹果官方总距离缩放；pause/resume 切分 lap；文件名含起止时间防碰撞 |
| `garmin_client.py` | garminconnect 封装：区服持久化（国区/全球）、token 复用、MFA、按年盘点、409→duplicate、429 退避、删除、体重 |
| `plan.py` | 匹配与完整性判定、苹果侧孪生折叠 |
| `keep_export.py` | Keep 导出解析（xlsx 字段 + bundle FIT 的权威 UTC 起点），转换为 Workout |
| `keep_plan.py` | Keep ↔ Garmin 类型感知去重（跑步含跑步机、健身按时间重叠匹配 "Other"）、垃圾时长判定 |
| `state.py` | SQLite 账本，幂等重跑 |

## 测试

```bash
.venv/bin/python tests/test_pipeline.py
.venv/bin/python tests/test_keep.py
```

合成 export.zip → 解析 → JSON 往返 → FIT 编码/解码校验（记录数、sport 映射、暂停分段、距离缩放、孪生折叠）→ 迁移计划判定，全链路离线运行，不需要 Garmin 账户。`test_keep.py` 用合成的 Keep 导出（xlsx + bundle zip）覆盖 keep-parse/plan/convert 全链路。

## License

MIT
