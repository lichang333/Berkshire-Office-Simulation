#!/usr/bin/env python3
"""office_tool.py · 总部模拟的账本小工具 [演练]

只用 Python 标准库。只由主会话用 Bash 运行；岗位 agent 不运行它（有 Bash 的岗位也只用 Bash 核对算术），只读主会话贴给它们的输出。
除 `index` 会重新生成 office/ledger/reviews.csv 之外，其余子命令只输出、不改任何文件。

子命令：
  gap   <date>   距上次开门几个工作日（上次开门 = office/memos/*-brief.md 文件名里最大的、早于 date 的日期）
  draw  <date>   对 open-items.md 里到期的未结事项按种子抽签，给出回件结局
  cards <date>   从 office/generator/archetypes.md 按种子选 2–3 张来件原型牌
  index          解析 decisions.md，重新生成 office/ledger/reviews.csv
  due   <date>   列出已到期、已逾期和即将到期的判断卡与旧格式复核日期
  lint  <date>   提交前检查当日备忘录与当日决策条目的格式
  score          判断卡记分（Brier 分，样本不足 20 张时只报样本数）

工作日 = 周一到周五，不考虑节假日。本工具属于组织决策流程模拟，不构成投资建议，不执行任何交易。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import math
import re
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

MEMOS_DIR = Path("office/memos")
DECISIONS = Path("office/ledger/decisions.md")
CALENDAR = Path("office/ledger/calendar.md")
OPEN_ITEMS = Path("office/ledger/open-items.md")
REVIEWS_CSV = Path("office/ledger/reviews.csv")
ARCHETYPES = Path("office/generator/archetypes.md")

WEEKDAY_CN = "一二三四五六日"
REASON_CODES = ("无来件", "证据不足", "运行断档", "条件变更")
IDLE_ROLES = ("treasury-desk", "cfo-controller", "general-counsel", "shareholder-scribe")
CSV_COLUMNS = ["id", "opened", "entry", "role", "proposition", "rule", "due", "p",
               "status", "outcome_date", "outcome", "code"]
BANNED_WORDS = ("赋能", "抓手", "生态位", "战略协同", "预计将实现")
MEMO_MAX_LINES = 60
DUE_AHEAD_DAYS = 31
CARD_WINDOW_DAYS = 10
SCORE_MIN_N = 20

EXIT_OK, EXIT_LINT_FAIL, EXIT_MISSING = 0, 1, 2


# ---------------------------------------------------------------------------
# 日期与工作日
# ---------------------------------------------------------------------------

DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")


def parse_date(s: str) -> dt.date:
    """严格解析 YYYY-MM-DD。"""
    s = s.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"日期要写成 YYYY-MM-DD：{s!r}")
    return dt.date.fromisoformat(s)


def try_date(s: str) -> dt.date | None:
    try:
        return parse_date(s)
    except ValueError:
        return None


def first_date(cell: str) -> dt.date | None:
    """取一段文字里第一个能解析的 YYYY-MM-DD。"""
    for m in DATE_RE.finditer(cell or ""):
        d = try_date(m.group(1))
        if d:
            return d
    return None


def is_workday(d: dt.date) -> bool:
    return d.weekday() < 5


def roll_to_workday(d: dt.date) -> dt.date:
    """周六、周日顺延到下一个周一；工作日原样返回。"""
    while not is_workday(d):
        d += dt.timedelta(days=1)
    return d


def busday_count(start: dt.date, end: dt.date) -> int:
    """[start, end) 里的工作日数（与 numpy.busday_count 同义）。end <= start 时为 0。"""
    if end <= start:
        return 0
    days = (end - start).days
    full_weeks, rest = divmod(days, 7)
    n = full_weeks * 5
    wd = start.weekday()
    for i in range(rest):
        if (wd + i) % 7 < 5:
            n += 1
    return n


def workdays_between(a: dt.date, b: dt.date) -> list[dt.date]:
    """严格介于 a 与 b 之间的工作日（两端都不含）。"""
    out = []
    d = a + dt.timedelta(days=1)
    while d < b:
        if is_workday(d):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def add_workdays(d: dt.date, n: int) -> dt.date:
    """从 d 起往后数 n 个工作日（不含 d 本身）。"""
    while n > 0:
        d += dt.timedelta(days=1)
        if is_workday(d):
            n -= 1
    return d


def weekday_cn(d: dt.date) -> str:
    return "周" + WEEKDAY_CN[d.weekday()]


def fmt_due(orig: dt.date) -> str:
    rolled = roll_to_workday(orig)
    if rolled == orig:
        return orig.isoformat()
    return f"{orig.isoformat()}→{rolled.isoformat()}（{weekday_cn(orig)}，顺延到工作日）"


# ---------------------------------------------------------------------------
# 文件与表格
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except FileNotFoundError:
        return None


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_sep_row(line: str) -> bool:
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c)


def _norm_header(h: str) -> str:
    h = h.replace("*", "").strip()
    return re.sub(r"\s*[（(].*$", "", h).strip()


def parse_table(text: str, key_header: str) -> list[dict]:
    """找第一张表头里含 key_header 的 markdown 表，返回行字典（键为表头，另带 _line 行号）。"""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.strip().startswith("|") or i + 1 >= len(lines):
            continue
        headers = [_norm_header(h) for h in _split_row(line)]
        if key_header not in headers or not _is_sep_row(lines[i + 1]):
            continue
        rows = []
        j = i + 2
        while j < len(lines) and lines[j].strip().startswith("|"):
            cells = _split_row(lines[j])
            cells += [""] * (len(headers) - len(cells))
            row = dict(zip(headers, cells[: len(headers)]))
            row["_line"] = j + 1
            rows.append(row)
            j += 1
        return rows
    return []


def brief_dates(root: Path) -> list[dt.date]:
    out = []
    memos = root / MEMOS_DIR
    if memos.is_dir():
        for p in memos.glob("*-brief.md"):
            m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})-brief\.md", p.name)
            if m:
                d = try_date(m.group(1))
                if d:
                    out.append(d)
    return sorted(set(out))


def calendar_rows(root: Path) -> list[dict]:
    text = read_text(root / CALENDAR)
    if text is None:
        return []
    rows = []
    for r in parse_table(text, "模拟日"):
        d = first_date(r.get("模拟日", ""))
        if d:
            r["_date"] = d
            rows.append(r)
    return rows


def opening_days(root: Path) -> tuple[list[dt.date], str]:
    """开门日：优先取 calendar.md 的模拟日列；没有 calendar 时退回 *-brief.md 的日期。"""
    rows = calendar_rows(root)
    if rows:
        return sorted({r["_date"] for r in rows}), str(CALENDAR)
    briefs = brief_dates(root)
    return briefs, f"{MEMOS_DIR}/*-brief.md（未找到 {CALENDAR} 的模拟日，临时退回）"


# ---------------------------------------------------------------------------
# gap
# ---------------------------------------------------------------------------

def gap_info(root: Path, d: dt.date) -> tuple[dt.date | None, int | None, list[dt.date]]:
    prior = [x for x in brief_dates(root) if x < d]
    if not prior:
        return None, None, []
    last = prior[-1]
    return last, busday_count(last, d), workdays_between(last, d)


def cmd_gap(args) -> int:
    root, d = args.root, args.date
    last, n, missed = gap_info(root, d)
    if last is None:
        if args.number:
            print("")
        else:
            print(f"距上次开门 —（{MEMOS_DIR} 里没有早于 {d} 的 *-brief.md，无开门记录）")
        return EXIT_OK
    if args.number:
        print(n)
        return EXIT_OK
    print(f"距上次开门 {n} 个工作日（上次开门 {last}，{weekday_cn(last)}）")
    if missed:
        print(f"未开门的工作日：{missed[0]} 至 {missed[-1]}，共 {len(missed)} 天")
    else:
        print("未开门的工作日：无")
    if not is_workday(d):
        print(f"注意：{d} 是{weekday_cn(d)}，不是工作日")
    return EXIT_OK


# ---------------------------------------------------------------------------
# draw
# ---------------------------------------------------------------------------

OPEN_STATUSES = ("待回", "迟到")


def draw_u(date: dt.date, item_id: str) -> int:
    return int(hashlib.sha256(f"{date.isoformat()}|{item_id}".encode("utf-8")).hexdigest(), 16) % 20


def draw_outcome(u: int, has_deadline: bool) -> str:
    if has_deadline:
        if u <= 10:
            return "按时"
        if u <= 14:
            return "迟到"
    else:
        if u <= 4:
            return "按时"
        if u <= 14:
            return "迟到"
    if u <= 17:
        return "沉默"
    return "坏消息"


def _status_of(cell: str) -> str:
    cell = cell.strip()
    for s in ("待回", "迟到", "已回", "沉默", "关闭"):
        if cell.startswith(s):
            return s
    return cell


def load_open_items(root: Path) -> tuple[list[dict] | None, list[str]]:
    text = read_text(root / OPEN_ITEMS)
    if text is None:
        return None, []
    items, warnings = [], []
    for r in parse_table(text, "编号"):
        oid = r.get("编号", "").strip()
        if not re.fullmatch(r"OI-\d{3,}", oid):
            continue
        raw = r.get("期限", "").strip()
        if raw.startswith("无"):
            deadline = None
        else:
            dates = [x for x in (try_date(m.group(1)) for m in DATE_RE.finditer(raw)) if x]
            if not dates:
                warnings.append(f"{OPEN_ITEMS}:{r['_line']} {oid} 的期限 {raw!r} 解析不了，本次跳过")
                continue
            # 期限只会往后顺延：格里有多个日期时取最晚的那个，并提醒只留一个
            deadline = max(dates)
            if len(dates) > 1:
                warnings.append(f"{OPEN_ITEMS}:{r['_line']} {oid} 的期限格有 {len(dates)} 个日期，按最晚的 {deadline} 算；"
                                "期限格只留当前期限，旧期限写进「结局记录」")
        items.append({
            "id": oid, "opened": first_date(r.get("发起日", "")), "what": r.get("事项", ""),
            "party": r.get("对方", ""), "owner": r.get("我方主责", ""), "deadline": deadline,
            "status": _status_of(r.get("状态", "")), "record": r.get("结局记录", ""), "line": r["_line"],
        })
    return items, warnings


def cmd_draw(args) -> int:
    root, d = args.root, args.date
    items, warnings = load_open_items(root)
    if items is None:
        print(f"找不到 {OPEN_ITEMS}，无法抽签。")
        return EXIT_MISSING
    print(f"抽签 · {d} · u = int(sha256(\"{d}|<编号>\"), 16) % 20 [演练]")
    print("有期限：0–10 按时 / 11–14 迟到 / 15–17 沉默 / 18–19 坏消息；期限为“无”：0–4 按时 / 5–14 迟到 / 15–17 沉默 / 18–19 坏消息")
    for w in warnings:
        print(f"警告：{w}")
    due = []
    for it in items:
        if it["status"] not in OPEN_STATUSES:
            continue
        if it["deadline"] is not None and roll_to_workday(it["deadline"]) > d:
            continue
        due.append(it)
    if not due:
        print("今日无到期事项（没有期限已到、状态为待回或迟到的事项）。")
    for it in due:
        u = draw_u(d, it["id"])
        outcome = draw_outcome(u, it["deadline"] is not None)
        dl = "无" if it["deadline"] is None else fmt_due(it["deadline"])
        extra = ""
        if outcome == "迟到":
            extra = f"｜新期限（今天起顺延 5 个工作日）{add_workdays(d, 5)}"
        elif outcome == "沉默":
            extra = "｜不写回件，只在 open-items 记一笔"
        print(f"{it['id']}｜u={u}｜{outcome}｜期限 {dl}｜对方 {it['party']}｜{it['what']}{extra}")
    silent = [it for it in items if it["status"] == "沉默"]
    for it in silent:
        rec_dates = [x for x in (try_date(m.group(1)) for m in DATE_RE.finditer(it["record"])) if x]
        since = max(rec_dates) if rec_dates else (roll_to_workday(it["deadline"]) if it["deadline"] else it["opened"])
        if since is None:
            continue
        n = busday_count(since, d)
        flag = "，满 30 个工作日，应自动关闭并写一行原因" if n >= 30 else ""
        print(f"沉默中：{it['id']} 自 {since} 起已沉默 {n} 个工作日{flag}")
    print("（只输出，不改文件。）")
    return EXIT_OK


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------

INJECTION_RE = re.compile(r"伪授权|提示注入")


def load_archetypes(root: Path) -> list[dict] | None:
    text = read_text(root / ARCHETYPES)
    if text is None:
        return None
    cards = []
    for r in parse_table(text, "编号"):
        cid = r.get("编号", "").strip()
        if not re.fullmatch(r"A-\d{2,}", cid):
            continue
        cards.append({
            "id": cid, "type": r.get("类型", ""), "disguise": r.get("常见伪装", ""),
            "assignee": r.get("派给", ""), "path": r.get("期望路径", ""),
            "last": first_date(r.get("上次使用", "")), "last_raw": r.get("上次使用", "").strip(),
        })
    return cards


def _is_idle_card(card: dict, roles: tuple[str, ...] = IDLE_ROLES) -> bool:
    return any(role in card["assignee"] for role in roles)


def never_played_roles(cal_rows: list[dict], d: dt.date) -> tuple[str, ...]:
    """IDLE_ROLES 里，在 d 所在 ISO 周之前的 calendar「调用岗位」中一次都没出现过的岗位。

    以周一为界：本周内第一次上场的岗位，本周仍算“从未上场”，这样它当周的出场能满足强制补牌。
    """
    week_start = d - dt.timedelta(days=d.weekday())
    played = " ".join(r.get("调用岗位", "") for r in cal_rows if r["_date"] < week_start)
    return tuple(role for role in IDLE_ROLES if role not in played)


def select_cards(cards: list[dict], d: dt.date, openings: list[dt.date],
                 week_roles: list[str] | None, force: bool = True,
                 idle_roles: tuple[str, ...] = IDLE_ROLES) -> dict:
    """纯函数：按 sha256(date) 选牌。week_roles 为本周 calendar 的调用岗位文字，None 表示 calendar 缺失；
    idle_roles 为“从未上场的岗位”（见 never_played_roles），为空时不再强制补牌。"""
    seed = hashlib.sha256(d.isoformat().encode("utf-8")).hexdigest()
    k = 2 + int(seed, 16) % 2

    def rank(c):
        return hashlib.sha256(f"{seed}|{c['id']}".encode("utf-8")).hexdigest()

    prior = sorted(o for o in openings if o < d)
    window = prior[-CARD_WINDOW_DAYS:]
    cutoff = window[0] if window else None

    def recent(c):
        if c["last"] is None:
            return False
        if c["last"] >= d:
            return True
        return cutoff is not None and c["last"] >= cutoff

    iso = d.isocalendar()[:2]
    injection_used_this_week = any(
        INJECTION_RE.search(c["type"]) and c["last"] and c["last"].isocalendar()[:2] == iso and c["last"] <= d
        for c in cards)

    def blocked(c):
        return bool(injection_used_this_week and INJECTION_RE.search(c["type"]))

    fresh = sorted((c for c in cards if not recent(c) and not blocked(c)), key=rank)
    stale = sorted((c for c in cards if recent(c) and not blocked(c)),
                   key=lambda c: (c["last"] or dt.date.min, rank(c)))
    picks = fresh[:k]
    notes = []
    if len(picks) < k:
        need = k - len(picks)
        filler = stale[:need]
        picks += filler
        notes.append(f"牌库不足：最近 {CARD_WINDOW_DAYS} 个模拟日内没用过的牌只有 {len(fresh)} 张，"
                     f"按最久未用补了 {len(filler)} 张（{'、'.join(c['id'] for c in filler) or '无'}）。该补新牌了。")
    if injection_used_this_week:
        notes.append("本周已用过伪授权/提示注入类牌，这类牌本周不再选（每周最多 1 次）。")

    forced = None
    idle_roles = tuple(idle_roles)
    idle_played = bool(week_roles) and any(role in text for text in week_roles for role in idle_roles)
    if force and idle_roles and picks and not idle_played \
            and not any(_is_idle_card(c, idle_roles) for c in picks):
        chosen = [c for c in picks]
        pool = [c for c in fresh if _is_idle_card(c, idle_roles) and c not in chosen]
        if not pool:
            pool = [c for c in stale if _is_idle_card(c, idle_roles) and c not in chosen]
        if pool:
            out_card, in_card = picks[-1], pool[0]
            picks = picks[:-1] + [in_card]
            forced = (out_card, in_card)
        else:
            notes.append("需要强制补牌，但牌库里没有派给 " + "/".join(idle_roles) + " 的可用牌。")
    return {"seed": seed, "k": k, "picks": picks, "forced": forced, "notes": notes,
            "window": window, "idle_played": idle_played, "idle_roles": idle_roles}


def cmd_cards(args) -> int:
    root, d = args.root, args.date
    cards = load_archetypes(root)
    if cards is None:
        print(f"找不到 {ARCHETYPES}，无法选牌。")
        return EXIT_MISSING
    if not cards:
        print(f"{ARCHETYPES} 里没有解析到牌（表头要有“编号”，编号形如 A-01）。")
        return EXIT_MISSING
    openings, source = opening_days(root)
    cal = calendar_rows(root)
    iso = d.isocalendar()[:2]
    week_roles = [r.get("调用岗位", "") for r in cal if r["_date"].isocalendar()[:2] == iso and r["_date"] <= d] \
        if cal else None
    res = select_cards(cards, d, openings, week_roles, idle_roles=never_played_roles(cal, d))
    print(f"选牌 · {d} · 种子 sha256(\"{d}\") = {res['seed'][:12]}… · 本次 {res['k']} 张 [演练]")
    if res["window"]:
        print(f"窗口：最近 {len(res['window'])} 个模拟日 {res['window'][0]} 至 {res['window'][-1]}（取自 {source}），其间用过的牌不选")
    else:
        print(f"窗口：{d} 之前没有模拟日记录（取自 {source}）")
    for c in res["picks"]:
        last = c["last"].isoformat() if c["last"] else (c["last_raw"] or "从未")
        print(f"{c['id']}｜{c['type']}｜常见伪装：{c['disguise']}｜派给 {c['assignee']}｜期望路径 {c['path']}｜上次使用 {last}")
    if res["forced"]:
        out_c, in_c = res["forced"]
        week = f"{iso[0]}-W{iso[1]:02d}"
        why = "calendar.md 缺失" if week_roles is None else "本周 calendar 的调用岗位里"
        print(f"强制补牌：{week}，{why}还没有 {'/'.join(res['idle_roles'])} 上场，已把 {out_c['id']} 换成 {in_c['id']}（派给 {in_c['assignee']}）")
    for n in res["notes"]:
        print(n)
    print(f"（只输出，不改文件。写完来件后由主会话把这几张牌的“上次使用”改成 {d}。牌库只给主会话看，不交给岗位 agent。）")
    return EXIT_OK


# ---------------------------------------------------------------------------
# decisions.md 解析（index / due / score / lint 共用）
# ---------------------------------------------------------------------------

ENTRY_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*[·•]\s*(.+?)\s*$")
CARD_START_RE = re.compile(r"^\s*(?:[-*]\s*)?(J-(\d{8})-(\d+|S))\s*[｜|]")
CARD_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<id>J-(?P<ymd>\d{8})-(?P<seq>\d+|S))\s*[｜|]\s*命题\s*(?P<c1>[：:])\s*(?P<prop>.+?)\s*[｜|]"
    r"\s*判定\s*(?P<c2>[：:])\s*(?P<rule>.+?)\s*[｜|]\s*截止\s*(?P<c3>[：:])\s*(?P<due>[^｜|]+?)\s*[｜|]"
    r"\s*p\s*=\s*(?P<p>[0-9]*\.?[0-9]+)(?P<tail>.*)$")
FIELD_REVIEW_RE = re.compile(r"^\s*[-*]\s*\*\*复核日期\*\*\s*[:：]\s*(.*)$")
REVIEW_ID_RE = re.compile(
    r"^\s*(?:[-*>]\s*)*复核\s*(\d{4}-\d{2}-\d{2})\s*[·•]\s*(J-\d{8}-(?:\d+|S)|L-\d{8}-\d+)\s*[：:]\s*(.*)$")
REVIEW_OLD_RE = re.compile(r"^\s*(?:[-*>]\s*)+复核\s*(\d{4}-\d{2}-\d{2})\s*[：:]\s*(.*)$")
# 看起来像复核记录（行首“- / >”后紧跟“复核 YYYY-MM-DD”）的行；lint 用它找出写错格式、会被 index 漏登的记录
REVIEW_LOOSE_RE = re.compile(r"^\s*(?:[-*>]\s*)+(?:\*\*)?复核(?:\*\*)?\s*(\d{4}-\d{1,2}-\d{1,2})")
OUTCOME_RE = re.compile(r"^\s*(顺延至\s*(\d{4}-\d{1,2}-\d{1,2})|顺延|未发生|发生|作废|对|错)")
CODE_PAREN_RE = re.compile(r"^\s*[（(]([^）)]*)[）)]")


class Entry:
    def __init__(self, date: dt.date, title: str, line: int):
        self.date, self.title, self.line = date, title, line
        self.cards: list[dict] = []
        self.field_lines: list[tuple[int, str]] = []
        self.old_reviews: list[dict] = []

    @property
    def label(self) -> str:
        return f"{self.date} · {self.title}"


class Item:
    def __init__(self, kind, iid, entry: Entry, due: dt.date, line: int, proposition="", rule="", p=None):
        self.kind, self.id, self.entry, self.line = kind, iid, entry, line
        self.opened = entry.date
        self.role = "vice-chairman-skeptic" if iid.endswith("-S") else "chairman"
        self.proposition, self.rule, self.p = proposition, rule, p
        self.due_orig = due
        self.due = due
        self.status = "未决"
        self.outcome_date: dt.date | None = None
        self.outcome = ""
        self.code = ""
        self.postpones = 0
        self.auto_void = False

    @property
    def open(self) -> bool:
        return self.status in ("未决", "顺延")


def parse_outcome(body: str) -> dict:
    """解析复核记录冒号后的正文。"""
    m = OUTCOME_RE.match(body)
    res = {"kind": None, "new_date": None, "code": "", "raw": body.strip(), "bad_date": None}
    if not m:
        return res
    word = m.group(1)
    if word.startswith("顺延"):
        res["kind"] = "顺延"
        if m.group(2):
            nd = try_date(m.group(2))
            if nd is None:
                res["bad_date"] = m.group(2)
            res["new_date"] = nd
    else:
        res["kind"] = word
    rest = body[m.end():]
    pm = CODE_PAREN_RE.match(rest)
    if pm:
        for code in REASON_CODES:
            if code in pm.group(1):
                res["code"] = code
                break
    return res


def _paren_depth(s: str, pos: int) -> int:
    depth = 0
    for ch in s[:pos]:
        if ch in "(（":
            depth += 1
        elif ch in ")）" and depth > 0:
            depth -= 1
    return depth


def _split_top_level(s: str, seps: str = ";；") -> list[str]:
    """只在括号外的分号处切分。"""
    parts, buf, depth = [], [], 0
    for ch in s:
        if ch in "(（":
            depth += 1
        elif ch in ")）" and depth > 0:
            depth -= 1
        if ch in seps and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def extract_field_dates(s: str) -> list[tuple[str, str]]:
    """从 **复核日期** 行取括号外的 YYYY-MM-DD，每个配一段说明文字。"""
    out = []
    for seg in _split_top_level(s):
        found = [m for m in DATE_RE.finditer(seg) if _paren_depth(seg, m.start()) == 0]
        if not found:
            continue
        note = seg
        for m in found:
            note = note.replace(m.group(1), " ")
        note = note.strip(" \t。.,，:：")
        if note[:1] in "(（" and note[-1:] in ")）":
            note = note[1:-1].strip()
        for m in found:
            out.append((m.group(1), note))
    return out


def parse_decisions(text: str) -> dict:
    lines = text.split("\n")
    entries: list[Entry] = []
    cur: Entry | None = None
    in_fence = False
    id_reviews = []
    malformed = []
    for idx, line in enumerate(lines, start=1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = ENTRY_RE.match(line)
        if m:
            d = try_date(m.group(1))
            if d:
                cur = Entry(d, m.group(2), idx)
                entries.append(cur)
            continue
        if cur is None:
            continue
        m = REVIEW_ID_RE.match(line)
        if m:
            id_reviews.append({"date": try_date(m.group(1)), "date_raw": m.group(1), "id": m.group(2),
                               "body": m.group(3), "line": idx, "entry": cur, **{"o": parse_outcome(m.group(3))}})
            continue
        m = REVIEW_OLD_RE.match(line)
        if m:
            cur.old_reviews.append({"date": try_date(m.group(1)), "date_raw": m.group(1), "body": m.group(2),
                                    "line": idx, "entry": cur, "o": parse_outcome(m.group(2))})
            continue
        if CARD_START_RE.match(line):
            cm = CARD_RE.match(line)
            due = try_date(cm.group("due").strip()) if cm else None
            p = None
            if cm:
                try:
                    p = float(cm.group("p"))
                except ValueError:
                    p = None
            if cm and due is not None and p is not None:
                cur.cards.append({"id": cm.group("id"), "ymd": cm.group("ymd"), "seq": cm.group("seq"),
                                  "prop": cm.group("prop"), "rule": cm.group("rule"), "due": due, "p": p,
                                  "line": idx, "text": line, "m": cm})
            else:
                malformed.append({"line": idx, "text": line, "entry": cur})
            continue
        m = FIELD_REVIEW_RE.match(line)
        if m:
            cur.field_lines.append((idx, m.group(1)))

    items: list[Item] = []
    by_id: dict[str, Item] = {}
    warnings = []
    for e in entries:
        for c in e.cards:
            if c["id"] in by_id:
                warnings.append(f"decisions.md:{c['line']} 卡号 {c['id']} 重复，只登记第一次出现的那张")
                continue
            it = Item("J", c["id"], e, c["due"], c["line"], c["prop"], c["rule"], c["p"])
            items.append(it)
            by_id[it.id] = it
    l_counter: dict[dt.date, int] = {}
    for e in entries:
        if e.cards:
            continue  # 新格式条目只按卡追踪
        for line_no, content in e.field_lines:
            for ds, note in extract_field_dates(content):
                d = try_date(ds)
                if d is None:
                    warnings.append(f"decisions.md:{line_no} 复核日期 {ds} 不是有效日期，未登记")
                    continue
                n = l_counter.get(e.date, 0) + 1
                l_counter[e.date] = n
                it = Item("L", f"L-{e.date.strftime('%Y%m%d')}-{n}", e, d, line_no, note)
                items.append(it)
                by_id[it.id] = it
    for m in malformed:
        warnings.append(f"decisions.md:{m['line']} 判断卡格式不对，未登记：{m['text'].strip()[:60]}")

    def apply(it: Item, rec: dict):
        o = rec["o"]
        if o["kind"] is None or rec["date"] is None:
            warnings.append(f"decisions.md:{rec['line']} 复核记录读不出结局，未计入")
            return
        if not it.open:
            warnings.append(f"decisions.md:{rec['line']} {it.id} 已经{it.status}，这条复核记录未计入")
            rec["skipped"] = it.status
            return
        rec["applied"] = True
        if o["kind"] == "顺延":
            it.postpones += 1
            rec["postpone_n"] = it.postpones
            if it.postpones >= 2:
                it.status, it.outcome, it.code = "作废", "作废", o["code"]
                it.outcome_date, it.auto_void = rec["date"], True
            else:
                it.status, it.code = "顺延", o["code"]
                if o["new_date"]:
                    it.due = o["new_date"]
        elif o["kind"] == "作废":
            it.status, it.outcome, it.code, it.outcome_date = "作废", "作废", o["code"], rec["date"]
        else:
            it.status, it.outcome, it.code, it.outcome_date = "已判", o["kind"], o["code"], rec["date"]

    for rec in sorted(id_reviews, key=lambda r: (r["date"] or dt.date.max, r["line"])):
        it = by_id.get(rec["id"])
        if it is None:
            warnings.append(f"decisions.md:{rec['line']} 复核记录引用了不存在的编号 {rec['id']}")
            continue
        rec["item"] = it
        apply(it, rec)
    old_all = []
    for e in entries:
        own = [it for it in items if it.entry is e]
        for rec in sorted(e.old_reviews, key=lambda r: (r["date"] or dt.date.max, r["line"])):
            old_all.append(rec)
            open_items = [it for it in own if it.open]
            if not open_items:
                warnings.append(f"decisions.md:{rec['line']} 这条复核记录所在条目没有未决的复核日期，未计入")
                continue
            ref = rec["date"] or dt.date.max
            due_now = [it for it in open_items if roll_to_workday(it.due) <= ref]
            target = min(due_now or open_items, key=lambda it: (it.due, it.line))
            rec["item"] = target
            apply(target, rec)

    return {"entries": entries, "items": items, "by_id": by_id, "warnings": warnings,
            "malformed": malformed, "id_reviews": id_reviews, "old_reviews": old_all}


def load_decisions(root: Path) -> dict | None:
    text = read_text(root / DECISIONS)
    if text is None:
        return None
    return parse_decisions(text)


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

def items_to_rows(items: list[Item]) -> list[list[str]]:
    rows = []
    for it in sorted(items, key=lambda i: i.line):
        rows.append([
            it.id, it.opened.isoformat(), it.entry.label, it.role, it.proposition, it.rule,
            it.due.isoformat(), "" if it.p is None else f"{it.p:.2f}", it.status,
            it.outcome_date.isoformat() if it.outcome_date else "", it.outcome, it.code,
        ])
    return rows


def cmd_index(args) -> int:
    root = args.root
    parsed = load_decisions(root)
    if parsed is None:
        print(f"找不到 {DECISIONS}。")
        return EXIT_MISSING
    rows = items_to_rows(parsed["items"])
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    w.writerows(rows)
    out = root / REVIEWS_CSV
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(buf.getvalue(), encoding="utf-8")
    items = parsed["items"]
    nj = sum(1 for i in items if i.kind == "J")
    nl = sum(1 for i in items if i.kind == "L")
    counts = {s: sum(1 for i in items if i.status == s) for s in ("未决", "顺延", "已判", "作废")}
    print(f"已写入 {REVIEWS_CSV}：{len(rows)} 行（判断卡 J {nj} 张，旧格式复核日期 L {nl} 条）")
    print("状态：" + "，".join(f"{k} {v}" for k, v in counts.items()))
    for wmsg in parsed["warnings"]:
        print(f"警告：{wmsg}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# due
# ---------------------------------------------------------------------------

def classify_due(items: list[Item], d: dt.date, openings: list[dt.date], ahead: int) -> dict:
    debt, cal_due, upcoming = [], [], []
    horizon = d + dt.timedelta(days=ahead)
    for it in items:
        if not it.open:
            continue
        rd = roll_to_workday(it.due)
        if rd > d:
            if rd <= horizon:
                upcoming.append((it, rd, (rd - d).days))
            continue
        cal_days = (d - rd).days
        opens = sum(1 for o in openings if rd <= o < d)
        row = (it, rd, cal_days, opens)
        (debt if opens >= 1 else cal_due).append(row)
    key = lambda r: (r[1], r[0].line)  # noqa: E731
    return {"debt": sorted(debt, key=key), "cal_due": sorted(cal_due, key=key),
            "upcoming": sorted(upcoming, key=key)}


def _item_desc(it: Item) -> str:
    if it.kind == "J":
        return f"命题：{it.proposition}｜判定：{it.rule}｜p={it.p:.2f}"
    return f"{it.proposition}" if it.proposition else "（旧格式，无说明）"


def _postpone_note(it: Item) -> str:
    if it.postpones:
        code = f"，原因码 {it.code}" if it.code else ""
        return f"｜已顺延 {it.postpones} 次（原定 {it.due_orig}{code}；再判不了只能作废）"
    return ""


def cmd_due(args) -> int:
    root, d = args.root, args.date
    parsed = load_decisions(root)
    if parsed is None:
        print(f"找不到 {DECISIONS}。")
        return EXIT_MISSING
    openings, source = opening_days(root)
    res = classify_due(parsed["items"], d, openings, args.ahead)
    print(f"到期复核 · {d}（截止日先经 roll_to_workday 顺延到工作日；开门日取自 {source}）")
    print("【按开门日已逾期】（办公室的欠账：截止后办公室开过门却没打分）")
    if not res["debt"]:
        print("无")
    for it, rd, cal_days, opens in res["debt"]:
        print(f"- {it.id}｜{it.entry.label}｜截止 {fmt_due(it.due)}｜按日历逾期 {cal_days} 天｜按开门日逾期 {opens} 次"
              f"｜{_item_desc(it)}{_postpone_note(it)}")
    print("【按日历已到期】（今天到期，或截止后办公室一直没开门；不算欠账，今天打分即可）")
    if not res["cal_due"]:
        print("无")
    for it, rd, cal_days, opens in res["cal_due"]:
        tag = "今天到期" if cal_days == 0 else f"按日历逾期 {cal_days} 天｜按开门日逾期 0 次"
        print(f"- {it.id}｜{it.entry.label}｜截止 {fmt_due(it.due)}｜{tag}｜{_item_desc(it)}{_postpone_note(it)}")
    print(f"【未来 {args.ahead} 天将到期】")
    if not res["upcoming"]:
        print("无")
    for it, rd, days in res["upcoming"]:
        print(f"- {it.id}｜{it.entry.label}｜截止 {fmt_due(it.due)}｜还有 {days} 天｜{_item_desc(it)}{_postpone_note(it)}")
    print("说明：L- 为旧格式“复核日期”，只追踪逾期、不计分；J- 为判断卡。只有“按开门日已逾期”算办公室欠账。")
    for wmsg in parsed["warnings"]:
        print(f"警告：{wmsg}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    ph = k / n
    denom = 1 + z * z / n
    center = (ph + z * z / (2 * n)) / denom
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def brier(pairs: list[tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


P_BUCKETS = (("0.05–0.35", 0.0, 0.375), ("0.40–0.60", 0.375, 0.625), ("0.65–0.95", 0.625, 1.01))


def score_report(items: list[Item]) -> list[str]:
    scored = [it for it in items if it.kind == "J" and it.status == "已判"
              and it.outcome in ("发生", "未发生") and it.p is not None]
    n = len(scored)
    if n < SCORE_MIN_N:
        return [f"样本不足 n={n}"]
    pairs = [(it.p, 1 if it.outcome == "发生" else 0) for it in scored]
    b = brier(pairs)
    voided = sum(1 for it in items if it.kind == "J" and it.status == "作废")
    out = [f"复核记分 · 已判定且有 p 的卡 n={n}（另有作废 {voided} 张，不计分）[演练]",
           f"Brier = {b:.4f}；常数 0.5 基准 = 0.2500；差 = {b - 0.25:+.4f}（越低越好）",
           "按岗位："]
    roles = sorted({it.role for it in scored})
    for role in roles:
        sub = [(it.p, 1 if it.outcome == "发生" else 0) for it in scored if it.role == role]
        k = sum(y for _, y in sub)
        out.append(f"  {role}：n={len(sub)}，Brier={brier(sub):.4f}，发生 {k}/{len(sub)}")
    out.append("按 p 分桶（Wilson 95% 区间）：")
    for label, lo, hi in P_BUCKETS:
        sub = [(p, y) for p, y in pairs if lo <= p < hi]
        if not sub:
            out.append(f"  {label}：n=0")
            continue
        k = sum(y for _, y in sub)
        lo_ci, hi_ci = wilson(k, len(sub))
        mean_p = sum(p for p, _ in sub) / len(sub)
        out.append(f"  {label}：n={len(sub)}，平均 p={mean_p:.2f}，实际发生 {k}/{len(sub)}={k / len(sub):.2f}，"
                   f"95% 区间 [{lo_ci:.2f}, {hi_ci:.2f}]")
    return out


def cmd_score(args) -> int:
    parsed = load_decisions(args.root)
    if parsed is None:
        print(f"找不到 {DECISIONS}。")
        return EXIT_MISSING
    for line in score_report(parsed["items"]):
        print(line)
    return EXIT_OK


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

SECTION_RES = {
    1: re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*1\s*[.．、]\s*(?:\*\*)?\s*事实"),
    2: re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*2\s*[.．、]\s*(?:\*\*)?\s*判断"),
    3: re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*3\s*[.．、]\s*(?:\*\*)?\s*我可能错在哪里?"),
    4: re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*4\s*[.．、]\s*(?:\*\*)?\s*请求"),
}
SECTION_NAMES = {1: "1. 事实", 2: "2. 判断", 3: "3. 我可能错在哪里", 4: "4. 请求裁决的具体事项"}
EMPTY_SECTION = {"", "无", "暂无", "没有", "无可能", "不适用"}
MEMO_HEADER_RE = re.compile(r"^备忘录\s*·\s*\d{4}-\d{2}-\d{2}\s*·\s*\S+\s*·\s*\S")
SOURCE_RE = re.compile(r"[（(]\s*来源\s*[：:]")
TAG_RE = re.compile(r"[\[［【]\s*(?:演练|记忆·未核)")
DOLLAR_NUM_RE = re.compile(r"\$\s?\d")
REVIEW_FIELD_RE = re.compile(r"(?<!到期)复核(?:日期|日|点)?\s*(?:\*\*)?\s*(?:[:：]|定为|设为|改为)")
DATEISH_RE = re.compile(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*日")
ISO_LOOSE_RE = re.compile(r"(?<!\d)(\d{4}-\d{1,2}-\d{1,2})(?!\d)")
CN_NUM = "零〇一二两三四五六七八九十百"
ARTICLE_RE = re.compile(
    r"第\s*((?:[0-9０-９]+|[" + CN_NUM + r"]+)(?:\s*[-–—~～至到、,，和及]\s*(?:[0-9０-９]+|[" + CN_NUM + r"]+))*)\s*条")


def cn_to_int(s: str) -> int | None:
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789")).strip()
    if s.isdigit():
        return int(s)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    total, cur = 0, 0
    for ch in s:
        if ch in digits:
            cur = digits[ch]
        elif ch == "十":
            total += (cur or 1) * 10
            cur = 0
        elif ch == "百":
            total += (cur or 1) * 100
            cur = 0
        else:
            return None
    return total + cur


def article_numbers(line: str) -> list[int]:
    out = []
    for m in ARTICLE_RE.finditer(line):
        for part in re.split(r"\s*[-–—~～至到、,，和及]\s*", m.group(1)):
            v = cn_to_int(part)
            if v is not None:
                out.append(v)
    return out


class Lint:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, where: str, msg: str):
        self.errors.append(f"{where}：{msg}")

    def warn(self, where: str, msg: str):
        self.warnings.append(f"{where}：{msg}")


def _check_review_dates_in_line(lint: Lint, where: str, line: str, require: bool):
    """检查一行里的复核日期：能解析、是工作日。require=True 时这一行必须至少有一个日期。"""
    found = False
    for m in ISO_LOOSE_RE.finditer(line):
        raw = m.group(1)
        d = try_date(raw)
        if d is None:
            lint.err(where, f"复核日期 {raw} 解析不了，要写成有效的 YYYY-MM-DD")
            continue
        found = True
        if not is_workday(d):
            lint.err(where, f"复核日期 {raw} 是{weekday_cn(d)}，要落在工作日（可改为 {roll_to_workday(d)}）")
    if not found and (require or DATEISH_RE.search(line)):
        lint.err(where, "复核日期解析不了，要写成 YYYY-MM-DD")


def _check_card(lint: Lint, where: str, line: str, today: dt.date, expect_ymd: str | None) -> dict | None:
    cm = CARD_RE.match(line)
    if not cm:
        lint.err(where, "判断卡格式不对，应为：- J-YYYYMMDD-n｜命题：…｜判定：…｜截止：YYYY-MM-DD｜p=0.xx")
        return None
    if "|" in line:
        lint.err(where, "判断卡的分隔符要用全角竖线“｜”")
    if any(cm.group(c) == ":" for c in ("c1", "c2", "c3")):
        lint.err(where, "判断卡的字段名后要用全角冒号“：”")
    if cm.group("tail").strip(" 。.;；"):
        lint.err(where, "p=0.xx 后面不要再写字（解释写进条目正文）")
    ymd = cm.group("ymd")
    try:
        dt.datetime.strptime(ymd, "%Y%m%d")
    except ValueError:
        lint.err(where, f"卡号里的日期 {ymd} 不是有效日期")
    if expect_ymd and ymd != expect_ymd:
        lint.err(where, f"卡号日期 {ymd} 应为当天模拟日 {expect_ymd}")
    due = try_date(cm.group("due").strip())
    if due is None:
        lint.err(where, f"截止 {cm.group('due').strip()!r} 解析不了，要写成 YYYY-MM-DD")
    else:
        if not is_workday(due):
            lint.err(where, f"截止 {due} 是{weekday_cn(due)}，要落在工作日（可改为 {roll_to_workday(due)}）")
        if due <= today:
            lint.err(where, f"截止 {due} 不晚于今天 {today}")
    try:
        p = float(cm.group("p"))
    except ValueError:
        p = None
    if p is None or not (0.05 - 1e-9 <= p <= 0.95 + 1e-9) or abs(p * 20 - round(p * 20)) > 1e-6:
        lint.err(where, f"p={cm.group('p')} 不合规：只能取 0.05–0.95，步长 0.05")
    return {"id": cm.group("id"), "seq": cm.group("seq"), "due": due}


def _section_spans(lines: list[str]) -> list[tuple[int, int]]:
    """返回 (编号, 行下标) 列表：备忘录里 1–4 节标题出现的位置。"""
    spans = []
    for i, line in enumerate(lines):
        for n, rx in SECTION_RES.items():
            if rx.match(line):
                spans.append((n, i))
                break
    return spans


def _section_body(lines: list[str], start: int, stop_rx) -> str:
    head = lines[start]
    m = SECTION_RES[3].match(head)
    first = head[m.end():] if m else ""
    first = re.sub(r"[（(]\s*必填[^）)]*[）)]", "", first)
    body = [first]
    for line in lines[start + 1:]:
        if stop_rx(line):
            break
        body.append(line)
    return "\n".join(body)


def _is_empty_text(s: str) -> bool:
    core = re.sub(r"[\s*_>\-—:：。.,，、|｜`]", "", s)
    return core in EMPTY_SECTION


def lint_memo(lint: Lint, path: Path, rel: str, today: dt.date):
    text = read_text(path) or ""
    lines = text.rstrip("\n").split("\n") if text.strip() else []
    name = path.name
    is_brief = name.endswith("-brief.md")
    is_close = name.endswith("-close.md")
    first = next((l.strip() for l in lines if l.strip()), "")
    is_skeptic = name.endswith("-skeptic.md") or first.startswith("证伪报告")

    if not (is_brief or is_close) and len(lines) > MEMO_MAX_LINES:
        lint.err(rel, f"{len(lines)} 行，超过一页（{MEMO_MAX_LINES} 行）")

    # 必备各节
    if is_skeptic:
        idx = next((i for i, l in enumerate(lines) if "【如果我错了】" in l), None)
        if idx is None:
            lint.err(rel, "证伪报告缺【如果我错了】")
        else:
            body = lines[idx].split("【如果我错了】", 1)[1]
            rest = [l for l in lines[idx + 1:]]
            joined = body + "\n" + "\n".join(rest)
            if _is_empty_text(joined):
                lint.err(rel, "【如果我错了】不许为空或写“无”")
            elif not re.search(r"p\s*=\s*[0-9.]+", joined):
                lint.warn(rel, "【如果我错了】里没写“本结论被推翻的概率 p=0.xx”")
    elif not (is_brief or is_close):
        if first and not MEMO_HEADER_RE.match(first):
            lint.warn(rel, "首行不是“备忘录 · YYYY-MM-DD · 发件人 · 一句话主题”")
        spans = _section_spans(lines)
        present = {n for n, _ in spans}
        for n in (1, 2, 3, 4):
            if n not in present:
                lint.err(rel, f"缺第五节规定的“{SECTION_NAMES[n]}”")

        def stop(line):
            return any(rx.match(line) for rx in SECTION_RES.values()) or line.strip() == "---" \
                or line.startswith("## ")

        for n, i in spans:
            if n == 3 and _is_empty_text(_section_body(lines, i, stop)):
                lint.err(f"{rel}:{i + 1}", "“3. 我可能错在哪里”不许为空或写“无”")

    # 逐行规则
    in_fence = False
    for i, line in enumerate(lines, start=1):
        where = f"{rel}:{i}"
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue  # 代码块里是引用的模板或原文，不按正文检查
        if "deal-screen" in name and "✅" in line:
            n_ok, n_src = line.count("✅"), len(SOURCE_RE.findall(line))
            if n_src < n_ok:
                lint.err(where, f"这一行有 {n_ok} 个 ✅，只有 {n_src} 处“（来源：”；每个 ✅ 同一行要注明来源")
        if not is_brief and REVIEW_FIELD_RE.search(line):
            _check_review_dates_in_line(lint, where, line, require=False)
        for v in article_numbers(line):
            if v > 10:
                lint.err(where, f"“第 {v} 条”超出宪法的 10 条（是不是把行号当成了条文？）")
        for w in BANNED_WORDS:
            if w in line:
                lint.warn(where, f"用了禁用词“{w}”（CLAUDE.md 第六节）")
        if CARD_START_RE.match(line) and not (is_brief or is_close):
            # brief/close 里的 J- 行是原样贴进来的 due 输出，不是新写的卡
            _check_card(lint, where, line, today, None)

    # 段落规则：出现 $数字 的段落要有 [演练] 或 [记忆·未核]
    para, start = [], 1
    for i, line in enumerate(lines + [""], start=1):
        if line.strip():
            if not para:
                start = i
            para.append(line)
            continue
        if para:
            block = "\n".join(para)
            if DOLLAR_NUM_RE.search(block) and not TAG_RE.search(block):
                lint.err(f"{rel}:{start}", "这一段出现了 $ 数字，但没有 [演练] 或 [记忆·未核]")
            para = []


def lint_decisions(lint: Lint, root: Path, today: dt.date):
    text = read_text(root / DECISIONS)
    if text is None:
        lint.warn(str(DECISIONS), "找不到决策日志，跳过")
        return
    lines = text.split("\n")
    parsed = parse_decisions(text)
    rel = str(DECISIONS)
    ymd = today.strftime("%Y%m%d")

    # 当日区块：从第一个“## <today> ·”标题到下一个其它日期的“##”标题
    start = end = None
    for i, line in enumerate(lines):
        m = ENTRY_RE.match(line)
        if m and try_date(m.group(1)) == today and start is None:
            start = i
        elif start is not None and line.startswith("## ") and not (m and try_date(m.group(1)) == today):
            end = i
            break
    if start is not None:
        end = end if end is not None else len(lines)
        in_fence = False
        for i in range(start, end):
            line = lines[i]
            where = f"{rel}:{i + 1}"
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if CARD_START_RE.match(line):
                _check_card(lint, where, line, today, ymd)
                continue
            if REVIEW_ID_RE.match(line) or REVIEW_OLD_RE.match(line):
                continue
            if FIELD_REVIEW_RE.match(line):
                _check_review_dates_in_line(lint, where, line, require=True)
            elif REVIEW_FIELD_RE.search(line):
                _check_review_dates_in_line(lint, where, line, require=False)
            for v in article_numbers(line):
                if v > 10:
                    lint.err(where, f"“第 {v} 条”超出宪法的 10 条")
            for w in BANNED_WORDS:
                if w in line:
                    lint.warn(where, f"用了禁用词“{w}”（CLAUDE.md 第六节）")
        todays = [e for e in parsed["entries"] if e.date == today]
        seen: dict[str, int] = {}
        for e in todays:
            for c in e.cards:
                if c["id"] in seen:
                    lint.err(f"{rel}:{c['line']}", f"卡号 {c['id']} 与第 {seen[c['id']]} 行重复（序号在当天所有条目里连续编）")
                else:
                    seen[c["id"]] = c["line"]
            if not e.cards:
                continue
            numbered = [c for c in e.cards if c["seq"] != "S"]
            if len(numbered) > 3:
                lint.warn(f"{rel}:{e.line}", f"这一条目有 {len(numbered)} 张董事长卡，规定 1–3 张")
            if not any(c["due"] <= today + dt.timedelta(days=90) for c in e.cards):
                lint.warn(f"{rel}:{e.line}", "这一条目没有一张卡的截止在 90 天以内")
            if e.field_lines:
                lint.warn(f"{rel}:{e.field_lines[0][0]}", "条目已有判断卡，“**复核日期**”行不再登记（只按卡追踪）")

    # 写错格式的复核记录（比如卡号前漏了“ · ”）index 不会登记，卡会一直挂着：按行找出来
    in_fence = False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = REVIEW_LOOSE_RE.match(line)
        if not m or try_date(m.group(1)) != today:
            continue
        if not (REVIEW_ID_RE.match(line) or REVIEW_OLD_RE.match(line)):
            lint.err(f"{rel}:{i + 1}", "复核记录格式不对，index 不会登记：判断卡写“- 复核 YYYY-MM-DD · J-…：发生/未发生/作废（原因码）”，"
                                       "旧条目写“> 复核 YYYY-MM-DD：对/错/顺延至 YYYY-MM-DD（原因码）。…”")

    # 当天写下的复核记录（可能在旧条目下方）
    by_id = parsed["by_id"]
    for rec in parsed["id_reviews"] + parsed["old_reviews"]:
        if rec["date"] != today:
            continue
        where = f"{rel}:{rec['line']}"
        o = rec["o"]
        it = rec.get("item") or by_id.get(rec.get("id", ""))
        if o["kind"] is None:
            lint.err(where, "复核结局读不出来：判断卡写 发生/未发生/作废/顺延至 YYYY-MM-DD，旧条目写 对/错/作废/顺延至 YYYY-MM-DD")
            continue
        if "id" in rec and rec["id"] not in by_id:
            lint.err(where, f"复核记录引用了不存在的编号 {rec['id']}")
            continue
        if it is not None and it.kind == "J" and o["kind"] in ("对", "错"):
            lint.err(where, "判断卡的结局用 发生/未发生/作废，不用 对/错")
        if o["kind"] in ("作废", "顺延") and not o["code"]:
            lint.err(where, "作废和顺延必须带原因码（无来件/证据不足/运行断档/条件变更），写在括号里")
        if o["kind"] == "顺延":
            if o["bad_date"]:
                lint.err(where, f"顺延日期 {o['bad_date']} 解析不了")
            elif o["new_date"] is None:
                lint.err(where, "顺延要写新日期：顺延至 YYYY-MM-DD")
            elif not is_workday(o["new_date"]):
                lint.err(where, f"顺延日期 {o['new_date']} 是{weekday_cn(o['new_date'])}，要落在工作日")
            if rec.get("postpone_n", 0) >= 2:
                lint.err(where, f"{it.id} 已顺延过一次，顺延只许一次：这次要写作废（原因码）或判出结果")
        if rec.get("skipped"):
            lint.err(where, f"{it.id} 此前已经{rec['skipped']}，这条复核记录不会计入")
        if "id" not in rec and it is not None and it.kind == "J":
            lint.err(where, f"判断卡的复核要写卡号：- 复核 {today} · {it.id}：发生/未发生/作废（原因码）")


def lint_calendar(lint: Lint, root: Path, today: dt.date):
    """当天要在 calendar.md 的表格里有一行，收盘时「调用岗位」要填上。没有 calendar.md 的仓库不查。"""
    if read_text(root / CALENDAR) is None:
        return
    rows = [r for r in calendar_rows(root) if r["_date"] == today]
    if not rows:
        lint.err(str(CALENDAR), f"表格里没有 {today} 这一行：第 0 步要在表格末尾登记当天（不要写到「断档记录」下面）")
    elif not any(r.get("调用岗位", "").strip() for r in rows):
        lint.err(f"{CALENDAR}:{rows[0]['_line']}", f"{today} 这一行的「调用岗位」还空着：收盘时按实际调用填上，用顿号分隔，没调用写“无”")


def cmd_lint(args) -> int:
    root, d = args.root, args.date
    lint = Lint()
    memo_dir = root / MEMOS_DIR
    memos = sorted(p for p in memo_dir.glob(f"{d.isoformat()}-*.md") if not p.name.endswith("-lint.md")) \
        if memo_dir.is_dir() else []
    if not memos:
        lint.warn(str(MEMOS_DIR), f"没有 {d} 的备忘录")
    for p in memos:
        lint_memo(lint, p, f"{MEMOS_DIR}/{p.name}", d)
    lint_decisions(lint, root, d)
    lint_calendar(lint, root, d)
    print(f"lint · {d} · 检查了 {len(memos)} 份备忘录、当日决策条目与 calendar.md 当天一行")
    for e in lint.errors:
        print(f"错误 {e}")
    for w in lint.warnings:
        print(f"警告 {w}")
    if lint.errors:
        print(f"未通过：{len(lint.errors)} 处错误，{len(lint.warnings)} 处警告。只修格式，不改判断；修完重跑一次。")
        return EXIT_LINT_FAIL
    print(f"通过（{len(lint.warnings)} 处警告）。")
    return EXIT_OK


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def _date_arg(s: str) -> dt.date:
    try:
        return parse_date(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=argparse.SUPPRESS,
                        help="仓库根目录（默认为本脚本上一级目录）")
    parser = argparse.ArgumentParser(
        prog="office_tool.py",
        description="总部模拟的账本小工具 [演练]。只用标准库；除 index 写 reviews.csv 外，其余子命令只输出、不改文件。"
                    "工作日 = 周一到周五，不考虑节假日。",
        epilog="例：python3 tools/office_tool.py gap 2026-09-28",
        parents=[common])
    sub = parser.add_subparsers(dest="cmd", metavar="<子命令>")

    p = sub.add_parser("gap", parents=[common], help="距上次开门几个工作日",
                       description="上次开门日 = office/memos/*-brief.md 文件名里最大的、严格早于 <date> 的日期。"
                                   "输出“距上次开门 N 个工作日”，N 为 [上次开门日, date) 里的工作日数"
                                   "（例：周五开过门、周一再开 = 1）；另列出中间没开门的工作日，供登记 calendar.md 的断档记录。"
                                   "N 大于 1 说明有断档。",
                       epilog="例：gap 2026-09-28 → 距上次开门 1 个工作日（上次开门 2026-09-25，周五）")
    p.add_argument("date", type=_date_arg, help="模拟日 YYYY-MM-DD")
    p.add_argument("--number", action="store_true", help="只输出数字 N")
    p.set_defaults(func=cmd_gap)

    p = sub.add_parser("draw", parents=[common], help="对到期的未结事项抽签，给出回件结局",
                       description="读 office/ledger/open-items.md。对状态为 待回/迟到、且期限经 roll_to_workday 后"
                                   "不晚于 <date> 的事项（期限为“无”的事项每次都参与），逐条输出 编号、u、结局。"
                                   "u = int(sha256(\"<date>|<编号>\"), 16) % 20；有期限：0–10 按时，11–14 迟到，"
                                   "15–17 沉默，18–19 坏消息；期限为“无”：0–4 按时，5–14 迟到，15–17 沉默，18–19 坏消息。"
                                   "另列出沉默满 30 个工作日、应自动关闭的事项。只输出，不改文件。",
                       epilog="例：draw 2026-10-08")
    p.add_argument("date", type=_date_arg, help="模拟日 YYYY-MM-DD")
    p.set_defaults(func=cmd_draw)

    p = sub.add_parser("cards", parents=[common], help="按种子选 2–3 张来件原型牌",
                       description="读 office/generator/archetypes.md。以 sha256(<date>) 为种子决定张数（2 或 3）"
                                   "和排序，只选最近 10 个模拟日（取自 calendar.md 的模拟日列）内没用过的牌；"
                                   "可选的牌不够时按最久未用补足并提示补新牌。伪授权/提示注入类牌每 ISO 周最多 1 次。"
                                   "“从未上场的岗位”= treasury-desk/cfo-controller/general-counsel/shareholder-scribe 中"
                                   "本 ISO 周之前的 calendar 调用岗位里一次都没出现过的；若本周 calendar 的调用岗位里还没有它们，"
                                   "且选中的牌也没有派给它们的，强制把最后一张换成派给它们的牌。"
                                   "只输出，不改文件；写完来件后由主会话更新“上次使用”。牌库只给主会话看。",
                       epilog="例：cards 2026-09-28")
    p.add_argument("date", type=_date_arg, help="模拟日 YYYY-MM-DD")
    p.set_defaults(func=cmd_cards)

    p = sub.add_parser("index", parents=[common], help="解析 decisions.md，重新生成 reviews.csv",
                       description="解析 office/ledger/decisions.md，重写 office/ledger/reviews.csv"
                                   "（列：" + ",".join(CSV_COLUMNS) + "）。"
                                   "判断卡按“- J-YYYYMMDD-n｜命题：…｜判定：…｜截止：YYYY-MM-DD｜p=0.xx”逐行登记；"
                                   "复核记录“- 复核 YYYY-MM-DD · J-…：发生/未发生/作废（原因码）”或“顺延至 YYYY-MM-DD（原因码）”。"
                                   "没有判断卡的旧条目，其“**复核日期**”行里括号外的每个日期登记为 L-<条目日期YYYYMMDD>-n"
                                   "（n 在同一条目日期内按出现顺序连续编），p 留空，只追踪逾期、不计分；"
                                   "“> 复核 YYYY-MM-DD：顺延至 …”记到该条目里当时已到期、最早的那个日期上。"
                                   "顺延只许一次，第二次自动作废。reviews.csv 是生成物，不要手写。",
                       epilog="例：index")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("due", parents=[common], help="列出到期、逾期和即将到期的复核",
                       description="直接解析 decisions.md（不依赖 reviews.csv）。截止日先经 roll_to_workday 顺延到工作日。"
                                   "分三栏：【按开门日已逾期】截止后办公室开过门却没打分，算欠账，给出按日历逾期天数与"
                                   "按开门日逾期次数（开门日取自 calendar.md 的模拟日列）；【按日历已到期】今天到期、或截止后一直没开门，"
                                   "不算欠账；【未来 N 天将到期】。",
                       epilog="例：due 2026-09-25；due 2026-10-01 --ahead 31")
    p.add_argument("date", type=_date_arg, help="模拟日 YYYY-MM-DD")
    p.add_argument("--ahead", type=int, default=DUE_AHEAD_DAYS, help=f"列出未来多少天内将到期的（默认 {DUE_AHEAD_DAYS}）")
    p.set_defaults(func=cmd_due)

    p = sub.add_parser("lint", parents=[common], help="提交前检查当日备忘录与决策条目格式",
                       description="检查 office/memos/<date>-*.md（不含 -lint.md）、decisions.md 里当天的条目和复核记录、"
                                   "calendar.md 当天一行。"
                                   "错误：备忘录超过 60 行（brief、close 除外）；缺 CLAUDE.md 第五节的 1–4 节，或第 3 节为空/写“无”"
                                   "（证伪报告查【如果我错了】）；deal-screen 里 ✅ 所在行没有等量的“（来源：”；"
                                   "复核日期/截止/顺延日期解析不了或落在周末；“第 N 条”的 N 大于 10；出现 $ 数字的段落没有"
                                   " [演练] 或 [记忆·未核]；判断卡格式、卡号日期、p 的范围与步长；复核记录格式不对、缺原因码、二次顺延；"
                                   "calendar.md 表格里没有当天一行，或当天「调用岗位」为空。"
                                   "警告：禁用词、卡数与 90 天内的卡。有错误时退出码为 1。只输出，不改文件。",
                       epilog="例：lint 2026-09-25")
    p.add_argument("date", type=_date_arg, help="模拟日 YYYY-MM-DD")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("score", parents=[common], help="判断卡记分（Brier）",
                       description=f"只统计已判定（发生/未发生）且有 p 的 J 卡。少于 {SCORE_MIN_N} 张时只输出“样本不足 n=…”；"
                                   "达到后输出 Brier 分、常数 0.5 基准（0.25）、按岗位的计数与 Brier、按 p 分桶"
                                   "（0.05–0.35 / 0.40–0.60 / 0.65–0.95）的计数与 Wilson 95% 区间。作废的卡不计分。",
                       epilog="例：score")
    p.set_defaults(func=cmd_score)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return EXIT_OK
    args.root = Path(getattr(args, "root", DEFAULT_ROOT)).resolve()
    return args.func(args)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
