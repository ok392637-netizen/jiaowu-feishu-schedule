#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jw_normalize.py 的离线自测（合成数据，不碰真实课表）。

被测脚本：同目录下的 jw_normalize.py
用法：
      python selftest.py
      python selftest.py --python C:\\path\\to\\python.exe
      python selftest.py --keep          # 保留临时目录，便于排查

覆盖点：
  * 合成原始 JSON：6 条上课（含 1 条无场地、1 条同课同时段平行排课、1 条内容时段不一致）、
    2 条考试（其中 1 条与无场地课程重叠，用于验证多条提示换行拼接）、2 条选课时间段，
    另加 1 条第二周记录用于周次锚点校验。
  * 断言 events / selections / overlaps 数量与内容、notes 文案逐字一致、
    weekday 与 periods 转换、start/end 补齐 HH:MM:SS、日期校验通过。
  * 断言 3 类硬校验失败时退出码 2、不写出输出文件；--no-checks 可跳过。
  * 断言输入同时接受 {"data":[...]} 与纯数组两种形态。
  * 链路自测：用合成 normalized 再跑 build_artifacts.py，断言三件套生成、CSV/ICS 条数与
    生成脚本内置自检全部通过（不碰真实课表、不联网）。

退出码：全部通过 0；有失败 1；参数/环境问题 2。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NORMALIZER = os.path.join(HERE, "jw_normalize.py")
BUILDER = os.path.join(HERE, "build_artifacts.py")

EXPECTED_TOP_KEYS = ["fetchedAt", "source", "semester", "timeZone", "events", "selections", "overlaps"]
EXPECTED_EVENT_KEYS = [
    "sourceId", "type", "title", "course", "date", "start", "end", "week", "weekday",
    "periods", "content", "teacher", "campus", "location", "phase", "form",
    "teachingClass", "assessment", "courseCode", "platformCourseCode", "sequence",
    "scheduledStudents", "enrolledStudents", "seat", "sourceRemark", "notes",
    "semester", "timeZone",
]
EXPECTED_SELECTION_KEYS = ["jssj", "qsrq", "color", "bz", "dm", "jsrq", "lx", "kcmc", "qssj"]
RAW_WRAPPER_URL = "https://jw.example.edu/new/welcome.page?ui=new"
NOTE_LOCATION = "网页未提供教学场地；官方导出标为“无”。"

# --------------------------------------------------------------------------
# 合成原始数据（纯虚构课程，不含任何真实个人课表）
# --------------------------------------------------------------------------
def make_kb(dm, **overrides):
    """构造一条 lx=kb（上课）原始记录，字段集合与真实接口一致。"""
    record = {
        "flfzmc": "", "khfsmc": "考试", "ps": "", "pkrs": 30, "sknrjj": "测试授课内容",
        "dm": dm, "zwh": 1, "jxhjmc": "理论教学", "qssj": "08:30:00", "lx": "kb",
        "jxbdm": "", "jssj": "10:05:00", "qsrq": "2026-09-07", "jxbrs": 30, "bz": "",
        "kxh": "1", "kcmc": "测试课程甲", "kcptbh": "90001", "teaxms": "教师甲",
        "jxbmc": "测试班甲", "zc": 1, "jxxsmc": "线下教学", "kcbh": "T0001",
        "jxcdmc": "测试楼A101", "bgcolor": "", "jcdm": "0102", "pe": "", "xqmc": "测试校区",
        "jsrq": "2026-09-07", "xq": 1,
    }
    record.update(overrides)
    return record


def make_ks(dm, **overrides):
    """构造一条 lx=ks（考试）原始记录；考试记录的 jxbrs 常为 ""。"""
    record = make_kb(dm, lx="ks", kcmc="测试课程丙", zwh=12, jxbrs="", jcdm="111213",
                     sknrjj="", teaxms="教师丙", jxcdmc="测试楼C301", kcbh="T0003",
                     kcptbh="90003", kxh="0", pkrs=40)
    record.update(overrides)
    return record


def make_xk(dm, kcmc, qsrq, jsrq, color):
    """构造一条 lx=xk（选课时间段）原始记录，键顺序与真实接口一致。"""
    return {
        "jssj": " 23:00:00", "qsrq": qsrq, "color": color, "bz": "", "dm": dm,
        "jsrq": jsrq, "lx": "xk", "kcmc": kcmc, "qssj": " 12:30:00",
    }


def synthetic_records():
    """10 条记录：6 上课 + 2 考试 + 2 选课时间段。"""
    return [
        # 第 1 周 星期一 08:30-10:05（3 节），与下面 10:00 的考试时间重叠
        make_kb("900001", qssj="08:30:00", jssj="10:05:00", jcdm="010203"),
        # 第 1 周 星期二，钟点故意带前导空格且小时只有一位 -> 应被补成 08:30:00；
        # 教学场地为空 -> 应生成「网页未提供教学场地」提示
        make_kb("900002", kcmc="测试课程乙", teaxms="教师乙", pkrs=20, jxbrs=21,
                qssj=" 8:30:00", jssj=" 10:05:00", qsrq="2026-09-08", jsrq="2026-09-08",
                xq=2, zc=1, jxcdmc="", jxbmc="测试班乙", kcbh="T0002", kcptbh="90002",
                sknrjj="第二讲", jcdm="0102"),
        # 第 2 条考试：与「无场地」的 900002 重叠 -> 同一事件出现两条提示（换行分隔）
        make_ks("900008", kcmc="测试课程己", teaxms="教师庚", jcdm="1112",
                qsrq="2026-09-08", jsrq="2026-09-08", xq=2, zc=1, qssj="09:00:00",
                jssj="10:00:00", jxcdmc="测试楼C302", kcbh="T0008", kcptbh="90008",
                pkrs=25, zwh=7),
        # 考试：与 900001 时间重叠；节次 111213 -> "11、12、13"；座位号取 zwh
        make_ks("900003", qsrq="2026-09-07", jsrq="2026-09-07", xq=1, zc=1,
                qssj="10:00:00", jssj="11:30:00"),
        # 同课同时段平行排课（两个教室/两位教师）-> parallel=True
        make_kb("900004", kcmc="测试课程丁", teaxms="教师丁", jxcdmc="测试楼B201",
                qsrq="2026-09-09", jsrq="2026-09-09", xq=3, qssj="14:00:00",
                jssj="15:35:00", jcdm="0607", kcbh="T0004", kcptbh="90004",
                sknrjj="平行排课"),
        make_kb("900005", kcmc="测试课程丁", teaxms="教师戊", jxcdmc="测试楼B202",
                qsrq="2026-09-09", jsrq="2026-09-09", xq=3, qssj="14:00:00",
                jssj="15:35:00", jcdm="0607", kcbh="T0004", kcptbh="90004",
                sknrjj="平行排课"),
        # 授课内容写明时段 10:20-15:40，排课到 18:00 -> 内容时段不一致提示
        make_kb("900006", kcmc="测试课程戊", teaxms="教师己", jxcdmc="测试楼D401",
                qsrq="2026-09-10", jsrq="2026-09-10", xq=4, qssj="10:20:00",
                jssj="18:00:00", jcdm="0304050607080910", kcbh="T0005", kcptbh="90006",
                sknrjj="实验（上课时间10：20-15：40）"),
        # 第 2 周 星期一：与 900001 同课程同星期，锚点应为同一天（2026-09-07）
        make_kb("900007", qssj="08:30:00", jssj="10:05:00", qsrq="2026-09-14",
                jsrq="2026-09-14", xq=1, zc=2, sknrjj="第三讲", jxcdmc="测试楼A101"),
        make_xk("900010", "测试选课阶段一", "2026-07-05", "2026-07-06", "Green"),
        make_xk("900011", "测试选课阶段二", "2026-07-07", "2026-07-10", "blue"),
    ]


# --------------------------------------------------------------------------
# 断言收集
# --------------------------------------------------------------------------
class Checks:
    def __init__(self):
        self.total = 0
        self.failures = []

    def check(self, condition, label, detail=""):
        self.total += 1
        if condition:
            print("  [PASS] %s" % label)
        else:
            print("  [FAIL] %s%s" % (label, ("  <- " + detail) if detail else ""))
            self.failures.append(label + (("  <- " + detail) if detail else ""))

    def equal(self, actual, expected, label):
        self.check(actual == expected, label, "期望 %r，实际 %r" % (expected, actual))


# --------------------------------------------------------------------------
# 运行被测脚本
# --------------------------------------------------------------------------
def run_normalizer(python, raw_path, out_path, extra_args=()):
    command = [python, NORMALIZER, "--raw", raw_path, "--out", out_path] + list(extra_args)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def write_json(path, payload):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 正向用例
# --------------------------------------------------------------------------
def test_positive(checks, python, tempdir):
    records = synthetic_records()
    raw_path = os.path.join(tempdir, "synthetic_raw.json")
    out_path = os.path.join(tempdir, "synthetic_normalized.json")
    write_json(raw_path, {"status": 200, "url": RAW_WRAPPER_URL, "data": records})

    proc = run_normalizer(python, raw_path, out_path, ["--fetched-at", "2026-10-26"])
    checks.equal(proc.returncode, 0, "正常数据退出码 0")
    if proc.returncode != 0:
        print("     stderr: %s" % proc.stderr.strip()[:2000])
        return
    checks.equal(proc.stderr.strip(), "", "正常数据 stderr 为空")
    checks.check(os.path.isfile(out_path), "正常数据写出了输出文件")

    with open(out_path, encoding="utf-8") as handle:
        data = json.load(handle)

    # ---- 顶层结构 ----
    checks.equal(list(data.keys()), EXPECTED_TOP_KEYS, "顶层键顺序与参考同构")
    checks.equal(data["fetchedAt"], "2026-10-26", "fetchedAt 取 --fetched-at")
    checks.equal(data["source"], "https://jw.example.edu.cn/new/welcome.page?ui=new",
                 "source 默认取教务系统入口页")
    checks.equal(data["semester"], "2026–2027 学年第一学期", "semester 按最早日期推断")
    checks.equal(data["timeZone"], "Asia/Shanghai", "timeZone")

    # ---- 数量 ----
    events = data["events"]
    checks.equal(len(events), 8, "events 数量 = 8（6 上课 + 2 考试）")
    checks.equal(sum(1 for e in events if e["type"] == "上课"), 6, "上课 6 条")
    checks.equal(sum(1 for e in events if e["type"] == "考试"), 2, "考试 2 条")
    checks.equal(len(data["selections"]), 2, "selections 数量 = 2")
    checks.equal(len(data["overlaps"]), 3, "overlaps 数量 = 3")

    # ---- 排序与键顺序 ----
    checks.equal([e["sourceId"] for e in events],
                 ["kb-900001", "ks-900003", "kb-900002", "ks-900008", "kb-900004",
                  "kb-900005", "kb-900006", "kb-900007"],
                 "events 按开始时间排序")
    checks.check(all(list(e.keys()) == EXPECTED_EVENT_KEYS for e in events),
                 "每条 event 键顺序与参考同构")
    checks.check(all(list(s.keys()) == EXPECTED_SELECTION_KEYS for s in data["selections"]),
                 "selections 键顺序与原始记录一致")
    checks.check(all(list(o.keys()) == ["a", "b", "date", "parallel"] for o in data["overlaps"]),
                 "overlaps 键为 a/b/date/parallel")

    by_id = {e["sourceId"]: e for e in events}
    first = by_id["kb-900001"]
    # ---- 字段映射 ----
    checks.equal(first["type"], "上课", "type=上课")
    checks.equal(first["title"], "测试课程甲", "上课 title=课程名")
    checks.equal(first["course"], "测试课程甲", "course=kcmc")
    checks.equal(first["date"], "2026-09-07", "date=qsrq")
    checks.equal(first["start"], "2026-09-07 08:30:00", "start=<qsrq> <qssj>")
    checks.equal(first["end"], "2026-09-07 10:05:00", "end=<jsrq> <jssj>")
    checks.equal(first["week"], 1, "week=int(zc)")
    checks.equal(first["weekday"], "星期一", "weekday：xq=1 -> 星期一")
    checks.equal(first["periods"], "1、2、3", "periods：010203 -> 1、2、3")
    checks.equal(by_id["kb-900006"]["periods"], "3、4、5、6、7、8、9、10",
                 "periods：0304…0910 -> 3…10")
    checks.equal(by_id["ks-900003"]["periods"], "11、12、13", "periods：111213 -> 11、12、13")
    checks.equal(by_id["kb-900002"]["start"], "2026-09-08 08:30:00",
                 "qssj ' 8:30:00' 去空格并补成 08:30:00")
    checks.equal(by_id["kb-900002"]["weekday"], "星期二", "weekday：xq=2 -> 星期二")
    checks.equal(by_id["kb-900004"]["weekday"], "星期三", "weekday：xq=3 -> 星期三")
    checks.equal(by_id["kb-900006"]["weekday"], "星期四", "weekday：xq=4 -> 星期四")
    checks.equal(first["teacher"], "教师甲", "teacher=teaxms")
    checks.equal(first["campus"], "测试校区", "campus=xqmc")
    checks.equal(first["location"], "测试楼A101", "location=jxcdmc")
    checks.equal(first["phase"], "理论教学", "phase=jxhjmc")
    checks.equal(first["form"], "线下教学", "form=jxxsmc")
    checks.equal(first["teachingClass"], "测试班甲", "teachingClass=jxbmc")
    checks.equal(first["assessment"], "考试", "assessment=khfsmc")
    checks.equal(first["courseCode"], "T0001", "courseCode=kcbh")
    checks.equal(first["platformCourseCode"], "90001", "platformCourseCode=kcptbh")
    checks.equal(first["sequence"], "1", "sequence=kxh（字符串）")
    checks.equal(first["scheduledStudents"], 30, "scheduledStudents=pkrs（数字）")
    checks.equal(first["enrolledStudents"], 30, "enrolledStudents=jxbrs（数字）")
    checks.equal(first["seat"], None, "上课记录 seat 一律 null")
    checks.equal(first["sourceRemark"], "", "sourceRemark=bz")
    checks.equal(first["semester"], "2026–2027 学年第一学期", "event.semester")
    checks.equal(first["timeZone"], "Asia/Shanghai", "event.timeZone")

    # ---- 考试专有 ----
    exam = by_id["ks-900003"]
    checks.equal(exam["title"], "[考试] 测试课程丙", "考试 title=[考试] 课程名")
    checks.equal(exam["seat"], 12, "考试记录 seat=zwh")
    checks.equal(exam["enrolledStudents"], "", "考试记录 enrolledStudents 保持原始空串")
    checks.equal(exam["content"], "", "考试记录 content 为空串")

    # ---- notes 文案逐字比对 ----
    expected_notes = {
        "kb-900001": "时间重叠：考试 测试课程丙 10:00–11:30（源记录 900003）。",
        "ks-900003": "时间重叠：测试课程甲 08:30–10:05（源记录 900001）。",
        "kb-900002": "时间重叠：考试 测试课程己 09:00–10:00（源记录 900008）。\n" + NOTE_LOCATION,
        "ks-900008": "时间重叠：测试课程乙 08:30–10:05（源记录 900002）。",
        "kb-900004": "同课同时段另有排课：测试楼B202／教师戊（源记录 900005）；保留两条。",
        "kb-900005": "同课同时段另有排课：测试楼B201／教师丁（源记录 900004）；保留两条。",
        "kb-900006": "授课内容注明 10:20–15:40，排课时间为 10:20–18:00；源时间不一致，均保留。",
        "kb-900007": "",
    }
    for source_id, text in expected_notes.items():
        checks.equal(by_id[source_id]["notes"], text, "notes 文案：%s" % source_id)
    checks.equal(sum(1 for e in events if e["notes"]), 7, "非空 notes 事件数 = 7")
    checks.equal(by_id["kb-900002"]["notes"].count("\n"), 1,
                 "多条提示用换行分隔（单事件 2 行 notes）")
    checks.equal(sum(len(e["notes"].split("\n")) for e in events if e["notes"]), 8,
                 "notes 总行数 = 8")

    # ---- overlaps 内容 ----
    checks.equal(data["overlaps"][0],
                 {"a": "900001", "b": "900003", "date": "2026-09-07", "parallel": False},
                 "overlaps[0]：普通时间重叠，a/b 用原始 dm")
    checks.equal(data["overlaps"][1],
                 {"a": "900002", "b": "900008", "date": "2026-09-08", "parallel": False},
                 "overlaps[1]：普通时间重叠（无场地课程 + 考试）")
    checks.equal(data["overlaps"][2],
                 {"a": "900004", "b": "900005", "date": "2026-09-09", "parallel": True},
                 "overlaps[2]：同课同时段 -> parallel=True")
    checks.check([o["date"] for o in data["overlaps"]] == sorted(o["date"] for o in data["overlaps"]),
                 "overlaps 按日期升序")

    # ---- selections 原样保留 ----
    checks.equal(data["selections"][0],
                 {"jssj": " 23:00:00", "qsrq": "2026-07-05", "color": "Green", "bz": "",
                  "dm": "900010", "jsrq": "2026-07-06", "lx": "xk",
                  "kcmc": "测试选课阶段一", "qssj": " 12:30:00"},
                 "selections 原样保留（不 trim 前后空格）")

    # ---- 校验报告 ----
    checks.check("校验：通过" in proc.stdout, "报告显示校验通过")
    checks.check("日期-星期 8/8" in proc.stdout, "报告显示日期-星期 8/8 通过")
    checks.check("第 1 周起点：2026-09-07（星期一）" in proc.stdout,
                 "报告推断第 1 周起点 2026-09-07（星期一）")
    checks.check("events：8 条（上课 6 / 考试 2）" in proc.stdout, "报告 events 数量")
    checks.check("selections：2 条" in proc.stdout, "报告 selections 数量")
    checks.check("overlaps：3 对" in proc.stdout, "报告 overlaps 数量")
    checks.check("notes：7 条非空" in proc.stdout, "报告 notes 数量")

    # ---- 纯数组输入 + 显式参数 ----
    array_raw = os.path.join(tempdir, "synthetic_array.json")
    array_out = os.path.join(tempdir, "synthetic_array_out.json")
    write_json(array_raw, records)
    proc2 = run_normalizer(python, array_raw, array_out,
                           ["--semester", "2099–2100 学年第一学期",
                            "--source", "https://jw.example.edu/calendar",
                            "--fetched-at", "2026-10-26"])
    checks.equal(proc2.returncode, 0, "纯数组输入退出码 0")
    if proc2.returncode == 0:
        with open(array_out, encoding="utf-8") as handle:
            data2 = json.load(handle)
        checks.equal(data2["semester"], "2099–2100 学年第一学期", "--semester 覆盖生效")
        checks.equal(data2["source"], "https://jw.example.edu/calendar", "--source 覆盖生效")
        checks.equal(len(data2["events"]), 8, "纯数组输入 events 数量一致")


# --------------------------------------------------------------------------
# 反向用例：硬校验失败必须退出码 2 且不写输出
# --------------------------------------------------------------------------
def test_negative(checks, python, tempdir):
    cases = [
        ("星期不一致", {"xq": 2}, "接口 xq 对应"),
        ("结束不晚于开始", {"jssj": "08:00:00"}, "结束时间不晚于开始时间"),
        ("周次锚点不一致", {"zc": 3}, "不恒定"),
    ]
    for index, (label, override, expect_text) in enumerate(cases):
        records = synthetic_records()
        records[0] = dict(records[0], **override)
        raw_path = os.path.join(tempdir, "bad_%d_raw.json" % index)
        out_path = os.path.join(tempdir, "bad_%d_out.json" % index)
        write_json(raw_path, records)                     # 纯数组形态
        if os.path.exists(out_path):
            os.remove(out_path)
        proc = run_normalizer(python, raw_path, out_path, ["--fetched-at", "2026-10-26"])
        checks.equal(proc.returncode, 2, "校验失败（%s）退出码 2" % label)
        checks.check(expect_text in proc.stderr, "校验失败（%s）报错含「%s」" % (label, expect_text),
                     proc.stderr.strip()[:300])
        checks.check(not os.path.exists(out_path), "校验失败（%s）不写出输出文件" % label)
        # 同样数据加 --no-checks 应放行
        proc_skip = run_normalizer(python, raw_path, out_path,
                                   ["--no-checks", "--fetched-at", "2026-10-26"])
        checks.equal(proc_skip.returncode, 0, "校验失败（%s）+ --no-checks 退出码 0" % label)
        checks.check("校验：已跳过" in proc_skip.stdout, "校验失败（%s）+ --no-checks 报告注明已跳过" % label)
        checks.check(os.path.exists(out_path), "校验失败（%s）+ --no-checks 正常写出" % label)


# --------------------------------------------------------------------------
# 链路自测：合成 normalized -> build_artifacts -> xlsx / csv / ics
# --------------------------------------------------------------------------
def test_artifact_chain(checks, python, tempdir):
    normalized = os.path.join(tempdir, "synthetic_normalized.json")
    if not os.path.isfile(BUILDER):
        checks.check(False, "找到 build_artifacts.py", BUILDER)
        return
    if not os.path.isfile(normalized):
        checks.check(False, "合成 normalized 存在（链路前置失败）", normalized)
        return

    outdir = os.path.join(tempdir, "artifacts")
    os.makedirs(outdir, exist_ok=True)
    command = [python, BUILDER, "--normalized", normalized,
               "--outdir", outdir, "--stem", "自测"]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)
    checks.equal(proc.returncode, 0, "build_artifacts 退出码 0")
    if proc.returncode != 0:
        print("     stdout: %s" % proc.stdout.strip()[-1500:])
        print("     stderr: %s" % proc.stderr.strip()[-1500:])
        return
    checks.check("FAIL 0" in proc.stdout, "build_artifacts 内置自检全部通过")

    xlsx = os.path.join(outdir, "自测_详细课表.xlsx")
    csv_path = os.path.join(outdir, "自测_飞书导入.csv")
    ics = os.path.join(outdir, "自测_北京时间.ics")
    for label, path in (("xlsx", xlsx), ("csv", csv_path), ("ics", ics)):
        checks.check(os.path.isfile(path) and os.path.getsize(path) > 0, "生成 %s 且非空" % label)

    with open(normalized, encoding="utf-8") as handle:
        data = json.load(handle)
    expected = len(data["events"])

    if os.path.isfile(csv_path):
        import csv as csv_module
        with open(csv_path, encoding="utf-8", newline="") as handle:
            rows = list(csv_module.reader(handle))
        checks.equal(len(rows) - 1, expected, "CSV 数据行数 = 事件数")
        checks.equal(len(rows[0]), 27, "CSV 27 列")
    if os.path.isfile(ics):
        with open(ics, "rb") as handle:
            raw = handle.read().decode("utf-8")
        checks.equal(raw.count("BEGIN:VEVENT"), expected, "ICS VEVENT 数 = 事件数")
        checks.check(max(len(line.encode("utf-8")) for line in raw.split("\r\n")) <= 75,
                     "ICS 物理行 ≤75 字节")
    if os.path.isfile(xlsx):
        with open(xlsx, "rb") as handle:
            checks.check(handle.read(2) == b"PK", "xlsx 是合法 zip 容器")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="jw_normalize.py 离线自测（合成数据）")
    parser.add_argument("--python", default=sys.executable,
                        help="运行被测脚本的解释器（默认当前解释器）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = parse_args(argv)
    checks = Checks()
    print("== jw_normalize 自测（合成数据，不含真实课表） ==")
    print("被测脚本：%s" % NORMALIZER)
    print("解释器：%s" % args.python)

    if not os.path.isfile(NORMALIZER):
        print("[FAIL] 找不到被测脚本：%s" % NORMALIZER)
        return 2
    if not os.path.isfile(args.python) and os.sep in args.python:
        print("[FAIL] 解释器不存在：%s" % args.python)
        return 2

    tempdir = tempfile.mkdtemp(prefix="jw_normalize_selftest_")
    keep = args.keep
    try:
        print("\n-- 正向用例 --")
        test_positive(checks, args.python, tempdir)
        print("\n-- 链路自测（normalize -> xlsx/csv/ics） --")
        test_artifact_chain(checks, args.python, tempdir)
        print("\n-- 反向用例（硬校验） --")
        test_negative(checks, args.python, tempdir)
    finally:
        if keep:
            print("\n临时目录已保留：%s" % tempdir)
        else:
            import shutil
            shutil.rmtree(tempdir, ignore_errors=True)

    passed = checks.total - len(checks.failures)
    print("\n== 汇总 ==")
    if checks.failures:
        print("FAIL：%d/%d 项通过，%d 项失败" % (passed, checks.total, len(checks.failures)))
        for item in checks.failures:
            print("  - %s" % item)
        return 1
    print("PASS：%d/%d 项全部通过" % (passed, checks.total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
