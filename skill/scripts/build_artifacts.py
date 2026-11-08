#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把规范化课表 JSON（normalized_schedule.json）生成为三件套交付物，并内置自检。

用法::

    python build_artifacts.py --normalized work/normalized_schedule.json --outdir outputs
    python build_artifacts.py --normalized work/normalized_schedule.json --outdir outputs \\
        --stem 张三_2026-2027-1 --prefix 张三 --report 自检报告.txt

输出（写入 --outdir）::

    <stem>_详细课表.xlsx   4 个工作表：课次明细 / 课程总览 / 考试安排 / 选课时间
    <stem>_飞书导入.csv    27 列，UTF-8 无 BOM + CRLF，供飞书多维表格导入
    <stem>_北京时间.ics    全量 VEVENT，TZID=Asia/Shanghai，RFC 5545 折行

约定与取舍：

- 只读 normalized，不修改、不猜测源数据。空教学场地在 xlsx 渲染为「未提供」，
  CSV 保留空串（导入时按空值处理），ICS 写「地点未提供」。
- 课程总览全部写静态数值（不写 Excel 公式），任何阅读器打开即看到最终值，
  不依赖重算缓存；代价是源数据变更后必须重跑本脚本。
- 事件顺序沿用 normalized 中的顺序（jw_normalize.py 已按开始时间排序），
  这样 xlsx/csv/ics 与源记录严格一一对应。
- 课程总览按课程名排序：优先用 GBK 字节序（GB2312 一级汉字按拼音排列，
  与 zh-CN 拼音排序结果一致，无需第三方 pypinyin）；GBK 无法编码的生僻字
  回退 UTF-8 字节序，保证排序稳定可复现。
- 依赖仅标准库 + openpyxl（xlsx 写入/读回校验）。

自检失败（任一项 FAIL）时退出码为 1；参数/输入错误退出码为 2。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
from datetime import datetime, time as dtime, timezone

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover - 环境缺依赖时给出可执行的提示
    sys.stderr.write(
        "缺少 openpyxl，无法生成 xlsx。请先安装（本环境已自带）：\n"
        "  pip install openpyxl\n"
    )
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# 常量（与验收基准成品对齐）
# ---------------------------------------------------------------------------

CLASS_TYPE = "上课"
EXAM_TYPE = "考试"

FONT_NAME = "Microsoft YaHei"
TEXT_COLOR = "FF182230"
TITLE_COLOR = "FF12375C"
SUBTITLE_COLOR = "FF526174"
NOTE_COLOR = "FF59677A"
HEADER_FILL_COLOR = "FF173B60"
HEADER_TEXT_COLOR = "FFFFFFFF"

DEFAULT_STEM = "课表"
DEFAULT_SEMESTER = "2026–2027 学年第一学期"
DEFAULT_SOURCE = "教务系统"
DEFAULT_FETCHED_AT = "未知日期"

DETAIL_HEADERS = [
    "日期", "开始", "结束", "课程", "授课内容", "授课教师", "教学场地",
    "教学周", "星期", "节次", "教学环节", "教学形式", "校区", "教学班",
    "考核方式", "课序号", "课程编号", "平台课程编号", "排课人数", "上课人数",
    "数据提示", "源备注", "源记录ID",
]
DETAIL_WIDTHS = [13, 9, 9, 30, 55, 22, 27, 9, 11, 23, 13, 13, 12, 45, 12, 10, 14, 17, 10, 10, 64, 32, 15]

OVERVIEW_HEADERS = [
    "课程", "授课教师", "排课记录数", "理论记录数", "实验记录数",
    "首次上课", "最后上课", "考核方式", "已提供教学场地",
]
OVERVIEW_WIDTHS = [31, 30, 14, 14, 14, 14, 14, 13, 70]

EXAM_HEADERS = [
    "考试日期", "开始", "结束", "课程", "考场", "校区", "座位号", "考试人数",
    "监考教师（接口）", "教学周", "节次", "数据提示", "源记录ID",
]
EXAM_WIDTHS = [14, 9, 9, 29, 25, 13, 10, 12, 34, 10, 22, 65, 16]

SELECTION_HEADERS = ["选课阶段", "开始时间", "结束时间", "说明", "源记录ID"]
SELECTION_WIDTHS = [47, 25, 25, 75, 19]

CSV_HEADERS = [
    "日程标题", "类型", "开始时间", "结束时间", "课程", "授课内容",
    "授课或监考教师", "校区", "教学场地", "教学周", "星期", "节次",
    "教学环节", "教学形式", "教学班", "考核方式", "课程编号", "平台课程编号",
    "课序号", "排课人数", "上课人数", "座位号", "源备注", "数据提示",
    "源记录ID", "学期", "时区",
]

ICS_TZID = "Asia/Shanghai"
ICS_PRODID = "-//DSH Skill//jiaowu-schedule//ZH"
ICS_UID_DOMAIN = "@jw.example.edu.cn"

# 表头样式使用的 PatternFill 对象可复用（openpyxl 会复制样式）
HEADER_FILL = PatternFill(fill_type="solid", start_color=HEADER_FILL_COLOR,
                          end_color=HEADER_FILL_COLOR)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def text_of(value) -> str:
    """None -> ''，其余转字符串（保持原样，不做 trim，避免改动源数据）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def parse_datetime(value) -> datetime:
    """解析 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD HH:MM' / ISO 'T' 分隔等形态。"""
    if isinstance(value, datetime):
        return value
    raw = text_of(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError("无法解析时间：%r" % (value,))


def week_value(value):
    """教学周：能转 int 就写数字，否则原样返回。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raw = text_of(value).strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def count_value(value):
    """排课人数/上课人数/座位号：数字保持数字，空值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else value
    raw = text_of(value).strip()
    if not raw:
        return None
    return int(raw) if raw.lstrip("-").isdigit() else raw


def source_record_id(event: dict) -> str:
    """'kb-1234567' -> '1234567'（成品「源记录ID」列用的是接口原始 dm）。"""
    sid = text_of(event.get("sourceId")).strip()
    if "-" in sid:
        return sid.split("-", 1)[1]
    return sid


def display_title(event: dict) -> str:
    """日程标题：上课=课程名；考试=[考试] 课程名（与成品一致）。"""
    title = text_of(event.get("title")).strip() or text_of(event.get("course")).strip()
    if event.get("type") == EXAM_TYPE and not title.startswith("[考试]"):
        title = "[考试] " + title
    return title


def course_sort_key(name: str):
    """中文课程名排序键：GBK 字节序近似 zh-CN 拼音排序，生僻字回退 UTF-8。"""
    try:
        return (0, name.encode("gbk"))
    except UnicodeEncodeError:
        return (1, name.encode("utf-8"))


def wrapped_lines(text, cols: int) -> int:
    """估算折行行数：按换行切段，每段按列宽向上取整（与成品行高算法一致）。"""
    raw = text_of(text)
    if not raw:
        return 1
    return sum(max(1, math.ceil(len(line) / cols)) for line in raw.split("\n")) or 1


# ---------------------------------------------------------------------------
# 数据装配（写入与自检共用，避免两份实现漂移）
# ---------------------------------------------------------------------------

def event_start(event: dict) -> datetime:
    return parse_datetime(event.get("start") or (text_of(event.get("date")) + " 00:00:00"))


def event_end(event: dict) -> datetime:
    return parse_datetime(event.get("end"))


def detail_values(event: dict) -> list:
    """课次明细一行（语义值；写出时会转成 datetime/time 等原生类型）。"""
    start = event_start(event)
    end = event_end(event)
    return [
        datetime(start.year, start.month, start.day),
        dtime(start.hour, start.minute, start.second),
        dtime(end.hour, end.minute, end.second),
        text_of(event.get("course")),
        text_of(event.get("content")) or None,
        text_of(event.get("teacher")) or None,
        text_of(event.get("location")).strip() or "未提供",
        week_value(event.get("week")),
        text_of(event.get("weekday")) or None,
        text_of(event.get("periods")) or None,
        text_of(event.get("phase")) or None,
        text_of(event.get("form")).strip() or "未提供",
        text_of(event.get("campus")) or None,
        text_of(event.get("teachingClass")) or None,
        text_of(event.get("assessment")) or None,
        text_of(event.get("sequence")) or None,
        text_of(event.get("courseCode")) or None,
        text_of(event.get("platformCourseCode")) or None,
        count_value(event.get("scheduledStudents")),
        count_value(event.get("enrolledStudents")),
        text_of(event.get("notes")) or None,
        text_of(event.get("sourceRemark")) or None,
        source_record_id(event),
    ]


def overview_values(classes: list) -> list:
    """课程总览 15 行（静态值，不含公式）。"""
    names = sorted({text_of(e.get("course")) for e in classes}, key=course_sort_key)
    rows = []
    for name in names:
        recs = [e for e in classes if text_of(e.get("course")) == name]
        # 教师：按原始记录顺序拆分（逗号/中文逗号/顿号），首次出现去重
        teachers, seen = [], set()
        for rec in recs:
            for token in re.split(r"[,，、]", text_of(rec.get("teacher"))):
                token = token.strip()
                if token and token not in seen:
                    seen.add(token)
                    teachers.append(token)
        theory = sum(1 for rec in recs if text_of(rec.get("phase")).strip() == "理论教学")
        lab = sum(1 for rec in recs if text_of(rec.get("phase")).strip() == "实验教学")
        dates = sorted(event_start(rec) for rec in recs)
        first, last = dates[0], dates[-1]
        assessments, seen_a = [], set()
        for rec in recs:
            assessment = text_of(rec.get("assessment")).strip()
            if assessment and assessment not in seen_a:
                seen_a.add(assessment)
                assessments.append(assessment)
        locations, seen_l = [], set()
        for rec in recs:
            location = text_of(rec.get("location")).strip()
            if location and location not in seen_l:
                seen_l.add(location)
                locations.append(location)
        rows.append([
            name,
            "、".join(teachers) or None,
            len(recs),
            theory,
            lab,
            datetime(first.year, first.month, first.day),
            datetime(last.year, last.month, last.day),
            "、".join(assessments) or None,
            "、".join(locations) or None,
        ])
    return rows


def exam_values(event: dict) -> list:
    start = event_start(event)
    end = event_end(event)
    return [
        datetime(start.year, start.month, start.day),
        dtime(start.hour, start.minute, start.second),
        dtime(end.hour, end.minute, end.second),
        text_of(event.get("course")) or None,
        text_of(event.get("location")) or None,
        text_of(event.get("campus")) or None,
        count_value(event.get("seat")),
        count_value(event.get("scheduledStudents")),
        text_of(event.get("teacher")) or None,
        week_value(event.get("week")),
        text_of(event.get("periods")) or None,
        text_of(event.get("notes")) or None,
        source_record_id(event),
    ]


def selection_values(selection: dict) -> list:
    """选课时间一行：开始/结束 = 日期 + 时间（qssj/jssj 带前导空格，需 strip）。"""
    def merge(date_key, time_key):
        date_part = text_of(selection.get(date_key)).strip()
        time_part = text_of(selection.get(time_key)).strip() or "00:00:00"
        return parse_datetime("%s %s" % (date_part, time_part))

    return [
        text_of(selection.get("kcmc")) or None,
        merge("qsrq", "qssj"),
        merge("jsrq", "jssj"),
        text_of(selection.get("bz")).strip() or "未提供",
        text_of(selection.get("dm")) or None,
    ]


def csv_row(event: dict) -> list:
    """CSV 一行 27 列（全部字符串，None -> ''）。"""
    return [
        display_title(event),
        text_of(event.get("type")),
        event_start(event).strftime("%Y-%m-%d %H:%M:%S"),
        event_end(event).strftime("%Y-%m-%d %H:%M:%S"),
        text_of(event.get("course")),
        text_of(event.get("content")),
        text_of(event.get("teacher")),
        text_of(event.get("campus")),
        text_of(event.get("location")),
        text_of(week_value(event.get("week"))),
        text_of(event.get("weekday")),
        text_of(event.get("periods")),
        text_of(event.get("phase")),
        text_of(event.get("form")),
        text_of(event.get("teachingClass")),
        text_of(event.get("assessment")),
        text_of(event.get("courseCode")),
        text_of(event.get("platformCourseCode")),
        text_of(event.get("sequence")),
        text_of(count_value(event.get("scheduledStudents"))),
        text_of(count_value(event.get("enrolledStudents"))),
        text_of(count_value(event.get("seat"))),
        text_of(event.get("sourceRemark")),
        text_of(event.get("notes")),
        text_of(event.get("sourceId")),
        text_of(event.get("semester")),
        text_of(event.get("timeZone")),
    ]


def readable(value) -> str:
    """把单元格语义值转成便于比对的字符串。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        if (value.hour, value.minute, value.second) == (0, 0, 0):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dtime):
        return value.strftime("%H:%M")
    return str(value)


# ---------------------------------------------------------------------------
# xlsx 写入
# ---------------------------------------------------------------------------

def _font(size=10, bold=False, color=TEXT_COLOR):
    return Font(name=FONT_NAME, size=size, bold=bold, color=color)


def _write_frame(ws, title, subtitle, note, headers, widths, freeze, tab_color,
                 include_header=True):
    """第 1 行空、第 2 行标题、第 4 行说明、第 5 行表头（含样式/列宽/冻结）。"""
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = tab_color

    ws["A2"] = title
    ws["A2"].font = _font(size=15, bold=True, color=TITLE_COLOR)
    ws["A2"].alignment = Alignment(vertical="center")
    ws["D2"] = subtitle
    ws["D2"].font = _font(size=10, color=SUBTITLE_COLOR)
    ws["D2"].alignment = Alignment(vertical="center")
    ws["A4"] = note
    ws["A4"].font = _font(size=9, color=NOTE_COLOR)
    ws["A4"].alignment = Alignment(vertical="center")

    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width

    if include_header:
        for col, label in enumerate(headers, start=1):
            cell = ws.cell(row=5, column=col, value=label)
            cell.fill = HEADER_FILL
            cell.font = _font(bold=True, color=HEADER_TEXT_COLOR)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.row_dimensions[5].height = 30

    ws.freeze_panes = freeze


def _write_data_cells(ws, first_row, rows):
    for offset, values in enumerate(rows):
        row_index = first_row + offset
        for col, value in enumerate(values, start=1):
            ws.cell(row=row_index, column=col, value=value)


def write_xlsx(path, classes, exams, selections, norm):
    """生成 4 表工作簿；返回用于自检的元信息。"""
    semester = text_of(norm.get("semester")).strip() or DEFAULT_SEMESTER
    source = text_of(norm.get("source")).strip() or DEFAULT_SOURCE
    fetched_at = text_of(norm.get("fetchedAt")).strip() or DEFAULT_FETCHED_AT

    wb = Workbook()
    detail = wb.active
    detail.title = "课次明细"
    overview = wb.create_sheet("课程总览")
    exam_sheet = wb.create_sheet("考试安排")
    selection_sheet = wb.create_sheet("选课时间")

    # ---------------- 课次明细 ----------------
    detail_title = ("%s 详细课表" % text_of(norm.get("_prefix")).strip()) if norm.get("_prefix") else "详细课表"
    detail_note = (
        "来源：教务系统日历 %s ；%s 抓取。所有钟点为北京时间，"
        "保留原始排课及内容中的差异。" % (source, fetched_at)
    )
    _write_frame(detail, detail_title, semester, detail_note,
                 DETAIL_HEADERS, DETAIL_WIDTHS, "E6", "173B60")

    detail_rows = [detail_values(event) for event in classes]
    _write_data_cells(detail, 6, detail_rows)
    last_row = 5 + len(detail_rows)
    for row_index in range(6, last_row + 1):
        for col in range(1, len(DETAIL_HEADERS) + 1):
            cell = detail.cell(row=row_index, column=col)
            cell.font = _font()
            cell.alignment = Alignment(vertical="center")
        detail.cell(row=row_index, column=1).number_format = "yyyy-mm-dd"
        detail.cell(row=row_index, column=2).number_format = "hh:mm"
        detail.cell(row=row_index, column=3).number_format = "hh:mm"
        detail.cell(row=row_index, column=2).alignment = Alignment(horizontal="center", vertical="center")
        detail.cell(row=row_index, column=3).alignment = Alignment(horizontal="center", vertical="center")
        for col in (4, 5, 6, 7, 14, 21, 22):  # D E F G N U V
            detail.cell(row=row_index, column=col).alignment = Alignment(vertical="center", wrap_text=True)
    # 行高按成品算法估算（内容/提示/课程名折行），保证长文本不被压扁
    for offset, event in enumerate(classes):
        lines = max(
            wrapped_lines(event.get("content"), 27),
            wrapped_lines(event.get("notes"), 33),
            wrapped_lines(event.get("course"), 14),
            2,
        )
        detail.row_dimensions[offset + 6].height = max(40, lines * 16 + 10)

    # 第 3 行统计区域：静态数值（避免依赖公式重算）
    detail["A3"] = "排课记录"
    detail["B3"] = len(classes)
    detail["D3"] = "课程数"
    detail["E3"] = len({text_of(e.get("course")) for e in classes})
    for coord in ("A3", "B3", "D3", "E3"):
        detail[coord].font = _font()
        detail[coord].alignment = Alignment(vertical="center")

    # ---------------- 课程总览 ----------------
    _write_frame(overview, "课程总览", semester,
                 "记录数按源数据逐条统计；同课的分段排课及同时段多教室记录均保留，"
                 "不等同于去重后的实际课次或学时。",
                 OVERVIEW_HEADERS, OVERVIEW_WIDTHS, "B6", "6F8CAB")
    overview_rows = overview_values(classes)
    _write_data_cells(overview, 6, overview_rows)
    overview_last = 5 + len(overview_rows)
    for row_index in range(6, overview_last + 1):
        for col in range(1, len(OVERVIEW_HEADERS) + 1):
            cell = overview.cell(row=row_index, column=col)
            cell.font = _font()
            cell.alignment = Alignment(vertical="center")
        overview.cell(row=row_index, column=6).number_format = "yyyy-mm-dd"
        overview.cell(row=row_index, column=7).number_format = "yyyy-mm-dd"
        for col in (1, 2, 9):  # A B I
            overview.cell(row=row_index, column=col).alignment = Alignment(vertical="center", wrap_text=True)
        overview.row_dimensions[row_index].height = 60
    overview["A3"] = "排课记录合计"
    overview["C3"] = sum(row[2] for row in overview_rows)
    for coord in ("A3", "C3"):
        overview[coord].font = _font()
        overview[coord].alignment = Alignment(vertical="center")

    # ---------------- 考试安排 ----------------
    exam_note = (
        "包含教务接口返回的%d条考试记录；已结束的考试仍保留。"
        "监考教师字段取自接口，教务页面可能显示为星号。" % len(exams)
    )
    _write_frame(exam_sheet, "考试安排", semester, exam_note,
                 EXAM_HEADERS, EXAM_WIDTHS, "B6", "8A4343")
    exam_rows = [exam_values(event) for event in exams]
    _write_data_cells(exam_sheet, 6, exam_rows)
    exam_last = 5 + len(exam_rows)
    for row_index in range(6, exam_last + 1):
        for col in range(1, len(EXAM_HEADERS) + 1):
            cell = exam_sheet.cell(row=row_index, column=col)
            cell.font = _font()
            cell.alignment = Alignment(vertical="center")
        exam_sheet.cell(row=row_index, column=1).number_format = "yyyy-mm-dd"
        exam_sheet.cell(row=row_index, column=2).number_format = "hh:mm"
        exam_sheet.cell(row=row_index, column=3).number_format = "hh:mm"
        for col in (2, 3):
            exam_sheet.cell(row=row_index, column=col).alignment = Alignment(horizontal="center", vertical="center")
        for col in (9, 12):  # I L
            exam_sheet.cell(row=row_index, column=col).alignment = Alignment(vertical="center", wrap_text=True)
        exam_sheet.row_dimensions[row_index].height = 80

    # ---------------- 选课时间 ----------------
    selection_note = _selection_note(selections, classes)
    _write_frame(selection_sheet, "选课时间", semester, selection_note,
                 SELECTION_HEADERS, SELECTION_WIDTHS, "B6", "7B877B")
    selection_rows = [selection_values(selection) for selection in selections]
    _write_data_cells(selection_sheet, 6, selection_rows)
    selection_last = 5 + len(selection_rows)
    for row_index in range(6, selection_last + 1):
        for col in range(1, len(SELECTION_HEADERS) + 1):
            cell = selection_sheet.cell(row=row_index, column=col)
            cell.font = _font()
            cell.alignment = Alignment(vertical="center")
        for col in (2, 3):
            selection_sheet.cell(row=row_index, column=col).number_format = "yyyy-mm-dd hh:mm:ss"
            selection_sheet.cell(row=row_index, column=col).alignment = Alignment(horizontal="center", vertical="center")
        for col in (1, 4):  # A D
            selection_sheet.cell(row=row_index, column=col).alignment = Alignment(vertical="center", wrap_text=True)
        selection_sheet.row_dimensions[row_index].height = 58

    wb.save(path)


def _selection_note(selections, classes):
    """选课时间说明：自动统计「早于首节课」的历史选课条数与月份。"""
    base = "教务接口返回的%d条选课时间段原样保留" % len(selections)
    if not selections:
        return base + "。"
    class_dates = [event_start(event) for event in classes]
    if not class_dates:
        return base + "。"
    class_start = min(class_dates)
    historical = []
    for selection in selections:
        try:
            end = selection_values(selection)[2]
        except (ValueError, KeyError):
            continue
        if end < class_start:
            historical.append(end)
    if not historical:
        return base + "，不计入上课或考试日历。"
    months = sorted({(moment.year, moment.month) for moment in historical})
    if len(months) == 1:
        year, month = months[0]
        return base + "，其中%d条为%d年%d月的历史选课，不计入上课或考试日历。" % (
            len(historical), year, month)
    return base + "，其中%d条为历史选课，不计入上课或考试日历。" % len(historical)


# ---------------------------------------------------------------------------
# CSV 写入
# ---------------------------------------------------------------------------

def write_csv(path, events):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)
    for event in events:
        writer.writerow(csv_row(event))
    data = buffer.getvalue().encode("utf-8")  # 显式 UTF-8 无 BOM
    with open(path, "wb") as handle:
        handle.write(data)


# ---------------------------------------------------------------------------
# ICS 写入
# ---------------------------------------------------------------------------

def _ics_escape(value) -> str:
    escaped = text_of(value).replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n")
    escaped = escaped.replace("\n", "\\n")
    escaped = escaped.replace(",", "\\,").replace(";", "\\;")
    return escaped


def _ics_fold(line: str) -> list:
    """RFC 5545 折行：物理行 ≤75 字节，续行以单个空格开头。"""
    parts, part, size = [], "", 0
    for char in line:
        char_size = len(char.encode("utf-8"))
        if size + char_size > 75:
            parts.append(part)
            part = " " + char
            size = 1 + char_size
        else:
            part += char
            size += char_size
    parts.append(part)
    return parts


def _ics_stamp(value) -> str:
    return parse_datetime(value).strftime("%Y%m%dT%H%M%S")


def _ics_description(event, semester, source, fetched_at) -> str:
    event_type = text_of(event.get("type"))
    lines = [
        "%s｜%s" % (semester, event_type),
        "教学周：%s；%s；节次：%s" % (
            text_of(week_value(event.get("week"))) or "未提供",
            text_of(event.get("weekday")) or "未提供",
            text_of(event.get("periods")) or "未提供",
        ),
        "授课内容：%s" % (text_of(event.get("content")).strip() or "未提供"),
        "%s：%s" % (
            "监考教师（接口）" if event_type == EXAM_TYPE else "授课教师",
            text_of(event.get("teacher")).strip() or "未提供",
        ),
        "教学环节：%s；教学形式：%s" % (
            text_of(event.get("phase")).strip() or "未提供",
            text_of(event.get("form")).strip() or "未提供",
        ),
        "教学班：%s" % (text_of(event.get("teachingClass")).strip() or "未提供"),
        "考核方式：%s" % (text_of(event.get("assessment")).strip() or "未提供"),
    ]
    seat = text_of(event.get("seat")).strip()
    if seat:
        lines.append("座位号：%s" % seat)
    notes = text_of(event.get("notes")).strip()
    if notes:
        lines.append("数据提示：%s" % notes)
    remark = text_of(event.get("sourceRemark")).strip()
    if remark:
        lines.append("源备注：%s" % remark)
    lines.append("源记录：%s" % text_of(event.get("sourceId")))
    lines.append("来源：%s（%s）" % (source, fetched_at))
    return "\n".join(lines)


def write_ics(path, events, norm):
    semester = text_of(norm.get("semester")).strip() or DEFAULT_SEMESTER
    source = text_of(norm.get("source")).strip() or DEFAULT_SOURCE
    fetched_at = text_of(norm.get("fetchedAt")).strip() or DEFAULT_FETCHED_AT
    prefix = text_of(norm.get("_prefix")).strip()
    calendar_name = ("%s %s 课表与考试" % (prefix, semester)) if prefix else (semester + " 课表与考试")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "Z"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:" + ICS_PRODID,
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + calendar_name,
        "X-WR-TIMEZONE:" + ICS_TZID,
        "BEGIN:VTIMEZONE",
        "TZID:" + ICS_TZID,
        "X-LIC-LOCATION:" + ICS_TZID,
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "TZNAME:CST",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for event in events:
        start = event_start(event)
        end = event_end(event)
        location_parts = [text_of(event.get("campus")).strip(),
                          text_of(event.get("location")).strip() or "地点未提供"]
        location = "／".join(part for part in location_parts if part)
        lines.extend([
            "BEGIN:VEVENT",
            "UID:%s%s" % (text_of(event.get("sourceId")), ICS_UID_DOMAIN),
            "DTSTAMP:" + stamp,
            "DTSTART;TZID=%s:%s" % (ICS_TZID, _ics_stamp(start)),
            "DTEND;TZID=%s:%s" % (ICS_TZID, _ics_stamp(end)),
            "SUMMARY:" + _ics_escape(display_title(event)),
            "LOCATION:" + _ics_escape(location),
            "DESCRIPTION:" + _ics_escape(_ics_description(event, semester, source, fetched_at)),
            "CATEGORIES:" + _ics_escape(text_of(event.get("type"))),
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")

    physical = []
    for line in lines:
        physical.extend(_ics_fold(line))
    data = ("\r\n".join(physical) + "\r\n").encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(data)


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

class Checker:
    def __init__(self):
        self.lines = []
        self.passed = 0
        self.failed = 0

    def check(self, section, label, ok, detail=""):
        ok = bool(ok)
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        status = "PASS" if ok else "FAIL"
        line = "[自检] %-4s %s %s" % (section, status, label)
        if detail:
            line += "（%s）" % detail
        self.lines.append(line)
        print(line)
        return ok

    def emit(self, line=""):
        self.lines.append(line)
        print(line)

    def summary(self):
        verdict = "全部通过" if self.failed == 0 else "存在 %d 项失败" % self.failed
        line = "== 自检汇总：PASS %d / FAIL %d -> %s ==" % (self.passed, self.failed, verdict)
        self.emit(line)
        return self.failed == 0


def _compare_rows(expected_rows, actual_rows, compare):
    """返回差异条数与首个差异描述。"""
    if len(expected_rows) != len(actual_rows):
        return max(len(expected_rows), len(actual_rows)), "行数不一致：期望 %d，实际 %d" % (
            len(expected_rows), len(actual_rows))
    diffs = 0
    first = ""
    for index, (expected, actual) in enumerate(zip(expected_rows, actual_rows), start=1):
        mismatch = compare(expected, actual)
        if mismatch:
            diffs += 1
            if not first:
                first = "第 %d 行：%s" % (index, mismatch)
    return diffs, first


def _detail_expected(classes):
    return [[readable(value) for value in detail_values(event)] for event in classes]


def _detail_actual(ws, first_row, count, ncols):
    rows = []
    for row_index in range(first_row, first_row + count):
        rows.append([readable(ws.cell(row=row_index, column=col).value) for col in range(1, ncols + 1)])
    return rows


def run_self_check(paths, norm, classes, exams, selections, events):
    checker = Checker()
    semester = text_of(norm.get("semester")).strip() or DEFAULT_SEMESTER

    # ---------------- xlsx ----------------
    detail_expected = _detail_expected(classes)
    overview_expected = [[readable(value) for value in row] for row in overview_values(classes)]
    exam_expected = [[readable(value) for value in exam_values(event)] for event in exams]
    selection_expected = [[readable(value) for value in selection_values(selection)] for selection in selections]

    try:
        wb = load_workbook(paths["xlsx"])
    except Exception as error:  # pragma: no cover - 文件损坏时走这里
        checker.check("xlsx", "工作簿可读取", False, repr(error))
        wb = None

    if wb is not None:
        names = wb.sheetnames
        checker.check("xlsx", "工作簿包含 4 个工作表且顺序正确",
                      names == ["课次明细", "课程总览", "考试安排", "选课时间"],
                      " / ".join(names))
        detail = wb["课次明细"]
        header = [detail.cell(row=5, column=col).value for col in range(1, 24)]
        checker.check("xlsx", "课次明细第 5 行表头 23 列文字正确",
                      header == DETAIL_HEADERS, "%d/%d 列匹配" % (
                          sum(1 for a, b in zip(header, DETAIL_HEADERS) if a == b), len(DETAIL_HEADERS)))
        checker.check("xlsx", "课次明细数据行数", detail.max_row - 5 == len(classes),
                      "%d/%d 行（第 6 行起）" % (detail.max_row - 5, len(classes)))
        detail_actual = _detail_actual(detail, 6, len(classes), 23)
        diffs, first = _compare_rows(detail_expected, detail_actual,
                                     lambda a, b: "" if a == b else "差异 %r != %r" % (a, b))
        checker.check("xlsx", "课次明细 23 列数据与 normalized 逐格一致", diffs == 0,
                      "%d 行 × 23 列，差异 %d%s" % (len(classes), diffs, ("；" + first) if first else ""))
        freeze_ok = detail.freeze_panes == "E6"
        width_ok = detail.column_dimensions["A"].width == 13 and detail.column_dimensions["E"].width == 55
        fmt_ok = (detail.cell(row=6, column=1).number_format == "yyyy-mm-dd"
                  and detail.cell(row=6, column=2).number_format == "hh:mm")
        checker.check("xlsx", "课次明细冻结窗格/列宽/日期时间格式",
                      freeze_ok and width_ok and fmt_ok,
                      "freeze=%s，A 列宽=%s，日期格式=%s" % (
                          detail.freeze_panes, detail.column_dimensions["A"].width,
                          detail.cell(row=6, column=1).number_format))

        overview = wb["课程总览"]
        overview_actual = [[readable(overview.cell(row=row, column=col).value) for col in range(1, 10)]
                           for row in range(6, 6 + len(overview_expected))]
        total = sum(int(row[2]) for row in overview_expected)
        recorded_total = overview["C3"].value
        diffs, first = _compare_rows(overview_expected, overview_actual,
                                     lambda a, b: "" if a == b else "差异 %r != %r" % (a, b))
        checker.check("xlsx", "课程总览 %d 门课逐格一致且合计=%d" % (len(overview_expected), len(classes)),
                      diffs == 0 and recorded_total == total,
                      "%d 行，差异 %d，C3=%r%s" % (len(overview_expected), diffs, recorded_total,
                                                  ("；" + first) if first else ""))
        checker.check("xlsx", "课程总览无公式（静态数值）",
                      not any(isinstance(overview.cell(row=row, column=col).value, str)
                              and str(overview.cell(row=row, column=col).value).startswith("=")
                              for row in range(3, 6 + len(overview_expected))
                              for col in (1, 2, 3, 4, 5, 6, 7, 8, 9)),
                      "C/D/E 列均为静态计数")

        exam_sheet = wb["考试安排"]
        exam_actual = [[readable(exam_sheet.cell(row=row, column=col).value) for col in range(1, 14)]
                       for row in range(6, 6 + len(exam_expected))]
        diffs, first = _compare_rows(exam_expected, exam_actual,
                                     lambda a, b: "" if a == b else "差异 %r != %r" % (a, b))
        checker.check("xlsx", "考试安排 %d 条且逐格一致" % len(exams), diffs == 0,
                      "%d 行，差异 %d%s" % (len(exam_expected), diffs, ("；" + first) if first else ""))

        selection_sheet = wb["选课时间"]
        selection_actual = [[readable(selection_sheet.cell(row=row, column=col).value)
                             for col in range(1, 6)]
                            for row in range(6, 6 + len(selection_expected))]
        diffs, first = _compare_rows(selection_expected, selection_actual,
                                     lambda a, b: "" if a == b else "差异 %r != %r" % (a, b))
        checker.check("xlsx", "选课时间 %d 条且逐格一致" % len(selections), diffs == 0,
                      "%d 行，差异 %d%s" % (len(selection_expected), diffs, ("；" + first) if first else ""))

    # ---------------- csv ----------------
    with open(paths["csv"], "rb") as handle:
        csv_bytes = handle.read()
    checker.check("csv", "UTF-8 编码且无 BOM", not csv_bytes.startswith(b"\xef\xbb\xbf"),
                  "首 3 字节 %r" % (csv_bytes[:3],))
    crlf = csv_bytes.count(b"\r\n")
    bare_lf = csv_bytes.count(b"\n") - crlf
    bare_cr = csv_bytes.count(b"\r") - crlf
    try:
        csv_text = csv_bytes.decode("utf-8")
        csv_rows = list(csv.reader(io.StringIO(csv_text, newline="")))
    except Exception as error:  # pragma: no cover
        csv_text = ""
        csv_rows = []
        checker.check("csv", "CSV 可解析", False, repr(error))
    checker.check("csv", "UTF-8 严格解码且无替换符", "\ufffd" not in csv_text, "无 U+FFFD")
    # 记录分隔符必须是 CRLF；字段内嵌换行（正文多行）会保留裸 LF，属预期
    checker.check("csv", "记录分隔符为 CRLF（无裸 CR）",
                  crlf == len(csv_rows) and bare_cr == 0,
                  "CRLF %d 处（= 记录数 %d），字段内嵌 LF %d 处" % (
                      crlf, len(csv_rows), bare_lf))
    checker.check("csv", "27 列表头正确", bool(csv_rows) and csv_rows[0] == CSV_HEADERS,
                  "%d 列" % (len(csv_rows[0]) if csv_rows else 0))
    body = csv_rows[1:] if csv_rows else []
    checker.check("csv", "数据行数", len(body) == len(events),
                  "%d/%d 行（%d 上课 + %d 考试）" % (
                      len(body), len(events),
                      sum(1 for e in events if e.get("type") == CLASS_TYPE),
                      sum(1 for e in events if e.get("type") == EXAM_TYPE)))
    bad_width = sum(1 for row in csv_rows if len(row) != 27)
    checker.check("csv", "每行列数均为 27", bad_width == 0, "不合规 %d 行" % bad_width)
    expected_csv = [csv_row(event) for event in events]
    diffs, first = _compare_rows(expected_csv, body,
                                 lambda a, b: "" if a == b else "差异 %r != %r" % (a, b))
    checker.check("csv", "27 列数据与 normalized 逐格一致", diffs == 0,
                  "%d 行，差异 %d%s" % (len(body), diffs, ("；" + first) if first else ""))

    # ---------------- ics ----------------
    with open(paths["ics"], "rb") as handle:
        ics_bytes = handle.read()
    physical_lines = ics_bytes.split(b"\r\n")
    if physical_lines and physical_lines[-1] == b"":
        physical_lines = physical_lines[:-1]
    max_len = max((len(line) for line in physical_lines), default=0)
    checker.check("ics", "最大物理行 ≤75 字节", max_len <= 75, "实测最长 %d 字节" % max_len)
    checker.check("ics", "CRLF 行尾且以 CRLF 结束", ics_bytes.endswith(b"\r\n")
                  and b"\n" not in ics_bytes.replace(b"\r\n", b""),
                  "%d 条物理行" % len(physical_lines))
    ics_text = ics_bytes.decode("utf-8", "replace")
    logical_lines = ics_text.replace("\r\n ", "").split("\r\n")
    checker.check("ics", "UTF-8 可解码且无替换符", "\ufffd" not in ics_text, "无 U+FFFD")
    checker.check("ics", "含 VTIMEZONE 且 TZID=Asia/Shanghai",
                  "BEGIN:VTIMEZONE" in logical_lines and ("TZID:" + ICS_TZID) in logical_lines
                  and "TZOFFSETFROM:+0800" in logical_lines and "TZOFFSETTO:+0800" in logical_lines,
                  "STANDARD 段偏移 +0800")
    event_starts = [line for line in logical_lines if line == "BEGIN:VEVENT"]
    checker.check("ics", "VEVENT 数量", len(event_starts) == len(events),
                  "%d/%d 条" % (len(event_starts), len(events)))
    dtstart_prefix = "DTSTART;TZID=%s:" % ICS_TZID
    dtend_prefix = "DTEND;TZID=%s:" % ICS_TZID
    dtstart_values = [line[len(dtstart_prefix):] for line in logical_lines
                      if line.startswith(dtstart_prefix)]
    dtend_values = [line[len(dtend_prefix):] for line in logical_lines
                    if line.startswith(dtend_prefix)]
    parsed_starts = parsed_ends = 0
    unparsed = []
    for raw in dtstart_values:
        try:
            datetime.strptime(raw, "%Y%m%dT%H%M%S")
            parsed_starts += 1
        except ValueError:
            unparsed.append(raw)
    for raw in dtend_values:
        try:
            datetime.strptime(raw, "%Y%m%dT%H%M%S")
            parsed_ends += 1
        except ValueError:
            unparsed.append(raw)
    checker.check("ics", "DTSTART/DTEND 均可解析且数量正确",
                  parsed_starts == len(events) and parsed_ends == len(events),
                  "DTSTART %d/%d，DTEND %d/%d，不可解析 %d" % (
                      parsed_starts, len(events), parsed_ends, len(events), len(unparsed)))
    uids = [line[len("UID:"):] for line in logical_lines if line.startswith("UID:")]
    checker.check("ics", "UID 唯一", len(set(uids)) == len(events) and len(uids) == len(events),
                  "%d 条，去重后 %d 条" % (len(uids), len(set(uids))))
    summaries = [line[len("SUMMARY:"):] for line in logical_lines if line.startswith("SUMMARY:")]
    exam_summaries = [summary for summary in summaries if summary.startswith("[考试] ")]
    checker.check("ics", "考试 SUMMARY 带 [考试] 前缀",
                  len(exam_summaries) == len(exams), "%d 条" % len(exam_summaries))
    categorie_values = [line for line in logical_lines if line.startswith("CATEGORIES:")]
    checker.check("ics", "每条 VEVENT 均带 CATEGORIES",
                  len(categorie_values) == len(events), "%d 条" % len(categorie_values))
    dtstamps = [line[len("DTSTAMP:"):] for line in logical_lines if line.startswith("DTSTAMP:")]
    checker.check("ics", "每条 VEVENT 均带 DTSTAMP",
                  len(dtstamps) == len(events) and all(len(stamp) == 16 for stamp in dtstamps),
                  "%d 条" % len(dtstamps))

    # ---------------- 三份一致性与文件 ----------------
    expected_ids = {text_of(event.get("sourceId")) for event in events}
    expected_detail_ids = {source_record_id(event) for event in classes}
    expected_exam_ids = {source_record_id(event) for event in exams}
    csv_ids = {row[24] for row in body}
    uid_ids = {uid[:-len(ICS_UID_DOMAIN)] if uid.endswith(ICS_UID_DOMAIN) else uid for uid in uids}
    detail_ids = set()
    exam_ids = set()
    if wb is not None:
        detail = wb["课次明细"]
        detail_ids = {readable(detail.cell(row=row, column=23).value)
                      for row in range(6, 6 + len(classes))}
        exam_sheet = wb["考试安排"]
        exam_ids = {readable(exam_sheet.cell(row=row, column=13).value)
                    for row in range(6, 6 + len(exams))}
    checker.check("三份", "xlsx/csv/ics 源记录 ID 集合一致",
                  detail_ids == expected_detail_ids and exam_ids == expected_exam_ids
                  and csv_ids == expected_ids and uid_ids == expected_ids,
                  "xlsx %d+%d 条 / csv %d 条 / ics %d 条" % (
                      len(detail_ids), len(exam_ids), len(csv_ids), len(uid_ids)))

    sizes = []
    all_exist = True
    for key, label in (("xlsx", "xlsx"), ("csv", "csv"), ("ics", "ics")):
        path = paths[key]
        exists = os.path.isfile(path) and os.path.getsize(path) > 0
        all_exist = all_exist and exists
        sizes.append("%s %d KB" % (os.path.basename(path), os.path.getsize(path) // 1024 if exists else 0))
    checker.check("文件", "3 个交付物均已生成且非空", all_exist, "；".join(sizes))

    checker.emit()
    checker.emit("-- 数据概览：%s；上课 %d 条 / 考试 %d 条 / 选课时段 %d 条；课程 %d 门" % (
        semester, len(classes), len(exams), len(selections), len(overview_expected)))
    checker.emit("-- 交付物目录：%s" % os.path.dirname(paths["xlsx"]))
    for key in ("xlsx", "csv", "ics"):
        checker.emit("   " + paths[key])

    ok = checker.summary()
    return ok, checker.lines


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def load_normalized(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("normalized JSON 顶层必须是对象")
    events = data.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("normalized JSON 缺少 events 数组或为空")
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not event.get("start"):
            raise ValueError("events[%d] 缺少 start 字段" % index)
        if event.get("type") not in (CLASS_TYPE, EXAM_TYPE):
            raise ValueError("events[%d] 的 type 必须是「上课」或「考试」：%r" % (index, event.get("type")))
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="把规范化课表 JSON 生成为 xlsx / csv / ics 三件套并自检")
    parser.add_argument("--normalized", required=True, help="normalized_schedule.json 路径")
    parser.add_argument("--outdir", required=True, help="输出目录（不存在会自动创建）")
    parser.add_argument("--stem", default=DEFAULT_STEM, help="文件名前缀，默认「课表」")
    parser.add_argument("--prefix", default="", help="姓名前缀，仅用于标题区显示（可选）")
    parser.add_argument("--report", default="", help="可选：把自检报告另写一份 UTF-8 文本")
    args = parser.parse_args(argv)

    try:
        norm = load_normalized(args.normalized)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        sys.stderr.write("读取 normalized 失败：%s\n" % error)
        return 2

    norm["_prefix"] = args.prefix.strip()
    events = list(norm.get("events") or [])
    classes = [event for event in events if event.get("type") == CLASS_TYPE]
    exams = [event for event in events if event.get("type") == EXAM_TYPE]
    selections = list(norm.get("selections") or [])

    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)
    stem = args.stem.strip() or DEFAULT_STEM
    paths = {
        "xlsx": os.path.join(outdir, "%s_详细课表.xlsx" % stem),
        "csv": os.path.join(outdir, "%s_飞书导入.csv" % stem),
        "ics": os.path.join(outdir, "%s_北京时间.ics" % stem),
    }

    try:
        write_xlsx(paths["xlsx"], classes, exams, selections, norm)
        write_csv(paths["csv"], events)
        write_ics(paths["ics"], events, norm)
    except Exception as error:  # pragma: no cover - 写入失败时给出可读信息
        sys.stderr.write("生成交付物失败：%s: %s\n" % (type(error).__name__, error))
        return 2

    ok, lines = run_self_check(paths, norm, classes, exams, selections, events)

    if args.report:
        report_dir = os.path.dirname(os.path.abspath(args.report))
        if report_dir:
            os.makedirs(report_dir, exist_ok=True)
        with open(args.report, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        print("[报告] 自检报告已写入 %s" % os.path.abspath(args.report))

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
