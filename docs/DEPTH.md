# 深度登记 门店积分到期与退货逆向冲销

规模：中型。使用者：连锁零售会员运营专员。核心规则：消费先用最早到期批次；退款按原消费分配逆向恢复，已过期部分不得复活。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 事件采信 | 执行并复核事件采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 权益规则 | 执行并复核权益规则 | `POST /api/policy` | `app/modules/policy/rules.py` | `tests/test_policy.py` |
| M3 积分归批 | 执行并复核积分归批 | `POST /api/lots` | `app/modules/lots/rules.py` | `tests/test_lots.py` |
| M4 消费到期 | 执行并复核消费到期 | `POST /api/consumption` | `app/modules/consumption/rules.py` | `tests/test_consumption.py` |
| M5 退货回拨 | 执行并复核退货回拨 | `POST /api/refund` | `app/modules/refund/rules.py` | `tests/test_refund.py` |
| M6 余额对账 | 执行并复核余额对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 期末归档 | 执行并复核期末归档 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |
| M8 等级发布 | 重算并发布会员等级 | `POST /api/level`、`POST /api/level/publish` | `app/modules/level/rules.py`、`app/store.py` | `tests/test_level.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030权益中文 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_gb18030_real` |
| TRAP-02 | BOM表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_real` |
| TRAP-08 | 会员前导零 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_member_leading_zero` |
| TRAP-04 | 重复表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_repeated_header` |
| TRAP-05 | 缺列记录 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_column_bad` |
| TRAP-14 | 编号冲突 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_duplicate_conflict` |
| TRAP-18 | 空态 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_empty_three` |
| TRAP-07 | 哨兵 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_sentinel_real` |
| TRAP-11 | UTC归一 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_utc_real` |
| TRAP-06 | 全角积分 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_fullwidth_points` |
| TRAP-23 | 权益半开窗 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_rule_end_exclusive` |

## 对账规则

原始逻辑输入=采纳行+隔离行+空行+重复表头+同值重复。业务计算按 DATA-DICTIONARY.md 分别核验，不能把上级与下级重复加总。所有隔离保留编号、原值、原始行号和稳定原因码。

## 已知不覆盖

数据来自仓内样例或用户粘贴文件，不连接外部业务生产系统。平台不执行实际资金划转或行政审批。

## 等级模块（M8）

### 生效时机策略

退货或到期使净积分跌破门槛时，采用方向感知策略，同时回应门店与客服两侧诉求：

- **升级：当期立即生效**，会员及时获得更高礼遇。
- **降级：下一周期生效**。当期已确认的礼遇不追回，页面以 `pending_tier`/`pending_period` 预告下一周期等级，当期仍显示原等级。
- 净积分取批次可用余额合计（`net_by_member`），与归档余额、总账守恒三处对齐后才允许发布。

### 约束与落点

| 题面约束 | 实现机制 | 位置 | 测试 |
|---|---|---|---|
| 立即重算还是下期生效 | 升级当期、降级下期的方向感知 `decide_change` | `app/modules/level/rules.py` | `test_downgrade_defers_to_next_period`、`test_upgrade_takes_effect_immediately` |
| 页面同时显示等级、净积分、生效版本 | 提案表 + 已发布版本表（tier/net_points/version/effective_period） | `app/templates/level.html` | `test_page_renders_tier_net_and_version` |
| 净积分变化进入对账，对账未过不能发布 | `reconcile_net` 三道闸门 + 发布前 `publishable` 拦截（409 RECONCILE_BLOCKED） | `app/modules/level/rules.py`、`app/store.py` | `test_tampered_net_points_blocks_gate`、`test_publish_blocked_when_reconcile_fails` |
| 同一退货重试不重复改变等级 | `basis_digest`（run 摘要+会员+净积分+候选等级+规则版本）唯一约束，命中即幂等跳过 | `app/store.py::publish_tiers` | `test_repeated_publish_is_idempotent`、`test_downgrade_lifecycle_with_real_refund` |
| 发布失败保留旧版结果 | 单事务写入，冲突整体回滚；先闸门校验后插入 | `app/store.py::publish_tiers` | `test_failed_publish_keeps_old_version` |
| 历史等级覆盖后无法恢复也不能发生 | `tier_versions` 仅追加（INSERT），无 UPDATE/DELETE，版本号每会员单调递增 | `app/store.py` | `test_history_is_append_only_and_never_overwritten` |

等级门槛在 `sample_data/context.json` 的 `tiers` 配置（黑钻500/金卡200/银卡80/普卡0），周期由 `level_period`、`next_period` 给出。

