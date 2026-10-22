# 办公室日历 [演练]

> 这本账只记办公室哪天开了门,不是会议日程。日历默认为空这件事不变。
>
> - 每个模拟日登记一行。开工第 0 步由主会话运行 `python3 tools/office_tool.py gap <date>`,把结果填进「距上次开门」列;间隔大于 1 时,先在「断档记录」追加一行,再登记当天。
> - 工作日 = 周一到周五,不考虑节假日。「距上次开门」= 上次开门日(含)到今天(不含)之间的工作日数。相邻两个工作日开门记 1;大于 1 说明中间有没开门的工作日。
> - 断档是运行环境的事,不是决策欠账。不补跑缺失的日子,不补写那些天的裁决。断档期间到期的复核,原因码写"运行断档"。
> - 「调用岗位」按当天实际产出的备忘录和 `decisions.md` 条目登记。下面前三行是 2026-09-25 按备忘录补登的,子代理当天是否真的被调用,没有核实。

| 模拟日 | 触发 | 距上次开门（工作日） | 调用岗位 | 备注 |
|---|---|---|---|---|
| 2026-08-09 | 手动 | — | chief-of-staff、ops-noninsurance、deal-screener、investment-analyst、vice-chairman-skeptic、chairman | 日志第一天(周日,手动开工);补登 |
| 2026-09-24 | 例行 | 33 | chief-of-staff、ops-insurance、ops-noninsurance、deal-screener、investment-analyst、vice-chairman-skeptic、chairman | 断档后第一天,2026-08-10 至 2026-09-23 共 33 个工作日没开门,见断档记录;补登 |
| 2026-09-25 | 例行 | 1 | chief-of-staff、ops-insurance、deal-screener、investment-analyst、general-counsel、chairman | general-counsel 首次上场;今日无提案进入证伪席;补登 |

## 断档记录

- 2026-08-10 至 2026-09-23：未开门,运行环境故障,不是决策;期间不补跑、不补裁决。
