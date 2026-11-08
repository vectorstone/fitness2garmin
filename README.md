# fitness2garmin

Apple Health（Apple Watch）完整历史 → Garmin Connect 迁移流水线。

**Pipeline**：`export.zip` 流式解析（workouts + GPS 路线 + 训练内心率）→ FIT 编码（官方 garmin-fit-sdk）→ python-garminconnect 静默批量上传（token 缓存、409 去重、2s 节流、SQLite 幂等账本）。

## Why

- 苹果健康 App 的「导出所有健康数据」是唯一覆盖**全部历史**的导出方式；但 `export.xml` 里训练心率是粗粒度聚合，GPS 轨迹单独放在 `workout-routes/*.gpx`——本工具把它们重新拼装成每个训练一个标准 FIT 文件
- Garmin 官方 Personal API（2026-10）是只读的；个人可用的上传通道是非官方 [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) 0.3.20+（与手机 App 同端点、支持 MFA、token 缓存后全程静默）
- 重复上传不会被覆盖（Garmin 按开始时间返回 409），所以先 `plan` 比对、替换残缺旧数据、再增量上传

## 已知边界

- **步数/睡眠/HRV 等每日指标没有任何 Garmin API 写入通道**（官方与非官方均无），本工具只迁移训练活动 + 体重（`add_body_composition`）
- Garmin China 账户加 `--cn` 登录
- 大规模回传时 Garmin 会限流（429），上传按 2s/文件节流；失败可重跑，账本保证幂等

## 安装

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
# 之后所有命令用 .venv/bin/f2g
```

## 使用（按顺序）

```bash
# 0. 把 iPhone 导出的压缩包放到 data/export.zip（健康 App → 头像 → 导出所有健康数据）

# 1. 一次性登录（终端输入密码 + MFA，token 存 data/garmin_tokens.json，之后静默）
f2g login [--cn]

# 2. 解析导出包（流式，GB 级 export.xml 不会爆内存）
f2g parse [--zip data/export.zip]

# 3. 每个训练生成一个 FIT（data/fit/）
f2g convert

# 4. 拉取 Garmin 现有活动清单
f2g inventory

# 5. 生成迁移计划：upload / replace（残缺待替换）/ skip / garmin_only
f2g plan          # 查看 data/plan.json 和报告

# 6. 删除 Garmin 上的残缺活动（先 dry-run 看，确认后 --apply）
f2g clean --apply

# 7. 静默批量上传（409 自动跳过、SQLite 账本去重、2s 节流）
f2g upload --include-replace

# 8. 可选：同步体重记录
f2g weight

# 状态总览
f2g status

# 一键跑到 plan（不删不传）
f2g sync
```

凭据只经终端 `getpass` 输入，token 落在 `data/`（已 gitignore），绝不进仓库。

## 测试

```bash
.venv/bin/python tests/test_pipeline.py
```

合成 export.zip → 解析 → JSON 往返 → FIT 编码/解码校验（记录数、sport、分段、距离缩放）→ 迁移计划比对，全链路无网络依赖。

## 设计要点

| 模块 | 职责 |
|---|---|
| `apple_export.py` | 两遍流式 iterparse：先收 workouts（含 pause/resume 事件、route 引用、HR 统计），再收窗口内心率 + 体重；GPX 按 FileReference 匹配、失配时按时间窗兜底 |
| `fit_builder.py` | 官方 SDK 编码 file_id/event/record/lap/session/activity；GPS 轨迹上贴最近心率（±5s）；GPS 累计距离按苹果官方总距离缩放消漂移；pause/resume 切分 lap |
| `garmin_client.py` | garminconnect 封装：token 复用、MFA、盘点（按年分页）、上传（409→duplicate）、删除、体重 |
| `plan.py` | 开始时间 ±120s 匹配；时长差 >20%/30s 或距离差 >30% 判为残缺 → replace |
| `state.py` | SQLite 账本，重跑幂等 |

## License

MIT
