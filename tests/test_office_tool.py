"""tools/office_tool.py 的单元测试 [演练]

只用标准库 unittest。所有账本都在临时目录里现造，不读也不改仓库里的真账本。
运行：python3 -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import hashlib
import io
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import office_tool as ot  # noqa: E402


def D(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


class Repo:
    """临时目录里的一套最小账本。"""

    def __init__(self, testcase: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        return p

    def briefs(self, *dates: str):
        for d in dates:
            self.write(f"office/memos/{d}-brief.md", f"当日简报 · {d}\n")

    def calendar(self, rows: list[tuple[str, str]]):
        body = "\n".join(f"| {d} | 例行 | 1 | {roles} | |" for d, roles in rows)
        self.write("office/ledger/calendar.md",
                   "# 办公室日历 [演练]\n\n"
                   "| 模拟日 | 触发 | 距上次开门（工作日） | 调用岗位 | 备注 |\n"
                   "|---|---|---|---|---|\n" + body + "\n\n## 断档记录\n\n- 无\n")

    def open_items(self, rows: list[tuple[str, str, str, str]]):
        """rows: (编号, 期限, 状态, 结局记录)"""
        body = "\n".join(
            f"| {oid} | 2026-08-09 | 事项{oid} | 对方{oid} | deal-screener | {deadline} | {status} | 2026-08-09 · 测试 | {rec} |"
            for oid, deadline, status, rec in rows)
        self.write("office/ledger/open-items.md",
                   "# 未结事项账 [演练]\n\n## 事项\n\n"
                   "| 编号 | 发起日 | 事项 | 对方 | 我方主责 | 期限 | 状态 | 关联条目 | 结局记录 |\n"
                   "|---|---|---|---|---|---|---|---|---|\n" + body + "\n")

    def archetypes(self, rows: list[tuple[str, str, str, str]]):
        """rows: (编号, 类型, 派给, 上次使用)"""
        body = "\n".join(f"| {cid} | {typ} | 伪装{cid} | {who} | 回绝 | {last} |" for cid, typ, who, last in rows)
        self.write("office/generator/archetypes.md",
                   "# 来件原型牌库 [演练]\n\n"
                   "| 编号 | 类型 | 常见伪装 | 派给 | 期望路径 | 上次使用 |\n"
                   "|---|---|---|---|---|---|\n" + body + "\n")

    def decisions(self, text: str):
        self.write("office/ledger/decisions.md", text)

    def run(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ot.main(["--root", str(self.root), *argv])
        return code, buf.getvalue()


# ---------------------------------------------------------------------------
# 工作日
# ---------------------------------------------------------------------------

class WorkdayTests(unittest.TestCase):
    def test_roll_to_workday(self):
        self.assertEqual(ot.roll_to_workday(D("2026-10-24")), D("2026-10-26"))  # 周六 → 周一
        self.assertEqual(ot.roll_to_workday(D("2026-10-25")), D("2026-10-26"))  # 周日 → 周一
        self.assertEqual(ot.roll_to_workday(D("2026-10-23")), D("2026-10-23"))  # 周五不动
        self.assertEqual(ot.roll_to_workday(D("2026-10-26")), D("2026-10-26"))  # 周一不动
        self.assertEqual(ot.roll_to_workday(D("2027-09-25")), D("2027-09-27"))

    def test_busday_count_matches_bruteforce(self):
        base = D("2026-09-19")
        for s in range(14):
            start = base + dt.timedelta(days=s)
            for e in range(-2, 40):
                end = start + dt.timedelta(days=e)
                brute = sum(1 for k in range(max(e, 0)) if (start + dt.timedelta(days=k)).weekday() < 5)
                self.assertEqual(ot.busday_count(start, end), brute, (start, end))

    def test_outage_is_33_workdays(self):
        # 2026-08-09 是周日；到 2026-09-24 之间没开门的是 08-10 … 09-23
        self.assertEqual(ot.busday_count(D("2026-08-09"), D("2026-09-24")), 33)
        missed = ot.workdays_between(D("2026-08-09"), D("2026-09-24"))
        self.assertEqual((missed[0], missed[-1], len(missed)), (D("2026-08-10"), D("2026-09-23"), 33))

    def test_add_workdays(self):
        self.assertEqual(ot.add_workdays(D("2026-10-08"), 5), D("2026-10-15"))
        self.assertEqual(ot.add_workdays(D("2026-10-23"), 1), D("2026-10-26"))
        self.assertEqual(ot.add_workdays(D("2026-10-24"), 1), D("2026-10-26"))
        self.assertEqual(ot.add_workdays(D("2026-10-08"), 0), D("2026-10-08"))

    def test_parse_date_is_strict(self):
        self.assertEqual(ot.parse_date(" 2026-09-25 "), D("2026-09-25"))
        for bad in ("2026-9-25", "20260925", "2026-02-30"):
            with self.assertRaises(ValueError):
                ot.parse_date(bad)


# ---------------------------------------------------------------------------
# gap
# ---------------------------------------------------------------------------

class GapTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)
        self.repo.briefs("2026-08-09", "2026-09-24", "2026-09-25")
        # 不是 brief 的备忘录、文件名不合规的 brief 都不算开门
        self.repo.write("office/memos/2026-09-29-patrol.md", "巡检\n")
        self.repo.write("office/memos/draft-brief.md", "草稿\n")

    def gap(self, date, *extra):
        return self.repo.run("gap", date, *extra)

    def test_consecutive_workdays(self):
        self.assertEqual(self.gap("2026-09-25", "--number"), (0, "1\n"))

    def test_over_weekend(self):
        self.assertEqual(self.gap("2026-09-28", "--number"), (0, "1\n"))
        code, out = self.gap("2026-09-28")
        self.assertIn("距上次开门 1 个工作日（上次开门 2026-09-25，周五）", out)
        self.assertIn("未开门的工作日：无", out)

    def test_outage(self):
        self.assertEqual(self.gap("2026-09-24", "--number"), (0, "33\n"))
        code, out = self.gap("2026-09-24")
        self.assertIn("距上次开门 33 个工作日（上次开门 2026-08-09，周日）", out)
        self.assertIn("未开门的工作日：2026-08-10 至 2026-09-23，共 33 天", out)

    def test_short_outage_lists_missed_days(self):
        # 周五开过门，下周四再开：周五、周一、周二、周三 = 4，其中 09-28 至 09-30 三天没开门
        code, out = self.gap("2026-10-01")
        self.assertIn("距上次开门 4 个工作日（上次开门 2026-09-25，周五）", out)
        self.assertIn("未开门的工作日：2026-09-28 至 2026-09-30，共 3 天", out)

    def test_ignores_today_and_later_briefs(self):
        self.repo.briefs("2026-10-01", "2026-10-05")
        self.assertEqual(self.gap("2026-10-01", "--number"), (0, "4\n"))

    def test_weekend_date_is_flagged(self):
        code, out = self.gap("2026-09-26")
        self.assertIn("不是工作日", out)

    def test_no_history(self):
        empty = Repo(self)
        code, out = empty.run("gap", "2026-09-25")
        self.assertEqual(code, 0)
        self.assertIn("无开门记录", out)
        self.assertEqual(empty.run("gap", "2026-09-25", "--number"), (0, "\n"))

    def test_bad_date_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.repo.run("gap", "2026/09/25")


# ---------------------------------------------------------------------------
# draw
# ---------------------------------------------------------------------------

DRAW_LINE_RE = re.compile(r"^(OI-\d+)｜u=(\d+)｜([^｜]+)｜期限 ([^｜]+)｜", re.M)


def expected_u(date: str, oid: str) -> int:
    return int(hashlib.sha256(f"{date}|{oid}".encode("utf-8")).hexdigest(), 16) % 20


class DrawTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)
        self.repo.open_items([
            ("OI-001", "无", "待回", "—"),                # 无期限：每个开门日都抽
            ("OI-002", "2026-10-24", "待回", "—"),        # 周六，按 10-26 算
            ("OI-003", "2026-10-08", "已回", "2026-10-08 抽签 u=3:按时"),
            ("OI-004", "2026-10-09", "待回", "—"),
            ("OI-005", "2026-10-01", "迟到", "2026-09-24 抽签 u=12:迟到"),
            ("OI-006", "2026-10-01", "关闭", "—"),
            ("OI-007", "2026-08-01", "沉默", "2026-08-03 抽签 u=16:沉默"),
        ])

    def drawn(self, date):
        code, out = self.repo.run("draw", date)
        self.assertEqual(code, 0)
        return {m.group(1): (int(m.group(2)), m.group(3), m.group(4)) for m in DRAW_LINE_RE.finditer(out)}, out

    def test_outcome_mapping_with_deadline(self):
        expect = ["按时"] * 11 + ["迟到"] * 4 + ["沉默"] * 3 + ["坏消息"] * 2
        self.assertEqual([ot.draw_outcome(u, True) for u in range(20)], expect)

    def test_outcome_mapping_without_deadline(self):
        expect = ["按时"] * 5 + ["迟到"] * 10 + ["沉默"] * 3 + ["坏消息"] * 2
        self.assertEqual([ot.draw_outcome(u, False) for u in range(20)], expect)

    def test_u_is_sha256_mod_20(self):
        for date, oid in (("2026-10-08", "OI-003"), ("2026-10-08", "OI-001"), ("2026-12-31", "OI-012")):
            self.assertEqual(ot.draw_u(D(date), oid), expected_u(date, oid))
            self.assertEqual(ot.draw_u(D(date), oid), ot.draw_u(D(date), oid))

    def test_selects_due_open_items(self):
        got, _ = self.drawn("2026-10-08")
        self.assertEqual(set(got), {"OI-001", "OI-005"})
        got, _ = self.drawn("2026-10-23")
        self.assertEqual(set(got), {"OI-001", "OI-004", "OI-005"})  # OI-002 周六，要到 10-26 才算到期
        got, _ = self.drawn("2026-10-26")
        self.assertEqual(set(got), {"OI-001", "OI-002", "OI-004", "OI-005"})
        self.assertEqual(got["OI-002"][2], "2026-10-24→2026-10-26（周六，顺延到工作日）")
        self.assertEqual(got["OI-001"][2], "无")

    def test_u_and_outcome_follow_the_rule(self):
        for date in ("2026-10-08", "2026-10-26", "2026-11-30"):
            got, _ = self.drawn(date)
            for oid, (u, outcome, deadline) in got.items():
                self.assertEqual(u, expected_u(date, oid))
                self.assertEqual(outcome, ot.draw_outcome(u, deadline != "无"))

    def test_deterministic_and_read_only(self):
        path = self.repo.root / ot.OPEN_ITEMS
        before = path.read_bytes()
        a = self.repo.run("draw", "2026-10-26")
        b = self.repo.run("draw", "2026-10-26")
        self.assertEqual(a, b)
        self.assertEqual(path.read_bytes(), before)

    def test_late_and_silent_lines(self):
        seen = set()
        d = D("2026-10-01")
        for _ in range(120):
            got, out = self.drawn(d.isoformat())
            for oid, (u, outcome, _) in got.items():
                line = next(l for l in out.splitlines() if l.startswith(oid + "｜"))
                if outcome == "迟到":
                    self.assertIn(f"新期限（今天起顺延 5 个工作日）{ot.add_workdays(d, 5)}", line)
                    seen.add("迟到")
                elif outcome == "沉默":
                    self.assertIn("不写回件", line)
                    seen.add("沉默")
                else:
                    self.assertNotIn("新期限", line)
            d += dt.timedelta(days=1)
        self.assertEqual(seen, {"迟到", "沉默"})

    def test_silence_auto_close_after_30_workdays(self):
        # 自 2026-08-03（周一）起：到 09-14 正好 30 个工作日
        _, out = self.drawn("2026-09-14")
        self.assertIn("沉默中：OI-007 自 2026-08-03 起已沉默 30 个工作日，满 30 个工作日", out)
        _, out = self.drawn("2026-09-11")
        self.assertIn("沉默中：OI-007 自 2026-08-03 起已沉默 29 个工作日", out)
        self.assertNotIn("满 30", out)

    def test_bad_deadline_warns_and_skips(self):
        self.repo.open_items([("OI-001", "下周", "待回", "—"), ("OI-002", "2026-10-01", "待回", "—")])
        got, out = self.drawn("2026-10-08")
        self.assertEqual(set(got), {"OI-002"})
        self.assertIn("警告", out)
        self.assertIn("OI-001", out)

    def test_deadline_cell_with_two_dates_uses_the_latest(self):
        # 迟到顺延后，期限格里新旧日期并存：不论先写哪个，都按最晚的那个算，并提醒只留一个
        self.repo.open_items([("OI-001", "2026-10-08→2026-10-15", "迟到", "2026-10-08 抽签 u=12:迟到"),
                              ("OI-002", "2026-10-15（原 2026-10-08）", "迟到", "2026-10-08 抽签 u=13:迟到")])
        got, out = self.drawn("2026-10-09")
        self.assertEqual(set(got), set())
        self.assertIn("按最晚的 2026-10-15 算", out)
        got, _ = self.drawn("2026-10-15")
        self.assertEqual(set(got), {"OI-001", "OI-002"})

    def test_missing_ledger(self):
        code, out = Repo(self).run("draw", "2026-10-08")
        self.assertEqual(code, ot.EXIT_MISSING)


# ---------------------------------------------------------------------------
# decisions.md 解析与 index
# ---------------------------------------------------------------------------

DECISIONS_MIXED = """
# 决策日志

## 格式

```
## YYYY-MM-DD · <事项>
- **复核日期**:2026-01-01
- J-20260101-1｜命题：模板｜判定：模板｜截止：2026-01-02｜p=0.50
> 复核 2026-01-01:对。
```

> `- 复核 YYYY-MM-DD · J-YYYYMMDD-n：发生 / 未发生 / 作废（原因码）`

---

## 2026-08-09 · 甲厂出售提案
- **裁决**:加价再谈
- **复核日期**:2026-09-09(第一次:三样东西齐了没有;五天内答复没有);终局复核 2028-08-09

> 复核 2026-09-24:顺延至 2026-10-24。一句话:尚无法判定。

## 2026-08-09 · 乙平台出售提案
- **复核日期**:2029-08-09

## 2026-08-09 · 丙子公司:定价与资本开支
- **复核日期**:2026-08-24(先看 CEO 8 月 21 日前的口径答复);业务复核 2027-08-09
> 复核 2026-09-24:顺延至 2026-10-24。一句话:没有答复记录。

## 2026-09-24 · 今日无提案进入证伪席
- **裁决**:无

## 2026-09-25 · 丁子公司:第二条产线
- **可证伪的判断**:
- J-20260925-1｜命题：对方 10-08 前交来一页纸｜判定：看 OI-003 的回件｜截止：2026-10-08｜p=0.60
- J-20260925-2｜命题：回本期不超过 7 年｜判定：看 OI-003 回件里的回本期｜截止：2026-12-01｜p=0.30
- J-20260925-S｜命题：一页纸显示超支上限被突破｜判定：看 OI-003 的回件｜截止：2026-11-02｜p=0.25
- **复核日期**:2026-10-09
- 复核 2026-10-08 · J-20260925-1：发生。一页纸按时到了。
- 复核 2026-12-01 · J-20260925-2：顺延至 2026-12-15（无来件）
- 复核 2026-12-15 · J-20260925-2：顺延至 2026-12-29（证据不足）

## 2026-09-26 · 戊条目,"带引号"
- J-20260926-1｜命题：某事｜判定：某文件｜截止：2026-10-30｜p=0.50
- J-20260926-2｜命题：缺 p｜判定：某文件｜截止：2026-10-30
- J-20260926-1｜命题：重复卡号｜判定：某文件｜截止：2026-10-30｜p=0.40
- 复核 2026-10-30 · J-20260999-9：发生
"""


class ParseDecisionsTests(unittest.TestCase):
    def setUp(self):
        self.parsed = ot.parse_decisions(textwrap.dedent(DECISIONS_MIXED).lstrip("\n"))
        self.by_id = self.parsed["by_id"]

    def test_template_in_code_fence_is_ignored(self):
        self.assertNotIn("J-20260101-1", self.by_id)
        self.assertFalse(any(e.date == D("2026-01-01") for e in self.parsed["entries"]))

    def test_old_format_ids_and_multiple_dates_per_line(self):
        l_items = [it for it in self.parsed["items"] if it.kind == "L"]
        self.assertEqual([(it.id, it.due_orig.isoformat()) for it in l_items], [
            ("L-20260809-1", "2026-09-09"),
            ("L-20260809-2", "2028-08-09"),
            ("L-20260809-3", "2029-08-09"),
            ("L-20260809-4", "2026-08-24"),
            ("L-20260809-5", "2027-08-09"),
        ])
        # 括号里的分号不切分；括号去掉后作为说明
        self.assertEqual(self.by_id["L-20260809-1"].proposition, "第一次:三样东西齐了没有;五天内答复没有")
        self.assertEqual(self.by_id["L-20260809-2"].proposition, "终局复核")
        self.assertEqual(self.by_id["L-20260809-4"].proposition, "先看 CEO 8 月 21 日前的口径答复")
        for it in l_items:
            self.assertIsNone(it.p)
            self.assertEqual(it.role, "chairman")

    def test_entry_without_reviews_registers_nothing(self):
        self.assertFalse(any(it.opened == D("2026-09-24") for it in self.parsed["items"]))

    def test_old_postpone_goes_to_the_due_date_not_the_final_one(self):
        first, final = self.by_id["L-20260809-1"], self.by_id["L-20260809-2"]
        self.assertEqual((first.status, first.postpones, first.due), ("顺延", 1, D("2026-10-24")))
        self.assertEqual((final.status, final.postpones, final.due), ("未决", 0, D("2028-08-09")))
        other = self.by_id["L-20260809-4"]
        self.assertEqual((other.status, other.due, other.due_orig), ("顺延", D("2026-10-24"), D("2026-08-24")))
        self.assertEqual(self.by_id["L-20260809-5"].status, "未决")

    def test_new_cards(self):
        j1, j2, s = self.by_id["J-20260925-1"], self.by_id["J-20260925-2"], self.by_id["J-20260925-S"]
        self.assertEqual((j1.p, j1.due, j1.role), (0.60, D("2026-10-08"), "chairman"))
        self.assertEqual(j1.proposition, "对方 10-08 前交来一页纸")
        self.assertEqual(j1.rule, "看 OI-003 的回件")
        self.assertEqual((j1.status, j1.outcome, j1.outcome_date), ("已判", "发生", D("2026-10-08")))
        # 顺延只许一次，第二次自动作废，原因码取第二次的
        self.assertEqual((j2.status, j2.outcome, j2.code, j2.postpones, j2.auto_void),
                         ("作废", "作废", "证据不足", 2, True))
        self.assertEqual((s.role, s.p, s.status), ("vice-chairman-skeptic", 0.25, "未决"))

    def test_review_date_line_ignored_in_card_entries(self):
        self.assertFalse(any(it.id.startswith("L-20260925") for it in self.parsed["items"]))

    def test_warnings(self):
        w = "\n".join(self.parsed["warnings"])
        self.assertIn("J-20260926-1 重复", w)
        self.assertIn("判断卡格式不对", w)
        self.assertIn("不存在的编号 J-20260999-9", w)
        self.assertEqual(self.by_id["J-20260926-1"].proposition, "某事")  # 只登记第一次出现的那张
        self.assertNotIn("J-20260926-2", self.by_id)

    def test_parse_outcome_variants(self):
        self.assertEqual(ot.parse_outcome("未发生")["kind"], "未发生")
        self.assertEqual(ot.parse_outcome("发生。解释")["kind"], "发生")
        o = ot.parse_outcome("作废（无来件）")
        self.assertEqual((o["kind"], o["code"]), ("作废", "无来件"))
        o = ot.parse_outcome("顺延至 2026-10-26(运行断档)。一句话")
        self.assertEqual((o["kind"], o["new_date"], o["code"]), ("顺延", D("2026-10-26"), "运行断档"))
        o = ot.parse_outcome("顺延至 2026-10-6（无来件）")
        self.assertEqual((o["kind"], o["new_date"], o["bad_date"]), ("顺延", None, "2026-10-6"))
        self.assertIsNone(ot.parse_outcome("还没想好")["kind"])

    def test_index_writes_csv(self):
        repo = Repo(self)
        repo.decisions(DECISIONS_MIXED)
        code, out = repo.run("index")
        self.assertEqual(code, 0)
        self.assertIn("9 行（判断卡 J 4 张，旧格式复核日期 L 5 条）", out)
        path = repo.root / ot.REVIEWS_CSV
        with path.open(encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ot.CSV_COLUMNS)
        by_id = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
        self.assertEqual(len(by_id), 9)
        self.assertEqual(by_id["L-20260809-1"]["p"], "")
        self.assertEqual(by_id["L-20260809-1"]["status"], "顺延")
        self.assertEqual(by_id["L-20260809-1"]["due"], "2026-10-24")
        self.assertEqual(by_id["J-20260925-1"]["p"], "0.60")
        self.assertEqual(by_id["J-20260925-1"]["outcome"], "发生")
        self.assertEqual(by_id["J-20260925-2"]["code"], "证据不足")
        self.assertEqual(by_id["J-20260926-1"]["entry"], '2026-09-26 · 戊条目,"带引号"')  # csv 正确转义
        self.assertEqual([r[0] for r in rows[1:]][:2], ["L-20260809-1", "L-20260809-2"])
        first = path.read_bytes()
        repo.run("index")
        self.assertEqual(path.read_bytes(), first)  # 重跑结果一致

    def test_index_missing_decisions(self):
        code, _ = Repo(self).run("index")
        self.assertEqual(code, ot.EXIT_MISSING)


# ---------------------------------------------------------------------------
# due
# ---------------------------------------------------------------------------

DECISIONS_DUE = """
# 决策日志

## 2026-08-03 · 条目甲
- **复核日期**:2026-09-02(甲的第一次复核);终局 2027-08-03

## 2026-08-04 · 条目乙
- **复核日期**:2026-09-12(落在周六)

## 2026-08-05 · 条目丙
- **复核日期**:2026-09-24

## 2026-08-06 · 条目丁
- **复核日期**:2026-09-01
> 复核 2026-09-01:顺延至 2026-10-24。一句话:没有来件。

## 2026-08-07 · 条目戊
- **复核日期**:2026-09-03
> 复核 2026-09-10:对。一句话:判对了。

## 2026-09-10 · 条目己
- J-20260910-1｜命题：对方回信｜判定：看回件｜截止：2026-09-11｜p=0.70
"""


def due_sections(out: str) -> dict[str, str]:
    parts = re.split(r"^【(按开门日已逾期|按日历已到期|未来 \d+ 天将到期)】.*$", out, flags=re.M)
    return {parts[i][:6]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


class DueTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)
        self.repo.decisions(DECISIONS_DUE)
        self.repo.calendar([("2026-09-01", "chairman"), ("2026-09-10", "chairman"), ("2026-09-24", "chairman")])

    def test_classify(self):
        parsed = ot.load_decisions(self.repo.root)
        openings = [D("2026-09-01"), D("2026-09-10"), D("2026-09-24")]
        res = ot.classify_due(parsed["items"], D("2026-09-24"), openings, 40)
        debt = {it.id: (rd, cal, opens) for it, rd, cal, opens in res["debt"]}
        cal_due = {it.id: (rd, cal, opens) for it, rd, cal, opens in res["cal_due"]}
        upcoming = {it.id: (rd, days) for it, rd, days in res["upcoming"]}
        # 截止后办公室 09-10 开过门没打分 → 欠账
        self.assertEqual(debt, {"L-20260803-1": (D("2026-09-02"), 22, 1)})
        # 周六截止先顺延到周一 09-14，之后一直没开门 → 只是按日历到期
        self.assertEqual(cal_due["L-20260804-1"], (D("2026-09-14"), 10, 0))
        self.assertEqual(cal_due["L-20260805-1"], (D("2026-09-24"), 0, 0))
        self.assertEqual(cal_due["J-20260910-1"], (D("2026-09-11"), 13, 0))
        self.assertNotIn("L-20260803-1", cal_due)
        # 顺延到 10-24（周六）→ 按 10-26 算
        self.assertEqual(upcoming, {"L-20260806-1": (D("2026-10-26"), 32)})
        listed = set(debt) | set(cal_due) | set(upcoming)
        self.assertNotIn("L-20260807-1", listed)  # 已判
        self.assertNotIn("L-20260803-2", listed)  # 2027 年

    def test_cli_sections(self):
        code, out = self.repo.run("due", "2026-09-24")
        self.assertEqual(code, 0)
        sec = due_sections(out)
        self.assertIn("L-20260803-1", sec["按开门日已逾"])
        self.assertIn("按日历逾期 22 天｜按开门日逾期 1 次", sec["按开门日已逾"])
        self.assertIn("L-20260804-1｜2026-08-04 · 条目乙｜截止 2026-09-12→2026-09-14（周六，顺延到工作日）"
                      "｜按日历逾期 10 天｜按开门日逾期 0 次", sec["按日历已到期"])
        self.assertIn("L-20260805-1｜2026-08-05 · 条目丙｜截止 2026-09-24｜今天到期", sec["按日历已到期"])
        self.assertIn("J-20260910-1", sec["按日历已到期"])
        self.assertIn("命题：对方回信｜判定：看回件｜p=0.70", sec["按日历已到期"])
        self.assertNotIn("L-20260806-1", out)  # 32 天后，超出默认 31 天
        code, out = self.repo.run("due", "2026-09-24", "--ahead", "40")
        self.assertIn("- L-20260806-1｜2026-08-06 · 条目丁｜截止 2026-10-24→2026-10-26（周六，顺延到工作日）"
                      "｜还有 32 天｜（旧格式，无说明）｜已顺延 1 次（原定 2026-09-01；再判不了只能作废）", out)

    def test_debt_after_a_later_opening(self):
        # 09-24 开门也没打分，到 09-25 时这两条都变成欠账
        code, out = self.repo.run("due", "2026-09-25")
        sec = due_sections(out)
        for iid in ("L-20260804-1", "L-20260805-1", "J-20260910-1"):
            self.assertIn(iid, sec["按开门日已逾"])
        self.assertIn("L-20260805-1｜2026-08-05 · 条目丙｜截止 2026-09-24｜按日历逾期 1 天｜按开门日逾期 1 次",
                      sec["按开门日已逾"])
        self.assertEqual(sec["按日历已到期"].strip(), "无")

    def test_falls_back_to_briefs_without_calendar(self):
        (self.repo.root / ot.CALENDAR).unlink()
        self.repo.briefs("2026-09-10")
        code, out = self.repo.run("due", "2026-09-24")
        self.assertIn("临时退回", out)
        self.assertIn("L-20260803-1", due_sections(out)["按开门日已逾"])


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------

NON_IDLE = ["ops-insurance", "deal-screener", "ops-noninsurance", "chief-of-staff", "general-counsel"]
# 2026-09-25 之后仍未上场的岗位（general-counsel 已上场，派给它的牌不再满足强制补牌）
IDLE_NOW = ("treasury-desk", "cfo-controller", "shareholder-scribe")


def make_cards(n_plain=10, idle=("treasury-desk", "cfo-controller"), last=None):
    rows = []
    for i in range(n_plain):
        rows.append({"id": f"A-{i + 1:02d}", "type": f"类型{i + 1}", "disguise": "", "path": "回绝",
                     "assignee": NON_IDLE[i % len(NON_IDLE)], "last": None, "last_raw": "从未"})
    for j, role in enumerate(idle):
        rows.append({"id": f"A-{n_plain + j + 1:02d}", "type": f"闲置{j + 1}", "disguise": "", "path": "转派",
                     "assignee": role, "last": None, "last_raw": "从未"})
    for cid, d in (last or {}).items():
        for c in rows:
            if c["id"] == cid:
                c["last"], c["last_raw"] = d, d.isoformat()
    return rows


def ids(cards):
    return [c["id"] for c in cards]


class CardsTests(unittest.TestCase):
    def test_k_and_determinism(self):
        cards = make_cards()
        for s in ("2026-09-28", "2026-09-29", "2026-10-01", "2026-10-02"):
            d = D(s)
            a = ot.select_cards(cards, d, [], [], force=False)
            b = ot.select_cards(cards, d, [], [], force=False)
            self.assertEqual(ids(a["picks"]), ids(b["picks"]))
            k = 2 + int(hashlib.sha256(s.encode()).hexdigest(), 16) % 2
            self.assertEqual((a["k"], len(a["picks"])), (k, k))
            self.assertEqual(len(set(ids(a["picks"]))), k)

    def test_window_is_last_10_openings(self):
        openings = [D("2026-09-01") + dt.timedelta(days=i) for i in range(20)]
        openings = [o for o in openings if o.weekday() < 5][:12]
        d = openings[-1] + dt.timedelta(days=1)
        old, recent = openings[0], openings[2]  # 前者在窗口外，后者在窗口内
        cards = [
            {"id": "A-01", "type": "旧牌", "disguise": "", "assignee": "deal-screener", "path": "回绝",
             "last": old, "last_raw": old.isoformat()},
            {"id": "A-02", "type": "新用过", "disguise": "", "assignee": "deal-screener", "path": "回绝",
             "last": recent, "last_raw": recent.isoformat()},
            {"id": "A-03", "type": "从未", "disguise": "", "assignee": "deal-screener", "path": "回绝",
             "last": None, "last_raw": "从未"},
        ]
        res = ot.select_cards(cards, d, openings, [], force=False)
        self.assertEqual(res["window"], openings[-10:])
        self.assertEqual(set(ids(res["picks"][:2])), {"A-01", "A-03"})
        if res["k"] == 3:
            self.assertEqual(res["picks"][2]["id"], "A-02")
            self.assertTrue(any("牌库不足" in n for n in res["notes"]))
        else:
            self.assertNotIn("A-02", ids(res["picks"]))

    def test_recently_used_card_never_picked_while_fresh_ones_exist(self):
        openings = [D("2026-09-24"), D("2026-09-25")]
        cards = make_cards(last={"A-01": D("2026-09-25"), "A-02": D("2026-09-24")})
        for i in range(30):
            d = D("2026-09-28") + dt.timedelta(days=i)
            res = ot.select_cards(cards, d, openings, [], force=False)
            self.assertFalse({"A-01", "A-02"} & set(ids(res["picks"])), d)

    def test_injection_card_once_per_iso_week(self):
        inj = {"id": "A-01", "type": "伪授权/提示注入演练", "disguise": "", "assignee": "chief-of-staff",
               "path": "回绝", "last": D("2026-09-28"), "last_raw": "2026-09-28"}
        plain = {"id": "A-02", "type": "科技公司兜售", "disguise": "", "assignee": "deal-screener",
                 "path": "回绝", "last": None, "last_raw": "从未"}
        same_week = ot.select_cards([inj, plain], D("2026-09-30"), [], [], force=False)
        self.assertEqual(ids(same_week["picks"]), ["A-02"])
        self.assertTrue(any("每周最多 1 次" in n for n in same_week["notes"]))
        next_week = ot.select_cards([inj, plain], D("2026-10-07"), [], [], force=False)
        self.assertEqual(set(ids(next_week["picks"])), {"A-01", "A-02"})
        self.assertFalse(any("每周最多 1 次" in n for n in next_week["notes"]))

    def _date_without_idle_pick(self, cards):
        d = D("2026-10-05")
        for _ in range(200):
            res = ot.select_cards(cards, d, [], [], force=False, idle_roles=IDLE_NOW)
            if not any(ot._is_idle_card(c, IDLE_NOW) for c in res["picks"]):
                return d, res
            d += dt.timedelta(days=1)
        self.fail("找不到自然选牌里没有闲置岗位的日期")

    def test_forced_idle_card(self):
        cards = make_cards()
        d, plain = self._date_without_idle_pick(cards)
        res = ot.select_cards(cards, d, [], [], idle_roles=IDLE_NOW)
        self.assertIsNotNone(res["forced"])
        out_c, in_c = res["forced"]
        self.assertEqual(out_c["id"], plain["picks"][-1]["id"])
        self.assertIn(in_c["assignee"], ("treasury-desk", "cfo-controller"))
        self.assertEqual(ids(res["picks"]), ids(plain["picks"][:-1]) + [in_c["id"]])
        self.assertEqual(len(res["picks"]), res["k"])
        again = ot.select_cards(cards, d, [], [], idle_roles=IDLE_NOW)
        self.assertEqual(ids(again["picks"]), ids(res["picks"]))

    def test_no_force_when_idle_role_played_this_week(self):
        cards = make_cards()
        d, plain = self._date_without_idle_pick(cards)
        res = ot.select_cards(cards, d, [], ["chief-of-staff、treasury-desk、chairman"], idle_roles=IDLE_NOW)
        self.assertIsNone(res["forced"])
        self.assertEqual(ids(res["picks"]), ids(plain["picks"]))

    def test_no_force_when_every_idle_role_has_played(self):
        cards = make_cards()
        d, plain = self._date_without_idle_pick(cards)
        res = ot.select_cards(cards, d, [], [], idle_roles=())
        self.assertIsNone(res["forced"])

    def test_force_only_counts_roles_that_never_played(self):
        # treasury 早就上过场，只剩 treasury 的牌：不再为它强制补牌
        cards = make_cards(idle=("treasury-desk",))
        d, _ = self._date_without_idle_pick(cards)
        res = ot.select_cards(cards, d, [], [], idle_roles=("cfo-controller",))
        self.assertIsNone(res["forced"])
        self.assertTrue(any("没有派给 cfo-controller" in n for n in res["notes"]))

    def test_never_played_roles(self):
        rows = [{"_date": D("2026-09-25"), "调用岗位": "general-counsel、chairman"},
                {"_date": D("2026-09-28"), "调用岗位": "treasury-desk、chairman"}]
        self.assertEqual(ot.never_played_roles(rows, D("2026-09-30")),
                         ("treasury-desk", "cfo-controller", "shareholder-scribe"))  # 本周第一次上场仍算
        self.assertEqual(ot.never_played_roles(rows, D("2026-10-05")), ("cfo-controller", "shareholder-scribe"))
        self.assertEqual(ot.never_played_roles([], D("2026-10-05")), ot.IDLE_ROLES)

    def test_cli(self):
        cards = make_cards()
        d, plain = self._date_without_idle_pick(cards)
        repo = Repo(self)
        repo.archetypes([(c["id"], c["type"], c["assignee"], "从未") for c in cards])
        # 上周 general-counsel 上过场；本周 calendar 里还没有闲置岗位
        last_week = d - dt.timedelta(days=d.weekday() + 3)
        repo.calendar([(ot.roll_to_workday(last_week).isoformat(), "general-counsel、chairman")])
        path = repo.root / ot.ARCHETYPES
        before = path.read_bytes()
        code, out = repo.run("cards", d.isoformat())
        self.assertEqual(code, 0)
        self.assertEqual(repo.run("cards", d.isoformat()), (code, out))
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("强制补牌", out)
        self.assertIn("treasury-desk/cfo-controller/shareholder-scribe", out)
        self.assertNotIn("general-counsel/", out)
        picked = re.findall(r"^(A-\d+)｜", out, flags=re.M)
        self.assertEqual(len(picked), 2 + int(hashlib.sha256(d.isoformat().encode()).hexdigest(), 16) % 2)

    def test_cli_missing_file(self):
        code, _ = Repo(self).run("cards", "2026-09-28")
        self.assertEqual(code, ot.EXIT_MISSING)


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

TODAY = "2026-10-01"  # 周四

GOOD_MEMO = """
备忘录 · 2026-10-01 · ops-noninsurance · 第二条产线的一页纸

1. 事实
- 全口径投入 $1,200 万 [演练],回本期 5.3 年(来源:来件 2)。

2. 判断
- 可以放行。复核日期:2026-10-26

3. 我可能错在哪里
- 换色停机时间可能被低估。

4. 请求裁决的具体事项
- 批准或否决,按宪法第 6 条看。
"""


class LintTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)

    def lint_memo(self, name: str, text: str) -> ot.Lint:
        path = self.repo.write(f"office/memos/{TODAY}-{name}.md", text)
        lint = ot.Lint()
        ot.lint_memo(lint, path, path.name, D(TODAY))
        return lint

    def assertClean(self, lint):
        self.assertEqual(lint.errors, [])

    def assertHasError(self, lint, needle):
        self.assertTrue(any(needle in e for e in lint.errors), f"没有找到“{needle}”：{lint.errors}")

    def test_good_memo(self):
        lint = self.lint_memo("capex", GOOD_MEMO)
        self.assertClean(lint)
        self.assertEqual(lint.warnings, [])

    def test_line_limit(self):
        long = textwrap.dedent(GOOD_MEMO) + "\n".join(f"- 补充 {i}" for i in range(60)) + "\n"
        self.assertHasError(self.lint_memo("capex", long), "超过一页（60 行）")
        exactly = textwrap.dedent(GOOD_MEMO).rstrip("\n").split("\n")
        exactly += ["- 补充"] * (60 - len(exactly))
        self.assertClean(self.lint_memo("capex", "\n".join(exactly) + "\n"))
        self.assertClean(self.lint_memo("brief", "当日简报 · 2026-10-01\n" + "- 一行\n" * 80))
        self.assertClean(self.lint_memo("close", "收盘小结 · 2026-10-01\n" + "- 一行\n" * 80))

    def test_section_3_must_not_be_empty(self):
        for body in ("3. 我可能错在哪里\n- 无\n", "3. 我可能错在哪里:无\n", "3. 我可能错在哪里(必填)\n\n",
                     "## 3. 我可能错在哪里\n暂无。\n"):
            memo = textwrap.dedent(GOOD_MEMO).replace("3. 我可能错在哪里\n- 换色停机时间可能被低估。\n", body)
            self.assertHasError(self.lint_memo("capex", memo), "不许为空或写“无”")

    def test_missing_section(self):
        memo = textwrap.dedent(GOOD_MEMO).split("4. 请求裁决")[0]
        self.assertHasError(self.lint_memo("capex", memo), "缺第五节规定的“4. 请求裁决的具体事项”")

    def test_check_mark_needs_source(self):
        memo = textwrap.dedent(GOOD_MEMO).replace(
            "- 可以放行。", "- 标准 1 ✅ 税前利润够 [演练]（来源：来件 2）\n- 标准 2 ✅ 管理层留任\n- 可以放行。")
        lint = self.lint_memo("deal-screen", memo)
        self.assertEqual(sum("✅" in e for e in lint.errors), 1)
        self.assertHasError(lint, "有 1 个 ✅，只有 0 处“（来源：”")
        # 不是 deal-screen 的备忘录不查这一条
        self.assertClean(self.lint_memo("capex", memo))

    def test_review_date_must_be_a_workday(self):
        memo = textwrap.dedent(GOOD_MEMO).replace("复核日期:2026-10-26", "复核日期:2026-10-24")
        self.assertHasError(self.lint_memo("capex", memo), "复核日期 2026-10-24 是周六，要落在工作日（可改为 2026-10-26）")
        memo = textwrap.dedent(GOOD_MEMO).replace("复核日期:2026-10-26", "复核日期:2026-10-32")
        self.assertHasError(self.lint_memo("capex", memo), "解析不了")
        memo = textwrap.dedent(GOOD_MEMO).replace("复核日期:2026-10-26", "复核日期:10 月 26 日")
        self.assertHasError(self.lint_memo("capex", memo), "要写成 YYYY-MM-DD")

    def test_dollar_numbers_need_a_tag(self):
        memo = textwrap.dedent(GOOD_MEMO).replace(" [演练]", "")
        lint = self.lint_memo("capex", memo)
        self.assertHasError(lint, "出现了 $ 数字，但没有 [演练] 或 [记忆·未核]")
        # 报在段落首行（“1. 事实”标题和列表之间没有空行，属于同一段）
        self.assertTrue(any(e.startswith(f"{TODAY}-capex.md:3：") for e in lint.errors), lint.errors)
        self.assertClean(self.lint_memo("capex", textwrap.dedent(GOOD_MEMO).replace("[演练]", "[记忆·未核]")))
        # 标签只要在同一段里就行
        memo = textwrap.dedent(GOOD_MEMO).replace(
            "- 全口径投入 $1,200 万 [演练],回本期 5.3 年(来源:来件 2)。",
            "- 全口径投入 $1,200 万,回本期 5.3 年。\n- 以上数字均为 [演练]。")
        self.assertClean(self.lint_memo("capex", memo))

    def test_article_number_over_10(self):
        memo = textwrap.dedent(GOOD_MEMO).replace("宪法第 6 条", "宪章第 21-26 条")
        lint = self.lint_memo("capex", memo)
        self.assertHasError(lint, "“第 21 条”超出宪法的 10 条")
        self.assertHasError(lint, "“第 26 条”超出宪法的 10 条")
        self.assertEqual(ot.article_numbers("第十二条与第2、3条"), [12, 2, 3])

    def test_skeptic_card_is_checked(self):
        memo = ("证伪报告 · 第二条产线\n【我的结论】勉强放行\n"
                "【如果我错了】可能错过更便宜的方案。本结论被推翻的概率 p=0.30\n"
                "- J-20261001-S｜命题：超支｜判定：看 OI-003 回件｜截止：2026-10-24｜p=0.33\n")
        lint = self.lint_memo("skeptic", memo)
        self.assertHasError(lint, "p=0.33 不合规")
        self.assertHasError(lint, "截止 2026-10-24 是周六")
        lint = self.lint_memo("skeptic", "证伪报告 · 某提案\n【如果我错了】无\n")
        self.assertHasError(lint, "【如果我错了】不许为空")

    def test_brief_with_pasted_due_output_passes(self):
        # chief-of-staff 把 due 的输出原样贴进简报：里面的 J- 行不是新写的卡，不能按卡格式报错
        self.repo.decisions("""
            # 决策日志

            ## 2026-09-25 · 某条目
            - J-20260925-1｜命题：对方交来一页纸｜判定：看 OI-003 回件｜截止：2026-10-01｜p=0.60
            """)
        self.repo.calendar([("2026-09-25", "chairman")])
        _, due_out = self.repo.run("due", TODAY)
        self.assertIn("- J-20260925-1｜", due_out)
        brief = "当日简报 · 2026-10-01\n【到期复核】\n" + due_out
        self.assertClean(self.lint_memo("brief", brief))

    # ---- decisions.md 里当天的条目与复核记录 ----

    def lint_decisions(self, text: str) -> ot.Lint:
        self.repo.decisions(text)
        lint = ot.Lint()
        ot.lint_decisions(lint, self.repo.root, D(TODAY))
        return lint

    OLD_ENTRY = """
        # 决策日志

        ## 2026-09-10 · 旧条目
        - J-20260910-1｜命题：对方回信｜判定：看回件｜截止：2026-10-01｜p=0.70
        """

    def test_good_cards_today(self):
        lint = self.lint_decisions(self.OLD_ENTRY + """
        ## 2026-10-01 · 今天的条目
        - J-20261001-1｜命题：一页纸按时到｜判定：看 OI-003 回件｜截止：2026-10-15｜p=0.60
        - J-20261001-S｜命题：超支｜判定：看 OI-003 回件｜截止：2026-11-02｜p=0.25
        """)
        self.assertClean(lint)
        self.assertEqual(lint.warnings, [])

    def test_bad_cards_today(self):
        lint = self.lint_decisions(self.OLD_ENTRY + """
        ## 2026-10-01 · 今天的条目
        - J-20261001-1｜命题：甲｜判定：乙｜截止：2026-10-24｜p=0.97
        - J-20260930-2｜命题：甲｜判定：乙｜截止：2026-10-15｜p=0.33
        - J-20261001-3|命题：甲|判定：乙|截止：2026-10-15|p=0.50
        - J-20261001-4｜命题：甲｜判定：乙｜截止：2026-10-15｜p=0.50 我很确定
        - **复核日期**:2026-10-24
        """)
        self.assertHasError(lint, "截止 2026-10-24 是周六")
        self.assertHasError(lint, "p=0.97 不合规")
        self.assertHasError(lint, "p=0.33 不合规")
        self.assertHasError(lint, "卡号日期 20260930 应为当天模拟日 20261001")
        self.assertHasError(lint, "全角竖线")
        self.assertHasError(lint, "p=0.xx 后面不要再写字")
        self.assertHasError(lint, "复核日期 2026-10-24 是周六")
        self.assertTrue(any("只按卡追踪" in w for w in lint.warnings))

    def test_review_records_today(self):
        base = self.OLD_ENTRY.rstrip() + "\n"
        lint = self.lint_decisions(base + "        - 复核 2026-10-01 · J-20260910-1：顺延至 2026-10-15\n")
        self.assertHasError(lint, "必须带原因码")
        lint = self.lint_decisions(base + "        - 复核 2026-10-01 · J-20260910-1：顺延至 2026-10-17（无来件）\n")
        self.assertHasError(lint, "顺延日期 2026-10-17 是周六")
        lint = self.lint_decisions(base + "        - 复核 2026-10-01 · J-20260910-1：对\n")
        self.assertHasError(lint, "不用 对/错")
        lint = self.lint_decisions(base + "        - 复核 2026-09-15 · J-20260910-1：顺延至 2026-10-01（无来件）\n"
                                          "        - 复核 2026-10-01 · J-20260910-1：顺延至 2026-10-15（无来件）\n")
        self.assertHasError(lint, "顺延只许一次")
        lint = self.lint_decisions(base + "        - 复核 2026-10-01 · J-20260910-1：未发生（无来件）\n")
        self.assertClean(lint)

    def test_malformed_review_line_is_flagged(self):
        # 漏了“ · ”或冒号的复核记录 index 读不到，卡会一直挂着：lint 要报出来
        base = self.OLD_ENTRY.rstrip() + "\n"
        for bad in ("        - 复核 2026-10-01 J-20260910-1：发生\n",
                    "        - 复核 2026-10-01 · J-20260910-1 发生\n"):
            lint = self.lint_decisions(base + bad)
            self.assertHasError(lint, "复核记录格式不对")
        # 别的日子写下的、以及格式正确的，不报
        lint = self.lint_decisions(base + "        - 复核 2026-09-15 J-20260910-1：发生\n")
        self.assertFalse(any("复核记录格式不对" in e for e in lint.errors), lint.errors)
        lint = self.lint_decisions(base + "        > 复核 2026-10-01：顺延至 2026-10-15（无来件）。一句话。\n")
        self.assertFalse(any("复核记录格式不对" in e for e in lint.errors), lint.errors)

    def test_calendar_row_today(self):
        self.repo.write(f"office/memos/{TODAY}-capex.md", GOOD_MEMO)
        self.repo.decisions(self.OLD_ENTRY)
        self.repo.calendar([("2026-09-30", "chairman")])
        code, out = self.repo.run("lint", TODAY)
        self.assertEqual(code, ot.EXIT_LINT_FAIL)
        self.assertIn(f"表格里没有 {TODAY} 这一行", out)
        self.repo.calendar([("2026-09-30", "chairman"), (TODAY, "")])
        code, out = self.repo.run("lint", TODAY)
        self.assertIn("「调用岗位」还空着", out)
        self.repo.calendar([("2026-09-30", "chairman"), (TODAY, "chief-of-staff、chairman")])
        code, out = self.repo.run("lint", TODAY)
        self.assertEqual(code, ot.EXIT_OK, out)

    def test_cli_exit_codes(self):
        self.repo.write(f"office/memos/{TODAY}-capex.md", GOOD_MEMO)
        self.repo.decisions(self.OLD_ENTRY)
        code, out = self.repo.run("lint", TODAY)
        self.assertEqual(code, ot.EXIT_OK, out)
        self.assertIn("通过", out)
        self.repo.write(f"office/memos/{TODAY}-patrol.md", GOOD_MEMO.replace(" [演练]", ""))
        code, out = self.repo.run("lint", TODAY)
        self.assertEqual(code, ot.EXIT_LINT_FAIL)
        self.assertIn("未通过：1 处错误", out)
        # 已写好的 lint 说明文件不参与检查
        self.repo.write(f"office/memos/{TODAY}-lint.md", "x\n" * 100)
        self.assertEqual(self.repo.run("lint", TODAY)[1].count("超过一页"), 0)


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------

def scored_decisions(samples: list[tuple[float, str]], extra: str = "") -> str:
    lines = ["# 决策日志", "", "## 2026-09-01 · 记分样本"]
    for i, (p, _) in enumerate(samples, start=1):
        seq = "S" if i == len(samples) and extra == "S" else str(i)
        lines.append(f"- J-20260901-{seq}｜命题：事件 {i}｜判定：看回件｜截止：2026-09-15｜p={p:.2f}")
    for i, (_, outcome) in enumerate(samples, start=1):
        seq = "S" if i == len(samples) and extra == "S" else str(i)
        lines.append(f"- 复核 2026-09-15 · J-20260901-{seq}：{outcome}")
    lines += ["", "## 2026-09-02 · 不计分的",
              "- J-20260902-1｜命题：作废的｜判定：看回件｜截止：2026-09-15｜p=0.90",
              "- J-20260902-2｜命题：未判的｜判定：看回件｜截止：2026-12-15｜p=0.90",
              "- 复核 2026-09-15 · J-20260902-1：作废（无来件）",
              "", "## 2026-09-03 · 旧格式",
              "- **复核日期**:2026-09-10",
              "> 复核 2026-09-10:对。"]
    return "\n".join(lines) + "\n"


class ScoreTests(unittest.TestCase):
    def test_insufficient_sample(self):
        repo = Repo(self)
        repo.decisions("# 决策日志\n")
        self.assertEqual(repo.run("score"), (0, "样本不足 n=0\n"))
        samples = [(0.6, "发生")] * 19
        repo.decisions(scored_decisions(samples))
        self.assertEqual(repo.run("score"), (0, "样本不足 n=19\n"))

    def test_brier_with_20_cards(self):
        ps = [0.1, 0.3, 0.5, 0.7, 0.9]
        samples = []
        for i in range(20):
            p = ps[i % 5]
            samples.append((p, "发生" if (i % 3 == 0) else "未发生"))
        repo = Repo(self)
        repo.decisions(scored_decisions(samples, extra="S"))
        code, out = repo.run("score")
        self.assertEqual(code, 0)
        ys = [1 if o == "发生" else 0 for _, o in samples]
        expected = sum((p - y) ** 2 for (p, _), y in zip(samples, ys)) / 20
        self.assertIn("n=20（另有作废 1 张，不计分）", out)
        self.assertIn(f"Brier = {expected:.4f}；常数 0.5 基准 = 0.2500；差 = {expected - 0.25:+.4f}", out)
        # 第 20 张是证伪席的 S 卡
        s_p, s_y = samples[-1][0], ys[-1]
        self.assertIn(f"vice-chairman-skeptic：n=1，Brier={(s_p - s_y) ** 2:.4f}，发生 {s_y}/1", out)
        chair = [((p - y) ** 2, y) for (p, _), y in zip(samples[:-1], ys[:-1])]
        self.assertIn(f"chairman：n=19，Brier={sum(b for b, _ in chair) / 19:.4f}，发生 {sum(y for _, y in chair)}/19", out)
        # 分桶：0.1/0.3 → 低桶，0.5 → 中桶，0.7/0.9 → 高桶
        for label, members in (("0.05–0.35", (0.1, 0.3)), ("0.40–0.60", (0.5,)), ("0.65–0.95", (0.7, 0.9))):
            sub = [y for (p, _), y in zip(samples, ys) if p in members]
            k, n = sum(sub), len(sub)
            lo, hi = ot.wilson(k, n)
            self.assertIn(f"  {label}：n={n}，", out)
            self.assertIn(f"实际发生 {k}/{n}={k / n:.2f}，95% 区间 [{lo:.2f}, {hi:.2f}]", out)

    def test_wilson(self):
        lo, hi = ot.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=4)
        self.assertAlmostEqual(hi, 0.7634, places=4)
        lo, hi = ot.wilson(0, 20)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.1611, places=4)
        self.assertEqual(ot.wilson(0, 0), (0.0, 1.0))

    def test_brier_function(self):
        self.assertAlmostEqual(ot.brier([(0.5, 1), (0.5, 0)]), 0.25)
        self.assertAlmostEqual(ot.brier([(0.9, 1), (0.2, 0)]), (0.01 + 0.04) / 2)


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

class CliTests(unittest.TestCase):
    def test_root_option_before_or_after_subcommand(self):
        repo = Repo(self)
        repo.briefs("2026-09-24")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ot.main(["gap", "2026-09-25", "--number", "--root", str(repo.root)])
        self.assertEqual(buf.getvalue(), "1\n")

    def test_no_subcommand_prints_help(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(ot.main([]), 0)
        self.assertIn("gap", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
