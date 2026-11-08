#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""教务系统原始接口 JSON -> 统一课表 JSON（规范化 + 硬校验 + 异常标注）。

输入：教务系统日历接口的原始响应，两种形态都接受：
      1) {"status":200,"url":"...","data":[ ...原始记录... ]}
      2) 纯数组 [ ...原始记录... ]

记录按 `lx` 分类：
      kb = 上课（同时作为 event，type="上课"）
      ks = 考试（同时作为 event，type="考试"）
      xk = 选课时间段（原样放进 selections，不去空格）

输出（与参考实现 normalized_schedule.json 同构）：
      fetchedAt / source / semester / timeZone / events / selections / overlaps

用法：
      python jw_normalize.py --raw calendar_raw.json --out normalized_schedule.json
      python jw_normalize.py --raw calendar_raw.json --out out.json \
             --semester "2026–2027 学年第一学期" --source "https://jw.example.edu.cn/new/welcome.page?ui=new"

硬校验（可用 --no-checks 关闭；失败退出码 2，只报错、不改数据、不写输出）：
      1) 每条 event 的日期星期几必须与接口 xq 一致；
      2) 同课程同星期必须满足 date-(week-1)*7 恒定（据此推断第 1 周起点）；
      3) 每条 event 必须 end > start。

铁律：所有源数据异常（时间重叠、缺场地、内容时段与排课不一致）只写进 notes 标注，
      不静默修改任何字段。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import locale as _locale
import os
import re
import sys
from functools import cmp_to_key

VERSION = "1.0.0"

TIME_ZONE = "Asia/Shanghai"
# 教务系统入口页（source 字段默认值；仅记录数据来源，不影响数据本身）
DEFAULT_SOURCE = "https://jw.example.edu.cn/new/welcome.page?ui=new"

# 同一院校的字段映射：换学校只改这里 + 上面的默认常量。
# key = 规范化字段名，value = 教务接口原始字段名。
FIELD_MAP = {
    "course": "kcmc",                   # 课程名称
    "date": "qsrq",                     # 开始日期
    "content": "sknrjj",                # 授课内容
    "teacher": "teaxms",                # 授课/监考教师
    "campus": "xqmc",                   # 校区
    "location": "jxcdmc",               # 教学场地
    "phase": "jxhjmc",                  # 教学环节
    "form": "jxxsmc",                   # 教学形式
    "teachingClass": "jxbmc",           # 教学班
    "assessment": "khfsmc",             # 考核方式
    "courseCode": "kcbh",               # 课程编号
    "platformCourseCode": "kcptbh",     # 平台课程编号
    "sequence": "kxh",                  # 课序号
    "scheduledStudents": "pkrs",        # 排课人数
    "enrolledStudents": "jxbrs",        # 上课人数（考试记录可能为 ""）
    "sourceRemark": "bz",               # 源备注
}
RAW_TIME_START = "qssj"                 # 开始钟点（可能带前导空格，可能缺秒）
RAW_TIME_END = "jssj"                   # 结束钟点
RAW_DATE_END = "jsrq"                   # 结束日期
RAW_TYPE = "lx"                         # 记录类型 kb/ks/xk
RAW_ID = "dm"                           # 源记录 ID
RAW_WEEK = "zc"                         # 教学周
RAW_WEEKDAY = "xq"                      # 星期 1..7
RAW_PERIODS = "jcdm"                    # 节次编码，每两位一节
RAW_SEAT = "zwh"                        # 座位号

# 归一化 event 的键顺序（与参考文件逐字一致，供自测断言）
EVENT_KEY_ORDER = (
    "sourceId", "type", "title", "course", "date", "start", "end", "week",
    "weekday", "periods", "content", "teacher", "campus", "location", "phase",
    "form", "teachingClass", "assessment", "courseCode", "platformCourseCode",
    "sequence", "scheduledStudents", "enrolledStudents", "seat",
    "sourceRemark", "notes", "semester", "timeZone",
)

WEEKDAY_BY_XQ = {
    1: "星期一", 2: "星期二", 3: "星期三", 4: "星期四",
    5: "星期五", 6: "星期六", 7: "星期日",
}
ISO_WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

NOTE_LOCATION_MISSING = "网页未提供教学场地；官方导出标为“无”。"
NOTE_OVERLAP = "时间重叠：{name} {start}–{end}（源记录 {dm}）。"
NOTE_PARALLEL = "同课同时段另有排课：{location}／{teacher}（源记录 {dm}）；保留两条。"
NOTE_CONTENT_TIME = "授课内容注明 {noted}，排课时间为 {scheduled}；源时间不一致，均保留。"
NOTE_SEPARATOR = "\n"                   # 多条提示用换行分隔（与参考实现一致）

CONTENT_TIME_RE = re.compile(
    r"上课时间\s*(\d{1,2})[：:](\d{2})\s*[-–—]\s*(\d{1,2})[：:](\d{2})"
)
RAW_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


# --------------------------------------------------------------------------
# 文本 / 时间 / 节次
# --------------------------------------------------------------------------
def _s(value):
    """任意值转字符串；None -> ""。"""
    return "" if value is None else str(value)


def normalize_time(value):
    """去前导空格并补成 HH:MM:SS（缺秒补 00，小时补前导 0）。"""
    text = _s(value).strip()
    if not text:
        return ""
    match = RAW_TIME_RE.match(text)
    if not match:
        return text  # 保留原文，交给校验去暴露
    hour, minute, second = match.group(1), match.group(2), match.group(3) or "00"
    return "%02d:%s:%s" % (int(hour), minute, second)


def event_start(record):
    """event.start = "<qsrq> <qssj HH:MM:SS>"。"""
    return "%s %s" % (_s(record.get(FIELD_MAP["date"])).strip(),
                      normalize_time(record.get(RAW_TIME_START)))


def event_end(record):
    """event.end = "<jsrq> <jssj HH:MM:SS>"。"""
    return "%s %s" % (_s(record.get(RAW_DATE_END)).strip(),
                      normalize_time(record.get(RAW_TIME_END)))


def _hhmm(value):
    """取 HH:MM 用于 notes 文案。"""
    return normalize_time(value)[:5]


def period_text(jcdm):
    """节次编码 -> "1、2、3"：每两位一节，去掉前导 0。"""
    text = _s(jcdm).strip()
    if not text:
        return ""
    parts = []
    for idx in range(0, len(text), 2):
        chunk = text[idx:idx + 2]
        try:
            parts.append(str(int(chunk)))
        except ValueError:
            parts.append(chunk)
    return "、".join(parts)


# --------------------------------------------------------------------------
# 中文字符串排序（对齐参考实现 JS 的 localeCompare(..., "zh-CN")）
# --------------------------------------------------------------------------
_COLLATOR = None


def _collator():
    global _COLLATOR
    if _COLLATOR is None:
        compare = None
        for name in ("zh_CN", "zh_CN.UTF-8", "Chinese_China.936", "zh-CN", "Chinese"):
            try:
                _locale.setlocale(_locale.LC_COLLATE, name)
                compare = _locale.strcoll
                break
            except (getattr(_locale, "Error", RuntimeError), ValueError):
                continue
        if compare is None:
            def compare(left, right):
                return (left > right) - (left < right)
        _COLLATOR = compare
    return _COLLATOR


def _compare_text(left, right):
    try:
        return _collator()(left, right)
    except Exception:                                   # 极端字符兜底
        return (left > right) - (left < right)


def _compare_records(left, right):
    """与参考实现 sort 一致：开始时间、结束时间、课程名（中文排序）、源记录 ID。"""
    key_left, key_right = event_start(left), event_start(right)
    if key_left != key_right:
        return -1 if key_left < key_right else 1
    key_left, key_right = event_end(left), event_end(right)
    if key_left != key_right:
        return -1 if key_left < key_right else 1
    order = _compare_text(_s(left.get(FIELD_MAP["course"])), _s(right.get(FIELD_MAP["course"])))
    if order:
        return -1 if order < 0 else 1
    left_id, right_id = _s(left.get(RAW_ID)), _s(right.get(RAW_ID))
    if left_id != right_id:
        return -1 if left_id < right_id else 1
    return 0


def sort_records(records):
    return sorted(records, key=cmp_to_key(_compare_records))


# --------------------------------------------------------------------------
# 日期 / 学期推断
# --------------------------------------------------------------------------
def parse_date(text):
    try:
        return _dt.date.fromisoformat(_s(text).strip())
    except ValueError:
        return None


def _beijing_today():
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=8)).date()


def infer_semester(date_strings):
    """按最早排课日期推断学期文案：<学年起>–<学年止> 学年第一/二学期。

    9-1 月属于当年 8 月起始的学年第一学期；2-7 月属于上一年起始学年的第二学期。
    推断不出来时返回 ""，由 --semester 显式指定。
    """
    dates = sorted(d for d in (parse_date(s) for s in date_strings) if d)
    if not dates:
        return ""
    earliest = dates[0]
    year = earliest.year
    if earliest.month >= 8:                 # 8-12 月：当年第一学期
        start_year, term = year, "第一学期"
    elif earliest.month == 1:               # 1 月仍属上一年秋季学期
        start_year, term = year - 1, "第一学期"
    else:                                   # 2-7 月：上一年第二学期
        start_year, term = year - 1, "第二学期"
    return "%d–%d 学年%s" % (start_year, start_year + 1, term)


# --------------------------------------------------------------------------
# 加载原始数据
# --------------------------------------------------------------------------
def load_raw(path):
    """返回 (records, meta)。meta 里保留顶层 url/status。"""
    with open(path, "r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        records, meta = payload, {}
    elif isinstance(payload, dict):
        records = payload.get("data")
        if isinstance(records, dict):              # {"data":{"data":[...]}} 兼容
            records = records.get("data")
        if not isinstance(records, list):
            raise ValueError("原始 JSON 缺少 data 数组：%s" % path)
        meta = {k: payload.get(k) for k in ("status", "url") if k in payload}
    else:
        raise ValueError("原始 JSON 顶层必须是对象或数组：%s" % path)
    return records, meta


# --------------------------------------------------------------------------
# 事件提示（notes）与重叠检测
# --------------------------------------------------------------------------
def _overlap_name(record):
    """时间重叠文案里对方课程的称呼：考试记录加 "考试 " 前缀。"""
    prefix = "考试 " if _s(record.get(RAW_TYPE)) == "ks" else ""
    return prefix + _s(record.get(FIELD_MAP["course"]))


def build_overlaps_and_notes(events, classes):
    """返回 (overlaps, notes_map)。events 已按参考顺序排好。

    - overlaps：[{a,b,date,parallel}]，a/b 用原始 dm（不带前缀），按日期升序；
      parallel=True 表示同类型、同课程、同起止（同课同时段平行排课）。
    - notes_map：sourceId -> 提示列表，多条用换行分隔；同一事件内相同文案去重。
    """
    notes_map = {}

    def warn(record, text):
        bucket = notes_map.setdefault("%s-%s" % (_s(record.get(RAW_TYPE)), _s(record.get(RAW_ID))), [])
        if text not in bucket:
            bucket.append(text)

    overlaps = []
    for index, left in enumerate(events):
        left_start, left_end = event_start(left), event_end(left)
        for right in events[index + 1:]:
            if right[FIELD_MAP["date"]] != left[FIELD_MAP["date"]]:
                # 事件已按开始时间升序，后续记录日期只会更晚
                if _s(right.get(FIELD_MAP["date"])) > _s(left.get(FIELD_MAP["date"])):
                    break
                continue
            right_start, right_end = event_start(right), event_end(right)
            if not (left_start < right_end and right_start < left_end):
                continue
            parallel = (
                left.get(RAW_TYPE) == right.get(RAW_TYPE)
                and left.get(FIELD_MAP["course"]) == right.get(FIELD_MAP["course"])
                and left_start == right_start
                and left_end == right_end
            )
            overlaps.append({
                "a": _s(left.get(RAW_ID)),
                "b": _s(right.get(RAW_ID)),
                "date": left.get(FIELD_MAP["date"]),
                "parallel": parallel,
            })
            if parallel:
                warn(left, NOTE_PARALLEL.format(
                    location=_s(right.get(FIELD_MAP["location"])).strip() or "地点未提供",
                    teacher=_s(right.get(FIELD_MAP["teacher"])).strip() or "教师未提供",
                    dm=_s(right.get(RAW_ID))))
                warn(right, NOTE_PARALLEL.format(
                    location=_s(left.get(FIELD_MAP["location"])).strip() or "地点未提供",
                    teacher=_s(left.get(FIELD_MAP["teacher"])).strip() or "教师未提供",
                    dm=_s(left.get(RAW_ID))))
            else:
                warn(left, NOTE_OVERLAP.format(
                    name=_overlap_name(right),
                    start=_hhmm(right.get(RAW_TIME_START)),
                    end=_hhmm(right.get(RAW_TIME_END)),
                    dm=_s(right.get(RAW_ID))))
                warn(right, NOTE_OVERLAP.format(
                    name=_overlap_name(left),
                    start=_hhmm(left.get(RAW_TIME_START)),
                    end=_hhmm(left.get(RAW_TIME_END)),
                    dm=_s(left.get(RAW_ID))))

    for record in classes:
        if not _s(record.get(FIELD_MAP["location"])).strip():
            warn(record, NOTE_LOCATION_MISSING)
        match = CONTENT_TIME_RE.search(_s(record.get(FIELD_MAP["content"])))
        if match:
            noted = "%02d:%s–%02d:%s" % (int(match.group(1)), match.group(2),
                                          int(match.group(3)), match.group(4))
            scheduled = "%s–%s" % (_hhmm(record.get(RAW_TIME_START)),
                                   _hhmm(record.get(RAW_TIME_END)))
            if noted != scheduled:
                warn(record, NOTE_CONTENT_TIME.format(noted=noted, scheduled=scheduled))
    return overlaps, notes_map


# --------------------------------------------------------------------------
# 规范化
# --------------------------------------------------------------------------
def build_events(events, notes_map, semester):
    normalized = []
    for record in events:
        kind = _s(record.get(RAW_TYPE))
        course = _s(record.get(FIELD_MAP["course"]))
        source_id = "%s-%s" % (kind, _s(record.get(RAW_ID)))
        normalized.append({
            "sourceId": source_id,
            "type": "上课" if kind == "kb" else "考试",
            "title": "[考试] " + course if kind == "ks" else course,
            "course": course,
            "date": _s(record.get(FIELD_MAP["date"])),
            "start": event_start(record),
            "end": event_end(record),
            "week": int(record.get(RAW_WEEK)),
            "weekday": WEEKDAY_BY_XQ.get(record.get(RAW_WEEKDAY), ""),
            "periods": period_text(record.get(RAW_PERIODS)),
            "content": _s(record.get(FIELD_MAP["content"])),
            "teacher": _s(record.get(FIELD_MAP["teacher"])),
            "campus": _s(record.get(FIELD_MAP["campus"])),
            "location": _s(record.get(FIELD_MAP["location"])),
            "phase": _s(record.get(FIELD_MAP["phase"])),
            "form": _s(record.get(FIELD_MAP["form"])),
            "teachingClass": _s(record.get(FIELD_MAP["teachingClass"])),
            "assessment": _s(record.get(FIELD_MAP["assessment"])),
            "courseCode": _s(record.get(FIELD_MAP["courseCode"])),
            "platformCourseCode": _s(record.get(FIELD_MAP["platformCourseCode"])),
            "sequence": _s(record.get(FIELD_MAP["sequence"])),
            "scheduledStudents": record.get(FIELD_MAP["scheduledStudents"]),
            "enrolledStudents": record.get(FIELD_MAP["enrolledStudents"]),
            "seat": record.get(RAW_SEAT) if kind == "ks" else None,
            "sourceRemark": _s(record.get(FIELD_MAP["sourceRemark"])),
            "notes": NOTE_SEPARATOR.join(notes_map.get(source_id, [])),
            "semester": semester,
            "timeZone": TIME_ZONE,
        })
    return normalized


def normalize(records, semester=None, fetched_at=None, source=DEFAULT_SOURCE):
    """原始记录 -> 规范化课表 dict（不写文件、不做校验）。"""
    classes = [r for r in records if r.get(RAW_TYPE) == "kb"]
    exams = [r for r in records if r.get(RAW_TYPE) == "ks"]
    selections = [r for r in records if r.get(RAW_TYPE) == "xk"]
    events = sort_records(classes + exams)

    if semester is None:
        semester = infer_semester([e.get(FIELD_MAP["date"]) for e in events])
    if fetched_at is None:
        fetched_at = _beijing_today().isoformat()

    overlaps, notes_map = build_overlaps_and_notes(events, classes)
    return {
        "fetchedAt": fetched_at,
        "source": source,
        "semester": semester,
        "timeZone": TIME_ZONE,
        "events": build_events(events, notes_map, semester),
        "selections": sort_records(selections),
        "overlaps": overlaps,
    }


# --------------------------------------------------------------------------
# 硬校验
# --------------------------------------------------------------------------
def infer_week1_start(events):
    """推断第 1 周起点：date-(week-1)*7 的最小值（与校验是否开启无关）。"""
    anchors = []
    for event in events:
        date = parse_date(event["date"])
        week = event.get("week")
        if date is not None and isinstance(week, int) and week >= 1:
            anchors.append(date - _dt.timedelta(days=(week - 1) * 7))
    return min(anchors) if anchors else None


def run_checks(events):
    """返回 (errors, stats)。errors 为空表示通过。"""
    errors = []
    weekday_ok = 0
    range_ok = 0
    anchors = {}                      # (课程, 星期) -> set(第 1 周该星期几的日期)
    for event in events:
        where = event["sourceId"]
        date = parse_date(event["date"])
        if date is None:
            errors.append("%s：日期格式非法 %r" % (where, event["date"]))
        else:
            expected = ISO_WEEKDAY_NAMES[date.isoweekday() - 1]
            if expected != event["weekday"]:
                errors.append("%s：日期 %s 是%s，但接口 xq 对应%s"
                              % (where, event["date"], expected, event["weekday"] or "<空>"))
            else:
                weekday_ok += 1
            if isinstance(event.get("week"), int):
                anchor = date - _dt.timedelta(days=(event["week"] - 1) * 7)
                anchors.setdefault((event["course"], event["weekday"]), set()).add(anchor)
            else:
                errors.append("%s：教学周不是整数 %r" % (where, event.get("week")))
        try:
            start_dt = _dt.datetime.strptime(event["start"], "%Y-%m-%d %H:%M:%S")
            end_dt = _dt.datetime.strptime(event["end"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            errors.append("%s：时间格式非法（%r / %r）" % (where, event.get("start"), event.get("end")))
        else:
            if end_dt <= start_dt:
                errors.append("%s：结束时间不晚于开始时间（%s / %s）"
                              % (where, event["start"], event["end"]))
            else:
                range_ok += 1

    anchor_groups = len(anchors)
    for key, values in sorted(anchors.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))):
        if len(values) > 1:
            errors.append("课程「%s」%s 的 date-(week-1)*7 不恒定：%s"
                          % (key[0], key[1], "、".join(sorted(str(v) for v in values))))

    all_anchors = sorted(a for values in anchors.values() for a in values)
    week1_start = all_anchors[0] if all_anchors else None
    stats = {
        "weekday_ok": weekday_ok,
        "range_ok": range_ok,
        "anchor_groups": anchor_groups,
        "week1_start": week1_start if week1_start is not None else infer_week1_start(events),
    }
    return errors, stats


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def note_kind_counts(events):
    counts = {"location": 0, "overlap": 0, "parallel": 0, "content": 0, "lines": 0}
    for event in events:
        for line in event["notes"].split(NOTE_SEPARATOR) if event["notes"] else []:
            if not line:
                continue
            counts["lines"] += 1
            if line.startswith("网页未提供教学场地"):
                counts["location"] += 1
            elif line.startswith("时间重叠"):
                counts["overlap"] += 1
            elif line.startswith("同课同时段另有排课"):
                counts["parallel"] += 1
            elif line.startswith("授课内容注明"):
                counts["content"] += 1
    return counts


def build_report(records, normalized, stats=None, check_errors=None, checks_enabled=True,
                 out_path=None, raw_path=None, semester_source="inferred"):
    events = normalized["events"]
    classes = [e for e in events if e["type"] == "上课"]
    exams = [e for e in events if e["type"] == "考试"]
    notes = [e for e in events if e["notes"]]
    kinds = note_kind_counts(events)
    dates = sorted(e["date"] for e in events)
    weeks = sorted(e["week"] for e in events)

    lines = []
    lines.append("== 教务课表规范化报告 ==")
    if raw_path:
        lines.append("原始文件：%s" % raw_path)
    lx_counts = {}
    for record in records:
        lx_counts[record.get(RAW_TYPE)] = lx_counts.get(record.get(RAW_TYPE), 0) + 1
    lines.append("原始记录：%d 条（%s）" % (
        len(records), " / ".join("%s=%d" % (k, v) for k, v in sorted(lx_counts.items(), key=lambda i: str(i[0])))))
    lines.append("events：%d 条（上课 %d / 考试 %d）" % (len(events), len(classes), len(exams)))
    lines.append("selections：%d 条" % len(normalized["selections"]))
    parallel_pairs = sum(1 for o in normalized["overlaps"] if o["parallel"])
    lines.append("overlaps：%d 对（其中同课同时段平行排课 %d 对）"
                 % (len(normalized["overlaps"]), parallel_pairs))
    lines.append("notes：%d 条非空（共 %d 行：无场地 %d / 时间重叠 %d / 平行排课 %d / 内容时段不一致 %d）"
                 % (len(notes), kinds["lines"], kinds["location"], kinds["overlap"],
                    kinds["parallel"], kinds["content"]))
    if dates:
        lines.append("日期范围：%s ~ %s" % (dates[0], dates[-1]))
    if weeks:
        lines.append("周次范围：第 %d ~ %d 周" % (weeks[0], weeks[-1]))
    lines.append("学期：%s（%s）" % (normalized["semester"], "命令行指定" if semester_source == "explicit" else "按最早日期推断"))
    lines.append("时区：%s；抓取日期：%s" % (normalized["timeZone"], normalized["fetchedAt"]))
    week1 = (stats or {}).get("week1_start")
    if week1:
        lines.append("第 1 周起点：%s（%s）" % (week1.isoformat(), ISO_WEEKDAY_NAMES[week1.isoweekday() - 1]))
    if checks_enabled:
        if check_errors:
            lines.append("校验：失败（%d 项）" % len(check_errors))
        else:
            lines.append("校验：通过（日期-星期 %d/%d；同课程同星期锚点 %d 组一致；end>start %d/%d）"
                         % (stats["weekday_ok"], len(events), stats["anchor_groups"],
                            stats["range_ok"], len(events)))
    else:
        lines.append("校验：已跳过（--no-checks）")
    if out_path:
        lines.append("输出：%s" % out_path)
    if check_errors:
        lines.append("-- 校验错误明细 --")
        lines.extend("  [FAIL] %s" % item for item in check_errors)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="教务系统原始 JSON -> 统一课表 JSON（含校验与异常标注）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python jw_normalize.py --raw calendar_raw.json --out normalized_schedule.json",
    )
    parser.add_argument("--raw", required=True, help="原始接口 JSON（对象含 data 数组，或纯数组）")
    parser.add_argument("--out", required=True, help="输出 normalized JSON 路径")
    parser.add_argument("--semester", default=None,
                        help="学期文案，默认按最早排课日期推断（例：2026–2027 学年第一学期）")
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help="写入 source 字段的教务系统 URL（默认入口页）")
    parser.add_argument("--no-checks", action="store_true",
                        help="关闭硬校验（仅用于已知脏数据的排查，不推荐）")
    parser.add_argument("--fetched-at", default=None, metavar="YYYY-MM-DD",
                        help="覆盖 fetchedAt（默认取北京时间当天，便于复现历史结果）")
    parser.add_argument("--version", action="version", version="jw_normalize %s" % VERSION)
    return parser.parse_args(argv)


def main(argv=None):
    try:                                                  # 中文报告在 GBK 控制台也不崩
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = parse_args(argv)
    try:
        records, _meta = load_raw(args.raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("[错误] 读取原始 JSON 失败：%s" % exc, file=sys.stderr)
        return 1

    semester_source = "explicit"
    semester = args.semester
    if semester is None:
        semester_source = "inferred"
    normalized = normalize(records, semester=semester, fetched_at=args.fetched_at, source=args.source)

    check_errors = []
    stats = None
    if not args.no_checks:
        check_errors, stats = run_checks(normalized["events"])
    else:                                     # 仍推断第 1 周起点供报告使用，但不做校验判定
        stats = {"week1_start": infer_week1_start(normalized["events"])}

    report = build_report(records, normalized, stats=stats, check_errors=check_errors,
                          checks_enabled=not args.no_checks, out_path=args.out,
                          raw_path=args.raw, semester_source=semester_source)
    print(report)

    if check_errors:
        for item in check_errors:
            print("  [FAIL] %s" % item, file=sys.stderr)
        print("[校验失败] 共 %d 项问题，未写出 %s（数据未被修改；确认无误后可加 --no-checks）"
              % (len(check_errors), args.out), file=sys.stderr)
        return 2

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(normalized, ensure_ascii=False, indent=2))
    print("[完成] 已写出 %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
