# fitness2garmin

Apple Health (Apple Watch) → Garmin Connect 历史数据迁移流水线。

**Pipeline**: `export.zip` → 流式解析（workouts + GPS 路线 + HR）→ FIT 编码（garmin-fit-sdk）→ python-garminconnect 静默批量上传（token 缓存、409 去重、节流重试）。

## Status

WIP — 由调研结论驱动的设计见 README 后续章节。

- 数据源：Apple 健康 App 导出的 `export.zip`（完整历史）
- 目标：Garmin Connect 个人账户（非官方 API，与手机 App 同端点）
- 已知限制：步数/睡眠/HRV 等每日指标无 API 写入通道；体重/身体成分可写（`add_body_composition`）
