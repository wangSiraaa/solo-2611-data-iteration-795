# 离线计量稽核示例（合成归档）

三个自包含场景。每个文件都包含案件（period、正偏差功率 d）、拓扑（按时段
分 epoch 的有根馈线树）、初始表计读数、可选修订（`readings_revision_to`）、
核对项（总表 + 时段）以及独立复核档（叶集合 + 正整数成本）。

复跑（精确分数，无浮点）：

```bash
python3 -m power_billing.audit_cli materials/900_audit/scenario.json
python3 -m power_billing.audit_cli materials/900_audit/fractional.json
python3 -m power_billing.audit_cli materials/900_audit/contradiction.json
```

查询入口同上，可加：

* `--json` 打印完整报告（含逐项实测/预计差额、缺证范围、决策树）；
* `--strategy NAME --candidates a,b,c,d --open-files 2,3` 对给定候选与开放档案
  清单临时求最小最坏成本策略。

## scenario.json（一小时四叶示例）

a、b、c、d 一小时内均报 0，根总表 `Mroot` 报 1，`d=1`。

* 根核对 `Xroot`：实测差额 60，任何叶预计差额都是 60 → 四个全匹配。
* 支路核对 `Xab`（总表 `M1`，仅覆盖 a、b）：实测 60；a、b 预计 60，c、d 预计 0
  → 候选缩为 `{a,b}`。
* 三份成本 1 档案 `G={a,b}`(1)、`A={a}`(2)、`C={c}`(3) 全开：最坏成本 2，首次读
  G（编号最小者优先）。
* 关闭 G（仅开 2、3）：b 与 d 对所有档案回答相同 → 列为不可区分同类。
* 取得 `Xab` 后候选 `{a,b}`，只需读 A，最坏成本 1。

## fractional.json（中途移出 + 表计修订）

a 在 `t=25/2` 从支路 `N1` 移到 `N2`，叶身份保持不变。核对 `Xp1`（`N1`，`[0,60)`）：

* 初始读数 r1 中 `N1` 缺 `[0,25/2)`，核对不完整 → 列为缺证，候选仍为四叶。
* 预计差额：a = d·25/2 = 25/2（分数），b = 60，c = d = 0，d = 0。
* 修订 r2 补齐 `N1` 后，`Xp1` 成为约束，实测 25/2 仅匹配 a → 唯一，采用修订。

## contradiction.json（测量矛盾）

`Q2` 读数使实测差额 30，而无任何叶的预计差额等于 30（c、d 预计 60，a、b 预计 0）
→ `contradiction`，与 `missing_evidence` 是不同的表现，并给出无匹配的解释。
