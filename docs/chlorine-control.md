# 加氯闭环控制

## 固定控制策略

1. **水质决定前馈方式**
   - 进水氨氮 `< 0.5 mg-N/L` 且较上一轮变化 `<= 0.2 mg-N/L`：`full_then_feedback`，前馈一次投足，到达接触时间后只做一次反馈修正。
   - 进水氨氮 `>= 0.5 mg-N/L`，或较上一轮变化 `> 0.2 mg-N/L`：`small_step_then_retest`，前馈只投一个保守小步，复测确认后才进入下一轮，避免在折点附近过量。计划中 `buildup=true` 表示高氨氮首轮还在爬坡，不代表已达到完整折点需量。
   - 阈值可在 `/chlor/config` 标定，但运行时不会在两种口径间自动漂移。
2. **接触时间口径固定为实测优先、设计兜底**
   - 有效流量 = 进水流量表读数 × 流量表校准系数。
   - 有效流量位于设计流量 20%–150% 时，接触时间 = 清水池有效体积 / 有效流量。
   - 流量丢失、为 0 或超出可信范围时，接触时间 = 有效体积 / 设计流量。
   - 全量模式的反馈闸门等于完整 HRT；小步模式的复测闸门固定为 `min(10 分钟, HRT)`，但每轮仍保存完整 HRT、复测时间和 `contact_source`，投加、反馈、审计使用同一份值。
3. **反馈必须在上一轮余氯确认后才能进入下一轮**
   - `/chlor/dose` 创建一个 round。
   - 未达到该 round 的接触时间，`/chlor/residual` 返回 409。
   - round 未关闭时，下一次 `/chlor/dose` 返回 409；相同 `request_id` 返回原 round，不重复投加。

## 前馈计算

默认单位：

- 目标/余氯：mg/L
- 氨氮：mg-N/L
- 流量：m³/h
- 投加浓度：mg/L
- 投加质量流量：kg/h = 流量 × 浓度 / 1000

浓度公式：

```text
D_full = target + target * (exp(k * HRT_hours) - 1) + r_NH3 * NH3-N + bias
```

- `r_NH3` 默认 7.6，可在 `/chlor/config` 标定。
- `k` 默认 0.03 1/h。
- 小步模式首轮投加 `min(0.25, 25% * D_full)` mg/L；若由氨氮变化触发，则小步按变化量折算，至少为 `10% * D_full`，且不超过 0.5 mg/L。
- `bias` 来自历史反馈，只用于模型修正；负反馈不会让现场立即“负投加”，会降低下一轮前馈量。

## 反馈修正

```text
error = target - measured_residual
correction = clamp(gain * error, -cap, cap)
```

- `|error| <= deadband`（默认 0.02 mg/L）时不修正。
- 正误差立即补氯，负误差更新 bias 并约束下一轮。
- 修正后 round 移入历史，当前 round 清空，才允许下一次前馈。

## 余氯表读数丢失

- `/chlor/residual/lost` 只把 round 标记为 `residual_reading_lost`，不补氯、不关闭 round。
- 丢失状态下不能开新一轮，也不能走普通余氯反馈。
- `/chlor/residual` 创建反馈审计时使用 `feedback_id`（或 `reading_id`）去重；重复提交返回第一次结果。
- 读数恢复后调用 `/chlor/residual/recover`：仍校验接触时间，然后只计算一次修正。
- `request_id`、`reading_id` 或固定 round/loss 键用于恢复幂等；重复恢复返回第一次结果，不重复审计、不重复投加。

## 控制台接口

| 接口 | 用途 |
| --- | --- |
| `POST /chlor/target` | 传 `target` 直接设目标；保留 `demand` 旧口径 |
| `GET/POST /chlor/config` | 体积、设计流量、氨氮比例、衰减、反馈增益/限幅/死区、水质阈值 |
| `POST /chlor/dose` | 按目标、氨氮、接触时间前馈投加并开 round |
| `GET /chlor/rounds` | 当前 round 和历史 round |
| `POST /chlor/residual/lost` | 标记余氯读数部分失败 |
| `POST /chlor/residual` | 接触时间后报实际余氯并反馈修正 |
| `POST /chlor/residual/recover` | 恢复丢失读数并幂等完成反馈 |

`/snapshot` 的 `chlor` 字段、`/report` 文本、`/health/checks` 的 `chlor_round`、`/audit` 和 `/export/audit` 均显示同一套 round、投加、余氯和修正口径。审计 CSV 的 `amount` 统一为 mg/L，kg/h 放在结构化 payload 中，避免把浓度和质量流量相加。

现场启用前应先通过 `/chlor/config` 写入真实清水池有效体积、设计流量，并按历史 C×T/折点数据标定氨氮比例和衰减系数；本服务负责执行策略和防重复，不替代工艺审批。
